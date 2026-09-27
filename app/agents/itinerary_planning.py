"""2. Itinerary Planning Agent.

`run` is generative and happens ONCE: one small-model call writes the day-by-day plan,
including a free alternative per day. `revise` is deterministic: it applies the budget
actions chosen by conflict resolution (swap in the free alternatives, lower the daily cost
assumptions) without generating anything — "generate once, decide many times"."""

from __future__ import annotations

from ..decisions.rules import normalize
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
Write the trip day by day following day_plan exactly: one entry per planned day, with the
same day number and the same area.
- activities: 2 or 3 short, concrete activities (max 8 words each) in that day's area. If the
  day lists highlights, build the activities around them using their exact names; never move
  a place to another day's area.
- estimated_cost_usd: realistic total for that day's tickets, entrance fees, tours and paid
  experiences for the whole group (not lodging, meals or transport). Museums, temples, tours
  and shows usually charge; typical city days cost about 10 to 60 USD per person.
- free_alternative: one free activity in the same area that could replace the paid ones.
Match the activities to the traveller's interests.
Write every activity in the requested language; keep the place names from day_plan exactly
as written (do not translate them, do not switch to another language around them)."""

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
        "group_size": req.travelers,
        "interests": req.interests,
        "day_plan": day_plan(ctx),
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete_json(
        agent="itinerary_planning", system=SYSTEM, user=user, output_model=ItineraryPlanningOutput,
        max_tokens=120 + 110 * req.days, budget=budget, mock=lambda: _mock(ctx), on_progress=on_progress,
    )
    return _normalize(out, ctx), usage


def day_plan(ctx: SharedContext) -> list[dict]:
    """Which area each day visits, decided by code (round-robin over the research areas), plus
    the highlights that are really in that area when the catalog knows them. A revisit of an
    area gets no highlights, so the model explores other spots instead of repeating them."""
    req, research = ctx.user_request, ctx.destination_research
    areas = research.recommended_areas or [req.destination]  # type: ignore[union-attr]
    plan = []
    for n in range(1, req.days + 1):
        area = areas[(n - 1) % len(areas)]
        entry: dict = {"day": n, "area": area}
        highlights = research.area_highlights.get(area)  # type: ignore[union-attr]
        if highlights and n <= len(areas):
            entry["highlights"] = highlights
        plan.append(entry)
    return plan


def free_alternative(ctx: SharedContext, area: str, generated: str) -> str:
    """For catalog cities the free fallback is written by code and anchored to the day's area:
    when the budget loop swaps it in, the model can't send every day to the same district
    (seen in a real run: "Explore Alfama's streets" for Baixa, Alfama and Belém)."""
    if ctx.destination_research is not None and ctx.destination_research.source == "catalog":
        return f"Paseo libre por {area}" if ctx.user_request.lang == "es" else f"Free walk around {area}"
    return generated.strip()


def _normalize(out: ItineraryPlanningOutput, ctx: SharedContext) -> ItineraryDraft:
    """Force the draft into shape (exactly `days` entries, sane costs) and derive the daily
    cost assumptions from the research reference costs — arithmetic stays in code."""
    req = ctx.user_request
    ref = ctx.destination_research.reference_costs  # type: ignore[union-attr]
    max_day_cost = 250.0 * req.travelers
    plan = day_plan(ctx)
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
            area=plan[n - 1]["area"],  # the plan decides the area, not the model
            activities=[a.strip() for a in src.activities if a.strip()][:4],
            estimated_cost_usd=round(min(max_day_cost, max(0.0, src.estimated_cost_usd)), 2),
            free_alternative=free_alternative(ctx, plan[n - 1]["area"], src.free_alternative),
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
    es = req.lang == "es"
    interests = req.interests or (["cultura", "gastronomía"] if es else ["culture", "food"])
    days = []
    for entry in day_plan(ctx):
        n, area = entry["day"], entry["area"]
        topic = interests[(n - 1) % len(interests)]
        if entry.get("highlights"):
            acts = [(f"Visita {h}" if es else f"Visit {h}") for h in entry["highlights"][:2]]
        else:
            acts = ([f"Visita guiada de {topic}", f"Experiencia local de {topic}"]
                    if es else [f"Guided {topic} visit", f"Local {topic} experience"])
        acts.append("Paseo por el mercado" if es else "Stroll through the market")
        days.append(ItineraryDayOutput(
            day=n, area=area, activities=acts, estimated_cost_usd=35 * req.travelers,
            free_alternative=(f"Recorrido a pie gratuito por {area}" if es else f"Free walking route around {area}"),
        ))
    return ItineraryPlanningOutput(days=days)
