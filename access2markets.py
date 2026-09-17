"""AB vergileri, menşe kuralları ve ticaret koşulları: Access2Markets açık API'si.

Avrupa Komisyonu'nun Access2Markets portalı bir Angular uygulamasıdır; arka planındaki
REST uçları **anahtarsız, ücretsiz ve sonuç döndürüyor** (17.09.2026'da canlı olarak
ölçüldü — varsayım değil):

* ``api/tariffs/get/{kod}/{menşe}/{varış}``   → ölçü satırları (üçüncü ülke vergisi,
  gümrük birliği vergisi, tercihli tarife, askıya alma, kota, ek vergiler)
* ``api/taxes/get/{kod}/{menşe}/{varış}``     → varış ülkesinin iç vergileri (KDV/ÖTV)
* ``api/v2/document/list?...``                → ticaret koşulları: istenen belgeler
* ``webgate…/roo/public/v1/classic/chapter/{fasıl}/country/{ülke}`` → menşe kuralları

Bu modül **ücretli Apify TARIC yolunun yerine geçmez, yanında durur.** ``eu_taric.py``
aynen çalışmaya devam eder; iki kaynak aynı özet şeklini üretir (ortak
``eu_taric.summarise_measures``) ve birbirini doğrular. Ücretsiz kaynak bir kodu
kapsamıyorsa ücretli arşiv hâlâ oradadır; ücretli arşivde olmayan bir kodu ücretsiz
kaynak veriyorsa artık ücret ödenmeden cevap verilebilir.

Ölçülen davranış, kodun dayandığı olgular:

* ``TR/DE`` sorgusunda Türkiye'ye özgü satır gerçekten geliyor
  (``Customs Union Duty | Türkiye | 0% | reg D9601421``) — yani menşe ayrımı yapılıyor.
* 10 haneli kod tek bir grup döndürüyor; 6 haneli kod o başlık altındaki tüm alt
  kalemleri döndürüyor. AB'de karşılığı olmayan kod **boş liste** döndürüyor (hata değil).
* Varış ülkesi AB dışıysa gövde tamamen değişiyor: ölçü listesi yerine ``schemas``
  (GEN/MFN/tercihli) yapısı geliyor. Bu modül o biçimi **oran olarak okumaz**; yalnız
  "AB dışı varış: bu uçtan oran okunmuyor" der. Yanlış okunan bir oranı ürüne sokmak,
  deponun "oran yalnız resmî anlık görüntüden" kuralını çiğnerdi.

**Değişmez:** buradan gelen hiçbir değer ``tariff_engine.calculate_landed_cost``
girdisine aktarılmaz. Türkiye maliyet hesabı yalnız TR resmî anlık görüntüleriyle yapılır;
AB verisi ayrı bir karşılaştırma/ihracat bloğunda, kendi kaynak künyesiyle gösterilir.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import httpx

from eu_taric import CUSTOMS_UNION_NOTE, summarise_measures
from security_firewall import validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

A2M_BASE = (os.environ.get("A2M_BASE_URL") or "https://trade.ec.europa.eu/access-to-markets").rstrip("/")
ROO_BASE = (os.environ.get("A2M_ROO_BASE_URL") or "https://webgate.ec.europa.eu").rstrip("/")
A2M_SITE_URL = "https://trade.ec.europa.eu/access-to-markets/en/home"

_A2M_HOSTS = frozenset({"trade.ec.europa.eu"})
_ROO_HOSTS = frozenset({"webgate.ec.europa.eu"})

_MAX_BYTES = 8 * 1024 * 1024
_MAX_REDIRECTS = 4

SOURCE_NOTE = (
    "Kaynak: Avrupa Komisyonu Access2Markets (trade.ec.europa.eu). Veri portalın kendi "
    "uçlarından alınmıştır; anlık gümrük kararı yerine geçmez."
)
NON_EU_NOTE = (
    "Varış ülkesi AB üyesi değil: Access2Markets bu yönde ölçü satırı yerine tarife şeması "
    "(GEN/MFN/tercihli) döndürüyor. Bu modül oradan oran okumaz."
)
ROO_NOTE = (
    "Menşe kuralları fasıl düzeyinde, Pan-Avrupa-Akdeniz (PEM) Konvansiyonu metninden gelir. "
    "Ürünün kuralı seçilen fasıl tablosunda aranır; tek bir 'kural' cümlesi otomatik seçilmez."
)


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() not in {"0", "false", "no", "off", ""}


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


A2M_ENABLED = _env_flag("A2M_ENABLED", "1")
A2M_TIMEOUT = max(10.0, _env_float("A2M_TIMEOUT_SECONDS", 45.0))
# Alınmış bir kod × menşe × varış üçlüsü bu kadar gün taze sayılır. Ücretsiz olduğu için
# tazeleme bir bütçe sorunu değil, yalnız kaynağa saygı sorunudur.
A2M_REFRESH_DAYS = max(1, _env_int("A2M_REFRESH_DAYS", 45))
# Ücretsiz olduğu için varsayılan AÇIK: bütçe kapısına gerek yok, kataloğun tamamı
# bir kez doldurulunca kullanıcı sorgusu ağa hiç çıkmadan cevaplanır.
A2M_FILL_ENABLED = _env_flag("A2M_FILL_ENABLED", "1")
A2M_FILL_BATCH = max(1, min(_env_int("A2M_FILL_BATCH", 200), 1000))
# İş BİTTİĞİNDE beklenen süre. İş varken bu kullanılmaz (aşağıdaki BUSY kullanılır):
# boş beklemek kataloğu günlerce yarım bırakıyordu.
A2M_FILL_INTERVAL_SECONDS = max(30.0, _env_float("A2M_FILL_INTERVAL_SECONDS", 900.0))
# İş varken turlar arası kısa soluklanma.
A2M_FILL_BUSY_SECONDS = max(0.0, _env_float("A2M_FILL_BUSY_SECONDS", 10.0))
# Ardışık istekler arası bekleme: kaynağa yüklenmemek için (ölçülen yanıt süresi ~1,3 sn).
A2M_DELAY_SECONDS = max(0.0, _env_float("A2M_DELAY_SECONDS", 0.5))
# Eş zamanlı istek sayısı. Kaynak ücretsiz olduğu için eş zamanlılık maliyeti
# değiştirmez, yalnız duvar saatini kısaltır: ölçülen kod başına ~2,3 sn ile sıralı
# dolum 7,7 saat, 3 eş zamanlı ~2,6 saat sürer. Bekleme semaforun İÇİNDE yapılır,
# böylece eş zamanlılık artsa da kaynağa giden istek sıklığı korunur.
A2M_CONCURRENCY = max(1, min(_env_int("A2M_CONCURRENCY", 3), 6))
A2M_DEFAULT_DESTINATION = (os.environ.get("A2M_DEFAULT_DESTINATION") or "DE").strip().upper()[:2] or "DE"

# AB-27 (varış ülkesi bu kümede değilse ölçü satırı beklenmez).
EU_MEMBER_STATES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }
)
# TEDB/TARIC Yunanistan'ı ``EL`` ile anar, ISO ``GR`` der (``eu_vat`` ile aynı tuzak).
_ISO_ALIASES = {"EL": "GR", "UK": "GB"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalise_iso2(value: Any) -> str:
    code = re.sub(r"[^A-Z]", "", str(value or "").upper())[:2]
    return _ISO_ALIASES.get(code, code)


def is_eu_member(value: Any) -> bool:
    return normalise_iso2(value) in EU_MEMBER_STATES


def normalise_code(value: Any, *, width: int = 10) -> str:
    """Türk GTİP'inden AB'de sorgulanacak kodu üretir.

    GTİP'in ilk 8 hanesi AB Kombine Nomanklatürü, 9-10. haneleri TARIC alt açılımıdır;
    11-12. haneler ulusaldır ve AB'de karşılığı yoktur — bu yüzden kesilir.
    """
    digits = _digits(value)
    if len(digits) < 4:
        return ""
    return digits[:width]


def _sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- saf çözümleyiciler

def measure_from_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Access2Markets ölçü satırını deponun ortak ölçü biçimine çevirir.

    Anahtar eşlemesi (ücretli aktörün ``importMeasures`` biçimiyle aynı çıktıyı verir):
    ``type`` → ``measure_type``, ``tariffFormula`` → ``duty_text``,
    ``geographicalArea`` → ``partner_area_code``, ``origin`` → ``partner_area``.
    """
    from eu_taric import classify_measure  # yerel içe aktarma: döngüsel bağımlılık olmasın

    measure_type = _clean(raw.get("type"), 200)
    conditions = [
        _clean(cond.get("description") if isinstance(cond, dict) else cond, 200)
        for cond in (raw.get("conditions") or [])
    ]
    documents = sorted(
        {
            _clean(cond.get("documentCode") if isinstance(cond, dict) else "", 40)
            for cond in (raw.get("conditions") or [])
            if isinstance(cond, dict) and cond.get("documentCode")
        }
    )
    additional = [
        _clean(code, 20)
        for code in (
            [raw.get("additionalCodeId")] if raw.get("additionalCodeId") else []
        )
    ]
    return {
        "measure_type": measure_type,
        "measure_type_code": _clean(raw.get("measureType"), 20),
        "kind": classify_measure(measure_type),
        "duty_text": _clean(raw.get("tariffFormula"), 200),
        "partner_area_code": _clean(raw.get("geographicalSigl") or raw.get("geographicalArea"), 20),
        "partner_area": _clean(raw.get("origin"), 120),
        "inherited": False,
        "order_number": _clean(raw.get("regulationOrderNumber"), 20) or None,
        "additional_codes": [code for code in additional if code][:10],
        "documents": [code for code in documents if code][:10],
        "conditions": [text for text in conditions if text][:10],
        "footnotes": [
            _clean(note.get("code") if isinstance(note, dict) else note, 20)
            for note in (raw.get("footnotes") or [])
        ][:10],
        "legal_basis": _clean(raw.get("regulationId"), 120) or None,
        "start_date": _clean(raw.get("startDate"), 20) or None,
        "end_date": _clean(raw.get("endDate"), 20) or None,
    }


