import asyncio
from types import SimpleNamespace

import pytest

from app.agents import budget as budget_agent
from app.guardrails import TokenBudget, TokenBudgetExceeded
from app.llm_client import AnthropicLLM, LLMError, MockLLM
from app.models import (
    CostAssumptions,
    DestinationResearchOutput,
    ItineraryDay,
    ItineraryDraft,
    SharedContext,
    UserRequest,
)
from app.orchestrator import ChainError, Orchestrator


def make_ctx(budget_usd=1200, days=5, travelers=1, lang="es"):
    req = UserRequest(destination="Kyoto", days=days, budget_usd=budget_usd, travelers=travelers,
                      interests=["food", "temples"], lang=lang)
    return SharedContext(session_id="test", user_request=req)


async def run_chain(ctx, llm=None, token_limit=8000):
    events = []

    async def emit(event, data):
        events.append((event, data))

    await Orchestrator(llm or MockLLM(0), token_limit).run(ctx, emit)
    return events


def test_budget_is_deterministic_arithmetic():
    ctx = make_ctx(budget_usd=500, days=2)
    ctx.itinerary_draft = ItineraryDraft(
        days=[ItineraryDay(day=1, area="A", activities=["x"], estimated_cost_usd=40),
              ItineraryDay(day=2, area="B", activities=["y"], estimated_cost_usd=10)],
        cost_assumptions=CostAssumptions(lodging_per_night_usd=100, food_per_day_usd=45, transport_per_day_usd=10),
    )
    a = budget_agent.analyze(ctx)
    assert a.breakdown.model_dump() == {"lodging": 200, "food": 90, "activities": 50, "transport": 20}
    assert a.estimated_total_usd == 360
    assert a.over_budget_by_usd == 0 and a.within_budget

    ctx.user_request.budget_usd = 300
    a = budget_agent.analyze(ctx)
    assert a.over_budget_by_usd == 60 and not a.within_budget


def test_happy_path_runs_every_agent_without_conflict():
    ctx = make_ctx(budget_usd=5000)
    events = asyncio.run(run_chain(ctx))
    agents = {d["agent"] for e, d in events if e == "trace"}
    assert agents == {"orchestrator", "destination_research", "itinerary_planning", "budget", "synthesis"}
    assert not ctx.conflict_resolution.triggered
    assert ctx.final_itinerary.within_budget
    assert len(ctx.final_itinerary.days) == 5
    # Sections stream progressively, in chain order.
    keys = [d["key"] for e, d in events if e == "section"]
    assert keys == ["destination_research", "itinerary_draft", "budget_analysis", "final_itinerary"]


def test_unrealistic_budget_triggers_bounded_conflict_loop():
    ctx = make_ctx(budget_usd=50)
    events = asyncio.run(run_chain(ctx))
    c = ctx.conflict_resolution
    assert c.triggered and c.iterations == 2 and c.resolved is False
    assert ctx.itinerary_draft.revision == 2
    assert ctx.final_itinerary.within_budget is False
    assert sum(1 for e, d in events if e == "trace" and d["event"] == "conflict_detected") == 2
    assert ctx.token_usage["conflict_resolution"].total > 0


def test_conflict_loop_can_converge():
    # 5-day mock plan costs ~$855 first, ~$620 after one revision.
    ctx = make_ctx(budget_usd=700)
    asyncio.run(run_chain(ctx))
    c = ctx.conflict_resolution
    assert c.triggered and c.iterations == 1 and c.resolved is True
    assert ctx.final_itinerary.within_budget


def test_token_budget_exhaustion_falls_back_honestly():
    # Enough for research + itinerary, not for the conflict loop or synthesis.
    ctx = make_ctx(budget_usd=50)
    events = asyncio.run(run_chain(ctx, token_limit=1300))
    assert ctx.final_itinerary is not None
    assert ctx.final_itinerary.within_budget is False
    assert any(d.get("message", "").startswith("conflict loop stopped") for e, d in events if e == "trace")
    assert "USD" in ctx.final_itinerary.summary  # deterministic template summary
    assert ctx.total_tokens <= 1300


def test_chain_error_when_mandatory_step_cannot_run():
    ctx = make_ctx()
    with pytest.raises(ChainError):
        asyncio.run(run_chain(ctx, token_limit=100))


class FlakyConflictLLM(MockLLM):
    async def complete(self, *, agent, **kw):
        if agent == "conflict_resolution":
            raise LLMError("boom")
        return await super().complete(agent=agent, **kw)


def test_llm_failure_in_conflict_loop_keeps_last_valid_draft():
    ctx = make_ctx(budget_usd=50)
    events = asyncio.run(run_chain(ctx, llm=FlakyConflictLLM(0)))
    assert ctx.final_itinerary is not None and not ctx.final_itinerary.within_budget
    assert ctx.itinerary_draft.revision == 0
    assert any(e == "trace" and d["event"] == "failed" for e, d in events)


# ---------------------------------------------------------------- Anthropic wrapper


class FakeMessages:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        text = self.texts.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            stop_reason="end_turn",
        )


def _llm_with(texts):
    llm = AnthropicLLM("claude-haiku-4-5")
    llm._client = SimpleNamespace(messages=FakeMessages(texts))
    return llm


VALID = DestinationResearchOutput(season_notes="s", recommended_areas=["a"], lodging_per_night_usd=1,
                                  meal_avg_usd=1, local_transport_day_usd=1, agent_notes="n").model_dump_json()


def call(llm, budget):
    return asyncio.run(llm.complete(agent="t", system="s", user="u", output_model=DestinationResearchOutput,
                                    max_tokens=500, budget=budget, mock=lambda: None))


def test_invalid_json_is_retried_once_and_usage_counted():
    llm = _llm_with(["not json", VALID])
    budget = TokenBudget(8000)
    out, usage = call(llm, budget)
    assert out.season_notes == "s"
    assert llm._client.messages.calls == 2
    assert usage.total == 300 and budget.used == 300


def test_invalid_json_twice_raises():
    llm = _llm_with(["nope", "{}"])
    with pytest.raises(LLMError):
        call(llm, TokenBudget(8000))


def test_budget_guard_blocks_calls_before_spending():
    llm = _llm_with([VALID])
    with pytest.raises(TokenBudgetExceeded):
        call(llm, TokenBudget(100))
    assert llm._client.messages.calls == 0
