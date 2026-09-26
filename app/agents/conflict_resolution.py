"""4. Conflict Resolution Agent — non-generative.

When the plan is over budget it picks cuts from a preset catalog. The work is split the
System-One way:
  * code computes how much each action would save (arithmetic stays in Python),
  * the decision engine judges how much each action would hurt the traveller's interests
    (a typed score with a probability — rules today, Jev-ready),
  * code ranks by utility = savings × (1 − P(harm)) and takes the fewest actions that
    close the gap (max 2 per iteration).
The chosen actions are applied by `itinerary_planning.revise` — no text is generated.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..decisions import DecisionEngine, TypedQuestion
from ..decisions.taxonomy import ACTION_CATALOG, FOOD_CUT, LODGING_CUT, TRANSPORT_CUT, CatalogAction
from ..models import DecisionRecord, SharedContext

MAX_ACTIONS_PER_ITERATION = 2


@dataclass
class Candidate:
    action: CatalogAction
    savings_usd: float
    harm: float = 0.0

    @property
    def utility(self) -> float:
        return self.savings_usd * (1 - self.harm)


@dataclass
class ConflictPlan:
    action_ids: list[str]
    descriptions: list[str]
    decisions: list[DecisionRecord]
    expected_savings_usd: float


def estimate_savings(ctx: SharedContext, action: CatalogAction) -> float:
    draft = ctx.itinerary_draft
    assert draft is not None
    n, a = len(draft.days), draft.cost_assumptions
    if action.id == "free_alternatives":
        return round(sum(d.estimated_cost_usd if d.free_alternative else d.estimated_cost_usd / 2
                         for d in draft.days if d.estimated_cost_usd > 0), 2)
    if action.id == "cheaper_lodging":
        return round(a.lodging_per_night_usd * n * LODGING_CUT, 2)
    if action.id == "street_food":
        return round(a.food_per_day_usd * n * FOOD_CUT, 2)
    if action.id == "transit_pass":
        return round(a.transport_per_day_usd * n * TRANSPORT_CUT, 2)
    return 0.0


async def run(ctx: SharedContext, engine: DecisionEngine) -> ConflictPlan:
    analysis = ctx.budget_analysis
    assert analysis is not None
    applied = set(ctx.conflict_resolution.applied_action_ids)
    candidates = [Candidate(a, estimate_savings(ctx, a)) for a in ACTION_CATALOG if a.id not in applied]
    candidates = [c for c in candidates if c.savings_usd > 0]
    if not candidates:
        return ConflictPlan([], [], [], 0.0)

    profile = ctx.interest_profile
    state = {
        "traveller_interests": ctx.user_request.interests,
        "interest_categories": profile.categories if profile else [],
        "over_budget_by_usd": analysis.over_budget_by_usd,
        "breakdown": analysis.breakdown.model_dump(),
    }
    questions = [
        TypedQuestion(id=c.action.id, family="action_harm", text=c.action.harm_question, kind="score",
                      params={"action_id": c.action.id})
        for c in candidates
    ]
    answers = await engine.evaluate(state, questions)
    decisions = []
    for c in candidates:
        ans = answers[c.action.id]
        c.harm = float(ans.value)
        decisions.append(DecisionRecord(question=f"harm:{c.action.id}", answer=ans.value,
                                        probabilities=ans.probabilities, source=ans.source))

    chosen: list[Candidate] = []
    remaining = analysis.over_budget_by_usd
    for c in sorted(candidates, key=lambda c: c.utility, reverse=True):
        if remaining <= 0 or len(chosen) == MAX_ACTIONS_PER_ITERATION:
            break
        chosen.append(c)
        remaining -= c.savings_usd

    es = ctx.user_request.lang == "es"
    descriptions = [
        f"{c.action.description_es if es else c.action.description_en} "
        f"(≈ −${c.savings_usd:,.0f}; P({'afecta intereses' if es else 'hurts interests'}) = {c.harm:.2f})"
        for c in chosen
    ]
    return ConflictPlan([c.action.id for c in chosen], descriptions, decisions,
                        round(sum(c.savings_usd for c in chosen), 2))
