"""Canned live data (LIVE_DATA=mock): offline UI work and tests, no network."""

from __future__ import annotations

from datetime import date, timedelta

from ..models import FxQuote, GeoPoint, PlacesReport, SourceInfo, WeatherDay, WeatherReport
from . import LiveError, LiveServices

MOCK_SOURCE = SourceInfo(name="mock", url="", attribution="Simulated data (offline mode)")
# A few known points so the offline demo shows local currencies.
POINTS = {
    "kyoto": ("Kyoto", "Japan", "JP"), "lisbon": ("Lisbon", "Portugal", "PT"),
    "quito": ("Quito", "Ecuador", "EC"), "valparaiso": ("Valparaíso", "Chile", "CL"),
}


class MockGeocoder:
    async def locate(self, query: str, country_hint: str | None = None) -> GeoPoint:
        from ..decisions.rules import normalize

        name, country, code = POINTS.get(normalize(query).strip(), (query, "", ""))
        if not code:
            raise LiveError(f"no match for {query!r} (mock)")
        return GeoPoint(name=name, country=country, country_code=code, latitude=0, longitude=0, population=500_000)


class MockWeather:
    async def weather(self, point: GeoPoint, start: date, end: date) -> WeatherReport:
        n = (end - start).days + 1
        days = [WeatherDay(date=start + timedelta(days=i), t_min_c=12 + i, t_max_c=21 + i,
                           precip_probability=70 if i == 1 else 10, weather_code=61 if i == 1 else 1)
                for i in range(n)]
        return WeatherReport(kind="forecast", days=days, source=MOCK_SOURCE)


class MockPlaces:
    async def places(self, point: GeoPoint, lang: str) -> PlacesReport:
        if point.name != "Valparaíso":
            raise LiveError("no places (mock)")
        return PlacesReport(
            area_highlights={"Cerro Alegre": ["Paseo Yugoslavo", "Palacio Baburizza"],
                             "Cerro Bellavista": ["La Sebastiana", "Museo a Cielo Abierto"],
                             "Barrio Puerto": ["Plaza Sotomayor"]},
            area_free={"Cerro Alegre": ["Paseo Yugoslavo"], "Cerro Bellavista": ["Museo a Cielo Abierto"],
                       "Barrio Puerto": ["Plaza Sotomayor"]},
            source=MOCK_SOURCE)


class MockFx:
    RATES = {"JPY": 147.0, "EUR": 0.92, "CLP": 940.0}

    async def rate(self, currency: str) -> FxQuote:
        if currency == "USD":
            return FxQuote(currency="USD", rate=1.0, as_of="", source=MOCK_SOURCE)
        if currency not in self.RATES:
            raise LiveError(f"no rate for {currency} (mock)")
        return FxQuote(currency=currency, rate=self.RATES[currency], as_of="2026-01-01", source=MOCK_SOURCE)


def mock_services() -> LiveServices:
    return LiveServices("mock", MockGeocoder(), MockWeather(), MockPlaces(), MockFx(), timeout_seconds=2)

