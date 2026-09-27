"""Decision layer — the non-generative ("System One") half of the reasoning.

Some steps of the chain don't need to *write* anything: they need a judgment that picks
among known options ("which interest category is 'ramen'?", "would cheaper lodging hurt
this traveller?"). Those go through a `DecisionEngine`, shaped after typed-decision models
such as TypeSafe's Jev: the caller sends a *state* plus a list of typed questions and gets
back typed answers with probabilities — never free text.

Every question carries both a machine `family` (so a rule-based engine can dispatch on it)
and a natural-language `text` (what a model-based engine like Jev would evaluate). That
keeps the engines interchangeable: `RulesDecisionEngine` ships today; a Jev-backed engine
only has to implement `evaluate()`.

Arithmetic never goes through this layer — it stays in plain Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class TypedQuestion:
    id: str
    family: str
    text: str
    kind: Literal["choice", "score"]
    options: tuple[str, ...] = ()
    # Extra structured input a rule needs (e.g. which action is being judged).
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Answer:
    """`choice` → value is one of the options; `score` → value is P(yes) in [0, 1]."""

    value: str | float
    probabilities: dict[str, float]
    source: str


class UnsupportedQuestion(Exception):
    pass


class DecisionEngine(Protocol):
    name: str

    async def evaluate(self, state: dict[str, Any], questions: list[TypedQuestion]) -> dict[str, Answer]: ...


def build_decision_engine(kind: str) -> DecisionEngine:
    from .rules import RulesDecisionEngine

    if kind == "rules":
        return RulesDecisionEngine()
    raise ValueError(f"Unknown DECISION_ENGINE '{kind}' (available: rules)")
