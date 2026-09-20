"""Versioned, evidence-first tariff and trade-measure engine.

The engine only imports machine-readable archives discovered on official Ministry
of Trade landing pages.  Every parsed rate keeps its archive checksum, workbook,
sheet, row and source URL.  A rate is never guessed from prose or from an LLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urljoin, urlsplit

import httpx
import xlrd
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
from openpyxl import load_workbook
from pydantic import BaseModel, ConfigDict, Field, field_validator

from countries import PENDING_AGREEMENTS, by_regime, column_1_keys, explicit_labels, find_country
from origin_documents import atr_eligible
from change_ledger import batch_id_for, diff_rows
from review_policy import DiffSummary, ReviewPolicy, decide, ensure_review_columns, review_metadata, row_review_fields
from temporal import (
    close_previous, covers, derive_validity, ensure_validity_columns, normalise_as_of, parse_iso_date, snapshot_validity,
    today_iso, validity_basis,
)
from security_firewall import validate_outbound_url

_GTIP_RE = re.compile(r"^\d{4}(?:\d{2}){0,4}$")
_RATE_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_OFFICIAL_HOSTS = {"ticaret.gov.tr", "www.ticaret.gov.tr"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ı", "i"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _digits(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\D", "", str(value))


def _normalise_gtip(value: Any) -> str | None:
    digits = _digits(value)
    if len(digits) in {11}:
        digits = digits.zfill(12)
    if len(digits) in {4, 6, 8, 10, 12}:
        return digits
    return None


def _number(value: Any) -> tuple[float | None, str]:
    text = "" if value is None else str(value).strip().replace("%", "")
    if not text:
        return None, ""
    canonical = text.replace(" ", "").replace(",", ".")
    if not _RATE_RE.fullmatch(canonical):
        return None, text[:160]
    return float(canonical), text[:160]



def _list_name_is(file_key: str, numeral: str) -> bool:
    """True when the (normalised) file name names exactly this Roman-numeral list."""
    return re.search(rf"(?:^|[^a-z]){numeral}\s*say", file_key) is not None


def _annex_sheet_is(sheet_key: str, number: int) -> bool:
    """Match 'Ek-2', 'EK 2', 'Ek2' and similar official sheet names."""
    return re.search(rf"(?:^|[^a-z])ek\s*[-–]?\s*{number}(?!\d)", sheet_key) is not None


def _official_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower().rstrip(".") in _OFFICIAL_HOSTS


def _repair_zip_name(name: str) -> str:
    try:
        return name.encode("cp437").decode("cp857")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _zip_name(item: zipfile.ZipInfo) -> str:
    """Decode Ministry ZIP member names written with the Turkish DOS codepage."""
    return item.filename if item.flag_bits & 0x800 else _repair_zip_name(item.filename)


class TariffMeasure(BaseModel):
    gtip: str
    measure_type: Literal[
        "customs_duty",
        "additional_duty",
        "additional_financial_liability",
        "customs_duty_suspension",
        "customs_duty_end_use",
    ]
    rate: float | None = None
    rate_text: str
    country_group: str
    country_group_description: str
    footnote: str | None = None
    description: str | None = None
    condition: str | None = None
    list_name: str
    source_id: str
    source_title: str
    source_url: str
    archive_url: str
    archive_sha256: str
    source_file: str
    source_sheet: str
    source_row: int
    valid_from: str
    retrieved_at: str
    snapshot_id: str
    automatic_calculation_allowed: bool = True


class TariffSnapshot(BaseModel):
    id: str
    source_id: str
    source_title: str
    landing_url: str
    archive_url: str
    archive_sha256: str
    retrieved_at: str
    checked_at: str
    valid_from: str
    measure_count: int
    active: bool
    valid_to: str | None = None
    valid_from_basis: str = "config"
    valid_to_basis: str | None = None


class TariffLookupResult(BaseModel):
    status: Literal["matched", "partial", "not_found", "unavailable"]
    gtip: str
    match_mode: Literal["exact", "prefix"] = "exact"
    # Resmî Türk Gümrük Tarife Cetvelinden gelen eşya tanımı (``tariff_nomenclature``).
    # Motor bağlı değilse boş kalır ve yanıt eskisiyle birebir aynıdır. ``goods_full_path``
    # ata satırlarla birleştirilmiş hâl: çoğu 12 haneli satırın kendi tanımı yalnız
    # "Diğerleri"dir ve tek başına hiçbir sınıflandırma sorusunu cevaplamaz.
    goods_description: str = ""
    goods_full_path: str = ""
    goods_unit: str = ""
    goods_matched_code: str = ""
    matched_gtips: list[str] = Field(default_factory=list)
    matched_gtip_count: int = 0
    origin_country: str | None = None
    dispatch_country: str | None = None
    origin_recognised: bool = True
    atr_free_circulation: bool = False
    atr_available: bool = False
    atr_certificate: bool | None = None
    origin_proof_required: list[str] = Field(default_factory=list)
    fallback_rates: dict[str, float] = Field(default_factory=dict)
    resolved_country_group: str | None = None
    rate_variants: dict[str, list[float]] = Field(default_factory=dict)
    unambiguous_rates: dict[str, float] = Field(default_factory=dict)
    ambiguous_measure_types: list[str] = Field(default_factory=list)
    measures: list[TariffMeasure] = Field(default_factory=list)
    conditional_measures: list[TariffMeasure] = Field(default_factory=list)
    alternatives: list[TariffMeasure] = Field(default_factory=list)
    snapshots: list[TariffSnapshot] = Field(default_factory=list)
    measure_coverage: dict[str, "MeasureCoverage"] = Field(default_factory=dict)
    unresolved_measure_types: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    trade_measures: dict[str, Any] | None = None
    # Temporal validity (PRD Faz 1.4): the as-of date answered and how the interval was established.
    as_of_date: str | None = None
    validity_basis: Literal["current", "legal", "observed", "unavailable"] = "current"
    snapshot_validity: list[dict[str, Any]] = Field(default_factory=list)
    excise_tax: dict[str, Any] | None = None
    vat_rate: dict[str, Any] | None = None  # vat_lists.VatRateIndex önerisi; otomatik uygulanmaz
    as_of: str


class MeasureCoverage(BaseModel):
    status: Literal["verified_snapshot", "partial_snapshot", "not_integrated", "user_confirmation_required"]
    source_ids: list[str] = Field(default_factory=list)
    note: str


class TariffTreeNode(BaseModel):
    """One deterministic branch in the HS6 -> CN8 -> TR10 -> GTIP12 tree.

    ``description`` ve ``full_path`` resmî Türk Gümrük Tarife Cetvelinden gelir
    (``tariff_nomenclature``). Ölçülen boşluk buydu: ağaç çocukları oran döndürüyor
    ama eşya tanımı döndürmüyordu, dolayısıyla "8471.60.60 mı 8471.60.70 mi"
    sorusu ağaca bakarak cevaplanamıyordu. Nomenklatür motoru bağlı değilse alanlar
    boş kalır ve davranış eskisiyle birebir aynıdır.
    """

    code: str
    level: Literal["HS6", "CN8", "TR10", "GTIP12"]
    final: bool
    description: str = ""
    full_path: str = ""
    unit: str = ""
    descendant_count: int = Field(..., ge=1)
    rate_status: Literal["unambiguous", "ambiguous", "origin_required"]
    unambiguous_rates: dict[str, float] = Field(default_factory=dict)
    rate_variants: dict[str, list[float]] = Field(default_factory=dict)
    ambiguous_measure_types: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TariffDecisionTreeResult(BaseModel):
    """Deterministic descendants of a tariff prefix from active official tables."""

    status: Literal["matched", "not_found", "unavailable"]
    prefix: str
    level: Literal["HS4", "HS6", "CN8", "TR10", "GTIP12"]
    next_level: Literal["HS6", "CN8", "TR10", "GTIP12"] | None = None
    origin_country: str | None = None
    total_children: int = 0
    children: list[TariffTreeNode] = Field(default_factory=list)
    requires_user_selection: bool = True
    exact_gtip_selected: bool = False
    warnings: list[str] = Field(default_factory=list)
    as_of: str
    as_of_date: str | None = None
    validity_basis: Literal["current", "legal", "observed", "unavailable"] = "current"


class TariffSyncStatus(BaseModel):
    ready: bool
    syncing: bool
    last_checked_at: str | None = None
    active_snapshots: list[TariffSnapshot] = Field(default_factory=list)
    measure_count: int = 0
    errors: list[str] = Field(default_factory=list)
    sync_interval_seconds: int
    pending_review_count: int = 0
    review_mode: str = "off"


class LandedCostInput(BaseModel):
    # Unknown keys (typos such as customs_duty_rat) must fail loudly instead of silently
    # letting the official rate replace the rate the user believed they had entered.
    model_config = ConfigDict(extra="forbid")

    invoice_value: float = Field(..., gt=0, le=1_000_000_000)
    freight: float = Field(0, ge=0, le=1_000_000_000)
    insurance: float = Field(0, ge=0, le=1_000_000_000)
    other_costs: float = Field(0, ge=0, le=1_000_000_000)
    quantity: float | None = Field(None, gt=0, le=1_000_000_000)
    currency: str = Field("USD", min_length=3, max_length=3)
    customs_duty_rate: float | None = Field(None, ge=0, le=1000)
    additional_duty_rate: float | None = Field(None, ge=0, le=1000)
    additional_financial_liability_rate: float | None = Field(None, ge=0, le=1000)
    anti_dumping_amount: float | None = Field(None, ge=0, le=1_000_000_000)
    kkdf_rate: float | None = Field(None, ge=0, le=100)
    vat_rate: float | None = Field(None, ge=0, le=100)
    sct_amount: float | None = Field(None, ge=0, le=1_000_000_000)
    surveillance_unit_value: float | None = Field(None, ge=0, le=1_000_000_000)
    has_surveillance_certificate: bool | None = None
    payment_method: str | None = Field(None, max_length=100)
    # TRT bandrol ücreti (3093 s. Kanun): oran ürün grubuna göre; matrah gümrük kıymeti + ithalat vergileri (ÖTV/KDV hariç).
    trt_bandrol_rate: float | None = Field(None, ge=0, le=100)
    # TL beyanname özeti: beyanname tescil tarihindeki TCMB döviz satış kuru (1 birim döviz = kaç TL).
    exchange_rate: float | None = Field(None, gt=0, le=1_000_000)
    exchange_rate_date: str | None = Field(None, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Yalnız TL olarak ödenen kalemler; kur verilince TL özetine girer.
    stamp_duty_try: float | None = Field(None, ge=0, le=1_000_000_000)      # beyanname damga vergisi (KDV matrahına dahil)
    port_storage_try: float | None = Field(None, ge=0, le=1_000_000_000)    # tescile kadar liman/ardiye/tahmil-tahliye (KDV matrahına dahil)
    gekap_try: float | None = Field(None, ge=0, le=1_000_000_000)           # GEKAP; gümrükte değil beyanla ödenir (KDV matrahı dışı)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = value.upper()
        if not value.isalpha():
            raise ValueError("Para birimi üç harfli olmalıdır.")
        return value


class LandedCostResult(BaseModel):
    status: Literal["complete", "partial", "blocked"]
    currency: str
    lines: list[dict[str, Any]]
    customs_value: float
    vat_base: float | None = None
    total_taxes: float | None = None
    landed_total: float | None = None
    unit_landed_cost: float | None = None
    missing_rates: list[str] = Field(default_factory=list)
    rate_overrides: list[dict[str, Any]] = Field(default_factory=list)
    try_summary: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    formula_version: str = "tr-landed-cost-v4"


@dataclass(slots=True)
class _ParsedArchive:
    measures: list[dict[str, Any]]
    metadata: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


_MEASURE_ROW_FIELDS = ("gtip", "measure_type", "country_group", "rate", "rate_text", "footnote", "list_name")


def _measure_row_key(row: Any) -> str:
    return f"{row['gtip']}|{row['measure_type']}|{row['country_group']}"


_MEASURE_LABELS = {
    "customs_duty": "gümrük vergisi",
    "additional_duty": "İGV",
    "additional_financial_liability": "ek mali yükümlülük",
}

_GROUP_DESCRIPTIONS = {
    "1": "AB, EFTA ve İthalat Rejimi Kararında 1 numaralı sütunda sayılan STA ülkeleri",
    "2": "Katar",
    "3": "Birleşik Arap Emirlikleri",
    "4": "En Az Gelişmiş Ülkeler",
    "5": "Özel Teşvik Düzenlemesinden Yararlanan Ülkeler",
    "6": "Gelişme Yolundaki Ülkeler",
    "7": "Diğer Ülkeler",
    "EAGÜ": "En Az Gelişmiş Ülkeler",
    "ÖTDÜ": "Özel Teşvik Düzenlemesinden Yararlanan Ülkeler",
    "GYÜ": "Gelişme Yolundaki Ülkeler",
    "DÜ": "Diğer Ülkeler",
}

# Country facts come from countries.py (single source shared with origin_documents.py).
_EU_COUNTRIES = by_regime("eu")
_EFTA_COUNTRIES = by_regime("efta")
_COLUMN_1_COUNTRIES = column_1_keys() - _EU_COUNTRIES - _EFTA_COUNTRIES
# Countries that have their own column token in at least one official list.
_EXPLICIT_LABELS = {alias: label for alias, label in explicit_labels().items() if label not in {"AB", "EFTA"}}


class TariffEngine:
    trade_measures: Any = None  # trade_measures.TradeMeasureEngine; sunucu başlangıcında bağlanır
    excise_tax: Any = None  # tax_lists.ExciseTaxIndex; sunucu başlangıcında bağlanır
    vat_rates: Any = None  # vat_lists.VatRateIndex; sunucu başlangıcında bağlanır
    """Synchronise, query and diff official tariff snapshots."""

    def __init__(self, config_path: str | Path | None = None, data_dir: str | Path | None = None) -> None:
        candidate = Path(config_path or Path(__file__).with_name("tariff_sources.json"))
        if not candidate.exists():
            for p in (Path(sys.prefix) / "tariff_sources.json", Path.cwd() / "tariff_sources.json"):
                if p.exists():
                    candidate = p
                    break
        config_file = candidate
        config = json.loads(config_file.read_text(encoding="utf-8"))
        self.sources = list(config.get("sources", []))
        self.sync_interval_seconds = max(300, int(config.get("sync_interval_seconds", 21600)))
        default_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mevzuat-mcp"
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR", default_root))
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "tariff.sqlite3"
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(60),
            headers={
                "User-Agent": "Gumrukce/1.4 (+official-tariff-sync)",
                "Accept-Language": "tr-TR,tr;q=0.9",
            },
            limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
        )
        self._sync_lock = asyncio.Lock()
        self._syncing = False
        self._errors: list[str] = []
        # Optional unified change ledger (change_ledger.ChangeLedger); set by the server.
        self.ledger: Any = None
        # Editorial review gate; the server replaces it with policy_from_env().
        self.review_policy: ReviewPolicy = ReviewPolicy()
        # Resmî eşya tanımı motoru (tariff_nomenclature.NomenclatureEngine); sunucuda
        # bağlanır. None ise karar ağacı bugünküyle birebir aynı, tanımsız çalışır.
        self.nomenclature: Any = None
        # Landing-page text of the last discovery per source (validity date derivation).
        self._landing_text: dict[str, str] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tariff_snapshots (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_title TEXT NOT NULL,
                    landing_url TEXT NOT NULL, archive_url TEXT NOT NULL, archive_sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL, checked_at TEXT NOT NULL, valid_from TEXT NOT NULL,
                    measure_count INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_tariff_snapshots_source ON tariff_snapshots(source_id, active);
                CREATE TABLE IF NOT EXISTS tariff_measures (
                    id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES tariff_snapshots(id) ON DELETE CASCADE,
                    gtip TEXT NOT NULL, measure_type TEXT NOT NULL, rate REAL, rate_text TEXT NOT NULL,
                    country_group TEXT NOT NULL, country_group_description TEXT NOT NULL,
                    footnote TEXT, description TEXT, condition_text TEXT, list_name TEXT NOT NULL,
                    source_file TEXT NOT NULL, source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL,
                    automatic_calculation_allowed INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_tariff_measures_gtip ON tariff_measures(gtip);
                CREATE INDEX IF NOT EXISTS idx_tariff_measures_snapshot ON tariff_measures(snapshot_id);
                """
            )
            ensure_review_columns(db, "tariff_snapshots")
            ensure_validity_columns(db, "tariff_snapshots")
            ensure_validity_columns(db, "tariff_measures", (("valid_from", "TEXT"), ("valid_to", "TEXT")))

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(3):
            try:
                current_url = url
                for _ in range(6):
                    validate_outbound_url(current_url, allowed_hosts=_OFFICIAL_HOSTS)
                    response = await self._http.get(current_url)
                    if not response.is_redirect:
                        break
                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError("Resmî tarife yönlendirmesi hedefsiz")
                    current_url = urljoin(str(response.url), location)
                else:
                    raise ValueError("Resmî tarife kaynağı çok fazla yönlendirme yaptı")
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException, ValueError) as exc:
                last = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Resmî kaynak alınamadı: {type(last).__name__}") from last

    async def _discover_archive(self, source: dict[str, Any]) -> str:
        response = await self._get(source["landing_url"])
        soup = BeautifulSoup(response.text, "lxml")
        self._landing_text[source["id"]] = soup.get_text(" ", strip=True)[:200_000]
        wanted = _key(source.get("archive_text", ""))
        candidates: list[tuple[int, str]] = []
        for anchor in soup.select("a[href]"):
            href = urljoin(str(response.url), anchor.get("href", ""))
            if not _official_url(href) or ".zip" not in _key(urlsplit(href).path):
                continue
            label = _key(anchor.get_text(" ", strip=True))
            score = 100 if wanted and wanted in label else 10
            if str(datetime.now().year) in label or str(datetime.now().year) in href:
                score += 5
            candidates.append((score, href))
        if not candidates:
            raise ValueError("Resmî sayfada ZIP tablo bağlantısı bulunamadı.")
        return max(candidates, key=lambda item: item[0])[1]

    async def _download_archive(self, url: str) -> bytes:
        last: Exception | None = None
        for attempt in range(4):
            chunks: list[bytes] = []
            try:
                async with self._http.stream("GET", url, headers={"Referer": "https://ticaret.gov.tr/"}) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                data = b"".join(chunks)
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if archive.testzip() is not None:
                        raise ValueError("Arşiv bütünlük denetiminden geçemedi.")
                return data
            except (httpx.HTTPError, httpx.TimeoutException, zipfile.BadZipFile, ValueError) as exc:
                # Some Ministry file endpoints reset the connection after sending
                # the complete body.  Accept it only when ZIP CRC validation passes.
                data = b"".join(chunks)
                if data:
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:
                            if archive.testzip() is None:
                                return data
                    except zipfile.BadZipFile:
                        pass
                last = exc
                await asyncio.sleep(0.75 * (attempt + 1))
        raise RuntimeError(f"Resmî tarife arşivi indirilemedi: {type(last).__name__}") from last

    @staticmethod
    def _sheet_rows(data: bytes, extension: str) -> Iterable[tuple[str, list[tuple[Any, ...]]]]:
        if extension == ".xlsx":
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            for sheet in workbook.worksheets:
                yield sheet.title, [tuple(row) for row in sheet.iter_rows(values_only=True)]
        elif extension == ".xls":
            workbook = xlrd.open_workbook(file_contents=data)
            for sheet in workbook.sheets():
                yield sheet.name, [tuple(sheet.row_values(index)) for index in range(sheet.nrows)]

    @staticmethod
    def _docx_text(data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p[^>]*>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))

    @staticmethod
    def _group_map(file_name: str, sheet_name: str, family: str) -> tuple[int, dict[int, str], str, str | None]:
        file_key, sheet_key = _key(file_name), _key(sheet_name)
        if family == "additional_duty":
            if "calisma" in sheet_key:
                return -1, {}, "", None
            if "ek-1" in file_key:
                return 0, {2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7"}, "İGV Ek-1", None
            if _annex_sheet_is(sheet_key, 2):
                groups = {2: "AB/BK/B-HER/EFTA/F.ADA/G.KORE/MLZ", 3: "KOS", 4: "SNG", 5: "VNZ", 6: "BAE", 7: "EAGÜ", 8: "ÖTDÜ", 9: "GYÜ", 10: "DÜ"}
                return 0, groups, "İGV Ek-2", None
            if _annex_sheet_is(sheet_key, 3):
                groups = {2: "AB/BK/B-HER/EFTA/F.ADA", 3: "G.KORE", 4: "MLZ", 5: "SNG", 6: "KOS", 7: "İRAN", 8: "VNZ", 9: "BAE", 10: "EAGÜ", 11: "ÖTDÜ", 12: "GYÜ", 13: "DÜ"}
                return 0, groups, "İGV Ek-3", None
            return -1, {}, "", None

        if file_key.startswith(("ek-", "ek ")) or "tablo" in file_key:
            return -1, {}, "", None
        # Roman numerals share substrings ("iv say" contains "v say"), so every
        # list is matched on a word boundary and the longer names are tested first.
        if _list_name_is(file_key, "vii"):
            return 0, {3: "NİHAİ KULLANIM"}, "VII Sayılı Liste", "customs_duty_end_use"
        if _list_name_is(file_key, "vi"):
            return 2, {5: "NİHAİ KULLANIM"}, "VI Sayılı Liste", "customs_duty_end_use"
        if _list_name_is(file_key, "iv"):
            groups = {2: "EFTA/B-HER/F.ADA", 3: "AB/BK", 4: "G.KORE", 5: "MLZ", 6: "SNG", 7: "KOS", 8: "VNZ", 9: "BAE", 10: "TPS-OIC", 11: "D-8", 12: "DÜ", 13: "EFTA/F.ADA"}
            return 0, groups, "IV Sayılı Liste", None
        if _list_name_is(file_key, "v"):
            return 0, {5: "NİHAİ KULLANIM"}, "V Sayılı Liste", "customs_duty_suspension"
        if "iii say" in file_key:
            groups = {2: "AB/BK/B-HER/EFTA/F.ADA", 3: "G.KORE", 4: "MLZ", 5: "SNG", 6: "KOS", 7: "İRAN", 8: "VNZ", 9: "BAE", 10: "EAGÜ", 11: "ÖTDÜ", 12: "GYÜ", 13: "DÜ"}
            return 0, groups, "III Sayılı Liste", None
        if "ii say" in file_key and "04-24" in file_key:
            groups = {2: "AB/B-HER/BK/EFTA/F.ADA/G.KORE/MLZ", 3: "KOS", 4: "SNG", 5: "VNZ", 6: "BAE", 7: "EAGÜ", 8: "ÖTDÜ", 9: "GYÜ", 10: "DÜ"}
            return 0, groups, "II Sayılı Liste (Tarım)", None
        if "ii say" in file_key:
            return 0, {2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7"}, "II Sayılı Liste (Sanayi)", None
        if re.search(r"(^|\W)i say", file_key):
            groups = {2: "AB/BK", 3: "GÜR", 4: "B-HER", 5: "G.KORE", 6: "MLZ", 7: "SNG", 8: "KOS", 9: "VNZ", 10: "BAE", 11: "TPS-OIC", 12: "D-8", 13: "DÜ"}
            return 0, groups, "I Sayılı Liste", None
        return -1, {}, "", None

    @staticmethod
    def _country_metadata(archive: zipfile.ZipFile) -> dict[str, Any]:
        groups: dict[str, dict[str, str]] = {}
        sectors: dict[str, str] = {}
        for item in archive.infolist():
            if item.is_dir():
                continue
            filename = _zip_name(item)
            file_key = _key(filename)
            if file_key == "ek-1.xlsx":
                rows = next(TariffEngine._sheet_rows(archive.read(item), ".xlsx"))[1]
                current = ""
                for row in rows:
                    heading = _key(row[0] if row else "")
                    if "gelisme yolundaki" in heading:
                        current = "GYÜ"
                    elif "ozel tesvik" in heading:
                        current = "ÖTDÜ"
                    elif "en az gelismis" in heading:
                        current = "EAGÜ"
                    if not current:
                        continue
                    for country_col, exclusion_col in ((0, 1), (2, 3)):
                        if len(row) <= country_col:
                            continue
                        country = str(row[country_col] or "").strip()
                        country_key = _key(country)
                        if not country or "ulkeler" in country_key or country_key.startswith(("a-", "b-", "c-", "*")):
                            continue
                        groups[country_key] = {
                            "name": country,
                            "group": current,
                            "exclusions": str(row[exclusion_col] or "").strip() if len(row) > exclusion_col else "",
                        }
            elif file_key in {"ek-2.xls", "ek-3.xls", "ek-4.xls"}:
                extension = Path(filename).suffix.lower()
                for _, rows in TariffEngine._sheet_rows(archive.read(item), extension):
                    for row in rows:
                        if len(row) < 3:
                            continue
                        gtip = _normalise_gtip(row[2])
                        sector = str(row[0] or "").strip()
                        if gtip and sector:
                            sectors[f"{file_key}:{gtip}"] = sector
        return {"gts_countries": groups, "gts_sectors": sectors}

    @classmethod
    def _parse_archive(cls, data: bytes, source: dict[str, Any], archive_url: str, sha256: str, retrieved_at: str) -> _ParsedArchive:
        measures: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        family = source["measure_family"]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if family == "customs_duty":
                metadata = cls._country_metadata(archive)
            for item in archive.infolist():
                if item.is_dir():
                    continue
                filename = _zip_name(item)
                extension = Path(filename).suffix.lower()
                if extension not in {".xlsx", ".xls"}:
                    continue
                for sheet_name, rows in cls._sheet_rows(archive.read(item), extension):
                    code_col, group_columns, list_name, override_type = cls._group_map(filename, sheet_name, family)
                    if code_col < 0:
                        continue
                    for row_number, row in enumerate(rows, 1):
                        if len(row) <= code_col:
                            continue
                        gtip = _normalise_gtip(row[code_col])
                        if not gtip:
                            continue
                        footnote = str(row[code_col + 1] or "").strip()[:500] if len(row) > code_col + 1 else ""
                        description = ""
                        if override_type == "customs_duty_end_use":
                            description_col = 4 if code_col == 2 else 1
                            if len(row) > description_col:
                                description = str(row[description_col] or "").strip()[:2000]
                        elif override_type == "customs_duty_suspension" and len(row) > 4:
                            description = str(row[4] or "").strip()[:2000]
                        for column, group in group_columns.items():
                            if len(row) <= column:
                                continue
                            rate, rate_text = _number(row[column])
                            if not rate_text:
                                continue
                            measure_type = override_type or family
                            if family == "customs_duty" and list_name == "IV Sayılı Liste" and column == 13:
                                measure_type = "additional_financial_liability"
                            automatic = rate is not None and override_type is None and not footnote
                            condition = None
                            if override_type:
                                condition = "Bu oran yalnızca ilgili nihai kullanım/askıya alma şartları sağlanırsa uygulanabilir."
                            elif footnote:
                                condition = "Dipnot koşulu resmî karar ekinden doğrulanmalıdır."
                            measures.append(
                                {
                                    "gtip": gtip,
                                    "measure_type": measure_type,
                                    "rate": rate,
                                    "rate_text": rate_text,
                                    "country_group": group,
                                    "country_group_description": _GROUP_DESCRIPTIONS.get(group, group.replace("/", ", ")),
                                    "footnote": footnote or None,
                                    "description": description or None,
                                    "condition": condition,
                                    "list_name": list_name,
                                    "source_file": filename,
                                    "source_sheet": sheet_name,
                                    "source_row": row_number,
                                    "automatic_calculation_allowed": automatic,
                                }
                            )
        return _ParsedArchive(measures=measures, metadata=metadata)

    def _latest_snapshot_row(self, source_id: str | None = None) -> sqlite3.Row | None:
        with self._connect() as db:
            if source_id:
                return db.execute(
                    "SELECT * FROM tariff_snapshots WHERE source_id=? ORDER BY checked_at DESC LIMIT 1",
                    (source_id,),
                ).fetchone()
            return db.execute("SELECT * FROM tariff_snapshots ORDER BY checked_at DESC LIMIT 1").fetchone()

    async def _sync_source(self, source: dict[str, Any]) -> TariffSnapshot:
        archive_url = await self._discover_archive(source)
        data = await self._download_archive(archive_url)
        checksum = hashlib.sha256(data).hexdigest()
        snapshot_id = f"{source['id']}:{checksum[:20]}"
        checked_at = _now()
        with self._connect() as db:
            existing = db.execute("SELECT * FROM tariff_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if existing:
                status = row_review_fields(existing)["status"]
                db.execute("UPDATE tariff_snapshots SET checked_at=? WHERE id=?", (checked_at, snapshot_id))
                if status == "approved":
                    # Same archive as before: keep it live. Rejected or pending
                    # versions only get their checked_at refreshed.
                    db.execute("UPDATE tariff_snapshots SET active=1 WHERE id=?", (snapshot_id,))
                    db.execute("UPDATE tariff_snapshots SET active=0 WHERE source_id=? AND id<>?", (source["id"], snapshot_id))
                db.commit()
                return self._snapshot(existing, checked_at=checked_at, active=status == "approved")

        retrieved_at = checked_at
        parsed = await asyncio.to_thread(self._parse_archive, data, source, archive_url, checksum, retrieved_at)
        with self._connect() as db:
            previous = db.execute(
                "SELECT * FROM tariff_snapshots WHERE source_id=? AND status='approved' ORDER BY active DESC, retrieved_at DESC LIMIT 1",
                (source["id"],),
            ).fetchone()
            if previous is not None and previous["measure_count"] and len(parsed.measures) < 0.8 * int(previous["measure_count"]):
                parsed.warnings.append(
                    f"satır sayısı önceki sürüme göre %20'den fazla düştü ({previous['measure_count']} → {len(parsed.measures)})"
                )
            validity = derive_validity(
                [self._landing_text.get(source["id"], ""), json.dumps(parsed.metadata, ensure_ascii=False)],
                floor=source["valid_from"], context=source["id"],
            )
            parsed.warnings.extend(validity.warnings)
            # Inserted inactive; activation happens after the review decision below.
            db.execute(
                """INSERT INTO tariff_snapshots
                (id,source_id,source_title,landing_url,archive_url,archive_sha256,retrieved_at,checked_at,valid_from,measure_count,active,metadata_json,status,
                 valid_from_basis,gazette_date,gazette_number,legal_act)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,?,'pending_review',?,?,?,?)""",
                (
                    snapshot_id, source["id"], source["title"], source["landing_url"], archive_url, checksum,
                    retrieved_at, checked_at, validity.valid_from, len(parsed.measures),
                    json.dumps(parsed.metadata, ensure_ascii=False, separators=(",", ":")),
                    validity.basis, validity.gazette_date, validity.gazette_number, validity.legal_act,
                ),
            )
            for item in parsed.measures:
                identity = hashlib.sha256(
                    json.dumps([snapshot_id, item], sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                db.execute(
                    """INSERT INTO tariff_measures
                    (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,country_group_description,
                     footnote,description,condition_text,list_name,source_file,source_sheet,source_row,automatic_calculation_allowed)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identity, snapshot_id, item["gtip"], item["measure_type"], item["rate"], item["rate_text"],
                        item["country_group"], item["country_group_description"], item["footnote"], item["description"],
                        item["condition"], item["list_name"], item["source_file"], item["source_sheet"],
                        item["source_row"], int(item["automatic_calculation_allowed"]),
                    ),
                )
            db.commit()
            current_rows = self._measure_rows_by_key(db, snapshot_id)
            previous_rows = self._measure_rows_by_key(db, previous["id"]) if previous is not None else {}
        changes = self.diff_measure_rows(current_rows, previous_rows) if previous is not None else []
        summary = DiffSummary(
            total_rows=len(current_rows), previous_rows=len(previous_rows),
            added=sum(1 for c in changes if c["change_type"] == "added"),
            removed=sum(1 for c in changes if c["change_type"] == "removed"),
            modified=sum(1 for c in changes if c["change_type"] == "modified"),
        )
        decision = decide(self.review_policy, summary, parse_warnings=parsed.warnings, first_snapshot=previous is None)
        warnings_json, diff_json = review_metadata(summary, decision, parsed.warnings)
        with self._connect() as db:
            db.execute(
                "UPDATE tariff_snapshots SET status=?, parse_warnings_json=?, diff_summary_json=? WHERE id=?",
                (decision.status, warnings_json, diff_json, snapshot_id),
            )
            if not decision.pending:
                self._activate(db, source["id"], snapshot_id)
            db.commit()
            row = db.execute("SELECT * FROM tariff_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        self._record_ledger_batch(row, previous, parse_warnings=parsed.warnings, changes=changes, review_status=decision.status)
        if decision.pending:
            logger.info("Tariff snapshot %s waits for editorial review: %s", snapshot_id, "; ".join(decision.reasons))
        return self._snapshot(row)

    # ---------------------------------------------------------- temporal validity
    @staticmethod
    def _activate(db: sqlite3.Connection, source_id: str, snapshot_id: str) -> None:
        """Make ``snapshot_id`` the live version and close the interval of the one it replaces."""
        new = db.execute("SELECT * FROM tariff_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        previous = db.execute(
            "SELECT * FROM tariff_snapshots WHERE source_id=? AND active=1 AND id<>?", (source_id, snapshot_id)
        ).fetchall()
        for old in previous:
            valid_to, basis = close_previous(str(new["valid_from"]), str(old["valid_from"]), str(new["retrieved_at"]))
            db.execute("UPDATE tariff_snapshots SET valid_to=?, valid_to_basis=? WHERE id=?", (valid_to, basis, old["id"]))
        db.execute("UPDATE tariff_snapshots SET active=0 WHERE source_id=?", (source_id,))
        db.execute("UPDATE tariff_snapshots SET active=1, valid_to=NULL, valid_to_basis=NULL WHERE id=?", (snapshot_id,))

    def _select_snapshots(self, db: sqlite3.Connection, as_of: str | None) -> list[sqlite3.Row]:
        """Active snapshots for today; for an as-of date the approved version covering that day."""
        if not as_of or as_of >= today_iso():
            return db.execute("SELECT * FROM tariff_snapshots WHERE active=1 ORDER BY source_id").fetchall()
        rows = db.execute(
            "SELECT * FROM tariff_snapshots WHERE status='approved' ORDER BY source_id, retrieved_at DESC"
        ).fetchall()
        chosen: dict[str, sqlite3.Row] = {}
        for row in rows:
            if row["source_id"] in chosen or not covers(row, as_of):
                continue
            chosen[row["source_id"]] = row
        return [chosen[key] for key in sorted(chosen)]

    @staticmethod
    def _stamp_validity(result: "TariffLookupResult", snapshots: list[sqlite3.Row], as_of: str | None) -> None:
        result.as_of_date = as_of or today_iso()
        result.snapshot_validity = [snapshot_validity(row) for row in snapshots]
        if not snapshots:
            result.validity_basis = "unavailable"
            return
        bases = {validity_basis(row, as_of) for row in snapshots}
        if "observed" in bases:
            result.validity_basis = "observed"
        elif "legal" in bases:
            result.validity_basis = "legal"
        else:
            result.validity_basis = "current"
        if as_of and as_of < today_iso():
            if result.validity_basis == "observed":
                result.warnings.append(
                    f"{as_of} tarihi için seçilen sürümün sınırı yasal yürürlük tarihinden değil, indirme tarihlerinden türetildi; "
                    "o günkü resmî metin ayrıca doğrulanmalıdır."
                )
            else:
                result.warnings.append(f"Sonuç {as_of} tarihinde yürürlükte olan resmî sürüm(ler)den üretildi.")

    def backfill_validity(self) -> int:
        """Close open intervals of superseded approved snapshots (observed boundaries)."""
        updated = 0
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM tariff_snapshots WHERE status='approved' ORDER BY source_id, retrieved_at ASC"
            ).fetchall()
            by_source: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_source.setdefault(row["source_id"], []).append(row)
            for versions in by_source.values():
                for older, newer in zip(versions, versions[1:]):
                    if older["active"] or older["valid_to"]:
                        continue
                    valid_to, basis = close_previous(str(newer["valid_from"]), str(older["valid_from"]), str(newer["retrieved_at"]))
                    db.execute("UPDATE tariff_snapshots SET valid_to=?, valid_to_basis=? WHERE id=?", (valid_to, basis, older["id"]))
                    updated += 1
            db.commit()
        return updated

    # ---------------------------------------------------------- editorial review
    def _review_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "kind": "tariff", "snapshot_id": row["id"], "source_id": row["source_id"], "title": row["source_title"],
            "source_url": row["archive_url"], "sha256": row["archive_sha256"], "retrieved_at": row["retrieved_at"],
            "valid_from": row["valid_from"], "total_rows": int(row["measure_count"] or 0), "active": bool(row["active"]),
            "ledger_batch": batch_id_for("tariff", row["source_id"], row["id"]) if self.ledger is not None else None,
        }
        item.update(row_review_fields(row))
        return item

    def pending_reviews(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM tariff_snapshots WHERE status='pending_review' ORDER BY retrieved_at DESC"
            ).fetchall()
        return [self._review_item(row) for row in rows]

    def review_snapshot(self, snapshot_id: str, action: str, *, reviewed_by: str, note: str = "") -> dict[str, Any]:
        """Approve (activate, retire siblings) or reject (never activates again) a snapshot."""
        if action not in {"approve", "reject"}:
            raise ValueError("Karar 'approve' veya 'reject' olmalıdır.")
        now = _now()
        with self._connect() as db:
            row = db.execute("SELECT * FROM tariff_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            if action == "approve":
                db.execute(
                    "UPDATE tariff_snapshots SET status='approved', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (reviewed_by, now, note, snapshot_id),
                )
                self._activate(db, row["source_id"], snapshot_id)
            else:
                db.execute(
                    "UPDATE tariff_snapshots SET active=0, status='rejected', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (reviewed_by, now, note, snapshot_id),
                )
                if row["active"]:
                    fallback = db.execute(
                        "SELECT id FROM tariff_snapshots WHERE source_id=? AND status='approved' AND id<>? ORDER BY retrieved_at DESC LIMIT 1",
                        (row["source_id"], snapshot_id),
                    ).fetchone()
                    if fallback:
                        db.execute("UPDATE tariff_snapshots SET active=1 WHERE id=?", (fallback["id"],))
            db.commit()
            updated = db.execute("SELECT * FROM tariff_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return self._review_item(updated)

    # ---------------------------------------------------------- change ledger
    def _measure_rows_by_key(self, db: sqlite3.Connection, snapshot_id: str) -> dict[str, dict[str, Any]]:
        rows = db.execute(
            "SELECT gtip,measure_type,country_group,rate,rate_text,footnote,list_name FROM tariff_measures WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        return {_measure_row_key(row): {name: row[name] for name in _MEASURE_ROW_FIELDS} for row in rows}

    @staticmethod
    def diff_measure_rows(current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Row-level diff of two measure snapshots keyed by gtip|measure_type|country_group."""
        return diff_rows(current, previous, fields=("rate_text", "footnote"))

    def _record_ledger_batch(
        self,
        snapshot: sqlite3.Row,
        previous: sqlite3.Row | None,
        *,
        parse_warnings: Iterable[str] = (),
        backfilled: bool = False,
        changes: list[dict[str, Any]] | None = None,
        review_status: str | None = None,
    ) -> str | None:
        """Write the transition previous → snapshot to the unified ledger (idempotent)."""
        if self.ledger is None or snapshot is None:
            return None
        with self._connect() as db:
            current_rows = self._measure_rows_by_key(db, snapshot["id"])
            if changes is None:
                previous_rows = self._measure_rows_by_key(db, previous["id"]) if previous is not None else {}
                changes = self.diff_measure_rows(current_rows, previous_rows) if previous is not None else []
        try:
            return self.ledger.record_batch(
                kind="tariff",
                source_id=snapshot["source_id"],
                title=snapshot["source_title"],
                new_snapshot_id=snapshot["id"],
                old_snapshot_id=previous["id"] if previous is not None else None,
                source_url=snapshot["archive_url"],
                sha256=snapshot["archive_sha256"],
                changes=changes,
                total_rows=len(current_rows),
                parse_warnings=parse_warnings,
                detected_at=snapshot["retrieved_at"],
                review_status=review_status or row_review_fields(snapshot)["status"],
                valid_from=snapshot["valid_from"],
                backfilled=backfilled,
            )
        except Exception as exc:  # noqa: BLE001 – the ledger must never break a sync
            logger.warning("Tariff change ledger write failed: %s", exc)
            return None

    def backfill_ledger(self) -> int:
        """Record every historical snapshot transition that the ledger does not know yet."""
        if self.ledger is None:
            return 0
        written = 0
        with self._connect() as db:
            snapshots = db.execute("SELECT * FROM tariff_snapshots ORDER BY source_id, retrieved_at ASC").fetchall()
        by_source: dict[str, list[sqlite3.Row]] = {}
        for row in snapshots:
            by_source.setdefault(row["source_id"], []).append(row)
        for versions in by_source.values():
            previous = None
            for snapshot in versions:
                if not self.ledger.has_batch(batch_id_for("tariff", snapshot["source_id"], snapshot["id"])):
                    if self._record_ledger_batch(snapshot, previous, backfilled=True):
                        written += 1
                previous = snapshot
        return written

    async def sync(self, *, force: bool = False) -> TariffSyncStatus:
        async with self._sync_lock:
            latest = self._latest_snapshot_row()
            if not force and latest:
                age = time.time() - datetime.fromisoformat(latest["checked_at"]).timestamp()
                if age < self.sync_interval_seconds and self.status().ready:
                    return self.status()
            self._syncing = True
            self._errors = []
            try:
                for source in self.sources:
                    try:
                        await self._sync_source(source)
                    except Exception as exc:
                        self._errors.append(f"{source['id']}: {type(exc).__name__}: {exc}")
            finally:
                self._syncing = False
            return self.status()

    async def periodic_sync_loop(self) -> None:
        """Refresh at startup and on the configured cadence without blocking ASGI startup."""
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors.append(f"periodic_sync: {type(exc).__name__}: {exc}")
            await asyncio.sleep(self.sync_interval_seconds)

    @staticmethod
    def _snapshot(row: sqlite3.Row, *, checked_at: str | None = None, active: bool | None = None) -> TariffSnapshot:
        return TariffSnapshot(
            id=row["id"], source_id=row["source_id"], source_title=row["source_title"], landing_url=row["landing_url"],
            archive_url=row["archive_url"], archive_sha256=row["archive_sha256"], retrieved_at=row["retrieved_at"],
            checked_at=checked_at or row["checked_at"], valid_from=row["valid_from"], measure_count=row["measure_count"],
            active=bool(row["active"]) if active is None else active,
            valid_to=row["valid_to"] if "valid_to" in row.keys() else None,
            valid_from_basis=(row["valid_from_basis"] if "valid_from_basis" in row.keys() else None) or "config",
            valid_to_basis=row["valid_to_basis"] if "valid_to_basis" in row.keys() else None,
        )

    def distinct_gtip_codes(self, *, width: int = 12) -> list[str]:
        """Yürürlükteki tarife cetvelinde geçen tekil GTİP kodları (yurt dışı karşılaştırma dolumu için)."""
        width = max(4, min(int(width or 12), 12))
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT substr(m.gtip,1,?) AS code FROM tariff_measures m "
                "JOIN tariff_snapshots s ON s.id=m.snapshot_id WHERE s.active=1 ORDER BY code",
                (width,),
            ).fetchall()
        return [row["code"] for row in rows if row["code"]]

    def status(self) -> TariffSyncStatus:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tariff_snapshots WHERE active=1 ORDER BY source_id").fetchall()
            count = db.execute(
                "SELECT COUNT(*) FROM tariff_measures m JOIN tariff_snapshots s ON s.id=m.snapshot_id WHERE s.active=1"
            ).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM tariff_snapshots WHERE status='pending_review'").fetchone()[0]
        snapshots = [self._snapshot(row) for row in rows]
        return TariffSyncStatus(
            pending_review_count=int(pending),
            review_mode=self.review_policy.mode,
            ready=bool(snapshots) and all(item.measure_count > 0 for item in snapshots),
            syncing=self._syncing,
            last_checked_at=max((item.checked_at for item in snapshots), default=None),
            active_snapshots=snapshots,
            measure_count=count,
            errors=list(self._errors),
            sync_interval_seconds=self.sync_interval_seconds,
        )

    # --- GTS (Genelleştirilmiş Tercihler Sistemi) kapsam teşhisi ----------------
    def gts_coverage(self) -> dict[str, Any]:
        """Report the official GTS country table and whether each row is reachable.

        Bu tablo bugüne kadar hiçbir uçta görünmüyordu ve sessiz bir hata sınıfı
        doğuruyordu: resmî ekte GTS'li olan bir ülkenin adı ``countries.py``
        kayıt defterinde çözülemezse, kullanıcı o ülkenin yaygın bir yazımını
        girdiğinde eşleşme olmaz ve sorgu "Diğer Ülkeler" sütununa düşer — yani
        **olması gerekenden yüksek vergi** gösterilir. Hata, ürünün hiçbir yerinde
        belirti vermez. Bu metot her satırı tek tek çözmeye çalışır ve
        çözülemeyenleri açıkça listeler.

        `resolved`, adın kayıt defterinde bir ülkeye bağlandığını gösterir; oranın
        doğruluğunu değil. Resmî ekteki yazımla yapılan sorgu her hâlükârda
        çalışır, çünkü tablo o yazımla anahtarlanmıştır.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT metadata_json FROM tariff_snapshots WHERE active=1 ORDER BY source_id"
            ).fetchall()
        metadata: dict[str, Any] = {}
        for row in rows:
            metadata.update(json.loads(row["metadata_json"] or "{}"))
        table = metadata.get("gts_countries", {}) or {}
        countries: list[dict[str, Any]] = []
        for key, item in sorted(table.items(), key=lambda pair: str(pair[1].get("name", ""))):
            name = str(item.get("name", "") or "")
            match = find_country(name)
            countries.append({
                "official_name": name,
                "official_key": key,
                "group": item.get("group"),
                "exclusions": item.get("exclusions", ""),
                "resolved": match is not None,
                "registry_key": match.key if match else None,
                "iso2": match.iso2 if match else None,
            })
        unresolved = [item["official_name"] for item in countries if not item["resolved"]]
        return {
            "total": len(countries),
            "resolved": len(countries) - len(unresolved),
            "unresolved": unresolved,
            "groups": {
                group: sum(1 for item in countries if item["group"] == group)
                for group in ("EAGÜ", "ÖTDÜ", "GYÜ")
            },
            "sector_exclusion_rows": len(metadata.get("gts_sectors", {}) or {}),
            "countries": countries,
            "note": (
                "GTS ülke listesi yürürlükteki İthalat Rejimi Kararı ekinden okunur. "
                "‘resolved=false’ olan satır, adın ülke kayıt defterinde karşılığı olmadığını "
                "gösterir: kullanıcı o ülkeyi farklı yazarsa sorgu ‘Diğer Ülkeler’ sütununa düşer "
                "ve vergi olduğundan yüksek çıkar. Sektör istisnaları her sorguda ayrıca uyarı üretir."
            ),
        }

    @staticmethod
    def _matching_group(
        origin: str,
        labels: set[str],
        metadata: dict[str, Any],
        gtip: str,
        measure_type: str = "customs_duty",
    ) -> tuple[str | None, list[str]]:
        """Pick the official column for one origin deterministically.

        Column labels such as ``AB/BK/B-HER/EFTA/F.ADA`` are split into tokens and a
        country is matched only against whole tokens, never substrings.  Labels are
        visited in sorted order so the result never depends on set iteration order.
        Priority: the country's own column (G.KORE, MLZ, B-HER, BK...), then the
        numeric column 1/2/3 of the industrial tables, then the shared AB / EFTA
        column, then GTS groups, and only then the residual "Diğer Ülkeler" column.
        A preferential country that has no column in the queried list is reported
        with a warning instead of being silently attached to the EU column.
        """
        warnings: list[str] = []
        if not str(origin or "").strip():
            return None, warnings
        country = find_country(origin)
        origin_key = country.key if country else _key(origin).strip()
        if country is None:
            warnings.append(
                f"Menşe ülke '{origin}' bilinen ülke listesinde bulunamadı; 'Diğer Ülkeler' sütunu varsayıldı. "
                "Adı Türkçe yazın (ör. Almanya, Çin, Güney Kore)."
            )
        elif country.key in PENDING_AGREEMENTS:
            warnings.append(PENDING_AGREEMENTS[country.key])
        ordered = sorted(labels)
        tokens = {label: {_key(part).strip() for part in label.split("/")} for label in ordered}

        def with_token(token: str) -> str | None:
            wanted = _key(token).strip()
            for label in ordered:
                if wanted in tokens[label]:
                    return label
            return None

        is_eu = origin_key in _EU_COUNTRIES
        is_efta = origin_key in _EFTA_COUNTRIES
        is_column_1 = origin_key in _COLUMN_1_COUNTRIES
        explicit = _EXPLICIT_LABELS.get(origin_key)
        if explicit:
            own = with_token(explicit)
            if own:
                return own, warnings
        if (is_eu or is_efta or is_column_1) and "1" in labels:
            return "1", warnings
        if origin_key in {"katar", "qatar"} and "2" in labels:
            return "2", warnings
        if origin_key in {"birlesik arap emirlikleri", "bae", "united arab emirates"} and "3" in labels:
            return "3", warnings
        if is_eu:
            shared = with_token("AB")
            if shared:
                return shared, warnings
        if is_efta:
            shared = with_token("EFTA")
            if shared:
                return shared, warnings
        if is_eu or is_efta or is_column_1 or explicit:
            if measure_type == "additional_financial_liability":
                # No EMY column for this origin means the liability does not apply.
                return None, warnings
            warnings.append(
                f"{origin} için sorgulanan listede ayrı tercihli sütun yok; anlaşma tavizi varsa "
                "anlaşma ekinden/tarife kontenjanından doğrulanmalıdır. 'Diğer Ülkeler' sütunu gösterildi."
            )

        gts_table = metadata.get("gts_countries", {})
        gts = gts_table.get(origin_key)
        if gts is None and country is not None:
            for alias in country.keys:
                gts = gts_table.get(alias) or gts_table.get(_key(alias))
                if gts:
                    break
        if gts:
            group = gts["group"]
            exclusions = gts.get("exclusions", "")
            if exclusions:
                sector_map = metadata.get("gts_sectors", {})
                prefix = {"GYÜ": "ek-2.xls", "ÖTDÜ": "ek-3.xls", "EAGÜ": "ek-4.xls"}[group]
                sector = sector_map.get(f"{prefix}:{gtip}")
                if sector and any(_key(part).strip() == _key(sector).strip() for part in exclusions.split(",")):
                    warnings.append(f"{origin} için {sector} sektörü GTS istisnasında; diğer ülkeler sütunu kullanıldı.")
                    return "7" if "7" in labels else ("DÜ" if "DÜ" in labels else None), warnings
                warnings.append(f"{origin} GTS ülkesidir; ürün sektörü istisnası ‘{exclusions}’ ayrıca doğrulanmalıdır.")
            if group in labels:
                return group, warnings
            numeric = {"EAGÜ": "4", "ÖTDÜ": "5", "GYÜ": "6"}[group]
            if numeric in labels:
                return numeric, warnings

        # Only use the residual group when the origin is not a listed preference
        # country in the official snapshot. This is a deterministic set complement.
        if "7" in labels:
            return "7", warnings
        if "DÜ" in labels:
            return "DÜ", warnings
        return None, warnings

    def _measure(self, row: sqlite3.Row, snapshot: sqlite3.Row) -> TariffMeasure:
        return TariffMeasure(
            gtip=row["gtip"], measure_type=row["measure_type"], rate=row["rate"], rate_text=row["rate_text"],
            country_group=row["country_group"], country_group_description=row["country_group_description"],
            footnote=row["footnote"], description=row["description"], condition=row["condition_text"],
            list_name=row["list_name"], source_id=snapshot["source_id"], source_title=snapshot["source_title"],
            source_url=snapshot["landing_url"], archive_url=snapshot["archive_url"],
            archive_sha256=snapshot["archive_sha256"], source_file=_repair_zip_name(row["source_file"]), source_sheet=row["source_sheet"],
            source_row=row["source_row"], valid_from=snapshot["valid_from"], retrieved_at=snapshot["retrieved_at"],
            snapshot_id=snapshot["id"], automatic_calculation_allowed=bool(row["automatic_calculation_allowed"]),
        )

    def _trade_coverage(self, kind: str, default_note: str) -> MeasureCoverage:
        engine = self.trade_measures
        if engine is None:
            return MeasureCoverage(status="not_integrated", note=default_note)
        meta = engine.store.metadata(kind)
        if meta.get("origin") == "missing":
            return MeasureCoverage(status="not_integrated", note=default_note)
        stamp = meta.get("fetched_at") or "depo tohum verisi"
        return MeasureCoverage(
            status="partial_snapshot",
            source_ids=[f"trade_measures:{kind}"],
            note=(
                f"{meta.get('source_label')} ({stamp}); GTİP ön ekiyle eşleşen resmî satırlar gösterilir, "
                "oran/tutar kullanıcı doğrulamasıyla hesaba girer."
            ),
        )

    def _attach_excise_tax(self, result: "TariffLookupResult") -> None:
        index = self.excise_tax
        if index is None:
            return
        try:
            report = index.lookup(result.gtip)
        except Exception:  # noqa: BLE001 – ÖTV verisi tarife sonucunu düşürmemeli
            logger.exception("Excise tax lookup failed for %s", result.gtip)
            return
        result.excise_tax = report
        result.warnings.extend(report.get("warnings", []))

    def _attach_vat_rate(self, result: "TariffLookupResult") -> None:
        """2007/13033 ekli listelerden KDV oranı önerisi ekler; oran hesaba otomatik girmez."""
        index = getattr(self, "vat_rates", None)
        if index is None:
            return
        try:
            report = index.lookup(result.gtip)
        except Exception:  # noqa: BLE001 – KDV önerisi tarife sonucunu düşürmemeli
            logger.exception("VAT rate lookup failed for %s", result.gtip)
            return
        result.vat_rate = report
        if report.get("ambiguous"):
            options = " / ".join(
                f"%{candidate['rate']:g}" + (f" ({', '.join(candidate['conditions'])})" if candidate.get("conditions") else "")
                for candidate in report.get("candidates", [])
                if candidate.get("rate") is not None
            )
            result.warnings.append(
                f"KDV önerisi belirsiz: {options} [{report.get('legal_basis')}]. Satırdaki şartı doğrulayıp oranı kendiniz girin."
            )
        elif report.get("basis") == "official_list" and report.get("rate") is not None:
            result.warnings.append(
                f"KDV önerisi %{report['rate']:g} – {report.get('legal_basis')}"
                + (f" (eşleşen ifade {report['matched_expression']})" if report.get("matched_expression") else "")
                + "; oran otomatik uygulanmaz, beyanname öncesi doğrulayın."
            )

    def _attach_trade_measures(self, result: "TariffLookupResult") -> None:
        self._attach_excise_tax(result)
        self._attach_vat_rate(result)
        engine = self.trade_measures
        if engine is None:
            return
        try:
            as_of_day = parse_iso_date(result.as_of_date) if result.as_of_date else None
            if as_of_day is not None and as_of_day < date.today():
                report = engine.lookup(result.gtip, result.origin_country, today=as_of_day)
            else:
                report = engine.lookup(result.gtip, result.origin_country)
        except Exception:  # noqa: BLE001 – önlem verisi tarife sonucunu düşürmemeli
            logger.exception("Trade measure lookup failed for %s", result.gtip)
            return
        result.trade_measures = report.as_dict()
        for hit in report.applicable_anti_dumping[:3]:
            label = "sübvansiyona karşı" if hit.measure_type == "countervailing" else "dampinge karşı"
            result.warnings.append(
                f"Resmî listede {hit.country} menşeli '{hit.product}' ({hit.matched_code}) için {label} önlem var: "
                f"{hit.rate_text} [{hit.legal_act}]. Oran/tutar firma bazlı olabilir; tebliğ metniyle doğrulayın."
            )
        for hit in [h for h in report.safeguard if h.status != "expired" and h.origin_match is not False][:3]:
            result.warnings.append(
                f"Korunma önlemi kapsamı: '{hit.product}' ({hit.matched_code}) – {hit.rate_text} [{hit.legal_act}]. "
                + (hit.notes or "")
            )
        for hit in report.surveillance[:3]:
            result.warnings.append(
                f"Gözetim tebliği kapsamı: {hit.matched_code} '{hit.product}' birim gümrük kıymeti {hit.rate_text} {hit.unit or ''} "
                f"[{hit.legal_act}]; gözetim belgesi yoksa kıymet bu eşiğe yükseltilir."
            )

    def _measure_coverage(self, snapshots: list[sqlite3.Row]) -> dict[str, MeasureCoverage]:
        active = {str(snapshot["source_id"]) for snapshot in snapshots}
        vat_index = getattr(self, "vat_rates", None)
        vat_ready = bool(vat_index is not None and getattr(vat_index, "ready", False))
        return {
            "customs_duty": MeasureCoverage(
                status="verified_snapshot" if "import_regime" in active else "not_integrated",
                source_ids=["import_regime"] if "import_regime" in active else [],
                note="Menşe sütunu, dipnot ve alt GTİP birliği sağlanırsa otomatik kullanılabilir.",
            ),
            "additional_duty": MeasureCoverage(
                status="verified_snapshot" if "additional_duty" in active else "not_integrated",
                source_ids=["additional_duty"] if "additional_duty" in active else [],
                note="İGV, menşe ve bütün GTİP12 alt satırları aynı oranı verirse otomatik kullanılabilir.",
            ),
            "additional_financial_liability": MeasureCoverage(
                status="partial_snapshot" if "import_regime" in active else "not_integrated",
                source_ids=["import_regime"] if "import_regime" in active else [],
                note="Konsolide cetveldeki IV sayılı liste okunur; diğer ek mali yükümlülük kararları ayrıca doğrulanmalıdır.",
            ),
            "anti_dumping": self._trade_coverage("anti_dumping", "Ürün, menşe ve üretici/ihracatçı bazlı güncel damping/sübvansiyon önlemi henüz yapılandırılmış hesap motorunda değildir."),
            "surveillance": self._trade_coverage("surveillance", "Gözetim tebliği, birim kıymet, menşe ve yürürlük tarihi ayrıca doğrulanmalıdır."),
            "safeguard": self._trade_coverage("safeguard", "Korunma önlemi ve varsa ülke/istisna kapsamı ayrıca doğrulanmalıdır."),
            "tariff_quota": self._trade_coverage("tariff_quota", "Tarife kontenjanı tahsis ve bakiye durumu işlem tarihinde ayrıca doğrulanmalıdır."),
            "vat": MeasureCoverage(
                status="partial_snapshot" if vat_ready else "user_confirmation_required",
                source_ids=["vat_lists"] if vat_ready else [],
                note=(
                    "2007/13033 sayılı Karar eki (I)/(II) sayılı listelerden resmî listeden öneri üretilir; "
                    "kullanıcı onayı gerekir. Oran hesaba otomatik girmez, şart ve istisnalar doğrulanmalıdır."
                    if vat_ready
                    else "Ürüne özgü güncel KDV oranı resmî kaynaktan doğrulanıp girilmelidir; kullanıcı onayı gerekir."
                ),
            ),
            "kkdf": MeasureCoverage(
                status="user_confirmation_required",
                note="Ödeme şekli ve istisnaya göre KKDF oranı doğrulanıp girilmelidir.",
            ),
            "sct": MeasureCoverage(
                status="partial_snapshot" if self.excise_tax is not None and self.excise_tax.ready else "user_confirmation_required",
                source_ids=["excise_tax_lists"] if self.excise_tax is not None and self.excise_tax.ready else [],
                note=(
                    "4760 sayılı Kanunun ekli listelerinden ÖTV kapsamı otomatik belirlenir; "
                    "yürürlükteki oran/tutar Cumhurbaşkanı kararıyla değişebileceğinden matrah ve "
                    "tutar doğrulanıp girilmelidir."
                ),
            ),
        }

    async def lookup(
        self,
        gtip: str,
        *,
        origin_country: str | None = None,
        dispatch_country: str | None = None,
        atr_certificate: bool | None = None,
        auto_sync: bool = True,
        as_of: str | None = None,
    ) -> TariffLookupResult:
        normalised = _normalise_gtip(gtip)
        if not normalised or len(normalised) not in {4, 6, 8, 10, 12}:
            raise ValueError("Tarife sorgusu için 4, 6, 8, 10 veya 12 haneli HS/CN/GTİP kodu gereklidir.")
        as_of = normalise_as_of(as_of)
        match_mode: Literal["exact", "prefix"] = "exact" if len(normalised) == 12 else "prefix"
        if auto_sync and not self.status().ready:
            await self.sync()
        with self._connect() as db:
            snapshots = self._select_snapshots(db, as_of)
            if not snapshots:
                message = (
                    f"{as_of} tarihini kapsayan onaylı resmî tarife sürümü arşivde yok."
                    if as_of and as_of < today_iso() else "Resmî tarife tabloları henüz eşitlenmedi."
                )
                result = TariffLookupResult(status="unavailable", gtip=normalised, origin_country=origin_country, dispatch_country=dispatch_country, as_of=_now(), warnings=[message])
                self._stamp_validity(result, [], as_of)
                self._attach_trade_measures(result)
                return result
            all_rows: list[tuple[sqlite3.Row, sqlite3.Row]] = []
            metadata: dict[str, Any] = {}
            for snapshot in snapshots:
                metadata.update(json.loads(snapshot["metadata_json"] or "{}"))
                if match_mode == "exact":
                    rows = db.execute(
                        "SELECT * FROM tariff_measures WHERE snapshot_id=? AND gtip=? ORDER BY gtip,measure_type,list_name,country_group",
                        (snapshot["id"], normalised),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM tariff_measures WHERE snapshot_id=? AND gtip LIKE ? ORDER BY gtip,measure_type,list_name,country_group",
                        (snapshot["id"], f"{normalised}%"),
                    ).fetchall()
                all_rows.extend((row, snapshot) for row in rows)
        if not all_rows:
            coverage = self._measure_coverage(snapshots)
            result = TariffLookupResult(
                status="not_found", gtip=normalised, match_mode=match_mode, origin_country=origin_country,
                dispatch_country=dispatch_country,
                snapshots=[self._snapshot(row) for row in snapshots], measure_coverage=coverage,
                unresolved_measure_types=[key for key, item in coverage.items() if item.status != "verified_snapshot"],
                as_of=_now(),
                warnings=["Bu kod aktif resmî tarife/İGV tablolarında bulunamadı; kod ve fasıl doğrulaması gerekir."],
            )
            self._stamp_validity(result, snapshots, as_of)
            self._attach_trade_measures(result)
            return result

        matched_gtips = sorted({row["gtip"] for row, _ in all_rows})
        selected_by_scope: dict[tuple[str, str, str], str | None] = {}
        warnings: list[str] = []
        origin_entry = find_country(origin_country) if origin_country else None
        dispatch_entry = find_country(dispatch_country) if dispatch_country else None
        if dispatch_country and dispatch_entry is None:
            warnings.append(f"Sevk ülkesi '{dispatch_country}' tanınmadı; A.TR/serbest dolaşım değerlendirmesi yapılmadı.")
        origin_is_eu = bool(origin_entry and origin_entry.regime == "eu")
        dispatch_is_eu = bool(dispatch_entry and dispatch_entry.regime == "eu")
        # Third-country goods in free circulation in the EU: with an A.TR the customs duty
        # follows the EU column, while İGV/EMY still follow the real origin.  The relief is
        # applied only when the A.TR movement certificate is confirmed; goods merely dispatched
        # from the EU (transit, no A.TR) stay on their origin column.
        atr_available = bool(dispatch_is_eu and not origin_is_eu and all(atr_eligible(code) for code in matched_gtips))
        atr_route = atr_available and atr_certificate is True
        origin_proof_required: list[str] = []
        fallback_rates: dict[str, float] = {}
        # The column is chosen per measure type: in the IV list the EMY column
        # carries its own label, so a duty column and an EMY column can both be
        # primary for the same GTIP without competing with each other.
        scopes = {(row["gtip"], snapshot["id"], row["measure_type"]) for row, snapshot in all_rows}
        for matched_gtip, snapshot_id, measure_type in sorted(scopes):
            labels = {
                row["country_group"]
                for row, snapshot in all_rows
                if row["gtip"] == matched_gtip and snapshot["id"] == snapshot_id and row["measure_type"] == measure_type
            }
            column_origin = origin_country or ""
            if atr_route and measure_type == "customs_duty":
                column_origin = dispatch_entry.name if dispatch_entry else column_origin
            selected_group, group_warnings = self._matching_group(
                column_origin, labels, metadata, matched_gtip, measure_type=measure_type
            )
            selected_by_scope[(matched_gtip, snapshot_id, measure_type)] = selected_group
            warnings.extend(group_warnings)
            if (
                measure_type in {"additional_duty", "additional_financial_liability"}
                and selected_group
                and (origin_is_eu or (origin_entry and origin_entry.regime in {"efta", "fta", "pta"}))
            ):
                residual = "7" if "7" in labels else ("DÜ" if "DÜ" in labels else None)
                if residual and residual != selected_group:
                    if measure_type not in origin_proof_required:
                        origin_proof_required.append(measure_type)
                    for row, snapshot in all_rows:
                        if (
                            row["gtip"] == matched_gtip and snapshot["id"] == snapshot_id
                            and row["measure_type"] == measure_type and row["country_group"] == residual
                            and row["rate"] is not None
                        ):
                            fallback_rates[measure_type] = max(fallback_rates.get(measure_type, 0.0), float(row["rate"]))
        warnings = list(dict.fromkeys(warnings))
        selected_groups = {group for group in selected_by_scope.values() if group}
        selected = " / ".join(sorted(selected_groups)) if selected_groups else None
        groups_by_type: dict[str, set[str]] = {}
        for (_, _, measure_type), group in selected_by_scope.items():
            if group:
                groups_by_type.setdefault(measure_type, set()).add(group)
        for measure_type, groups in sorted(groups_by_type.items()):
            if len(groups) > 1:
                warnings.append(
                    f"Resmî tablolar {measure_type} için aynı menşeye farklı sütun etiketleri kullanıyor: "
                    + " / ".join(sorted(groups))
                )
        if not as_of and any(int(str(snapshot["valid_from"])[:4]) < datetime.now().year for snapshot in snapshots):
            warnings.append(
                "Aktif tarife snapshot'ı cari yıldan eskidir; oran otomatik karar için kullanılmadan önce yıllık cetvel güncellemesi doğrulanmalıdır."
            )
        primary: list[TariffMeasure] = []
        conditional: list[TariffMeasure] = []
        alternatives: list[TariffMeasure] = []
        for row, snapshot in all_rows:
            measure = self._measure(row, snapshot)
            if measure.measure_type in {"customs_duty_suspension", "customs_duty_end_use"}:
                conditional.append(measure)
            elif selected_by_scope.get((measure.gtip, measure.snapshot_id, measure.measure_type)) == measure.country_group:
                primary.append(measure)
            else:
                alternatives.append(measure)
        if atr_route:
            warnings.append(
                f"Sevk ülkesi {dispatch_entry.name} (AB) ve menşe {origin_country}: A.TR beyan edildiği için gümrük vergisi "
                "serbest dolaşım sütunundan alındı; belge gümrükte ibraz edilmezse menşe ülkesi sütunu uygulanır. "
                "İGV ve ek mali yükümlülük menşe ülkesi sütunundan uygulanır."
            )
        elif atr_available:
            warnings.append(
                f"Sevk ülkesi {dispatch_entry.name} (AB), menşe {origin_country}: A.TR teyit edilmediği için gümrük vergisi "
                "menşe ülkesi sütunundan uygulandı. Eşya AB'de serbest dolaşımdaysa ve A.TR ibraz edilecekse sorguyu "
                "A.TR beyanıyla yenileyin; gümrük vergisi serbest dolaşım sütunundan hesaplanır."
            )
        elif dispatch_is_eu and not origin_is_eu:
            warnings.append(
                f"Sevk ülkesi {dispatch_entry.name} (AB) olsa da bu tarife satırı için A.TR düzenlenmez (tarım/AKÇT ürünü); "
                "gümrük vergisi menşe ülkesine göre uygulandı."
            )
        if origin_proof_required:
            fallback_text = ", ".join(
                f"{_MEASURE_LABELS.get(item, item)} %{fallback_rates[item]:g}" for item in origin_proof_required if item in fallback_rates
            )
            warnings.append(
                "İGV/EMY için tercihli sütun, eşyanın AB/Türkiye veya anlaşma ülkesi menşeli olduğunun tedarikçi beyanı, "
                "menşe şahadetnamesi ya da anlaşma menşe belgesiyle tevsikine bağlıdır; A.TR tek başına menşe ispatı değildir. "
                + (f"Tevsik yoksa 'Diğer Ülkeler' sütunu uygulanır: {fallback_text}." if fallback_text else "Tevsik yoksa 'Diğer Ülkeler' sütunu uygulanır.")
            )
        if not origin_country:
            warnings.append("Menşe ülke verilmediği için uygulanacak ülke sütunu seçilmedi.")
        elif not selected:
            warnings.append("Menşe ülke resmî ülke gruplarına güvenle eşlenemedi; oran otomatik seçilmedi.")
        if any(item.footnote for item in primary):
            warnings.append("Seçilen oranlardan en az biri dipnotludur; otomatik maliyet hesabından önce dipnot şartı doğrulanmalıdır.")

        calculable_types = ("customs_duty", "additional_duty", "additional_financial_liability")
        rate_variants: dict[str, list[float]] = {}
        unambiguous_rates: dict[str, float] = {}
        ambiguous_measure_types: list[str] = []
        matched_gtip_set = set(matched_gtips)
        for measure_type in calculable_types:
            typed = [item for item in primary if item.measure_type == measure_type]
            if not typed:
                continue
            valid = [
                item for item in typed
                if item.automatic_calculation_allowed and item.rate is not None and not item.footnote
            ]
            values = sorted({float(item.rate) for item in valid if item.rate is not None})
            rate_variants[measure_type] = values
            covered_gtips = {item.gtip for item in valid}
            has_unsafe_row = len(valid) != len(typed)
            if len(values) == 1 and covered_gtips == matched_gtip_set and not has_unsafe_row:
                unambiguous_rates[measure_type] = values[0]
            else:
                ambiguous_measure_types.append(measure_type)

        if match_mode == "prefix":
            warnings.insert(
                0,
                f"{len(normalised)} haneli kod {len(matched_gtips)} adet 12 haneli Türk GTİP satırıyla eşleşti.",
            )
            if ambiguous_measure_types:
                warnings.append(
                    "Alt GTİP satırlarında oran veya önlemin varlığı değişiyor; kesin alt GTİP seçilmeden bu kalemler maliyete otomatik alınmadı."
                )
            elif unambiguous_rates:
                warnings.append(
                    "Gösterilen otomatik oranlar bütün eşleşen 12 haneli satırlarda aynıdır; beyan öncesinde kesin 12 haneli GTİP yine doğrulanmalıdır."
                )
        coverage = self._measure_coverage(snapshots)
        lookup_status = "matched" if primary and not ambiguous_measure_types else "partial"
        if origin_country and selected and lookup_status:
            if (
                "additional_duty" not in rate_variants
                and "additional_duty" not in ambiguous_measure_types
                and coverage.get("additional_duty")
                and coverage["additional_duty"].status == "verified_snapshot"
            ):
                warnings.append("Bu GTİP İlave Gümrük Vergisi (İGV) ekli listelerinde yer almamaktadır; İGV uygulanmaz (%0).")
            safeguards = []
            if self.trade_measures:
                try:
                    if hasattr(self.trade_measures, "lookup"):
                        safeguards = getattr(self.trade_measures.lookup(normalised, origin_country), "safeguard", [])
                    elif hasattr(self.trade_measures, "measures_for_gtip"):
                        safeguards = self.trade_measures.measures_for_gtip(normalised, origin=origin_country).get("safeguard") or []
                except Exception:
                    safeguards = []
            has_live_safeguard = any(
                (getattr(hit, "status", None) or (hit.get("status") if isinstance(hit, dict) else None)) != "expired"
                and (getattr(hit, "origin_match", None) or (hit.get("origin_match") if isinstance(hit, dict) else None)) is not False
                for hit in safeguards
            )
            if (
                "additional_financial_liability" not in rate_variants
                and "additional_financial_liability" not in ambiguous_measure_types
                and not has_live_safeguard
            ):
                warnings.append("Bu GTİP için ek mali yükümlülük (EMY) tespit edilmemiştir; EMY uygulanmaz (%0).")
        unresolved = [key for key, item in coverage.items() if item.status != "verified_snapshot"]
        if unresolved:
            warnings.append(
                "Bu sonuç bütün ticaret politikası ve iç vergi kalemlerinin doğrulandığı anlamına gelmez; "
                "kapsam matrisi 'partial/not_integrated/user_confirmation_required' kalemlerini ayrı gösterir."
            )
        result = TariffLookupResult(
            status="matched" if primary and not ambiguous_measure_types else "partial",
            gtip=normalised, match_mode=match_mode, matched_gtips=matched_gtips[:500],
            matched_gtip_count=len(matched_gtips), origin_country=origin_country,
            dispatch_country=dispatch_country, origin_recognised=not origin_country or origin_entry is not None,
            atr_free_circulation=atr_route, atr_available=atr_available, atr_certificate=atr_certificate,
            origin_proof_required=origin_proof_required, fallback_rates=fallback_rates,
            resolved_country_group=selected, rate_variants=rate_variants,
            unambiguous_rates=unambiguous_rates, ambiguous_measure_types=ambiguous_measure_types,
            measures=primary[:500], conditional_measures=conditional[:240],
            alternatives=alternatives[:120], snapshots=[self._snapshot(row) for row in snapshots],
            measure_coverage=coverage, unresolved_measure_types=unresolved, warnings=warnings, as_of=_now(),
        )
        self._stamp_validity(result, snapshots, as_of)
        self._attach_trade_measures(result)
        return self._describe_lookup(result)

    async def decision_tree(
        self,
        gtip: str,
        *,
        origin_country: str | None = None,
        auto_sync: bool = True,
        as_of: str | None = None,
    ) -> TariffDecisionTreeResult:
        """Return the next official tariff level without guessing a child code.

        The method deliberately exposes every immediate branch.  It never ranks or
        auto-selects a child, because a rate row proving that a code exists does not
        prove that the user's goods belong under that code.
        """
        normalised = _normalise_gtip(gtip)
        level_by_length: dict[int, Literal["HS4", "HS6", "CN8", "TR10", "GTIP12"]] = {
            4: "HS4",
            6: "HS6",
            8: "CN8",
            10: "TR10",
            12: "GTIP12",
        }
        next_by_length: dict[int, tuple[int, Literal["HS6", "CN8", "TR10", "GTIP12"]]] = {
            4: (6, "HS6"),
            6: (8, "CN8"),
            8: (10, "TR10"),
            10: (12, "GTIP12"),
        }
        if not normalised or len(normalised) not in level_by_length:
            raise ValueError("Tarife karar ağacı için 4, 6, 8, 10 veya 12 haneli kod gereklidir.")
        if auto_sync and not self.status().ready:
            await self.sync()
        if not self.status().ready:
            return TariffDecisionTreeResult(
                status="unavailable",
                prefix=normalised,
                level=level_by_length[len(normalised)],
                origin_country=origin_country,
                warnings=["Resmî tarife tabloları henüz eşitlenmedi."],
                as_of=_now(),
            )

        current = await self.lookup(normalised, origin_country=origin_country, auto_sync=False, as_of=as_of)
        if current.status in {"not_found", "unavailable"} or current.matched_gtip_count < 1:
            return TariffDecisionTreeResult(
                status="not_found" if current.status == "not_found" else "unavailable",
                prefix=normalised,
                level=level_by_length[len(normalised)],
                origin_country=origin_country,
                warnings=current.warnings,
                as_of=current.as_of, as_of_date=current.as_of_date, validity_basis=current.validity_basis,
            )

        if len(normalised) == 12:
            return TariffDecisionTreeResult(
                status="matched",
                prefix=normalised,
                level="GTIP12",
                origin_country=origin_country,
                requires_user_selection=False,
                exact_gtip_selected=True,
                warnings=current.warnings,
                as_of=current.as_of, as_of_date=current.as_of_date, validity_basis=current.validity_basis,
            )

        child_length, next_level = next_by_length[len(normalised)]
        child_codes = sorted({code[:child_length] for code in current.matched_gtips})
        child_lookups = await asyncio.gather(
            *(
                self.lookup(code, origin_country=origin_country, auto_sync=False)
                for code in child_codes
            )
        )
        children: list[TariffTreeNode] = []
        for code, lookup in zip(child_codes, child_lookups):
            if not origin_country:
                rate_status: Literal["unambiguous", "ambiguous", "origin_required"] = "origin_required"
            elif lookup.ambiguous_measure_types or "customs_duty" not in lookup.unambiguous_rates:
                rate_status = "ambiguous"
            else:
                rate_status = "unambiguous"
            children.append(
                TariffTreeNode(
                    code=code,
                    level=next_level,
                    final=child_length == 12,
                    descendant_count=lookup.matched_gtip_count,
                    rate_status=rate_status,
                    unambiguous_rates=lookup.unambiguous_rates,
                    rate_variants=lookup.rate_variants,
                    ambiguous_measure_types=lookup.ambiguous_measure_types,
                    warnings=lookup.warnings[:6],
                )
            )

        self._describe_children(children)
        warnings = [
            f"{normalised} altında {len(children)} adet {next_level} dalı ve "
            f"{current.matched_gtip_count} adet GTİP12 satırı bulundu.",
            "Bir alt dal yalnızca kullanıcı tarafından doğrulanan ürün evsafıyla seçilmelidir.",
        ]
        if len(children) == 1:
            warnings.append(
                "Tek alt dal bulunması ürün sınıflandırmasını hukuken kesinleştirmez; ürün evsafı yine doğrulanmalıdır."
            )
        return TariffDecisionTreeResult(
            status="matched",
            prefix=normalised,
            level=level_by_length[len(normalised)],
            next_level=next_level,
            origin_country=origin_country,
            total_children=len(children),
            children=children,
            requires_user_selection=True,
            exact_gtip_selected=False,
            warnings=warnings,
            as_of=current.as_of, as_of_date=current.as_of_date, validity_basis=current.validity_basis,
        )

    def _describe_lookup(self, result: TariffLookupResult) -> TariffLookupResult:
        """Sorgu sonucuna resmî eşya tanımını ekler; motor yoksa hiçbir şey yapmaz.

        Tam eşleşme yoksa nomenklatür motoru en yakın üst pozisyondan cevap verir ve
        ``goods_matched_code`` hangi seviyeden geldiğini söyler — 12 haneli bir kodun
        tanımı 8 haneden geliyorsa kullanıcının bunu bilmesi gerekir.
        """
        engine = getattr(self, "nomenclature", None)
        if engine is None or not result.gtip:
            return result
        try:
            described = engine.lookup(result.gtip, with_children=False)
        except Exception:
            logger.exception("Tarife sonucuna eşya tanımı eklenemedi")
            return result
        if getattr(described, "status", "") != "matched":
            return result
        return result.model_copy(
            update={
                "goods_description": described.description,
                "goods_full_path": described.full_path,
                "goods_unit": described.unit,
                "goods_matched_code": described.matched_code,
            }
        )

    def _describe_children(self, children: list[TariffTreeNode]) -> None:
        """Ağaç dallarına resmî eşya tanımını ekler; motor yoksa hiçbir şey yapmaz.

        Tek toplu sorgu: dal başına ayrı okuma, 8471 gibi kalabalık bir pozisyonda
        onlarca gereksiz sorgu demekti.
        """
        engine = getattr(self, "nomenclature", None)
        if engine is None or not children:
            return
        try:
            described = engine.describe_many(node.code for node in children)
        except Exception:
            logger.exception("Tarife ağacına eşya tanımı eklenemedi")
            return
        for node in children:
            row = described.get(node.code)
            if not row:
                continue
            node.description = str(row.get("description") or "")
            node.full_path = str(row.get("full_path") or "")
            node.unit = str(row.get("unit") or "")

    async def calculate(
        self,
        gtip: str,
        origin_country: str,
        data: LandedCostInput,
        *,
        dispatch_country: str | None = None,
        atr_certificate: bool | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Apply only one unambiguous, unfootnoted official rate per measure type."""
        lookup = await self.lookup(
            gtip, origin_country=origin_country, dispatch_country=dispatch_country, atr_certificate=atr_certificate, as_of=as_of
        )
        safe_rates = lookup.unambiguous_rates
        conflicts = [
            f"{measure_type}: alt GTİP satırlarında oran veya önlem kapsamı farklı"
            for measure_type in lookup.ambiguous_measure_types
        ]
        # A user-supplied rate never silently replaces a different official rate.
        overrides: list[dict[str, Any]] = []
        for measure_type, field_name in (
            ("customs_duty", "customs_duty_rate"),
            ("additional_duty", "additional_duty_rate"),
            ("additional_financial_liability", "additional_financial_liability_rate"),
        ):
            user_rate = getattr(data, field_name)
            official = safe_rates.get(measure_type)
            if user_rate is not None and official is not None and abs(float(user_rate) - float(official)) > 1e-9:
                overrides.append({"measure_type": measure_type, "user_rate": float(user_rate), "official_rate": float(official)})
                conflicts.append(
                    f"{_MEASURE_LABELS.get(measure_type, measure_type)}: girdiğiniz %{float(user_rate):g} resmî satırdaki "
                    f"%{float(official):g} oranından farklı; hesapta girdiğiniz oran kullanıldı, beyan öncesi doğrulayın."
                )
        inferred_additional_duty = safe_rates.get("additional_duty")
        if (
            inferred_additional_duty is None
            and data.additional_duty_rate is None
            and "additional_duty" not in lookup.ambiguous_measure_types
            and lookup.measure_coverage.get("additional_duty")
            and lookup.measure_coverage["additional_duty"].status == "verified_snapshot"
            and lookup.status in {"matched", "partial"}
        ):
            inferred_additional_duty = 0.0

        enriched = data.model_copy(
            update={
                "customs_duty_rate": data.customs_duty_rate if data.customs_duty_rate is not None else safe_rates.get("customs_duty"),
                "additional_duty_rate": data.additional_duty_rate if data.additional_duty_rate is not None else inferred_additional_duty,
                "additional_financial_liability_rate": (
                    data.additional_financial_liability_rate
                    if data.additional_financial_liability_rate is not None
                    else safe_rates.get("additional_financial_liability")
                ),
            }
        )
        cost = calculate_landed_cost(enriched)
        cost.rate_overrides = overrides
        cost.warnings.extend(conflicts)
        trade = lookup.trade_measures or {}
        if trade:
            live_dumping = [
                hit for hit in trade.get("anti_dumping", []) if hit.get("origin_match") is not False and hit.get("status") != "expired"
            ]
            if live_dumping and data.anti_dumping_amount is None:
                cost.warnings.append(
                    "Resmî listede bu GTİP ve menşe için dampinge/sübvansiyona karşı önlem var; tebliğdeki oran veya birim tutarı "
                    "uygulayıp 'Damping / sübvansiyon toplamı' alanına girin: "
                    + "; ".join(f"{hit['country']} – {hit['rate_text']}" for hit in live_dumping[:3])
                )
            live_safeguard = [hit for hit in trade.get("safeguard", []) if hit.get("status") != "expired" and hit.get("origin_match") is not False]
            if live_safeguard and data.additional_financial_liability_rate is None and data.anti_dumping_amount is None:
                cost.warnings.append(
                    "Korunma önlemi (ek mali yükümlülük) kapsamı: "
                    + "; ".join(f"{hit['product']} – {hit['rate_text']}" for hit in live_safeguard[:2])
                    + ". Kontenjan dışıysa tutarı ek mali yükümlülük veya damping alanına girin."
                )
            threshold = trade.get("surveillance_unit_value")
            if threshold and data.surveillance_unit_value is None:
                units = {hit.get("unit") for hit in trade.get("surveillance", []) if hit.get("unit")}
                cost.warnings.append(
                    f"Gözetim tebliği kapsamı: birim gümrük kıymeti eşiği {threshold:g} {'/'.join(sorted(units)) or 'ABD Doları'}; "
                    "gözetim belgesi yoksa kıymet bu eşiğe yükseltilir. 'Gözetim birim kıymeti' alanını doldurun."
                )
        return {
            "tariff": lookup.model_dump(mode="json"),
            "cost": cost.model_dump(mode="json"),
            "as_of_date": lookup.as_of_date,
            "validity_basis": lookup.validity_basis,
            "legal_notice": (
                "Hesap, gösterilen snapshot ve kullanıcı girdileriyle hazırlanmış ön çalışmadır. "
                "GTİP, menşe, kıymet, belge ve yürürlük durumu gümrük işlemi öncesinde doğrulanmalıdır."
            ),
        }

    def changes(self, source_id: str, *, limit: int = 200) -> dict[str, Any]:
        bounded = max(1, min(limit, 1000))
        if self.ledger is not None:
            batch = self.ledger.latest_batch("tariff", source_id)
            if batch and batch.get("old_snapshot_id"):
                rows = self.ledger.changes(kind="tariff", batch_id=batch["id"], limit=bounded)
                changes = []
                for item in rows:
                    before, after = item.get("before") or {}, item.get("after") or {}
                    changes.append(
                        {
                            "gtip": item.get("gtip"), "measure_type": (after or before).get("measure_type"),
                            "country_group": (after or before).get("country_group"),
                            "before": before.get("rate_text") if before else None, "after": after.get("rate_text") if after else None,
                            "before_footnote": before.get("footnote") if before else None,
                            "after_footnote": after.get("footnote") if after else None,
                            "change_type": item.get("change_type"),
                        }
                    )
                return {
                    "source_id": source_id, "status": "compared", "new_snapshot": batch["new_snapshot_id"],
                    "old_snapshot": batch["old_snapshot_id"], "detected_at": batch["detected_at"],
                    "total_changes": int(batch["added"]) + int(batch["removed"]) + int(batch["modified"]),
                    "changes": changes, "ledger_batch": batch["id"],
                }
        with self._connect() as db:
            snapshots = db.execute(
                "SELECT * FROM tariff_snapshots WHERE source_id=? ORDER BY retrieved_at DESC LIMIT 2", (source_id,)
            ).fetchall()
            if len(snapshots) < 2:
                return {"source_id": source_id, "status": "no_previous_snapshot", "changes": []}
            newer, older = snapshots
            def rows(snapshot_id: str) -> dict[tuple[str, str, str], sqlite3.Row]:
                result = db.execute("SELECT * FROM tariff_measures WHERE snapshot_id=?", (snapshot_id,)).fetchall()
                return {(row["gtip"], row["measure_type"], row["country_group"]): row for row in result}
            current, previous = rows(newer["id"]), rows(older["id"])
        changes: list[dict[str, Any]] = []
        for key in sorted(set(current) | set(previous)):
            new, old = current.get(key), previous.get(key)
            before = old["rate_text"] if old else None
            after = new["rate_text"] if new else None
            before_note = old["footnote"] if old else None
            after_note = new["footnote"] if new else None
            if before != after or before_note != after_note:
                changes.append({"gtip": key[0], "measure_type": key[1], "country_group": key[2], "before": before, "after": after, "before_footnote": before_note, "after_footnote": after_note})
        return {
            "source_id": source_id, "status": "compared", "new_snapshot": newer["id"], "old_snapshot": older["id"],
            "total_changes": len(changes), "changes": changes[:bounded],
        }


def calculate_landed_cost(data: LandedCostInput) -> LandedCostResult:
    """Reproducible arithmetic; no tax rate is inferred inside this function."""
    warnings: list[str] = []
    missing: list[str] = []
    customs_value = data.invoice_value + data.freight + data.insurance
    if data.surveillance_unit_value is None:
        missing.append("Gözetim birim kıymeti (uygulanmıyorsa 0)")
    elif data.surveillance_unit_value > 0:
        if data.quantity is None:
            missing.append("Gözetim kıymeti için miktar")
        elif data.has_surveillance_certificate is False:
            customs_value = max(customs_value, data.surveillance_unit_value * data.quantity)
            warnings.append("Gözetim belgesi olmadığı beyanına göre gümrük kıymeti birim gözetim kıymetine yükseltildi.")
        elif data.has_surveillance_certificate is None:
            missing.append("Gözetim belgesi durumu")

    lines: list[dict[str, Any]] = [
        {"code": "customs_value", "label": "Gümrük kıymeti", "base": None, "rate": None, "amount": round(customs_value, 2), "formula": "fatura + navlun + sigorta (varsa gözetim düzeltmesi)"}
    ]

    def percentage(code: str, label: str, base: float, rate: float | None) -> float | None:
        if rate is None:
            missing.append(label)
            lines.append({"code": code, "label": label, "base": round(base, 2), "rate": None, "amount": None, "formula": "oran doğrulanmadı"})
            return None
        amount = base * rate / 100
        lines.append({"code": code, "label": label, "base": round(base, 2), "rate": rate, "amount": round(amount, 2), "formula": f"{round(base, 2)} × %{rate}"})
        return amount

    duty = percentage("customs_duty", "Gümrük vergisi oranı", customs_value, data.customs_duty_rate)
    additional = percentage("additional_duty", "İlave gümrük vergisi oranı", customs_value, data.additional_duty_rate)
    emy = percentage("financial_liability", "Ek mali yükümlülük oranı", customs_value, data.additional_financial_liability_rate)
    if data.anti_dumping_amount is None:
        missing.append("Damping/sübvansiyon önlemi (uygulanmıyorsa 0)")
        lines.append({"code": "anti_dumping", "label": "Damping karşıtı vergi", "base": None, "rate": None, "amount": None, "formula": "uygulanabilirlik doğrulanmadı"})
    else:
        lines.append({"code": "anti_dumping", "label": "Damping karşıtı vergi", "base": None, "rate": None, "amount": round(data.anti_dumping_amount, 2), "formula": "doğrulanmış sabit/toplam tutar"})
    kkdf = data.kkdf_rate
    payment_key = _key(data.payment_method)
    deferred_payment = bool(payment_key) and any(token in payment_key for token in ("mal mukabili", "vadeli", "kredi"))
    cash_payment = bool(payment_key) and "pesin" in payment_key and not deferred_payment
    if kkdf is None and deferred_payment:
        kkdf = 6.0
        warnings.append("Kredili/vadeli ödemede KKDF %6 önerildi; finansman yapınıza göre doğrulayın. Peşin ödemeyi seçerseniz bu kalem %0 olur.")
    elif kkdf is None and cash_payment:
        kkdf = 0.0
        warnings.append("Peşin ödemede KKDF uygulanmaz; KKDF %0 olarak önerildi, onaylayın.")
    elif kkdf is None and payment_key:
        warnings.append("Ödeme şekli anlaşılamadı; KKDF oranını kendiniz doğrulayıp girin (peşin %0, kredili/vadeli %6).")
    if kkdf is None and not payment_key:
        missing.append("Ödeme şekli (peşin ise KKDF %0, kredili/vadeli ise %6)")
    kkdf_amount = percentage("kkdf", "KKDF oranı", data.invoice_value, kkdf)
    if data.sct_amount is None:
        missing.append("ÖTV tutarı (uygulanmıyorsa 0)")
        lines.append({"code": "sct", "label": "ÖTV", "base": None, "rate": None, "amount": None, "formula": "uygulanabilirlik doğrulanmadı"})
    else:
        lines.append({"code": "sct", "label": "ÖTV", "base": None, "rate": None, "amount": round(data.sct_amount, 2), "formula": "doğrulanmış toplam tutar"})

    trt_bandrol = None
    if data.trt_bandrol_rate is not None:
        bandrol_base = customs_value + (duty or 0.0) + (additional or 0.0) + (emy or 0.0) + (data.anti_dumping_amount or 0.0)
        trt_bandrol = percentage("trt_bandrol", "TRT bandrol ücreti", bandrol_base, data.trt_bandrol_rate)
        if data.trt_bandrol_rate > 0:
            warnings.append(
                "TRT bandrol ücreti matrahı gümrük kıymeti + ithalat vergileri (ÖTV ve KDV hariç) olarak alındı; "
                "ürün grubuna göre oran ve matrah 3093 sayılı Kanun ve güncel bandrol tebliğinden doğrulanmalıdır."
            )
    else:
        warnings.append("TRT bandrol ücreti (TV, radyo, telefon, taşıt vb.) uygulanıyorsa oranını girin; girilmedi, hesaba alınmadı.")

    pre_vat_known = all(
        value is not None
        for value in (duty, additional, emy, data.anti_dumping_amount, kkdf_amount, data.sct_amount, data.surveillance_unit_value)
    )
    vat_base = None
    vat = None
    if pre_vat_known:
        vat_base = (
            customs_value + duty + additional + emy + data.anti_dumping_amount + kkdf_amount + data.sct_amount
            + (trt_bandrol or 0.0) + data.other_costs
        )
        vat = percentage("vat", "KDV oranı", vat_base, data.vat_rate)
    else:
        if data.vat_rate is None:
            missing.append("KDV oranı")
        lines.append({"code": "vat", "label": "KDV", "base": None, "rate": data.vat_rate, "amount": None, "formula": "önceki vergi oranları eksik"})
    total = vat_base + vat if vat_base is not None and vat is not None else None
    unit = total / data.quantity if total is not None and data.quantity else None
    tax_components = (duty, additional, emy, data.anti_dumping_amount, kkdf_amount, data.sct_amount, trt_bandrol or 0.0, vat)
    total_taxes = sum(component for component in tax_components if component is not None) if all(
        component is not None for component in tax_components
    ) else None
    try_summary = _try_summary(data, customs_value, vat_base, total_taxes, vat, lines, warnings)
    if data.stamp_duty_try is None and data.exchange_rate is None:
        warnings.append(
            "Gümrük beyannamesi damga vergisi (maktu, TL) ayrıca ödenir ve KDV matrahına dahildir; tutarı her yıl yeniden "
            "belirlendiği için kur ile birlikte girildiğinde TL özetine alınır."
        )
    status: Literal["complete", "partial", "blocked"] = "complete" if total is not None and not missing else "partial"
    return LandedCostResult(
        status=status, currency=data.currency, lines=lines, customs_value=round(customs_value, 2),
        vat_base=round(vat_base, 2) if vat_base is not None else None,
        total_taxes=round(total_taxes, 2) if total_taxes is not None else None,
        landed_total=round(total, 2) if total is not None else None,
        unit_landed_cost=round(unit, 4) if unit is not None else None,
        missing_rates=list(dict.fromkeys(missing)), try_summary=try_summary, warnings=warnings,
    )


def _try_summary(
    data: LandedCostInput,
    customs_value: float,
    vat_base: float | None,
    total_taxes: float | None,
    vat: float | None,
    lines: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any] | None:
    """Convert the ledger to Turkish lira and add the lira-only declaration costs.

    Customs value and every import tax are converted with the declaration-date exchange
    rate; stamp duty and pre-registration port/storage costs enter the VAT base in lira,
    GEKAP is a post-clearance cost outside the VAT base.  VAT is recalculated on the lira
    base so the lira summary is internally consistent.
    """
    lira_items = {
        "stamp_duty": data.stamp_duty_try, "port_storage": data.port_storage_try, "gekap": data.gekap_try,
    }
    if data.exchange_rate is None:
        if any(value for value in lira_items.values()):
            warnings.append("Damga vergisi / liman-ardiye / GEKAP TL tutarları girildi; TL özeti için beyanname tarihli kur da girilmelidir.")
        return None
    rate = float(data.exchange_rate)
    if data.currency == "TRY":
        rate = 1.0
    to_try = lambda value: round(float(value) * rate, 2)  # noqa: E731
    try_lines = [
        {"code": line["code"], "label": line["label"], "amount_try": to_try(line["amount"]) if line.get("amount") is not None else None}
        for line in lines if line["code"] != "vat"
    ]
    stamp = float(data.stamp_duty_try or 0.0)
    port = float(data.port_storage_try or 0.0)
    gekap = float(data.gekap_try or 0.0)
    try_lines.append({"code": "stamp_duty", "label": "Beyanname damga vergisi (TL)", "amount_try": round(stamp, 2) if data.stamp_duty_try is not None else None})
    try_lines.append({"code": "port_storage", "label": "Tescil öncesi liman / ardiye / tahmil-tahliye (TL)", "amount_try": round(port, 2) if data.port_storage_try is not None else None})
    complete = vat_base is not None and vat is not None and total_taxes is not None
    vat_base_try = round(vat_base * rate + stamp + port, 2) if complete else None
    vat_try = round(vat_base_try * (data.vat_rate or 0.0) / 100, 2) if complete and data.vat_rate is not None else None
    try_lines.append({"code": "vat", "label": "KDV (TL matrah üzerinden)", "amount_try": vat_try})
    try_lines.append({"code": "gekap", "label": "GEKAP (beyanla ödenir, KDV matrahı dışı)", "amount_try": round(gekap, 2) if data.gekap_try is not None else None})
    taxes_ex_vat = (total_taxes - vat) if complete else None
    total_taxes_try = round(taxes_ex_vat * rate + stamp + vat_try, 2) if complete and vat_try is not None else None
    landed_total_try = round(vat_base_try + vat_try + gekap, 2) if complete and vat_try is not None else None
    unit_try = round(landed_total_try / data.quantity, 4) if landed_total_try is not None and data.quantity else None
    notes = [
        f"Kur: 1 {data.currency} = {rate:g} TL" + (f" ({data.exchange_rate_date})" if data.exchange_rate_date else "") + "; beyanname tescil tarihindeki TCMB döviz satış kuru esas alınmalıdır.",
        "Damga vergisi ve tescil öncesi liman/ardiye giderleri KDV matrahına dahil edildi; GEKAP toplama eklendi, KDV matrahına alınmadı.",
    ]
    if data.stamp_duty_try is None:
        notes.append("Beyanname damga vergisi girilmedi; maktu tutarı ekleyin.")
    return {
        "status": "complete" if landed_total_try is not None else "partial",
        "exchange_rate": rate,
        "exchange_rate_date": data.exchange_rate_date,
        "currency": data.currency,
        "customs_value_try": to_try(customs_value),
        "lines": try_lines,
        "vat_base_try": vat_base_try,
        "vat_try": vat_try,
        "total_taxes_try": total_taxes_try,
        "landed_total_try": landed_total_try,
        "unit_landed_cost_try": unit_try,
        "notes": notes,
    }
