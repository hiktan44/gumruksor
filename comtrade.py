"""Dış ticaret istatistikleri: UN Comtrade açık ``preview`` ucu.

Bu modül **istatistik** verir, **oran** vermez. Pazar araştırması, alıcı sunumu ve
rapor içindir: hangi ülke ne kadar alıyor, kilogram başına ne ödüyor, eğilim ne yönde.
Buradan gelen hiçbir sayı gümrük vergisi, KDV ya da maliyet hesabına girmez — o alanlar
yalnız resmî tarife anlık görüntülerinden beslenir.

Uç ``https://comtradeapi.un.org/public/v1/preview/C/A/HS`` anahtarsız ve ücretsizdir.
17.09.2026'da canlı ölçüldü; kodun dayandığı olgular:

* **Satırlar hem toplamı hem kırılımı içerir.** Körlemesine toplamak çift sayar:
  Almanya'ya 8517 ihracatı gerçek toplamda 19.154.002 USD iken tüm satırlar
  toplandığında 55.768.235 USD çıkıyor (~3 katı). Kırılım **üç** boyutta oluyor ve
  üçü de kapatılmalı: taşıma şekli (``motCode``), gümrük rejimi (``customsCode``) ve
  **ikinci partner ülke** (``partner2Code``). Doğru satır üçünün de toplam olduğu
  satırdır; istek bu üç süzgeçle gönderilir ve gelen satırlar **bir daha** süzülür
  (sunucu bir süzgeci yok sayarsa sessizce şişmiş rakam üretmeyelim).
* **``partner2Code`` unutulursa her ülke iki kez listelenir.** 17.09.2026 ölçümü,
  851713 / 2024 / Türkiye ihracatı: üç süzgeçten yalnız ikisi gönderildiğinde 112 satır
  ve 38 partnerin her biri **iki kez** (biri ``partner2Code=0``, biri kendi kodu) geliyor;
  üçüncü süzgeçle 38 satır ve 38 ayrı partner kalıyor. Ayrıca ``partnerCode=0`` satırı
  tek başına "Dünya toplamı" **değildir**: o kodun da ``partner2Code`` kırılımları var.
  Gerçek dünya toplamı yalnız ``partnerCode=0`` **ve** ``partner2Code=0`` satırıdır —
  ölçümde 16.595.026 USD, ki bu 37 partnerin toplamına birebir eşit.
* Tek istek en fazla **500 satır** döndürür; daha fazlası kırpılır ve sonuç
  ``truncated`` ile işaretlenir — eksik sıralamayı tam sanmak yanlış karar verdirir.
* **Tek dönem** kabul edilir; ``period=2020,2021`` isteği 400 döner. Eğilim için yıl
  başına ayrı istek yapılır ve aralarında beklenir.
* Arka arkaya istekte **429** gelir, bir dakikadan kısa sürede toparlar. Bu yüzden
  sorgular seridir, aralarında bekleme vardır ve 429'da ısrar edilmez.

Ülke adları ayrı referans tablosundan gelir (``partnerAreas.json``, 310 kayıt); ölçü
satırlarında yalnız sayısal kod bulunur.
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
from time import monotonic as _monotonic
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from security_firewall import validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

COMTRADE_BASE = (os.environ.get("COMTRADE_BASE_URL") or "https://comtradeapi.un.org").rstrip("/")
_COMTRADE_HOSTS = frozenset({"comtradeapi.un.org"})
COMTRADE_SITE_URL = "https://comtradeplus.un.org/"

_MAX_BYTES = 16 * 1024 * 1024
_MAX_REDIRECTS = 4
# Tek istek en fazla bu kadar satır döndürür (ölçüldü); daha fazlası kırpılır.
PREVIEW_ROW_CAP = 500

SOURCE_NOTE = (
    "Kaynak: Birleşmiş Milletler Comtrade (comtradeplus.un.org). Ülkelerin kendi beyan "
    "ettiği yıllık dış ticaret istatistiğidir; gümrük vergisi veya maliyet hesabı değildir."
)
STATISTIC_ONLY_NOTE = (
    "Bu rakamlar pazar büyüklüğü ve birim fiyat göstergesidir. Vergi oranı, KDV ve "
    "maliyet kalemleri buradan okunmaz; onlar resmî tarife kaynaklarından gelir."
)
MIRROR_NOTE = (
    "İhracatçı ve ithalatçı ülke aynı ticareti farklı beyan edebilir (navlun, zamanlama, "
    "sınıflandırma farkı). Tek bir ülkenin beyanı tek başına kesin değildir."
)

TURKIYE_CODE = 792
WORLD_CODE = 0


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


COMTRADE_ENABLED = _env_flag("COMTRADE_ENABLED", "1")
COMTRADE_TIMEOUT = max(10.0, _env_float("COMTRADE_TIMEOUT_SECONDS", 45.0))
# Yıllık istatistik yılda bir değişir; 90 gün fazlasıyla taze sayılır.
COMTRADE_REFRESH_DAYS = max(1, _env_int("COMTRADE_REFRESH_DAYS", 90))
# Ardışık istekler arası bekleme. Ölçüm: arka arkaya istek 429 veriyor, bir dakikadan
# kısa sürede toparlıyor. Eş zamanlılık YOK — sorgular seridir.
COMTRADE_DELAY_SECONDS = max(0.0, _env_float("COMTRADE_DELAY_SECONDS", 6.0))
# 429 görülünce bu kadar süre kaynağa hiç dokunulmaz.
COMTRADE_COOLDOWN_SECONDS = max(5.0, _env_float("COMTRADE_COOLDOWN_SECONDS", 90.0))
# Eğilim için en fazla kaç yıl sorgulanır (her yıl ayrı istek + bekleme).
COMTRADE_MAX_YEARS = max(1, min(_env_int("COMTRADE_MAX_YEARS", 5), 10))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def hs_code(value: Any, *, width: int = 6) -> str:
    """GTİP'ten Comtrade'in anladığı HS kodunu üretir.

    Comtrade HS'in uluslararası ortak kısmını taşır: 2, 4 ya da 6 hane. Türk GTİP'inin
    7. haneden sonrası ulusaldır ve istatistikte karşılığı yoktur — kesilir.
    """
    digits = _digits(value)
    if len(digits) < 2:
        return ""
    for size in (6, 4, 2):
        if width >= size and len(digits) >= size:
            return digits[:size]
    return digits[:2]


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------- saf çözümleyiciler

def is_total_row(row: dict[str, Any]) -> bool:
    """Yalnız tam toplam satırı: taşıma şekli, gümrük rejimi ve ikinci partner kırılımsız.

    Üç boyut da kapatılmalı; biri açık kalırsa aynı ticaret birden çok satırda sayılır.
    Ölçülen örnekler: taşıma şekli açıkken Almanya 19.154.002 yerine 55.768.235 USD
    (~3 katı); ikinci partner açıkken 38 partnerin **her biri iki kez** listeleniyor ve
    pay yüzdeleri yarıya düşüyor.
    """
    return (
        row.get("motCode") in (0, "0")
        and str(row.get("customsCode") or "") == "C00"
        and row.get("partner2Code") in (0, "0", None)
    )


def parse_rows(payload: Any) -> list[dict[str, Any]]:
    """Comtrade gövdesini sade satırlara çevirir; yalnız tam toplam satırları alınır."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    rows: list[dict[str, Any]] = []
    for raw in data or []:
        if not isinstance(raw, dict) or not is_total_row(raw):
            continue
        value = _number(raw.get("fobvalue")) or _number(raw.get("primaryValue"))
        rows.append(
            {
                "year": raw.get("refYear"),
                "reporter_code": raw.get("reporterCode"),
                "partner_code": raw.get("partnerCode"),
                "flow": str(raw.get("flowCode") or ""),
                "hs_code": str(raw.get("cmdCode") or ""),
                "value_usd": value,
                "net_weight_kg": _number(raw.get("netWgt")),
                "cif_usd": _number(raw.get("cifvalue")),
            }
        )
    return rows


