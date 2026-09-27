"""0b. Live Data step — no LLM: geocode the destination, then fetch weather, places and the
exchange rate in parallel. Each provider is optional; a missing piece is recorded in
`errors` and the later agents fall back to the catalog or the model.

The weather summary is written by code from the numbers (same principle as the budget
paragraph): the model never restates a temperature it could get wrong."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

from .. import catalog
from ..live import LiveError, LiveServices
from ..live.fx import currency_for
from ..models import LiveData, SharedContext, UserRequest, WeatherReport

log = logging.getLogger("trip_planner.live_data")

MONTHS = {
    "es": "ene feb mar abr may jun jul ago sep oct nov dic".split(),
    "en": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
}


async def run(ctx: SharedContext, live: LiveServices) -> LiveData:
    req = ctx.user_request
    city = catalog.lookup(req.destination)
    if city is not None:
        query, hint = city.name, city.country
    else:
        query, _, hint = (p.strip() for p in req.destination.partition(","))
    data = LiveData()
    try:
        data.location = await _bounded(live.geocoder.locate(query, hint or None), live.timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — live data is optional by design
        data.errors["location"] = _describe(exc)
        return data

    calls: dict[str, Awaitable[Any]] = {}
    if req.start_date is not None:
        calls["weather"] = live.weather.weather(data.location, req.start_date, req.end_date)
    else:
        data.errors["weather"] = "no travel dates"
    if city is None:  # the catalog already has curated districts and highlights
        calls["places"] = live.places.places(data.location, req.lang)
    currency = currency_for(data.location.country_code)
    if currency:
        calls["fx"] = live.fx.rate(currency)
    else:
        data.errors["fx"] = f"unknown currency for {data.location.country_code or '?'}"

    results = await asyncio.gather(*(_bounded(c, live.timeout_seconds) for c in calls.values()),
                                   return_exceptions=True)
    for key, result in zip(calls, results):
        if isinstance(result, BaseException):
            data.errors[key] = _describe(result)
        else:
            setattr(data, key, result)
    if data.weather is not None:
        data.weather.summary = weather_summary(data.weather, req)
    return data


async def _bounded(call: Awaitable[Any], seconds: float) -> Any:
    async with asyncio.timeout(seconds):
        return await call


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, LiveError):
        return str(exc)
    log.warning("live data provider crashed: %r", exc)
    return type(exc).__name__


def describe(data: LiveData) -> str:
    """One trace line: what came back, and what is missing (and why)."""
    parts = []
    if data.weather:
        parts.append(f"weather: {data.weather.kind} ({data.weather.source.name})")
    if data.places:
        parts.append(f"places: {len(data.places.area_highlights)} areas ({data.places.source.name})")
    if data.fx:
        parts.append(f"fx: 1 USD = {data.fx.rate:,.4g} {data.fx.currency}"
                     + (f" ({data.fx.source.name})" if data.fx.currency != "USD" else ""))
    if data.errors:
        parts.append("fallback: " + ", ".join(f"{k} ({v})" for k, v in data.errors.items()))
    return " · ".join(parts) or "nothing to fetch"


def weather_summary(report: WeatherReport, req: UserRequest) -> str:
    es = req.lang == "es"
    lows = [d.t_min_c for d in report.days if d.t_min_c is not None]
    highs = [d.t_max_c for d in report.days if d.t_max_c is not None]
    rainy = sum(d.rainy for d in report.days)
    n = len(report.days)
    temps = f"{min(lows or highs):.0f}–{max(highs):.0f} °C"
    start, end = req.start_date, req.end_date
    span = _date(start, req.lang) if start == end else f"{_date(start, req.lang)} – {_date(end, req.lang)}"

    if es:
        rain = f"lluvia probable {rainy} de {n} días" if rainy else "sin lluvia a la vista"
        if report.kind == "forecast":
            text = f"Pronóstico para {span}: {temps}; {rain}."
        else:
            rain = f"llovió {rainy} de {n} días" if rainy else "no llovió"
            text = (f"Aún no hay pronóstico para {span}; de referencia, esas mismas fechas en "
                    f"{report.days[0].date.year} tuvieron {temps} y {rain}.")
    else:
        rain = f"rain likely on {rainy} of {n} days" if rainy else "no rain in sight"
        if report.kind == "forecast":
            text = f"Forecast for {span}: {temps}; {rain}."
        else:
            rain = f"it rained on {rainy} of {n} days" if rainy else "it stayed dry"
            text = (f"No forecast yet for {span}; for reference, the same dates in "
                    f"{report.days[0].date.year} saw {temps} and {rain}.")
    return f"{text} {_advice(max(highs), min(lows or highs), rainy, es)}"


def _advice(high: float, low: float, rainy: int, es: bool) -> str:
    tips = []
    if rainy:
        tips.append("paraguas" if es else "an umbrella")
    if high >= 30:
        tips.append("protección para el calor" if es else "sun and heat protection")
    if low <= 5:
        tips.append("abrigo" if es else "a warm coat")
    if not tips:
        tips.append("una capa ligera para la noche" if es else "a light layer for the evening")
    return ("Lleva " if es else "Pack ") + (" y " if es else " and ").join(tips) + "."


def _date(d, lang: str) -> str:
    month = MONTHS[lang][d.month - 1]
    return f"{d.day} {month}" if lang == "es" else f"{month} {d.day}"
