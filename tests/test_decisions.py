import asyncio

import pytest

from app.decisions import TypedQuestion, UnsupportedQuestion, build_decision_engine
from app.decisions.taxonomy import CATEGORY_OPTIONS

ENGINE = build_decision_engine("rules")


def ask(state, *questions):
    return asyncio.run(ENGINE.evaluate(state, list(questions)))


def category(interest):
    q = TypedQuestion(id="q", family="interest_category", kind="choice", options=CATEGORY_OPTIONS,
                      text=f"Category of {interest}?", params={"interest": interest})
    return ask({}, q)["q"]


@pytest.mark.parametrize("interest,expected", [
    ("ramen", "food"), ("Gastronomía", "food"), ("templos", "religion_heritage"),
    ("museos", "history_museums"), ("senderismo", "nature_outdoors"), ("vida nocturna", "nightlife"),
    ("xyzzy", "other"),
])
def test_interest_classification(interest, expected):
    ans = category(interest)
    assert ans.value == expected and ans.source == "rules"
    assert abs(sum(ans.probabilities.values()) - 1) < 0.02


def harm(action_id, categories):
    q = TypedQuestion(id="h", family="action_harm", kind="score", text="?", params={"action_id": action_id})
    return ask({"interest_categories": categories}, q)["h"].value


def test_action_harm_depends_on_interests():
    assert harm("free_alternatives", ["history_museums"]) > 0.8
    assert harm("free_alternatives", ["food"]) < 0.2
    assert harm("cheaper_lodging", ["relaxation", "food"]) == pytest.approx(0.5)
    assert 0 < harm("street_food", []) < 1


def test_plan_matches_interests():
    q = TypedQuestion(id="m", family="plan_matches_interests", kind="score", text="?")
    good = ask({"interests": ["ramen"], "interest_categories": ["food"], "activities": ["Ramen tasting tour"]}, q)
    bad = ask({"interests": ["ramen"], "interest_categories": ["food"], "activities": ["Free city walk"]}, q)
    assert good["m"].value > 0.9 > 0.2 > bad["m"].value


def test_unknown_question_family_is_rejected():
    with pytest.raises(UnsupportedQuestion):
        ask({}, TypedQuestion(id="x", family="nope", kind="score", text="?"))


def test_unknown_engine_is_rejected():
    with pytest.raises(ValueError):
        build_decision_engine("jev")
