"""Versioned official classification-decision evidence index.

The index deliberately stores text and metadata only.  It does not crawl EBTI result
pages or copy applicant photographs.  Its first source is the European Commission's
official consolidated list of valid classification regulations, whose authentic acts
remain the versions published in the Official Journal/EUR-Lex.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import logging

import httpx
from pdfminer.high_level import extract_text
from pydantic import BaseModel, Field

from change_ledger import batch_id_for, diff_rows
from review_policy import DiffSummary, ReviewPolicy, decide, ensure_review_columns, review_metadata, row_review_fields
from security_firewall import sanitize_untrusted_context, validate_outbound_url

logger = logging.getLogger(__name__)

_OFFICIAL_HOSTS = {"taxation-customs.ec.europa.eu", "eur-lex.europa.eu"}
_SOURCE_URL = (
    "https://taxation-customs.ec.europa.eu/document/download/"
    "9d6824da-835d-4d09-bc02-d18a06f403f8_en"
    "?filename=Consolidated-list-of-%E2%80%9CClassification-Regulations%E2%80%9D.pdf"
)
_SOURCE_PAGE_URL = (
    "https://taxation-customs.ec.europa.eu/news/"
    "big-step-simplification-commission-publishes-consolidated-list-classification-regulations-2025-05-12_en"
)
_CODE_RE = re.compile(r"(?<!\d)(?:\d{4}(?:\s+\d{2}){1,3}|\d{6,10})(?!\d)")
_REGULATION_RE = re.compile(
    r"(?:Regulation\s*)?(?:\(EEC\)|\(EC\)|\(EU\))?\s*(?:No\s*)?"
    r"(\d{1,4}/\d{2,4})(?:\s+of\s+\d{1,2}\.\d{1,2}\.\d{4})?",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _excerpt(text: str, needles: list[str], limit: int = 1800) -> str:
    compact = " ".join(text.split())
    lowered = compact.casefold()
    positions = [lowered.find(needle.casefold()) for needle in needles if len(needle.strip()) >= 3]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - 350)
    value = compact[start : start + limit]
    if start:
        value = "… " + value
    if start + limit < len(compact):
        value += " …"
    return value


class ClassificationEvidenceHit(BaseModel):
    id: str
    source_kind: Literal["eu_classification_regulation"] = "eu_classification_regulation"
    title: str
    authority: str = "European Commission – DG TAXUD"
    codes: list[str] = Field(default_factory=list)
    regulation_references: list[str] = Field(default_factory=list)
    page_number: int = Field(..., ge=1)
    excerpt: str
    url: str
    source_page_url: str = _SOURCE_PAGE_URL
    archive_sha256: str
    retrieved_at: str
    legal_effect: str = (
        "Karşılaştırmalı AB sınıflandırma kanıtıdır; Türk GTİP12, Türkiye vergi oranı veya "
        "Türkiye'de bağlayıcı karar değildir. Otantik metin ilgili AB Resmî Gazetesi/EUR-Lex belgesidir."
    )


class ClassificationEvidenceSearchResult(BaseModel):
    status: Literal["matched", "not_found", "unavailable"]
    query: str = ""
    code_prefix: str | None = None
    hits: list[ClassificationEvidenceHit] = Field(default_factory=list)
    snapshot_sha256: str | None = None
    retrieved_at: str | None = None
    warnings: list[str] = Field(default_factory=list)
    as_of: str = Field(default_factory=_now)


class ClassificationEvidenceStatus(BaseModel):
    ready: bool
    syncing: bool
    page_count: int = 0
    active_sha256: str | None = None
    last_checked_at: str | None = None
    errors: list[str] = Field(default_factory=list)
    pending_review_count: int = 0
    review_mode: str = "off"


class ClassificationEvidenceEngine:
    """Download, version and search official classification-regulation text."""

    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        source_url: str = _SOURCE_URL,
        sync_interval_seconds: int | None = None,
    ) -> None:
        default_dir = Path(
            os.environ.get(
                "MEVZUAT_DATA_DIR",
                Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mevzuat-mcp",
            )
        )
        self.data_dir = Path(data_dir or default_dir)
        self.database_path = self.data_dir / "classification-evidence.sqlite3"
        self.source_url = source_url
        self.sync_interval_seconds = max(
            3600,
            int(sync_interval_seconds or os.environ.get("CLASSIFICATION_SYNC_INTERVAL_SECONDS", "86400")),
        )
        self._sync_lock = asyncio.Lock()
        self._syncing = False
        self._errors: list[str] = []
        # Optional unified change ledger (change_ledger.ChangeLedger); set by the server.
        self.ledger: Any = None
        # Editorial review gate; the server replaces it with policy_from_env().
        self.review_policy: ReviewPolicy = ReviewPolicy()
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(90),
            headers={"User-Agent": "Gumrukce/1.0 (+official-classification-index)"},
        )
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    archive_sha256 TEXT NOT NULL UNIQUE,
                    retrieved_at TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
                    page_number INTEGER NOT NULL,
                    codes_json TEXT NOT NULL,
                    regulations_json TEXT NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(snapshot_id, page_number)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                    page_id UNINDEXED,
                    snapshot_id UNINDEXED,
                    codes,
                    content,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            ensure_review_columns(connection, "snapshots")
        self.database_path.chmod(0o600)

    def status(self) -> ClassificationEvidenceStatus:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT archive_sha256,retrieved_at,page_count FROM snapshots WHERE active=1 LIMIT 1"
            ).fetchone()
            checked = connection.execute("SELECT value FROM metadata WHERE key='last_checked_at'").fetchone()
            pending = connection.execute("SELECT COUNT(*) FROM snapshots WHERE status='pending_review'").fetchone()[0]
        return ClassificationEvidenceStatus(
            pending_review_count=int(pending),
            review_mode=self.review_policy.mode,
            ready=bool(row),
            syncing=self._syncing,
            page_count=int(row["page_count"]) if row else 0,
            active_sha256=str(row["archive_sha256"]) if row else None,
            last_checked_at=str(checked["value"]) if checked else None,
            errors=self._errors[-8:],
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _download(self) -> bytes:
        current = self.source_url
        for _ in range(5):
            validate_outbound_url(current, allowed_hosts=_OFFICIAL_HOSTS)
            response: httpx.Response | None = None
            last_transport_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await self._http.get(current)
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_transport_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if response is None:
                if last_transport_error is None:
                    raise RuntimeError("Sınıflandırma kaynağı indirilemedi.")
                raise last_transport_error
            if not response.is_redirect:
                response.raise_for_status()
                content = response.content
                if len(content) > 15 * 1024 * 1024 or not content.startswith(b"%PDF-"):
                    raise ValueError("Sınıflandırma kaynağı beklenen resmî PDF biçiminde değil.")
                return content
            location = response.headers.get("location", "")
            if not location:
                raise ValueError("Sınıflandırma kaynağı hedefsiz yönlendirme döndürdü.")
            current = urljoin(str(response.url), location)
        raise ValueError("Sınıflandırma kaynağı çok fazla yönlendirme yaptı.")

    @staticmethod
    def _extract_pages(content: bytes) -> list[str]:
        text = extract_text(io.BytesIO(content))
        return [page.strip() for page in text.split("\f") if page.strip()]

    @staticmethod
    def _page_metadata(page: str) -> tuple[list[str], list[str]]:
        codes = sorted(
            {
                _digits(match.group(0))
                for match in _CODE_RE.finditer(page)
                if len(_digits(match.group(0))) in {6, 8, 10}
            }
        )
        regulations = list(dict.fromkeys(match.group(1) for match in _REGULATION_RE.finditer(page)))[:30]
        return codes[:80], regulations

    async def sync(self, *, force: bool = False) -> ClassificationEvidenceStatus:
        async with self._sync_lock:
            self._syncing = True
            try:
                current_status = self.status()
                if not force and current_status.last_checked_at:
                    try:
                        checked = datetime.fromisoformat(current_status.last_checked_at)
                        if (datetime.now(UTC) - checked).total_seconds() < self.sync_interval_seconds:
                            return current_status
                    except ValueError:
                        pass
                content = await self._download()
                archive_sha256 = hashlib.sha256(content).hexdigest()
                retrieved_at = _now()
                with self._connect() as connection:
                    existing = connection.execute(
                        "SELECT * FROM snapshots WHERE archive_sha256=?",
                        (archive_sha256,),
                    ).fetchone()
                    if existing:
                        if row_review_fields(existing)["status"] == "approved":
                            connection.execute("UPDATE snapshots SET active=(id=?)", (existing["id"],))
                        connection.execute(
                            "INSERT INTO metadata(key,value) VALUES('last_checked_at',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (retrieved_at,),
                        )
                        self._errors.clear()
                        return self.status()

                pages = await asyncio.to_thread(self._extract_pages, content)
                snapshot_id = f"eu-classification-{archive_sha256[:16]}"
                rows: list[tuple[str, str, int, str, str, str]] = []
                fts_rows: list[tuple[str, str, str, str]] = []
                for page_number, raw_page in enumerate(pages, start=1):
                    clean_page, _ = sanitize_untrusted_context(raw_page)
                    codes, regulations = self._page_metadata(clean_page)
                    page_id = f"{snapshot_id}-p{page_number}"
                    rows.append(
                        (
                            page_id,
                            snapshot_id,
                            page_number,
                            json.dumps(codes, ensure_ascii=False),
                            json.dumps(regulations, ensure_ascii=False),
                            clean_page[:100_000],
                        )
                    )
                    fts_rows.append((page_id, snapshot_id, " ".join(codes), clean_page[:100_000]))

                with self._connect() as connection:
                    previous = connection.execute(
                        "SELECT id FROM snapshots WHERE status='approved' ORDER BY active DESC, retrieved_at DESC LIMIT 1"
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO snapshots(id,source_url,archive_sha256,retrieved_at,page_count,active,status) "
                        "VALUES(?,?,?,?,?,0,'pending_review')",
                        (snapshot_id, self.source_url, archive_sha256, retrieved_at, len(rows)),
                    )
                    connection.executemany(
                        "INSERT INTO pages(id,snapshot_id,page_number,codes_json,regulations_json,content) "
                        "VALUES(?,?,?,?,?,?)",
                        rows,
                    )
                    connection.executemany(
                        "INSERT INTO pages_fts(page_id,snapshot_id,codes,content) VALUES(?,?,?,?)",
                        fts_rows,
                    )
                    connection.execute(
                        "INSERT INTO metadata(key,value) VALUES('last_checked_at',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (retrieved_at,),
                    )
                    current_rows = self._page_rows_by_key(connection, snapshot_id)
                    previous_rows = self._page_rows_by_key(connection, previous["id"]) if previous else {}
                changes = (
                    diff_rows(current_rows, previous_rows, fields=("content_sha256",), gtip_of=lambda row: (row.get("codes") or [None])[0])
                    if previous else []
                )
                summary = DiffSummary(
                    total_rows=len(current_rows), previous_rows=len(previous_rows),
                    added=sum(1 for c in changes if c["change_type"] == "added"),
                    removed=sum(1 for c in changes if c["change_type"] == "removed"),
                    modified=sum(1 for c in changes if c["change_type"] == "modified"),
                )
                decision = decide(self.review_policy, summary, first_snapshot=previous is None)
                warnings_json, diff_json = review_metadata(summary, decision, [])
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE snapshots SET status=?, parse_warnings_json=?, diff_summary_json=? WHERE id=?",
                        (decision.status, warnings_json, diff_json, snapshot_id),
                    )
                    if not decision.pending:
                        connection.execute("UPDATE snapshots SET active=0")
                        connection.execute("UPDATE snapshots SET active=1 WHERE id=?", (snapshot_id,))
                self._record_ledger_batch(snapshot_id, previous["id"] if previous else None, changes=changes, review_status=decision.status)
                if decision.pending:
                    logger.info("Classification snapshot %s waits for editorial review: %s", snapshot_id, "; ".join(decision.reasons))
                self._errors.clear()
            except Exception as exc:
                self._errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            finally:
                self._syncing = False
        return self.status()

    # ---------------------------------------------------------- change ledger
    def _page_rows_by_key(self, connection: sqlite3.Connection, snapshot_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT page_number, codes_json, regulations_json, content FROM pages WHERE snapshot_id=?", (snapshot_id,)
        ).fetchall()
        return {
            f"p{row['page_number']}": {
                "page_number": row["page_number"],
                "content_sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
                "codes": json.loads(row["codes_json"])[:20],
                "regulations": json.loads(row["regulations_json"])[:10],
            }
            for row in rows
        }

    def _record_ledger_batch(
        self,
        snapshot_id: str,
        previous_id: str | None,
        *,
        backfilled: bool = False,
        changes: list[dict[str, Any]] | None = None,
        review_status: str | None = None,
    ) -> str | None:
        if self.ledger is None:
            return None
        try:
            with self._connect() as connection:
                snapshot = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
                if snapshot is None:
                    return None
                current_rows = self._page_rows_by_key(connection, snapshot_id)
                if changes is None:
                    previous_rows = self._page_rows_by_key(connection, previous_id) if previous_id else {}
                    changes = (
                        diff_rows(current_rows, previous_rows, fields=("content_sha256",), gtip_of=lambda row: (row.get("codes") or [None])[0])
                        if previous_id else []
                    )
            return self.ledger.record_batch(
                kind="classification",
                source_id="eu_classification_regulations",
                title="AB sınıflandırma tüzükleri konsolide listesi",
                new_snapshot_id=snapshot["id"],
                old_snapshot_id=previous_id,
                source_url=snapshot["source_url"],
                sha256=snapshot["archive_sha256"],
                changes=changes,
                total_rows=len(current_rows),
                detected_at=snapshot["retrieved_at"],
                review_status=review_status or row_review_fields(snapshot)["status"],
                backfilled=backfilled,
            )
        except Exception as exc:  # noqa: BLE001 – the ledger must never break a sync
            logger.warning("Classification change ledger write failed: %s", exc)
            return None

    # ---------------------------------------------------------- editorial review
    def _review_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "kind": "classification", "snapshot_id": row["id"], "source_id": "eu_classification_regulations",
            "title": "AB sınıflandırma tüzükleri konsolide listesi", "source_url": row["source_url"],
            "sha256": row["archive_sha256"], "retrieved_at": row["retrieved_at"], "valid_from": None,
            "total_rows": int(row["page_count"] or 0), "active": bool(row["active"]),
            "ledger_batch": batch_id_for("classification", "eu_classification_regulations", row["id"]) if self.ledger is not None else None,
        }
        item.update(row_review_fields(row))
        return item

    def pending_reviews(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM snapshots WHERE status='pending_review' ORDER BY retrieved_at DESC").fetchall()
        return [self._review_item(row) for row in rows]

    def review_snapshot(self, snapshot_id: str, action: str, *, reviewed_by: str, note: str = "") -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError("Karar 'approve' veya 'reject' olmalıdır.")
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            if action == "approve":
                connection.execute("UPDATE snapshots SET active=0")
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
                        "SELECT id FROM snapshots WHERE status='approved' AND id<>? ORDER BY retrieved_at DESC LIMIT 1", (snapshot_id,)
                    ).fetchone()
                    if fallback:
                        connection.execute("UPDATE snapshots SET active=1 WHERE id=?", (fallback["id"],))
            updated = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return self._review_item(updated)

    def backfill_ledger(self) -> int:
        if self.ledger is None:
            return 0
        written = 0
        with self._connect() as connection:
            snapshots = connection.execute("SELECT id FROM snapshots ORDER BY retrieved_at ASC").fetchall()
        previous_id = None
        for row in snapshots:
            if not self.ledger.has_batch(batch_id_for("classification", "eu_classification_regulations", row["id"])):
                if self._record_ledger_batch(row["id"], previous_id, backfilled=True):
                    written += 1
            previous_id = row["id"]
        return written

    async def periodic_sync_loop(self) -> None:
        while True:
            status = await self.sync()
            await asyncio.sleep(self.sync_interval_seconds if status.ready else 300)

    async def search(
        self,
        query: str,
        *,
        code_prefix: str | None = None,
        limit: int = 5,
        auto_sync: bool = True,
    ) -> ClassificationEvidenceSearchResult:
        limit = max(1, min(limit, 12))
        code = _digits(code_prefix)
        if code and len(code) not in {4, 6, 8, 10}:
            raise ValueError("Sınıflandırma kanıtı kodu 4, 6, 8 veya 10 haneli olmalıdır.")
        if auto_sync and not self.status().ready:
            await self.sync()
        status = self.status()
        if not status.ready:
            return ClassificationEvidenceSearchResult(
                status="unavailable",
                query=query[:500],
                code_prefix=code or None,
                warnings=["Resmî sınıflandırma karar indeksi henüz hazır değil.", *status.errors[-3:]],
            )

        terms = [term for term in re.findall(r"[\wÀ-ž]{3,}", query.casefold()) if not term.isdigit()][:16]
        rows: list[sqlite3.Row] = []
        with self._connect() as connection:
            active = connection.execute(
                "SELECT id,archive_sha256,retrieved_at FROM snapshots WHERE active=1 LIMIT 1"
            ).fetchone()
            if code:
                rows.extend(
                    connection.execute(
                        "SELECT * FROM pages WHERE snapshot_id=? AND codes_json LIKE ? ORDER BY page_number LIMIT ?",
                        (active["id"], f'%"{code}%', limit * 3),
                    ).fetchall()
                )
            if terms and len(rows) < limit:
                fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
                rows.extend(
                    connection.execute(
                        "SELECT p.* FROM pages_fts f JOIN pages p ON p.id=f.page_id "
                        "WHERE f.snapshot_id=? AND pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ?",
                        (active["id"], fts_query, limit * 2),
                    ).fetchall()
                )

        seen: set[str] = set()
        hits: list[ClassificationEvidenceHit] = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            codes = json.loads(row["codes_json"])
            regulations = json.loads(row["regulations_json"])
            hits.append(
                ClassificationEvidenceHit(
                    id=f"classreg_{active['archive_sha256'][:10]}_p{row['page_number']}",
                    title=(
                        f"AB Sınıflandırma Tüzükleri 2026 konsolide listesi — sayfa {row['page_number']}"
                    ),
                    codes=codes,
                    regulation_references=regulations,
                    page_number=row["page_number"],
                    excerpt=_excerpt(row["content"], [code, *terms]),
                    url=self.source_url,
                    archive_sha256=active["archive_sha256"],
                    retrieved_at=active["retrieved_at"],
                )
            )
            if len(hits) >= limit:
                break
        return ClassificationEvidenceSearchResult(
            status="matched" if hits else "not_found",
            query=query[:500],
            code_prefix=code or None,
            hits=hits,
            snapshot_sha256=active["archive_sha256"],
            retrieved_at=active["retrieved_at"],
            warnings=[
                "Bu indeks AB karşılaştırmalı sınıflandırma kanıtıdır; Türk GTİP12 sonucunu tek başına belirlemez."
            ],
        )
