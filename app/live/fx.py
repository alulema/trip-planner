"""Exchange rates: USD → the destination's currency.

Frankfurter serves the European Central Bank's daily reference rates (~30 currencies). For
currencies the ECB doesn't publish (CLP, COP, PEN, VND…) the chain falls back to
ExchangeRate-API's open endpoint (no key, daily updates, attribution required)."""

from __future__ import annotations

from ..models import FxQuote, SourceInfo
from . import Http, LiveError

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
OPEN_ER_URL = "https://open.er-api.com/v6/latest/USD"

ECB_CURRENCIES = frozenset(
    "AUD BGN BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KRW MXN MYR NOK NZD PHP "
    "PLN RON SEK SGD THB TRY ZAR".split())

_EURO = "AD AT BE BG CY DE EE ES FI FR GR HR IE IT LT LU LV MC ME MT NL PT SI SK SM VA XK"
_USD = "AS BQ EC FM GU MH MP PA PR PW SV TC TL US VG VI"
_OTHER = """
AE AED AF AFN AL ALL AM AMD AO AOA AR ARS AU AUD AW AWG AZ AZN BA BAM BB BBD BD BDT BH BHD BI BIF
BM BMD BN BND BO BOB BR BRL BS BSD BT BTN BW BWP BY BYN BZ BZD CA CAD CD CDF CH CHF CL CLP CN CNY
CO COP CR CRC CU CUP CV CVE CZ CZK DJ DJF DK DKK DO DOP DZ DZD EG EGP ER ERN ET ETB FJ FJD GB GBP
GE GEL GH GHS GM GMD GN GNF GT GTQ GY GYD HK HKD HN HNL HT HTG HU HUF ID IDR IL ILS IN INR IQ IQD
IR IRR IS ISK JM JMD JO JOD JP JPY KE KES KG KGS KH KHR KR KRW KW KWD KZ KZT LA LAK LB LBP LK LKR
LR LRD LS LSL LY LYD MA MAD MD MDL MG MGA MK MKD MM MMK MN MNT MO MOP MR MRU MU MUR MV MVR MW MWK
MX MXN MY MYR MZ MZN NA NAD NG NGN NI NIO NO NOK NP NPR NZ NZD OM OMR PE PEN PG PGK PH PHP PK PKR
PL PLN PY PYG QA QAR RO RON RS RSD RU RUB RW RWF SA SAR SB SBD SC SCR SD SDG SE SEK SG SGD SL SLE
SO SOS SR SRD SS SSP ST STN SY SYP SZ SZL TH THB TJ TJS TM TMT TN TND TO TOP TR TRY TT TTD TW TWD
TZ TZS UA UAH UG UGX UY UYU UZ UZS VE VES VN VND VU VUV WS WST YE YER ZA ZAR ZM ZMW
CI XOF SN XOF ML XOF BF XOF NE XOF TG XOF BJ XOF GW XOF CM XAF GA XAF CG XAF TD XAF CF XAF GQ XAF
PF XPF NC XPF LI CHF GL DKK FO DKK GI GIP JE GBP GG GBP IM GBP CW ANG SX ANG AI XCD AG XCD DM XCD
GD XCD KN XCD LC XCD VC XCD MS XCD KI AUD NR AUD TV AUD CK NZD NU NZD
"""


def _table() -> dict[str, str]:
    table = {c: "EUR" for c in _EURO.split()} | {c: "USD" for c in _USD.split()}
    words = _OTHER.split()
    table.update(zip(words[::2], words[1::2]))
    return table


CURRENCY_BY_COUNTRY = _table()


def currency_for(country_code: str) -> str | None:
    return CURRENCY_BY_COUNTRY.get((country_code or "").upper())


class FxChain:
    def __init__(self, http: Http):
        self.http = http

    async def rate(self, currency: str) -> FxQuote:
        currency = currency.upper()
        if currency == "USD":
            return FxQuote(currency="USD", rate=1.0, as_of="", source=SourceInfo(
                name="USD", url="", attribution="Local currency is the US dollar"))
        errors = []
        if currency in ECB_CURRENCIES:
            try:
                return await self._frankfurter(currency)
            except LiveError as exc:
                errors.append(str(exc))
        try:
            return await self._open_er(currency)
        except LiveError as exc:
            errors.append(str(exc))
        raise LiveError("; ".join(errors))

    async def _frankfurter(self, currency: str) -> FxQuote:
        payload = await self.http.get_json(FRANKFURTER_URL, {"base": "USD", "symbols": currency})
        rate = (payload.get("rates") or {}).get(currency)
        if not rate:
            raise LiveError(f"Frankfurter has no rate for {currency}")
        return FxQuote(currency=currency, rate=float(rate), as_of=str(payload.get("date", "")), source=SourceInfo(
            name="Frankfurter (ECB reference rates)", url="https://frankfurter.dev/",
            attribution="Exchange rates: European Central Bank via Frankfurter"))

    async def _open_er(self, currency: str) -> FxQuote:
        payload = await self.http.get_json(OPEN_ER_URL)
        rate = (payload.get("rates") or {}).get(currency) if payload.get("result") == "success" else None
        if not rate:
            raise LiveError(f"ExchangeRate-API has no rate for {currency}")
        return FxQuote(currency=currency, rate=float(rate), as_of=str(payload.get("time_last_update_utc", ""))[:16],
                       source=SourceInfo(name="ExchangeRate-API (open access)",
                                         url="https://www.exchangerate-api.com",
                                         attribution="Rates By Exchange Rate API"))