def parse_tariff_groups(payload: Any) -> list[dict[str, Any]]:
    """Ölçü gövdesini ``{description, measures[]}`` gruplarına çevirir.

    AB dışı varış ülkelerinde gövde bir **sözlük** (``schemas``) olarak geliyor; bu
    fonksiyon o durumda boş liste döndürür ve çağıran ``non_eu_destination`` der.
    """
    if not isinstance(payload, list):
        return []
    groups: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        measures = [
            measure_from_payload(raw)
            for raw in (item.get("measures") or [])
            if isinstance(raw, dict)
        ]
        groups.append(
            {
                "code": _digits(item.get("code"))[:10] or None,
                "description": _clean(item.get("description"), 600),
                "measures": measures,
            }
        )
    return groups


def parse_taxes(payload: Any) -> list[dict[str, Any]]:
    """Varış ülkesinin iç vergileri (KDV/ÖTV). Oran ``-`` ise 'veri yok' demektir."""
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rate = _clean(item.get("taxRate"), 60)
        rows.append(
            {
                "tax_type": _clean(item.get("taxType"), 20),
                "label": _clean(item.get("taxLabel"), 200) or None,
                "rate": rate if rate and rate != "-" else None,
                "destination": normalise_iso2(item.get("destinationCountry")) or None,
                "revision_date": _clean(item.get("revisionDate"), 20) or None,
            }
        )
    return rows


