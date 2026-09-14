"""AB TARIC önlemleri: Apify aktörü üzerinden kod × ülke sorgusu ve kalıcı arşiv.

Avrupa Komisyonu TARIC verisini **danışma ekranı** olarak yayımlıyor; o ekran robots
politikasıyla otomatik erişime kapalı ve zaten sonuç değil arama formu döndürüyor
(canlı olarak sınandı). Ham veri ise ``TARIC & Quota Data and Information`` CIRCABC
grubunda aylık XLSX çıkarımları hâlinde yayımlanıyor fakat grup listelemesi hesap
istiyor.

Bu modül, aradaki boşluğu kullanıcının seçtiği yoldan kapatır: Apify'daki
``nordicdataforge/eu-taric-customs-measures-monitor`` aktörü aynı **resmî aylık XLSX
çıkarımını** işleyip yapılandırılmış ölçü satırları döndürüyor. Aktör sorgu başına
ücretli olduğu için:

* her sorgu **kalıcı arşive** yazılır ve aynı kod × ülke × ay için bir daha ücret
  ödenmez (kaynak zaten aylık anlık görüntü olduğundan bu doğru davranıştır),
* arka planda **kendiliğinden dolum yapılmaz** — yalnız kullanıcı istediğinde çağrılır,
* jeton yalnız ortam değişkeninden okunur, günlüğe ve hata metnine asla yazılmaz.

Ölçü satırlarından türetilen özet (üçüncü ülke vergisi, menşeye özgü tercihli oran,
ek vergiler, gereken belgeler) **koşulludur**: aktörün kendi belgesinin de vurguladığı
gibi TARIC'te tek bir "nihai vergi" sayısı yoktur; oran ek koda, kotaya, belgeye ve
nihai kullanıma bağlıdır. Bu yüzden sonuç ayrı bir karşılaştırma bloğunda gösterilir ve
**hiçbir değeri Türkiye maliyet hesabına girmez.**
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from security_firewall import SecurityViolation, validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

APIFY_BASE = (os.environ.get("APIFY_BASE_URL") or "https://api.apify.com/v2").rstrip("/")
APIFY_ACTOR = os.environ.get("EU_TARIC_ACTOR") or "nordicdataforge~eu-taric-customs-measures-monitor"
_OFFICIAL_HOSTS = frozenset({"api.apify.com"})
EU_TARIC_ENABLED = (os.environ.get("EU_TARIC_ENABLED") or "0").strip().lower() not in {"0", "false", "no", "off", ""}
EU_TARIC_TIMEOUT = max(30.0, float(os.environ.get("EU_TARIC_TIMEOUT_SECONDS") or 180))
EU_TARIC_MAX_CODES = max(1, min(int(os.environ.get("EU_TARIC_MAX_CODES") or 5), 20))
_MAX_BYTES = 16 * 1024 * 1024


def _env_flag(name: str, default: str = "0") -> bool:
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


# --- Toplu dolum (tüm fasıllar) ------------------------------------------------
# Kaynak sorgu başına ücretli olduğundan dolum üç kapıdan geçer: açık olmalı, aylık
# harcama tavanı aşılmamış olmalı ve aynı kod × ülke × ay arşivde bulunmamalı.
EU_TARIC_FILL_ENABLED = _env_flag("EU_TARIC_FILL_ENABLED")
EU_TARIC_FILL_LEVEL = (os.environ.get("EU_TARIC_FILL_LEVEL") or "hs10").strip().lower()
EU_TARIC_FILL_ORIGINS = os.environ.get("EU_TARIC_FILL_ORIGINS") or "TR"
EU_TARIC_FILL_BATCH = max(1, min(_env_int("EU_TARIC_FILL_BATCH", 40), 500))
EU_TARIC_FILL_INTERVAL_SECONDS = max(60.0, _env_float("EU_TARIC_FILL_INTERVAL_SECONDS", 900.0))
# Varsayılan 0: tavan açıkça verilmeden hiçbir dolum sorgusu yapılmaz.
EU_TARIC_MONTHLY_BUDGET_USD = max(0.0, _env_float("EU_TARIC_MONTHLY_BUDGET_USD", 0.0))
EU_TARIC_UNIT_COST_USD = max(0.0, _env_float("EU_TARIC_UNIT_COST_USD", 0.015))

_FILL_LEVELS: dict[str, int] = {"hs6": 6, "hs8": 8, "cn8": 8, "hs10": 10, "taric10": 10}

SOURCE_NOTE = (
    "Kaynak: Avrupa Komisyonu'nun resmî aylık TARIC ham veri çıkarımı (TARIC & Quota Data and "
    "Information). Veri aylık anlık görüntüdür, anlık gümrük kararı değildir."
)
CONDITIONAL_NOTE = (
    "TARIC'te tek bir nihai vergi sayısı yoktur: oran ek koda, tarife kontenjanına, ibraz edilen "
    "belgeye ve nihai kullanıma göre değişir. Aşağıdaki özet koşulludur ve Türkiye maliyet "
    "hesabına aktarılmaz."
)
CUSTOMS_UNION_NOTE = (
    "Türkiye–AB Gümrük Birliği sanayi ürünlerinde gümrük vergisini kaldırır; serbest dolaşım "
    "statüsü A.TR dolaşım belgesiyle kanıtlanır. Tarım ürünleri, AKÇT ürünleri ve ticaret "
    "politikası savunma önlemleri ayrıca değerlendirilir."
)

# Ölçü türü kodları yerine metin üzerinden sınıflandırma (aktör yerelleştirilmiş metin döndürür).
_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("third country duty", "third_country_duty"),
    ("customs union duty", "customs_union_duty"),
    ("tariff preference", "preference"),
    ("preferential", "preference"),
    ("suspension", "suspension"),
    ("quota", "quota"),
    ("anti-dumping", "anti_dumping"),
    ("antidumping", "anti_dumping"),
    ("countervailing", "countervailing"),
    ("safeguard", "safeguard"),
    ("additional duties", "additional_duty"),
    ("agricultural component", "agricultural_component"),
    ("prohibition", "prohibition"),
    ("restriction", "restriction"),
    ("surveillance", "surveillance"),
    ("import control", "restriction"),
)

KIND_LABELS = {
    "third_country_duty": "Üçüncü ülke gümrük vergisi",
    "customs_union_duty": "Gümrük birliği vergisi",
    "preference": "Tercihli tarife",
    "suspension": "Vergi askıya alma",
    "quota": "Tarife kontenjanı",
    "anti_dumping": "Damping önlemi",
    "countervailing": "Telafi edici vergi",
    "safeguard": "Korunma önlemi",
    "additional_duty": "Ek vergi",
    "agricultural_component": "Tarım bileşeni",
    "prohibition": "İthalat yasağı",
    "restriction": "İthalat kontrolü / kısıtlama",
    "surveillance": "Gözetim",
    "other": "Diğer önlem",
}
_EXTRA_DUTY_KINDS = frozenset({"anti_dumping", "countervailing", "safeguard", "additional_duty", "agricultural_component"})


def _valid_token(value: Any) -> str:
    """Jetonu yalnız HTTP başlığında güvenle taşınabiliyorsa kabul eder (boşluk/ASCII dışı yok)."""
    token = str(value or "").strip()
    if not token or len(token) > 400:
        return ""
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in token):
        return ""
    return token


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalise_goods_code(value: Any) -> str:
    """Türk GTİP'i veya kısa kod → aktörün beklediği 10 haneli TARIC kodu."""
    code = _digits(value)
    if not code:
        return ""
    return (code[:10] + "0" * 10)[:10]


