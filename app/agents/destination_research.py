"""1. Destination Research Agent — season notes, areas, highlights and reference costs.

Each fact comes from the most reliable source available, and generation is the last resort:
  * season notes: the real weather for the travel dates (live step, written by code); the
    model only when there is no weather data;
  * areas and highlights: the curated catalog (app/catalog.py); Wikidata/OpenStreetMap for cities
    outside it (source="live"); the model when neither is available (source="model");
  * reference costs: the catalog; otherwise a model estimate clamped to sane ranges."""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

from .. import catalog
from ..guardrails import TokenBudget, TokenBudgetExceeded
from ..llm_client import LLMClient, LLMError, OnProgress
from ..models import (
    DestinationResearch,
    CostsOutput,
    DestinationResearchOutput,
    ReferenceCosts,
    SeasonNotesOutput,
    SharedContext,
    TokenUsage,
)
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

log = logging.getLogger("trip_planner.research")

SEASON_SYSTEM = """You are a travel research agent. Reply with JSON only.
Give season_notes: weather and season advice for visiting the destination in the given month,
in one short sentence of at most 25 words."""

COSTS_SYSTEM = """You are a travel research agent. Reply with JSON only.
Estimate for the destination:
- lodging_per_night_usd: a mid-range room for the whole group, per night.
- meal_avg_usd: one typical meal for one person.
- local_transport_day_usd: local transport for one person per day.
Use THIS destination's cost of living. For reference, lodging ranges from about 25 (very cheap
countries) to 300 (the most expensive cities), a meal from 3 to 45, and local transport from 2 to 25."""

LIVE_NOTES = {
    "es": "Zonas y lugares de {source}; costos estimados por un modelo de IA local (no tarifas en tiempo real).",
    "en": "Areas and places from {source}; costs estimated by a local AI model (not live prices).",
}
AGENT_NOTES = {
    "es": "Estimaciones generales de un modelo de IA local, no tarifas en tiempo real.",
    "en": "General estimates from a local AI model, not live prices.",
}
GENERIC_SEASON = {
    "es": "Revisa el clima de tu fecha de viaje antes de salir.",
    "en": "Check the weather for your travel dates before you go.",
}
CATALOG_NOTES = {
    "es": "Zonas y costos de referencia del catálogo curado (aproximados, no tarifas en tiempo real).",
    "en": "Areas and reference costs from the curated catalog (approximate, not live prices).",
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


def travel_month(ctx: SharedContext) -> str:
    start = ctx.user_request.start_date
    return (start or datetime.now(timezone.utc)).strftime("%B")


def live_weather_note(ctx: SharedContext) -> str | None:
    live = ctx.live_data
    return live.weather.summary if live and live.weather and live.weather.summary else None


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget,
              on_progress: OnProgress | None = None) -> tuple[DestinationResearch, TokenUsage]:
    req = ctx.user_request
    city = catalog.lookup(req.destination)
    if city is not None:
        return await _from_catalog(ctx, city, llm, budget, on_progress)
    if ctx.live_data and ctx.live_data.places:
        return await _from_live_places(ctx, llm, budget, on_progress)
    user = compact({
        "destination": req.destination,
        "days": req.days,
        "group_size": req.travelers,
        "travel_month": travel_month(ctx),
    }) + "\n" + language_rule(req)

    out, usage = await llm.complete_json(
        agent="destination_research", system=SYSTEM, user=user, output_model=DestinationResearchOutput,
        max_tokens=260, budget=budget, mock=lambda: _mock(ctx), on_progress=on_progress,
    )
    areas = [clean_area(a) for a in out.recommended_areas]
    areas = list(dict.fromkeys(a for a in areas if a))[:3] or [req.destination]
    section = DestinationResearch(
        season_notes=live_weather_note(ctx) or out.season_notes.strip(),
        recommended_areas=areas,
        reference_costs=_costs(out),
        agent_notes=AGENT_NOTES[req.lang],
    )
    return section, usage


