import asyncio

import pytest

from app.agents import budget as budget_agent
from app.agents import conflict_resolution
from app.decisions import build_decision_engine
from app.decisions.taxonomy import ACTION_CATALOG
from app.llm_client import LLMError, MockLLM
from app.models import CostAssumptions, ItineraryDay, ItineraryDraft, SharedContext, UserRequest
from app.orchestrator import ChainError, Orchestrator

ENGINE = build_decision_engine("rules")


def make_ctx(budget_usd=1200, days=5, travelers=1, interests=("food", "temples"), lang="es",
             destination="Testville"):
    # Default destination is NOT in the catalog, so the numbers below come from the mock model.
    req = UserRequest(destination=destination, days=days, budget_usd=budget_usd, travelers=travelers,
                      interests=list(interests), lang=lang)
    return SharedContext(session_id="test", user_request=req)


async def run_chain(ctx, llm=None, token_limit=6000):
    events = []

    async def emit(event, data):
        events.append((event, data))

    await Orchestrator(llm or MockLLM(0), ENGINE, token_limit).run(ctx, emit)
    return events


def traces(events, event=None):
    return [d for e, d in events if e == "trace" and (event is None or d["event"] == event)]


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


def test_happy_path_runs_every_step_without_conflict():
    ctx = make_ctx(budget_usd=5000)
    events = asyncio.run(run_chain(ctx))
    agents = {d["agent"] for d in traces(events)}
    assert agents == {"orchestrator", "destination_research", "itinerary_planning", "budget", "synthesis"}
    assert not ctx.conflict_resolution.triggered
    assert ctx.final_itinerary.within_budget and len(ctx.final_itinerary.days) == 5
    keys = [d["key"] for e, d in events if e == "section"]
    assert keys == ["interest_profile", "destination_research", "itinerary_draft", "budget_analysis", "final_itinerary"]
    # Interests were classified by the decision engine, visible in the trace.
    assert [m.category for m in ctx.interest_profile.matches] == ["food", "religion_heritage"]
    assert len(traces(events, "decision")) == 2
    # Generation progress and the streamed narrative reach the client.
    assert any(e == "progress" for e, _ in events)
    streamed = "".join(d["text"] for e, d in events if e == "token")
    assert streamed == ctx.final_itinerary.summary


def test_unrealistic_budget_triggers_bounded_conflict_loop_without_generation():
    ctx = make_ctx(budget_usd=50)
    events = asyncio.run(run_chain(ctx))
    c = ctx.conflict_resolution
    assert c.triggered and c.iterations == 2 and c.resolved is False
    assert ctx.itinerary_draft.revision == 2
    assert ctx.final_itinerary.within_budget is False
    assert len(traces(events, "conflict_detected")) == 2
    # The loop is decisions + arithmetic: no tokens spent by conflict resolution or revisions.
    assert "conflict_resolution" not in ctx.token_usage
    assert ctx.token_usage["itinerary_planning"].total > 0  # only the initial generation
    assert len(c.applied_action_ids) == len(set(c.applied_action_ids)) == 4
    assert all(d.source == "rules" for d in c.decisions)


def test_conflict_loop_converges_with_the_highest_utility_action():
    # Mock plan: 5 days → lodging 425 + food 210 + activities 175 + transport 45 = 855.
    ctx = make_ctx(budget_usd=700)
    asyncio.run(run_chain(ctx))
    c = ctx.conflict_resolution
    assert c.iterations == 1 and c.resolved is True
    assert c.applied_action_ids == ["free_alternatives"]  # saves 175 and hurts neither food nor temples
    assert all(d.adjusted and d.estimated_cost_usd == 0 for d in ctx.final_itinerary.days)
    assert ctx.final_itinerary.total_cost_usd == 680


def test_harmful_action_is_ranked_down():
    # A museum lover: swapping paid entrances for free walks should lose to cheaper lodging.
    ctx = make_ctx(budget_usd=780, interests=("museos",))
    asyncio.run(run_chain(ctx))
    assert ctx.conflict_resolution.applied_action_ids[0] == "cheaper_lodging"


def test_catalog_exhaustion_returns_empty_plan():
    ctx = make_ctx(budget_usd=50)
    asyncio.run(run_chain(ctx))
    ctx.conflict_resolution.applied_action_ids = [a.id for a in ACTION_CATALOG]
    plan = asyncio.run(conflict_resolution.run(ctx, ENGINE))
    assert plan.action_ids == [] and plan.decisions == []


