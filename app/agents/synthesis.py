"""5. Synthesis Agent (generative) — a short, warm narrative streamed token by token.

The structured part of `final_itinerary` (days, totals, within_budget) is copied from the
context by code; the model only writes prose. If it can't run (token budget exhausted,
model error), a deterministic template is used so the user still gets an honest result."""

from __future__ import annotations

from ..guardrails import TokenBudget
from ..llm_client import LLMClient, OnToken
from ..models import FinalItinerary, SharedContext, TokenUsage
from . import compact, language_rule

SYSTEM = """You write the final summary of a trip plan. Plain text, no lists, no markdown,
2 short paragraphs, at most 90 words. Be warm and concrete. If the budget was adjusted, say
what changed. If the plan is still over budget, say so honestly with the gap in USD. Costs
are AI estimates, not live prices."""


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget,
              on_token: OnToken | None = None) -> tuple[FinalItinerary, TokenUsage]:
    req = ctx.user_request
    draft, analysis, conflict = ctx.itinerary_draft, ctx.budget_analysis, ctx.conflict_resolution
    assert draft is not None and analysis is not None
    user = compact({
        "destination": req.destination,
        "days": req.days,
        "interests": req.interests,
        "highlights": [f"Day {d.day} {d.area}: {', '.join(d.activities[:2])}" for d in draft.days],
        "estimated_total_usd": analysis.estimated_total_usd,
        "budget_usd": req.budget_usd,
        "over_budget_by_usd": analysis.over_budget_by_usd,
        "budget_changes": conflict.actions_taken,
    }) + "\n" + language_rule(req)

    text, usage = await llm.complete_text(
        agent="synthesis", system=SYSTEM, user=user, max_tokens=220, budget=budget,
        mock=lambda: fallback_summary(ctx), on_token=on_token,
    )
    return build_final(ctx, text), usage


def build_final(ctx: SharedContext, summary: str) -> FinalItinerary:
    draft, analysis = ctx.itinerary_draft, ctx.budget_analysis
    assert draft is not None and analysis is not None
    return FinalItinerary(
        summary=summary,
        days=draft.days,
        total_cost_usd=analysis.estimated_total_usd,
        budget_usd=ctx.user_request.budget_usd,
        within_budget=analysis.within_budget,
        breakdown=analysis.breakdown,
    )


def fallback_summary(ctx: SharedContext) -> str:
    req, analysis, conflict = ctx.user_request, ctx.budget_analysis, ctx.conflict_resolution
    assert analysis is not None
    es = req.lang == "es"
    if es:
        text = (f"Tu viaje de {req.days} día(s) a {req.destination} tiene un costo estimado de "
                f"${analysis.estimated_total_usd:,.0f} USD frente a un presupuesto de ${req.budget_usd:,.0f}.")
        if conflict.triggered:
            text += f" El plan se ajustó {conflict.iterations} vez/veces para recortar costos."
        text += (" Queda dentro del presupuesto." if analysis.within_budget else
                 f" Aun así excede el presupuesto por ${analysis.over_budget_by_usd:,.0f}: considera "
                 "ampliar el presupuesto o acortar el viaje.")
        return text + " Las cifras son estimaciones generadas por IA, no tarifas en tiempo real."
    text = (f"Your {req.days}-day trip to {req.destination} is estimated at "
            f"${analysis.estimated_total_usd:,.0f} USD against a ${req.budget_usd:,.0f} budget.")
    if conflict.triggered:
        text += f" The plan was adjusted {conflict.iterations} time(s) to cut costs."
    text += (" It fits the budget." if analysis.within_budget else
             f" It is still ${analysis.over_budget_by_usd:,.0f} over budget: consider raising the "
             "budget or shortening the trip.")
    return text + " Figures are AI-generated estimates, not live prices."