def rank_partners(
    rows: Iterable[dict[str, Any]], *, limit: int = 20, names: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Partner ülkeleri değere göre sıralar ve kg başına birim fiyatı türetir.

    ``partner_code == 0`` (Dünya) sıralamaya girmez: toplam, bir pazar değildir.
    Birim fiyat yalnız ağırlık varsa hesaplanır — bölünemeyen satırda uydurulmaz.
    """
    names = names or {}
    ranked: list[dict[str, Any]] = []
    for row in rows:
        code = row.get("partner_code")
        if code in (None, WORLD_CODE, str(WORLD_CODE)):
            continue
        value = row.get("value_usd")
        if not value:
            continue
        weight = row.get("net_weight_kg")
        ranked.append(
            {
                "partner_code": code,
                "partner": names.get(str(code)) or f"#{code}",
                "value_usd": value,
                "net_weight_kg": weight,
                "unit_price_usd_per_kg": round(value / weight, 2) if weight else None,
                "year": row.get("year"),
            }
        )
    ranked.sort(key=lambda item: item["value_usd"], reverse=True)
    total = sum(item["value_usd"] for item in ranked) or 0.0
    for item in ranked:
        item["share"] = round(item["value_usd"] / total, 4) if total else None
    return ranked[: max(1, int(limit or 1))]


def world_total(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Comtrade'in kendi 'Dünya' satırı: pay hesabının doğru paydası.

    Satırlar ``parse_rows`` tarafından zaten ``partner2Code=0`` ile süzüldüğü için
    burada ``partnerCode=0`` tektir. Süzgeçsiz ham gövdede ``partnerCode=0`` satırı
    **tek başına dünya toplamı değildir**; onun da ikinci partner kırılımları vardır.
    """
    for row in rows:
        if row.get("partner_code") in (WORLD_CODE, str(WORLD_CODE)):
            return {"value_usd": row.get("value_usd"), "net_weight_kg": row.get("net_weight_kg")}
    return None


# --------------------------------------------------------------------------- sonuç modeli

@dataclass
class MarketReport:
    hs_code: str
    reporter_code: int
    flow: str
    year: int | None
    status: str  # ok | disabled | unavailable | not_found | rate_limited
    partners: list[dict[str, Any]] = field(default_factory=list)
    world: dict[str, Any] | None = None
    truncated: bool = False
    fetched_at: str | None = None
    from_archive: bool = False
    age_days: int | None = None
    source_url: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hs_code": self.hs_code,
            "reporter_code": self.reporter_code,
            "flow": self.flow,
            "year": self.year,
            "status": self.status,
            "partners": self.partners,
            "world": self.world,
            "truncated": self.truncated,
            "fetched_at": self.fetched_at,
            "from_archive": self.from_archive,
            "age_days": self.age_days,
            "source_url": self.source_url,
            "warnings": self.warnings,
            "source": "un_comtrade",
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
            "mirror_note": MIRROR_NOTE,
            "site_url": COMTRADE_SITE_URL,
        }


