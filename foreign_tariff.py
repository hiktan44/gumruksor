"""Yurt dışı tarife karşılaştırma: Birleşik Krallık açık API'si, AB ve İsviçre resmî sorgu bağlantıları.

Üç yargı alanı verisini aynı biçimde yayımlamıyor; bu modül farkı gizlemek yerine açıkça
taşır:

* **Birleşik Krallık** — ``trade-tariff.service.gov.uk`` JSON:API uçları anahtarsız ve
  makine okunur. Fasıl listesi arka planda eşitlenir; pozisyon ve emtia gövdeleri talep
  anında çekilip önbelleğe alınır. Üçüncü ülke vergisi, tercihli oranlar, kota, damping ve
  yasaklar resmî ölçü satırlarından ayrıştırılır.
* **Avrupa Birliği (TARIC/EBTI)** ve **İsviçre (Tares)** — resmî açık uç nokta
  yayımlanmıyor (TARIC danışma ekranı oturum/POST ile çalışır, Tares giriş kontrolüne
  yönlendirir). Bu iki yargı alanı için oran **çekilmez**; yalnız sorguyu resmî ekranda
  hazır açan doğrulanmış derin bağlantılar üretilir.

Kesin kural: buradan dönen hiçbir yabancı oran Türkiye maliyet hesabına girmez.
``tariff_engine.calculate_landed_cost`` yalnız Türk resmî anlık görüntüleriyle çalışır;
yabancı veriler ayrı bir karşılaştırma bloğunda, kendi kaynak künyesiyle gösterilir.

Eşleşme HS-6 düzeyindedir: Türk 12 haneli GTİP'inin ilk 6 hanesi Dünya Gümrük Örgütü
armonize sistemiyle ortaktır, sonraki haneler ulusaldır ve eşleştirilmez.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin

import httpx

from change_ledger import batch_id_for, diff_rows
from countries import find_country
from review_policy import (
    DiffSummary,
    ReviewPolicy,
    decide,
    ensure_review_columns,
    review_metadata,
    row_review_fields,
)
from security_firewall import SecurityViolation, validate_outbound_url
from temporal import ensure_validity_columns, today_iso

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
SEED_DIR = ROOT / "data" / "official"
LINKS_SEED = "foreign_tariff_links.json"
USER_AGENT = "Mozilla/5.0 (compatible; MevzuatMCP/1.5; +https://gumruksor.com/)"

UK_BASE_URL = (os.environ.get("UK_TARIFF_BASE_URL") or "https://www.trade-tariff.service.gov.uk/api/v2").rstrip("/")
UK_SITE_URL = "https://www.trade-tariff.service.gov.uk"
_OFFICIAL_HOSTS = frozenset({"trade-tariff.service.gov.uk"})
# İsviçre BAZG açık veri dosyası: tarife numarası yapısı (kod + DE/EN/FR tanım + geçerlilik).
CH_NOMENCLATURE_URL = (
    os.environ.get("CH_TARIFF_DATA_URL")
    or "https://ocean.bazg.admin.ch/open-data-reports/TN_STRUCTURE_v1/TN_STRUCTURE_v1.csv"
)
CH_SOURCE_PAGE = "https://www.bazg.admin.ch/en"
_CH_HOSTS = frozenset({"admin.ch"})
CH_SYNC_ENABLED = (os.environ.get("CH_TARIFF_SYNC_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
_MAX_BYTES = 8 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_REDIRECTS = 5

DEFAULT_SYNC_INTERVAL = max(3600, int(os.environ.get("FOREIGN_TARIFF_SYNC_SECONDS") or 86400))
DEFAULT_CACHE_DAYS = max(1, int(os.environ.get("FOREIGN_TARIFF_CACHE_DAYS") or 7))
SYNC_ENABLED = (os.environ.get("FOREIGN_TARIFF_SYNC_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}

JURISDICTIONS: tuple[str, ...] = ("uk", "eu", "ch")
JURISDICTION_LABELS = {"uk": "Birleşik Krallık", "eu": "Avrupa Birliği", "ch": "İsviçre"}
UK_DATASET = "uk_chapters"
CH_DATASET = "ch_nomenclature"

COMPARABILITY_NOTE = (
    "Birleşik Krallık 10 haneli tarife kodu Türk 12 haneli GTİP'ine birebir denk değildir; "
    "eşleşme HS-6 düzeyindedir ve karşılaştırma yalnızca bilgilendirme amaçlıdır."
)
NO_CALCULATION_NOTE = (
    "Yurt dışı oranlar Türkiye ithalat maliyeti hesabına aktarılmaz; yalnız karşılaştırma için gösterilir."
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def origin_iso2(value: Any) -> str | None:
    """Kullanıcının yazdığı ülke adını (TR/EN) ISO-3166 alfa-2 koduna çevirir."""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 2 and text.isalpha():
        return text.upper()
    country = find_country(text)
    return country.iso2.upper() if country and country.iso2 else None


# --------------------------------------------------------------------------- pure parsers

_MEASURE_KINDS: tuple[tuple[str, str], ...] = (
    ("third country duty", "third_country_duty"),
    ("tariff preference", "preference"),
    ("customs union duty", "customs_union_duty"),
    ("suspension", "suspension"),
    ("quota", "quota"),
    ("anti-dumping", "anti_dumping"),
    ("antidumping", "anti_dumping"),
    ("countervailing", "countervailing"),
    ("safeguard", "safeguard"),
    ("prohibition", "prohibition"),
    ("restriction", "restriction"),
    ("import control", "restriction"),
    ("value added tax", "vat"),
    ("excise", "excise"),
    ("supplementary unit", "supplementary_unit"),
    ("unit of quantity", "supplementary_unit"),
)

MEASURE_KIND_LABELS = {
    "third_country_duty": "Üçüncü ülke gümrük vergisi",
    "preference": "Tercihli tarife",
    "customs_union_duty": "Gümrük birliği vergisi",
    "suspension": "Vergi askıya alma",
    "quota": "Tarife kontenjanı",
    "anti_dumping": "Damping önlemi",
    "countervailing": "Telafi edici vergi",
    "safeguard": "Korunma önlemi",
    "prohibition": "İthalat yasağı",
    "restriction": "İthalat kontrolü / kısıtlama",
    "vat": "KDV",
    "excise": "Özel tüketim",
    "supplementary_unit": "Tamamlayıcı ölçü birimi",
    "other": "Diğer önlem",
}

# Karşılaştırma tablosunda gösterilmeyen, bilgi değeri düşük satırlar.
_QUIET_KINDS = frozenset({"supplementary_unit"})


def classify_measure(description: Any) -> str:
    text = str(description or "").lower()
    for needle, kind in _MEASURE_KINDS:
        if needle in text:
            return kind
    return "other"


def _index_included(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("type")), str(item.get("id"))): item
        for item in payload.get("included") or []
        if isinstance(item, dict)
    }


def _related_id(relationships: dict[str, Any], name: str) -> str | None:
    data = (relationships.get(name) or {}).get("data")
    if isinstance(data, dict) and data.get("id") is not None:
        return str(data["id"])
    return None


def _related_ids(relationships: dict[str, Any], name: str) -> list[str]:
    data = (relationships.get(name) or {}).get("data")
    if not isinstance(data, list):
        return []
    return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id") is not None]


def parse_chapters(payload: dict[str, Any]) -> list[dict[str, str]]:
    """``/chapters`` gövdesinden fasıl kodu + tanımı."""
    rows: list[dict[str, str]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes") or {}
        code = digits_only(attributes.get("goods_nomenclature_item_id"))[:2]
        description = str(attributes.get("formatted_description") or attributes.get("description") or "").strip()
        if not code or not description:
            continue
        rows.append({"code": code, "description": _plain(description)})
    rows.sort(key=lambda row: row["code"])
    return rows


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(value: Any) -> str:
    """Resmî tanımlar HTML işaretlemesi içerebilir; düz metne indirger."""
    text = _TAG_RE.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_heading(payload: dict[str, Any]) -> dict[str, Any]:
    """``/headings/{4 hane}`` gövdesinden pozisyon tanımı ve altındaki emtia satırları."""
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}
    included = _index_included(payload)
    commodities: list[dict[str, Any]] = []
    for key, item in included.items():
        if key[0] != "commodity":
            continue
        item_attributes = item.get("attributes") or {}
        code = digits_only(item_attributes.get("goods_nomenclature_item_id"))
        if len(code) != 10:
            continue
        commodities.append(
            {
                "code": code,
                "description": _plain(item_attributes.get("formatted_description") or item_attributes.get("description")),
                "leaf": bool(item_attributes.get("leaf")),
                "declarable": bool(item_attributes.get("declarable", item_attributes.get("leaf"))),
                "indents": int(item_attributes.get("number_indents") or 0),
                "suffix": str(item_attributes.get("producline_suffix") or ""),
            }
        )
    commodities.sort(key=lambda row: (row["code"], row["suffix"]))
    return {
        "heading": digits_only(attributes.get("goods_nomenclature_item_id"))[:4],
        "description": _plain(attributes.get("formatted_description") or attributes.get("description")),
        "commodities": commodities,
    }


def parse_commodity(payload: dict[str, Any]) -> dict[str, Any]:
    """``/commodities/{10 hane}`` gövdesinden tanım, ölçü satırları ve coğrafi grup üyelikleri."""
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}
    included = _index_included(payload)

    geo_children: dict[str, list[str]] = {}
    geo_names: dict[str, str] = {}
    for (kind, identifier), item in included.items():
        if kind != "geographical_area":
            continue
        geo_names[identifier] = _plain((item.get("attributes") or {}).get("description"))
        children = _related_ids(item.get("relationships") or {}, "children_geographical_areas")
        if children:
            geo_children[identifier] = children

    measures: list[dict[str, Any]] = []
    for (kind, identifier), item in included.items():
        if kind != "measure":
            continue
        measure_attributes = item.get("attributes") or {}
        relationships = item.get("relationships") or {}
        type_id = _related_id(relationships, "measure_type")
        type_row = included.get(("measure_type", type_id or ""), {})
        type_description = _plain((type_row.get("attributes") or {}).get("description"))
        duty_id = _related_id(relationships, "duty_expression")
        duty_row = included.get(("duty_expression", duty_id or ""), {})
        duty_attributes = duty_row.get("attributes") or {}
        area_id = _related_id(relationships, "geographical_area")
        order_id = _related_id(relationships, "order_number")
        measures.append(
            {
                "measure_id": identifier,
                "measure_type_id": type_id,
                "measure_type": type_description,
                "kind": classify_measure(type_description),
                "duty_expression": _plain(duty_attributes.get("verbose_duty") or duty_attributes.get("base")),
                "geographical_area_id": area_id,
                "geographical_area": geo_names.get(area_id or "", ""),
                "excluded_countries": _related_ids(relationships, "excluded_countries"),
                "order_number": order_id,
                "import": bool(measure_attributes.get("import", True)),
                "effective_start": str(measure_attributes.get("effective_start_date") or "")[:10] or None,
                "effective_end": str(measure_attributes.get("effective_end_date") or "")[:10] or None,
            }
        )
    measures.sort(key=lambda row: (row["kind"], row.get("geographical_area_id") or "", row["measure_id"]))
    return {
        "code": digits_only(attributes.get("goods_nomenclature_item_id")),
        "description": _plain(attributes.get("formatted_description") or attributes.get("description")),
        "bti_url": str(attributes.get("bti_url") or "") or None,
        "validity_start": str(attributes.get("validity_start_date") or "")[:10] or None,
        "validity_end": str(attributes.get("validity_end_date") or "")[:10] or None,
        "declarable": bool(attributes.get("declarable", True)),
        "measures": measures,
        "geo_children": geo_children,
    }


def measure_applies(measure: dict[str, Any], iso2: str | None, geo_children: dict[str, list[str]]) -> bool:
    """Ölçü satırı verilen menşe ülkesini kapsıyor mu (grup üyeliği ve istisnalar dâhil)?"""
    if not iso2:
        return False
    if iso2 in (measure.get("excluded_countries") or []):
        return False
    area = measure.get("geographical_area_id")
    if not area:
        return False
    if area == iso2:
        return True
    return iso2 in (geo_children.get(area) or [])


def summarise_commodity(parsed: dict[str, Any], iso2: str | None) -> dict[str, Any]:
    """Ölçü satırlarından üçüncü ülke vergisi, menşeye özgü tercih ve diğer önlemleri ayırır."""
    geo_children = parsed.get("geo_children") or {}
    measures = [row for row in parsed.get("measures") or [] if row.get("import")]
    third_country = next(
        (row for row in measures if row["kind"] == "third_country_duty" and row.get("geographical_area_id") == "1011"),
        None,
    )
    if third_country is None:
        third_country = next((row for row in measures if row["kind"] == "third_country_duty"), None)
    origin_measures = [row for row in measures if measure_applies(row, iso2, geo_children)]
    preference = next((row for row in origin_measures if row["kind"] == "preference"), None)
    notable = [
        row
        for row in (origin_measures or measures)
        if row["kind"] not in _QUIET_KINDS and row["kind"] != "third_country_duty"
    ]
    return {
        "third_country_duty": third_country["duty_expression"] if third_country else None,
        "third_country_measure": third_country,
        "origin_preference": preference,
        "origin_measures": origin_measures,
        "measures": notable[:40],
    }


def parse_swiss_nomenclature(text: str, *, limit: int = 40_000) -> list[dict[str, Any]]:
    """BAZG ``TN_STRUCTURE`` dosyasından İsviçre tarife numaralarını (8 hane) çıkarır.

    Dosya noktalı virgülle ayrılmıştır ve her satır fasıl → pozisyon → HS6 → ulusal 8 hane
    zincirini tanımlarıyla birlikte taşır. Tanımlar Almanca, İngilizce ve Fransızcadır;
    ürün İngilizceyi esas alır, yoksa Almancaya düşer.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    names = {str(name or "").strip() for name in (reader.fieldnames or [])}
    if "tn8" not in names or "tn6" not in names:
        raise ValueError("İsviçre tarife dosyası beklenen sütunları taşımıyor.")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in reader:
        code = digits_only(raw.get("tn8"))
        if len(code) != 8 or code in seen:
            continue
        seen.add(code)
        description = _plain(raw.get("tn8_txt_e") or raw.get("tn8_txt_d"))
        if not description:
            continue
        rows.append(
            {
                "code": code,
                "hs6": digits_only(raw.get("tn6"))[:6] or code[:6],
                "description": description[:600],
                "description_alt": _plain(raw.get("tn8_txt_d"))[:600],
                "heading_description": _plain(raw.get("tn4_txt_e") or raw.get("tn4_txt_d"))[:600],
                "valid_from": str(raw.get("tn8_validfrom") or "")[:10] or None,
                "valid_to": str(raw.get("tn8_validto") or "")[:10] or None,
            }
        )
        if len(rows) >= limit:
            break
    if not rows:
        raise ValueError("İsviçre tarife dosyasında satır bulunamadı.")
    rows.sort(key=lambda item: item["code"])
    return rows


