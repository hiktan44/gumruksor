"""Brezilya dış ticaret istatistiği: ComexStat açık API'si (MDIC).

Bu modül **istatistik** verir, **oran** vermez. Sorusu: *Brezilya bu ürünü hangi ülkelerden
alıyor (ya da kime satıyor), Türkiye'nin payı ve sırası ne, kilogram başına ne ödeniyor?*
Latin Amerika'da anahtarsız ve makine okunur tek resmî kaynak budur. Buradan gelen hiçbir
sayı gümrük vergisi, KDV ya da maliyet hesabına girmez.

Uç: ``POST https://api-comexstat.mdic.gov.br/general`` (JSON gövde). Anahtarsız, ücretsiz.
18.09.2026'da canlı ölçüldü; kodun dayandığı olgular:

* Ürün süzgeci **NCM (8 hane)**, **``heading`` (4 hane)** ve **``chapter`` (2 hane)** kabul
  eder; ``sh6`` yoktur.
  NCM'in ilk 6 hanesi HS6'dır. HS6 için NCM tablosu (``/tables/ncm``, 13.746 kayıt) bir kez
  indirilir, arşivlenir ve o HS6 altındaki NCM'ler tek sorguda gönderilir; sunucu bunları
  ülke bazında **toplar** (85171300 + 85171400 sorgusu 16 satır, tek NCM ile aynı).
* Boş sonuç ``{"data":{"list":[]},"success":true}`` döner; hata değildir.
* ``"language": "en"`` ile ülke adları **İngilizce** gelir ("Germany"); varsayılan Portekizcedir
  ("Alemanha"). Satırda ülke **kodu yoktur**; Türkiye odağı ad eşleşmesiyle bulunur
  (``/tables/countries``: Turquia = 827). Sık ülkeler Türkçe adla gösterilir, kalanlar
  İngilizce kalır.
* Değerler dizgedir (``"metricFOB": "441309928"``); FOB USD ve kg.
* **Hız sınırı sıkıdır**: arka arkaya ~6 istekte 429 ve "10 saniye sonra deneyin"; 4 sn
  arayla bile pencere dolabiliyor. Bu yüzden istekler seridir, aralarında
  ``COMEXSTAT_DELAY_SECONDS`` (6 sn) beklenir, 429'da ısrar edilmez ve 15 sn soğunur.
* Kök adres Cloudflare arkasındadır (403); API yolları açıktır.
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
from urllib.parse import urljoin

import httpx

from security_firewall import validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

COMEXSTAT_BASE = (os.environ.get("COMEXSTAT_BASE_URL") or "https://api-comexstat.mdic.gov.br").rstrip("/")
_COMEXSTAT_HOSTS = frozenset({"api-comexstat.mdic.gov.br"})
COMEXSTAT_SITE_URL = "https://comexstat.mdic.gov.br/"

_MAX_BYTES = 16 * 1024 * 1024
_MAX_REDIRECTS = 4
# Bir HS6 altındaki NCM sayısı nadiren 20'yi geçer; sorgu gövdesi bu kadarla sınırlanır.
MAX_NCM_PER_QUERY = 80

SOURCE_NOTE = (
    "Kaynak: Brezilya Kalkınma, Sanayi, Ticaret ve Hizmetler Bakanlığı ComexStat (resmî dış "
    "ticaret istatistiği, FOB USD). Gümrük vergisi veya maliyet hesabı değildir."
)
STATISTIC_ONLY_NOTE = (
    "Bu rakamlar pazar büyüklüğü, rakip ülke ve birim fiyat göstergesidir. Vergi oranı, "
    "KDV ve maliyet kalemleri buradan okunmaz; onlar resmî tarife kaynaklarından gelir."
)
MIRROR_NOTE = (
    "Brezilya'nın beyanı (FOB USD) ile Türkiye'nin ihracat beyanı aynı ticareti farklı "
    "gösterebilir; navlun, zamanlama ve sınıflandırma farkı vardır."
)

TURKIYE_NAMES = frozenset({"Turkey", "Türkiye", "Turkiye", "Turquia"})
TURKIYE_CODE = "827"

# İngilizce (language=en) → Türkçe ülke adı (sık görülenler). Eşleşmeyen ad İngilizce kalır.
COUNTRY_NAMES_TR: dict[str, str] = {
    "Turkey": "Türkiye", "Türkiye": "Türkiye", "China": "Çin", "United States": "ABD",
    "Germany": "Almanya", "Argentina": "Arjantin", "Chile": "Şili", "South Korea": "Güney Kore",
    "Korea, Republic of": "Güney Kore", "India": "Hindistan", "Italy": "İtalya", "Spain": "İspanya",
    "France": "Fransa", "Mexico": "Meksika", "Japan": "Japonya", "United Kingdom": "Birleşik Krallık",
    "Netherlands": "Hollanda", "Russia": "Rusya", "Russian Federation": "Rusya", "Vietnam": "Vietnam",
    "Viet Nam": "Vietnam", "Taiwan": "Tayvan", "Taiwan (Formosa)": "Tayvan", "Hong Kong": "Hong Kong",
    "Malaysia": "Malezya", "Thailand": "Tayland", "Indonesia": "Endonezya", "Poland": "Polonya",
    "Belgium": "Belçika", "Switzerland": "İsviçre", "Sweden": "İsveç", "Austria": "Avusturya",
    "Egypt": "Mısır", "Iran": "İran", "Israel": "İsrail", "Colombia": "Kolombiya", "Peru": "Peru",
    "Uruguay": "Uruguay", "Paraguay": "Paraguay", "Canada": "Kanada", "Australia": "Avustralya",
    "South Africa": "Güney Afrika", "United Arab Emirates": "BAE", "Saudi Arabia": "Suudi Arabistan",
    "Greece": "Yunanistan", "Portugal": "Portekiz", "Hungary": "Macaristan", "Romania": "Romanya",
    "Czech Republic": "Çekya", "Czechia": "Çekya", "Denmark": "Danimarka", "Finland": "Finlandiya",
    "Norway": "Norveç", "Ireland": "İrlanda", "Morocco": "Fas", "Bangladesh": "Bangladeş",
    "Pakistan": "Pakistan", "Philippines": "Filipinler", "Singapore": "Singapur",
    "New Zealand": "Yeni Zelanda", "Ukraine": "Ukrayna", "Kazakhstan": "Kazakistan",
    "Azerbaijan": "Azerbaycan", "Bulgaria": "Bulgaristan", "Slovakia": "Slovakya",
    "Slovenia": "Slovenya", "Croatia": "Hırvatistan", "Serbia": "Sırbistan", "Lithuania": "Litvanya",
    "Latvia": "Letonya", "Estonia": "Estonya", "Luxembourg": "Lüksemburg", "Qatar": "Katar",
    "Kuwait": "Kuveyt", "Oman": "Umman", "Iraq": "Irak", "Lebanon": "Lübnan", "Jordan": "Ürdün",
    "Tunisia": "Tunus", "Algeria": "Cezayir", "Libya": "Libya", "Nigeria": "Nijerya", "Kenya": "Kenya",
    "Ethiopia": "Etiyopya", "Ghana": "Gana", "Angola": "Angola", "Mozambique": "Mozambik",
    "Bolivia": "Bolivya", "Ecuador": "Ekvador", "Venezuela": "Venezuela", "Cuba": "Küba",
    "Panama": "Panama", "North Korea": "Kuzey Kore", "Sri Lanka": "Sri Lanka", "Cambodia": "Kamboçya",
    "Myanmar": "Myanmar", "Macao": "Makao", "American Samoa": "Amerikan Samoası",
}


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


COMEXSTAT_ENABLED = _env_flag("COMEXSTAT_ENABLED", "1")
COMEXSTAT_TIMEOUT = max(10.0, _env_float("COMEXSTAT_TIMEOUT_SECONDS", 60.0))
COMEXSTAT_REFRESH_DAYS = max(1, _env_int("COMEXSTAT_REFRESH_DAYS", 60))
COMEXSTAT_NCM_REFRESH_DAYS = max(1, _env_int("COMEXSTAT_NCM_REFRESH_DAYS", 30))
# Ölçüm: arka arkaya ~6 istekte 429; 4 sn ara yetmedi. Seri istek, aralarında 6 sn.
COMEXSTAT_DELAY_SECONDS = max(0.0, _env_float("COMEXSTAT_DELAY_SECONDS", 6.0))
COMEXSTAT_COOLDOWN_SECONDS = max(5.0, _env_float("COMEXSTAT_COOLDOWN_SECONDS", 15.0))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def product_query(gtip: str, ncm_codes: Iterable[str]) -> tuple[dict[str, Any] | None, str, str]:
    """GTİP'ten ComexStat süzgecini üretir: ``(filter, level, code)``.

    * 8+ hane → tek NCM (Türk GTİP'inin ilk 8 hanesi CN8'dir; NCM ile ilk 6 hane ortak,
      7-8. haneler Mercosur'a özgü olabilir — bu yüzden NCM tablosunda **yoksa** HS6'ya düşülür).
    * 6-7 hane → HS6 altındaki NCM listesi (tablodan).
    * 4-5 hane → ``heading``; 2-3 hane → ``chapter``.
    * Daha kısa → ``None``.
    """
    digits = _digits(gtip)
    codes = list(ncm_codes)
    if len(digits) >= 8 and digits[:8] in codes:
        return {"filter": "ncm", "values": [digits[:8]]}, "ncm8", digits[:8]
    if len(digits) >= 6:
        under = sorted({code for code in codes if code.startswith(digits[:6])})
        if under:
            return {"filter": "ncm", "values": under[:MAX_NCM_PER_QUERY]}, "hs6", digits[:6]
        return {"filter": "heading", "values": [digits[:4]]}, "hs4", digits[:4]
    if len(digits) >= 4:
        return {"filter": "heading", "values": [digits[:4]]}, "hs4", digits[:4]
    if len(digits) >= 2:
        return {"filter": "chapter", "values": [digits[:2]]}, "hs2", digits[:2]
    return None, "", digits


def parse_rows(payload: Any) -> list[dict[str, Any]]:
    """ComexStat gövdesini partner satırlarına çevirir (FOB USD, kg; ad Türkçe, yoksa kaynak dili)."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    items = data.get("list") if isinstance(data, dict) else None
    rows: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("country") or "").strip()
        value = _number(raw.get("metricFOB"))
        if not name or value is None:
            continue
        rows.append(
            {
                "partner_source": name,
                "partner": COUNTRY_NAMES_TR.get(name, name),
                "value_usd": value,
                "net_weight_kg": _number(raw.get("metricKG")),
                "year": raw.get("year"),
            }
        )
    return rows


