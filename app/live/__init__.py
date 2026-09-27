"""Live data providers: real weather, places and exchange rates, behind small interfaces.

Same pattern as the decision engine: the chain depends on the interfaces below, not on a
vendor. Every provider has a hard timeout and an in-memory TTL cache, and every failure is
non-fatal — the live-data step records why a piece is missing and the chain falls back to
the curated catalog or the model.

Phase 1 sources (free, no API key):
  * Geocoding + weather: Open-Meteo (forecast for trips within 16 days; the same dates of an
    earlier year from its historical archive beyond that). CC BY 4.0, non-commercial use.
  * Places: OpenStreetMap via the Overpass API — districts plus notable places (those linked
    to Wikidata) for destinations outside the catalog. ODbL.
  * Exchange rates: Frankfurter (European Central Bank reference rates), with
    ExchangeRate-API's open endpoint as a fallback for currencies the ECB doesn't publish.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Protocol

import httpx

from ..models import FxQuote, GeoPoint, PlacesReport, WeatherReport

log = logging.getLogger("trip_planner.live")

USER_AGENT = "trip-planner-demo/1.0 (+https://github.com/alulema/trip-planner)"


class LiveError(Exception):
    """A provider could not answer (network, timeout, bad payload, nothing found)."""


class Geocoder(Protocol):
    async def locate(self, query: str, country_hint: str | None = None) -> GeoPoint: ...


class WeatherProvider(Protocol):
    async def weather(self, point: GeoPoint, start: date, end: date) -> WeatherReport: ...


class PlacesProvider(Protocol):
    async def places(self, point: GeoPoint, lang: str) -> PlacesReport: ...


class FxProvider(Protocol):
    async def rate(self, currency: str) -> FxQuote: ...


class LiveServices:
    """The providers one chain run uses. `name` is shown in the trace and /api/config."""

    def __init__(self, name: str, geocoder: Geocoder, weather: WeatherProvider, places: PlacesProvider,
                 fx: FxProvider, timeout_seconds: float = 15.0):
        self.name = name
        self.geocoder = geocoder
        self.weather = weather
        self.places = places
        self.fx = fx
        self.timeout_seconds = timeout_seconds


class TTLCache:
    """Tiny in-memory cache (the container is ephemeral; nothing is persisted)."""

    def __init__(self, ttl_seconds: float, max_entries: int = 256):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, now: float | None = None) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        if (now if now is not None else time.monotonic()) >= hit[0]:
            del self._data[key]
            return None
        return hit[1]

    def put(self, key: Any, value: Any, now: float | None = None) -> None:
        if len(self._data) >= self.max_entries:
            self._data.pop(next(iter(self._data)))  # oldest insertion first
        self._data[key] = ((now if now is not None else time.monotonic()) + self.ttl, value)


class Http:
    """JSON over HTTP with a per-call timeout, a TTL cache and one error type.

    A client is opened per call (a handful of calls per trip), so nothing is bound to an
    event loop between requests. A timeout, a connection error or a 5xx is retried once
    (seen in CI: public APIs occasionally stall on one request and answer the next at
    once). `transport` lets tests plug in `httpx.MockTransport`."""

    def __init__(self, ttl_seconds: float, timeout_seconds: float = 6.0,
                 transport: httpx.AsyncBaseTransport | None = None, retries: int = 1, retry_delay: float = 0.3):
        self.cache = TTLCache(ttl_seconds)
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds))
        self.transport = transport
        self.retries = retries
        self.retry_delay = retry_delay

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", url, params=params)

    async def post_json(self, url: str, data: dict[str, str]) -> Any:
        return await self._request("POST", url, data=data)

    async def _request(self, method: str, url: str, **kw) -> Any:
        key = (method, url, repr(sorted((kw.get("params") or kw.get("data") or {}).items())))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                payload = await self._once(method, url, **kw)
            except LiveError as exc:
                retryable = exc.args[0].startswith(("timeout", "HTTP 5", "Connect", "Read", "Remote"))
                log.warning("live %s %s failed after %.1fs (attempt %d): %s", method, httpx.URL(url).host,
                            time.monotonic() - t0, attempt + 1, exc)
                if attempt < self.retries and retryable:
                    await asyncio.sleep(self.retry_delay)
                    continue
                raise
            self.cache.put(key, payload)
            return payload
        raise AssertionError("unreachable")

    async def _once(self, method: str, url: str, **kw) -> Any:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout,
                                         headers={"User-Agent": USER_AGENT}) as client:
                r = await client.request(method, url, **kw)
                r.raise_for_status()
                payload = r.json()
        except httpx.TimeoutException as exc:
            raise LiveError(f"timeout ({httpx.URL(url).host})") from exc
        except httpx.HTTPStatusError as exc:
            raise LiveError(f"HTTP {exc.response.status_code} ({httpx.URL(url).host})") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LiveError(f"{type(exc).__name__} ({httpx.URL(url).host})") from exc
        return payload


def build_live(mode: str, transport: httpx.AsyncBaseTransport | None = None,
               overpass_url: str | None = None) -> LiveServices | None:
    """"on" → real providers; "mock" → canned data (offline UI work, tests); "off" → none."""
    if mode == "off":
        return None
    if mode == "mock":
        from .mock import mock_services
        return mock_services()
    if mode != "on":
        raise ValueError(f"unknown LIVE_DATA mode: {mode!r}")
    from .fx import FxChain
    from .open_meteo import OpenMeteoGeocoder, OpenMeteoWeather
    from .osm import OverpassPlaces

    return LiveServices(
        "open-meteo+osm+ecb",
        geocoder=OpenMeteoGeocoder(Http(24 * 3600, transport=transport)),
        weather=OpenMeteoWeather(Http(3600, transport=transport)),
        places=OverpassPlaces(Http(24 * 3600, timeout_seconds=12, transport=transport, retries=0), url=overpass_url),
        fx=FxChain(Http(6 * 3600, transport=transport)),
    )
