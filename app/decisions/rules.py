"""Rule-based `DecisionEngine`: keyword taxonomy + explicit heuristics.

Deterministic, instant and offline. It answers the same typed questions a model-based
engine (e.g. Jev) would, with probabilities derived from the evidence it found, so the rest
of the chain doesn't care which engine is plugged in.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from . import Answer, TypedQuestion, UnsupportedQuestion
from .taxonomy import ACTIONS_BY_ID, INTEREST_CATEGORIES, OTHER

SOURCE = "rules"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def category_hits(text: str) -> dict[str, int]:
    t = normalize(text)
    return {cat: sum(1 for kw in kws if kw in t) for cat, kws in INTEREST_CATEGORIES.items()}


def _clamp(p: float) -> float:
    # Rules never claim certainty: keep probabilities inside (0.02, 0.98).
    return round(min(0.98, max(0.02, p)), 3)


class RulesDecisionEngine:
    name = "rules"

    async def evaluate(self, state: dict[str, Any], questions: list[TypedQuestion]) -> dict[str, Answer]:
        answers = {}
        for q in questions:
            handler = getattr(self, f"_{q.family}", None)
            if handler is None:
                raise UnsupportedQuestion(q.family)
            answers[q.id] = handler(state, q)
        return answers

    # ------------------------------------------------------------------ choice questions

    def _interest_category(self, state: dict[str, Any], q: TypedQuestion) -> Answer:
        hits = category_hits(q.params["interest"])
        total = sum(hits.values())
        if total == 0:
            rest = 0.1 / (len(q.options) - 1)
            probs = {o: (0.9 if o == OTHER else rest) for o in q.options}
        else:
            # Evidence share, with a little mass kept for "other".
            probs = {o: 0.9 * hits.get(o, 0) / total for o in q.options}
            probs[OTHER] = probs.get(OTHER, 0) + 0.1
        best = max(probs, key=probs.get)
        return Answer(best, {k: round(v, 3) for k, v in probs.items() if v > 0.005}, SOURCE)

    # ------------------------------------------------------------------ score questions

    def _action_harm(self, state: dict[str, Any], q: TypedQuestion) -> Answer:
        action = ACTIONS_BY_ID[q.params["action_id"]]
        cats = [c for c in state.get("interest_categories", []) if c != OTHER]
        if not cats:
            p = 0.2
        else:
            harmed = sum(1 for c in cats if c in action.harms)
            p = 0.1 + 0.8 * harmed / len(cats)
        p = _clamp(p)
        return Answer(p, {"yes": p, "no": round(1 - p, 3)}, SOURCE)

    def _plan_matches_interests(self, state: dict[str, Any], q: TypedQuestion) -> Answer:
        activities = normalize(" ".join(state.get("activities", [])))
        raw = [normalize(i) for i in state.get("interests", [])]
        cats = [c for c in state.get("interest_categories", []) if c != OTHER]
        if not raw and not cats:
            p = 0.5
        else:
            checks = [i in activities for i in raw]
            checks += [any(kw in activities for kw in INTEREST_CATEGORIES[c]) for c in cats]
            p = 0.1 + 0.85 * sum(checks) / len(checks)
        p = _clamp(p)
        return Answer(p, {"yes": p, "no": round(1 - p, 3)}, SOURCE)
