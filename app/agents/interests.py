"""Interest intake (non-generative): maps each free-text interest to the fixed taxonomy and
checks whether a plan still matches the traveller's interests. Both are typed decisions
answered by the decision engine."""

from __future__ import annotations

from ..decisions import DecisionEngine, TypedQuestion
from ..decisions.taxonomy import CATEGORY_OPTIONS
from ..models import DecisionRecord, InterestMatch, InterestProfile, SharedContext


async def classify(ctx: SharedContext, engine: DecisionEngine) -> tuple[InterestProfile, list[DecisionRecord]]:
    interests = ctx.user_request.interests
    if not interests:
        return InterestProfile(matches=[]), []
    questions = [
        TypedQuestion(id=f"interest:{i}", family="interest_category", kind="choice", options=CATEGORY_OPTIONS,
                      text=f"Which travel-interest category best describes '{interest}'?",
                      params={"interest": interest})
        for i, interest in enumerate(interests)
    ]
    answers = await engine.evaluate({"traveller_interests": interests}, questions)
    matches, decisions = [], []
    for q, interest in zip(questions, interests):
        ans = answers[q.id]
        matches.append(InterestMatch(interest=interest, category=str(ans.value),
                                     probability=ans.probabilities.get(str(ans.value), 0.0)))
        decisions.append(DecisionRecord(question=f"category:{interest}", answer=ans.value,
                                        probabilities=_top(ans.probabilities), source=ans.source))
    return InterestProfile(matches=matches), decisions


async def plan_matches(ctx: SharedContext, engine: DecisionEngine) -> DecisionRecord:
    draft = ctx.itinerary_draft
    assert draft is not None
    state = {
        "interests": ctx.user_request.interests,
        "interest_categories": ctx.interest_profile.categories if ctx.interest_profile else [],
        "activities": [a for d in draft.days for a in d.activities],
    }
    q = TypedQuestion(id="plan_matches", family="plan_matches_interests", kind="score",
                      text="Does this itinerary still include activities that match the traveller's interests?")
    ans = (await engine.evaluate(state, [q]))[q.id]
    return DecisionRecord(question="plan_matches_interests", answer=ans.value,
                          probabilities=ans.probabilities, source=ans.source)


def _top(probs: dict[str, float], k: int = 3) -> dict[str, float]:
    return dict(sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:k])
