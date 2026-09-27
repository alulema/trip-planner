"""3. Budget Agent — deterministic, no LLM.

Arithmetic is code, not model output: an LLM that adds numbers wrong would make the whole
conflict-resolution step meaningless."""

from __future__ import annotations

from ..models import BudgetAnalysis, BudgetBreakdown, SharedContext


def analyze(ctx: SharedContext) -> BudgetAnalysis:
    draft = ctx.itinerary_draft
    assert draft is not None, "itinerary must exist before budgeting"
    days = len(draft.days)
    a = draft.cost_assumptions
    # One night per trip day (arrival night included, departure day excluded) keeps the
    # model simple and slightly conservative.
    breakdown = BudgetBreakdown(
        lodging=round(a.lodging_per_night_usd * days, 2),
        food=round(a.food_per_day_usd * days, 2),
        activities=round(sum(d.estimated_cost_usd for d in draft.days), 2),
        transport=round(a.transport_per_day_usd * days, 2),
    )
    total = round(breakdown.lodging + breakdown.food + breakdown.activities + breakdown.transport, 2)
    over = round(max(0.0, total - ctx.user_request.budget_usd), 2)
    return BudgetAnalysis(
        estimated_total_usd=total,
        over_budget_by_usd=over,
        breakdown=breakdown,
        within_budget=over == 0,
        itinerary_revision=draft.revision,
    )
