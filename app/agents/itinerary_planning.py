"""2. Itinerary Planning Agent — day-by-day plan grouped by area, with cost estimates.

Also re-invoked by the conflict loop with concrete revision instructions."""

from __future__ import annotations

from ..guardrails import TokenBudget
from ..llm_client import LLMClient
from ..models import (
    CostAssumptions,
    ItineraryDay,
    ItineraryDayOutput,
    ItineraryDraft,
    ItineraryPlanningOutput,
    SharedContext,
    TokenUsage,
)
from . import compact, language_rule

SYSTEM = """You are an itinerary planning agent in a multi-agent trip planner.
Using the destination research already done, build a day-by-day itinerary that matches the
traveller's interests and groups activities by area to minimise transfers.
Rules:
- Exactly one entry per trip day, numbered from 1. 2-4 short activities per day.
- estimated_cost_usd per day = paid activities / entrance fees for the WHOLE party only
  (not lodging, not food, not local transport).
- Also commit to daily cost assumptions for the WHOLE party, starting from the reference costs:
  lodging_per_night_usd, food_per_day_usd, transport_per_day_usd.
- planner_notes: one short sentence about your approach.
If you receive a "budget revision" block, apply those actions: swap paid activities for free
or cheaper ones and/or lower the daily assumptions, never removing days or ignoring the main
interests."""


async def run(
    ctx: SharedContext,
    llm: LLMClient,
    budget: TokenBudget,
    revision_actions: list[str] | None = None,
) -> tuple[ItineraryDraft, TokenUsage]:
    req = ctx.user_request
    research = ctx.destination_research
    assert research is not None, "destination research must run first"
    prev = ctx.itinerary_draft
    revision = (prev.revision + 1) if (prev and revision_actions) else 0

    payload = {
        "request": req.model_dump(exclude={"lang"}),
        "research": {
            "recommended_areas": research.recommended_areas,
            "reference_costs": research.reference_costs.model_dump(),
        },
    }
    if revision_actions and prev and ctx.budget_analysis:
        payload["budget_revision"] = {
            "over_budget_by_usd": ctx.budget_analysis.over_budget_by_usd,
            "actions": revision_actions,
            "current_itinerary": {
                "days": [d.model_dump() for d in prev.days],
                "cost_assumptions": prev.cost_assumptions.model_dump(),
            },
        }
    user = compact(payload) + "\n" + language_rule(req)

    out, usage = await llm.complete(
        agent="itinerary_planning",
        system=SYSTEM,
        user=user,
        output_model=ItineraryPlanningOutput,
        max_tokens=250 + 170 * req.days,
        budget=budget,
        mock=lambda: _mock(ctx, revision),
    )
    return _normalize(out, ctx, revision), usage


def _normalize(out: ItineraryPlanningOutput, ctx: SharedContext, revision: int) -> ItineraryDraft:
    """Force the draft into shape: exactly `days` entries, non-negative costs, and the
    research reference costs as fallback if the model left an assumption at zero."""
    req = ctx.user_request
    ref = ctx.destination_research.reference_costs  # type: ignore[union-attr]
    by_day = {d.day: d for d in out.days}
    ordered = sorted(out.days, key=lambda d: d.day)
    days = []
    for n in range(1, req.days + 1):
        src = by_day.get(n) or (ordered[n - 1] if n - 1 < len(ordered) else None)
        if src is None:
            src = ItineraryDayOutput(day=n, area=ordered[-1].area if ordered else req.destination,
                                     activities=[], estimated_cost_usd=0)
        days.append(ItineraryDay(day=n, area=src.area, activities=src.activities[:5],
                                 estimated_cost_usd=round(max(0.0, src.estimated_cost_usd), 2)))

    def pick(value: float, fallback: float) -> float:
        return round(value if value > 0 else fallback, 2)

    return ItineraryDraft(
        days=days,
        cost_assumptions=CostAssumptions(
            lodging_per_night_usd=pick(out.lodging_per_night_usd, ref.lodging_per_night_usd),
            food_per_day_usd=pick(out.food_per_day_usd, ref.meal_avg_usd * 3 * req.travelers),
            transport_per_day_usd=pick(out.transport_per_day_usd, ref.local_transport_day_usd * req.travelers),
        ),
        revision=revision,
        planner_notes=out.planner_notes,
    )


def _mock(ctx: SharedContext, revision: int) -> ItineraryPlanningOutput:
    req = ctx.user_request
    ref = ctx.destination_research.reference_costs  # type: ignore[union-attr]
    areas = ctx.destination_research.recommended_areas or [req.destination]  # type: ignore[union-attr]
    es = req.lang == "es"
    interests = req.interests or (["cultura", "gastronomía"] if es else ["culture", "food"])
    # Each revision halves paid activities and trims lodging/food, like a real planner would.
    factor = 0.5 ** revision
    days = []
    for n in range(1, req.days + 1):
        topic = interests[(n - 1) % len(interests)]
        if revision:
            acts = ([f"Paseo gratuito por {areas[(n - 1) % len(areas)]}", f"Actividad económica de {topic}"]
                    if es else [f"Free walk around {areas[(n - 1) % len(areas)]}", f"Budget {topic} activity"])
        else:
            acts = ([f"Visita guiada de {topic}", f"Experiencia local de {topic}", "Cena en restaurante recomendado"]
                    if es else [f"Guided {topic} visit", f"Local {topic} experience", "Dinner at a recommended spot"])
        days.append(ItineraryDayOutput(day=n, area=areas[(n - 1) % len(areas)], activities=acts,
                                       estimated_cost_usd=round(35 * req.travelers * factor, 2)))
    return ItineraryPlanningOutput(
        days=days,
        lodging_per_night_usd=round(ref.lodging_per_night_usd * (0.75 ** revision), 2),
        food_per_day_usd=round(ref.meal_avg_usd * 3 * req.travelers * (0.8 ** revision), 2),
        transport_per_day_usd=round(ref.local_transport_day_usd * req.travelers, 2),
        planner_notes=("Plan simulado agrupado por zonas." if es else "Simulated plan grouped by area."),
    )
