"""Persistent, unified change ledger for the official data snapshots.

Every sync of the tariff schedule, the import-control communiqués, the EU
classification regulations and the trade-measure lists records one *batch*
(which snapshot replaced which, checksum, source URL, row counts, parse
warnings) plus the row-level before/after diff. The on-read diffs that the
engines still compute for backwards compatibility only ever compare the two
newest snapshots; this ledger keeps the full history so a change is never
lost when a third snapshot arrives, and so the editorial review gate and the
compliance warnings (later PRs) have a durable, queryable source.

The ledger is deliberately its own SQLite file (``changes.sqlite3`` under
``MEVZUAT_DATA_DIR``): engines stay independently testable and receive the
ledger as an optional collaborator (``engine.ledger = ChangeLedger()``).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, TypedDict


class RowChange(TypedDict, total=False):
    entity_key: str
    gtip: str | None
    change_type: str  # added | removed | modified
    before: dict[str, Any] | None
    after: dict[str, Any] | None


KINDS: tuple[str, ...] = ("tariff", "controls", "classification", "trade_measures")
KIND_LABELS: dict[str, str] = {
    "tariff": "Tarife cetveli",
    "controls": "İthalat kontrol tebliğleri",
    "classification": "AB sınıflandırma tüzükleri",
    "trade_measures": "Ticaret politikası önlemleri",
}
CHANGE_TYPES = ("added", "removed", "modified")
# One annual cetvel rollover can rewrite every measure row; keep the ledger bounded.
MAX_ROWS_PER_BATCH = 50_000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def batch_id_for(kind: str, source_id: str, new_snapshot_id: str) -> str:
    return f"{kind}:{source_id}:{str(new_snapshot_id)[:24]}"


class ChangeLedger:
    """Append-only record of official data changes with row-level lineage."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or Path.home() / ".cache" / "mevzuat-mcp")
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "changes.sqlite3"
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS change_batches (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    new_snapshot_id TEXT NOT NULL,
                    old_snapshot_id TEXT,
                    detected_at TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    valid_from TEXT,
                    gazette_date TEXT,
                    gazette_number TEXT,
                    total_rows INTEGER NOT NULL DEFAULT 0,
                    added INTEGER NOT NULL DEFAULT 0,
                    removed INTEGER NOT NULL DEFAULT 0,
                    modified INTEGER NOT NULL DEFAULT 0,
                    parse_warnings_json TEXT NOT NULL DEFAULT '[]',
                    review_status TEXT NOT NULL DEFAULT 'approved',
                    truncated INTEGER NOT NULL DEFAULT 0,
                    backfilled INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_cb_kind_time ON change_batches(kind, detected_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cb_source ON change_batches(kind, source_id, detected_at DESC);
                CREATE TABLE IF NOT EXISTS data_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES change_batches(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    gtip TEXT,
                    change_type TEXT NOT NULL CHECK(change_type IN ('added','removed','modified')),
                    before_json TEXT,
                    after_json TEXT,
                    detected_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dc_kind_time ON data_changes(kind, detected_at DESC);
                CREATE INDEX IF NOT EXISTS idx_dc_gtip ON data_changes(gtip);
                CREATE INDEX IF NOT EXISTS idx_dc_batch ON data_changes(batch_id);
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ write
    def has_batch(self, batch_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM change_batches WHERE id=?", (batch_id,)).fetchone() is not None

    def record_batch(
        self,
        *,
        kind: str,
        source_id: str,
        new_snapshot_id: str,
        old_snapshot_id: str | None,
        source_url: str,
        sha256: str,
        changes: Iterable[RowChange],
        total_rows: int,
        title: str = "",
        parse_warnings: Iterable[str] = (),
        detected_at: str | None = None,
        review_status: str = "approved",
        valid_from: str | None = None,
        gazette_date: str | None = None,
        gazette_number: str | None = None,
        backfilled: bool = False,
    ) -> str:
        """Persist one snapshot transition; idempotent on the derived batch id."""
        if kind not in KINDS:
            raise ValueError(f"Bilinmeyen değişiklik türü: {kind}")
        batch_id = batch_id_for(kind, source_id, new_snapshot_id)
        detected = detected_at or _now()
        rows = list(changes)
        counts = {name: 0 for name in CHANGE_TYPES}
        for row in rows:
            change_type = str(row.get("change_type", ""))
            if change_type not in counts:
                raise ValueError(f"Geçersiz değişiklik tipi: {change_type}")
            counts[change_type] += 1
        truncated = len(rows) > MAX_ROWS_PER_BATCH
        with self._connect() as db:
            if db.execute("SELECT 1 FROM change_batches WHERE id=?", (batch_id,)).fetchone():
                return batch_id
            db.execute(
                """INSERT INTO change_batches
                (id,kind,source_id,title,new_snapshot_id,old_snapshot_id,detected_at,source_url,sha256,valid_from,
                 gazette_date,gazette_number,total_rows,added,removed,modified,parse_warnings_json,review_status,truncated,backfilled)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id, kind, source_id, title[:300], str(new_snapshot_id), old_snapshot_id, detected,
                    source_url[:1000], sha256, valid_from, gazette_date, gazette_number, int(total_rows),
                    counts["added"], counts["removed"], counts["modified"],
                    _dumps([str(item)[:500] for item in parse_warnings][:50]), review_status, int(truncated),
                    int(backfilled),
                ),
            )
            db.executemany(
                "INSERT INTO data_changes(batch_id,kind,source_id,entity_key,gtip,change_type,before_json,after_json,detected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        batch_id, kind, source_id, str(row.get("entity_key", ""))[:300],
                        (str(row["gtip"]) if row.get("gtip") else None), row["change_type"],
                        _dumps(row.get("before")) if row.get("before") is not None else None,
                        _dumps(row.get("after")) if row.get("after") is not None else None,
                        detected,
                    )
                    for row in rows[:MAX_ROWS_PER_BATCH]
                ],
            )
        return batch_id

    def set_review_status(self, batch_id: str, status: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE change_batches SET review_status=? WHERE id=?", (status, batch_id))

    # ------------------------------------------------------------------- read
    @staticmethod
    def _batch(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["parse_warnings"] = json.loads(item.pop("parse_warnings_json") or "[]")
        item["label"] = KIND_LABELS.get(item["kind"], item["kind"])
        item["truncated"] = bool(item["truncated"])
        item["backfilled"] = bool(item["backfilled"])
        return item

    @staticmethod
    def _change(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["before"] = json.loads(item.pop("before_json")) if item.get("before_json") else None
        item["after"] = json.loads(item.pop("after_json")) if item.get("after_json") else None
        return item

    def latest_batch(self, kind: str, source_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM change_batches WHERE kind=? AND source_id=? ORDER BY detected_at DESC, rowid DESC LIMIT 1",
                (kind, source_id),
            ).fetchone()
        return self._batch(row) if row else None

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM change_batches WHERE id=?", (batch_id,)).fetchone()
        return self._batch(row) if row else None

    def batches(self, *, kind: str | None = None, source_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM change_batches{where} ORDER BY detected_at DESC, rowid DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._batch(row) for row in rows]

    def changes(
        self,
        *,
        kind: str | None = None,
        source_id: str | None = None,
        batch_id: str | None = None,
        gtip_prefix: str | None = None,
        since: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if gtip_prefix:
            digits = "".join(ch for ch in str(gtip_prefix) if ch.isdigit())
            if digits:
                # A watched 6-digit code matches both longer official rows and shorter parents.
                clauses.append("(gtip LIKE ? OR ? LIKE gtip || '%')")
                params.extend([f"{digits}%", digits])
        if since:
            clauses.append("detected_at>=?")
            params.append(str(since))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM data_changes{where} ORDER BY detected_at DESC, id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [self._change(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._connect() as db:
            per_kind = db.execute(
                "SELECT kind, COUNT(*) batches, SUM(added) added, SUM(removed) removed, SUM(modified) modified, MAX(detected_at) last "
                "FROM change_batches GROUP BY kind"
            ).fetchall()
            pending = int(db.execute("SELECT COUNT(*) FROM change_batches WHERE review_status='pending_review'").fetchone()[0])
        return {
            "kinds": {row["kind"]: {**dict(row), "label": KIND_LABELS.get(row["kind"], row["kind"])} for row in per_kind},
            "pending_review": pending,
        }


def diff_rows(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    *,
    fields: Iterable[str],
    gtip_of=None,
) -> list[RowChange]:
    """Generic keyed diff: rows present only on one side or differing in ``fields``."""
    tracked = tuple(fields)
    result: list[RowChange] = []

    def gtip(row: dict[str, Any] | None) -> str | None:
        if row is None:
            return None
        if gtip_of is not None:
            return gtip_of(row)
        value = row.get("gtip") or row.get("gtip_prefix")
        return str(value) if value else None

    for key in sorted(set(current) | set(previous)):
        new, old = current.get(key), previous.get(key)
        if old is None:
            result.append({"entity_key": key, "gtip": gtip(new), "change_type": "added", "before": None, "after": new})
        elif new is None:
            result.append({"entity_key": key, "gtip": gtip(old), "change_type": "removed", "before": old, "after": None})
        elif any(old.get(field) != new.get(field) for field in tracked):
            result.append({"entity_key": key, "gtip": gtip(new), "change_type": "modified", "before": old, "after": new})
    return result
