"""Wikidata places via the public SPARQL endpoint (data CC0) — for destinations outside the
catalog.

One geo query returns, around the city centre, the items that are neighbourhoods/districts
and the items that are museums, viewpoints, monuments, parks… with their coordinates and
number of Wikipedia sitelinks (a good notability signal). The same code as for OpenStreetMap
then pairs each place with its nearest district.

Added after the CI runs showed the public Overpass instances timing out from shared runners;
Overpass stays as the fallback (see `PlacesChain`)."""

from __future__ import annotations

import logging
import re
import time

from ..decisions.rules import normalize
from ..models import GeoPoint, PlacesReport, SourceInfo
from . import Http, LiveError
from .osm import Area, Poi, latin, pair, radius_for

log = logging.getLogger("trip_planner.live")

SPARQL_URL = "https://query.wikidata.org/sparql"
ATTRIBUTION = "Wikidata (CC0)"

# Wikidata class → (kind, base score, free to visit). "area" marks districts.
CLASSES: dict[str, tuple[str, float, bool]] = {
    "Q123705": ("area", 0, False),     # neighbourhood
    "Q2983893": ("area", 0, False),    # quarter
    "Q188509": ("area", 0, False),     # suburb
    "Q33506": ("museum", 3, False),    # museum
    "Q207694": ("museum", 3, False),   # art museum
    "Q570116": ("attraction", 3, False),  # tourist attraction
    "Q16560": ("palace", 3, False),    # palace
    "Q23413": ("castle", 3, False),    # castle
    "Q6017969": ("viewpoint", 2, True),  # scenic viewpoint
    "Q4989906": ("monument", 2, True),   # monument
    "Q174782": ("square", 2, True),      # town square
    "Q22698": ("park", 2, True),         # park
    "Q16970": ("church", 2, True),       # church building
    "Q2977": ("church", 3, True),        # cathedral
}


def build_query(point: GeoPoint, lang: str) -> str:
    values = " ".join(f"wd:{q}" for q in CLASSES)
    km = radius_for(point) / 1000
    langs = f"{lang},en" if lang != "en" else "en"
    return f"""SELECT ?item ?itemLabel ?type ?coord ?links WHERE {{
  SERVICE wikibase:around {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point({point.longitude} {point.latitude})"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "{km:g}" .
  }}
  VALUES ?type {{ {values} }}
  ?item wdt:P31 ?type .
  ?item wikibase:sitelinks ?links .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{langs}". }}
}} LIMIT 600"""


POINT_RE = re.compile(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)")


def parse_bindings(rows: list[dict], point: GeoPoint) -> tuple[list[Area], list[Poi]]:
    areas: list[Area] = []
    pois: list[Poi] = []
    city = normalize(point.name).strip()
    for row in rows:
        name = row.get("itemLabel", {}).get("value", "")
        qid = row.get("type", {}).get("value", "").rsplit("/", 1)[-1]
        m = POINT_RE.search(row.get("coord", {}).get("value", ""))
        # Unlabelled items come back as their Q-id: skip them, like non-Latin names.
        if not m or qid not in CLASSES or not name or re.fullmatch(r"Q\d+", name) or not latin(name):
            continue
        if normalize(name).strip() == city:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        kind, base, free = CLASSES[qid]
        if kind == "area":
            areas.append((name, lat, lon))
        else:
            links = int(row.get("links", {}).get("value", 0) or 0)
            pois.append((name, lat, lon, base + min(links, 40) / 5, free))
    return areas, pois


class WikidataPlaces:
    def __init__(self, http: Http, url: str = SPARQL_URL):
        self.http = http
        self.url = url

    async def places(self, point: GeoPoint, lang: str) -> PlacesReport:
        t0 = time.monotonic()
        payload = await self.http.get_json(self.url, {"query": build_query(point, lang), "format": "json"})
        rows = (payload.get("results") or {}).get("bindings") or []
        areas, pois = parse_bindings(rows, point)
        log.info("wikidata: %d rows (%d areas, %d places) in %.1fs", len(rows), len(areas), len(pois),
                 time.monotonic() - t0)
        report = pair(areas, pois, SourceInfo(name="Wikidata (SPARQL)", url="https://www.wikidata.org/",
                                              attribution=ATTRIBUTION))
        if report is None:
            raise LiveError("Wikidata: not enough districts with notable places")
        return report


class PlacesChain:
    """Try each places provider in order; the first usable answer wins."""

    def __init__(self, *providers):
        self.providers = providers

    async def places(self, point: GeoPoint, lang: str) -> PlacesReport:
        errors = []
        for provider in self.providers:
            try:
                return await provider.places(point, lang)
            except LiveError as exc:
                errors.append(str(exc))
        raise LiveError("; ".join(errors))