def parse_documents(payload: Any) -> list[dict[str, Any]]:
    """Ticaret koşulları: varış ülkesinin istediği belgeler."""
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = _clean(item.get("code"), 60)
        label = _clean(item.get("label"), 300)
        if not code or not label:
            # ``overview`` gibi etiketsiz gezinme kayıtları belge değildir.
            continue
        rows.append({"code": code, "label": label, "kind": _clean(item.get("type"), 10) or None})
    return rows


def parse_rules_of_origin(payload: Any) -> list[dict[str, Any]]:
    """Menşe kuralı bölümleri. HTML gövde **kırpılarak** saklanır, yorumlanmaz."""
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "code": _clean(item.get("code"), 40),
                "label": _clean(item.get("label"), 200),
                "has_rules": bool(_clean(item.get("rules"), 10)),
                "rules_html": str(item.get("rules") or "")[:200_000] or None,
                "notes_html": str(item.get("notes") or "")[:60_000] or None,
                "important_html": str(item.get("important") or "")[:20_000] or None,
                "how_to_read_html": str(item.get("howtoread") or "")[:40_000] or None,
            }
        )
    return rows


# --------------------------------------------------------------------------- sonuç modeli

@dataclass
class A2MResult:
    goods_code: str
    origin: str
    destination: str
    status: str  # ok | disabled | unavailable | not_found | non_eu_destination
    summary: dict[str, Any] = field(default_factory=dict)
    taxes: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: str | None = None
    from_archive: bool = False
    age_days: int | None = None
    stale: bool = False
    source_url: str | None = None
    sha256: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goods_code": self.goods_code,
            "origin": self.origin,
            "destination": self.destination,
            "status": self.status,
            "summary": self.summary,
            "taxes": self.taxes,
            "documents": self.documents,
            "fetched_at": self.fetched_at,
            "from_archive": self.from_archive,
            "age_days": self.age_days,
            "stale": self.stale,
            "source_url": self.source_url,
            "sha256": self.sha256,
            "warnings": self.warnings,
            "source": "access2markets",
            "source_note": SOURCE_NOTE,
            "customs_union_note": CUSTOMS_UNION_NOTE,
        }


def _ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# --------------------------------------------------------------------------- motor

