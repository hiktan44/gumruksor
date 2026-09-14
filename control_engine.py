"""Versioned import-control scope engine backed by official consolidated texts.

The engine indexes GTIP annexes from the Ministry of Justice's official
Bedesten legislation service.  It deliberately separates three concepts:

* appearing in a communique annex (scope signal),
* being processed in TAREKS or by another competent authority, and
* being selected for physical inspection or laboratory testing.

The last item is normally decided by the authority's risk analysis.  It is
therefore never inferred merely from a GTIP match.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import logging

import httpx
import openpyxl
import xlrd
from markitdown import MarkItDown
from pydantic import BaseModel, Field

from bedesten_client import BedestenClient
from trusted_certificates import GEOTRUST_TLS_RSA_CA_G1_PEM
from change_ledger import batch_id_for, diff_rows
from review_policy import DiffSummary, ReviewPolicy, decide, ensure_review_columns, review_metadata, row_review_fields
from temporal import (
    close_previous, covers, ensure_validity_columns, extract_effective_date, normalise_as_of, snapshot_validity, today_iso,
    validity_basis,
)
from security_firewall import validate_outbound_url

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(
    r"(?<!\d)(\d{4}(?:[.\t ]\d{2}){1,4}|\d{2}(?:[.\t ]\d{2}){1,5}|\d{4})(?!\d)"
)
_NEXT_ANNEX_RE = re.compile(r"(?im)^\s*Ek\s*[-–]?\s*[2-9]\b")
_DOCUMENT_HEADING_RE = re.compile(
    r"(?i)(YÜKLENMESİ\s+GEREKEN\s+BELGELER|İBRAZ\s+EDİLECEK\s+BELGELER|BELGELER\s+VE\s+MUAFİYETLER)"
)


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ı", "i"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _normalise_gtip(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) in {4, 6, 8, 10, 12}:
        return digits
    return None


class ControlScopeRow(BaseModel):
    gtip_prefix: str
    description: str | None = None
    source_line: str
    source_offset: int
    excluded: bool = False
    # "scope": product is subject to the control; "prohibited": the annex lists
    # goods whose import is banned outright (e.g. waste and chemical communiques).
    list_kind: Literal["scope", "prohibited"] = "scope"


class RequiredDocument(BaseModel):
    order: int
    text: str
    # "conditional": the communique itself makes the document depend on a case ("varsa", "…halinde").
    kind: Literal["required", "conditional"] = "required"


class ImportControlRule(BaseModel):
    code: str
    title: str
    category: str
    authority: str
    system: str
    risk_based: bool
    physical_inspection_possible: bool
    laboratory_test_possible: bool
    required_documents_excerpt: str | None = None
    required_documents: list[RequiredDocument] = Field(default_factory=list)
    # Sentences of the communique that carve products or importers out of the control
    # (muafiyet, istisna, numune, sanayici, AB'de serbest dolaşım...).  Evidence, not a decision.
    exemptions: list[str] = Field(default_factory=list)
    scope_count: int
    mevzuat_id: str
    source_url: str
    official_gazette_date: str | None = None
    official_gazette_number: str | None = None
    document_sha256: str
    retrieved_at: str
    valid_from: str
    snapshot_id: str


class ImportControlMatch(BaseModel):
    rule: ImportControlRule
    matched_scope: ControlScopeRow
    match_type: Literal["exact", "prefix"]
    assessment: str
    cautions: list[str] = Field(default_factory=list)


class ImportControlLookupResult(BaseModel):
    status: Literal["matched", "not_found", "unavailable"]
    gtip: str
    scope_determination: Literal["annex_match", "no_indexed_match", "unavailable"] = "unavailable"
    risk_selection_status: Literal["not_determined_by_this_system"] = "not_determined_by_this_system"
    product_scope_review_required: bool = True
    matches: list[ImportControlMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    as_of: str
    as_of_date: str | None = None
    validity_basis: Literal["current", "legal", "observed", "unavailable"] = "current"
    snapshot_validity: list[dict[str, Any]] = Field(default_factory=list)


class ControlSnapshot(BaseModel):
    id: str
    code: str
    title: str
    mevzuat_id: str
    document_sha256: str
    retrieved_at: str
    valid_from: str
    scope_count: int
    active: bool
    valid_to: str | None = None
    valid_from_basis: str = "config"


class ControlSyncStatus(BaseModel):
    ready: bool
    syncing: bool
    last_checked_at: str | None = None
    active_snapshots: list[ControlSnapshot] = Field(default_factory=list)
    scope_count: int = 0
    errors: list[str] = Field(default_factory=list)
    sync_interval_seconds: int
    pending_review_count: int = 0
    review_mode: str = "off"


def _scope_rows_from_segment(segment: str) -> list[ControlScopeRow]:
    """Parse GTIP/GTP codes and their adjacent descriptions from one table."""
    matches = list(_CODE_RE.finditer(segment))
    rows: list[ControlScopeRow] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        code = _normalise_gtip(match.group(1))
        if not code or code in seen:
            continue
        # Four-digit years are common in footnotes; genuine four-digit tariff
        # headings are dotted or appear after a GTIP/GTP column heading.
        before = segment[max(0, match.start() - 300) : match.start()]
        before_key = _key(before)
        if len(code) == 4:
            # Bare four-digit values in rich annex tables are overwhelmingly
            # standard numbers and years (TS 1275, 2016, 2024), not tariff
            # headings. Genuine four-digit headings are printed as 87.02.
            if "." not in match.group(1):
                continue
            line_start = max(segment.rfind("\n", 0, match.start()), segment.rfind("|", 0, match.start()))
            line_prefix = segment[line_start + 1 : match.start()]
            if not (
                re.fullmatch(r"\s*(?:\d+\s*[.)-]?)?\s*", line_prefix)
                or "gtip" in before_key[-100:]
                or "gtp" in before_key[-100:]
            ):
                continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else match.end() + 500
        raw_description = segment[match.end() : min(next_start, match.end() + 500)]
        description = re.sub(r"\s+", " ", raw_description).strip(" \n\t-–:;.,")
        source_line = re.sub(r"\s+", " ", segment[match.start() : min(next_start, match.end() + 500)]).strip()
        before_context = segment[max(0, match.start() - 250) : match.start()]
        after_context = segment[match.end() : min(len(segment), match.end() + 350)]
        open_paren = before_context.rfind("(")
        close_before = before_context.rfind(")")
        close_paren = after_context.find(")")
        parenthetical = (
            before_context[open_paren:] + after_context[: close_paren + 1]
            if open_paren > close_before and close_paren >= 0
            else ""
        )
        excluded = "haric" in _key(parenthetical)
        seen.add(code)
        rows.append(
            ControlScopeRow(
                gtip_prefix=code,
                description=description[:400] or None,
                source_line=source_line[:600],
                source_offset=match.start(),
                excluded=excluded,
            )
        )
    return rows


def communique_code_matches(code: str, title: str) -> bool:
    """True only when the full communique number appears in the title.

    Substring checks make "2026/1" match "2026/12" and would let a later
    communique overwrite the TSE list; the number must be delimited on both
    sides.
    """
    pattern = rf"(?<![\d/.]){re.escape(code.strip())}(?![\d/.])"
    return re.search(pattern, re.sub(r"\s+", "", title)) is not None


def _annex_heading_re(annex_number: int) -> re.Pattern[str]:
    # "Ek-1", "EK 1", "Ek-\n1", "Ek-1/A" but not "Ek-12" and not the body
    # reference "Ek-1'de" ("Ek-1 de" style references are rare in official texts).
    return re.compile(rf"(?m)^\s*[Ee][Kk]\s*[-–]?\s*{annex_number}(?:\s*/\s*[A-ZÇĞİÖŞÜa-zçğıöşü])?{_HEADING_TAIL}")


# A heading ends the line or continues with a title in capitals ("Ek-1 DENETİME
# TABİ ÜRÜNLER"); running text such as "Ek-1 sayılı listede" is not a heading.
_HEADING_TAIL = r"(?![\d’'`´\w])(?=[ \t]*(?:$|[:\-–(]|[A-ZÇĞİÖŞÜ]))"
_ANY_ANNEX_HEADING_RE = re.compile(rf"(?m)^\s*[Ee][Kk]\s*[-–]?\s*[1-9](?:\s*/\s*[A-ZÇĞİÖŞÜa-zçğıöşü])?{_HEADING_TAIL}")


def extract_annex_scope(text: str, annex_number: int = 1) -> list[ControlScopeRow]:
    """Return GTIP rows from every table of the requested annex.

    Each heading ("Ek-1", "Ek-1/A", "Ek-1/B") opens a segment that ends at the
    next annex heading of any number, so multi-part annexes are merged instead
    of keeping only the densest part.  Body references such as "Ek-1'de" are
    not headings.  Segments without a single code are ignored, which drops the
    remaining false starts.
    """
    if annex_number < 1 or annex_number > 9:
        raise ValueError("Ek numarası 1 ile 9 arasında olmalıdır.")
    annex_re = _annex_heading_re(annex_number)
    rows: list[ControlScopeRow] = []
    for match in annex_re.finditer(text):
        tail = text[match.end():]
        end = _ANY_ANNEX_HEADING_RE.search(tail)
        segment = text[match.start(): match.end() + (end.start() if end else len(tail))]
        if not _CODE_RE.search(segment):
            continue
        for row in _scope_rows_from_segment(segment):
            row.source_offset += match.start()
            rows.append(row)
    return _dedupe_scope(rows)


def annex_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise ``scope_annex`` / ``scope_annexes`` into [{annex, kind}, ...]."""
    raw = config.get("scope_annexes")
    if not raw:
        return [{"annex": int(config.get("scope_annex", 1)), "kind": "scope"}]
    plan: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            kind = str(item.get("kind", "scope"))
            plan.append({"annex": int(item.get("annex", 1)), "kind": kind if kind in {"scope", "prohibited"} else "scope"})
        else:
            plan.append({"annex": int(item), "kind": "scope"})
    return plan


