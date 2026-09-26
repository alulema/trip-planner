"""1. Destination Research Agent — season notes, areas to stay in and reference costs."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from ..guardrails import TokenBudget
from ..llm_client import LLMClient
from ..models import DestinationResearch, DestinationResearchOutput, ReferenceCosts, SharedContext, TokenUsage
from . import compact, language_rule

SYSTEM = """You are a travel destination research agent in a multi-agent trip planner.
Given a destination, trip length and the (approximate) travel month, produce:
- season_notes: weather/season considerations in 1-2 sentences,
- recommended_areas: 2-3 neighbourhoods or zones to stay in or explore,
- realistic reference costs in USD:
  lodging_per_night_usd = a mid-range room for the WHOLE party per night,
  meal_avg_usd = one typical meal for ONE person,
  local_transport_day_usd = local transport for ONE person per day,
- agent_notes: one short sentence stating these are general AI estimates, not live prices.
Do not invent sources. Be concise."""


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget) -> tuple[DestinationResearch, TokenUsage]:
    req = ctx.user_request
    month = datetime.now(timezone.utc).strftime("%B")
    user = compact({
        "destination": req.destination,
        "days": req.days,
        "travelers": req.travelers,
        "interests": req.interests,
        "approx_travel_month": month,
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete(
        agent="destination_research",
        system=SYSTEM,
        user=user,
        output_model=DestinationResearchOutput,
        max_tokens=600,
        budget=budget,
        mock=lambda: _mock(ctx),
    )
    section = DestinationResearch(
        season_notes=out.season_notes,
        recommended_areas=out.recommended_areas[:3],
        reference_costs=ReferenceCosts(
            lodging_per_night_usd=max(0.0, out.lodging_per_night_usd),
            meal_avg_usd=max(0.0, out.meal_avg_usd),
            local_transport_day_usd=max(0.0, out.local_transport_day_usd),
        ),
        agent_notes=out.agent_notes,
    )
    return section, usage


def _mock(ctx: SharedContext) -> DestinationResearchOutput:
    req = ctx.user_request
    es = req.lang == "es"
    rooms = math.ceil(req.travelers / 2)
    return DestinationResearchOutput(
        season_notes=(
            f"Clima templado en {req.destination}; lleva capas y calzado cómodo."
            if es else f"Mild weather in {req.destination}; pack layers and comfortable shoes."
        ),
        recommended_areas=[f"{req.destination} Centro" if es else f"{req.destination} Downtown",
                           "Old Town", "Riverside"],
        lodging_per_night_usd=85 * rooms,
        meal_avg_usd=14,
        local_transport_day_usd=9,
        agent_notes=(
            "Datos simulados (modo offline): estimaciones genéricas, no tarifas reales."
            if es else "Simulated data (offline mode): generic estimates, not live prices."
        ),
    )
