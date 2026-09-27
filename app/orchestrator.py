"""Chain-of-Agents Orchestrator, built as a LangGraph state graph.

The orchestrator does no domain work. It owns the `SharedContext` (the shared memory, and
the graph state), decides the next step and runs one agent per node; each node returns only
the section it produced and LangGraph merges it (trace entries and token usage accumulate
through reducers declared on the model). Every node reports what it does as it happens —
trace, section, progress and token events — through LangGraph's custom stream; that stream
is what the UI renders live.

Reasoning is split in two kinds, and each step uses the cheapest one that does the job:
  * generative (local Qwen via Ollama): research, the itinerary (once), the final narrative;
  * non-generative (decision engine: rules today, Jev-ready): interest classification,
    choosing budget cuts, validating the revised plan; plus plain Python for arithmetic.

Control flow is deterministic — the routing functions are plain code, not an LLM:

    intake → destination_research → itinerary_planning → budget
      budget ──(over budget, iterations left)──▶ conflict_resolution
      conflict_resolution ──(actions chosen)──▶ revise_itinerary → budget
      conflict_resolution ──(catalog exhausted)──▶ synthesis
      budget ──(fits, or loop cap reached)──▶ synthesis → finish
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

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


@dataclass
class Deps:
    """Per-run dependencies handed to every node (LangGraph runtime context, not state)."""

    llm: LLMClient
    engine: DecisionEngine
    budget: TokenBudget
    token_limit: int
    max_conflict_iterations: int


# --------------------------------------------------------------------------- node helpers


class Reporter:
    """Collects a node's trace entries (returned as its state update) and streams every
    event to the client as it happens."""

    def __init__(self) -> None:
        self._write = get_stream_writer()
        self.trace: list[TraceEntry] = []

    def event(self, kind: str, data: dict[str, Any]) -> None:
        self._write({"event": kind, "data": data})

    def trace_event(self, agent: AgentName, event: TraceEvent, **extra) -> None:
        entry = TraceEntry(agent=agent, event=event, **extra)
        self.trace.append(entry)
        self.event("trace", entry.model_dump(exclude_none=True))

    def decision(self, agent: AgentName, record: DecisionRecord) -> None:
        self.trace_event(agent, "decision", decision=record)

    def section(self, key: str, value: Any) -> None:
        self.event("section", {"key": key, "value": value.model_dump() if value is not None else None})

    def progress(self, agent: str) -> Callable[[int], Awaitable[None]]:
        async def cb(tokens: int) -> None:
            self.event("progress", {"agent": agent, "tokens": tokens})
        return cb

    async def step(self, agent: AgentName, call: Callable[[], Awaitable[Any]], label: str | None = None,
                   describe: Callable[[Any], str] | None = None) -> tuple[Any, TokenUsage]:
        self.trace_event(agent, "started", message=label)
        t0 = time.perf_counter()
        try:
            result, usage = await call()
        except (LLMError, TokenBudgetExceeded) as exc:
            self.trace_event(agent, "failed", duration_ms=_ms(t0), message=str(exc))
            raise
        self.trace_event(agent, "completed", duration_ms=_ms(t0), tokens=usage.total,
                         message=describe(result) if describe else label)
        return result, usage


# --------------------------------------------------------------------------- nodes


async def intake(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    deps, rep = runtime.context, Reporter()
    rep.trace_event("orchestrator", "plan_created", message=(
        f"intake → research → itinerary → budget → [conflict loop ≤{deps.max_conflict_iterations}] → synthesis"
        f" · llm={deps.llm.model} · decisions={deps.engine.name} · {deps.token_limit} tokens · langgraph"))
    profile, records = await interests.classify(state, deps.engine)
    for rec in records:
        rep.decision("orchestrator", rec)
    rep.section("interest_profile", profile)
    return {"interest_profile": profile, "trace": rep.trace}


async def research(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    deps, rep = runtime.context, Reporter()
    try:
        result, usage = await rep.step(
            "destination_research",
            lambda: destination_research.run(state, deps.llm, deps.budget,
                                             on_progress=rep.progress("destination_research")),
            describe=lambda r: ("source: catalog (areas, highlights, costs) + model (season)"
                                if r.source == "catalog" else "source: model (not in catalog)"))
    except (LLMError, TokenBudgetExceeded) as exc:
        # Mandatory step: without research there is nothing to show.
        raise ChainError(str(exc)) from exc
    rep.section("destination_research", result)
    return {"destination_research": result, "token_usage": {"destination_research": usage}, "trace": rep.trace}


async def plan_itinerary(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    deps, rep = runtime.context, Reporter()
    try:
        draft, usage = await rep.step(
            "itinerary_planning",
            lambda: itinerary_planning.run(state, deps.llm, deps.budget,
                                           on_progress=rep.progress("itinerary_planning")))
    except (LLMError, TokenBudgetExceeded) as exc:
        raise ChainError(str(exc)) from exc
    rep.section("itinerary_draft", draft)
    return {"itinerary_draft": draft, "token_usage": {"itinerary_planning": usage}, "trace": rep.trace}


async def check_budget(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    rep = Reporter()
    n = state.conflict_resolution.iterations
    label = f"re-check {n}" if n else None
    rep.trace_event("budget", "started", message=label)
    t0 = time.perf_counter()
    analysis = budget_agent.analyze(state)
    rep.trace_event("budget", "completed", duration_ms=_ms(t0), tokens=0, message=label)
    rep.section("budget_analysis", analysis)
    return {"budget_analysis": analysis, "trace": rep.trace}


async def resolve_conflict(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    """Decisions + arithmetic, no generation: pick preset cuts for the current overrun."""
    deps, rep = runtime.context, Reporter()
    rep.trace_event("orchestrator", "conflict_detected",
                    message=f"over budget by ${state.budget_analysis.over_budget_by_usd:,.2f}")
    prev = state.conflict_resolution
    iteration = prev.iterations + 1
    label = f"iteration {iteration}/{deps.max_conflict_iterations}"

    rep.trace_event("conflict_resolution", "started", message=label)
    t0 = time.perf_counter()
    plan = await conflict_resolution.run(state, deps.engine)
    for rec in plan.decisions:
        rep.decision("conflict_resolution", rec)
    conflict = prev.model_copy(update={
        "triggered": True,
        "iterations": iteration,
        "decisions": [*prev.decisions, *plan.decisions],
        "applied_action_ids": [*prev.applied_action_ids, *plan.action_ids],
        "actions_taken": [*prev.actions_taken, *plan.descriptions],
        "last_action_ids": plan.action_ids,
    })
    if plan.action_ids:
        rep.trace_event("conflict_resolution", "completed", duration_ms=_ms(t0), tokens=0,
                        message=f"{label}: {', '.join(plan.action_ids)} (≈ −${plan.expected_savings_usd:,.0f})")
        rep.section("conflict_resolution", conflict)
    else:
        rep.trace_event("conflict_resolution", "skipped", duration_ms=_ms(t0),
                        message="no actions left in the catalog")
    return {"conflict_resolution": conflict, "trace": rep.trace}


async def revise_itinerary(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    """Apply the chosen cuts by code (no LLM), then validate the plan with a decision."""
    deps, rep = runtime.context, Reporter()
    conflict = state.conflict_resolution
    draft, usage = await rep.step(
        "itinerary_planning", lambda: _sync(itinerary_planning.revise(state, conflict.last_action_ids)),
        f"revision {conflict.iterations} (applied by code, no LLM)")
    rep.section("itinerary_draft", draft)
    revised = state.model_copy(update={"itinerary_draft": draft})
    rep.decision("orchestrator", await interests.plan_matches(revised, deps.engine))
    return {"itinerary_draft": draft, "trace": rep.trace}


async def synthesize(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    """Narrative streamed token by token; template fallback if the model can't run."""
    deps, rep = runtime.context, Reporter()
    update: dict[str, Any] = {}
    conflict = state.conflict_resolution
    if conflict.triggered:
        conflict = conflict.model_copy(update={"resolved": state.budget_analysis.within_budget})
        rep.section("conflict_resolution", conflict)
        update["conflict_resolution"] = conflict
        state = state.model_copy(update={"conflict_resolution": conflict})

    async def on_token(text: str) -> None:
        rep.event("token", {"agent": "synthesis", "text": text})

    try:
        final, usage = await rep.step("synthesis", lambda: synthesis.run(state, deps.llm, deps.budget, on_token))
        update["token_usage"] = {"synthesis": usage}
    except (LLMError, TokenBudgetExceeded):
        final = synthesis.build_final(state, synthesis.fallback_summary(state))
        rep.trace_event("synthesis", "completed", message="template fallback (no LLM)", tokens=0)
    rep.section("final_itinerary", final)
    return {**update, "final_itinerary": final, "trace": rep.trace}


