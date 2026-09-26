"""5. Synthesis Agent — warm, readable summary of the final plan.

The structured part of `final_itinerary` (days, totals, within_budget) is copied from the
context by code; the LLM only writes the narrative. If the token budget is exhausted, a
deterministic template is used instead so the user still gets an honest result."""

from __future__ import annotations

from ..guardrails import TokenBudget
from ..llm_client import LLMClient
from ..models import FinalItinerary, SharedContext, SynthesisOutput, TokenUsage
from . import compact, language_rule

SYSTEM = """You are the synthesis agent in a multi-agent trip planner. Write a warm, clear
trip summary (2-3 short paragraphs, max 150 words, plain text, no lists, no markdown) from the
structured data. If a budget adjustment happened, say so transparently and mention what changed.
If the plan is still over budget, state it honestly with the gap in USD and suggest the
traveller raise the budget or shorten the trip. Costs are AI estimates, not live prices."""


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget) -> tuple[FinalItinerary, TokenUsage]:
    req = ctx.user_request
    draft, analysis, conflict = ctx.itinerary_draft, ctx.budget_analysis, ctx.conflict_resolution
    assert draft is not None and analysis is not None
    user = compact({
        "request": req.model_dump(exclude={"lang"}),
        "season_notes": ctx.destination_research.season_notes if ctx.destination_research else "",
        "days": [{"day": d.day, "area": d.area, "activities": d.activities} for d in draft.days],
        "estimated_total_usd": analysis.estimated_total_usd,
        "within_budget": analysis.within_budget,
        "over_budget_by_usd": analysis.over_budget_by_usd,
        "budget_adjustment": {
            "happened": conflict.triggered,
            "iterations": conflict.iterations,
            "actions": conflict.actions_taken,
        },
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete(
        agent="synthesis",
        system=SYSTEM,
        user=user,
        output_model=SynthesisOutput,
        max_tokens=500,
        budget=budget,
        mock=lambda: SynthesisOutput(summary=fallback_summary(ctx)),
    )
    return build_final(ctx, out.summary.strip()), usage


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
