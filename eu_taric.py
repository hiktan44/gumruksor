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
                """
            )
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
        }


__all__ = [
    "CONDITIONAL_NOTE",
    "CUSTOMS_UNION_NOTE",
    "EU_TARIC_ENABLED",
    "EuTaricEngine",
    "EuTaricResult",
    "KIND_LABELS",
    "SOURCE_NOTE",
    "classify_measure",
    "normalise_goods_code",
    "parse_measures",
    "resolve_rates",
]
