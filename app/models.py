"""Data contract shared by every agent in the chain (the `shared_context`).

One `SharedContext` object travels through the whole chain — it is also the LangGraph
state. Each agent receives the full context and returns ONLY its own section; the graph
merges sections back in (plain replacement per key, or the reducers declared below). Sections are append-only: once written, a key is only replaced
by a newer revision of the same section (e.g. `itinerary_draft.revision` 0 → 1), never
deleted.

Two kinds of models live here:

* Contract models (`UserRequest`, `DestinationResearch`, ...) — the shape of the context.
* `*Output` models — the exact JSON the generative agents must return. They are kept small
  (a CPU model pays for every token) and free of numeric constraints so they translate
  cleanly into JSON schemas; sanity checks happen in Python after parsing.
"""

from __future__ import annotations

import operator
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

AgentName = Literal[
    "orchestrator",
    "live_data",
    "destination_research",
    "itinerary_planning",
    "budget",
    "conflict_resolution",
    "synthesis",
]

TraceEvent = Literal[
    "plan_created", "started", "completed", "decision", "conflict_detected", "skipped", "failed", "finished"
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- request

MAX_DAYS_AHEAD = 365


class UserRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=60)
    days: int = Field(ge=1, le=7)
    budget_usd: float = Field(gt=0, le=100_000)
    travelers: int = Field(default=1, ge=1, le=8)
    interests: list[str] = Field(default_factory=list, max_length=5)
    lang: Literal["es", "en"] = "es"
    # First day of the trip. Optional for API compatibility; without it there is no real
    # weather to look up and the season note falls back to the model.
    start_date: date | None = None

    @field_validator("start_date")
    @classmethod
    def _check_start(cls, v: date | None) -> date | None:
        if v is None:
            return v
        today = datetime.now(timezone.utc).date()
        if not today - timedelta(days=1) <= v <= today + timedelta(days=MAX_DAYS_AHEAD):
            raise ValueError(f"start_date must be between today and {MAX_DAYS_AHEAD} days ahead")
        return v

    @property
    def end_date(self) -> date | None:
        return self.start_date + timedelta(days=self.days - 1) if self.start_date else None

    @field_validator("destination")
    @classmethod
    def _strip_destination(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("destination is too short")
        return v

    @field_validator("interests")
    @classmethod
    def _clean_interests(cls, v: list[str]) -> list[str]:
        cleaned = []
        for item in v:
            item = " ".join(item.split())[:30]
            if item and item.lower() not in (c.lower() for c in cleaned):
                cleaned.append(item)
        return cleaned[:5]


# --------------------------------------------------------------------------- sections


class DecisionRecord(BaseModel):
    """One typed decision taken by the decision engine (rules today, Jev-ready)."""

    question: str
    answer: str | float
    probabilities: dict[str, float]
    source: str


class InterestMatch(BaseModel):
    interest: str
    category: str
    probability: float


class InterestProfile(BaseModel):
    matches: list[InterestMatch]

    @property
    def categories(self) -> list[str]:
        return sorted({m.category for m in self.matches})


class SourceInfo(BaseModel):
    """Where a live figure came from and when it was fetched — shown next to the figure."""

    name: str
    url: str
    attribution: str
    fetched_at: str = Field(default_factory=utc_now_iso)


class GeoPoint(BaseModel):
    name: str
    country: str = ""
    country_code: str = ""
    latitude: float
    longitude: float
    population: int = 0


class WeatherDay(BaseModel):
    date: date
    t_min_c: float | None = None
    t_max_c: float | None = None
    # Forecast: max precipitation probability (%); reference year: rain in mm.
    precip_probability: float | None = None
    precip_mm: float | None = None
    weather_code: int | None = None

    @property
    def rainy(self) -> bool:
        return ((self.precip_probability or 0) >= 50) or ((self.precip_mm or 0) >= 1)


class WeatherReport(BaseModel):
    # "forecast" = the real forecast for the trip dates; "reference" = the same dates in an
    # earlier year (historical data), used when the trip is beyond the forecast horizon.
    kind: Literal["forecast", "reference"]
    days: list[WeatherDay]
    summary: str = ""  # written by code from `days`, in the request language
    source: SourceInfo


class PlacesReport(BaseModel):
    """Areas and notable places around the destination (Wikidata, or OpenStreetMap)."""

    area_highlights: dict[str, list[str]]
    area_free: dict[str, list[str]] = Field(default_factory=dict)
    source: SourceInfo


class FxQuote(BaseModel):
    currency: str
    rate: float  # 1 USD = rate × currency
    as_of: str  # date of the published rate
    source: SourceInfo


class LiveData(BaseModel):
    """Output of the live-data step. Every provider is optional: a missing piece falls back
    to the catalog or the model, and `errors` says why it is missing."""

    location: GeoPoint | None = None
    weather: WeatherReport | None = None
    places: PlacesReport | None = None
    fx: FxQuote | None = None
    errors: dict[str, str] = Field(default_factory=dict)


class ReferenceCosts(BaseModel):
    """Reference costs in USD: lodging for the whole party per night; one meal for one
    person; local transport for one person per day."""

    lodging_per_night_usd: float = 0
    meal_avg_usd: float = 0
    local_transport_day_usd: float = 0


class DestinationResearch(BaseModel):
    season_notes: str
    recommended_areas: list[str]
    reference_costs: ReferenceCosts
    agent_notes: str
    # "catalog" = areas, highlights and costs from the curated table; "live" = areas and
    # highlights from Wikidata/OpenStreetMap, costs from the model; "model" = all from the LLM.
    source: Literal["catalog", "live", "model"] = "model"
    highlights: list[str] = Field(default_factory=list)
    # Catalog / live only: which highlights belong to which area.
    area_highlights: dict[str, list[str]] = Field(default_factory=dict)
    area_free: dict[str, list[str]] = Field(default_factory=dict)


class ItineraryDay(BaseModel):
    day: int
    area: str
    activities: list[str]
    # Activities / entrance fees / experiences for the whole party. Lodging, food and
    # local transport are costed separately through `CostAssumptions`.
    estimated_cost_usd: float = 0
    # Generated up front so a budget revision can swap it in without another LLM call.
    free_alternative: str = ""
    adjusted: bool = False
    # Real forecast for that date, when the trip is within the forecast horizon.
    weather: WeatherDay | None = None


class CostAssumptions(BaseModel):
    """Daily costs for the whole party, derived by code from the reference costs. Budget
    revisions lower them (cheaper lodging, street food, transit passes), which is how
    conflict resolution acts on more than the activity list."""

    lodging_per_night_usd: float = 0
    food_per_day_usd: float = 0
    transport_per_day_usd: float = 0


class ItineraryDraft(BaseModel):
    days: list[ItineraryDay]
    cost_assumptions: CostAssumptions
    revision: int = 0
    planner_notes: str = ""


class BudgetBreakdown(BaseModel):
    lodging: float = 0
    food: float = 0
    activities: float = 0
    transport: float = 0


class BudgetAnalysis(BaseModel):
    estimated_total_usd: float
    over_budget_by_usd: float
    breakdown: BudgetBreakdown
    within_budget: bool
    itinerary_revision: int = 0


class ConflictResolution(BaseModel):
    triggered: bool = False
    iterations: int = 0
    actions_taken: list[str] = Field(default_factory=list)  # human-readable
    applied_action_ids: list[str] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    resolved: bool | None = None
    # Actions chosen in the latest iteration, applied next by the itinerary revision step
    # (empty after an iteration that found nothing left in the catalog).
    last_action_ids: list[str] = Field(default_factory=list)


class FinalItinerary(BaseModel):
    summary: str
    days: list[ItineraryDay]
    total_cost_usd: float
    budget_usd: float
    within_budget: bool
    breakdown: BudgetBreakdown
    # The total converted to the destination's currency (live rate), when available.
    fx: FxQuote | None = None
    total_local: float | None = None


class TraceEntry(BaseModel):
    agent: AgentName
    event: TraceEvent
    timestamp: str = Field(default_factory=utc_now_iso)
    duration_ms: int | None = None
    message: str | None = None
    tokens: int | None = None
    decision: DecisionRecord | None = None


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def add_usage(current: dict[str, TokenUsage], new: dict[str, TokenUsage]) -> dict[str, TokenUsage]:
    """Reducer: sum token usage per agent across steps."""
    merged = {k: v.model_copy() for k, v in current.items()}
    for agent, usage in new.items():
        total = merged.setdefault(agent, TokenUsage())
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
    return merged


class SharedContext(BaseModel):
    session_id: str
    user_request: UserRequest
    interest_profile: InterestProfile | None = None
    live_data: LiveData | None = None
    destination_research: DestinationResearch | None = None
    itinerary_draft: ItineraryDraft | None = None
    budget_analysis: BudgetAnalysis | None = None
    conflict_resolution: ConflictResolution = Field(default_factory=ConflictResolution)
    final_itinerary: FinalItinerary | None = None
    # Reducers (LangGraph): steps return only their new trace entries / token usage and the
    # graph accumulates them, so the trace stays append-only.
    trace: Annotated[list[TraceEntry], operator.add] = Field(default_factory=list)
    # Tokens used per agent (summed across calls) — observability for cost control.
    token_usage: Annotated[dict[str, TokenUsage], add_usage] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return sum(u.total for u in self.token_usage.values())


# --------------------------------------------------------------------------- LLM output


class DestinationResearchOutput(BaseModel):
    season_notes: str
    recommended_areas: list[str]
    lodging_per_night_usd: float
    meal_avg_usd: float
    local_transport_day_usd: float


class CostsOutput(BaseModel):
    lodging_per_night_usd: float
    meal_avg_usd: float
    local_transport_day_usd: float


class SeasonNotesOutput(BaseModel):
    season_notes: str


class ItineraryDayOutput(BaseModel):
    day: int
    area: str
    activities: list[str]
    estimated_cost_usd: float
    free_alternative: str


class ItineraryPlanningOutput(BaseModel):
    days: list[ItineraryDayOutput]
