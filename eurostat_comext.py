"""AB dış ticaret istatistiği: Eurostat Comext açık API'si.

Bu modül **istatistik** verir, **oran** vermez. Sorusu şudur: *bir AB ülkesi bu ürünü
hangi ülkelerden alıyor (ya da kime satıyor), Türkiye'nin payı ve sırası ne, kilogram
başına ne ödeniyor?* Cevap ihracat pazar araştırması ve alıcı sunumu içindir. Buradan
gelen hiçbir sayı gümrük vergisi, KDV ya da maliyet hesabına girmez.

Uç: ``https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/DS-045409``
("EU trade since 1988 by HS2-4-6 and CN8"). Anahtarsız ve ücretsizdir. 18.09.2026'da
canlı ölçüldü; kodun dayandığı olgular:

* Yanıt **JSON-stat 2.0**: ``id`` boyut sırası, ``size`` boyut boyları, her boyutta
  ``category.index`` (kod → konum) ve ``category.label`` (kod → ad), ``value`` düz
  indeks → sayı. Son boyut en hızlı değişir. **Ülke adları yanıtın içinde gelir**;
  ayrı referans tablosu gerekmez.
* Partner boyutu 269 ISO2 ülke + 9 toplam koddan (``WORLD``, ``EXT_EU27_2020``,
  ``INT_EU27_2020``…) oluşur. **İki harfli kodların toplamı WORLD'e birebir eşit**
  (Almanya 851713 ithalatı 2024: 11.056.799.487 EUR). Sıralamaya yalnız ISO2 girer;
  toplam kodlar payda değil, çift sayım kaynağıdır.
* Ağırlık göstergesi ``QUANTITY_IN_100KG``: kilogram = değer × 100.
* Ürün kodu HS2/4/6, CN8 ve ``TOTAL`` kabul edilir. Türk GTİP'inin ilk 8 hanesi CN8'dir;
  önce 8 hane denenir, boş dönerse HS6'ya düşülür ve sonuç işaretlenir.
* Geçersiz ürün, veri olmayan yıl ya da ülke **200 ve boş ``value``** döner; hata kodu
  yoktur. Boş yanıt "bulunamadı" demektir, arıza değil.
* Akış: ``flow=1`` ithalat, ``flow=2`` ihracat. Yunanistan raporlayan kodu ``EL``.
* Arka arkaya 5 istekte sınır görülmedi; yine de istekler seri ve aralıklıdır.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic as _monotonic
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from security_firewall import validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

COMEXT_BASE = (os.environ.get("COMEXT_BASE_URL") or "https://ec.europa.eu").rstrip("/")
COMEXT_DATASET = "DS-045409"
_COMEXT_HOSTS = frozenset({"ec.europa.eu"})
COMEXT_SITE_URL = "https://ec.europa.eu/eurostat/web/international-trade-in-goods/database"

_MAX_BYTES = 16 * 1024 * 1024
_MAX_REDIRECTS = 4

SOURCE_NOTE = (
    "Kaynak: Eurostat Comext (AB üye devletlerinin resmî dış ticaret istatistiği). "
    "Ülkelerin kendi beyanıdır; gümrük vergisi veya maliyet hesabı değildir."
)
STATISTIC_ONLY_NOTE = (
    "Bu rakamlar pazar büyüklüğü, rakip ülke ve birim fiyat göstergesidir. Vergi oranı, "
    "KDV ve maliyet kalemleri buradan okunmaz; onlar resmî tarife kaynaklarından gelir."
)
MIRROR_NOTE = (
    "AB ülkesinin beyanı (CIF, EUR) ile Türkiye'nin ihracat beyanı (FOB, USD) aynı ticareti "
    "farklı gösterebilir; navlun, zamanlama ve sınıflandırma farkı vardır."
)

# Eurostat raporlayan kodları (Yunanistan EL'dir) → Türkçe ad.
EU_REPORTERS: dict[str, str] = {
    "AT": "Avusturya", "BE": "Belçika", "BG": "Bulgaristan", "HR": "Hırvatistan",
    "CY": "Güney Kıbrıs", "CZ": "Çekya", "DK": "Danimarka", "EE": "Estonya",
    "FI": "Finlandiya", "FR": "Fransa", "DE": "Almanya", "EL": "Yunanistan",
    "HU": "Macaristan", "IE": "İrlanda", "IT": "İtalya", "LV": "Letonya",
    "LT": "Litvanya", "LU": "Lüksemburg", "MT": "Malta", "NL": "Hollanda",
    "PL": "Polonya", "PT": "Portekiz", "RO": "Romanya", "SK": "Slovakya",
    "SI": "Slovenya", "ES": "İspanya", "SE": "İsveç",
}
_ISO_TO_EUROSTAT = {"GR": "EL"}
WORLD_CODE = "WORLD"
TURKIYE_CODE = "TR"


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.environ.get(name) or default).strip()))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in {"0", "false", "no", "off", ""}


COMEXT_ENABLED = _env_flag("COMEXT_ENABLED", "1")
COMEXT_TIMEOUT = max(10.0, _env_float("COMEXT_TIMEOUT_SECONDS", 45.0))
# Eurostat aylık günceller; 60 gün taze sayılır.
COMEXT_REFRESH_DAYS = max(1, _env_int("COMEXT_REFRESH_DAYS", 60))
COMEXT_DELAY_SECONDS = max(0.0, _env_float("COMEXT_DELAY_SECONDS", 1.0))
COMEXT_COOLDOWN_SECONDS = max(5.0, _env_float("COMEXT_COOLDOWN_SECONDS", 60.0))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def product_codes(value: Any) -> list[str]:
    """GTİP'ten Comext'in anladığı ürün kodlarını, en hassastan kabaya, üretir.

    Türk GTİP'inin ilk 8 hanesi AB Kombine Nomenklatürü (CN8) ile aynıdır; 9. haneden
    sonrası ulusaldır. CN8 boş dönerse HS6'ya düşülür (kod yıl içinde değişmiş olabilir).
    """
    digits = _digits(value)
    out: list[str] = []
    for size in (8, 6, 4, 2):
        if len(digits) >= size:
            code = digits[:size]
            if code not in out:
                out.append(code)
    return out


def reporter_code(value: Any) -> str | None:
    """ISO2 → Eurostat raporlayan kodu; AB-27 dışı ya da tanınmayan kod ``None``."""
    code = str(value or "").strip().upper()
    code = _ISO_TO_EUROSTAT.get(code, code)
    return code if code in EU_REPORTERS else None


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------- JSON-stat çözümü

def _coordinates(flat: int, sizes: list[int]) -> list[int]:
    """Düz indeksi boyut konumlarına çevirir; JSON-stat'ta son boyut en hızlı değişir."""
    out = [0] * len(sizes)
    for position in range(len(sizes) - 1, -1, -1):
        size = sizes[position] or 1
        out[position] = flat % size
        flat //= size
    return out


