"""4. Conflict Resolution Agent — proposes concrete cuts when the plan is over budget.

Its actions are fed back into the Itinerary Planning Agent. The orchestrator bounds the
Itinerary↔Budget↔Conflict loop (max 2 iterations)."""

from __future__ import annotations

from ..guardrails import TokenBudget
from ..llm_client import LLMClient
from ..models import ConflictResolutionOutput, SharedContext, TokenUsage
from . import compact, language_rule

SYSTEM = """You are the conflict resolution agent in a multi-agent trip planner.
The proposed itinerary exceeds the traveller's budget. Identify the 2-3 most effective actions
to reduce cost without sacrificing the traveller's main interests (e.g. replace a paid activity
with a free one, choose cheaper lodging, eat more street food / markets instead of restaurants).
Look at the breakdown: target the biggest cost lines first. Each action must be specific and
actionable, one sentence, and ideally state the expected saving in USD — it is forwarded to the
itinerary agent to apply."""


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget) -> tuple[list[str], TokenUsage]:
    req = ctx.user_request
    draft, analysis = ctx.itinerary_draft, ctx.budget_analysis
    assert draft is not None and analysis is not None
    user = compact({
        "budget_usd": req.budget_usd,
        "travelers": req.travelers,
        "interests": req.interests,
        "estimated_total_usd": analysis.estimated_total_usd,
        "over_budget_by_usd": analysis.over_budget_by_usd,
        "breakdown": analysis.breakdown.model_dump(),
        "cost_assumptions": draft.cost_assumptions.model_dump(),
        "days": [{"day": d.day, "activities": d.activities, "cost": d.estimated_cost_usd} for d in draft.days],
        "reference_costs": ctx.destination_research.reference_costs.model_dump(),  # type: ignore[union-attr]
        "previous_actions": ctx.conflict_resolution.actions_taken,
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete(
        agent="conflict_resolution",
        system=SYSTEM,
        user=user,
        output_model=ConflictResolutionOutput,
        max_tokens=400,
        budget=budget,
        mock=lambda: _mock(ctx),
    )
    actions = [a.strip() for a in out.actions_taken if a.strip()][:3]
    return actions, usage


def _mock(ctx: SharedContext) -> ConflictResolutionOutput:
    es = ctx.user_request.lang == "es"
    over = ctx.budget_analysis.over_budget_by_usd  # type: ignore[union-attr]
    if es:
        actions = [
            "Reemplazar las visitas guiadas de pago por recorridos a pie gratuitos.",
            "Cambiar a un alojamiento más económico (~25% menos por noche).",
            f"Comer en mercados y puestos callejeros para recortar parte de los ${over:.0f} de exceso.",
        ]
    else:
        actions = [
            "Replace paid guided visits with free walking routes.",
            "Switch to cheaper lodging (~25% less per night).",
            f"Eat at markets and street stalls to cut part of the ${over:.0f} overrun.",
        ]
    return ConflictResolutionOutput(actions_taken=actions)
