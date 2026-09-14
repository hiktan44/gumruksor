"""Editorial review gate (human-in-the-loop) for official data snapshots.

Every engine that synchronises an official source (tariff schedule, import
control communiqués, EU classification regulations) records the new snapshot,
diffs it against the previous one and then asks :func:`decide` whether the
snapshot may go live immediately or must wait for an editor/admin.

Modes (``DATA_REVIEW_MODE``):

* ``off``    – today's behaviour, every snapshot is activated at once (default).
* ``auto``   – small, warning-free diffs go live; large diffs, parse warnings
               or row-count drops wait in the review queue.
* ``strict`` – every new snapshot waits for a human decision.

The decision function is pure so the policy table is unit-testable; the
:class:`ReviewService` glues the engines, the unified change ledger and the
audit trail together for the HTTP layer.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterable

logger = logging.getLogger("mevzuat.review")

MODES = ("off", "auto", "strict")
STATUSES = ("approved", "pending_review", "rejected")
ACTIONS = ("approve", "reject")

# Columns added to every snapshot table (PRAGMA-guarded ALTER TABLE, additive only).
REVIEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("status", "TEXT NOT NULL DEFAULT 'approved'"),
    ("reviewed_by", "TEXT"),
    ("reviewed_at", "TEXT"),
    ("review_note", "TEXT"),
    ("parse_warnings_json", "TEXT"),
    ("diff_summary_json", "TEXT"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_review_columns(db: sqlite3.Connection, table: str) -> None:
    """Add the review columns to ``table`` when missing; existing rows count as approved."""
    if not table.replace("_", "").isalnum():
        raise ValueError("Geçersiz tablo adı")
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, definition in REVIEW_COLUMNS:
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@dataclass(frozen=True)
class ReviewPolicy:
    mode: str = "off"
    max_auto_rows: int = 200
    max_auto_ratio: float = 0.02
    block_on_warnings: bool = True

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Bilinmeyen inceleme modu: {self.mode}")
        if self.max_auto_rows < 0 or self.max_auto_ratio < 0:
            raise ValueError("İnceleme eşikleri negatif olamaz")

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "max_auto_rows": self.max_auto_rows,
            "max_auto_ratio": self.max_auto_ratio,
            "block_on_warnings": self.block_on_warnings,
        }


def policy_from_env(environ: dict[str, str] | None = None) -> ReviewPolicy:
    """Build the policy from ``DATA_REVIEW_*`` variables; bad values fall back to defaults."""
    env = os.environ if environ is None else environ
    mode = (env.get("DATA_REVIEW_MODE") or "off").strip().lower()
    if mode not in MODES:
        logger.warning("DATA_REVIEW_MODE=%r tanınmadı; 'off' kullanılıyor", mode)
        mode = "off"

    def _int(name: str, default: int) -> int:
        try:
            return max(0, int(env.get(name, "") or default))
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        try:
            return max(0.0, float(env.get(name, "") or default))
        except ValueError:
            return default

    warn_raw = (env.get("DATA_REVIEW_BLOCK_ON_WARNINGS") or "1").strip().lower()
    return ReviewPolicy(
        mode=mode,
        max_auto_rows=_int("DATA_REVIEW_MAX_AUTO_ROWS", 200),
        max_auto_ratio=_float("DATA_REVIEW_MAX_AUTO_RATIO", 0.02),
        block_on_warnings=warn_raw not in {"0", "false", "no", "off"},
    )


@dataclass(frozen=True)
class DiffSummary:
    total_rows: int
    previous_rows: int
    added: int = 0
    removed: int = 0
    modified: int = 0

    @property
    def changed(self) -> int:
        return self.added + self.removed + self.modified

    @property
    def ratio(self) -> float:
        base = max(self.previous_rows, 1)
        return self.changed / base

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "previous_rows": self.previous_rows,
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "changed": self.changed,
            "ratio": round(self.ratio, 4),
        }


@dataclass(frozen=True)
class ReviewDecision:
    status: str  # approved | pending_review
    reasons: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        return self.status == "pending_review"


def decide(
    policy: ReviewPolicy,
    summary: DiffSummary,
    *,
    parse_warnings: Iterable[str] = (),
    first_snapshot: bool = False,
) -> ReviewDecision:
    """Pure decision table: should this snapshot go live or wait for an editor?"""
    warnings = [str(item) for item in parse_warnings if str(item).strip()]
    if policy.mode == "off":
        return ReviewDecision("approved")
    if policy.mode == "strict":
        return ReviewDecision("pending_review", ("strict: her yeni snapshot editör onayı bekler",))
    reasons: list[str] = []
    if warnings and policy.block_on_warnings:
        reasons.append(f"{len(warnings)} ayrıştırma uyarısı")
    if first_snapshot:
        # The first version of a source has nothing to compare against; it goes
        # live so a fresh installation is usable, unless the parser complained.
        return ReviewDecision("pending_review", tuple(reasons)) if reasons else ReviewDecision("approved")
    if summary.changed > policy.max_auto_rows:
        reasons.append(f"{summary.changed} değişen satır > {policy.max_auto_rows} eşiği")
    if summary.previous_rows and summary.ratio > policy.max_auto_ratio:
        reasons.append(f"değişim oranı %{summary.ratio * 100:.2f} > %{policy.max_auto_ratio * 100:.2f} eşiği")
    if summary.previous_rows and summary.total_rows < 0.8 * summary.previous_rows:
        reasons.append(f"satır sayısı {summary.previous_rows} → {summary.total_rows}")
    if reasons:
        return ReviewDecision("pending_review", tuple(reasons))
    return ReviewDecision("approved")


def review_metadata(summary: DiffSummary, decision: ReviewDecision, parse_warnings: Iterable[str]) -> tuple[str, str]:
    """JSON blobs stored next to the snapshot row (parse warnings, diff summary + reasons)."""
    warnings_json = json.dumps([str(item)[:500] for item in parse_warnings][:50], ensure_ascii=False)
    diff = summary.as_dict()
    diff["reasons"] = list(decision.reasons)
    return warnings_json, json.dumps(diff, ensure_ascii=False, separators=(",", ":"))


def row_review_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Decode the review columns of a snapshot row (tolerates legacy rows)."""
    keys = row.keys() if hasattr(row, "keys") else ()

    def _get(name: str) -> Any:
        return row[name] if name in keys else None

    def _load(name: str, default: Any) -> Any:
        raw = _get(name)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    return {
        "status": _get("status") or "approved",
        "reviewed_by": _get("reviewed_by"),
        "reviewed_at": _get("reviewed_at"),
        "review_note": _get("review_note"),
        "parse_warnings": _load("parse_warnings_json", []),
        "diff_summary": _load("diff_summary_json", {}),
    }