# --------------------------------------------------------------------------- motor

def _ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


class ComtradeRateLimited(RuntimeError):
    """429: ısrar edilmez, soğuma penceresi açılır."""


class ComtradeEngine:
    """Comtrade sorgularını yapar, sonucu kalıcı arşive yazar, ücret doğurmaz."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
        refresh_days: int = COMTRADE_REFRESH_DAYS,
        delay_seconds: float = COMTRADE_DELAY_SECONDS,
        cooldown_seconds: float = COMTRADE_COOLDOWN_SECONDS,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "comtrade.sqlite3"
        self.enabled = COMTRADE_ENABLED if enabled is None else bool(enabled)
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(COMTRADE_TIMEOUT, connect=15.0), follow_redirects=False
        )
        self.refresh_days = max(1, int(refresh_days or 1))
        self.delay_seconds = max(0.0, float(delay_seconds or 0.0))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds or 0.0))
        self._cooldown_until = 0.0
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._errors: list[str] = []
        self._names: dict[str, str] = {}
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
                    hs_code TEXT NOT NULL,
                    reporter_code INTEGER NOT NULL,
                    flow TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    rows_json TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (hs_code, reporter_code, flow, year)
                );
                CREATE INDEX IF NOT EXISTS idx_comtrade_code ON market_queries(hs_code);
                CREATE TABLE IF NOT EXISTS reference_areas (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                """
            )
            _ensure_columns(connection, "market_queries", {"truncated": "INTEGER NOT NULL DEFAULT 0"})
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

    def archived(self, code: str, reporter: int, flow: str, year: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rows_json, truncated, source_url, fetched_at FROM market_queries "
                "WHERE hs_code=? AND reporter_code=? AND flow=? AND year=?",
                (code, int(reporter), flow, int(year)),
            ).fetchone()
        if row is None:
            return None
        try:
            rows = json.loads(row["rows_json"])
        except ValueError:
            return None
        return {
            "rows": rows,
            "truncated": bool(row["truncated"]),
            "source_url": row["source_url"] or None,
            "fetched_at": row["fetched_at"],
        }

    def _store(
        self, code: str, reporter: int, flow: str, year: int, rows: list[dict[str, Any]],
        *, truncated: bool, source_url: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO market_queries"
                "(hs_code,reporter_code,flow,year,rows_json,truncated,source_url,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    code, int(reporter), flow, int(year),
                    json.dumps(rows, ensure_ascii=False), 1 if truncated else 0, source_url, _now(),
                ),
            )

    # ---- ağ
    async def _pace(self) -> None:
        """Seri istek + soğuma. Eş zamanlılık yok: kaynak arka arkaya isteğe 429 veriyor."""
        now = _monotonic()
        if now < self._cooldown_until:
            await asyncio.sleep(min(self._cooldown_until - now, self.cooldown_seconds))
        gap = self.delay_seconds - (_monotonic() - self._last_request)
        if gap > 0:
            await asyncio.sleep(gap)

    async def _get_json(self, url: str) -> tuple[Any, str]:
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_COMTRADE_HOSTS)
            await self._pace()
            response = await self._http.get(current, headers={"Accept": "application/json"})
            self._last_request = _monotonic()
            if response.status_code == 429:
                # Israr etmiyoruz: tek bir 429 bile kaynağın "yavaşla" demesidir.
                self._cooldown_until = _monotonic() + self.cooldown_seconds
                raise ComtradeRateLimited("Comtrade 429 döndürdü; soğuma penceresi açıldı.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("Comtrade hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Comtrade {response.status_code} döndürdü.")
            content = response.content
            if len(content) > _MAX_BYTES:
                raise ValueError("Comtrade yanıtı beklenenden büyük.")
            return json.loads(content.decode("utf-8", errors="replace")), current
        raise ValueError("Comtrade çok fazla yönlendirme yaptı.")

    def _market_url(self, code: str, reporter: int, flow: str, year: int) -> str:
        query = urlencode(
            {
                "reporterCode": int(reporter),
                "period": int(year),
                "cmdCode": code,
                "flowCode": flow,
                # ÜÇÜ DE ŞART: sunucu yalnız tam toplam satırlarını döndürsün. Biri
                # eksik kalırsa 500 satırlık pencere kırılımlarla dolar, sıralama eksik
                # kalır ve aynı ülke birden çok kez listelenir.
                "motCode": 0,
                "customsCode": "C00",
                "partner2Code": 0,
            }
        )
        return f"{COMTRADE_BASE}/public/v1/preview/C/A/HS?{query}"

    async def area_names(self, *, refresh: bool = False) -> dict[str, str]:
        """Ülke kodu → ad. Ölçü satırlarında yalnız sayısal kod var."""
        if self._names and not refresh:
            return self._names
        with self._connect() as connection:
            rows = connection.execute("SELECT code, name FROM reference_areas").fetchall()
        if rows and not refresh:
            self._names = {row["code"]: row["name"] for row in rows}
            return self._names
        try:
            payload, _ = await self._get_json(
                f"{COMTRADE_BASE}/files/v1/app/reference/partnerAreas.json"
            )
        except Exception as exc:
            self._errors = ([*self._errors, f"Referans tablosu alınamadı: {type(exc).__name__}"])[-10:]
            return self._names
        names = {
            str(item.get("id")): str(item.get("text") or "")
            for item in (payload or {}).get("results", [])
            if item.get("id") is not None and item.get("text")
        }
        if not names:
            return self._names
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO reference_areas(code,name,fetched_at) VALUES(?,?,?)",
                [(code, name, _now()) for code, name in names.items()],
            )
        self._names = names
        return names

    # ---- sorgu
    async def markets(
        self,
        gtip: str,
        *,
        reporter: int = TURKIYE_CODE,
        flow: str = "X",
        year: int | None = None,
        limit: int = 20,
        refresh: bool = False,
    ) -> MarketReport:
        """Bir kod için pazar sıralaması: kim ne kadar alıyor, kg başına ne ödüyor.

        ``flow="X"`` raporlayan ülkenin ihracatı, ``"M"`` ithalatıdır. Yıl verilmezse
        en son tamamlanmış yıl denenir; istatistik gecikmeli yayımlandığı için bir
        önceki yıla düşülebilir.
        """
        code = hs_code(gtip)
        flow_code = "M" if str(flow or "").upper().startswith("M") else "X"
        target_year = int(year) if year else datetime.now(UTC).year - 1
        report = MarketReport(
            hs_code=code, reporter_code=int(reporter), flow=flow_code, year=target_year,
            status="unavailable",
        )
        if not code:
            report.status = "not_found"
            report.warnings.append("Geçerli bir HS kodu türetilemedi (en az 2 hane gerekir).")
            return report

        archived = self.archived(code, reporter, flow_code, target_year)
        age = self._age_days(archived["fetched_at"]) if archived else None
        if archived and not refresh and age is not None and age < self.refresh_days:
            names = await self.area_names()
            report.status = "ok" if archived["rows"] else "not_found"
            report.partners = rank_partners(archived["rows"], limit=limit, names=names)
            report.world = world_total(archived["rows"])
            report.truncated = archived["truncated"]
            report.fetched_at = archived["fetched_at"]
            report.source_url = archived["source_url"]
            report.from_archive = True
            report.age_days = age
            return report

        if not self.enabled:
            report.status = "disabled"
            report.warnings.append("Comtrade kaynağı kapalı (COMTRADE_ENABLED).")
            return report

        url = self._market_url(code, reporter, flow_code, target_year)
        try:
            async with self._lock:
                payload, final_url = await self._get_json(url)
        except ComtradeRateLimited as exc:
            report.status = "rate_limited"
            report.warnings.append(str(exc))
            self._errors = ([*self._errors, "429"])[-10:]
            if archived:
                names = await self.area_names()
                report.status = "ok"
                report.partners = rank_partners(archived["rows"], limit=limit, names=names)
                report.world = world_total(archived["rows"])
                report.truncated = archived["truncated"]
                report.fetched_at = archived["fetched_at"]
                report.from_archive = True
                report.age_days = age
                report.warnings.append("Kaynak hız sınırı uyguladı; arşivdeki kayıt gösteriliyor.")
            return report
        except Exception as exc:
            message = f"Comtrade sorgusu başarısız: {type(exc).__name__}"
            logger.warning("%s (%s)", message, code)
            self._errors = ([*self._errors, message])[-10:]
            if archived:
                names = await self.area_names()
                report.status = "ok"
                report.partners = rank_partners(archived["rows"], limit=limit, names=names)
                report.world = world_total(archived["rows"])
                report.fetched_at = archived["fetched_at"]
                report.from_archive = True
                report.age_days = age
                report.warnings.append("Kaynak yanıt vermedi; arşivdeki kayıt gösteriliyor.")
                return report
            report.warnings.append(message)
            return report

        rows = parse_rows(payload)
        raw_count = len((payload or {}).get("data") or []) if isinstance(payload, dict) else 0
        truncated = raw_count >= PREVIEW_ROW_CAP
        self._store(code, reporter, flow_code, target_year, rows, truncated=truncated, source_url=final_url)
        names = await self.area_names()
        report.status = "ok" if rows else "not_found"
        report.partners = rank_partners(rows, limit=limit, names=names)
        report.world = world_total(rows)
        report.truncated = truncated
        report.source_url = final_url
        report.fetched_at = _now()
        report.age_days = 0
        if truncated:
            report.warnings.append(
                f"Kaynak tek istekte en fazla {PREVIEW_ROW_CAP} satır veriyor; sıralama "
                "eksik olabilir. Daha küçük HS kodu ya da tek partner ile daraltın."
            )
        if not rows:
            report.warnings.append("Bu kod ve yıl için beyan edilmiş istatistik bulunamadı.")
        return report

    async def trend(
        self,
        gtip: str,
        *,
        reporter: int = TURKIYE_CODE,
        flow: str = "X",
        years: int = 5,
        partner: int | None = None,
    ) -> dict[str, Any]:
        """Yıl yıl toplam: eğilim. Her yıl AYRI istek (kaynak çok dönemi 400 ile reddediyor)."""
        span = max(1, min(int(years or 1), COMTRADE_MAX_YEARS))
        last = datetime.now(UTC).year - 1
        series: list[dict[str, Any]] = []
        for offset in range(span):
            year = last - offset
            report = await self.markets(gtip, reporter=reporter, flow=flow, year=year, limit=250)
            if report.status not in {"ok"}:
                series.append({"year": year, "status": report.status, "value_usd": None})
                continue
            if partner is not None:
                match = next(
                    (p for p in report.partners if int(p["partner_code"]) == int(partner)), None
                )
                total = match["value_usd"] if match else None
                weight = match.get("net_weight_kg") if match else None
            else:
                world = report.world
                total = (world or {}).get("value_usd")
                weight = (world or {}).get("net_weight_kg")
                if total is None:
                    total = sum(p["value_usd"] for p in report.partners) or None
            series.append(
                {
                    "year": year,
                    "status": "ok" if total else "not_found",
                    "value_usd": total,
                    "net_weight_kg": weight,
                    "unit_price_usd_per_kg": round(total / weight, 2) if (total and weight) else None,
                    "from_archive": report.from_archive,
                }
            )
        series.sort(key=lambda item: item["year"])
        return {
            "hs_code": hs_code(gtip),
            "reporter_code": int(reporter),
            "flow": "M" if str(flow or "").upper().startswith("M") else "X",
            "partner_code": partner,
            "series": series,
            "source": "un_comtrade",
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
        }

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            queries = connection.execute("SELECT COUNT(*) FROM market_queries").fetchone()[0]
            latest = connection.execute("SELECT MAX(fetched_at) FROM market_queries").fetchone()[0]
            areas = connection.execute("SELECT COUNT(*) FROM reference_areas").fetchone()[0]
        return {
            "enabled": self.enabled,
            "base_url": COMTRADE_BASE,
            "archived_queries": queries,
            "reference_areas": areas,
            "last_fetch_at": latest,
            "refresh_days": self.refresh_days,
            "delay_seconds": self.delay_seconds,
            "cooldown_seconds": max(0, round(self._cooldown_until - _monotonic())),
            "row_cap": PREVIEW_ROW_CAP,
            "errors": list(self._errors[-5:]),
            "source_note": SOURCE_NOTE,
            "statistic_only_note": STATISTIC_ONLY_NOTE,
            "site_url": COMTRADE_SITE_URL,
        }

    async def close(self) -> None:
        await self._http.aclose()


__all__ = [
    "COMTRADE_MAX_YEARS",
    "MIRROR_NOTE",
    "PREVIEW_ROW_CAP",
    "SOURCE_NOTE",
    "STATISTIC_ONLY_NOTE",
    "TURKIYE_CODE",
    "WORLD_CODE",
    "ComtradeEngine",
    "ComtradeRateLimited",
    "MarketReport",
    "hs_code",
    "is_total_row",
    "parse_rows",
    "rank_partners",
    "world_total",
]