class Access2MarketsEngine:
    """Access2Markets uçlarını çağırır, sonucu kalıcı arşive yazar, ücret doğurmaz."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        enabled: bool | None = None,
        code_source: "Callable[[], Iterable[str]] | None" = None,
        refresh_days: int = A2M_REFRESH_DAYS,
        fill_enabled: bool | None = None,
        fill_batch: int = A2M_FILL_BATCH,
        delay_seconds: float = A2M_DELAY_SECONDS,
        concurrency: int = A2M_CONCURRENCY,
        destination: str = A2M_DEFAULT_DESTINATION,
        origins: Iterable[str] = ("TR",),
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "access2markets.sqlite3"
        self.enabled = A2M_ENABLED if enabled is None else bool(enabled)
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(A2M_TIMEOUT, connect=15.0), follow_redirects=False
        )
        self.code_source = code_source
        self.refresh_days = max(1, int(refresh_days or 1))
        self.fill_enabled = A2M_FILL_ENABLED if fill_enabled is None else bool(fill_enabled)
        self.fill_batch = max(1, min(int(fill_batch or 1), 500))
        self.delay_seconds = max(0.0, float(delay_seconds or 0.0))
        self.concurrency = max(1, min(int(concurrency or 1), 6))
        self.destination = normalise_iso2(destination) or "DE"
        self.origins = tuple(dict.fromkeys(normalise_iso2(o) for o in origins if normalise_iso2(o))) or ("TR",)
        self._errors: list[str] = []
        self._fill_errors: list[str] = []
        self._fill_lock = asyncio.Lock()
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
                CREATE TABLE IF NOT EXISTS tariff_lookups (
                    goods_code TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    taxes_json TEXT NOT NULL DEFAULT '[]',
                    documents_json TEXT NOT NULL DEFAULT '[]',
                    source_url TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (goods_code, origin, destination)
                );
                CREATE INDEX IF NOT EXISTS idx_a2m_code ON tariff_lookups(goods_code);
                CREATE INDEX IF NOT EXISTS idx_a2m_fetched ON tariff_lookups(fetched_at);
                CREATE TABLE IF NOT EXISTS roo_chapters (
                    chapter TEXT NOT NULL,
                    partner TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (chapter, partner)
                );
                """
            )
            _ensure_columns(connection, "tariff_lookups", {"status": "TEXT NOT NULL DEFAULT 'ok'"})
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def archived(self, goods_code: str, origin: str, destination: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json, taxes_json, documents_json, source_url, sha256, status, fetched_at "
                "FROM tariff_lookups WHERE goods_code=? AND origin=? AND destination=?",
                (goods_code, origin, destination),
            ).fetchone()
        if row is None:
            return None
        try:
            summary = json.loads(row["summary_json"])
            taxes = json.loads(row["taxes_json"] or "[]")
            documents = json.loads(row["documents_json"] or "[]")
        except ValueError:
            return None
        return {
            "summary": summary,
            "taxes": taxes,
            "documents": documents,
            "source_url": row["source_url"] or None,
            "sha256": row["sha256"] or None,
            "status": row["status"] or "ok",
            "fetched_at": row["fetched_at"],
        }

    def _store(
        self,
        goods_code: str,
        origin: str,
        destination: str,
        *,
        summary: dict[str, Any],
        taxes: list[dict[str, Any]],
        documents: list[dict[str, Any]],
        source_url: str,
        status: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tariff_lookups"
                "(goods_code,origin,destination,summary_json,taxes_json,documents_json,source_url,sha256,status,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    goods_code,
                    origin,
                    destination,
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(taxes, ensure_ascii=False),
                    json.dumps(documents, ensure_ascii=False),
                    source_url,
                    _sha256(summary),
                    status,
                    _now(),
                ),
            )

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

    # ---- ağ
    async def _get_json(self, url: str, *, allowed_hosts: Iterable[str]) -> tuple[Any, str]:
        """JSON çeker; **her yönlendirme adımında** hedef yeniden doğrulanır."""
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=allowed_hosts)
            response = await self._http.get(current, headers={"Accept": "application/json"})
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("Access2Markets hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            content = response.content
            if len(content) > _MAX_BYTES:
                raise ValueError("Access2Markets yanıtı beklenenden büyük.")
            media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if media_type and "json" not in media_type:
                # Portal hata durumunda 200/404 ile tam sayfa HTML döndürüyor; onu veri sanmayalım.
                raise ValueError(f"Access2Markets JSON yerine {media_type} döndürdü.")
            return json.loads(content.decode("utf-8", errors="replace")), current
        raise ValueError("Access2Markets çok fazla yönlendirme yaptı.")

    def _tariff_url(self, code: str, origin: str, destination: str, *, lang: str = "EN") -> str:
        return f"{A2M_BASE}/api/tariffs/get/{quote(code)}/{quote(origin)}/{quote(destination)}?lang={quote(lang)}"

    def _taxes_url(self, code: str, origin: str, destination: str, *, lang: str = "EN") -> str:
        return f"{A2M_BASE}/api/taxes/get/{quote(code)}/{quote(origin)}/{quote(destination)}?lang={quote(lang)}"

    def _documents_url(self, code: str, origin: str, destination: str, *, lang: str = "EN") -> str:
        query = urlencode(
            {"destinationCountry": destination, "originCountry": origin, "product": code, "lang": lang}
        )
        return f"{A2M_BASE}/api/v2/document/list?{query}"

    def _roo_url(self, chapter: str, partner: str, *, lang: str = "EN") -> str:
        return f"{ROO_BASE}/roo/public/v1/classic/chapter/{quote(chapter)}/country/{quote(partner)}?language={quote(lang)}"

    # ---- sorgu
    async def lookup(
        self,
        goods_code: str,
        *,
        origin: str = "TR",
        destination: str | None = None,
        refresh: bool = False,
        with_extras: bool = True,
    ) -> A2MResult:
        code = normalise_code(goods_code)
        origin_iso = normalise_iso2(origin) or "TR"
        destination_iso = normalise_iso2(destination or self.destination) or self.destination
        result = A2MResult(goods_code=code, origin=origin_iso, destination=destination_iso, status="unavailable")
        if not code:
            result.status = "not_found"
            result.warnings.append("Geçerli bir AB tarife kodu türetilemedi (en az 4 hane gerekir).")
            return result
        if not is_eu_member(destination_iso):
            result.status = "non_eu_destination"
            result.warnings.append(NON_EU_NOTE)
            return result

        archived = self.archived(code, origin_iso, destination_iso)
        age = self._age_days(archived["fetched_at"]) if archived else None
        if archived and not refresh and age is not None and age < self.refresh_days:
            result.status = archived["status"]
            result.summary = archived["summary"]
            result.taxes = archived["taxes"]
            result.documents = archived["documents"]
            result.fetched_at = archived["fetched_at"]
            result.source_url = archived["source_url"]
            result.sha256 = archived["sha256"]
            result.from_archive = True
            result.age_days = age
            return result

        if not self.enabled:
            if archived:
                result.status = archived["status"]
                result.summary = archived["summary"]
                result.taxes = archived["taxes"]
                result.documents = archived["documents"]
                result.fetched_at = archived["fetched_at"]
                result.source_url = archived["source_url"]
                result.sha256 = archived["sha256"]
                result.from_archive = True
                result.age_days = age
                result.stale = True
                result.warnings.append("Access2Markets kapalı; arşivdeki kayıt gösteriliyor.")
                return result
            result.status = "disabled"
            result.warnings.append("Access2Markets kaynağı kapalı (A2M_ENABLED).")
            return result

        try:
            payload, url, matched_code, matched_level = await self._fetch_with_fallback(
                code, origin_iso, destination_iso
            )
        except Exception as exc:  # ağ/biçim hatası ürünü kırmamalı
            message = f"Access2Markets tarife sorgusu başarısız: {type(exc).__name__}"
            logger.warning("%s (%s)", message, code)
            self._errors = ([*self._errors, message])[-20:]
            if archived:
                result.status = archived["status"]
                result.summary = archived["summary"]
                result.taxes = archived["taxes"]
                result.documents = archived["documents"]
                result.fetched_at = archived["fetched_at"]
                result.source_url = archived["source_url"]
                result.sha256 = archived["sha256"]
                result.from_archive = True
                result.age_days = age
                result.stale = True
                result.warnings.append("Kaynak yanıt vermedi; arşivdeki kayıt gösteriliyor.")
                return result
            result.warnings.append(message)
            return result

        if isinstance(payload, dict):
            # AB dışı varışta gövde ``schemas`` sözlüğü olur; buraya normalde düşmeyiz.
            result.status = "non_eu_destination"
            result.warnings.append(NON_EU_NOTE)
            return result

        groups = parse_tariff_groups(payload)
        measures = [measure for group in groups for measure in group["measures"]]
        if not measures:
            result.status = "not_found"
            result.source_url = url
            result.warnings.append("Bu kod AB nomenklatüründe bulunamadı ya da ölçü satırı yok.")
            self._store(
                code, origin_iso, destination_iso,
                summary={}, taxes=[], documents=[], source_url=url, status="not_found",
            )
            result.fetched_at = _now()
            return result

        description = next((group["description"] for group in groups if group["description"]), "")
        summary = summarise_measures(
            measures,
            origin_iso,
            goods_description=description,
            cn_code=matched_code[:8],
        )
        summary["group_count"] = len(groups)
        summary["queried_code"] = code
        summary["matched_code"] = matched_code
        summary["match_level"] = matched_level
        if matched_level != "hs10":
            # Kullanıcı hangi kodun cevap verdiğini görmeli: 10 hanenin karşılığı yoksa
            # oran daha kaba bir kodun oranıdır ve alt kalemler farklılaşabilir.
            result.warnings.append(
                f"AB nomenklatüründe {code} bulunamadı; oran {matched_code} "
                f"({'CN8' if matched_level == 'cn8' else 'HS6'}) düzeyinden okundu."
            )

        taxes: list[dict[str, Any]] = []
        documents: list[dict[str, Any]] = []
        if with_extras:
            taxes = await self._safe_extra(self._taxes_url(code, origin_iso, destination_iso), parse_taxes)
            documents = await self._safe_extra(
                self._documents_url(code, origin_iso, destination_iso), parse_documents
            )

        self._store(
            code, origin_iso, destination_iso,
            summary=summary, taxes=taxes, documents=documents, source_url=url, status="ok",
        )
        result.status = "ok"
        result.summary = summary
        result.taxes = taxes
        result.documents = documents
        result.source_url = url
        result.sha256 = _sha256(summary)
        result.fetched_at = _now()
        result.age_days = 0
        return result


    async def _fetch_with_fallback(
        self, code: str, origin: str, destination: str
    ) -> tuple[Any, str, str, str]:
        """10 hane boş dönerse CN8'e, o da boşsa HS6'ya düşer.

        Bunu ölçüm zorunlu kıldı: 40 fasla yayılmış 120 gerçek GTİP denendiğinde
        **51'i (%42,5) 10 hanede boş döndü ama CN8'de veri verdi.** Sebep yapısal —
        Türk GTİP'inin 9-10. haneleri AB'nin TARIC alt açılımıyla aynı olmak zorunda
        değil; karşılığı olmayan TARIC alt kodu AB'de yoktur, ama CN8 vardır.
        Düşme yapılmazsa bu kodlar "AB'de bulunamadı" diye raporlanır — oysa oran
        bellidir. Hangi düzeyden okunduğu sonuçta ``match_level`` ile taşınır ve
        kullanıcıya uyarı olarak yazılır: sessizce daha kaba bir oran vermek,
        bulunamadı demekten daha tehlikelidir.
        """
        levels: list[tuple[str, str]] = [(code, "hs10")]
        if len(code) > 8:
            levels.append((code[:8], "cn8"))
        if len(code) > 6:
            levels.append((code[:6], "hs6"))
        payload: Any = []
        url = ""
        for candidate, level in levels:
            payload, url = await self._get_json(
                self._tariff_url(candidate, origin, destination), allowed_hosts=_A2M_HOSTS
            )
            if isinstance(payload, dict):
                return payload, url, candidate, level
            if payload:
                return payload, url, candidate, level
        return payload, url, code, "hs10"

    async def _safe_extra(self, url: str, parser: Callable[[Any], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Yan uçlar (vergi, belge) kırılırsa ana oran sonucu düşmemeli."""
        try:
            payload, _ = await self._get_json(url, allowed_hosts=_A2M_HOSTS)
        except Exception as exc:
            self._errors = ([*self._errors, f"{type(exc).__name__}: yan uç alınamadı"])[-20:]
            return []
        return parser(payload)

    async def rules_of_origin(self, chapter: Any, *, partner: str = "TR", refresh: bool = False) -> dict[str, Any]:
        """Fasıl düzeyinde menşe kuralları (PEM Konvansiyonu metni)."""
        chapter_code = _digits(chapter)[:2].zfill(2) if _digits(chapter) else ""
        partner_iso = normalise_iso2(partner) or "TR"
        if not chapter_code or chapter_code == "00":
            return {"status": "not_found", "chapter": chapter_code, "partner": partner_iso, "sections": [], "note": ROO_NOTE}
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, sha256, source_url, fetched_at FROM roo_chapters WHERE chapter=? AND partner=?",
                (chapter_code, partner_iso),
            ).fetchone()
        age = self._age_days(row["fetched_at"]) if row else None
        if row and not refresh and age is not None and age < self.refresh_days:
            try:
                sections = json.loads(row["payload_json"])
            except ValueError:
                sections = []
            return {
                "status": "ok", "chapter": chapter_code, "partner": partner_iso, "sections": sections,
                "sha256": row["sha256"] or None, "source_url": row["source_url"] or None,
                "fetched_at": row["fetched_at"], "from_archive": True, "age_days": age, "note": ROO_NOTE,
            }
        if not self.enabled:
            return {"status": "disabled", "chapter": chapter_code, "partner": partner_iso, "sections": [], "note": ROO_NOTE}
        url = self._roo_url(chapter_code, partner_iso)
        try:
            payload, final_url = await self._get_json(url, allowed_hosts=_ROO_HOSTS)
        except Exception as exc:
            message = f"Menşe kuralı sorgusu başarısız: {type(exc).__name__}"
            self._errors = ([*self._errors, message])[-20:]
            return {"status": "unavailable", "chapter": chapter_code, "partner": partner_iso, "sections": [], "warnings": [message], "note": ROO_NOTE}
        sections = parse_rules_of_origin(payload)
        digest = _sha256(sections)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO roo_chapters(chapter,partner,payload_json,sha256,source_url,fetched_at) VALUES(?,?,?,?,?,?)",
                (chapter_code, partner_iso, json.dumps(sections, ensure_ascii=False), digest, final_url, _now()),
            )
        return {
            "status": "ok" if sections else "not_found", "chapter": chapter_code, "partner": partner_iso,
            "sections": sections, "sha256": digest, "source_url": final_url, "fetched_at": _now(),
            "from_archive": False, "age_days": 0, "note": ROO_NOTE,
        }

    # ---- toplu dolum (ücretsiz)
    def _pending_codes(self, limit: int, origin: str, destination: str) -> list[str]:
        """Hiç alınmamışlar önce, sonra en eski kayıtlar (açlık olmaz)."""
        if self.code_source is None:
            return []
        try:
            raw = list(self.code_source())
        except Exception as exc:
            self._fill_errors = ([*self._fill_errors, f"Kod kaynağı okunamadı: {type(exc).__name__}"])[-10:]
            return []
        candidates = sorted({normalise_code(value) for value in raw if normalise_code(value)})
        if not candidates:
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=self.refresh_days)).isoformat(timespec="seconds")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT goods_code, fetched_at FROM tariff_lookups WHERE origin=? AND destination=?",
                (origin, destination),
            ).fetchall()
        seen = {row["goods_code"]: row["fetched_at"] for row in rows}
        never = [code for code in candidates if code not in seen]
        due = sorted(
            (code for code in candidates if code in seen and seen[code] < cutoff),
            key=lambda code: seen[code],
        )
        return (never + due)[:limit]

    def fill_plan(self) -> dict[str, Any]:
        origin = self.origins[0]
        destination = self.destination
        total = 0
        if self.code_source is not None:
            try:
                total = len({normalise_code(value) for value in self.code_source() if normalise_code(value)})
            except Exception:
                total = 0
        with self._connect() as connection:
            stored = connection.execute(
                "SELECT COUNT(*) FROM tariff_lookups WHERE origin=? AND destination=?", (origin, destination)
            ).fetchone()[0]
            ok_rows = connection.execute(
                "SELECT COUNT(*) FROM tariff_lookups WHERE origin=? AND destination=? AND status='ok'",
                (origin, destination),
            ).fetchone()[0]
        pending = self._pending_codes(10_000, origin, destination)
        return {
            "enabled": self.fill_enabled,
            "origin": origin,
            "destination": destination,
            "total_codes": total,
            "stored": stored,
            "ok": ok_rows,
            "not_found": max(0, stored - ok_rows),
            "pending": len(pending),
            "batch": self.fill_batch,
            "concurrency": self.concurrency,
            "refresh_days": self.refresh_days,
            "cost_usd": 0.0,
            "cost_note": "Access2Markets ücretsizdir; bu dolum hiçbir ücret doğurmaz.",
            "errors": list(self._fill_errors[-5:]),
        }

    async def fill_once(self, *, limit: int | None = None) -> dict[str, Any]:
        if not self.fill_enabled or not self.enabled:
            return {"status": "disabled", "processed": 0, "ok": 0, "not_found": 0}
        async with self._fill_lock:
            origin = self.origins[0]
            destination = self.destination
            codes = self._pending_codes(int(limit or self.fill_batch), origin, destination)
            processed = ok = missing = failed = 0
            gate = asyncio.Semaphore(self.concurrency)

            async def _one(code: str) -> str:
                async with gate:
                    try:
                        result = await self.lookup(
                            code, origin=origin, destination=destination, with_extras=False
                        )
                        status = result.status
                    except Exception as exc:
                        self._fill_errors = (
                            [*self._fill_errors, f"{code}: {type(exc).__name__}"]
                        )[-10:]
                        status = "error"
                    # Bekleme semaforun İÇİNDE: eş zamanlılık artsa da kaynağa giden
                    # istek sıklığı korunur.
                    if self.delay_seconds:
                        await asyncio.sleep(self.delay_seconds)
                    return status

            for status in await asyncio.gather(*(_one(code) for code in codes)):
                if status == "error":
                    failed += 1
                    continue
                processed += 1
                if status == "ok":
                    ok += 1
                elif status == "not_found":
                    missing += 1
                else:
                    failed += 1
            return {
                "status": "complete" if not codes else "ran",
                "processed": processed,
                "ok": ok,
                "not_found": missing,
                "failed": failed,
                "remaining": max(0, len(self._pending_codes(10_000, origin, destination))),
            }

    async def periodic_fill_loop(self, initial_delay: float = 180.0) -> None:
        """İş varken kısa, iş bitince uzun bekler.

        Sabit uzun aralık kataloğu günlerce yarım bırakıyordu: 11.997 kodluk katalog
        200'lük turlarla 15 dakikada bir işlenirse 15 gün sürer. Ücretsiz kaynakta
        boş beklemenin hiçbir karşılığı yok, o yüzden kuyrukta iş kaldıkça tur
        `A2M_FILL_BUSY_SECONDS` sonra tekrarlanır; kuyruk boşalınca uzun aralığa döner.
        """
        await asyncio.sleep(initial_delay)
        while True:
            remaining = 0
            try:
                if self.fill_enabled and self.enabled:
                    report = await self.fill_once()
                    remaining = int(report.get("remaining") or 0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # döngü hiçbir hatada ölmemeli
                logger.warning("Access2Markets dolum turu başarısız: %s", type(exc).__name__)
            await asyncio.sleep(A2M_FILL_BUSY_SECONDS if remaining else A2M_FILL_INTERVAL_SECONDS)

    # ---- durum
    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM tariff_lookups").fetchone()[0]
            ok_rows = connection.execute("SELECT COUNT(*) FROM tariff_lookups WHERE status='ok'").fetchone()[0]
            latest = connection.execute("SELECT MAX(fetched_at) FROM tariff_lookups").fetchone()[0]
            roo_rows = connection.execute("SELECT COUNT(*) FROM roo_chapters").fetchone()[0]
        return {
            "enabled": self.enabled,
            "base_url": A2M_BASE,
            "archived": rows,
            "archived_ok": ok_rows,
            "roo_chapters": roo_rows,
            "last_fetch_at": latest,
            "refresh_days": self.refresh_days,
            "destination": self.destination,
            "origins": list(self.origins),
            "fill": self.fill_plan(),
            "errors": list(self._errors[-5:]),
            "source_note": SOURCE_NOTE,
            "site_url": A2M_SITE_URL,
        }

    async def close(self) -> None:
        await self._http.aclose()


__all__ = [
    "A2MResult",
    "Access2MarketsEngine",
    "EU_MEMBER_STATES",
    "NON_EU_NOTE",
    "ROO_NOTE",
    "SOURCE_NOTE",
    "is_eu_member",
    "measure_from_payload",
    "normalise_code",
    "normalise_iso2",
    "parse_documents",
    "parse_rules_of_origin",
    "parse_tariff_groups",
    "parse_taxes",
]


# --------------------------------------------------------------------------- kaynak karşılaştırma

def _rate_key(value: Any) -> str:
    """İki kaynağın oran metnini karşılaştırılabilir biçime getirir.

    ``"0%"``, ``"0.00 %"`` ve ``"0,00%"`` aynı orandır; ``"12.00%"`` ile ``"12 %"`` de
    öyle. Biçim farkını 'uyuşmazlık' saymak, ücretli kaynağı gereksiz yere haklı
    çıkarırdı — bu yüzden sayı ayıklanır, sayı yoksa metin sadeleştirilir.
    """
    text = _clean(value, 200).lower().replace(",", ".")
    if not text:
        return ""
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if numbers and "%" in text:
        return f"{float(numbers[0]):.2f}%"
    if numbers:
        unit = re.sub(r"[\d\s.]+", " ", text).strip()
        return f"{float(numbers[0]):.2f} {unit}".strip()
    return re.sub(r"[^a-zçğıöşü ]", "", text).strip()


def compare_summaries(paid: dict[str, Any] | None, free: dict[str, Any] | None) -> dict[str, Any]:
    """Aynı kod için ücretli ve ücretsiz özetin oran alanlarını karşılaştırır (saf fonksiyon)."""
    paid = paid or {}
    free = free or {}
    fields: dict[str, Any] = {}
    agree = True
    for key in ("mfn_rate", "partner_rate"):
        left = _rate_key(paid.get(key))
        right = _rate_key(free.get(key))
        same = left == right
        # Bir tarafta oran yok, diğerinde var: bu uyuşmazlık değil **kapsam farkı**dır;
        # ayrı raporlanır ki "veri yanlış" ile "veri eksik" karışmasın.
        missing = bool(left) != bool(right)
        if not same and not missing:
            agree = False
        fields[key] = {"paid": paid.get(key), "free": free.get(key), "match": same, "coverage_gap": missing}
    return {
        "agree": agree,
        "fields": fields,
        "paid_rate_status": paid.get("rate_status"),
        "free_rate_status": free.get("rate_status"),
        "paid_documents": len(paid.get("required_documents") or []),
        "free_documents": len(free.get("required_documents") or []),
    }


async def compare_sources(
    paid_engine: Any,
    free_engine: "Access2MarketsEngine",
    *,
    limit: int = 25,
    destination: str | None = None,
) -> dict[str, Any]:
    """Ücretli arşivdeki çiftleri ücretsiz kaynakla karşılaştırır.

    **Ücret doğurmaz:** ücretli taraf yalnız ``archived()`` ile okunur, aktör hiç
    çağrılmaz; ücretsiz taraf zaten bedavadır. Karar ("Apify durdurulsun mu")
    bu ölçümün sonucuna bakılarak verilir, tahminle değil.
    """
    pairs = sorted(paid_engine._stored_pairs())[: max(1, min(int(limit or 1), 200))]
    rows: list[dict[str, Any]] = []
    agree = disagree = gap = missing_free = 0
    for code, partner in pairs:
        archived = paid_engine.archived(code, partner)
        if not archived:
            continue
        free = await free_engine.lookup(
            code, origin=partner, destination=destination, with_extras=False
        )
        if free.status != "ok":
            missing_free += 1
            rows.append({"code": code, "partner": partner, "free_status": free.status, "agree": None})
            continue
        verdict = compare_summaries(archived.get("summary"), free.summary)
        if any(field["coverage_gap"] for field in verdict["fields"].values()):
            gap += 1
        elif verdict["agree"]:
            agree += 1
        else:
            disagree += 1
        rows.append({"code": code, "partner": partner, "free_status": "ok", **verdict})
    compared = agree + disagree + gap
    return {
        "compared": compared,
        "agree": agree,
        "disagree": disagree,
        "coverage_gap": gap,
        "free_missing": missing_free,
        "agreement_rate": round(agree / compared, 4) if compared else None,
        "rows": rows[:100],
        "cost_usd": 0.0,
        "note": (
            "Ücretli taraf yalnız arşivden okundu (yeni sorgu yapılmadı, ücret doğmadı). "
            "'coverage_gap' bir tarafta oranın hiç olmaması demektir; yanlış oran değildir."
        ),
    }