def parse_jsonstat(payload: Any) -> dict[str, Any]:
    """Comext JSON-stat gövdesini partner satırlarına çevirir.

    Dönen sözlük: ``partners`` (yalnız iki harfli ISO2 kodlar), ``world`` (``WORLD`` satırı),
    ``labels`` (kod → ad). Toplam kodlar (``EXT_EU27_2020`` vb.) partner listesine girmez:
    ISO2 toplamı zaten WORLD'e eşittir, onları da saymak çift sayım olur.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), dict):
        return {"partners": [], "world": None, "labels": {}}
    order: list[str] = list(payload.get("id") or [])
    sizes: list[int] = [int(s) for s in (payload.get("size") or [])]
    dimensions = payload.get("dimension") or {}
    if not order or len(order) != len(sizes) or "partner" not in order or "indicators" not in order:
        return {"partners": [], "world": None, "labels": {}}
    position_to_code: dict[str, dict[int, str]] = {}
    labels: dict[str, str] = {}
    for name in order:
        category = (dimensions.get(name) or {}).get("category") or {}
        index = category.get("index") or {}
        if isinstance(index, list):
            index = {code: position for position, code in enumerate(index)}
        position_to_code[name] = {int(position): str(code) for code, position in index.items()}
        if name == "partner":
            labels = {str(code): str(text) for code, text in (category.get("label") or {}).items()}
    partner_axis = order.index("partner")
    indicator_axis = order.index("indicators")
    cells: dict[str, dict[str, float]] = {}
    for flat, raw in payload["value"].items():
        try:
            coords = _coordinates(int(flat), sizes)
        except (TypeError, ValueError):
            continue
        partner = position_to_code["partner"].get(coords[partner_axis])
        indicator = position_to_code["indicators"].get(coords[indicator_axis])
        number = _number(raw)
        if partner is None or indicator is None or number is None:
            continue
        cells.setdefault(partner, {})[indicator] = number
    partners: list[dict[str, Any]] = []
    world: dict[str, Any] | None = None
    for code, values in cells.items():
        value = values.get("VALUE_IN_EUROS")
        if value is None:
            continue
        hundred_kg = values.get("QUANTITY_IN_100KG")
        row = {
            "partner_code": code,
            "partner": labels.get(code) or code,
            "value_eur": value,
            # Eurostat ağırlığı 100 kg biriminde yayımlar.
            "net_weight_kg": round(hundred_kg * 100.0, 3) if hundred_kg else None,
        }
        if code == WORLD_CODE:
            world = {"value_eur": value, "net_weight_kg": row["net_weight_kg"]}
        elif len(code) == 2 and code.isalpha():
            partners.append(row)
    return {"partners": partners, "world": world, "labels": labels}


def rank_partners(
    rows: Iterable[dict[str, Any]], *, world: dict[str, Any] | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Değere göre sıralar, EUR/kg türetir, payı WORLD'e (yoksa toplama) göre hesaplar."""
    ranked = [dict(row) for row in rows if row.get("value_eur")]
    ranked.sort(key=lambda item: item["value_eur"], reverse=True)
    total = (world or {}).get("value_eur") or sum(item["value_eur"] for item in ranked) or 0.0
    for position, item in enumerate(ranked, start=1):
        weight = item.get("net_weight_kg")
        item["rank"] = position
        item["unit_price_eur_per_kg"] = round(item["value_eur"] / weight, 2) if weight else None
        # Türkiye gibi küçük paylar 4 hanede sıfıra yuvarlanır; 6 hane tutulur.
        item["share"] = round(item["value_eur"] / total, 6) if total else None
    return ranked[: max(1, int(limit or 1))]


