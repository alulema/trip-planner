"""Chain-of-Agents Orchestrator.

The orchestrator does no domain work. It owns the `SharedContext` (the shared memory),
decides the next step, runs one agent at a time, merges the section that agent returns and
emits a trace event before and after every step — that stream is what the UI renders live.

Control flow is deterministic (cheaper and easier to debug than an LLM controller):

    research → itinerary → budget → [conflict → itinerary(revision) → budget] ×≤2 → synthesis
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from .agents import budget as budget_agent
from .agents import conflict_resolution, destination_research, itinerary_planning, synthesis
from .guardrails import TokenBudget, TokenBudgetExceeded
from .llm_client import LLMClient, LLMError
from .models import AgentName, SharedContext, TokenUsage, TraceEntry, TraceEvent

log = logging.getLogger("trip_planner.orchestrator")

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChainError(Exception):
    """The chain cannot produce an itinerary (a mandatory step failed)."""


class Orchestrator:
    def __init__(self, llm: LLMClient, token_limit: int, max_conflict_iterations: int = 2):
        self.llm = llm
        self.token_limit = token_limit
        self.max_conflict_iterations = max_conflict_iterations

    async def run(self, ctx: SharedContext, emit: Emit) -> SharedContext:
        budget = TokenBudget(self.token_limit)

        async def trace(agent: AgentName, event: TraceEvent, **extra) -> None:
            entry = TraceEntry(agent=agent, event=event, **extra)
            ctx.trace.append(entry)
            await emit("trace", entry.model_dump(exclude_none=True))

        async def section(key: str) -> None:
            value = getattr(ctx, key)
            await emit("section", {"key": key, "value": value.model_dump() if value is not None else None})

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

        loop_note = f"max {self.max_conflict_iterations} conflict iterations, {self.token_limit} tokens"
        await trace("orchestrator", "plan_created",
                    message=f"research → itinerary → budget → [conflict loop] → synthesis ({loop_note})")

        # 1-2. Mandatory steps: without research and a draft there is nothing to show.
        try:
            ctx.destination_research = await step(
                "destination_research", lambda: destination_research.run(ctx, self.llm, budget))
            await section("destination_research")
            ctx.itinerary_draft = await step(
                "itinerary_planning", lambda: itinerary_planning.run(ctx, self.llm, budget))
            await section("itinerary_draft")
        except (LLMError, TokenBudgetExceeded) as exc:
            raise ChainError(str(exc)) from exc

        # 3. Deterministic budget check.
        await run_budget()

        # 4. Bounded conflict loop.
        conflict = ctx.conflict_resolution
        while ctx.budget_analysis.over_budget_by_usd > 0 and conflict.iterations < self.max_conflict_iterations:
            await trace("orchestrator", "conflict_detected",
                        message=f"over budget by ${ctx.budget_analysis.over_budget_by_usd:,.2f}")
            conflict.triggered = True
            conflict.iterations += 1
            label = f"iteration {conflict.iterations}/{self.max_conflict_iterations}"
            try:
                actions = await step("conflict_resolution",
                                     lambda: conflict_resolution.run(ctx, self.llm, budget), label)
                conflict.actions_taken.extend(actions)
                await section("conflict_resolution")
                ctx.itinerary_draft = await step(
                    "itinerary_planning",
                    lambda: itinerary_planning.run(ctx, self.llm, budget, revision_actions=actions),
                    f"revision {conflict.iterations}")
                await section("itinerary_draft")
            except (LLMError, TokenBudgetExceeded) as exc:
                # Keep the last valid draft and be honest about it instead of failing.
                await trace("orchestrator", "skipped", message=f"conflict loop stopped: {exc}")
                break
            await run_budget(f"re-check {conflict.iterations}")

        if conflict.triggered:
            conflict.resolved = ctx.budget_analysis.within_budget
            await section("conflict_resolution")

        # 5. Synthesis (falls back to a deterministic summary if the LLM can't run).
        try:
            ctx.final_itinerary = await step("synthesis", lambda: synthesis.run(ctx, self.llm, budget))
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


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
