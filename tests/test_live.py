"""Live data providers, tested against canned HTTP responses (httpx.MockTransport)."""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.agents import live_data
from app.agents.synthesis import budget_paragraph
from app.live import Http, LiveError, TTLCache, build_live
from app.live.fx import FxChain, currency_for
from app.live.mock import mock_services
from app.live.open_meteo import OpenMeteoGeocoder, OpenMeteoWeather
from app.live.osm import OverpassPlaces, build_query, pair_places
from app.llm_client import MockLLM
from app.models import GeoPoint, LiveData, SharedContext, UserRequest, WeatherDay, WeatherReport, SourceInfo
from app.orchestrator import Orchestrator
from tests.test_chain import ENGINE, traces

TODAY = datetime.now(timezone.utc).date()
POINT = GeoPoint(name="Valparaíso", country="Chile", country_code="CL", latitude=-33.04, longitude=-71.62,
                 population=300_000)


def transport(routes, calls=None):
    """Answer by host with a JSON body (or an int status code / exception)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        body = routes[request.url.host]
        if callable(body):
            body = body(request)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def daily(start: date, n: int, rain_day: int | None = None, prob=True):
    days = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    d = {"time": days, "temperature_2m_min": [11.2] * n, "temperature_2m_max": [22.6] * n,
         "weather_code": [1] * n}
    if prob:
        d["precipitation_probability_max"] = [80 if i == rain_day else 5 for i in range(n)]
    else:
        d["precipitation_sum"] = [4.0 if i == rain_day else 0.0 for i in range(n)]
    return {"daily": d}


# --------------------------------------------------------------------------- Open-Meteo


def test_geocoder_prefers_the_country_hint_then_population():
    results = {"results": [
        {"name": "Valencia", "country": "Venezuela", "country_code": "VE", "latitude": 10.1, "longitude": -68,
         "population": 1_400_000},
        {"name": "Valencia", "country": "Spain", "country_code": "ES", "latitude": 39.4, "longitude": -0.37,
         "population": 800_000},
    ]}
    geo = OpenMeteoGeocoder(Http(60, transport=transport({"geocoding-api.open-meteo.com": results})))
    assert asyncio.run(geo.locate("Valencia", "Spain")).country_code == "ES"
    assert asyncio.run(geo.locate("Valencia", "España")).country_code == "VE"  # hint unmatched → population
    empty = OpenMeteoGeocoder(Http(60, transport=transport({"geocoding-api.open-meteo.com": {}})))
    with pytest.raises(LiveError):
        asyncio.run(empty.locate("Nowhere"))


def test_trip_within_16_days_uses_the_real_forecast():
    start = TODAY + timedelta(days=3)
    calls = []
    w = OpenMeteoWeather(Http(60, transport=transport({"api.open-meteo.com": daily(start, 3, rain_day=1)}, calls)))
    report = asyncio.run(w.weather(POINT, start, start + timedelta(days=2)))
    assert report.kind == "forecast" and len(report.days) == 3
    assert [d.rainy for d in report.days] == [False, True, False]
    assert calls[0].url.params["start_date"] == start.isoformat()
    assert "CC BY 4.0" in report.source.attribution


def test_trip_beyond_the_horizon_uses_the_same_dates_of_an_earlier_year():
    start = TODAY + timedelta(days=60)
    calls = []
    w = OpenMeteoWeather(Http(60, transport=transport(
        {"archive-api.open-meteo.com": lambda r: daily(date.fromisoformat(r.url.params["start_date"]), 2,
                                                       rain_day=0, prob=False)}, calls)))
    report = asyncio.run(w.weather(POINT, start, start + timedelta(days=1)))
    assert report.kind == "reference" and report.days[0].rainy
    ref = date.fromisoformat(calls[0].url.params["start_date"])
    assert ref < TODAY and (start - ref).days in (365, 730)
    assert str(ref.year) in report.source.name


def test_weather_summary_is_written_by_code():
    req = UserRequest(destination="Lisbon", days=3, budget_usd=900, start_date=TODAY + timedelta(days=2))
    days = [WeatherDay(date=req.start_date + timedelta(days=i), t_min_c=14.4, t_max_c=31.2 if i else 24.0,
                       precip_probability=70 if i == 2 else 0) for i in range(3)]
    src = SourceInfo(name="x", url="", attribution="")
    text = live_data.weather_summary(WeatherReport(kind="forecast", days=days, source=src), req)
    assert text.startswith("Pronóstico para ") and "14–31 °C" in text and "1 de 3 días" in text
    assert "paraguas" in text and "calor" in text
    en = req.model_copy(update={"lang": "en"})
    ref = live_data.weather_summary(WeatherReport(kind="reference", days=days, source=src), en)
    assert ref.startswith("No forecast yet for") and "rained on 1 of 3 days" in ref


# --------------------------------------------------------------------------- OpenStreetMap


def node(name, lat, lon, **tags):
    return {"type": "node", "lat": lat, "lon": lon, "tags": {"name": name, **tags}}


ELEMENTS = [
    node("Cerro Alegre", -33.043, -71.627, place="neighbourhood"),
    node("Cerro Bellavista", -33.047, -71.620, place="neighbourhood"),
    node("Barrio Puerto", -33.037, -71.630, place="suburb"),
    node("Lejano", -33.20, -71.40, place="suburb"),
    node("Paseo Yugoslavo", -33.0432, -71.6272, tourism="viewpoint", wikidata="Q1", wikipedia="es:x"),
    node("Palacio Baburizza", -33.0431, -71.6268, tourism="museum", wikidata="Q2", wikipedia="es:y"),
    node("La Sebastiana", -33.0475, -71.6205, tourism="museum", wikidata="Q3", wikipedia="es:z"),
    {"type": "way", "center": {"lat": -33.0371, "lon": -71.6301},
     "tags": {"name": "Plaza Sotomayor", "historic": "monument", "wikidata": "Q4"}},
    node("Far Away Fort", -33.30, -71.10, historic="fort", wikidata="Q5"),
    node("Valparaíso", -33.04, -71.62, tourism="attraction", wikidata="Q6"),  # the city itself
    node("Some Shop", -33.043, -71.627, shop="yes", wikidata="Q7"),
]


def test_places_are_paired_with_their_nearest_district():
    report = pair_places(ELEMENTS, POINT, "es")
    assert list(report.area_highlights) == ["Cerro Alegre", "Cerro Bellavista", "Barrio Puerto"]
    assert report.area_highlights["Cerro Alegre"] == ["Palacio Baburizza", "Paseo Yugoslavo"]
    assert report.area_highlights["Cerro Bellavista"] == ["La Sebastiana"]
    assert report.area_free == {"Cerro Alegre": ["Paseo Yugoslavo"], "Cerro Bellavista": [],
                                "Barrio Puerto": ["Plaza Sotomayor"]}
    all_places = {h for hs in report.area_highlights.values() for h in hs}
    assert not {"Far Away Fort", "Valparaíso", "Some Shop"} & all_places
    assert "OpenStreetMap" in report.source.attribution


def test_places_prefer_a_latin_script_name():
    elements = [node("東山", 35.0, 135.78, place="suburb", **{"name:en": "Higashiyama"}),
                node("祇園", 35.004, 135.775, place="suburb", **{"name:en": "Gion"}),
                node("清水寺", 35.0001, 135.7801, tourism="attraction", wikidata="Q1", **{"name:en": "Kiyomizu-dera"}),
                node("八坂神社", 35.0041, 135.7751, tourism="attraction", wikidata="Q2", **{"name:es": "Santuario Yasaka"})]
    point = GeoPoint(name="Kyoto", latitude=35.0, longitude=135.77)
    report = pair_places(elements, point, "es")
    assert report.area_highlights == {"Gion": ["Santuario Yasaka"], "Higashiyama": ["Kiyomizu-dera"]}


def test_busy_overpass_instance_falls_back_to_the_next_one():
    calls = []
    places = OverpassPlaces(Http(60, transport=transport(
        {"overpass-api.de": 504, "overpass.private.coffee": {"elements": ELEMENTS}}, calls), retries=0))
    report = asyncio.run(places.places(POINT, "es"))
    assert [c.url.host for c in calls] == ["overpass-api.de", "overpass.private.coffee"]
    assert "Cerro Alegre" in report.area_highlights
    down = OverpassPlaces(Http(60, transport=transport({"overpass-api.de": 504, "overpass.private.coffee": 429}),
                               retries=0))
    with pytest.raises(LiveError, match="HTTP 504.*HTTP 429"):
        asyncio.run(down.places(POINT, "es"))


def test_too_few_districts_is_a_provider_error():
    places = OverpassPlaces(Http(60, transport=transport({"overpass-api.de": {"elements": ELEMENTS[:1]}})),
                            urls=("https://overpass-api.de/api/interpreter",))
    with pytest.raises(LiveError):
        asyncio.run(places.places(POINT, "es"))
    q = build_query(POINT)
    assert "-33.09390,-71.68430,-32.98610,-71.55570" in q  # ~6 km box around the centre
    assert "around" not in q and "nwr" not in q and "relation" not in q
    assert '["wikidata"]' in q and "out tags center" in q


# --------------------------------------------------------------------------- exchange rates


def test_fx_chain_uses_ecb_then_falls_back():
    calls = []
    fx = FxChain(Http(60, transport=transport({
        "api.frankfurter.dev": {"amount": 1, "base": "USD", "date": "2026-09-25", "rates": {"EUR": 0.9123}},
        "open.er-api.com": {"result": "success", "time_last_update_utc": "Fri, 25 Sep 2026 00:02:31 +0000",
                            "rates": {"CLP": 941.2, "EUR": 0.91}},
    }, calls)))
    eur = asyncio.run(fx.rate("EUR"))
    assert (eur.rate, eur.as_of, eur.source.name) == (0.9123, "2026-09-25", "Frankfurter (ECB reference rates)")
    clp = asyncio.run(fx.rate("CLP"))  # not published by the ECB
    assert clp.rate == 941.2 and clp.source.attribution == "Rates By Exchange Rate API"
    usd = asyncio.run(fx.rate("USD"))
    assert usd.rate == 1 and len(calls) == 2  # USD needs no call

    broken = FxChain(Http(60, transport=transport({
        "api.frankfurter.dev": 503,
        "open.er-api.com": {"result": "success", "rates": {"EUR": 0.91}},
    })))
    assert asyncio.run(broken.rate("EUR")).source.name.startswith("ExchangeRate-API")


def test_currency_table():
    assert (currency_for("PT"), currency_for("ec"), currency_for("JP"), currency_for("CL")) == ("EUR", "USD", "JPY", "CLP")
    assert currency_for("") is None and currency_for("ZZ") is None


# --------------------------------------------------------------------------- cache and errors


def test_ttl_cache_expires():
    c = TTLCache(10)
    c.put("k", 1, now=0)
    assert c.get("k", now=9.9) == 1 and c.get("k", now=10) is None


def test_http_caches_and_maps_errors():
    calls = []
    http = Http(60, transport=transport({"a.test": {"ok": 1}, "b.test": 500,
                                         "c.test": httpx.ConnectTimeout("slow")}, calls), retry_delay=0)
    assert asyncio.run(http.get_json("https://a.test/x", {"q": 1})) == {"ok": 1}
    assert asyncio.run(http.get_json("https://a.test/x", {"q": 1})) == {"ok": 1}
    assert len(calls) == 1
    with pytest.raises(LiveError, match="HTTP 500"):
        asyncio.run(http.get_json("https://b.test/"))
    with pytest.raises(LiveError, match="timeout"):
        asyncio.run(http.get_json("https://c.test/"))


def test_http_retries_once_after_a_stall():
    calls = []

    def flaky(request):
        return httpx.ReadTimeout("stall") if len(calls) == 1 else {"ok": 2}

    http = Http(60, transport=transport({"a.test": flaky}, calls), retry_delay=0)
    assert asyncio.run(http.get_json("https://a.test/")) == {"ok": 2} and len(calls) == 2
    calls.clear()
    no_retry = Http(60, transport=transport({"b.test": 404}, calls), retry_delay=0)
    with pytest.raises(LiveError, match="HTTP 404"):
        asyncio.run(no_retry.get_json("https://b.test/"))
    assert len(calls) == 1  # a 4xx is not retried


def test_build_live_modes():
    assert build_live("off") is None
    assert build_live("mock").name == "mock"
    assert build_live("on").name == "open-meteo+osm+ecb"
    with pytest.raises(ValueError):
        build_live("sometimes")


# --------------------------------------------------------------------------- the live-data step


def make_ctx(destination="Kyoto", start_in=3, days=3, lang="es", budget_usd=5000):
    req = UserRequest(destination=destination, days=days, budget_usd=budget_usd, lang=lang,
                      interests=["food"], start_date=TODAY + timedelta(days=start_in) if start_in is not None else None)
    return SharedContext(session_id="t", user_request=req)


def test_slow_provider_is_skipped_and_the_rest_is_kept():
    services = mock_services()

    class Slow:
        async def places(self, point, lang):
            await asyncio.sleep(5)

    services.places, services.timeout_seconds = Slow(), 0.05
    data = asyncio.run(live_data.run(make_ctx("Valparaíso"), services))
    assert data.errors == {"places": "timeout"}
    assert data.weather is not None and data.fx.currency == "CLP"
    assert "fallback: places (timeout)" in live_data.describe(data)


def test_unknown_location_skips_everything():
    data = asyncio.run(live_data.run(make_ctx("Testville"), mock_services()))
    assert data.location is None and list(data.errors) == ["location"]


def test_no_dates_means_no_weather():
    data = asyncio.run(live_data.run(make_ctx(start_in=None), mock_services()))
    assert data.weather is None and data.errors["weather"] == "no travel dates" and data.fx.currency == "JPY"


def test_catalog_city_does_not_ask_for_places():
    data = asyncio.run(live_data.run(make_ctx("Kioto"), mock_services()))
    assert data.places is None and "places" not in data.errors


# --------------------------------------------------------------------------- the chain with live data


async def run_live_chain(ctx, live=None):
    events = []

    async def emit(event, data):
        events.append((event, data))

    await Orchestrator(MockLLM(0), ENGINE, 6000, live=live or mock_services()).run(ctx, emit)
    return events


def test_catalog_city_with_dates_gets_the_season_from_real_weather_without_the_model():
    ctx = make_ctx("Kyoto")
    events = asyncio.run(run_live_chain(ctx))
    r = ctx.destination_research
    assert r.source == "catalog" and r.season_notes.startswith("Pronóstico para")
    assert ctx.token_usage["destination_research"].total == 0  # no generation needed
    assert [d.weather.rainy for d in ctx.itinerary_draft.days] == [False, True, False]
    final = ctx.final_itinerary
    assert final.fx.currency == "JPY" and final.total_local == round(final.total_cost_usd * 147.0, 2)
    assert "JPY" in final.summary and "1 USD = 147 JPY" in final.summary
    live_trace = [d for d in traces(events) if d["agent"] == "live_data"]
    assert [d["event"] for d in live_trace] == ["started", "completed"]
    assert "weather: forecast" in live_trace[1]["message"] and "fx: 1 USD = 147 JPY" in live_trace[1]["message"]


def test_rainy_forecast_day_is_flagged_for_the_planner():
    from app.agents.itinerary_planning import day_plan

    ctx = make_ctx("Kyoto")
    asyncio.run(run_live_chain(ctx))
    assert [e.get("rain", False) for e in day_plan(ctx)] == [False, True, False]


def test_reference_weather_is_not_used_per_day():
    from app.agents.itinerary_planning import day_plan

    ctx = make_ctx("Kyoto")
    services = mock_services()
    real = services.weather.weather

    async def as_reference(point, start, end):
        return (await real(point, start, end)).model_copy(update={"kind": "reference"})

    services.weather.weather = as_reference
    asyncio.run(run_live_chain(ctx, services))
    assert all(d.weather is None for d in ctx.itinerary_draft.days)
    assert not any("rain" in e for e in day_plan(ctx))
    assert ctx.destination_research.season_notes.startswith("Aún no hay pronóstico")


def test_unknown_city_gets_districts_and_places_from_openstreetmap():
    ctx = make_ctx("Valparaíso")
    events = asyncio.run(run_live_chain(ctx))
    r = ctx.destination_research
    assert r.source == "live"
    assert r.recommended_areas == ["Cerro Alegre", "Cerro Bellavista", "Barrio Puerto"]
    assert r.area_highlights["Cerro Bellavista"] == ["La Sebastiana", "Museo a Cielo Abierto"]
    assert ctx.token_usage["destination_research"].total > 0  # the model still estimates costs
    day1 = ctx.itinerary_draft.days[0]
    assert day1.area == "Cerro Alegre" and day1.free_alternative == "Paseo libre por Cerro Alegre: Paseo Yugoslavo"
    assert any("source: live places" in d.get("message", "") for d in traces(events, "completed"))


def test_live_failure_keeps_the_previous_behaviour():
    ctx = make_ctx("Testville")
    asyncio.run(run_live_chain(ctx))
    assert ctx.destination_research.source == "model" and ctx.final_itinerary.fx is None
    assert ctx.live_data.errors.keys() == {"location"}


def test_usd_destination_shows_no_conversion():
    ctx = make_ctx("Quito")
    asyncio.run(run_live_chain(ctx))
    assert ctx.live_data.fx.currency == "USD" and ctx.final_itinerary.fx is None
    assert "≈" not in budget_paragraph(ctx)


def test_live_data_off_means_no_node_output():
    ctx = make_ctx("Kyoto")
    events = asyncio.run(_run_off(ctx))
    assert ctx.live_data is None and not [d for d in traces(events) if d["agent"] == "live_data"]


async def _run_off(ctx):
    events = []

    async def emit(event, data):
        events.append((event, data))

    await Orchestrator(MockLLM(0), ENGINE, 6000, live=None).run(ctx, emit)
    return events


def test_start_date_is_validated():
    with pytest.raises(ValueError):
        UserRequest(destination="Kyoto", days=2, budget_usd=100, start_date=TODAY - timedelta(days=3))
    with pytest.raises(ValueError):
        UserRequest(destination="Kyoto", days=2, budget_usd=100, start_date=TODAY + timedelta(days=400))
    req = UserRequest(destination="Kyoto", days=3, budget_usd=100, start_date=TODAY)
    assert req.end_date == TODAY + timedelta(days=2)
    assert json.loads(LiveData().model_dump_json()) == {"location": None, "weather": None, "places": None,
                                                        "fx": None, "errors": {}}


# --------------------------------------------------------------------------- Wikidata


def binding(name, qid, lat, lon, links=0):
    return {"itemLabel": {"value": name}, "type": {"value": f"http://www.wikidata.org/entity/{qid}"},
            "coord": {"value": f"Point({lon} {lat})"}, "links": {"value": str(links)}}


WIKIDATA_ROWS = {"results": {"bindings": [
    binding("Cerro Alegre", "Q123705", -33.043, -71.627),
    binding("Cerro Bellavista", "Q123705", -33.047, -71.620),
    binding("Palacio Baburizza", "Q33506", -33.0431, -71.6268, links=8),
    binding("Paseo Yugoslavo", "Q6017969", -33.0432, -71.6272, links=2),
    binding("La Sebastiana", "Q207694", -33.0475, -71.6205, links=25),
    binding("Q98765432", "Q33506", -33.0476, -71.6206),  # unlabelled item
    binding("Valparaíso", "Q570116", -33.04, -71.62, links=90),  # the city itself
]}}


def test_wikidata_places_rank_by_sitelinks_and_pair_with_districts():
    from app.live.wikidata import WikidataPlaces, build_query

    calls = []
    places = WikidataPlaces(Http(60, transport=transport({"query.wikidata.org": WIKIDATA_ROWS}, calls)))
    report = asyncio.run(places.places(POINT, "es"))
    assert report.area_highlights == {"Cerro Bellavista": ["La Sebastiana"],
                                      "Cerro Alegre": ["Palacio Baburizza", "Paseo Yugoslavo"]}
    assert list(report.area_highlights)[0] == "Cerro Bellavista"  # 25 sitelinks outrank the rest
    assert report.area_free["Cerro Alegre"] == ["Paseo Yugoslavo"]
    assert report.source.name == "Wikidata (SPARQL)"
    q = calls[0].url.params["query"]
    assert "Point(-71.62 -33.04)" in q and 'wikibase:radius "6"' in q and '"es,en"' in q
    assert "wd:Q123705" in build_query(POINT, "en") and '"en"' in build_query(POINT, "en")


def test_places_chain_falls_back_to_overpass():
    from app.live.wikidata import PlacesChain, WikidataPlaces

    calls = []
    t = transport({"query.wikidata.org": 500, "overpass-api.de": {"elements": ELEMENTS}}, calls)
    chain = PlacesChain(WikidataPlaces(Http(60, transport=t, retries=0)),
                        OverpassPlaces(Http(60, transport=t, retries=0), urls=("https://overpass-api.de/x",)))
    report = asyncio.run(chain.places(POINT, "es"))
    assert report.source.name.startswith("OpenStreetMap") and [c.url.host for c in calls] == [
        "query.wikidata.org", "overpass-api.de"]
    both_down = PlacesChain(WikidataPlaces(Http(60, transport=transport({"query.wikidata.org": 500}), retries=0)))
    with pytest.raises(LiveError, match="HTTP 500"):
        asyncio.run(both_down.places(POINT, "es"))