# --------------------------------------------------------------------------- sonuç modeli

@dataclass
class ComextReport:
    product: str
    reporter: str
    flow: str
    year: int | None
    status: str  # ok | disabled | unavailable | not_found | rate_limited
    partners: list[dict[str, Any]] = field(default_factory=list)
    world: dict[str, Any] | None = None
    focus: dict[str, Any] | None = None
    match_level: str | None = None  # cn8 | hs6 | hs4 | hs2
    requested_code: str = ""
    fetched_at: str | None = None
    from_archive: bool = False
    age_days: int | None = None
    source_url: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "requested_code": self.requested_code,
            "match_level": self.match_level,
            "reporter": self.reporter,
            "reporter_name": EU_REPORTERS.get(self.reporter, self.reporter),
            "flow": self.flow,
            "year": self.year,
            "status": self.status,
            "partners": self.partners,
            "world": self.world,
            "focus": self.focus,
            "fetched_at": self.fetched_at,
            "from_archive": self.from_archive,
            "age_days": self.age_days,
            "source_url": self.source_url,
            "warnings": self.warnings,
            "source": "eurostat_comext",
            "currency": "EUR",
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
            "mirror_note": MIRROR_NOTE,
            "site_url": COMEXT_SITE_URL,
        }


class ComextRateLimited(RuntimeError):
    """429: ısrar edilmez, soğuma penceresi açılır."""


# --------------------------------------------------------------------------- motor

