"""Open-Meteo: geocoding and weather (free, no key; CC BY 4.0, non-commercial use)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..decisions.rules import normalize
from ..models import GeoPoint, SourceInfo, WeatherDay, WeatherReport
from . import Http, LiveError

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# The forecast API serves today + 15 days; the archive lags a few days behind today.
FORECAST_HORIZON_DAYS = 15
ARCHIVE_LAG_DAYS = 7

ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"


class OpenMeteoGeocoder:
    def __init__(self, http: Http):
        self.http = http

    async def locate(self, query: str, country_hint: str | None = None) -> GeoPoint:
        payload = await self.http.get_json(GEOCODING_URL, {"name": query, "count": 10, "language": "en",
                                                           "format": "json"})
        results = payload.get("results") or []
        if not results:
            raise LiveError(f"no match for {query!r}")
        if country_hint:
            hint = normalize(country_hint).strip()
            same = [r for r in results
                    if hint in (normalize(r.get("country", "")).strip(), normalize(r.get("country_code", "")))]
            results = same or results
        best = max(results, key=lambda r: r.get("population") or 0)
        return GeoPoint(name=best["name"], country=best.get("country", ""), country_code=best.get("country_code", ""),
                        latitude=best["latitude"], longitude=best["longitude"], population=best.get("population") or 0)


class OpenMeteoWeather:
    def __init__(self, http: Http, today: date | None = None):
        self.http = http
        self._today = today

    def today(self) -> date:
        return self._today or datetime.now(timezone.utc).date()

    async def weather(self, point: GeoPoint, start: date, end: date) -> WeatherReport:
        today = self.today()
        base = {"latitude": point.latitude, "longitude": point.longitude, "timezone": "auto"}
        if start >= today - timedelta(days=1) and end <= today + timedelta(days=FORECAST_HORIZON_DAYS):
            payload = await self.http.get_json(FORECAST_URL, {
                **base, "start_date": start.isoformat(), "end_date": end.isoformat(),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            })
            return WeatherReport(kind="forecast", days=_days(payload),
                                 source=SourceInfo(name="Open-Meteo (forecast)", url="https://open-meteo.com/",
                                                   attribution=ATTRIBUTION))
        # Beyond the forecast horizon there is no real forecast: show what the same dates were
        # like in the most recent year the archive fully covers — labelled as a reference.
        years = 1
        while end - _years(years) > today - timedelta(days=ARCHIVE_LAG_DAYS):
            years += 1
        ref_start, ref_end = start - _years(years), end - _years(years)
        payload = await self.http.get_json(ARCHIVE_URL, {
            **base, "start_date": ref_start.isoformat(), "end_date": ref_end.isoformat(),
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
        })
        return WeatherReport(kind="reference", days=_days(payload),
                             source=SourceInfo(name=f"Open-Meteo (historical, {ref_start.year})",
                                               url="https://open-meteo.com/", attribution=ATTRIBUTION))


def _years(n: int) -> timedelta:
    return timedelta(days=365 * n)


def _days(payload: dict) -> list[WeatherDay]:
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        raise LiveError("empty weather payload")

    def col(name: str) -> list:
        values = daily.get(name) or []
        return values + [None] * (len(times) - len(values))

    days = [
        WeatherDay(date=t, t_min_c=lo, t_max_c=hi, precip_probability=pp, precip_mm=mm, weather_code=code)
        for t, lo, hi, pp, mm, code in zip(times, col("temperature_2m_min"), col("temperature_2m_max"),
                                          col("precipitation_probability_max"), col("precipitation_sum"),
                                          col("weather_code"))
    ]
    if all(d.t_max_c is None for d in days):
        raise LiveError("weather payload without temperatures")
    return days