def rank_partners(rows: Iterable[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    """Değere göre sıralar, USD/kg türetir, payı toplam üzerinden hesaplar."""
    ranked = [dict(row) for row in rows if row.get("value_usd")]
    ranked.sort(key=lambda item: item["value_usd"], reverse=True)
    total = sum(item["value_usd"] for item in ranked) or 0.0
    for position, item in enumerate(ranked, start=1):
        weight = item.get("net_weight_kg")
        item["rank"] = position
        item["unit_price_usd_per_kg"] = round(item["value_usd"] / weight, 2) if weight else None
        item["share"] = round(item["value_usd"] / total, 6) if total else None
    return ranked[: max(1, int(limit or 1))]


# --------------------------------------------------------------------------- sonuç modeli

@dataclass
class ComexStatReport:
    product: str
    flow: str
    year: int | None
    status: str  # ok | disabled | unavailable | not_found | rate_limited
    partners: list[dict[str, Any]] = field(default_factory=list)
    total: dict[str, Any] | None = None
    focus: dict[str, Any] | None = None
    match_level: str | None = None  # ncm8 | hs6 | hs4
    ncm_codes: list[str] = field(default_factory=list)
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
            "ncm_codes": self.ncm_codes,
            "reporter": "BR",
            "reporter_name": "Brezilya",
            "flow": self.flow,
            "year": self.year,
            "status": self.status,
            "partners": self.partners,
            "total": self.total,
            "focus": self.focus,
            "fetched_at": self.fetched_at,
            "from_archive": self.from_archive,
            "age_days": self.age_days,
            "source_url": self.source_url,
            "warnings": self.warnings,
            "source": "comexstat",
            "currency": "USD",
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
            "mirror_note": MIRROR_NOTE,
            "site_url": COMEXSTAT_SITE_URL,
        }


class ComexStatRateLimited(RuntimeError):
    """429: ısrar edilmez, soğuma penceresi açılır."""


# --------------------------------------------------------------------------- motor

class ComexStatEngine:
    """ComexStat sorgularını yapar, NCM tablosunu ve sonuçları kalıcı arşive yazar."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
        refresh_days: int = COMEXSTAT_REFRESH_DAYS,
        ncm_refresh_days: int = COMEXSTAT_NCM_REFRESH_DAYS,
        delay_seconds: float = COMEXSTAT_DELAY_SECONDS,
        cooldown_seconds: float = COMEXSTAT_COOLDOWN_SECONDS,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "comexstat.sqlite3"
        self.enabled = COMEXSTAT_ENABLED if enabled is None else bool(enabled)
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(COMEXSTAT_TIMEOUT, connect=15.0), follow_redirects=False
        )
        self.refresh_days = max(1, int(refresh_days or 1))
        self.ncm_refresh_days = max(1, int(ncm_refresh_days or 1))
        self.delay_seconds = max(0.0, float(delay_seconds or 0.0))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds or 0.0))
        self._cooldown_until = 0.0
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._errors: list[str] = []
        self._ncm_cache: list[str] | None = None
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
                    flow TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    rows_json TEXT NOT NULL,
                    match_level TEXT NOT NULL DEFAULT '',
                    ncm_json TEXT NOT NULL DEFAULT '[]',
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (product, flow, year)
                );
                CREATE TABLE IF NOT EXISTS ncm_codes (
                    code TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL
                );
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

    def archived(self, product: str, flow: str, year: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rows_json, match_level, ncm_json, source_url, fetched_at FROM market_queries "
                "WHERE product=? AND flow=? AND year=?",
                (product, flow, int(year)),
            ).fetchone()
        if row is None:
            return None
        try:
            rows = json.loads(row["rows_json"])
            ncm = json.loads(row["ncm_json"] or "[]")
        except ValueError:
            return None
        return {
            "rows": rows, "match_level": row["match_level"], "ncm_codes": ncm,
            "source_url": row["source_url"] or None, "fetched_at": row["fetched_at"],
        }

    def _store(self, product: str, flow: str, year: int, rows: list[dict[str, Any]], *,
               match_level: str, ncm_codes: list[str], source_url: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO market_queries"
                "(product,flow,year,rows_json,match_level,ncm_json,source_url,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                (product, flow, int(year), json.dumps(rows, ensure_ascii=False), match_level,
                 json.dumps(ncm_codes), source_url, _now()),
            )

    def ncm_codes(self) -> list[str]:
        """Arşivdeki NCM kodları (boşsa ``[]``); tazeliğe bakmaz."""
        if self._ncm_cache is not None:
            return self._ncm_cache
        with self._connect() as connection:
            rows = connection.execute("SELECT code FROM ncm_codes ORDER BY code").fetchall()
        self._ncm_cache = [row["code"] for row in rows]
        return self._ncm_cache

    def _ncm_age_days(self) -> int | None:
        with self._connect() as connection:
            latest = connection.execute("SELECT MAX(fetched_at) FROM ncm_codes").fetchone()[0]
        return self._age_days(latest)

    # ---- ağ
    async def _pace(self) -> None:
        now = _monotonic()
        if now < self._cooldown_until:
            await asyncio.sleep(min(self._cooldown_until - now, self.cooldown_seconds))
        gap = self.delay_seconds - (_monotonic() - self._last_request)
        if gap > 0:
            await asyncio.sleep(gap)

    async def _request(self, url: str, *, body: dict[str, Any] | None = None) -> tuple[Any, str]:
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_COMEXSTAT_HOSTS)
            await self._pace()
            headers = {"Accept": "application/json"}
            if body is None:
                response = await self._http.get(current, headers=headers)
            else:
                response = await self._http.post(current, headers=headers, json=body)
            self._last_request = _monotonic()
            if response.status_code == 429:
                self._cooldown_until = _monotonic() + self.cooldown_seconds
                raise ComexStatRateLimited("ComexStat 429 döndürdü; soğuma penceresi açıldı.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("ComexStat hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"ComexStat {response.status_code} döndürdü.")
            content = response.content
            if len(content) > _MAX_BYTES:
                raise ValueError("ComexStat yanıtı beklenenden büyük.")
            return json.loads(content.decode("utf-8", errors="replace")), current
        raise ValueError("ComexStat çok fazla yönlendirme yaptı.")

    async def ensure_ncm_table(self) -> list[str]:
        """NCM tablosunu tazeyse arşivden, değilse kaynaktan (bir kez, ~1,8 MB) alır."""
        age = self._ncm_age_days()
        codes = self.ncm_codes()
        if codes and age is not None and age < self.ncm_refresh_days:
            return codes
        if not self.enabled:
            return codes
        try:
            async with self._lock:
                payload, _ = await self._request(f"{COMEXSTAT_BASE}/tables/ncm")
        except Exception as exc:
            self._errors = ([*self._errors, f"NCM tablosu alınamadı: {type(exc).__name__}"])[-10:]
            return codes
        items = ((payload or {}).get("data") or {}).get("list") or []
        fresh = [
            (str(item.get("coNcm")), str(item.get("noNCM") or ""))
            for item in items if isinstance(item, dict) and _digits(item.get("coNcm"))
        ]
        if len(fresh) < 1000:
            self._errors = ([*self._errors, f"NCM tablosu şüpheli küçük ({len(fresh)})"])[-10:]
            return codes
        stamp = _now()
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO ncm_codes(code,description,fetched_at) VALUES(?,?,?)",
                [(code, desc, stamp) for code, desc in fresh],
            )
        self._ncm_cache = None
        return self.ncm_codes()

    @staticmethod
    def _body(product_filter: dict[str, Any], flow: str, year: int) -> dict[str, Any]:
        return {
            "flow": "export" if flow == "X" else "import",
            "language": "en",
            "monthDetail": False,
            "period": {"from": f"{int(year)}-01", "to": f"{int(year)}-12"},
            "filters": [product_filter],
            "details": ["country"],
            "metrics": ["metricFOB", "metricKG"],
        }

    # ---- sorgu
    @staticmethod
    def _fill(report: ComexStatReport, rows: list[dict[str, Any]], *, limit: int, focus: bool) -> None:
        ranked_all = rank_partners(rows, limit=max(limit, len(rows) or 1))
        report.partners = ranked_all[: max(1, int(limit or 1))]
        total = sum(item["value_usd"] for item in ranked_all)
        weight = sum(item["net_weight_kg"] or 0.0 for item in ranked_all)
        report.total = {"value_usd": total, "net_weight_kg": weight or None} if ranked_all else None
        report.status = "ok" if rows else "not_found"
        if focus:
            match = next((item for item in ranked_all if item.get("partner_source") in TURKIYE_NAMES), None)
            report.focus = (
                {**match, "present": True}
                if match
                else {"partner": "Türkiye", "partner_source": "Turkey", "present": False,
                      "value_usd": None, "share": None, "rank": None}
            )

    async def markets(
        self,
        gtip: str,
        *,
        flow: str = "M",
        year: int | None = None,
        limit: int = 20,
        focus: bool = True,
        refresh: bool = False,
    ) -> ComexStatReport:
        """Brezilya bu ürünü kimden alıyor (``flow="M"``) ya da kime satıyor (``"X"``)."""
        flow_code = "X" if str(flow or "").upper().startswith("X") else "M"
        target_year = int(year) if year else datetime.now(UTC).year - 1
        digits = _digits(gtip)
        report = ComexStatReport(product=digits[:8], flow=flow_code, year=target_year,
                                 status="unavailable", requested_code=digits)
        if len(digits) < 2:
            report.status = "not_found"
            report.warnings.append("Geçerli bir ürün kodu türetilemedi (en az 2 hane gerekir).")
            return report

        key = next(width for width in (8, 6, 4, 2) if len(digits) >= width)
        key = digits[:key]
        archived = self.archived(key, flow_code, target_year)
        age = self._age_days(archived["fetched_at"]) if archived else None
        if archived and not refresh and age is not None and age < self.refresh_days:
            self._fill(report, archived["rows"], limit=limit, focus=focus)
            report.product = key
            report.match_level = archived["match_level"] or None
            report.ncm_codes = archived["ncm_codes"]
            report.fetched_at = archived["fetched_at"]
            report.source_url = archived["source_url"]
            report.from_archive = True
            report.age_days = age
            self._note_level(report, digits)
            return report

        if not self.enabled:
            report.status = "disabled"
            report.warnings.append("ComexStat kaynağı kapalı (COMEXSTAT_ENABLED).")
            return report

        codes = await self.ensure_ncm_table()
        product_filter, level, code = product_query(digits, codes)
        if product_filter is None:
            report.status = "not_found"
            return report
        if level == "hs4" and len(digits) >= 6 and not codes:
            report.warnings.append("NCM tablosu alınamadı; sorgu 4 hane (pozisyon) düzeyine düşürüldü.")
        url = f"{COMEXSTAT_BASE}/general"
        try:
            async with self._lock:
                payload, final_url = await self._request(url, body=self._body(product_filter, flow_code, target_year))
        except ComexStatRateLimited as exc:
            self._errors = ([*self._errors, "429"])[-10:]
            return self._fallback(report, archived, key, limit, focus, status="rate_limited", message=str(exc))
        except Exception as exc:
            message = f"ComexStat sorgusu başarısız: {type(exc).__name__}"
            logger.warning("%s (%s)", message, key)
            self._errors = ([*self._errors, message])[-10:]
            return self._fallback(report, archived, key, limit, focus, status="unavailable", message=message)

        rows = parse_rows(payload)
        ncm_list = list(product_filter["values"]) if product_filter["filter"] == "ncm" else []
        self._store(key, flow_code, target_year, rows, match_level=level, ncm_codes=ncm_list, source_url=final_url)
        self._fill(report, rows, limit=limit, focus=focus)
        report.product = key
        report.match_level = level
        report.ncm_codes = ncm_list
        report.source_url = final_url
        report.fetched_at = _now()
        report.age_days = 0
        self._note_level(report, digits)
        if not rows:
            report.warnings.append("Bu ürün ve yıl için ComexStat'ta beyan bulunamadı.")
        return report

    def _fallback(self, report, archived, key, limit, focus, *, status, message):
        report.status = status
        report.warnings.append(message)
        if archived and archived["rows"]:
            self._fill(report, archived["rows"], limit=limit, focus=focus)
            report.product = key
            report.match_level = archived["match_level"] or None
            report.ncm_codes = archived["ncm_codes"]
            report.fetched_at = archived["fetched_at"]
            report.source_url = archived["source_url"]
            report.from_archive = True
            report.age_days = self._age_days(archived["fetched_at"])
            report.warnings.append("Kaynak yanıt vermedi; arşivdeki kayıt gösteriliyor.")
        return report

    @staticmethod
    def _note_level(report: ComexStatReport, digits: str) -> None:
        if report.status != "ok":
            return
        if report.match_level == "hs6" and len(digits) >= 8:
            report.warnings.append(
                f"{digits[:8]} Brezilya NCM listesinde yok; sonuç HS6 ({digits[:6]}) altındaki "
                f"{len(report.ncm_codes)} NCM'in toplamıdır."
            )
        elif report.match_level == "hs4" and len(digits) >= 6:
            report.warnings.append(
                f"{digits[:6]} altında NCM bulunamadı; sonuç pozisyon ({digits[:4]}) düzeyinde ve "
                "daha geniş bir ürün grubunu gösteriyor olabilir."
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            queries = connection.execute("SELECT COUNT(*) FROM market_queries").fetchone()[0]
            latest = connection.execute("SELECT MAX(fetched_at) FROM market_queries").fetchone()[0]
            ncm = connection.execute("SELECT COUNT(*) FROM ncm_codes").fetchone()[0]
        return {
            "enabled": self.enabled,
            "base_url": COMEXSTAT_BASE,
            "archived_queries": queries,
            "ncm_codes": ncm,
            "ncm_age_days": self._ncm_age_days(),
            "last_fetch_at": latest,
            "refresh_days": self.refresh_days,
            "delay_seconds": self.delay_seconds,
            "cooldown_seconds": max(0, round(self._cooldown_until - _monotonic())),
            "errors": list(self._errors[-5:]),
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
            "site_url": COMEXSTAT_SITE_URL,
        }

    async def close(self) -> None:
        await self._http.aclose()


__all__ = [
    "COUNTRY_NAMES_TR",
    "MAX_NCM_PER_QUERY",
    "MIRROR_NOTE",
    "SOURCE_NOTE",
    "STATISTIC_ONLY_NOTE",
    "TURKIYE_CODE",
    "TURKIYE_NAMES",
    "ComexStatEngine",
    "ComexStatRateLimited",
    "ComexStatReport",
    "parse_rows",
    "product_query",
    "rank_partners",
]
