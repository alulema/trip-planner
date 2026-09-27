"""Curated destination catalog (app/data/cities.json): preset facts instead of generated ones.

A 1.5B model on a CPU gets geography and prices wrong (in the first real runs it placed
Shibuya in Kyoto and priced Hanoi above Lisbon). For popular destinations the facts that
must be right — real districts, well-known highlights, the cost level — come from this
hand-curated table; the model only writes around them. Unknown destinations fall back to
the model, and the trace says which source was used.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .decisions.rules import normalize

DATA = Path(__file__).resolve().parent / "data" / "cities.json"


@dataclass(frozen=True)
class City:
    name: str
    country: str
    areas: tuple[str, ...]
    highlights: tuple[str, ...]
    lodging_room_night_usd: float
    meal_usd: float
    transport_day_usd: float


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, City], tuple[str, ...]]:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    index: dict[str, City] = {}
    for c in raw["cities"]:
        city = City(c["name"], c["country"], tuple(c["areas"]), tuple(c["highlights"]),
                    c["lodging_room_night_usd"], c["meal_usd"], c["transport_day_usd"])
        for key in (c["name"], *c["aliases"]):
            index[normalize(key)] = city
    return index, tuple(index)


def lookup(destination: str) -> City | None:
    """Match "Kioto", "Kyoto, Japan", "lisboa" or a small typo ("Barcelonna")."""
    index, keys = _index()
    key = normalize(destination.split(",")[0]).strip()
    if key in index:
        return index[key]
    close = difflib.get_close_matches(key, keys, n=1, cutoff=0.85)
    return index[close[0]] if close else None


def all_cities() -> list[City]:
    return list({c.name: c for c in _index()[0].values()}.values())
