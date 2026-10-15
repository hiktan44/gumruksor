"""TCMB gösterge kurları ve gümrük kıymeti için geçerli döviz satış kuru.

4458 sayılı Gümrük Kanunu md. 30: gümrük kıymetinin belirlenmesinde döviz
cinsinden unsurlar, beyannamenin tescil tarihinde yürürlükte olan T.C. Merkez
Bankası döviz satış kuru ile Türk lirasına çevrilir. TCMB gösterge kurları
ilan edildikleri iş gününü izleyen günden itibaren geçerli olduğundan, tescil
tarihinde yürürlükte olan kur, tescil tarihinden önceki son iş gününün
bültenidir. Bu modül tam olarak bu kuralı uygular ve her sonuçta bültenin
tarihini, numarasını ve kaynağını verir.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

import httpx

from temporal import today_local

TCMB_TODAY_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
TCMB_ARCHIVE_URL = "https://www.tcmb.gov.tr/kurlar/{yyyymm}/{ddmmyyyy}.xml"
LEGAL_BASIS = "4458 sayılı Gümrük Kanunu md. 30 – tescil tarihinde yürürlükte olan TCMB döviz satış kuru"
MAX_LOOKBACK_DAYS = 12
DEFAULT_TIMEOUT = 15.0
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ExchangeRateError(RuntimeError):
    """TCMB bülteni alınamadığında veya döviz kodu bulunamadığında."""


@dataclass(frozen=True)
class RateQuote:
    currency: str
    unit: int
    name: str
    forex_buying: float | None
    forex_selling: float | None
    banknote_buying: float | None
    banknote_selling: float | None

    @property
    def customs_rate(self) -> float | None:
        """Bir birim döviz için döviz satış kuru (TL)."""
        if self.forex_selling is None or not self.unit:
            return None
        return self.forex_selling / self.unit


@dataclass(frozen=True)
class Bulletin:
    bulletin_date: date
    bulletin_no: str
    source_url: str
    rates: dict[str, RateQuote] = field(default_factory=dict)

    def quote(self, currency: str) -> RateQuote:
        code = normalise_currency(currency)
        if code == "TRY":
            return RateQuote("TRY", 1, "TÜRK LİRASI", 1.0, 1.0, 1.0, 1.0)
        try:
            return self.rates[code]
        except KeyError as exc:
            raise ExchangeRateError(f"{code} TCMB {self.bulletin_no} sayılı bültende yer almıyor.") from exc


def normalise_currency(value: str) -> str:
    code = (value or "").strip().upper().replace("TL", "TRY")
    aliases = {"EURO": "EUR", "DOLAR": "USD", "USD$": "USD", "$": "USD", "€": "EUR", "£": "GBP"}
    code = aliases.get(code, code)
    if not _CURRENCY_RE.match(code):
        raise ExchangeRateError(f"Geçersiz döviz kodu: {value!r}")
    return code


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_bulletin(xml_text: str, source_url: str = TCMB_TODAY_URL) -> Bulletin:
    """TCMB XML bültenini ayrıştırır (today.xml ve arşiv dosyaları aynı biçimdedir)."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ExchangeRateError(f"TCMB bülteni XML olarak okunamadı: {exc}") from exc
    if root.tag != "Tarih_Date":
        raise ExchangeRateError("TCMB bülteni beklenen biçimde değil (Tarih_Date kökü yok).")
    raw_date = root.attrib.get("Tarih", "")
    try:
        bulletin_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
    except ValueError as exc:
        raise ExchangeRateError(f"TCMB bülten tarihi okunamadı: {raw_date!r}") from exc
    rates: dict[str, RateQuote] = {}
    for node in root.findall("Currency"):
        code = (node.attrib.get("CurrencyCode") or node.attrib.get("Kod") or "").strip().upper()
        if not _CURRENCY_RE.match(code):
            continue
        unit_text = (node.findtext("Unit") or "1").strip()
        unit = int(_number(unit_text) or 1)
        rates[code] = RateQuote(
            currency=code,
            unit=unit,
            name=(node.findtext("Isim") or node.findtext("CurrencyName") or code).strip(),
            forex_buying=_number(node.findtext("ForexBuying")),
            forex_selling=_number(node.findtext("ForexSelling")),
            banknote_buying=_number(node.findtext("BanknoteBuying")),
            banknote_selling=_number(node.findtext("BanknoteSelling")),
        )
    if not rates:
        raise ExchangeRateError("TCMB bülteninde döviz satırı bulunamadı.")
    return Bulletin(
        bulletin_date=bulletin_date,
        bulletin_no=root.attrib.get("Bulten_No", "").strip(),
        source_url=source_url,
        rates=rates,
    )