# --------------------------------------------------------------------------- resmî bağlantı kataloğu

_LINK_FIELDS = ("code12", "code10", "code8", "hs6", "heading", "chapter", "area", "date_compact", "date_iso")


def _link_values(gtip: str, iso2: str | None, as_of: str | None) -> dict[str, str]:
    code = digits_only(gtip)
    day = (as_of or today_iso())[:10]
    return {
        "code12": code[:12],
        "code10": (code[:10] + "0000000000")[:10] if code else "",
        "code8": (code[:8] + "00000000")[:8] if code else "",
        "hs6": code[:6],
        "heading": code[:4],
        "chapter": code[:2],
        "area": iso2 or "",
        "date_compact": day.replace("-", ""),
        "date_iso": day,
    }


def load_link_catalog(seed_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """``data/official/foreign_tariff_links.json`` tohumunu okur; bozuk dosyada boş katalog."""
    path = Path(seed_dir or SEED_DIR) / LINKS_SEED
    if not path.exists():
        logger.warning("Yurt dışı tarife bağlantı kataloğu bulunamadı: %s", path)
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("Yurt dışı tarife bağlantı kataloğu okunamadı: %s", exc)
        return {}
    catalog: dict[str, dict[str, Any]] = {}
    for record in payload.get("jurisdictions") or []:
        code = str(record.get("code") or "").strip().lower()
        if code in JURISDICTIONS:
            catalog[code] = record
    return catalog


def build_links(record: dict[str, Any], gtip: str, iso2: str | None, as_of: str | None) -> list[dict[str, str]]:
    """Katalog şablonlarından sorguyu hazır açan resmî bağlantılar üretir."""
    values = _link_values(gtip, iso2, as_of)
    links: list[dict[str, str]] = []
    for entry in record.get("links") or []:
        template = str(entry.get("url") or "")
        if not template:
            continue
        required = [str(item) for item in entry.get("requires") or []]
        if any(not values.get(name) for name in required):
            continue
        try:
            url = template.format(**{name: quote(values[name], safe="") for name in _LINK_FIELDS})
        except (KeyError, IndexError, ValueError):
            logger.warning("Geçersiz bağlantı şablonu atlandı: %s", entry.get("id"))
            continue
        try:
            validate_outbound_url(url, allowed_hosts=entry.get("hosts") or record.get("hosts") or [])
        except SecurityViolation:
            logger.warning("Bağlantı şablonu güvenlik listesinde değil, atlandı: %s", entry.get("id"))
            continue
        links.append(
            {
                "id": str(entry.get("id") or ""),
                "title": str(entry.get("title") or ""),
                "url": url,
                "authority": str(entry.get("authority") or record.get("authority") or ""),
                "note": str(entry.get("note") or ""),
            }
        )
    return links


# --------------------------------------------------------------------------- depo

class ForeignTariffStore:
    """Anlık görüntüler, nomenklatür satırları ve talep anında çekilen gövdelerin önbelleği."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:  # paylaşılan birimlerde izin değiştirilemeyebilir
            pass
        self.db_path = root / "foreign_tariff.sqlite3"
        self._initialise()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    jurisdiction TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 0,
                    valid_from TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_foreign_snapshots_dataset ON snapshots(dataset, retrieved_at);
                CREATE TABLE IF NOT EXISTS nomenclature (
                    snapshot_id TEXT NOT NULL,
                    jurisdiction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    code TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    description_alt TEXT NOT NULL DEFAULT '',
                    valid_from TEXT,
                    valid_to TEXT,
                    PRIMARY KEY (snapshot_id, kind, code)
                );
                CREATE INDEX IF NOT EXISTS idx_foreign_nomenclature ON nomenclature(jurisdiction, kind, code);
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            ensure_review_columns(connection, "snapshots")
            ensure_validity_columns(connection, "snapshots")
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    # ---- önbellek
    def cached(self, cache_key: str, *, max_age_days: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row is None:
            return None
        try:
            fetched = datetime.fromisoformat(str(row["fetched_at"]))
        except ValueError:
            return None
        if datetime.now(UTC) - fetched > timedelta(days=max_age_days):
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            return None
        return {"payload": payload, "sha256": row["sha256"], "source_url": row["source_url"], "fetched_at": row["fetched_at"]}

    def store_cache(self, cache_key: str, payload: Any, *, source_url: str) -> dict[str, Any]:
        digest = _sha256(payload)
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO cache(cache_key,payload,sha256,source_url,fetched_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256, "
                "source_url=excluded.source_url, fetched_at=excluded.fetched_at",
                (cache_key, json.dumps(payload, ensure_ascii=False), digest, source_url, now),
            )
        return {"payload": payload, "sha256": digest, "source_url": source_url, "fetched_at": now}

    def purge_cache(self, *, max_age_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days * 4)).isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM cache WHERE fetched_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    # ---- anlık görüntüler
    def snapshot_by_sha(self, dataset: str, sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM snapshots WHERE dataset=? AND sha256=?", (dataset, sha256)).fetchone()

    def active_snapshot(self, dataset: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM snapshots WHERE dataset=? AND active=1 ORDER BY retrieved_at DESC LIMIT 1", (dataset,)
            ).fetchone()

    def latest_approved(self, dataset: str, *, exclude: str | None = None) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM snapshots WHERE dataset=? AND status='approved' AND id<>? ORDER BY retrieved_at DESC LIMIT 1",
                (dataset, exclude or ""),
            ).fetchone()

    def rows_of(self, snapshot_id: str, kind: str) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT code, description FROM nomenclature WHERE snapshot_id=? AND kind=?", (snapshot_id, kind)
            ).fetchall()
        return {row["code"]: {"code": row["code"], "description": row["description"]} for row in rows}

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None


# --------------------------------------------------------------------------- sonuç modelleri

@dataclass
class JurisdictionResult:
    jurisdiction: str
    label: str
    data_kind: str  # api | links
    authority: str = ""
    matched_code: str | None = None
    description: str | None = None
    match_quality: str = "not_found"  # exact_hs6 | heading_only | not_found | unavailable
    third_country_duty: str | None = None
    origin_preference: dict[str, Any] | None = None
    measures: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, str]] = field(default_factory=list)
    bti_url: str | None = None
    source_url: str | None = None
    sha256: str | None = None
    retrieved_at: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "jurisdiction": self.jurisdiction,
            "label": self.label,
            "data_kind": self.data_kind,
            "authority": self.authority,
            "matched_code": self.matched_code,
            "description": self.description,
            "match_quality": self.match_quality,
            "third_country_duty": self.third_country_duty,
            "origin_preference": self.origin_preference,
            "measures": self.measures,
            "candidates": self.candidates,
            "bti_url": self.bti_url,
            "source_url": self.source_url,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
            "links": self.links,
            "notes": self.notes,
        }


@dataclass
class ForeignTariffLookup:
    gtip: str
    hs6: str
    origin_country: str | None
    origin_code: str | None
    as_of: str | None
    results: list[JurisdictionResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gtip": self.gtip,
            "hs6": self.hs6,
            "origin_country": self.origin_country,
            "origin_code": self.origin_code,
            "as_of": self.as_of,
            "results": [item.as_dict() for item in self.results],
            "warnings": self.warnings,
            "comparability_note": COMPARABILITY_NOTE,
            "calculation_note": NO_CALCULATION_NOTE,
        }


# --------------------------------------------------------------------------- motor

class ForeignTariffEngine:
    """UK açık API'si + AB/İsviçre resmî sorgu bağlantıları."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        review_policy: ReviewPolicy | None = None,
        ledger: Any = None,
        seed_dir: str | Path | None = None,
        base_url: str = UK_BASE_URL,
        sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL,
        cache_days: int = DEFAULT_CACHE_DAYS,
    ) -> None:
        self.store = ForeignTariffStore(data_dir)
        self.base_url = base_url.rstrip("/")
        self.review_policy = review_policy or ReviewPolicy()
        self.ledger = ledger
        self.link_catalog = load_link_catalog(seed_dir)
        self.sync_interval_seconds = sync_interval_seconds
        self.cache_days = cache_days
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )
        self._sync_lock = asyncio.Lock()
        self._syncing = False
        self._errors: list[str] = []

    # ---- HTTP
    async def _get_json(self, path: str) -> tuple[dict[str, Any], str]:
        current = f"{self.base_url}{path}"
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_OFFICIAL_HOSTS)
            response: httpx.Response | None = None
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await self._http.get(current)
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if response is None:
                raise last_error or RuntimeError("Yurt dışı tarife kaynağı yanıt vermedi.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("Yurt dışı tarife kaynağı hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            content = response.content
            if len(content) > _MAX_BYTES:
                raise ValueError("Yurt dışı tarife yanıtı beklenenden büyük.")
            media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if "json" not in media_type:
                raise ValueError("Yurt dışı tarife kaynağı JSON döndürmedi.")
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("Yurt dışı tarife yanıtı çözümlenemedi.") from exc
            if not isinstance(payload, dict):
                raise ValueError("Yurt dışı tarife yanıtı beklenen biçimde değil.")
            return payload, current
        raise ValueError("Yurt dışı tarife kaynağı çok fazla yönlendirme yaptı.")

    async def _get_bytes(self, url: str, *, allowed_hosts: Iterable[str]) -> tuple[bytes, str]:
        """Büyük resmî veri dosyalarını indirir; her yönlendirme adımı yeniden doğrulanır."""
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=allowed_hosts)
            response: httpx.Response | None = None
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await self._http.get(current, timeout=httpx.Timeout(180.0, connect=15.0))
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if response is None:
                raise last_error or RuntimeError("Resmî veri dosyası alınamadı.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("Resmî veri kaynağı hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            content = response.content
            if len(content) > _MAX_FILE_BYTES:
                raise ValueError("Resmî veri dosyası beklenenden büyük.")
            return content, current
        raise ValueError("Resmî veri kaynağı çok fazla yönlendirme yaptı.")

    async def _cached_json(self, path: str, cache_key: str, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            cached = self.store.cached(cache_key, max_age_days=self.cache_days)
            if cached:
                return cached
        payload, url = await self._get_json(path)
        return self.store.store_cache(cache_key, payload, source_url=url)

    async def close(self) -> None:
        await self._http.aclose()

    # ---- eşitleme
    async def sync(self, *, force: bool = False) -> dict[str, Any]:
        """UK fasıl listesini çeker, farkı inceleme kapısından geçirir ve deftere yazar."""
        async with self._sync_lock:
            self._syncing = True
            try:
                retrieved_at = _now()
                self._errors.clear()
                # Her kaynak ayrı ayrı denenir ve kendi zamanlamasına bakar: birinin hatası
                # diğerini engellemez, yeni eklenen bir veri seti de diğerinin damgası yüzünden
                # bir gün beklemez (hiç eşitlenmemiş veri seti her zaman hemen çekilir).
                sources: list[tuple[str, str, Any]] = [("UK", UK_DATASET, self._sync_uk_chapters)]
                if CH_SYNC_ENABLED:
                    sources.append(("CH", CH_DATASET, self._sync_swiss_nomenclature))
                ran = False
                for label, dataset, handler in sources:
                    if not force and not self._dataset_due(dataset):
                        continue
                    ran = True
                    self.store.set_metadata(f"last_checked_at:{dataset}", retrieved_at)
                    try:
                        await handler(retrieved_at)
                    except Exception as exc:  # noqa: BLE001
                        self._errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:250]}")
                        logger.warning("Yurt dışı tarife eşitlemesi başarısız (%s): %s", label, exc)
                if not ran:
                    return self.status()
                self.store.set_metadata("last_checked_at", retrieved_at)
                self.store.purge_cache(max_age_days=self.cache_days)
            except Exception as exc:  # noqa: BLE001 – eşitleme hatası sunucuyu durdurmaz
                self._errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
                logger.warning("Yurt dışı tarife eşitlemesi başarısız: %s", exc)
            finally:
                self._syncing = False
        return self.status()

    def _dataset_due(self, dataset: str) -> bool:
        """Hiç eşitlenmemiş veri seti hemen çekilir; diğerleri kendi aralığını bekler."""
        if self.store.active_snapshot(dataset) is None:
            return True
        stamp = self.store.get_metadata(f"last_checked_at:{dataset}")
        if not stamp:
            return True
        try:
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
        except ValueError:
            return True
        return elapsed >= self.sync_interval_seconds

    async def _sync_uk_chapters(self, retrieved_at: str) -> None:
        payload, url = await self._get_json("/chapters")
        rows = parse_chapters(payload)
        if not rows:
            raise ValueError("UK fasıl listesi boş döndü.")
        self._commit_snapshot(
            dataset=UK_DATASET, jurisdiction="uk", kind="chapter", prefix="uk-chapters",
            rows=rows, source_url=url, retrieved_at=retrieved_at,
            title="Birleşik Krallık tarife nomenklatürü (fasıl listesi)",
        )

    async def _sync_swiss_nomenclature(self, retrieved_at: str) -> None:
        content, url = await self._get_bytes(CH_NOMENCLATURE_URL, allowed_hosts=_CH_HOSTS)
        rows = await asyncio.to_thread(parse_swiss_nomenclature, content.decode("utf-8-sig", errors="replace"))
        self._commit_snapshot(
            dataset=CH_DATASET, jurisdiction="ch", kind="ch_tariff", prefix="ch-nomenclature",
            rows=rows, source_url=url, retrieved_at=retrieved_at,
            title="İsviçre gümrük tarifesi nomenklatürü (BAZG açık verisi)",
        )

    def _commit_snapshot(
        self, *, dataset: str, jurisdiction: str, kind: str, prefix: str, rows: list[dict[str, Any]],
        source_url: str, retrieved_at: str, title: str,
    ) -> None:
        """Yeni bir anlık görüntüyü kaydeder, farkı inceleme kapısından geçirir ve deftere yazar."""
        digest = _sha256(rows)
        existing = self.store.snapshot_by_sha(dataset, digest)
        if existing is not None:
            if row_review_fields(existing)["status"] == "approved":
                with self.store.connect() as connection:
                    connection.execute("UPDATE snapshots SET active=(id=?) WHERE dataset=?", (existing["id"], dataset))
            return
        snapshot_id = f"{prefix}-{digest[:16]}"
        previous = self.store.latest_approved(dataset, exclude=snapshot_id)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO snapshots(id,jurisdiction,dataset,source_url,sha256,retrieved_at,item_count,"
                "active,valid_from,status) VALUES(?,?,?,?,?,?,?,0,?, 'pending_review')",
                (snapshot_id, jurisdiction, dataset, source_url, digest, retrieved_at, len(rows), retrieved_at[:10]),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO nomenclature(snapshot_id,jurisdiction,kind,code,description,source_url,"
                "description_alt,valid_from,valid_to) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        snapshot_id, jurisdiction, kind, row["code"], row["description"], source_url,
                        row.get("description_alt") or "", row.get("valid_from"), row.get("valid_to"),
                    )
                    for row in rows
                ],
            )
        current_rows = {row["code"]: row for row in rows}
        previous_rows = self.store.rows_of(previous["id"], kind) if previous else {}
        changes = (
            diff_rows(current_rows, previous_rows, fields=("description",), gtip_of=lambda row: row.get("code"))
            if previous
            else []
        )
        summary = DiffSummary(
            total_rows=len(current_rows),
            previous_rows=len(previous_rows),
            added=sum(1 for item in changes if item["change_type"] == "added"),
            removed=sum(1 for item in changes if item["change_type"] == "removed"),
            modified=sum(1 for item in changes if item["change_type"] == "modified"),
        )
        decision = decide(self.review_policy, summary, first_snapshot=previous is None)
        warnings_json, diff_json = review_metadata(summary, decision, [])
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE snapshots SET status=?, parse_warnings_json=?, diff_summary_json=? WHERE id=?",
                (decision.status, warnings_json, diff_json, snapshot_id),
            )
            if not decision.pending:
                connection.execute("UPDATE snapshots SET active=0 WHERE dataset=?", (dataset,))
                connection.execute("UPDATE snapshots SET active=1 WHERE id=?", (snapshot_id,))
        self._record_ledger_batch(
            snapshot_id, previous["id"] if previous else None, changes=changes, source_url=source_url,
            sha256=digest, total_rows=len(current_rows), detected_at=retrieved_at,
            review_status=decision.status, dataset=dataset, title=title,
        )
        if decision.pending:
            logger.info("%s anlık görüntüsü %s editör onayı bekliyor: %s", title, snapshot_id, "; ".join(decision.reasons))

    async def periodic_sync_loop(self, *, initial_delay: float = 120.0) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            status = await self.sync()
            await asyncio.sleep(self.sync_interval_seconds if status.get("ready") else 900)

    # ---- sorgu
    async def lookup(
        self,
        gtip: str,
        *,
        origin: str | None = None,
        jurisdiction: str = "all",
        as_of: str | None = None,
    ) -> ForeignTariffLookup:
        code = digits_only(gtip)
        if len(code) < 6:
            raise ValueError("Yurt dışı karşılaştırma için en az 6 haneli bir GTİP/HS kodu gerekir.")
        selected = JURISDICTIONS if jurisdiction in {"", "all", None} else tuple(
            item for item in JURISDICTIONS if item == str(jurisdiction).lower()
        )
        if not selected:
            raise ValueError("Desteklenen yargı alanları: uk, eu, ch (veya all).")
        iso2 = origin_iso2(origin)
        result = ForeignTariffLookup(
            gtip=code, hs6=code[:6], origin_country=(str(origin).strip() or None) if origin else None,
            origin_code=iso2, as_of=as_of,
        )
        if origin and not iso2:
            result.warnings.append(f"Menşe ülke tanınmadı: {str(origin)[:60]}. Tercihli oran eşleştirilemedi.")
        for item in selected:
            if item == "uk":
                result.results.append(await self._uk_result(code, iso2, as_of))
            elif item == "ch":
                result.results.append(self._swiss_result(code, iso2, as_of))
            else:
                result.results.append(self._link_result(item, code, iso2, as_of))
        return result

    async def _uk_result(self, code: str, iso2: str | None, as_of: str | None) -> JurisdictionResult:
        record = self.link_catalog.get("uk", {})
        outcome = JurisdictionResult(
            jurisdiction="uk",
            label=JURISDICTION_LABELS["uk"],
            data_kind="api",
            authority=str(record.get("authority") or "HM Revenue & Customs – UK Trade Tariff"),
            links=build_links(record, code, iso2, as_of) if record else [],
            notes=[COMPARABILITY_NOTE],
        )
        heading = code[:4]
        try:
            heading_cache = await self._cached_json(f"/headings/{heading}", f"uk:heading:{heading}")
            parsed_heading = parse_heading(heading_cache["payload"])
        except (SecurityViolation, httpx.HTTPError, ValueError) as exc:
            outcome.match_quality = "unavailable"
            outcome.notes.append(f"UK tarife verisi şu anda alınamadı: {str(exc)[:160]}")
            return outcome

        hs6 = code[:6]
        leaves = [row for row in parsed_heading["commodities"] if row["leaf"]]
        exact = [row for row in leaves if row["code"].startswith(hs6)]
        candidates = exact or leaves
        outcome.candidates = [{"code": row["code"], "description": row["description"]} for row in candidates[:12]]
        if not candidates:
            outcome.notes.append(f"UK tarifesinde {heading} pozisyonu altında beyan edilebilir kod bulunamadı.")
            outcome.description = parsed_heading.get("description")
            outcome.source_url = heading_cache["source_url"]
            return outcome
        outcome.match_quality = "exact_hs6" if exact else "heading_only"
        if not exact:
            outcome.notes.append(
                f"UK tarifesinde {hs6} ile başlayan kod bulunamadı; {heading} pozisyonunun tamamı listelendi."
            )
        chosen = candidates[0]
        try:
            commodity_cache = await self._cached_json(f"/commodities/{chosen['code']}", f"uk:commodity:{chosen['code']}")
            parsed = parse_commodity(commodity_cache["payload"])
        except (SecurityViolation, httpx.HTTPError, ValueError) as exc:
            outcome.matched_code = chosen["code"]
            outcome.description = chosen["description"]
            outcome.source_url = heading_cache["source_url"]
            outcome.notes.append(f"UK ölçü satırları alınamadı: {str(exc)[:160]}")
            return outcome
        summary = summarise_commodity(parsed, iso2)
        outcome.matched_code = parsed["code"] or chosen["code"]
        outcome.description = parsed["description"] or chosen["description"]
        outcome.third_country_duty = summary["third_country_duty"]
        outcome.origin_preference = summary["origin_preference"]
        outcome.measures = summary["measures"]
        outcome.bti_url = parsed.get("bti_url")
        outcome.source_url = commodity_cache["source_url"]
        outcome.sha256 = commodity_cache["sha256"]
        outcome.retrieved_at = commodity_cache["fetched_at"]
        if iso2 and summary["origin_preference"] is None:
            outcome.notes.append(f"{iso2} menşeli eşya için UK tarifesinde tercihli oran satırı bulunmadı.")
        return outcome

    def _swiss_result(self, code: str, iso2: str | None, as_of: str | None) -> JurisdictionResult:
        """İsviçre: resmî açık veriden kod ve tanım; oran yayımlanmadığı için sorgu bağlantısı da verilir."""
        outcome = self._link_result("ch", code, iso2, as_of)
        active = self.store.active_snapshot(CH_DATASET)
        if active is None:
            outcome.notes.append("İsviçre tarife nomenklatürü henüz eşitlenmedi; yalnız resmî sorgu bağlantıları gösteriliyor.")
            return outcome
        hs6 = code[:6]
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT code, description, description_alt, valid_from, valid_to FROM nomenclature "
                "WHERE snapshot_id=? AND code LIKE ? ORDER BY code LIMIT 12",
                (active["id"], f"{hs6}%"),
            ).fetchall()
        outcome.data_kind = "nomenclature"
        outcome.source_url = str(active["source_url"])
        outcome.sha256 = str(active["sha256"])
        outcome.retrieved_at = str(active["retrieved_at"])
        if not rows:
            outcome.notes.append(f"İsviçre tarifesinde {hs6} ile başlayan numara bulunamadı.")
            return outcome
        outcome.match_quality = "exact_hs6"
        outcome.matched_code = str(rows[0]["code"])
        outcome.description = str(rows[0]["description"])
        outcome.candidates = [
            {"code": str(row["code"]), "description": str(row["description"])} for row in rows
        ]
        outcome.notes.append(
            "İsviçre resmî açık verisi tarife numarası ve eşya tanımını içerir; **vergi oranı yayımlanmaz**, "
            "oran için Tares sorgu ekranı kullanılır."
        )
        return outcome

    def _link_result(self, jurisdiction: str, code: str, iso2: str | None, as_of: str | None) -> JurisdictionResult:
        record = self.link_catalog.get(jurisdiction, {})
        notes = [str(record.get("note") or "")] if record.get("note") else []
        if not record:
            notes.append("Bu yargı alanı için resmî bağlantı kataloğu yüklenemedi.")
        return JurisdictionResult(
            jurisdiction=jurisdiction,
            label=JURISDICTION_LABELS.get(jurisdiction, jurisdiction.upper()),
            data_kind="links",
            authority=str(record.get("authority") or ""),
            match_quality="unavailable",
            links=build_links(record, code, iso2, as_of) if record else [],
            notes=notes,
        )

    # ---- durum ve inceleme
    def status(self) -> dict[str, Any]:
        active = self.store.active_snapshot(UK_DATASET)
        swiss = self.store.active_snapshot(CH_DATASET)
        with self.store.connect() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE status='pending_review'"
            ).fetchone()[0]
            cached = connection.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        return {
            "ready": bool(active),
            "syncing": self._syncing,
            "review_mode": self.review_policy.mode,
            "pending_review_count": int(pending),
            "chapter_count": int(active["item_count"]) if active else 0,
            "swiss_ready": bool(swiss),
            "swiss_code_count": int(swiss["item_count"]) if swiss else 0,
            "active_snapshot": str(active["id"]) if active else None,
            "active_sha256": str(active["sha256"]) if active else None,
            "last_checked_at": self.store.get_metadata("last_checked_at"),
            "cached_documents": int(cached),
            "jurisdictions": [
                {
                    "code": item,
                    "label": JURISDICTION_LABELS[item],
                    "data_kind": "api" if item == "uk" else ("nomenclature" if item == "ch" and swiss else "links"),
                    "authority": str((self.link_catalog.get(item) or {}).get("authority") or ""),
                }
                for item in JURISDICTIONS
            ],
            "errors": self._errors[-8:],
        }

    def _review_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "kind": "foreign_tariff",
            "snapshot_id": row["id"],
            "source_id": row["dataset"],
            "title": f"{JURISDICTION_LABELS.get(row['jurisdiction'], row['jurisdiction'])} tarife nomenklatürü",
            "source_url": row["source_url"],
            "sha256": row["sha256"],
            "retrieved_at": row["retrieved_at"],
            "valid_from": row["valid_from"],
            "total_rows": int(row["item_count"] or 0),
            "active": bool(row["active"]),
            "ledger_batch": batch_id_for("foreign_tariff", row["dataset"], row["id"]) if self.ledger is not None else None,
        }
        item.update(row_review_fields(row))
        return item

    def pending_reviews(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE status='pending_review' ORDER BY retrieved_at DESC"
            ).fetchall()
        return [self._review_item(row) for row in rows]

    def review_snapshot(self, snapshot_id: str, action: str, *, reviewed_by: str, note: str = "") -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError("Karar 'approve' veya 'reject' olmalıdır.")
        now = _now()
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            dataset = row["dataset"]
            if action == "approve":
                connection.execute("UPDATE snapshots SET active=0 WHERE dataset=?", (dataset,))
                connection.execute(
                    "UPDATE snapshots SET active=1, status='approved', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (reviewed_by, now, note, snapshot_id),
                )
            else:
                connection.execute(
                    "UPDATE snapshots SET active=0, status='rejected', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (reviewed_by, now, note, snapshot_id),
                )
                if row["active"]:
                    fallback = connection.execute(
                        "SELECT id FROM snapshots WHERE dataset=? AND status='approved' AND id<>? ORDER BY retrieved_at DESC LIMIT 1",
                        (dataset, snapshot_id),
                    ).fetchone()
                    if fallback:
                        connection.execute("UPDATE snapshots SET active=1 WHERE id=?", (fallback["id"],))
            updated = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return self._review_item(updated)

    # ---- değişiklik defteri
    def _record_ledger_batch(
        self,
        snapshot_id: str,
        previous_id: str | None,
        *,
        changes: list[dict[str, Any]],
        source_url: str,
        sha256: str,
        total_rows: int,
        detected_at: str,
        review_status: str,
        dataset: str = UK_DATASET,
        title: str = "Birleşik Krallık tarife nomenklatürü (fasıl listesi)",
        backfilled: bool = False,
    ) -> str | None:
        if self.ledger is None:
            return None
        try:
            return self.ledger.record_batch(
                kind="foreign_tariff",
                source_id=dataset,
                title=title,
                new_snapshot_id=snapshot_id,
                old_snapshot_id=previous_id,
                source_url=source_url,
                sha256=sha256,
                changes=changes,
                total_rows=total_rows,
                detected_at=detected_at,
                review_status=review_status,
                backfilled=backfilled,
            )
        except Exception as exc:  # noqa: BLE001 – defter bir eşitlemeyi asla bozmaz
            logger.warning("Yurt dışı tarife değişiklik defteri yazımı başarısız: %s", exc)
            return None

    def backfill_ledger(self) -> int:
        if self.ledger is None:
            return 0
        written = 0
        for dataset, kind in ((UK_DATASET, "chapter"), (CH_DATASET, "ch_tariff")):
            with self.store.connect() as connection:
                snapshots = connection.execute(
                    "SELECT * FROM snapshots WHERE dataset=? ORDER BY retrieved_at ASC", (dataset,)
                ).fetchall()
            previous_id: str | None = None
            for row in snapshots:
                if not self.ledger.has_batch(batch_id_for("foreign_tariff", dataset, row["id"])):
                    changes = (
                        diff_rows(
                            self.store.rows_of(row["id"], kind),
                            self.store.rows_of(previous_id, kind) if previous_id else {},
                            fields=("description",),
                            gtip_of=lambda item: item.get("code"),
                        )
                        if previous_id
                        else []
                    )
                    if self._record_ledger_batch(
                        row["id"], previous_id, changes=changes, source_url=row["source_url"], sha256=row["sha256"],
                        total_rows=int(row["item_count"] or 0), detected_at=row["retrieved_at"],
                        review_status=row_review_fields(row)["status"], dataset=dataset, backfilled=True,
                    ):
                        written += 1
                previous_id = row["id"]
        return written

    # ---- hibrit indeks beslemesi
    def corpus_rows(self, limit: int = 4000) -> list[dict[str, Any]]:
        """Onaylı UK nomenklatür satırlarını hibrit indeks belgesi biçiminde döndürür."""
        documents: list[dict[str, Any]] = []
        for dataset, label, site in ((UK_DATASET, "UK Fasıl", UK_SITE_URL), (CH_DATASET, "İsviçre tarife no.", CH_SOURCE_PAGE)):
            active = self.store.active_snapshot(dataset)
            if active is None:
                continue
            with self.store.connect() as connection:
                rows = connection.execute(
                    "SELECT code, description, source_url FROM nomenclature WHERE snapshot_id=? ORDER BY code LIMIT ?",
                    (active["id"], int(limit)),
                ).fetchall()
            for row in rows:
                if not str(row["description"] or "").strip():
                    continue
                documents.append(
                    {
                        "id": f"{dataset}-{row['code']}",
                        "corpus": "foreign_tariff",
                        "title": f"{label} {row['code']}",
                        "text": row["description"],
                        "gtip_codes": [row["code"]],
                        "source_url": row["source_url"] or site,
                        "source_sha256": str(active["sha256"]),
                        "snapshot_id": str(active["id"]),
                    }
                )
        return documents


__all__ = [
    "CH_DATASET",
    "COMPARABILITY_NOTE",
    "SYNC_ENABLED",
    "ForeignTariffEngine",
    "ForeignTariffLookup",
    "ForeignTariffStore",
    "JURISDICTIONS",
    "JURISDICTION_LABELS",
    "JurisdictionResult",
    "MEASURE_KIND_LABELS",
    "NO_CALCULATION_NOTE",
    "UK_DATASET",
    "build_links",
    "classify_measure",
    "digits_only",
    "load_link_catalog",
    "measure_applies",
    "origin_iso2",
    "parse_chapters",
    "parse_commodity",
    "parse_heading",
    "parse_swiss_nomenclature",
    "summarise_commodity",
]
