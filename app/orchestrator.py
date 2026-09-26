"""Chain-of-Agents Orchestrator.

The orchestrator does no domain work. It owns the `SharedContext` (the shared memory),
decides the next step, runs one agent at a time, merges the section that agent returns and
emits a trace event before and after every step — that stream is what the UI renders live.

Reasoning is split in two kinds, and each step uses the cheapest one that does the job:
  * generative (local Qwen via Ollama): research, the itinerary (once), the final narrative;
  * non-generative (decision engine: rules today, Jev-ready): interest classification,
    choosing budget cuts, validating the revised plan; plus plain Python for arithmetic.

Control flow is deterministic:

    intake → research → itinerary → budget → [conflict → revise → budget]×≤2 → synthesis
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .agents import budget as budget_agent
from .agents import conflict_resolution, destination_research, interests, itinerary_planning, synthesis
from .decisions import DecisionEngine
from .guardrails import TokenBudget, TokenBudgetExceeded
from .llm_client import LLMClient, LLMError
from .models import AgentName, DecisionRecord, SharedContext, TokenUsage, TraceEntry, TraceEvent

log = logging.getLogger("trip_planner.orchestrator")

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChainError(Exception):
    """The chain cannot produce an itinerary (a mandatory step failed)."""


class Orchestrator:
    def __init__(self, llm: LLMClient, engine: DecisionEngine, token_limit: int, max_conflict_iterations: int = 2):
        self.llm = llm
        self.engine = engine
        self.token_limit = token_limit
        self.max_conflict_iterations = max_conflict_iterations

    async def run(self, ctx: SharedContext, emit: Emit) -> SharedContext:
        budget = TokenBudget(self.token_limit)

        async def trace(agent: AgentName, event: TraceEvent, **extra) -> None:
            entry = TraceEntry(agent=agent, event=event, **extra)
            ctx.trace.append(entry)
            await emit("trace", entry.model_dump(exclude_none=True))

        async def decision(agent: AgentName, record: DecisionRecord) -> None:
            await trace(agent, "decision", decision=record)

        async def section(key: str) -> None:
            value = getattr(ctx, key)
            await emit("section", {"key": key, "value": value.model_dump() if value is not None else None})

        def progress(agent: str) -> Callable[[int], Awaitable[None]]:
            async def cb(tokens: int) -> None:
                await emit("progress", {"agent": agent, "tokens": tokens})
            return cb

        def add_usage(agent: str, usage: TokenUsage) -> None:
            total = ctx.token_usage.setdefault(agent, TokenUsage())
            total.input_tokens += usage.input_tokens
            total.output_tokens += usage.output_tokens

        async def step(agent: AgentName, call: Callable[[], Awaitable[Any]], label: str | None = None) -> Any:
            await trace(agent, "started", message=label)
            t0 = time.perf_counter()
            try:
                result, usage = await call()
            except (LLMError, TokenBudgetExceeded) as exc:
                await trace(agent, "failed", duration_ms=_ms(t0), message=str(exc))
                raise
            add_usage(agent, usage)
            await trace(agent, "completed", duration_ms=_ms(t0), tokens=usage.total, message=label)
            return result

        async def run_budget(label: str | None = None) -> None:
            await trace("budget", "started", message=label)
            t0 = time.perf_counter()
            ctx.budget_analysis = budget_agent.analyze(ctx)
            await trace("budget", "completed", duration_ms=_ms(t0), tokens=0, message=label)
            await section("budget_analysis")

        await trace("orchestrator", "plan_created", message=(
            f"intake → research → itinerary → budget → [conflict loop ≤{self.max_conflict_iterations}] → synthesis"
            f" · llm={self.llm.model} · decisions={self.engine.name} · {self.token_limit} tokens"))

        # 0. Intake: typed decisions, no generation.
        ctx.interest_profile, records = await interests.classify(ctx, self.engine)
        for rec in records:
            await decision("orchestrator", rec)
        await section("interest_profile")

        # 1-2. Mandatory generative steps: without research and a draft there is nothing to show.
        try:
            ctx.destination_research = await step("destination_research", lambda: destination_research.run(
                ctx, self.llm, budget, on_progress=progress("destination_research")))
            await section("destination_research")
            ctx.itinerary_draft = await step("itinerary_planning", lambda: itinerary_planning.run(
                ctx, self.llm, budget, on_progress=progress("itinerary_planning")))
            await section("itinerary_draft")
        except (LLMError, TokenBudgetExceeded) as exc:
            raise ChainError(str(exc)) from exc

        # 3. Deterministic budget check.
        await run_budget()

        # 4. Bounded conflict loop — decisions + arithmetic, no generation.
        conflict = ctx.conflict_resolution
        while ctx.budget_analysis.over_budget_by_usd > 0 and conflict.iterations < self.max_conflict_iterations:
            await trace("orchestrator", "conflict_detected",
                        message=f"over budget by ${ctx.budget_analysis.over_budget_by_usd:,.2f}")
            conflict.triggered = True
            conflict.iterations += 1
            label = f"iteration {conflict.iterations}/{self.max_conflict_iterations}"

            await trace("conflict_resolution", "started", message=label)
            t0 = time.perf_counter()
            plan = await conflict_resolution.run(ctx, self.engine)
            for rec in plan.decisions:
                await decision("conflict_resolution", rec)
            conflict.decisions.extend(plan.decisions)
            if not plan.action_ids:
                await trace("conflict_resolution", "skipped", duration_ms=_ms(t0),
                            message="no actions left in the catalog")
                break
            conflict.applied_action_ids.extend(plan.action_ids)
            conflict.actions_taken.extend(plan.descriptions)
            await trace("conflict_resolution", "completed", duration_ms=_ms(t0), tokens=0,
                        message=f"{label}: {', '.join(plan.action_ids)} (≈ −${plan.expected_savings_usd:,.0f})")
            await section("conflict_resolution")

            ctx.itinerary_draft = await step(
                "itinerary_planning", lambda: _sync(itinerary_planning.revise(ctx, plan.action_ids)),
                f"revision {conflict.iterations} (applied by code, no LLM)")
            await section("itinerary_draft")
            await decision("orchestrator", await interests.plan_matches(ctx, self.engine))
            await run_budget(f"re-check {conflict.iterations}")

        if conflict.triggered:
            conflict.resolved = ctx.budget_analysis.within_budget
            await section("conflict_resolution")

        # 5. Synthesis, streamed token by token (template fallback if the model can't run).
        async def on_token(text: str) -> None:
            await emit("token", {"agent": "synthesis", "text": text})

        try:
            ctx.final_itinerary = await step("synthesis", lambda: synthesis.run(ctx, self.llm, budget, on_token))
        except (LLMError, TokenBudgetExceeded):
            ctx.final_itinerary = synthesis.build_final(ctx, synthesis.fallback_summary(ctx))
            await trace("synthesis", "completed", message="template fallback (no LLM)", tokens=0)
        await section("final_itinerary")

        await trace("orchestrator", "finished",
                    message="within budget" if ctx.final_itinerary.within_budget else "still over budget",
                    tokens=ctx.total_tokens)
        log.info("session=%s tokens=%s total=%d within_budget=%s", ctx.session_id,
                 {k: v.total for k, v in ctx.token_usage.items()}, ctx.total_tokens,
                 ctx.final_itinerary.within_budget)
        return ctx


async def _sync(value):
    return value


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
