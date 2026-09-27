"""2. Itinerary Planning Agent.

`run` is generative and happens ONCE: one small-model call writes the day-by-day plan,
including a free alternative per day. `revise` is deterministic: it applies the budget
actions chosen by conflict resolution (swap in the free alternatives, lower the daily cost
assumptions) without generating anything — "generate once, decide many times"."""

from __future__ import annotations

from ..decisions.rules import normalize
from .destination_research import clean_area
from ..decisions.taxonomy import FOOD_CUT, FREE_ALTERNATIVE_COST_USD, LODGING_CUT, TRANSPORT_CUT
from ..guardrails import TokenBudget
from ..llm_client import LLMClient, OnProgress
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

SYSTEM = """You are an itinerary planning agent. Reply with JSON only.
Plan the trip day by day for the traveller's interests, grouping activities by area.
Rules:
- One entry per day, numbered from 1, for exactly the requested number of days.
- area: one of the given areas, written exactly as given.
- activities: 2 or 3 short, concrete activities (max 8 words each) in that area.
- estimated_cost_usd: realistic total for that day's tickets, entrance fees, tours and paid
  experiences for the whole group (not lodging, meals or transport). Museums, temples, tours
  and shows usually charge; typical city days cost about 10 to 60 USD per person.
- free_alternative: one free activity in the same area that could replace the paid ones."""

# Words that suggest an activity costs nothing (kept when paid ones are swapped out).
FREE_HINTS = ("free", "gratis", "gratuit", "walk", "paseo", "caminar", "stroll", "park", "parque",
              "market", "mercado", "viewpoint", "mirador", "beach", "playa", "garden", "jardin")


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget,
              on_progress: OnProgress | None = None) -> tuple[ItineraryDraft, TokenUsage]:
    req = ctx.user_request
    research = ctx.destination_research
    assert research is not None, "destination research must run first"
    user = compact({
        "destination": req.destination,
        "days": req.days,
        "group_size": req.travelers,
        "interests": req.interests,
        "areas": research.recommended_areas,
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete_json(
        agent="itinerary_planning", system=SYSTEM, user=user, output_model=ItineraryPlanningOutput,
        max_tokens=120 + 110 * req.days, budget=budget, mock=lambda: _mock(ctx), on_progress=on_progress,
    )
    return _normalize(out, ctx), usage


def _normalize(out: ItineraryPlanningOutput, ctx: SharedContext) -> ItineraryDraft:
    """Force the draft into shape (exactly `days` entries, sane costs) and derive the daily
    cost assumptions from the research reference costs — arithmetic stays in code."""
    req = ctx.user_request
    ref = ctx.destination_research.reference_costs  # type: ignore[union-attr]
    max_day_cost = 250.0 * req.travelers
    by_day = {d.day: d for d in out.days}
    ordered = sorted(out.days, key=lambda d: d.day)
    days = []
    for n in range(1, req.days + 1):
        src = by_day.get(n) or (ordered[n - 1] if n - 1 < len(ordered) else None)
        if src is None:
            area = ordered[-1].area if ordered else req.destination
            src = ItineraryDayOutput(day=n, area=area, activities=[], estimated_cost_usd=0, free_alternative="")
        days.append(ItineraryDay(
            day=n,
            area=clean_area(src.area) or req.destination,
            activities=[a.strip() for a in src.activities if a.strip()][:4],
            estimated_cost_usd=round(min(max_day_cost, max(0.0, src.estimated_cost_usd)), 2),
            free_alternative=src.free_alternative.strip(),
        ))
    return ItineraryDraft(
        days=days,
        cost_assumptions=CostAssumptions(
            lodging_per_night_usd=ref.lodging_per_night_usd,
            food_per_day_usd=round(ref.meal_avg_usd * 3 * req.travelers, 2),
            transport_per_day_usd=round(ref.local_transport_day_usd * req.travelers, 2),
        ),
        revision=0,
        planner_notes="",
    )


def revise(ctx: SharedContext, action_ids: list[str]) -> tuple[ItineraryDraft, TokenUsage]:
    """Apply preset budget actions to the current draft. No LLM call."""
    draft = ctx.itinerary_draft
    assert draft is not None
    days = [d.model_copy(deep=True) for d in draft.days]
    a = draft.cost_assumptions.model_copy()
    for action in action_ids:
        if action == "free_alternatives":
            for d in days:
                if d.estimated_cost_usd > 0 and d.free_alternative:
                    kept = [x for x in d.activities if any(h in normalize(x) for h in FREE_HINTS)]
                    d.activities = [d.free_alternative, *kept][:4]
                    d.estimated_cost_usd = FREE_ALTERNATIVE_COST_USD
                    d.adjusted = True
                elif d.estimated_cost_usd > 0:
                    d.estimated_cost_usd = round(d.estimated_cost_usd / 2, 2)
                    d.adjusted = True
        elif action == "cheaper_lodging":
            a.lodging_per_night_usd = round(a.lodging_per_night_usd * (1 - LODGING_CUT), 2)
        elif action == "street_food":
            a.food_per_day_usd = round(a.food_per_day_usd * (1 - FOOD_CUT), 2)
        elif action == "transit_pass":
            a.transport_per_day_usd = round(a.transport_per_day_usd * (1 - TRANSPORT_CUT), 2)
    revised = ItineraryDraft(days=days, cost_assumptions=a, revision=draft.revision + 1,
                             planner_notes=", ".join(action_ids))
    return revised, TokenUsage()


def _mock(ctx: SharedContext) -> ItineraryPlanningOutput:
    req = ctx.user_request
    areas = ctx.destination_research.recommended_areas or [req.destination]  # type: ignore[union-attr]
    es = req.lang == "es"
    interests = req.interests or (["cultura", "gastronomía"] if es else ["culture", "food"])
    days = []
    for n in range(1, req.days + 1):
        topic = interests[(n - 1) % len(interests)]
        area = areas[(n - 1) % len(areas)]
        acts = ([f"Visita guiada de {topic}", f"Experiencia local de {topic}", "Paseo por el mercado"]
                if es else [f"Guided {topic} visit", f"Local {topic} experience", "Stroll through the market"])
        days.append(ItineraryDayOutput(
            day=n, area=area, activities=acts, estimated_cost_usd=35 * req.travelers,
            free_alternative=(f"Recorrido a pie gratuito por {area}" if es else f"Free walking route around {area}"),
        ))
    return ItineraryPlanningOutput(days=days)