async def finish(state: SharedContext, runtime: Runtime[Deps]) -> dict:
    rep = Reporter()
    rep.trace_event("orchestrator", "finished",
                    message="within budget" if state.final_itinerary.within_budget else "still over budget",
                    tokens=state.total_tokens)
    log.info("session=%s tokens=%s total=%d within_budget=%s", state.session_id,
             {k: v.total for k, v in state.token_usage.items()}, state.total_tokens,
             state.final_itinerary.within_budget)
    return {"trace": rep.trace}


# --------------------------------------------------------------------------- routing (plain code)


def after_budget(state: SharedContext, runtime: Runtime[Deps]) -> str:
    over = state.budget_analysis.over_budget_by_usd > 0
    if over and state.conflict_resolution.iterations < runtime.context.max_conflict_iterations:
        return "conflict_resolution"
    return "synthesis"


def after_conflict(state: SharedContext) -> str:
    return "revise_itinerary" if state.conflict_resolution.last_action_ids else "synthesis"


def build_graph():
    g = StateGraph(SharedContext, context_schema=Deps)
    g.add_node("intake", intake)
    g.add_node("destination_research", research)
    g.add_node("itinerary_planning", plan_itinerary)
    g.add_node("budget", check_budget)
    g.add_node("conflict_resolution", resolve_conflict)
    g.add_node("revise_itinerary", revise_itinerary)
    g.add_node("synthesis", synthesize)
    g.add_node("finish", finish)

    g.add_edge(START, "intake")
    g.add_edge("intake", "destination_research")
    g.add_edge("destination_research", "itinerary_planning")
    g.add_edge("itinerary_planning", "budget")
    g.add_conditional_edges("budget", after_budget, ["conflict_resolution", "synthesis"])
    g.add_conditional_edges("conflict_resolution", after_conflict, ["revise_itinerary", "synthesis"])
    g.add_edge("revise_itinerary", "budget")
    g.add_edge("synthesis", "finish")
    g.add_edge("finish", END)
    return g.compile(name="trip-planner")