def test_token_budget_exhaustion_falls_back_to_template():
    probe = make_ctx(budget_usd=5000)
    asyncio.run(run_chain(probe))
    needed = probe.token_usage["destination_research"].total + probe.token_usage["itinerary_planning"].total
    ctx = make_ctx(budget_usd=5000)
    events = asyncio.run(run_chain(ctx, token_limit=needed + 100))
    assert "synthesis" not in ctx.token_usage
    assert any(d.get("message") == "template fallback (no LLM)" for d in traces(events))
    assert "USD" in ctx.final_itinerary.summary


def test_chain_error_when_mandatory_step_cannot_run():
    with pytest.raises(ChainError):
        asyncio.run(run_chain(make_ctx(), token_limit=100))


class BrokenSynthesisLLM(MockLLM):
    async def complete_text(self, **kw):
        raise LLMError("model crashed")


def test_llm_failure_in_synthesis_keeps_an_honest_result():
    ctx = make_ctx(budget_usd=50)
    events = asyncio.run(run_chain(ctx, llm=BrokenSynthesisLLM(0)))
    assert traces(events, "failed")[0]["agent"] == "synthesis"
    assert ctx.final_itinerary.within_budget is False
    assert "excede" in ctx.final_itinerary.summary


class LyingNarrativeLLM(MockLLM):
    """Reproduces what the real 1.5B model did in the first e2e run."""

    async def complete_text(self, *, agent, system, user, max_tokens, budget, mock, on_token=None):
        assert "$" not in user and "budget" not in user.lower()  # the model never sees numbers
        text = "You can expect to spend $366, which is within your budget of $50. Day 1 in Baixa is lovely."
        if on_token:
            await on_token(text)
        return text, (await super().complete_text(agent=agent, system=system, user=user, max_tokens=max_tokens,
                                                  budget=budget, mock=mock))[1]


def test_budget_facts_in_the_summary_come_from_code_not_the_model():
    ctx = make_ctx(budget_usd=50, lang="en")
    asyncio.run(run_chain(ctx, llm=LyingNarrativeLLM(0)))
    summary = ctx.final_itinerary.summary
    assert "within your budget" not in summary and "$366" not in summary
    assert "Day 1 in Baixa is lovely." in summary
    assert f"${ctx.final_itinerary.total_cost_usd:,.0f} USD" in summary
    assert "over budget" in summary


def test_clean_area_keeps_only_the_place_name():
    from app.agents.destination_research import clean_area

    assert clean_area("Baixa - the historic heart of Lisbon") == "Baixa"
    assert clean_area("Chiado (shopping and nightlife)") == "Chiado"
    assert clean_area("Gion, Kyoto") == "Gion"
    assert clean_area("Higashiyama") == "Higashiyama"


def test_catalog_destination_uses_curated_facts_and_the_model_only_for_the_season():
    ctx = make_ctx(budget_usd=5000, destination="Kioto", travelers=3)
    events = asyncio.run(run_chain(ctx))
    r = ctx.destination_research
    assert r.source == "catalog"
    assert r.recommended_areas == ["Higashiyama", "Gion", "Arashiyama"]
    assert "Fushimi Inari Taisha" in r.highlights
    assert r.reference_costs.lodging_per_night_usd == 240  # 2 rooms for 3 travelers
    assert any(d.get("message", "").startswith("source: catalog") for d in traces(events, "completed"))
    assert {d.area for d in ctx.itinerary_draft.days} <= set(r.recommended_areas)


def test_unknown_destination_falls_back_to_the_model():
    ctx = make_ctx(budget_usd=5000, destination="Valparaíso")
    events = asyncio.run(run_chain(ctx))
    assert ctx.destination_research.source == "model" and ctx.destination_research.highlights == []
    assert any(d.get("message") == "source: model (not in catalog)" for d in traces(events, "completed"))


def test_truncated_last_sentence_is_dropped():
    from app.agents.synthesis import clean_narrative

    assert clean_narrative("Visit Gion. Then the market. Also do not forget to") == "Visit Gion. Then the market."
    assert clean_narrative("A single unfinished sentence") == "A single unfinished sentence"