class ComextEngine:
    """Comext sorgularını yapar, sonucu kalıcı arşive yazar, ücret doğurmaz."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
        refresh_days: int = COMEXT_REFRESH_DAYS,
        delay_seconds: float = COMEXT_DELAY_SECONDS,
        cooldown_seconds: float = COMEXT_COOLDOWN_SECONDS,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "eurostat_comext.sqlite3"
        self.enabled = COMEXT_ENABLED if enabled is None else bool(enabled)
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(COMEXT_TIMEOUT, connect=15.0), follow_redirects=False
        )
        self.refresh_days = max(1, int(refresh_days or 1))
        self.delay_seconds = max(0.0, float(delay_seconds or 0.0))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds or 0.0))
        self._cooldown_until = 0.0
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._errors: list[str] = []
        self._initialise()

    # ---- depo
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_queries (
                    product TEXT NOT NULL,
                    reporter TEXT NOT NULL,
                    flow TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    rows_json TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (product, reporter, flow, year)
                );
                CREATE INDEX IF NOT EXISTS idx_comext_product ON market_queries(product);
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def _age_days(self, fetched_at: str | None) -> int | None:
        if not fetched_at:
            return None
        try:
            moment = datetime.fromisoformat(fetched_at)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return max(0, (datetime.now(UTC) - moment).days)

    def archived(self, product: str, reporter: str, flow: str, year: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rows_json, source_url, fetched_at FROM market_queries "
                "WHERE product=? AND reporter=? AND flow=? AND year=?",
                (product, reporter, flow, int(year)),
            ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row["rows_json"])
        except ValueError:
            return None
        return {
            "partners": parsed.get("partners") or [],
            "world": parsed.get("world"),
            "source_url": row["source_url"] or None,
            "fetched_at": row["fetched_at"],
        }

    def _store(
        self, product: str, reporter: str, flow: str, year: int, parsed: dict[str, Any], *, source_url: str
    ) -> None:
        payload = {"partners": parsed.get("partners") or [], "world": parsed.get("world")}
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO market_queries"
                "(product,reporter,flow,year,rows_json,source_url,fetched_at) VALUES(?,?,?,?,?,?,?)",
                (product, reporter, flow, int(year), json.dumps(payload, ensure_ascii=False), source_url, _now()),
            )

    # ---- ağ
    async def _pace(self) -> None:
        now = _monotonic()
        if now < self._cooldown_until:
            await asyncio.sleep(min(self._cooldown_until - now, self.cooldown_seconds))
        gap = self.delay_seconds - (_monotonic() - self._last_request)
        if gap > 0:
            await asyncio.sleep(gap)

    async def _get_json(self, url: str) -> tuple[Any, str]:
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_COMEXT_HOSTS)
            await self._pace()
            response = await self._http.get(current, headers={"Accept": "application/json"})
            self._last_request = _monotonic()
            if response.status_code == 429:
                self._cooldown_until = _monotonic() + self.cooldown_seconds
                raise ComextRateLimited("Eurostat 429 döndürdü; soğuma penceresi açıldı.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("Eurostat hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Eurostat {response.status_code} döndürdü.")
            content = response.content
            if len(content) > _MAX_BYTES:
                raise ValueError("Eurostat yanıtı beklenenden büyük.")
            return json.loads(content.decode("utf-8", errors="replace")), current
        raise ValueError("Eurostat çok fazla yönlendirme yaptı.")

    def _market_url(self, product: str, reporter: str, flow: str, year: int) -> str:
        query = urlencode(
            [
                ("format", "JSON"),
                ("lang", "EN"),
                ("freq", "A"),
                ("reporter", reporter),
                ("product", product),
                ("flow", "1" if flow == "M" else "2"),
                ("indicators", "VALUE_IN_EUROS"),
                ("indicators", "QUANTITY_IN_100KG"),
                ("time", str(int(year))),
            ]
        )
        return f"{COMEXT_BASE}/eurostat/api/comext/dissemination/statistics/1.0/data/{COMEXT_DATASET}?{query}"

    # ---- sorgu
    @staticmethod
    def _fill(report: ComextReport, parsed: dict[str, Any], *, limit: int, focus: str | None) -> None:
        partners = parsed.get("partners") or []
        world = parsed.get("world")
        ranked_all = rank_partners(partners, world=world, limit=max(limit, len(partners) or 1))
        report.partners = ranked_all[: max(1, int(limit or 1))]
        report.world = world
        report.status = "ok" if partners else "not_found"
        if focus:
            match = next((item for item in ranked_all if item["partner_code"] == focus), None)
            report.focus = (
                {**match, "present": True}
                if match
                else {"partner_code": focus, "present": False, "value_eur": None, "share": None, "rank": None}
            )

    async def markets(
        self,
        gtip: str,
        *,
        reporter: str = "DE",
        flow: str = "M",
        year: int | None = None,
        limit: int = 20,
        focus: str | None = TURKIYE_CODE,
        refresh: bool = False,
    ) -> ComextReport:
        """Bir AB ülkesi bu ürünü kimden alıyor (``flow="M"``) ya da kime satıyor (``"X"``).

        ``focus`` verilirse o partnerin (varsayılan Türkiye) değeri, payı ve sırası
        ayrıca döner; listede yoksa ``present: False``.
        """
        codes = product_codes(gtip)
        reporter_key = reporter_code(reporter)
        flow_code = "X" if str(flow or "").upper().startswith("X") else "M"
        target_year = int(year) if year else datetime.now(UTC).year - 1
        report = ComextReport(
            product=codes[0] if codes else "", reporter=reporter_key or str(reporter or "").upper(),
            flow=flow_code, year=target_year, status="unavailable", requested_code=_digits(gtip),
        )
        if not codes:
            report.status = "not_found"
            report.warnings.append("Geçerli bir ürün kodu türetilemedi (en az 2 hane gerekir).")
            return report
        if reporter_key is None:
            report.status = "not_found"
            report.warnings.append("Raporlayan ülke AB-27 üyesi olmalı (ör. DE, FR, NL).")
            return report

        # Arşiv: en hassas koddan başlayarak taze bir kayıt varsa ağa çıkılmaz.
        for code in codes:
            archived = self.archived(code, reporter_key, flow_code, target_year)
            if not archived or refresh:
                continue
            age = self._age_days(archived["fetched_at"])
            if age is None or age >= self.refresh_days:
                continue
            if not archived["partners"] and code != codes[-1]:
                continue  # boş arşiv kaydı: daha kaba kodu da dene
            self._fill(report, archived, limit=limit, focus=focus)
            report.product = code
            report.match_level = _level(code)
            report.fetched_at = archived["fetched_at"]
            report.source_url = archived["source_url"]
            report.from_archive = True
            report.age_days = age
            self._note_fallback(report, codes)
            return report

        if not self.enabled:
            report.status = "disabled"
            report.warnings.append("Eurostat Comext kaynağı kapalı (COMEXT_ENABLED).")
            return report

        stale = None
        for code in codes:
            url = self._market_url(code, reporter_key, flow_code, target_year)
            try:
                async with self._lock:
                    payload, final_url = await self._get_json(url)
            except ComextRateLimited as exc:
                self._errors = ([*self._errors, "429"])[-10:]
                return self._fallback(report, code, reporter_key, flow_code, target_year, limit, focus,
                                      status="rate_limited", message=str(exc))
            except Exception as exc:
                message = f"Eurostat sorgusu başarısız: {type(exc).__name__}"
                logger.warning("%s (%s)", message, code)
                self._errors = ([*self._errors, message])[-10:]
                return self._fallback(report, code, reporter_key, flow_code, target_year, limit, focus,
                                      status="unavailable", message=message)
            parsed = parse_jsonstat(payload)
            self._store(code, reporter_key, flow_code, target_year, parsed, source_url=final_url)
            if parsed["partners"] or code == codes[-1]:
                self._fill(report, parsed, limit=limit, focus=focus)
                report.product = code
                report.match_level = _level(code)
                report.source_url = final_url
                report.fetched_at = _now()
                report.age_days = 0
                self._note_fallback(report, codes)
                if not parsed["partners"]:
                    report.warnings.append("Bu ürün, ülke ve yıl için Eurostat'ta beyan bulunamadı.")
                return report
            stale = code
        report.status = "not_found"
        if stale:
            report.warnings.append("Bu ürün, ülke ve yıl için Eurostat'ta beyan bulunamadı.")
        return report

    def _fallback(self, report, code, reporter_key, flow_code, year, limit, focus, *, status, message):
        archived = self.archived(code, reporter_key, flow_code, year)
        report.status = status
        report.warnings.append(message)
        if archived and archived["partners"]:
            self._fill(report, archived, limit=limit, focus=focus)
            report.product = code
            report.match_level = _level(code)
            report.fetched_at = archived["fetched_at"]
            report.source_url = archived["source_url"]
            report.from_archive = True
            report.age_days = self._age_days(archived["fetched_at"])
            report.warnings.append("Kaynak yanıt vermedi; arşivdeki kayıt gösteriliyor.")
        return report

    @staticmethod
    def _note_fallback(report: ComextReport, codes: list[str]) -> None:
        if codes and report.product != codes[0] and report.status == "ok":
            report.warnings.append(
                f"{codes[0]} (CN8) için beyan yok; sonuç {report.product} ({report.match_level.upper()}) "
                "düzeyinde. Daha geniş bir ürün grubunu gösteriyor olabilir."
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            queries = connection.execute("SELECT COUNT(*) FROM market_queries").fetchone()[0]
            latest = connection.execute("SELECT MAX(fetched_at) FROM market_queries").fetchone()[0]
        return {
            "enabled": self.enabled,
            "base_url": COMEXT_BASE,
            "dataset": COMEXT_DATASET,
            "archived_queries": queries,
            "last_fetch_at": latest,
            "refresh_days": self.refresh_days,
            "delay_seconds": self.delay_seconds,
            "cooldown_seconds": max(0, round(self._cooldown_until - _monotonic())),
            "reporters": EU_REPORTERS,
            "errors": list(self._errors[-5:]),
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
            "site_url": COMEXT_SITE_URL,
        }

    async def close(self) -> None:
        await self._http.aclose()


def _level(code: str) -> str:
    return {8: "cn8", 6: "hs6", 4: "hs4", 2: "hs2"}.get(len(code), "hs6")


__all__ = [
    "COMEXT_DATASET",
    "EU_REPORTERS",
    "MIRROR_NOTE",
    "SOURCE_NOTE",
    "STATISTIC_ONLY_NOTE",
    "TURKIYE_CODE",
    "WORLD_CODE",
    "ComextEngine",
    "ComextRateLimited",
    "ComextReport",
    "parse_jsonstat",
    "product_codes",
    "rank_partners",
    "reporter_code",
]
