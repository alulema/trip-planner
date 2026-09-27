"""1. Destination Research Agent (generative) — season notes, areas and reference costs."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from ..guardrails import TokenBudget
from ..llm_client import LLMClient, OnProgress
from ..models import DestinationResearch, DestinationResearchOutput, ReferenceCosts, SharedContext, TokenUsage
from . import compact, language_rule

SYSTEM = """You are a travel research agent. Reply with JSON only.
Given a destination, trip length and approximate travel month, return:
- season_notes: weather/season advice for that month, one short sentence.
- recommended_areas: 3 neighbourhoods or districts INSIDE the destination city itself
  (not other towns, not single attractions). Name only, no descriptions.
- lodging_per_night_usd: a mid-range room for the whole group, per night.
- meal_avg_usd: one typical meal for one person.
- local_transport_day_usd: local transport for one person per day.
Estimate the costs for THIS destination's cost of living. For reference, lodging ranges from
about 25 (very cheap countries) to 300 (the most expensive cities), a meal from 3 to 45, and
local transport from 2 to 25."""

AGENT_NOTES = {
    "es": "Estimaciones generales de un modelo de IA local, no tarifas en tiempo real.",
    "en": "General estimates from a local AI model, not live prices.",
}

# Sanity bounds for numbers coming from a small model (USD).
LIMITS = {"lodging": (8, 1500), "meal": (1, 150), "transport": (0, 100)}


def clean_area(name: str) -> str:
    """Keep just the place name: small models append descriptions ("Baixa - the historic heart")."""
    name = re.split(r"\s+[-–—:]\s+|\s*\(|,\s", name.strip(), maxsplit=1)[0]
    return name.strip(" .\"'")[:40]


def _bound(value: float, key: str) -> float:
    lo, hi = LIMITS[key]
    return round(min(hi, max(lo, value)), 2)


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget,
              on_progress: OnProgress | None = None) -> tuple[DestinationResearch, TokenUsage]:
    req = ctx.user_request
    user = compact({
        "destination": req.destination,
        "days": req.days,
        "group_size": req.travelers,
        "travel_month": datetime.now(timezone.utc).strftime("%B"),
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete_json(
        agent="destination_research", system=SYSTEM, user=user, output_model=DestinationResearchOutput,
        max_tokens=260, budget=budget, mock=lambda: _mock(ctx), on_progress=on_progress,
    )
    areas = [clean_area(a) for a in out.recommended_areas]
    areas = list(dict.fromkeys(a for a in areas if a))[:3] or [req.destination]
    section = DestinationResearch(
        season_notes=out.season_notes.strip(),
        recommended_areas=areas,
        reference_costs=ReferenceCosts(
            lodging_per_night_usd=_bound(out.lodging_per_night_usd, "lodging"),
            meal_avg_usd=_bound(out.meal_avg_usd, "meal"),
            local_transport_day_usd=_bound(out.local_transport_day_usd, "transport"),
        ),
        agent_notes=AGENT_NOTES[req.lang],
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
    )
