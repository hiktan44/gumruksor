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

* her sorgu **kalıcı arşive** yazılır; alınan bir kod × ülke çifti ``EU_TARIC_REFRESH_DAYS``
  boyunca taze sayılır ve o süre dolmadan — hangi takvim ayında olursa olsun — yeniden
  ücretlendirilmez,
* toplu dolum ancak açıkça etkinleştirilip aylık harcama tavanı verilirse çalışır,
* jeton yalnız ortam değişkeninden okunur, günlüğe ve hata metnine asla yazılmaz.

Ölçü satırlarından türetilen özet (üçüncü ülke vergisi, menşeye özgü tercihli oran,
ek vergiler, gereken belgeler) **koşulludur**: aktörün kendi belgesinin de vurguladığı
gibi TARIC'te tek bir "nihai vergi" sayısı yoktur; oran ek koda, kotaya, belgeye ve
nihai kullanıma bağlıdır. Bu yüzden sonuç ayrı bir karşılaştırma bloğunda gösterilir ve
**hiçbir değeri Türkiye maliyet hesabına girmez.**
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
# harcama tavanı aşılmamış olmalı ve çift arşivde **taze** olmamalı.
#
# Tazelik takvim ayına değil kaydın **yaşına** bakar. Bir kez indirilen kod × ülke
# çifti kalıcıdır; yalnız EU_TARIC_REFRESH_DAYS geçtikten sonra yeniden sorgulanır.
# (Takvim ayına bakan bir kural, ayın 1'inde tüm katalogu yeniden satın almak
# demekti: kaynak o ay yeni çıkarım yayımlamamışsa aynı satır aynı anahtara
# yeniden yazılır, para gider ve tek bir yeni bilgi gelmezdi.)
EU_TARIC_FILL_ENABLED = _env_flag("EU_TARIC_FILL_ENABLED")
EU_TARIC_FILL_LEVEL = (os.environ.get("EU_TARIC_FILL_LEVEL") or "hs10").strip().lower()
EU_TARIC_FILL_ORIGINS = os.environ.get("EU_TARIC_FILL_ORIGINS") or "TR"
EU_TARIC_FILL_BATCH = max(1, min(_env_int("EU_TARIC_FILL_BATCH", 40), 500))
EU_TARIC_FILL_INTERVAL_SECONDS = max(60.0, _env_float("EU_TARIC_FILL_INTERVAL_SECONDS", 900.0))
# Varsayılan 0: tavan açıkça verilmeden hiçbir dolum sorgusu yapılmaz.
EU_TARIC_MONTHLY_BUDGET_USD = max(0.0, _env_float("EU_TARIC_MONTHLY_BUDGET_USD", 0.0))
EU_TARIC_UNIT_COST_USD = max(0.0, _env_float("EU_TARIC_UNIT_COST_USD", 0.015))
# Alınmış bir çift bu kadar gün taze sayılır; AB oranları ağırlıkla 1 Ocak'taki yıllık
# güncellemede değişir, damping/kota önlemleri yıl içinde de değişebilir.
EU_TARIC_REFRESH_DAYS = max(1, _env_int("EU_TARIC_REFRESH_DAYS", 90))
# AB'de beyana elverişli olmayan kod her turda değil, bu aralıkla yeniden yoklanır.
EU_TARIC_NOT_DECLARABLE_RETRY_DAYS = max(1, _env_int("EU_TARIC_NOT_DECLARABLE_RETRY_DAYS", 180))
# Aktörün çökmesi geçici de olabilir; beyana elverişsiz koddan daha kısa aralıkla yoklanır.
EU_TARIC_FAILED_RETRY_DAYS = max(1, _env_int("EU_TARIC_FAILED_RETRY_DAYS", 7))
# Zaman aşımından sonra küçülen grup boyutu, bu kadar hatasız turun ardından yeniden büyür.
EU_TARIC_CHUNK_RECOVER_ROUNDS = max(1, _env_int("EU_TARIC_CHUNK_RECOVER_ROUNDS", 5))
# Ardışık aktör çağrıları arasında bekleme (``UK_MEASURES_DELAY_SECONDS`` deseni): kaynağın
# eşzamanlılık/kaynak sınırlarına toptan 400 ile takılmamak için.
EU_TARIC_FILL_DELAY_SECONDS = max(0.0, _env_float("EU_TARIC_FILL_DELAY_SECONDS", 3.0))
# Es zamanli aktor cagrisi sayisi. Bugune kadar TEK bir kilit butun cagrilari siraya
# diziyordu; bir grup 400 alip kod kod yeniden denendiginde 20 kod ardi ardina calisiyor
# ve tur 20-30 dakika suruyordu (sayaclar tur bitene kadar hic kipirdamiyor). Aktor sonuc
# basina ucretlendirildigi icin es zamanlilik MALIYETI DEGISTIRMEZ, yalnizca duvar saatini
# kisaltir. 1 yazilirsa bugunku birebir sirali davranisa donulur.
EU_TARIC_CONCURRENCY = max(1, min(_env_int("EU_TARIC_CONCURRENCY", 3), 8))

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


