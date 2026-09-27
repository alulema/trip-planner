"""OpenStreetMap places via the Overpass API (ODbL) — for destinations outside the catalog.

One query returns the named districts around the city centre and the notable places near
it. "Notable" means linked to Wikidata, which filters out the long tail of minor POIs. Each
place is then paired, by code, with its nearest district — the same area↔highlight pairing
the catalog provides, so the itinerary keeps landmarks in the right district."""

from __future__ import annotations

import math

from ..decisions.rules import normalize
from ..models import GeoPoint, PlacesReport, SourceInfo
from . import Http, LiveError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"

MAX_AREAS = 3
HIGHLIGHTS_PER_AREA = 3
# A place further than this from every district centre is not paired with any of them.
MAX_PAIR_DISTANCE_M = 2500

# (tag, value) → base notability score, and whether visiting it is usually free.
KINDS: dict[tuple[str, str], tuple[float, bool]] = {
    ("tourism", "museum"): (3, False), ("tourism", "attraction"): (3, False),
    ("tourism", "gallery"): (2, False), ("tourism", "zoo"): (2, False),
    ("tourism", "viewpoint"): (2, True),
    ("historic", "castle"): (3, False), ("historic", "palace"): (3, False), ("historic", "fort"): (2, False),
    ("historic", "monument"): (2, True), ("historic", "memorial"): (1, True), ("historic", "ruins"): (2, True),
    ("historic", "church"): (2, True),
    ("leisure", "park"): (2, True), ("leisure", "garden"): (2, True),
}


def radius_for(point: GeoPoint) -> int:
    if point.population >= 2_000_000:
        return 9000
    if point.population >= 300_000:
        return 6000
    return 4000


def build_query(point: GeoPoint) -> str:
    r, lat, lon = radius_for(point), point.latitude, point.longitude
    around = f"(around:{r},{lat},{lon})"
    by_key: dict[str, list[str]] = {}
    for key, value in KINDS:
        by_key.setdefault(key, []).append(value)
    pois = "".join(f'nwr{around}["{k}"~"^({"|".join(vs)})$"]["wikidata"]["name"];' for k, vs in by_key.items())
    return (f'[out:json][timeout:10];(node{around}["place"~"^(suburb|quarter|neighbourhood)$"]["name"];'
            f"{pois});out tags center 400;")


class OverpassPlaces:
    def __init__(self, http: Http, url: str | None = None):
        self.http = http
        self.url = url or OVERPASS_URL

    async def places(self, point: GeoPoint, lang: str) -> PlacesReport:
        payload = await self.http.post_json(self.url, {"data": build_query(point)})
        report = pair_places(payload.get("elements") or [], point, lang)
        if report is None:
            raise LiveError("not enough districts with notable places")
        return report


def pair_places(elements: list[dict], point: GeoPoint, lang: str) -> PlacesReport | None:
    areas: list[tuple[str, float, float]] = []
    pois: list[tuple[str, float, float, float, bool]] = []
    city = normalize(point.name).strip()
    for el in elements:
        tags = el.get("tags") or {}
        name = _name(tags, lang)
        lat, lon = _coords(el)
        if not name or lat is None or normalize(name).strip() == city:
            continue
        if tags.get("place"):
            areas.append((name, lat, lon))
            continue
        kind = next((KINDS[(k, tags[k])] for k in ("tourism", "historic", "leisure") if (k, tags.get(k)) in KINDS), None)
        if kind is None:
            continue
        score = kind[0] + (2 if tags.get("wikipedia") else 0) + min(len(tags), 30) / 10
        pois.append((name, lat, lon, score, kind[1]))

    # Pair every place with its nearest district.
    by_area: dict[str, list[tuple[float, str, bool]]] = {}
    for name, lat, lon, score, free in pois:
        best = min(areas, key=lambda a: _distance(lat, lon, a[1], a[2]), default=None)
        if best is None or _distance(lat, lon, best[1], best[2]) > MAX_PAIR_DISTANCE_M:
            continue
        if normalize(name).strip() == normalize(best[0]).strip():
            continue
        by_area.setdefault(best[0], []).append((score, name, free))

    ranked = []
    for area, items in by_area.items():
        seen, top = set(), []
        for score, name, free in sorted(items, reverse=True):
            key = normalize(name).strip()
            if key not in seen:
                seen.add(key)
                top.append((score, name, free))
        top = top[:HIGHLIGHTS_PER_AREA]
        ranked.append((sum(s for s, _, _ in top), area, top))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    chosen = ranked[:MAX_AREAS]
    if len(chosen) < 2:
        return None
    return PlacesReport(
        area_highlights={area: [n for _, n, _ in top] for _, area, top in chosen},
        area_free={area: [n for _, n, free in top if free] for _, area, top in chosen},
        source=SourceInfo(name="OpenStreetMap (Overpass API)", url="https://www.openstreetmap.org/copyright",
                          attribution=ATTRIBUTION),
    )


def _name(tags: dict, lang: str) -> str:
    """Localised name when OSM has one; the local name if it is in Latin script; else English."""
    for candidate in (tags.get(f"name:{lang}"), tags.get("name"), tags.get("name:en")):
        if candidate and _latin(candidate):
            return " ".join(candidate.split())[:60]
    return ""


def _latin(text: str) -> bool:
    return all(ord(c) < 0x250 for c in text if c.isalpha())


def _coords(el: dict) -> tuple[float | None, float | None]:
    if "lat" in el:
        return el["lat"], el["lon"]
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))
