"""Data contract shared by every agent in the chain (the `shared_context`).

One `SharedContext` object travels through the whole chain. Each agent receives the full
context and returns ONLY its own section; the orchestrator is the only component that
merges sections back in. Sections are append-only: once written, a key is only replaced
by a newer revision of the same section (e.g. `itinerary_draft.revision` 0 → 1), never
deleted.

Two kinds of models live here:

* Contract models (`UserRequest`, `DestinationResearch`, ...) — the shape of the context.
* `*Output` models — the exact JSON the generative agents must return. They are kept small
  (a CPU model pays for every token) and free of numeric constraints so they translate
  cleanly into JSON schemas; sanity checks happen in Python after parsing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

AgentName = Literal[
    "orchestrator",
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


class UserRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=60)
    days: int = Field(ge=1, le=7)
    budget_usd: float = Field(gt=0, le=100_000)
    travelers: int = Field(default=1, ge=1, le=8)
    interests: list[str] = Field(default_factory=list, max_length=5)
    lang: Literal["es", "en"] = "es"

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
    # "catalog" = areas, highlights and costs from the curated table; "model" = all from the LLM.
    source: Literal["catalog", "model"] = "model"
    highlights: list[str] = Field(default_factory=list)
    # Catalog only: which highlights belong to which area.
    area_highlights: dict[str, list[str]] = Field(default_factory=dict)


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


class FinalItinerary(BaseModel):
    summary: str
    days: list[ItineraryDay]
    total_cost_usd: float
    budget_usd: float
    within_budget: bool
    breakdown: BudgetBreakdown


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


class SharedContext(BaseModel):
    session_id: str
    user_request: UserRequest
    interest_profile: InterestProfile | None = None
    destination_research: DestinationResearch | None = None
    itinerary_draft: ItineraryDraft | None = None
    budget_analysis: BudgetAnalysis | None = None
    conflict_resolution: ConflictResolution = Field(default_factory=ConflictResolution)
    final_itinerary: FinalItinerary | None = None
    trace: list[TraceEntry] = Field(default_factory=list)
    # Tokens used per agent (summed across calls) — observability for cost control.
    token_usage: dict[str, TokenUsage] = Field(default_factory=dict)

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