def _parse_origins(value: Any) -> tuple[str, ...]:
    """``"TR,CN"`` ya da liste → normalleştirilmiş, tekrarsız menşe kodları."""
    if isinstance(value, str):
        parts: Iterable[Any] = value.replace(";", ",").split(",")
    elif value is None:
        parts = ()
    else:
        parts = value
    seen: list[str] = []
    for part in parts:
        code = re.sub(r"[^A-Z0-9]", "", str(part or "").upper())[:4]
        if code and code not in seen:
            seen.append(code)
    return tuple(seen)


def month_key(moment: datetime | None = None) -> str:
    """Harcama tavanının izlendiği takvim ayı (UTC, ``YYYY-MM``)."""
    return (moment or datetime.now(UTC)).strftime("%Y-%m")


def candidate_codes(gtip_codes: Any, *, level: str = EU_TARIC_FILL_LEVEL) -> list[str]:
    """Türk GTİP listesinden AB'de sorgulanacak 10 haneli TARIC adaylarını üretir.

    Türk GTİP'inin ilk 8 hanesi AB Kombine Nomanklatürü, 9-10. haneleri AB'nin TARIC
    alt açılımıdır; 11-12. haneler ulusaldır ve AB'de karşılığı yoktur. Bu yüzden
    ``hs10`` düzeyinde ilk 10 hane doğrudan aday koddur, daha kaba düzeylerde kalan
    haneler sıfırlanır.
    """
    width = _FILL_LEVELS.get(str(level or "").strip().lower(), 10)
    seen: set[str] = set()
    for value in gtip_codes or ():
        digits = _digits(value)
        if len(digits) < 6:
            continue
        seen.add((digits[:width] + "0" * 10)[:10])
    return sorted(seen)