def archive_url(day: date) -> str:
    return TCMB_ARCHIVE_URL.format(yyyymm=day.strftime("%Y%m"), ddmmyyyy=day.strftime("%d%m%Y"))


def candidate_bulletin_days(registration_date: date) -> list[date]:
    """Tescil tarihinden önceki günler, en yeniden eskiye (hafta sonları atlanır)."""
    days: list[date] = []
    cursor = registration_date - timedelta(days=1)
    while len(days) < MAX_LOOKBACK_DAYS:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


class ExchangeRateService:
    """TCMB bültenlerini getirir ve önbelleğe alır."""

    def __init__(self, http: httpx.AsyncClient | None = None, *, cache_ttl: float = 6 * 3600, miss_ttl: float = 900):
        self._http = http
        self._own_http = http is None
        self._cache: dict[str, tuple[float, Bulletin]] = {}
        self._missing: dict[str, float] = {}
        self._cache_ttl = cache_ttl
        self._miss_ttl = miss_ttl
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers={"User-Agent": "MevzuatMCP/1.4 (+https://gumruksor.com/)"})
        return self._http

    async def _fetch(self, url: str) -> Bulletin | None:
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]
        missed_at = self._missing.get(url)
        if missed_at is not None and now - missed_at < self._miss_ttl:
            return None
        try:
            response = await self._client().get(url)
        except httpx.HTTPError as exc:
            raise ExchangeRateError(f"TCMB'ye ulaşılamadı: {exc}") from exc
        if response.status_code == 404:
            self._missing[url] = now
            return None
        if response.status_code != 200:
            raise ExchangeRateError(f"TCMB {response.status_code} döndürdü ({url}).")
        bulletin = parse_bulletin(response.text, url)
        self._cache[url] = (now, bulletin)
        return bulletin

    async def latest(self) -> Bulletin:
        bulletin = await self._fetch(TCMB_TODAY_URL)
        if bulletin is None:
            raise ExchangeRateError("TCMB güncel bülteni bulunamadı.")
        return bulletin

    async def bulletin_in_force(self, registration_date: date) -> Bulletin:
        """Tescil tarihinde yürürlükte olan bülten: önceki son iş gününün bülteni."""
        async with self._lock:
            for day in candidate_bulletin_days(registration_date):
                bulletin = await self._fetch(archive_url(day))
                if bulletin is not None:
                    return bulletin
        raise ExchangeRateError(
            f"{registration_date.isoformat()} tarihinden önceki {MAX_LOOKBACK_DAYS} iş gününde TCMB bülteni bulunamadı."
        )

    async def customs_quote(self, currency: str, registration_date: date | None = None) -> dict:
        """Gümrük kıymeti için kullanılacak kur ve dayanağı."""
        code = normalise_currency(currency)
        target = registration_date or today_local()
        bulletin = await self.bulletin_in_force(target)
        quote = bulletin.quote(code)
        rate = quote.customs_rate
        if rate is None:
            raise ExchangeRateError(f"{code} için döviz satış kuru bültende boş.")
        return {
            "currency": code,
            "registration_date": target.isoformat(),
            "rate": round(rate, 6),
            "basis": "forex_selling",
            "basis_label": "TCMB döviz satış kuru",
            "unit": quote.unit,
            "bulletin_date": bulletin.bulletin_date.isoformat(),
            "bulletin_no": bulletin.bulletin_no,
            "source_url": bulletin.source_url,
            "legal_basis": LEGAL_BASIS,
            "quote": asdict(quote),
            "note": (
                f"{bulletin.bulletin_date.strftime('%d.%m.%Y')} tarihli {bulletin.bulletin_no} sayılı bülten, "
                f"{target.strftime('%d.%m.%Y')} tescil tarihi için yürürlükteki kurdur."
            ),
        }


def parse_registration_date(value: str | None) -> date | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ExchangeRateError(f"Tarih anlaşılamadı: {value!r} (YYYY-AA-GG veya GG.AA.YYYY bekleniyor)")