def _costs(out: CostsOutput | DestinationResearchOutput) -> ReferenceCosts:
    return ReferenceCosts(
        lodging_per_night_usd=_bound(out.lodging_per_night_usd, "lodging"),
        meal_avg_usd=_bound(out.meal_avg_usd, "meal"),
        local_transport_day_usd=_bound(out.local_transport_day_usd, "transport"),
    )


async def _from_live_places(ctx: SharedContext, llm: LLMClient, budget: TokenBudget,
                            on_progress: OnProgress | None) -> tuple[DestinationResearch, TokenUsage]:
    """Outside the catalog, with districts and places from a live source: the model only
    estimates the cost level (and nothing else)."""
    req, places = ctx.user_request, ctx.live_data.places  # type: ignore[union-attr]
    loc = ctx.live_data.location  # type: ignore[union-attr]
    user = compact({
        "destination": f"{loc.name}, {loc.country}" if loc else req.destination,
        "group_size": req.travelers,
    })
    out, usage = await llm.complete_json(
        agent="destination_research", system=COSTS_SYSTEM, user=user, output_model=CostsOutput,
        max_tokens=80, budget=budget, on_progress=on_progress,
        mock=lambda: CostsOutput(**_mock(ctx).model_dump(include=set(CostsOutput.model_fields))),
    )
    section = DestinationResearch(
        season_notes=live_weather_note(ctx) or GENERIC_SEASON[req.lang],
        recommended_areas=list(places.area_highlights),
        reference_costs=_costs(out),
        agent_notes=LIVE_NOTES[req.lang].format(source=places.source.name.split(" (")[0]),
        source="live",
        highlights=[h for hs in places.area_highlights.values() for h in hs],
        area_highlights={a: list(hs) for a, hs in places.area_highlights.items()},
        area_free={a: list(hs) for a, hs in places.area_free.items()},
    )
    return section, usage


async def _from_catalog(ctx: SharedContext, city: catalog.City, llm: LLMClient, budget: TokenBudget,
                        on_progress: OnProgress | None) -> tuple[DestinationResearch, TokenUsage]:
    req = ctx.user_request
    season = live_weather_note(ctx)
    if season is not None:
        usage = TokenUsage()  # real weather for the dates: nothing left for the model to write
    else:
        season, usage = await _season_from_model(ctx, city, llm, budget, on_progress)
    rooms = math.ceil(req.travelers / 2)
    section = DestinationResearch(
        season_notes=season,
        recommended_areas=list(city.areas),
        reference_costs=ReferenceCosts(
            lodging_per_night_usd=city.lodging_room_night_usd * rooms,
            meal_avg_usd=city.meal_usd,
            local_transport_day_usd=city.transport_day_usd,
        ),
        agent_notes=CATALOG_NOTES[req.lang],
        source="catalog",
        highlights=list(city.highlights),
        area_highlights={a: list(hs) for a, hs in city.area_highlights},
        area_free={a: list(hs) for a, hs in city.area_free},
    )
    return section, usage


async def _season_from_model(ctx: SharedContext, city: catalog.City, llm: LLMClient, budget: TokenBudget,
                             on_progress: OnProgress | None) -> tuple[str, TokenUsage]:
    user = compact({
        "destination": f"{city.name}, {city.country}",
        "travel_month": travel_month(ctx),
    }) + "\n" + language_rule(ctx.user_request)
    try:
        out, usage = await llm.complete_json(
            agent="destination_research", system=SEASON_SYSTEM, user=user, output_model=SeasonNotesOutput,
            max_tokens=200, budget=budget, on_progress=on_progress,
            mock=lambda: SeasonNotesOutput(season_notes=_mock(ctx).season_notes),
        )
        return out.season_notes.strip(), usage
    except (LLMError, TokenBudgetExceeded) as exc:
        # The catalog facts don't depend on the model: a failed season note must not sink the trip.
        log.warning("season notes unavailable for %s: %s", city.name, exc)
        return GENERIC_SEASON[ctx.user_request.lang], TokenUsage()


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