def parse_iso_datetime(value: Any) -> datetime | None:
    """Arşivdeki ISO damgasını okur; bozuk değer yaşı hesaplanamaz sayılır."""
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    status: str  # ok | disabled | unavailable | archive_miss
    summary: dict[str, Any] = field(default_factory=dict)
    fetched_at: str | None = None
    from_archive: bool = False
    age_days: int | None = None
    stale: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goods_code": self.goods_code,
            "partner_country": self.partner_country,
            "status": self.status,
            "summary": self.summary,
            "fetched_at": self.fetched_at,
            "from_archive": self.from_archive,
            "age_days": self.age_days,
            "stale": self.stale,
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


class ActorTransportError(RuntimeError):
    """Yanıt hiç alınamadı (zaman aşımı, bağlantı kopması).

    Bu durumda aktör sunucuda çalışmaya devam edip ücreti yazmış olabilir; grup aynı turda
    **yeniden denenmez** ve bir sonraki tur daha küçük gruplarla dener.
    """


class ActorRequestError(RuntimeError):
    """Aktörün reddettiği istek; ``status_code`` 400 ise kod başına yeniden denenir."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        refresh_days: int = EU_TARIC_REFRESH_DAYS,
        not_declarable_retry_days: int = EU_TARIC_NOT_DECLARABLE_RETRY_DAYS,
        failed_retry_days: int = EU_TARIC_FAILED_RETRY_DAYS,
        fill_delay_seconds: float = EU_TARIC_FILL_DELAY_SECONDS,
        max_chunk_size: int = EU_TARIC_MAX_CODES,
        chunk_recover_rounds: int = EU_TARIC_CHUNK_RECOVER_ROUNDS,
        concurrency: int = EU_TARIC_CONCURRENCY,
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
        # Semafor (eski asyncio.Lock yerine): kapasite 1 iken davranis birebir aynidir.
        self.concurrency = max(1, min(int(concurrency or 1), 8))
        self._lock = asyncio.Semaphore(self.concurrency)
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
        self.refresh_days = max(1, int(refresh_days or 1))
        self.not_declarable_retry_days = max(1, int(not_declarable_retry_days or 1))
        self.failed_retry_days = max(1, int(failed_retry_days or 1))
        self.fill_delay_seconds = max(0.0, float(fill_delay_seconds or 0.0))
        # Uyarlanabilir grup boyutu: zaman aşımında yarılanır, hatasız turlarda geri büyür.
        self.max_chunk_size = max(1, min(int(max_chunk_size or 1), 20))
        self._chunk_size = self.max_chunk_size
        self._clean_rounds = 0
        self.chunk_recover_rounds = max(1, int(chunk_recover_rounds or 1))
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
    def _safe_detail(self, response: httpx.Response, limit: int = 200) -> str:
        """Hata gövdesinden kısa bir açıklama çıkarır; jeton geçerse maskelenir."""
        try:
            body = response.content[:2000].decode("utf-8", "replace")
        except (AttributeError, ValueError):
            return ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    body = str(error.get("message") or error.get("type") or body)
                elif error:
                    body = str(error)
        except ValueError:
            pass
        text = re.sub(r"\s+", " ", body).strip()
        if self._token:
            text = text.replace(self._token, "***")
        return text[:limit]

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
            # Kaynağın kendi hata metni tanı için gerekli; jeton yine de ayrıca temizlenir.
            detail = self._safe_detail(exc.response)
            status_code = exc.response.status_code
            raise ActorRequestError(
                f"Apify aktörü {status_code} döndürdü{(': ' + detail) if detail else '.'}",
                status_code=status_code,
            ) from None
        except httpx.HTTPError as exc:
            raise ActorTransportError(f"Apify aktörüne ulaşılamadı: {type(exc).__name__}") from None
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
    def _apply_archive(self, result: "EuTaricResult", archived: dict[str, Any]) -> None:
        """Arşiv kaydını sonuca yazar ve yaşını bildirir (bayat kayıt gizlenmez, işaretlenir)."""
        result.status = "ok"
        result.summary = archived["summary"]
        result.fetched_at = archived["fetched_at"]
        result.from_archive = True
        fetched = parse_iso_datetime(archived["fetched_at"])
        if fetched is not None:
            age = (datetime.now(UTC) - fetched).days
            result.age_days = max(0, age)
            if age > self.refresh_days:
                result.stale = True
                result.warnings.append(
                    f"Bu özet {archived['fetched_at'][:10]} tarihinde alındı ({result.age_days} gün önce); "
                    "AB verisi o tarihten sonra değişmiş olabilir."
                )

    async def lookup(
        self, gtip: str, *, origin: str = "TR", refresh: bool = False, archive_only: bool = False
    ) -> EuTaricResult:
        """``archive_only=True`` ücretli aktörü ASLA çalıştırmaz; yalnız yerel arşivi okur.

        Ön değerlendirme yolu bunu kullanır: ``/api/customs/precheck`` dakikada 20 istekle
        açıktır ve aktörü oradan tetiklemek TARIC bütçesini sınırsız hâle getirirdi. Arşivde
        satır yoksa ``status="archive_miss"`` döner ve çağıran dürüst bir kademe düşürmesi yapar.
        """
        code = normalise_goods_code(gtip)
        partner = (str(origin or "TR").strip().upper() or "TR")[:4]
        if len(_digits(gtip)) < 6:
            raise ValueError("AB TARIC sorgusu için en az 6 haneli bir kod gerekir.")
        result = EuTaricResult(goods_code=code, partner_country=partner, status="unavailable")
        if not refresh:
            archived = self.archived(code, partner)
            if archived is not None:
                self._apply_archive(result, archived)
                return result
        if archive_only:
            result.status = "archive_miss"
            result.warnings.append(
                "Bu kod arşivde yok. Ücretli canlı sorgu ön değerlendirme akışından tetiklenmez; "
                "gerekirse yurt dışı tarife aracından ayrıca çalıştırılabilir."
            )
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
                    self._apply_archive(result, archived)
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

    def _cutoff(self, days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=int(days))).isoformat(timespec="seconds")

    def _recent_attempts(self) -> set[tuple[str, str]]:
        """Son denemesi hâlâ geçerli sayılan çiftler; takvim ayı değil, denemenin yaşı belirler.

        ``ok`` satırı ``refresh_days``, ``not_declarable`` satırı ``not_declarable_retry_days``
        boyunca çifti kuyruk dışında tutar. Başarısız tur zaten hiç kaydedilmez.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT goods_code, partner_country, status, MAX(attempted_at) AS attempted_at "
                "FROM fill_attempts GROUP BY goods_code, partner_country",
            ).fetchall()
        cutoffs = {
            "not_declarable": self._cutoff(self.not_declarable_retry_days),
            "actor_failed": self._cutoff(self.failed_retry_days),
        }
        fresh_cutoff = self._cutoff(self.refresh_days)
        pairs: set[tuple[str, str]] = set()
        for row in rows:
            attempted = str(row["attempted_at"] or "")
            if attempted >= cutoffs.get(row["status"], fresh_cutoff):
                pairs.add((row["goods_code"], row["partner_country"]))
        return pairs

    def attempt_counts(self) -> dict[str, int]:
        """Cift basina SON denemenin durum dokumu (ucret dogurmaz).

        ``ok`` alindi · ``not_declarable`` AB'de karsiligi yok · ``actor_failed`` aktor
        o kodda coktu. Grup halinde sorgulamanin hâlâ ise yarayip yaramadigi bu orana
        bakilarak karara baglanir; tahmin edilmez.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM ("
                "  SELECT goods_code, partner_country, status, MAX(attempted_at)"
                "  FROM fill_attempts GROUP BY goods_code, partner_country"
                ") GROUP BY status",
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def _fresh_pairs(self) -> set[tuple[str, str]]:
        """Arşivde tazeliği sürenler: hangi ay alınmış olursa olsun yeniden ücret ödenmez."""
        cutoff = self._cutoff(self.refresh_days)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT goods_code, partner_country FROM lookups "
                "GROUP BY goods_code, partner_country HAVING MAX(fetched_at) >= ?",
                (cutoff,),
            ).fetchall()
        return {(row["goods_code"], row["partner_country"]) for row in rows}

    def _stored_pairs(self) -> set[tuple[str, str]]:
        """Yaşı ne olursa olsun bir kez alınmış çiftler (hiç alınmamışları ayırmak için)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT goods_code, partner_country FROM lookups"
            ).fetchall()
        return {(row["goods_code"], row["partner_country"]) for row in rows}

    def _queue(self, codes: list[str], limit: int | None = None) -> tuple[list[tuple[str, str]], int, int, int]:
        """Sıradaki çiftler: önce hiç alınmamışlar, sonra tazeliği geçenler (``foreign_tariff`` deseni).

        Döner: (kuyruk, hiç alınmamış sayısı, tazelemesi gelen sayısı, atlanan taze sayısı).
        """
        held = self._recent_attempts() | self._fresh_pairs()
        stored = self._stored_pairs()
        never: list[tuple[str, str]] = []
        due: list[tuple[str, str]] = []
        skipped = 0
        for code in codes:
            for origin in self.fill_origins:
                pair = (code, origin)
                if pair in held:
                    skipped += 1
                    continue
                (due if pair in stored else never).append(pair)
        # Kuyruk katalog sirasinda gezilirse tek bir kotu fasil (orn. 04 peynir kodlari)
        # arkasindaki her seyi kilitler. Kodun kararli ozetine gore siralamak isi butun
        # kataloğa yayar: bozuk bir bolge yalniz kendi payi kadar yavaslatir ve arsiv
        # bastan itibaren genis kapsamli olur. Rastgelelik yok, tohum sabit.
        never.sort(key=lambda pair: hashlib.blake2s(pair[0].encode("ascii"), digest_size=8).digest())
        queue = never + due
        if limit is not None:
            queue = queue[: max(0, int(limit))]
        return queue, len(never), len(due), skipped

    def _shrink_chunk(self, attempted: int) -> None:
        """Zaman aşımı: grup boyutunu yarıya indir (1'in altına inmez)."""
        if attempted <= 1:
            return
        reduced = max(1, min(self._chunk_size, attempted) // 2)
        if reduced < self._chunk_size:
            self._chunk_size = reduced
            self._fill_errors.append(
                f"{_now()}: Zaman aşımı sonrası grup boyutu {reduced} koda indirildi."
            )
        self._clean_rounds = 0

    def _note_clean_round(self) -> None:
        """Hatasız tur: yeterince biriktiyse grup boyutunu kademeli geri büyüt."""
        if self._chunk_size >= self.max_chunk_size:
            self._clean_rounds = 0
            return
        self._clean_rounds += 1
        if self._clean_rounds >= self.chunk_recover_rounds:
            self._chunk_size = min(self.max_chunk_size, self._chunk_size * 2)
            self._clean_rounds = 0

    async def _run_chunk(
        self, chunk: list[str], origin: str
    ) -> tuple[list[dict[str, Any]], dict[str, str], set[str]]:
        """Bir grubu çalıştırır; aktör grubu 400 ile reddederse kodları tek tek dener.

        Döner: (sonuçlar, **kendi başına** çalıştırılıp başarısız olan kodlar → sebep,
        geçici olarak düşen kodlar). Ayrım önemlidir: kendi başına başarısız olan kod
        kaydedilir ve bir süre yeniden denenmez (aktör bazı kodlarda çöküyor); geçici
        hata ise hiç kaydedilmez, bir sonraki turda yeniden denenir.
        """
        try:
            async with self._lock:
                items = await self._run_actor(chunk, origin)
            await asyncio.sleep(self.fill_delay_seconds)
            return items, {}, set()
        except ActorRequestError as exc:
            self._fill_errors.append(f"{_now()}: {exc}")
            if exc.status_code != 400:
                # 500 ve benzeri geçicidir: kaydedilmez, sonraki turda yeniden denenir.
                return [], {}, set(chunk)
            if len(chunk) == 1:
                # Tek kodluk çağrı 400 aldı: kodun kendisi sorunlu, kayda geçer.
                return [], {chunk[0]: str(exc)}, set()
        except ActorTransportError as exc:
            self._fill_errors.append(f"{_now()}: {exc}")
            self._shrink_chunk(len(chunk))
            return [], {}, set(chunk)
        except (SecurityViolation, RuntimeError, ValueError) as exc:
            self._fill_errors.append(f"{_now()}: {str(exc)[:200]}")
            return [], {}, set(chunk)
        # Kod kod kurtarma ES ZAMANLI calisir. Sirali hâlinde 20 kodluk bir grup
        # 20-30 dakika suruyordu ve tur bitene kadar hicbir sey kaydedilmiyordu; kuyruk
        # bir fasila takilinca butun dolum duruyordu. Es zamanlilik ucreti degistirmez.
        results = await asyncio.gather(
            *(self._rescue_one(code, origin) for code in chunk), return_exceptions=False
        )
        items: list[dict[str, Any]] = []
        broken: dict[str, str] = {}
        transient: set[str] = set()
        for code, found, reason, is_broken in results:
            items.extend(found)
            if reason is None:
                continue
            if is_broken:
                broken[code] = reason
            else:
                transient.add(code)
        return items, broken, transient

    async def _rescue_one(
        self, code: str, origin: str
    ) -> tuple[str, list[dict[str, Any]], str | None, bool]:
        """Tek kodu kendi basina dener. Döner: (kod, sonuçlar, hata sebebi | None, kalıcı mı).

        ``400`` kodun kendisinin sorunlu oldugunu gosterir ve kayda gecer; digerleri
        gecicidir ve hic kaydedilmez, bir sonraki turda yeniden denenir.
        """
        try:
            # Gecikme SEMAFORUN ICINDE: disarida olsaydi butun kurtarma coroutine'leri
            # ayni anda uyanip sirayla kilitsiz ard arda cagri yapardi ve kaynagi koruyan
            # 3 saniyelik aralik tamamen kaybolurdu. Burada her slot kendi cagrisindan
            # once bekler, es zamanlilik yine semaforun kapasitesiyle sinirli kalir.
            async with self._lock:
                await asyncio.sleep(self.fill_delay_seconds)
                return code, await self._run_actor([code], origin), None, False
        except (SecurityViolation, RuntimeError, ValueError) as exc:
            message = str(exc)[:160]
            self._fill_errors.append(f"{_now()}: {code}: {message}")
            permanent = isinstance(exc, ActorRequestError) and exc.status_code == 400
            return code, [], message, permanent

    def fill_plan(self) -> dict[str, Any]:
        """Dolumun mevcut durumu: aday sayısı, kalan iş ve tahmini maliyet (ücret doğurmaz)."""
        period = month_key()
        codes = self.candidates()
        origins = self.fill_origins
        total = len(codes) * len(origins)
        _, never, due, _ = self._queue(codes)
        # Katalog dolduktan sonraki yinelenen maliyet: her çift refresh_days'te bir tazelenir.
        monthly = (total * self.unit_cost_usd * 30.0 / self.refresh_days) if self.refresh_days else 0.0
        return {
            "enabled": bool(self.fill_enabled and self.enabled and self._token and self.monthly_budget_usd > 0),
            "level": self.fill_level,
            "origins": list(origins),
            "candidate_codes": len(codes),
            "total_pairs": total,
            "completed_pairs": total - never - due,
            "pending_pairs": never,
            "refresh_due_pairs": due,
            "refresh_days": self.refresh_days,
            "not_declarable_retry_days": self.not_declarable_retry_days,
            "failed_retry_days": self.failed_retry_days,
            "batch": self.fill_batch,
            "concurrency": self.concurrency,
            "attempts": self.attempt_counts(),
            "chunk_size": self._chunk_size,
            "max_chunk_size": self.max_chunk_size,
            "estimated_total_usd": round(total * self.unit_cost_usd, 2),
            "estimated_pending_usd": round((never + due) * self.unit_cost_usd, 2),
            "estimated_monthly_usd": round(monthly, 2),
            "spend": self.spend_status(period),
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
            budget = min(int(limit or self.fill_batch), spend["remaining_lookups"])
            pending, _, _, skipped = await asyncio.to_thread(self._queue, codes, budget)
            if not pending:
                return {"status": "complete", "period": period, "requested": 0, "fetched": 0, "charged": 0, "skipped": skipped}

            fetched = 0
            charged = 0
            missing = 0
            failed = 0
            by_origin: dict[str, list[str]] = {}
            for code, origin in pending:
                by_origin.setdefault(origin, []).append(code)
            for origin, origin_codes in by_origin.items():
                start = 0
                while start < len(origin_codes):
                    # Grup boyutu her adımda yeniden okunur: zaman aşımı olduysa küçülmüştür.
                    size = max(1, self._chunk_size)
                    chunk = origin_codes[start : start + size]
                    start += len(chunk)
                    items, broken, transient = await self._run_chunk(chunk, origin)
                    if broken:
                        # Kod kendi başına denendi ve yine başarısız: kaydedilir ki her turda
                        # yeniden denenip kuyruğu tıkamasın (aktör bazı kodlarda çöküyor).
                        failed += len(broken)
                        await asyncio.to_thread(
                            self._record_attempts,
                            period,
                            [(code, origin, "actor_failed", reason) for code, reason in broken.items()],
                        )
                    if transient:
                        # Geçici hata: kaydedilmez, bir sonraki turda yeniden denenir.
                        failed += len(transient)
                    skip = set(broken) | transient
                    chunk = [code for code in chunk if code not in skip]
                    if not chunk:
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
            if not failed:
                self._note_clean_round()
            return {
                "status": "ok" if fetched or missing else "failed",
                "period": period,
                "requested": len(pending),
                "fetched": fetched,
                "charged": charged,
                "not_declarable": missing,
                "failed": failed,
                "skipped": skipped,
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
    "EU_TARIC_NOT_DECLARABLE_RETRY_DAYS",
    "EU_TARIC_CHUNK_RECOVER_ROUNDS",
    "EU_TARIC_FAILED_RETRY_DAYS",
    "EU_TARIC_REFRESH_DAYS",
    "EU_TARIC_UNIT_COST_USD",
    "candidate_codes",
    "month_key",
    "parse_iso_datetime",
    "EuTaricEngine",
    "EuTaricResult",
    "ActorRequestError",
    "ActorTransportError",
    "KIND_LABELS",
    "SOURCE_NOTE",
    "classify_measure",
    "normalise_goods_code",
    "parse_measures",
    "resolve_rates",
]