def extract_scope_table(text: str, start_pattern: str, end_pattern: str) -> list[ControlScopeRow]:
    """Parse an inline GTIP/GTP table bounded by explicit official headings."""
    start = re.search(start_pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not start:
        return []
    tail = text[start.start() :]
    end = re.search(end_pattern, tail[start.end() - start.start() :], flags=re.IGNORECASE | re.MULTILINE)
    segment_end = (start.end() - start.start()) + end.start() if end else len(tail)
    return _scope_rows_from_segment(tail[:segment_end])


def _dedupe_scope(rows: list[ControlScopeRow]) -> list[ControlScopeRow]:
    result: list[ControlScopeRow] = []
    seen: set[str] = set()
    for row in rows:
        if row.gtip_prefix in seen:
            continue
        seen.add(row.gtip_prefix)
        result.append(row)
    return result


def _tabular_bytes_to_text(data: bytes, extension: str) -> str:
    lines: list[str] = []
    if extension == ".xlsx":
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for sheet in workbook.worksheets:
            lines.append(f"SAYFA {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() for value in row if value not in (None, "")]
                if values:
                    lines.append("\t".join(values))
        workbook.close()
    elif extension == ".xls":
        workbook = xlrd.open_workbook(file_contents=data)
        for sheet in workbook.sheets():
            lines.append(f"SAYFA {sheet.name}")
            for row_index in range(sheet.nrows):
                values = [str(value).strip() for value in sheet.row_values(row_index) if value not in (None, "")]
                if values:
                    lines.append("\t".join(values))
    return "\n".join(lines)


def extract_attachment_scope(
    data: bytes,
    converter: MarkItDown | None = None,
    member_suffixes: list[str] | None = None,
) -> list[ControlScopeRow]:
    """Extract tariff rows from selected scope members of an official ZIP."""
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("Resmî ek arşivi 50 MB güvenlik sınırını aşıyor.")
    rows: list[ControlScopeRow] = []
    converter = converter or MarkItDown()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total_uncompressed = sum(item.file_size for item in archive.infolist() if not item.is_dir())
        if total_uncompressed > 150 * 1024 * 1024:
            raise ValueError("Resmî ek arşivinin açılmış boyutu güvenlik sınırını aşıyor.")
        for item in archive.infolist():
            if item.is_dir() or item.file_size > 50 * 1024 * 1024:
                continue
            if member_suffixes:
                member_key = _key(Path(item.filename).name)
                if not any(member_key.endswith(_key(suffix)) for suffix in member_suffixes):
                    continue
            extension = Path(item.filename).suffix.casefold()
            member = archive.read(item)
            if extension in {".xlsx", ".xls"}:
                text = _tabular_bytes_to_text(member, extension)
            elif extension in {".docx", ".pdf"}:
                converted = converter.convert_stream(io.BytesIO(member), file_extension=extension)
                text = converted.text_content
            elif extension in {".csv", ".txt", ".htm", ".html"}:
                text = member.decode("utf-8", errors="replace")
            else:
                continue
            rows.extend(_scope_rows_from_segment(text))
    return _dedupe_scope(rows)


def extract_required_documents(text: str) -> str | None:
    matches = list(_DOCUMENT_HEADING_RE.finditer(text))
    if not matches:
        return None
    # Annex document lists are normally near the end; prefer the last heading.
    start = matches[-1].start()
    tail = text[start : start + 5000]
    next_annex = _NEXT_ANNEX_RE.search(tail[matches[-1].end() - start :])
    if next_annex:
        tail = tail[: (matches[-1].end() - start) + next_annex.start()]
    return re.sub(r"\s+", " ", tail).strip()[:3000] or None


_DOCUMENT_ITEM_RE = re.compile(r"(?:(?<=\s)|^)(\d{1,2})\s*[.)-]\s+(?=\S)")
_CONDITIONAL_HINTS = ("varsa", " ise ", "halinde", "gerektiginde", "talep edilmesi", "istenmesi", "olmasi durumunda", "gerekmesi")
_EXEMPTION_HINTS = (
    "muaf", "istisna", "kapsam disi", "kapsami disinda", "kapsamina girmez", "uygulanmaz", "numune", "sanayici",
    "serbest dolasim", "a.tr", "dahilde isleme", "geri gelen esya", "bedelsiz",
)


def structure_document_list(excerpt: str | None) -> list[RequiredDocument]:
    """Turn the raw 'yüklenmesi gereken belgeler' excerpt into numbered rows."""
    if not excerpt:
        return []
    text = re.sub(r"\s+", " ", excerpt).strip()
    matches = list(_DOCUMENT_ITEM_RE.finditer(text))
    rows: list[RequiredDocument] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip(" ;,-–")
        if len(body) < 4:
            continue
        # Stop a trailing item at the next annex/form heading that may be glued to it.
        body = re.split(r"\s(?=(?:EK|Ek)\s*[-–]?\s*\d)", body)[0].strip()
        order = int(match.group(1))
        if rows and order <= rows[-1].order and order != 1:
            # Numbering restarted or a stray number: keep sequential order.
            order = rows[-1].order + 1
        kind = "conditional" if any(hint in _key(f" {body} ") for hint in _CONDITIONAL_HINTS) else "required"
        rows.append(RequiredDocument(order=order, text=body[:400], kind=kind))
        if len(rows) >= 25:
            break
    return rows


def extract_exemptions(text: str) -> list[str]:
    """Sentences that limit the control's scope; returned verbatim for the reader to judge."""
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.;])\s+(?=[A-ZÇĞİÖŞÜ(])", flat)
    found: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        key = _key(sentence)
        if not any(hint in key for hint in _EXEMPTION_HINTS):
            continue
        if len(sentence) < 25 or len(sentence) > 600:
            continue
        # Skip the list tables and annex headings; keep article prose.
        if _CODE_RE.search(sentence) and len(_CODE_RE.findall(sentence)) > 2:
            continue
        cleaned = sentence.strip()
        if cleaned in seen:
            continue
        seen.add(cleaned)
        found.append(cleaned[:400])
        if len(found) >= 12:
            break
    return found


def infer_process(text: str, title: str) -> dict[str, Any]:
    normal = _key(text)
    title_key = _key(title)
    tareks = "tareks" in normal
    risk_based = "risk analizi" in normal or "risk analizine gore" in normal
    physical = "fiili denetim" in normal or "fiziki muayene" in normal
    lab = "laboratuvar" in normal or "test raporu" in normal

    if tareks:
        system = "TAREKS"
    elif "tarim ve orman" in title_key:
        system = "Tarım ve Orman Bakanlığı kontrol sistemi"
    elif "saglik bakan" in title_key or "tibbi cihaz" in title_key:
        system = "Sağlık Bakanlığı kontrol sistemi"
    elif "cevre" in title_key:
        system = "Çevre, Şehircilik ve İklim Değişikliği Bakanlığı izin/kontrol süreci"
    else:
        system = "Yetkili kurum/gümrük kontrol süreci"

    if "tse" in normal:
        authority = "Türk Standardları Enstitüsü (denetim birimi)"
    elif "tarim ve orman" in title_key:
        authority = "Tarım ve Orman Bakanlığı"
    elif "saglik bakan" in title_key or "tibbi cihaz" in title_key:
        authority = "Sağlık Bakanlığı"
    elif "cevre" in title_key:
        authority = "Çevre, Şehircilik ve İklim Değişikliği Bakanlığı"
    else:
        authority = "Ticaret Bakanlığı / tebliğde belirtilen yetkili kurum"
    return {
        "authority": authority,
        "system": system,
        "risk_based": risk_based,
        "physical_inspection_possible": physical,
        "laboratory_test_possible": lab,
    }


class ImportControlEngine:
    def __init__(self, config_path: str | Path | None = None, data_dir: str | Path | None = None) -> None:
        candidate = Path(config_path or Path(__file__).with_name("control_sources.json"))
        if not candidate.exists():
            for p in (Path(sys.prefix) / "control_sources.json", Path.cwd() / "control_sources.json"):
                if p.exists():
                    candidate = p
                    break
        config_file = candidate
        config = json.loads(config_file.read_text(encoding="utf-8"))
        self.rules_config = list(config.get("rules", []))
        configured_year = str(config.get("valid_from", ""))[:4]
        self.year = datetime.now(UTC).year
        if configured_year.isdigit() and int(configured_year) != self.year:
            for rule in self.rules_config:
                rule["code"] = str(rule["code"]).replace(configured_year, str(self.year), 1)
        self.valid_from = f"{self.year}-01-01"
        self.sync_interval_seconds = max(300, int(config.get("sync_interval_seconds", 21600)))
        default_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mevzuat-mcp"
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR", default_root))
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "controls.sqlite3"
        self._client = BedestenClient(cache_ttl=self.sync_interval_seconds)
        attachment_ssl = httpx.create_ssl_context()
        attachment_ssl.load_verify_locations(cadata=GEOTRUST_TLS_RSA_CA_G1_PEM)
        self._attachment_http = httpx.AsyncClient(
            verify=attachment_ssl,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MevzuatMCP/1.4; +https://gumruksor.com/)",
                "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.8",
            },
            follow_redirects=False,
            timeout=httpx.Timeout(75.0),
            trust_env=False,
        )
        self._converter = MarkItDown()
        self._sync_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._request_interval = max(0.75, float(os.environ.get("CONTROL_REQUEST_INTERVAL_SECONDS", "6.5")))
        self._syncing = False
        self._errors: list[str] = []
        # Optional unified change ledger (change_ledger.ChangeLedger); set by the server.
        self.ledger: Any = None
        # Editorial review gate; the server replaces it with policy_from_env().
        self.review_policy: ReviewPolicy = ReviewPolicy()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_snapshots (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    mevzuat_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    official_gazette_date TEXT,
                    official_gazette_number TEXT,
                    document_sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    scope_count INTEGER NOT NULL,
                    authority TEXT NOT NULL,
                    system TEXT NOT NULL,
                    risk_based INTEGER NOT NULL,
                    physical_inspection_possible INTEGER NOT NULL,
                    laboratory_test_possible INTEGER NOT NULL,
                    required_documents_excerpt TEXT,
                    active INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_control_snapshot_active
                    ON control_snapshots(code, active);
                CREATE TABLE IF NOT EXISTS control_scope (
                    snapshot_id TEXT NOT NULL REFERENCES control_snapshots(id) ON DELETE CASCADE,
                    gtip_prefix TEXT NOT NULL,
                    description TEXT,
                    source_line TEXT NOT NULL,
                    source_offset INTEGER NOT NULL,
                    excluded INTEGER NOT NULL DEFAULT 0,
                    list_kind TEXT NOT NULL DEFAULT 'scope',
                    PRIMARY KEY (snapshot_id, gtip_prefix, list_kind)
                );
                CREATE INDEX IF NOT EXISTS idx_control_scope_gtip ON control_scope(gtip_prefix);
                """
            )
            snapshot_columns = {row[1] for row in db.execute("PRAGMA table_info(control_snapshots)").fetchall()}
            for column in ("required_documents_json", "exemptions_json"):
                if column not in snapshot_columns:
                    db.execute(f"ALTER TABLE control_snapshots ADD COLUMN {column} TEXT")
            ensure_review_columns(db, "control_snapshots")
            ensure_validity_columns(db, "control_snapshots")
            ensure_validity_columns(db, "control_snapshots", (("effective_clause", "TEXT"),))
            columns = {row[1] for row in db.execute("PRAGMA table_info(control_scope)").fetchall()}
            if "excluded" not in columns:
                db.execute("ALTER TABLE control_scope ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
            if "list_kind" not in columns:
                # The primary key gains list_kind; SQLite cannot alter keys in place,
                # so the derived table is rebuilt and existing rows keep kind "scope".
                db.executescript(
                    """
                    ALTER TABLE control_scope RENAME TO control_scope_legacy;
                    CREATE TABLE control_scope (
                        snapshot_id TEXT NOT NULL REFERENCES control_snapshots(id) ON DELETE CASCADE,
                        gtip_prefix TEXT NOT NULL,
                        description TEXT,
                        source_line TEXT NOT NULL,
                        source_offset INTEGER NOT NULL,
                        excluded INTEGER NOT NULL DEFAULT 0,
                        list_kind TEXT NOT NULL DEFAULT 'scope',
                        PRIMARY KEY (snapshot_id, gtip_prefix, list_kind)
                    );
                    INSERT INTO control_scope
                        (snapshot_id, gtip_prefix, description, source_line, source_offset, excluded, list_kind)
                    SELECT snapshot_id, gtip_prefix, description, source_line, source_offset, excluded, 'scope'
                    FROM control_scope_legacy;
                    DROP TABLE control_scope_legacy;
                    CREATE INDEX IF NOT EXISTS idx_control_scope_gtip ON control_scope(gtip_prefix);
                    """
                )

    async def close(self) -> None:
        await self._client.close()
        await self._attachment_http.aclose()

    async def _pace(self) -> None:
        delay = self._request_interval - (time.monotonic() - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _discover_documents(self) -> dict[str, Any]:
        """Discover the current annual set with a few paged searches.

        One broad official search replaces one search per communique, which is
        both faster and substantially friendlier to the public Bedesten API.
        """
        documents: dict[str, Any] = {}
        for page in range(1, 5):
            result = None
            for attempt in range(5):
                async with self._request_lock:
                    await self._pace()
                    result = await self._client.search_documents(
                        mevzuat_adi="Denetimi Tebliği",
                        resmi_gazete_tarihi_start=f"31/12/{self.year - 1}",
                        resmi_gazete_tarihi_end=f"31/12/{self.year - 1}",
                        page=page,
                        page_size=20,
                    )
                    self._last_request_at = time.monotonic()
                if not result.error_message:
                    break
                if "429" not in result.error_message and "Too Many" not in result.error_message:
                    raise RuntimeError(result.error_message)
                await asyncio.sleep(min(30, 3 * (2 ** attempt)))
            if result is None or result.error_message:
                raise RuntimeError(result.error_message if result else "Resmî arama yanıt vermedi")
            for document in result.documents:
                for config in self.rules_config:
                    if communique_code_matches(config["code"], document.mevzuat_adi):
                        if config["code"] in documents and documents[config["code"]].mevzuat_id != document.mevzuat_id:
                            self._errors.append(
                                f"{config['code']}: birden fazla resmî belge eşleşti; ilk bulunan korundu"
                            )
                            continue
                        documents[config["code"]] = document
            if len(result.documents) < 20 or len(documents) >= len(self.rules_config):
                break
        return documents

    async def _fetch_rule(
        self,
        config: dict[str, Any],
        document: Any,
        semaphore: asyncio.Semaphore,
    ) -> tuple[dict[str, Any], str, Any, str]:
        async with semaphore:
            text = ""
            raw_html = ""
            for attempt in range(4):
                async with self._request_lock:
                    await self._pace()
                    text = await self._client.get_document_plain_text(document.mevzuat_id)
                    self._last_request_at = time.monotonic()
                if text:
                    # get_document_plain_text populated the Bedesten client's
                    # cache, so this exposes official attachment hrefs without
                    # a second network request.
                    content = await self._client.get_document_content(document.mevzuat_id)
                    raw_html = content.content
                    break
                await asyncio.sleep(min(30, 3 * (2 ** attempt)))
            if not text:
                raise RuntimeError(f"{config['code']} resmî metni boş döndü")
            return config, text, document, raw_html

    @staticmethod
    def _official_attachment_url(raw_html: str, document_attachments: list[str] | None = None) -> str | None:
        candidates = list(document_attachments or [])
        candidates.extend(re.findall(r"(?is)href\s*=\s*['\"]([^'\"]+)['\"]", raw_html))
        for candidate in candidates:
            href = html.unescape(candidate).strip()
            if not href.casefold().endswith(".zip"):
                continue
            url = urljoin("https://www.mevzuat.gov.tr/MevzuatMetin/", href)
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname in {"mevzuat.gov.tr", "www.mevzuat.gov.tr"}:
                return url
        return None

    async def _download_attachment(
        self,
        raw_html: str,
        document_attachments: list[str] | None = None,
    ) -> tuple[bytes, str]:
        url = self._official_attachment_url(raw_html, document_attachments)
        if not url:
            raise RuntimeError("Resmî metindeki ek arşivi bağlantısı bulunamadı")
        current_url = url
        for _ in range(6):
            validate_outbound_url(current_url, allowed_hosts={"mevzuat.gov.tr"})
            async with self._attachment_http.stream("GET", current_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise RuntimeError("Resmî ek arşivi yönlendirmesi hedefsiz")
                    current_url = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                resolved = str(response.url)
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > 50 * 1024 * 1024:
                    raise RuntimeError("Resmî ek arşivi 50 MB güvenlik sınırını aşıyor")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > 50 * 1024 * 1024:
                        raise RuntimeError("Resmî ek arşivi 50 MB güvenlik sınırını aşıyor")
                    chunks.append(chunk)
                return b"".join(chunks), resolved
        raise RuntimeError("Resmî ek arşivi çok fazla yönlendirme yaptı")

    async def sync(self, force: bool = False) -> ControlSyncStatus:
        async with self._sync_lock:
            if not force and self._fresh():
                return self.status()
            self._syncing = True
            self._errors = []
            checked_at = _now()
            try:
                try:
                    discovered = await self._discover_documents()
                except Exception as exc:
                    self._errors.append(f"Tebliğ listesi: {exc}")
                    discovered = {}
                semaphore = asyncio.Semaphore(3)
                pending: list[tuple[dict[str, Any], Any]] = []
                for config in self.rules_config:
                    document = discovered.get(config["code"])
                    if document is None:
                        if not config.get("optional"):
                            self._errors.append(f"{config['code']}: güncel resmî metin listede bulunamadı")
                        continue
                    pending.append((config, document))
                # Sequential reads are intentional: the official public service
                # enforces a conservative request budget and returns 429 when a
                # cold start fans out all annual communiques at once.
                results: list[Any] = []
                for config, document in pending:
                    try:
                        results.append(await self._fetch_rule(config, document, semaphore))
                    except Exception as exc:
                        results.append(exc)
                successes = 0
                for (config, _), result in zip(pending, results):
                    errors_before = len(self._errors)
                    if isinstance(result, Exception):
                        if not config.get("optional"):
                            self._errors.append(f"{config['code']}: {result}")
                        continue
                    _, text, document, raw_html = result
                    attachment_digest = ""
                    if config.get("scope_table"):
                        table = config["scope_table"]
                        scope = extract_scope_table(text, table["start_pattern"], table["end_pattern"])
                    else:
                        scope = []
                        for index, step in enumerate(annex_plan(config)):
                            annex_rows = extract_annex_scope(text, step["annex"])
                            for row in annex_rows:
                                row.list_kind = step["kind"]
                            if not annex_rows and index > 0:
                                # Extra annexes are best effort; the primary one stays mandatory.
                                self._errors.append(
                                    f"{config['code']}: Ek-{step['annex']} ({step['kind']}) listesi ayrıştırılamadı"
                                )
                            scope.extend(annex_rows)
                    if config.get("scope_attachment"):
                        try:
                            attachment, _ = await self._download_attachment(raw_html, document.ekler)
                            scope = extract_attachment_scope(
                                attachment,
                                self._converter,
                                list(config.get("scope_attachment_member_suffixes", [])),
                            )
                            attachment_digest = hashlib.sha256(attachment).hexdigest()
                        except Exception as exc:
                            self._errors.append(f"{config['code']}: resmî ek arşivi işlenemedi ({exc})")
                            continue
                    if not scope:
                        location = (
                            "resmî ek arşivi" if config.get("scope_attachment")
                            else f"Ek-{annex_plan(config)[0]['annex']}"
                        )
                        self._errors.append(f"{config['code']}: {location} GTİP kapsamı ayrıştırılamadı")
                        continue
                    digest_material = text.encode("utf-8") + attachment_digest.encode("ascii")
                    digest = hashlib.sha256(digest_material).hexdigest()
                    snapshot_id = hashlib.sha256(f"{config['code']}:{digest}".encode()).hexdigest()[:32]
                    process = infer_process(text, document.mevzuat_adi)
                    documents = extract_required_documents(text)
                    documents_json = json.dumps(
                        [item.model_dump(mode="json") for item in structure_document_list(documents)], ensure_ascii=False
                    )
                    exemptions_json = json.dumps(extract_exemptions(text), ensure_ascii=False)
                    retrieved_at = _now()
                    source_url = document.url or f"https://www.mevzuat.gov.tr/MevzuatMetin/9.5.{document.mevzuat_no}.pdf"
                    effective_date, effective_clause, effective_basis = extract_effective_date(
                        text, document.resmi_gazete_tarihi, floor=self.valid_from
                    )
                    valid_from = effective_date or self.valid_from
                    with self._connect() as db:
                        existing = db.execute("SELECT * FROM control_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                        if existing is not None and row_review_fields(existing)["status"] != "approved":
                            # Same official text as a rejected or still-pending version: nothing to do.
                            successes += 1
                            continue
                        previous = db.execute(
                            "SELECT * FROM control_snapshots WHERE code=? AND id<>? AND status='approved' "
                            "ORDER BY active DESC, retrieved_at DESC LIMIT 1",
                            (config["code"], snapshot_id),
                        ).fetchone()
                        db.execute(
                            """
                            INSERT OR REPLACE INTO control_snapshots (
                                id, code, title, category, mevzuat_id, source_url,
                                official_gazette_date, official_gazette_number,
                                document_sha256, retrieved_at, valid_from, scope_count,
                                authority, system, risk_based,
                                physical_inspection_possible, laboratory_test_possible,
                                required_documents_excerpt, required_documents_json, exemptions_json, active, status,
                                valid_from_basis, effective_clause
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'pending_review',?,?)
                            """,
                            (
                                snapshot_id, config["code"], document.mevzuat_adi, config["category"],
                                document.mevzuat_id, source_url, document.resmi_gazete_tarihi,
                                document.resmi_gazete_sayisi, digest, retrieved_at, valid_from,
                                sum(not row.excluded for row in scope), process["authority"], process["system"],
                                int(process["risk_based"]), int(process["physical_inspection_possible"]),
                                int(process["laboratory_test_possible"]), documents, documents_json, exemptions_json,
                                effective_basis, effective_clause,
                            ),
                        )
                        db.execute("DELETE FROM control_scope WHERE snapshot_id=?", (snapshot_id,))
                        db.executemany(
                            """INSERT OR REPLACE INTO control_scope
                               (snapshot_id, gtip_prefix, description, source_line, source_offset, excluded, list_kind)
                               VALUES (?,?,?,?,?,?,?)""",
                            [
                                (
                                    snapshot_id, row.gtip_prefix, row.description, row.source_line,
                                    row.source_offset, int(row.excluded), row.list_kind,
                                )
                                for row in scope
                            ],
                        )
                        current_rows = self._scope_rows_by_key(db, snapshot_id)
                        previous_rows = self._scope_rows_by_key(db, previous["id"]) if previous is not None else {}
                    parse_warnings = self._errors[errors_before:]
                    changes = diff_rows(current_rows, previous_rows, fields=("description", "excluded")) if previous is not None else []
                    summary = DiffSummary(
                        total_rows=len(current_rows), previous_rows=len(previous_rows),
                        added=sum(1 for c in changes if c["change_type"] == "added"),
                        removed=sum(1 for c in changes if c["change_type"] == "removed"),
                        modified=sum(1 for c in changes if c["change_type"] == "modified"),
                    )
                    if existing is not None:
                        # Re-parsed approved text (parser update): keep it live without a new review.
                        decision = decide(ReviewPolicy(), summary)
                    else:
                        decision = decide(self.review_policy, summary, parse_warnings=parse_warnings, first_snapshot=previous is None)
                    warnings_json, diff_json = review_metadata(summary, decision, parse_warnings)
                    with self._connect() as db:
                        db.execute(
                            "UPDATE control_snapshots SET status=?, parse_warnings_json=?, diff_summary_json=? WHERE id=?",
                            (decision.status, warnings_json, diff_json, snapshot_id),
                        )
                        if not decision.pending:
                            self._activate(db, config["code"], snapshot_id)
                    successes += 1
                    self._record_ledger_batch(snapshot_id, previous, parse_warnings=parse_warnings, changes=changes, review_status=decision.status)
                    if decision.pending:
                        logger.info("Control snapshot %s (%s) waits for editorial review: %s", snapshot_id, config["code"], "; ".join(decision.reasons))
                with self._connect() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO control_meta(key,value) VALUES('last_checked_at',?)",
                        (checked_at,),
                    )
                if successes == 0 and not self._errors:
                    self._errors.append("Hiçbir kontrol tebliği güncellenemedi")
            finally:
                self._syncing = False
            return self.status()

    # ---------------------------------------------------------- change ledger
    def _scope_rows_by_key(self, db: sqlite3.Connection, snapshot_id: str) -> dict[str, dict[str, Any]]:
        rows = db.execute(
            "SELECT gtip_prefix, list_kind, description, excluded FROM control_scope WHERE snapshot_id=?", (snapshot_id,)
        ).fetchall()
        return {
            f"{row['gtip_prefix']}|{row['list_kind']}": {
                "gtip": row["gtip_prefix"], "list_kind": row["list_kind"],
                "description": row["description"], "excluded": bool(row["excluded"]),
            }
            for row in rows
        }

    def _record_ledger_batch(
        self,
        snapshot_id: str,
        previous: sqlite3.Row | None,
        *,
        parse_warnings: list[str] | None = None,
        backfilled: bool = False,
        changes: list[dict[str, Any]] | None = None,
        review_status: str | None = None,
    ) -> str | None:
        if self.ledger is None:
            return None
        try:
            with self._connect() as db:
                snapshot = db.execute("SELECT * FROM control_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if snapshot is None:
                    return None
                current_rows = self._scope_rows_by_key(db, snapshot_id)
                if changes is None:
                    previous_rows = self._scope_rows_by_key(db, previous["id"]) if previous is not None else {}
                    changes = diff_rows(current_rows, previous_rows, fields=("description", "excluded")) if previous is not None else []
            return self.ledger.record_batch(
                kind="controls",
                source_id=snapshot["code"],
                title=snapshot["title"],
                new_snapshot_id=snapshot["id"],
                old_snapshot_id=previous["id"] if previous is not None else None,
                source_url=snapshot["source_url"],
                sha256=snapshot["document_sha256"],
                changes=changes,
                total_rows=len(current_rows),
                parse_warnings=parse_warnings or [],
                detected_at=snapshot["retrieved_at"],
                review_status=review_status or row_review_fields(snapshot)["status"],
                valid_from=snapshot["valid_from"],
                gazette_date=snapshot["official_gazette_date"],
                gazette_number=snapshot["official_gazette_number"],
                backfilled=backfilled,
            )
        except Exception as exc:  # noqa: BLE001 – the ledger must never break a sync
            logger.warning("Control change ledger write failed: %s", exc)
            return None

    def backfill_ledger(self) -> int:
        if self.ledger is None:
            return 0
        written = 0
        with self._connect() as db:
            snapshots = db.execute("SELECT id, code FROM control_snapshots ORDER BY code, retrieved_at ASC").fetchall()
        by_code: dict[str, list[sqlite3.Row]] = {}
        for row in snapshots:
            by_code.setdefault(row["code"], []).append(row)
        for versions in by_code.values():
            previous = None
            for snapshot in versions:
                if not self.ledger.has_batch(batch_id_for("controls", snapshot["code"], snapshot["id"])):
                    if self._record_ledger_batch(snapshot["id"], previous, backfilled=True):
                        written += 1
                previous = snapshot
        return written

    def _fresh(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM control_meta WHERE key='last_checked_at'").fetchone()
            active_codes = {
                item["code"]
                for item in db.execute("SELECT code FROM control_snapshots WHERE active=1").fetchall()
            }
        required_codes = {item["code"] for item in self.rules_config if not item.get("optional")}
        if not required_codes.issubset(active_codes):
            # A partially parsed annual set must be retried on restart instead
            # of being treated as fresh for the full sync interval.
            return False
        if not row:
            return False
        try:
            checked = datetime.fromisoformat(row["value"])
            age = (datetime.now(checked.tzinfo) - checked).total_seconds()
            return age < self.sync_interval_seconds
        except (ValueError, TypeError):
            return False

    # ---------------------------------------------------------- temporal validity
    @staticmethod
    def _activate(db: sqlite3.Connection, code: str, snapshot_id: str) -> None:
        new = db.execute("SELECT * FROM control_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        for old in db.execute("SELECT * FROM control_snapshots WHERE code=? AND active=1 AND id<>?", (code, snapshot_id)).fetchall():
            valid_to, basis = close_previous(str(new["valid_from"]), str(old["valid_from"]), str(new["retrieved_at"]))
            db.execute("UPDATE control_snapshots SET valid_to=?, valid_to_basis=? WHERE id=?", (valid_to, basis, old["id"]))
        db.execute("UPDATE control_snapshots SET active=0 WHERE code=?", (code,))
        db.execute("UPDATE control_snapshots SET active=1, valid_to=NULL, valid_to_basis=NULL WHERE id=?", (snapshot_id,))

    def _select_snapshot_ids(self, db: sqlite3.Connection, as_of: str | None) -> list[sqlite3.Row] | None:
        """None → use the active flag; otherwise the approved rows covering ``as_of`` (one per code)."""
        if not as_of or as_of >= today_iso():
            return None
        rows = db.execute("SELECT * FROM control_snapshots WHERE status='approved' ORDER BY code, retrieved_at DESC").fetchall()
        chosen: dict[str, sqlite3.Row] = {}
        for row in rows:
            if row["code"] not in chosen and covers(row, as_of):
                chosen[row["code"]] = row
        return list(chosen.values())

    def backfill_validity(self) -> int:
        updated = 0
        with self._connect() as db:
            rows = db.execute("SELECT * FROM control_snapshots WHERE status='approved' ORDER BY code, retrieved_at ASC").fetchall()
            by_code: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_code.setdefault(row["code"], []).append(row)
            for versions in by_code.values():
                for older, newer in zip(versions, versions[1:]):
                    if older["active"] or older["valid_to"]:
                        continue
                    valid_to, basis = close_previous(str(newer["valid_from"]), str(older["valid_from"]), str(newer["retrieved_at"]))
                    db.execute("UPDATE control_snapshots SET valid_to=?, valid_to_basis=? WHERE id=?", (valid_to, basis, older["id"]))
                    updated += 1
        return updated

    # ---------------------------------------------------------- editorial review
    def _review_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "kind": "controls", "snapshot_id": row["id"], "source_id": row["code"], "title": row["title"],
            "source_url": row["source_url"], "sha256": row["document_sha256"], "retrieved_at": row["retrieved_at"],
            "valid_from": row["valid_from"], "total_rows": int(row["scope_count"] or 0), "active": bool(row["active"]),
            "gazette_date": row["official_gazette_date"], "gazette_number": row["official_gazette_number"],
            "ledger_batch": batch_id_for("controls", row["code"], row["id"]) if self.ledger is not None else None,
        }
        item.update(row_review_fields(row))
        return item

    def pending_reviews(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM control_snapshots WHERE status='pending_review' ORDER BY retrieved_at DESC"
            ).fetchall()
        return [self._review_item(row) for row in rows]

    def review_snapshot(self, snapshot_id: str, action: str, *, reviewed_by: str, note: str = "") -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError("Karar 'approve' veya 'reject' olmalıdır.")
        now = _now()
        with self._connect() as db:
            row = db.execute("SELECT * FROM control_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            if action == "approve":
                db.execute(
                    "UPDATE control_snapshots SET status='approved', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (reviewed_by, now, note, snapshot_id),
                )
                self._activate(db, row["code"], snapshot_id)
            else:
                db.execute(
                    "UPDATE control_snapshots SET active=0, status='rejected', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (reviewed_by, now, note, snapshot_id),
                )
                if row["active"]:
                    fallback = db.execute(
                        "SELECT id FROM control_snapshots WHERE code=? AND status='approved' AND id<>? ORDER BY retrieved_at DESC LIMIT 1",
                        (row["code"], snapshot_id),
                    ).fetchone()
                    if fallback:
                        db.execute("UPDATE control_snapshots SET active=1 WHERE id=?", (fallback["id"],))
            updated = db.execute("SELECT * FROM control_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return self._review_item(updated)

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> ControlSnapshot:
        return ControlSnapshot(
            id=row["id"], code=row["code"], title=row["title"], mevzuat_id=row["mevzuat_id"],
            document_sha256=row["document_sha256"], retrieved_at=row["retrieved_at"],
            valid_from=row["valid_from"], scope_count=row["scope_count"], active=bool(row["active"]),
            valid_to=row["valid_to"] if "valid_to" in row.keys() else None,
            valid_from_basis=(row["valid_from_basis"] if "valid_from_basis" in row.keys() else None) or "config",
        )

    @staticmethod
    def _rule(row: sqlite3.Row) -> ImportControlRule:
        def _load_json_list(source: sqlite3.Row, column: str) -> list[Any]:
            if column not in source.keys() or not source[column]:
                return []
            try:
                value = json.loads(source[column])
            except (TypeError, ValueError):
                return []
            return value if isinstance(value, list) else []

        return ImportControlRule(
            code=row["code"], title=row["title"], category=row["category"], authority=row["authority"],
            system=row["system"], risk_based=bool(row["risk_based"]),
            physical_inspection_possible=bool(row["physical_inspection_possible"]),
            laboratory_test_possible=bool(row["laboratory_test_possible"]),
            required_documents_excerpt=row["required_documents_excerpt"],
            required_documents=_load_json_list(row, "required_documents_json") or structure_document_list(row["required_documents_excerpt"]),
            exemptions=[str(item) for item in _load_json_list(row, "exemptions_json")],
            scope_count=row["scope_count"],
            mevzuat_id=row["mevzuat_id"], source_url=row["source_url"],
            official_gazette_date=row["official_gazette_date"],
            official_gazette_number=row["official_gazette_number"], document_sha256=row["document_sha256"],
            retrieved_at=row["retrieved_at"], valid_from=row["valid_from"], snapshot_id=row["id"],
        )

    def status(self) -> ControlSyncStatus:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM control_snapshots WHERE active=1 ORDER BY code").fetchall()
            meta = db.execute("SELECT value FROM control_meta WHERE key='last_checked_at'").fetchone()
            count = db.execute(
                """SELECT COUNT(*) AS n FROM control_scope s
                   JOIN control_snapshots d ON d.id=s.snapshot_id
                   WHERE d.active=1 AND s.excluded=0"""
            ).fetchone()["n"]
            pending = db.execute("SELECT COUNT(*) FROM control_snapshots WHERE status='pending_review'").fetchone()[0]
        snapshots = [self._snapshot(row) for row in rows]
        required_codes = {item["code"] for item in self.rules_config if not item.get("optional")}
        active_codes = {item.code for item in snapshots}
        return ControlSyncStatus(
            pending_review_count=int(pending), review_mode=self.review_policy.mode,
            ready=bool(required_codes) and required_codes.issubset(active_codes), syncing=self._syncing,
            last_checked_at=meta["value"] if meta else None, active_snapshots=snapshots,
            scope_count=count, errors=list(self._errors), sync_interval_seconds=self.sync_interval_seconds,
        )

    async def lookup(self, gtip: str, *, as_of: str | None = None) -> ImportControlLookupResult:
        code = _normalise_gtip(gtip)
        if code is None or len(code) not in {4, 6, 8, 10, 12}:
            raise ValueError("Kontrol sorgusu için 4, 6, 8, 10 veya 12 haneli GTİP gereklidir.")
        as_of = normalise_as_of(as_of)
        if not self.status().ready:
            return ImportControlLookupResult(
                status="unavailable", gtip=code,
                scope_determination="unavailable",
                warnings=["Resmî kontrol tebliğleri arka planda eşitleniyor; uygunluk hakkında sonuç verilmedi. Kısa süre sonra yeniden deneyin."],
                as_of=_now(), as_of_date=as_of or today_iso(), validity_basis="unavailable",
            )
        with self._connect() as db:
            selected = self._select_snapshot_ids(db, as_of)
            if selected is None:
                selected_rows = db.execute("SELECT * FROM control_snapshots WHERE active=1").fetchall()
                snapshot_filter, params = "d.active=1", []
            else:
                selected_rows = selected
                if not selected_rows:
                    return ImportControlLookupResult(
                        status="unavailable", gtip=code, scope_determination="unavailable",
                        warnings=[f"{as_of} tarihini kapsayan onaylı kontrol tebliği sürümü arşivde yok."],
                        as_of=_now(), as_of_date=as_of, validity_basis="unavailable",
                    )
                placeholders = ",".join("?" for _ in selected_rows)
                snapshot_filter, params = f"d.id IN ({placeholders})", [row["id"] for row in selected_rows]
            rows = db.execute(
                f"""
                SELECT d.*, s.gtip_prefix, s.description, s.source_line, s.source_offset, s.excluded, s.list_kind
                FROM control_scope s
                JOIN control_snapshots d ON d.id=s.snapshot_id
                WHERE {snapshot_filter} AND (
                    substr(?,1,length(s.gtip_prefix))=s.gtip_prefix
                    OR substr(s.gtip_prefix,1,length(?))=?
                )
                ORDER BY length(s.gtip_prefix) DESC, d.code
                """,
                (*params, code, code, code),
            ).fetchall()
        validity_rows = [row for row in selected_rows if any(row["id"] == hit["id"] for hit in rows)] or selected_rows
        bases = {validity_basis(row, as_of) for row in validity_rows}
        basis = "observed" if "observed" in bases else "legal" if "legal" in bases else "current"
        temporal_warnings: list[str] = []
        if as_of and as_of < today_iso():
            temporal_warnings.append(
                f"Sonuç {as_of} tarihinde yürürlükte olan tebliğ sürümlerinden üretildi"
                + ("; sürüm sınırı indirme tarihlerinden türetildi, o günkü resmî metin ayrıca doğrulanmalıdır." if basis == "observed" else ".")
            )
        validity_info = [snapshot_validity(row) for row in validity_rows]
        excluded_by_rule: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            if row["excluded"]:
                excluded_by_rule.setdefault(row["code"], []).append(row)
        matches: list[ImportControlMatch] = []
        for row in rows:
            if row["excluded"] or row["code"] in excluded_by_rule:
                continue
            rule = self._rule(row)
            list_kind = row["list_kind"] if "list_kind" in row.keys() else "scope"
            if list_kind == "prohibited":
                risk_sentence = (
                    "GTİP tebliğin ithali yasak eşya listesinde yer alıyor; kapsam istisnası yoksa ithalat izni verilmez."
                )
            elif rule.risk_based:
                risk_sentence = "GTİP tebliğ ekinde yer alıyor; fiilî denetime yönlendirme TAREKS risk analiziyle belirlenir."
            else:
                risk_sentence = "GTİP tebliğ ekinde yer alıyor; işlem sonucu yetkili kurumun incelemesine bağlıdır."
            cautions = [
                "Bu eşleşme tek başına ürünün teknik kapsamda olduğunu veya fiilî denetime seçildiğini kanıtlamaz; ürün niteliği ve istisnalar kontrol edilmelidir."
            ]
            if list_kind == "prohibited":
                cautions.insert(0, "Yasak listesi eşleşmesi: ürünün liste tanımına ve tebliğdeki istisnalara uyup uymadığı resmî metinden teyit edilmelidir.")
            if rule.laboratory_test_possible:
                cautions.append(
                    "Laboratuvar testi mevzuatta mümkün bir fiilî denetim yöntemidir; belirli bir özel laboratuvar otomatik veya zorunlu kabul edilmemiştir."
                )
            matches.append(
                ImportControlMatch(
                    rule=rule,
                    matched_scope=ControlScopeRow(
                        gtip_prefix=row["gtip_prefix"], description=row["description"],
                        source_line=row["source_line"], source_offset=row["source_offset"],
                        excluded=False, list_kind=list_kind,
                    ),
                    match_type="exact" if len(row["gtip_prefix"]) == 12 else "prefix",
                    assessment=risk_sentence,
                    cautions=cautions,
                )
            )
        warnings = [
            (
                f"{rule_code} tebliğinde {', '.join(item['gtip_prefix'] for item in exclusions)} "
                "'hariç' hükmü eşleşti; bu tebliğ için pozitif kapsam sonucu üretilmedi."
            )
            for rule_code, exclusions in sorted(excluded_by_rule.items())
        ]
        if not matches:
            warnings.append(
                "GTİP, indekslenen güncel tebliğ eklerinde bulunamadı. Bu sonuç 'kontrole tabi değildir' anlamına gelmez; ürün niteliği, başka izin mevzuatı ve güncel değişiklikler ayrıca incelenmelidir."
            )
        return ImportControlLookupResult(
            status="matched" if matches else "not_found", gtip=code, matches=matches,
            scope_determination="annex_match" if matches else "no_indexed_match",
            warnings=warnings + temporal_warnings, as_of=_now(),
            as_of_date=as_of or today_iso(), validity_basis=basis, snapshot_validity=validity_info,
        )

    def changes(self, code: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 200))
        if code:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM control_snapshots WHERE code=? ORDER BY retrieved_at DESC LIMIT ?",
                    (code, bounded_limit),
                ).fetchall()
        else:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM control_snapshots ORDER BY retrieved_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["code"], []).append(row)
        result: list[dict[str, Any]] = []
        for rule_code, versions in grouped.items():
            for newer, older in zip(versions, versions[1:]):
                if newer["document_sha256"] == older["document_sha256"]:
                    continue
                result.append(
                    {
                        "code": rule_code,
                        "title": newer["title"],
                        "new_snapshot_id": newer["id"],
                        "old_snapshot_id": older["id"],
                        "new_scope_count": newer["scope_count"],
                        "old_scope_count": older["scope_count"],
                        "scope_count_delta": newer["scope_count"] - older["scope_count"],
                        "changed_at": newer["retrieved_at"],
                    }
                )
        return result[:limit]

    def search_controls(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Ürün adı, kelime veya GTİP parçasıyla denetim tebliğleri ve Ek-1 kapsamlarını arar."""
        text = str(query or "").strip()
        if not text:
            return []
        bounded_limit = max(1, min(limit, 100))
        wildcard = f"%{text}%"
        normalised_num = _normalise_gtip(text) or ""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT s.gtip_prefix, s.description, s.list_kind, d.code, d.title, d.authority,
                       d.system, d.source_url, d.required_documents_json, d.exemptions_json
                FROM control_scope s
                JOIN control_snapshots d ON d.id=s.snapshot_id
                WHERE d.active=1 AND s.excluded=0 AND (
                    s.description LIKE ?
                    OR d.title LIKE ?
                    OR d.code LIKE ?
                    OR (? != '' AND s.gtip_prefix LIKE ?)
                )
                ORDER BY d.code, s.gtip_prefix
                LIMIT ?
                """,
                (wildcard, wildcard, wildcard, normalised_num, f"{normalised_num}%", bounded_limit),
            ).fetchall()
        
        results: list[dict[str, Any]] = []
        for row in rows:
            docs = []
            if row["required_documents_json"]:
                try:
                    docs = json.loads(row["required_documents_json"])
                except Exception:
                    docs = []
            exemptions = []
            if row["exemptions_json"]:
                try:
                    exemptions = json.loads(row["exemptions_json"])
                except Exception:
                    exemptions = []
            results.append({
                "gtip_prefix": row["gtip_prefix"],
                "gtip": row["gtip_prefix"],
                "description": row["description"],
                "commodity_name": row["description"],
                "list_kind": row["list_kind"],
                "communique_code": row["code"],
                "communique_title": row["title"],
                "authority": row["authority"],
                "system": row["system"],
                "source_url": row["source_url"],
                "required_documents": docs,
                "exemptions": exemptions,
                "rule": {
                    "code": row["code"],
                    "title": row["title"],
                    "system": row["system"],
                    "source_url": row["source_url"],
                    "required_documents": docs,
                    "exemptions": exemptions,
                },
            })
        return results

    def get_communiques_catalog(self) -> list[dict[str, Any]]:
        """Tüm güncel Ürün Güvenliği ve Denetimi tebliğlerinin fihristini döndürür."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, code, title, category, authority, system, risk_based,
                       physical_inspection_possible, laboratory_test_possible,
                       required_documents_excerpt, required_documents_json, exemptions_json,
                       scope_count, source_url, official_gazette_date, official_gazette_number
                FROM control_snapshots
                WHERE active=1
                ORDER BY CAST(SUBSTR(code, INSTR(code, '/') + 1) AS INTEGER) ASC, code ASC
                """
            ).fetchall()
        catalog: list[dict[str, Any]] = []
        for row in rows:
            docs = []
            if row["required_documents_json"]:
                try:
                    docs = json.loads(row["required_documents_json"])
                except Exception:
                    docs = []
            if not docs and row["required_documents_excerpt"]:
                raw_docs = structure_document_list(row["required_documents_excerpt"])
                docs = [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in raw_docs]
            exemptions = []
            if row["exemptions_json"]:
                try:
                    exemptions = json.loads(row["exemptions_json"])
                except Exception:
                    exemptions = []
            catalog.append({
                "code": row["code"],
                "title": row["title"],
                "category": row["category"],
                "authority": row["authority"],
                "system": row["system"],
                "risk_based": bool(row["risk_based"]),
                "physical_inspection_possible": bool(row["physical_inspection_possible"]),
                "laboratory_test_possible": bool(row["laboratory_test_possible"]),
                "scope_count": row["scope_count"],
                "source_url": row["source_url"],
                "official_gazette_date": row["official_gazette_date"],
                "official_gazette_number": row["official_gazette_number"],
                "required_documents": docs,
                "exemptions": exemptions,
            })
        return catalog

    async def periodic_sync_loop(self) -> None:
        while True:
            try:
                await self.sync(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - service resilience
                self._errors.append(str(exc))
            await asyncio.sleep(self.sync_interval_seconds)