@dataclass
class ReviewService:
    """Aggregates engine review queues and applies editor decisions.

    ``engines`` maps ledger kind → engine exposing ``pending_reviews()`` and
    ``review_snapshot(snapshot_id, action, *, reviewed_by, note)``.
    """

    policy: ReviewPolicy
    engines: dict[str, Any] = field(default_factory=dict)
    ledger: Any = None
    audit: Callable[..., None] | None = None
    on_pending: Callable[[list[dict[str, Any]]], Any] | None = None

    def pending(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for kind, engine in self.engines.items():
            try:
                for item in engine.pending_reviews():
                    item.setdefault("kind", kind)
                    items.append(item)
            except Exception as exc:  # noqa: BLE001 – one broken engine must not hide the others
                logger.warning("Review queue for %s unavailable: %s", kind, exc)
        items.sort(key=lambda item: str(item.get("retrieved_at") or ""), reverse=True)
        return items

    def pending_count(self) -> int:
        return len(self.pending())

    def overview(self) -> dict[str, Any]:
        pending = self.pending()
        return {"policy": self.policy.as_dict(), "pending": pending, "pending_count": len(pending)}

    def review(self, kind: str, snapshot_id: str, action: str, *, actor: dict[str, Any], note: str = "") -> dict[str, Any]:
        if kind not in self.engines:
            raise KeyError(kind)
        if action not in ACTIONS:
            raise ValueError("Karar 'approve' veya 'reject' olmalıdır.")
        engine = self.engines[kind]
        reviewer = str(actor.get("email") or actor.get("sub") or "editor")[:200]
        note = str(note or "").strip()[:1000]
        result = engine.review_snapshot(snapshot_id, action, reviewed_by=reviewer, note=note)
        status = "approved" if action == "approve" else "rejected"
        if self.ledger is not None and result.get("ledger_batch"):
            try:
                self.ledger.set_review_status(result["ledger_batch"], status)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ledger review status update failed: %s", exc)
        if self.audit is not None:
            try:
                self.audit(
                    actor,
                    "data_review",
                    kind,
                    snapshot_id,
                    {"action": action, "status": status, "note": note, "source_id": result.get("source_id")},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Review audit failed: %s", exc)
        result.update({"kind": kind, "status": status, "reviewed_by": reviewer, "note": note})
        return result

    async def notify_pending(self, items: list[dict[str, Any]]) -> None:
        """Best-effort notification hook (e-mail to editors); never raises."""
        if not items or self.on_pending is None:
            return
        try:
            outcome = self.on_pending(items)
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pending review notification failed: %s", exc)
