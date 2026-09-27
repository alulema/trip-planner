import json

import pytest

from app import catalog
from app.decisions.rules import normalize


@pytest.mark.parametrize("query,expected", [
    ("Kioto", "Kyoto"), ("Kyoto, Japan", "Kyoto"), ("lisboa", "Lisbon"), ("Barcelonna", "Barcelona"),
    ("Hanói", "Hanoi"), ("CDMX", "Mexico City"), ("Nueva York", "New York"), ("Valparaíso", None),
])
def test_lookup(query, expected):
    city = catalog.lookup(query)
    assert (city.name if city else None) == expected


def test_catalog_data_is_well_formed():
    raw = json.loads(catalog.DATA.read_text(encoding="utf-8"))
    names, keys = set(), []
    for c in raw["cities"]:
        assert c["name"] not in names
        names.add(c["name"])
        assert len(c["areas"]) == 3
        for area in c["areas"]:
            assert "(" not in area["name"] and " - " not in area["name"]
            assert 1 <= len(area["highlights"]) <= 3, (c["name"], area["name"])
        highlights = [h for a in c["areas"] for h in a["highlights"]]
        assert len(highlights) == len(set(highlights)), c["name"]  # a place lives in one area only
        assert 20 <= c["lodging_room_night_usd"] <= 400
        assert 2 <= c["meal_usd"] <= 60
        assert 1 <= c["transport_day_usd"] <= 30
        keys += [normalize(k) for k in (c["name"], *c["aliases"])]
    # No alias may point at two different cities.
    by_key = {}
    for c in raw["cities"]:
        for k in (c["name"], *c["aliases"]):
            assert by_key.setdefault(normalize(k), c["name"]) == c["name"]
    assert len(catalog.all_cities()) == len(raw["cities"])