GRAPH = build_graph()


def graph_mermaid() -> str:
    """The chain as a Mermaid diagram, generated from the graph itself."""
    return GRAPH.get_graph().draw_mermaid()


# --------------------------------------------------------------------------- entry point


class Orchestrator:
    def __init__(self, llm: LLMClient, engine: DecisionEngine, token_limit: int, max_conflict_iterations: int = 2):
        self.llm = llm
        self.engine = engine
        self.token_limit = token_limit
        self.max_conflict_iterations = max_conflict_iterations

    async def run(self, ctx: SharedContext, emit: Emit) -> SharedContext:
        """Run the graph, forwarding every streamed event to `emit`. The final state is
        copied back into `ctx`, so callers keep working with the object they passed in."""
        deps = Deps(self.llm, self.engine, TokenBudget(self.token_limit), self.token_limit,
                    self.max_conflict_iterations)
        final: dict | None = None
        async for mode, chunk in GRAPH.astream(ctx, context=deps, stream_mode=["custom", "values"]):
            if mode == "custom":
                await emit(chunk["event"], chunk["data"])
            else:
                final = chunk
        result = SharedContext.model_validate(final)
        for field in SharedContext.model_fields:
            setattr(ctx, field, getattr(result, field))
        return ctx


async def _sync(value):
    return value


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