def classify_measure(description: Any) -> str:
    text = str(description or "").lower()
    for needle, kind in _KIND_PATTERNS:
        if needle in text:
            return kind
    return "other"


def _clean(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# --------------------------------------------------------------------------- saf çözümleyici

def parse_measures(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Aktörün ``importMeasures`` listesini ortak ölçü biçimine çevirir."""
    measures: list[dict[str, Any]] = []
    for raw in item.get("importMeasures") or []:
        if not isinstance(raw, dict):
            continue
        measure_type = _clean(raw.get("measureType"))
        documents = [
            _clean(doc.get("code") if isinstance(doc, dict) else doc, 40)
            for doc in (raw.get("documents") or [])
        ]
        conditions = [
            _clean(cond.get("description") if isinstance(cond, dict) else cond, 200)
            for cond in (raw.get("conditions") or [])
        ]
        measures.append(
            {
                "measure_type": measure_type,
                "measure_type_code": _clean(raw.get("measureTypeCode"), 20),
                "kind": classify_measure(measure_type),
                "duty_text": _clean(raw.get("dutyText"), 200),
                "partner_area_code": _clean(raw.get("partnerAreaCode"), 20),
                "partner_area": _clean(raw.get("partnerArea"), 120),
                "inherited": bool(raw.get("inherited")),
                "order_number": _clean(raw.get("orderNumber"), 20) or None,
                "additional_codes": [_clean(code, 20) for code in (raw.get("additionalCodes") or [])][:10],
                "documents": [code for code in documents if code][:10],
                "conditions": [text for text in conditions if text][:10],
                "footnotes": [_clean(note.get("code") if isinstance(note, dict) else note, 20) for note in (raw.get("footnotes") or [])][:10],
                "legal_basis": _clean(raw.get("legalBase") or raw.get("legalBasis"), 120) or None,
            }
        )
    return measures


def resolve_rates(item: dict[str, Any], partner: str | None) -> dict[str, Any]:
    """Ölçü satırlarından koşullu bir özet üretir (tek bir 'nihai vergi' iddia etmez)."""
    measures = parse_measures(item)
    partner = (partner or "").upper() or None
    erga = next(
        (m for m in measures if m["kind"] == "third_country_duty" and m["partner_area_code"] in {"1011", ""}),
        None,
    )
    if erga is None:
        erga = next((m for m in measures if m["kind"] == "third_country_duty"), None)
    partner_measures = [
        m for m in measures
        if partner and (m["partner_area_code"] == partner or partner in m["partner_area"].upper())
    ]
    preference = next(
        (m for m in partner_measures if m["kind"] in {"preference", "customs_union_duty", "suspension"}),
        None,
    )
    extra = [m for m in measures if m["kind"] in _EXTRA_DUTY_KINDS and (not partner or not partner_measures or m in partner_measures or m["partner_area_code"] in {"1011", ""})]
    documents = sorted({code for m in ((preference,) if preference else ()) for code in m["documents"]})
    conditional = bool(documents) or any(m["additional_codes"] or m["conditions"] for m in measures)
    return {
        "mfn_rate": erga["duty_text"] if erga else None,
        "mfn_measure": erga,
        "partner_rate": preference["duty_text"] if preference else None,
        "partner_rate_kind": preference["kind"] if preference else None,
        "partner_measure": preference,
        "required_documents": documents,
        "additional_duties": extra[:10],
        "measures": [m for m in measures if m["kind"] != "other"][:40],
        "rate_status": "conditional" if conditional else ("definitive" if erga or preference else "unknown"),
        "snapshot_month": _clean(item.get("sourceSnapshotMonth"), 20) or None,
        "snapshot_date": _clean(item.get("sourceSnapshotDate"), 20) or None,
        "goods_description": _clean(item.get("goodsDescription"), 600),
        "cn_code": _digits(item.get("cnCode"))[:8] or None,
    }


# --------------------------------------------------------------------------- sonuç modeli

@dataclass
class EuTaricResult:
    goods_code: str
    partner_country: str | None
    status: str  # ok | disabled | unavailable
    summary: dict[str, Any] = field(default_factory=dict)
    fetched_at: str | None = None
    from_archive: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goods_code": self.goods_code,
            "partner_country": self.partner_country,
            "status": self.status,
            "summary": self.summary,
            "fetched_at": self.fetched_at,
            "from_archive": self.from_archive,
            "warnings": self.warnings,
            "source_note": SOURCE_NOTE,
            "conditional_note": CONDITIONAL_NOTE,
            "customs_union_note": CUSTOMS_UNION_NOTE,
        }


# --------------------------------------------------------------------------- motor

def _ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Mevcut tabloya eksik sütunları ekler (``CREATE TABLE IF NOT EXISTS`` var olan tabloyu değiştirmez)."""
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if not existing:
        return
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


class EuTaricEngine:
    """Apify aktörünü çağırır, sonucu kalıcı arşive yazar ve aynı sorgu için bir daha ücret ödemez."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        token: str | None = None,
        actor: str = APIFY_ACTOR,
        enabled: bool | None = None,
        code_source: "Callable[[], Iterable[str]] | None" = None,
        fill_enabled: bool | None = None,
        fill_level: str = EU_TARIC_FILL_LEVEL,
        fill_origins: "Iterable[str] | str | None" = None,
        fill_batch: int = EU_TARIC_FILL_BATCH,
        monthly_budget_usd: float | None = None,
        unit_cost_usd: float = EU_TARIC_UNIT_COST_USD,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "eu_taric.sqlite3"
        self.actor = actor
        raw_token = token if token is not None else (os.environ.get("APIFY_TOKEN") or "")
        self._token = _valid_token(raw_token)
        if raw_token.strip() and not self._token:
            logger.warning("APIFY_TOKEN başlık olarak gönderilemeyecek karakterler içeriyor; devre dışı bırakıldı.")
        self.enabled = EU_TARIC_ENABLED if enabled is None else enabled
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(EU_TARIC_TIMEOUT, connect=15.0))
        self._lock = asyncio.Lock()
        self._errors: list[str] = []
        self.code_source = code_source
        self.fill_enabled = EU_TARIC_FILL_ENABLED if fill_enabled is None else bool(fill_enabled)
        self.fill_level = str(fill_level or "hs10").strip().lower()
        self.fill_origins = _parse_origins(fill_origins if fill_origins is not None else EU_TARIC_FILL_ORIGINS)
        self.fill_batch = max(1, min(int(fill_batch or 1), 500))
        self.monthly_budget_usd = max(
            0.0, EU_TARIC_MONTHLY_BUDGET_USD if monthly_budget_usd is None else float(monthly_budget_usd)
        )
        self.unit_cost_usd = max(0.0, float(unit_cost_usd or 0.0))
        self._fill_lock = asyncio.Lock()
        self._fill_errors: list[str] = []
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
                CREATE TABLE IF NOT EXISTS lookups (
                    goods_code TEXT NOT NULL,
                    partner_country TEXT NOT NULL,
                    snapshot_month TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (goods_code, partner_country, snapshot_month)
                );
                CREATE INDEX IF NOT EXISTS idx_eu_taric_code ON lookups(goods_code);
                CREATE TABLE IF NOT EXISTS fill_attempts (
                    goods_code TEXT NOT NULL,
                    partner_country TEXT NOT NULL,
                    period TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (goods_code, partner_country, period)
                );
                CREATE INDEX IF NOT EXISTS idx_eu_taric_fill_period ON fill_attempts(period, status);
                CREATE TABLE IF NOT EXISTS fill_spend (
                    period TEXT PRIMARY KEY,
                    lookups INTEGER NOT NULL DEFAULT 0,
                    usd REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            _ensure_columns(connection, "fill_attempts", {"note": "TEXT NOT NULL DEFAULT ''"})
            _ensure_columns(connection, "fill_spend", {"updated_at": "TEXT NOT NULL DEFAULT ''"})
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def archived(self, goods_code: str, partner: str) -> dict[str, Any] | None:
        """En son alınmış özet (ay fark etmeksizin en tazesi)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json, fetched_at, snapshot_month FROM lookups WHERE goods_code=? AND partner_country=? "
                "ORDER BY snapshot_month DESC, fetched_at DESC LIMIT 1",
                (goods_code, partner),
            ).fetchone()
        if row is None:
            return None
        try:
            summary = json.loads(row["summary_json"])
        except ValueError:
            return None
        return {"summary": summary, "fetched_at": row["fetched_at"], "snapshot_month": row["snapshot_month"]}

    def _store(self, goods_code: str, partner: str, summary: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO lookups(goods_code,partner_country,snapshot_month,summary_json,fetched_at) "
                "VALUES(?,?,?,?,?)",
                (
                    goods_code, partner, str(summary.get("snapshot_month") or ""),
                    json.dumps(summary, ensure_ascii=False), _now(),
                ),
            )

    # ---- Apify çağrısı
    async def _run_actor(self, goods_codes: list[str], partner: str) -> list[dict[str, Any]]:
        if not self._token:
            raise SecurityViolation("Apify jetonu yapılandırılmamış.", code="config_missing")
        url = f"{APIFY_BASE}/acts/{quote(self.actor, safe='~')}/run-sync-get-dataset-items"
        validate_outbound_url(url, allowed_hosts=_OFFICIAL_HOSTS)
        payload = {
            "goodsCodes": goods_codes,
            "partnerCountries": [partner],
            "direction": "import",
            "language": "en",
            "snapshotMonth": "latest",
            "enableChangeMonitoring": False,
            "outputMode": "all",
        }
        try:
            response = await self._http.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Jeton hiçbir hata metnine sızmamalı.
            raise RuntimeError(f"Apify aktörü {exc.response.status_code} döndürdü.") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Apify aktörüne ulaşılamadı: {type(exc).__name__}") from None
        except (UnicodeEncodeError, TypeError) as exc:  # bozuk jeton/başlık: jeton metne sızmasın
            raise RuntimeError(f"Apify isteği oluşturulamadı: {type(exc).__name__}") from None
        content = response.content
        if len(content) > _MAX_BYTES:
            raise ValueError("Apify yanıtı beklenenden büyük.")
        try:
            items = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Apify yanıtı çözümlenemedi.") from exc
        if not isinstance(items, list):
            raise ValueError("Apify yanıtı beklenen biçimde değil.")
        return [item for item in items if isinstance(item, dict)]

    # ---- sorgu
    async def lookup(self, gtip: str, *, origin: str = "TR", refresh: bool = False) -> EuTaricResult:
        code = normalise_goods_code(gtip)
        partner = (str(origin or "TR").strip().upper() or "TR")[:4]
        if len(_digits(gtip)) < 6:
            raise ValueError("AB TARIC sorgusu için en az 6 haneli bir kod gerekir.")
        result = EuTaricResult(goods_code=code, partner_country=partner, status="unavailable")
        if not refresh:
            archived = self.archived(code, partner)
            if archived is not None:
                result.status = "ok"
                result.summary = archived["summary"]
                result.fetched_at = archived["fetched_at"]
                result.from_archive = True
                return result
        if not self.enabled or not self._token:
            result.status = "disabled"
            result.warnings.append(
                "AB TARIC sorgusu kapalı: bu kaynak sorgu başına ücretlidir ve etkinleştirilmesi için "
                "yönetici tarafından açılması gerekir."
            )
            return result
        async with self._lock:
            try:
                items = await self._run_actor([code], partner)
            except (SecurityViolation, RuntimeError, ValueError) as exc:
                message = str(exc)[:200]
                self._errors.append(f"{_now()}: {message}")
                result.warnings.append(f"AB TARIC verisi alınamadı: {message}")
                archived = self.archived(code, partner)
                if archived is not None:
                    result.status = "ok"
                    result.summary = archived["summary"]
                    result.fetched_at = archived["fetched_at"]
                    result.from_archive = True
                    result.warnings.append("Arşivdeki son bilinen AB verisi gösteriliyor.")
                return result
        match = next((item for item in items if _digits(item.get("goodsCode")) == code), None) or (items[0] if items else None)
        if match is None:
            result.warnings.append("AB TARIC bu kod için sonuç döndürmedi.")
            return result
        summary = resolve_rates(match, partner)
        self._store(code, partner, summary)
        self._errors.clear()
        result.status = "ok"
        result.summary = summary
        result.fetched_at = _now()
        return result

    # ---- toplu dolum (tüm fasıllar)
    def _spend_row(self, period: str) -> tuple[int, float]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lookups, usd FROM fill_spend WHERE period=?", (period,)
            ).fetchone()
        if row is None:
            return 0, 0.0
        return int(row["lookups"] or 0), float(row["usd"] or 0.0)

    def _record_spend(self, period: str, count: int) -> None:
        if count <= 0:
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fill_spend(period,lookups,usd,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(period) DO UPDATE SET lookups=lookups+excluded.lookups, "
                "usd=usd+excluded.usd, updated_at=excluded.updated_at",
                (period, count, count * self.unit_cost_usd, _now()),
            )

    def _record_attempts(self, period: str, rows: list[tuple[str, str, str, str]]) -> None:
        if not rows:
            return
        moment = _now()
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO fill_attempts(goods_code,partner_country,period,status,attempted_at,note) "
                "VALUES(?,?,?,?,?,?)",
                [(code, partner, period, status, moment, note[:200]) for code, partner, status, note in rows],
            )

    def spend_status(self, period: str | None = None) -> dict[str, Any]:
        """Bu ayın dolum harcaması ve tavana kalan pay (tavan aşılırsa dolum durur)."""
        period = period or month_key()
        lookups, usd = self._spend_row(period)
        remaining_usd = max(0.0, self.monthly_budget_usd - usd)
        remaining = int(remaining_usd / self.unit_cost_usd) if self.unit_cost_usd > 0 else 0
        return {
            "period": period,
            "lookups": lookups,
            "spent_usd": round(usd, 4),
            "budget_usd": round(self.monthly_budget_usd, 2),
            "remaining_usd": round(remaining_usd, 4),
            "remaining_lookups": remaining,
            "unit_cost_usd": self.unit_cost_usd,
        }

    def candidates(self) -> list[str]:
        """Yapılandırılmış düzeye göre aday TARIC kodları (ücret doğurmaz)."""
        if self.code_source is None:
            return []
        try:
            codes = self.code_source()
        except Exception as exc:  # kaynak motor hazır değilse dolum sessizce beklesin
            logger.warning("AB TARIC dolum kod kaynağı okunamadı: %s", type(exc).__name__)
            return []
        return candidate_codes(codes, level=self.fill_level)

    def _done_pairs(self, period: str) -> set[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT goods_code, partner_country FROM fill_attempts WHERE period=?", (period,)
            ).fetchall()
        return {(row["goods_code"], row["partner_country"]) for row in rows}

    def _archived_pairs(self, period: str) -> set[tuple[str, str]]:
        """Bu ay zaten (talep üzerine) alınmış kod × ülke çiftleri: bir daha ücret ödenmez."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT goods_code, partner_country FROM lookups WHERE substr(fetched_at,1,7)=?", (period,)
            ).fetchall()
        return {(row["goods_code"], row["partner_country"]) for row in rows}

    def fill_plan(self) -> dict[str, Any]:
        """Dolumun mevcut durumu: aday sayısı, kalan iş ve tahmini aylık maliyet (ücretsiz)."""
        period = month_key()
        codes = self.candidates()
        origins = self.fill_origins
        total = len(codes) * len(origins)
        done = self._done_pairs(period) | self._archived_pairs(period)
        pending = sum(1 for code in codes for origin in origins if (code, origin) not in done)
        spend = self.spend_status(period)
        return {
            "enabled": bool(self.fill_enabled and self.enabled and self._token and self.monthly_budget_usd > 0),
            "level": self.fill_level,
            "origins": list(origins),
            "candidate_codes": len(codes),
            "total_pairs": total,
            "completed_pairs": total - pending,
            "pending_pairs": pending,
            "batch": self.fill_batch,
            "estimated_total_usd": round(total * self.unit_cost_usd, 2),
            "estimated_pending_usd": round(pending * self.unit_cost_usd, 2),
            "spend": spend,
            "errors": self._fill_errors[-5:],
        }

    async def fill_once(self, *, limit: int | None = None) -> dict[str, Any]:
        """Bir turluk dolum: tavan ve arşiv kontrolünden geçen çiftleri toplu olarak sorgular."""
        period = month_key()
        if not (self.fill_enabled and self.enabled and self._token):
            return {"status": "disabled", "period": period, "requested": 0, "fetched": 0, "charged": 0}
        spend = self.spend_status(period)
        if spend["remaining_lookups"] <= 0:
            return {"status": "budget_exhausted", "period": period, "requested": 0, "fetched": 0, "charged": 0, "spend": spend}
        async with self._fill_lock:
            codes = await asyncio.to_thread(self.candidates)
            if not codes:
                return {"status": "no_candidates", "period": period, "requested": 0, "fetched": 0, "charged": 0}
            done = await asyncio.to_thread(self._done_pairs, period)
            archived = await asyncio.to_thread(self._archived_pairs, period)
            budget = min(int(limit or self.fill_batch), spend["remaining_lookups"])
            pending: list[tuple[str, str]] = []
            skipped: list[tuple[str, str, str, str]] = []
            for code in codes:
                for origin in self.fill_origins:
                    pair = (code, origin)
                    if pair in done:
                        continue
                    if pair in archived:
                        skipped.append((code, origin, "archived", "talep üzerine zaten alınmış"))
                        continue
                    pending.append(pair)
                    if len(pending) >= budget:
                        break
                if len(pending) >= budget:
                    break
            if skipped:
                await asyncio.to_thread(self._record_attempts, period, skipped)
            if not pending:
                return {"status": "complete", "period": period, "requested": 0, "fetched": 0, "charged": 0, "skipped": len(skipped)}

            fetched = 0
            charged = 0
            missing = 0
            failed = 0
            by_origin: dict[str, list[str]] = {}
            for code, origin in pending:
                by_origin.setdefault(origin, []).append(code)
            for origin, origin_codes in by_origin.items():
                for start in range(0, len(origin_codes), EU_TARIC_MAX_CODES):
                    chunk = origin_codes[start : start + EU_TARIC_MAX_CODES]
                    try:
                        async with self._lock:
                            items = await self._run_actor(chunk, origin)
                    except (SecurityViolation, RuntimeError, ValueError) as exc:
                        message = str(exc)[:200]
                        self._fill_errors.append(f"{_now()}: {message}")
                        failed += len(chunk)
                        # Hatalı tur kaydedilmez; bir sonraki turda yeniden denenir.
                        continue
                    found = {
                        code: item
                        for item in items
                        for code in (normalise_goods_code(item.get("goodsCode")),)
                        if code
                    }
                    attempts: list[tuple[str, str, str, str]] = []
                    chunk_ok = 0
                    for code in chunk:
                        item = found.get(code)
                        if item is None:
                            # Beyana elverişli olmayan kod aktörde ücretlendirilmez.
                            missing += 1
                            attempts.append((code, origin, "not_declarable", "AB bu ay için bu kodda sonuç döndürmedi"))
                            continue
                        summary = resolve_rates(item, origin)
                        await asyncio.to_thread(self._store, code, origin, summary)
                        chunk_ok += 1
                        attempts.append((code, origin, "ok", str(summary.get("snapshot_month") or "")))
                    fetched += chunk_ok
                    charged += chunk_ok
                    await asyncio.to_thread(self._record_attempts, period, attempts)
                    await asyncio.to_thread(self._record_spend, period, chunk_ok)
            if fetched:
                self._errors.clear()
            return {
                "status": "ok" if fetched or missing else "failed",
                "period": period,
                "requested": len(pending),
                "fetched": fetched,
                "charged": charged,
                "not_declarable": missing,
                "failed": failed,
                "skipped": len(skipped),
                "spend": self.spend_status(period),
            }

    async def fill_loop(self, *, initial_delay: float = 420.0) -> None:
        """Arka plan dolum döngüsü: tavan dolana ya da adaylar bitene kadar kademeli ilerler."""
        await asyncio.sleep(initial_delay)
        while True:
            try:
                report = await self.fill_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AB TARIC toplu dolumu başarısız oldu")
                report = {"status": "failed"}
            if report.get("status") in {"disabled", "budget_exhausted", "complete", "no_candidates"}:
                # İş kalmadıysa ya da tavan dolduysa bir sonraki güne kadar bekle.
                await asyncio.sleep(max(3600.0, EU_TARIC_FILL_INTERVAL_SECONDS))
            else:
                await asyncio.sleep(EU_TARIC_FILL_INTERVAL_SECONDS)

    async def close(self) -> None:
        await self._http.aclose()

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            archived = connection.execute("SELECT COUNT(*) FROM lookups").fetchone()[0]
            latest = connection.execute("SELECT MAX(snapshot_month) FROM lookups").fetchone()[0]
        return {
            "enabled": bool(self.enabled and self._token),
            "configured": bool(self._token),
            "actor": self.actor,
            "archived_lookups": int(archived),
            "latest_snapshot_month": latest or None,
            "errors": self._errors[-5:],
            "fill": self.fill_plan(),
        }


__all__ = [
    "CONDITIONAL_NOTE",
    "CUSTOMS_UNION_NOTE",
    "EU_TARIC_ENABLED",
    "EU_TARIC_FILL_ENABLED",
    "EU_TARIC_FILL_INTERVAL_SECONDS",
    "EU_TARIC_FILL_LEVEL",
    "EU_TARIC_MONTHLY_BUDGET_USD",
    "EU_TARIC_UNIT_COST_USD",
    "candidate_codes",
    "month_key",
    "EuTaricEngine",
    "EuTaricResult",
    "KIND_LABELS",
    "SOURCE_NOTE",
    "classify_measure",
    "normalise_goods_code",
    "parse_measures",
    "resolve_rates",
]
