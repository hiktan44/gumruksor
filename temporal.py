"""Temporal validity helpers: GTİP × ülke × tarih (PRD Faz 1.4).

Every official snapshot carries ``valid_from`` (start of legal validity) and,
once a newer version goes live, ``valid_to``.  The helpers here are pure so the
engines share one rule set:

* :func:`derive_validity` reads "… tarihinden itibaren" / Resmî Gazete
  patterns from landing pages or workbook titles (tariff schedule).
* :func:`extract_effective_date` reads the "yürürlüğe girer" clause of a
  communiqué (import controls).
* :func:`close_previous` decides the ``valid_to`` of the version that is being
  superseded: the legal boundary when the new version starts later, otherwise
  the observed boundary (the day the new version was downloaded).
* :func:`validity_basis` labels a lookup: ``current`` (today, active snapshot),
  ``legal`` (as-of date inside a legally bounded interval), ``observed``
  (interval bounded only by download dates) or ``unavailable``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Iterable

BASES = ("config", "document", "gazette", "observed", "admin")

_DATE_RE = r"(\d{1,2})[./](\d{1,2})[./](\d{4})"
_FROM_RE = re.compile(_DATE_RE + r"\s*tarih(?:inden|i)\s+itibar(?:en|[ıi]yle?)", re.IGNORECASE)
_GAZETTE_RE = re.compile(
    _DATE_RE + r"\s*tarih(?:li)?\s*(?:ve|,)?\s*(\d{4,6})\s*(?:\(?\s*(?:mükerrer|m[üu]kerrer)\s*\)?\s*)?say[ıi]l[ıi]\s*Resm[iî]\s*Gazete",
    re.IGNORECASE,
)
_ACT_RE = re.compile(
    r"(\d{1,2})[./](\d{1,2})[./](\d{4})\s*tarih(?:li)?\s*(?:ve)?\s*(\d{1,6})\s*say[ıi]l[ıi]\s*(Cumhurbaşkanı\s+Kararı|Bakanlar\s+Kurulu\s+Kararı|Karar)",
    re.IGNORECASE,
)
_EFFECTIVE_RE = re.compile(
    r"(?:(\d{1,2})[./](\d{1,2})[./](\d{4})\s*tarihinde|(yayımı|yayım[ıi]n[ıi]\s+izleyen\s+g[üu]n(?:den)?)\s*(?:tarihinde)?\s*(?:itibaren)?)\s*y[üu]r[üu]rl[üu][ğg]e\s+girer",
    re.IGNORECASE,
)


def _iso(day: str, month: str, year: str) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def parse_iso_date(value: Any) -> date | None:
    """Accept YYYY-MM-DD (or a longer ISO timestamp) and return a date; else None."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


@dataclass(frozen=True)
class Validity:
    valid_from: str
    basis: str = "config"
    gazette_date: str | None = None
    gazette_number: str | None = None
    legal_act: str | None = None
    warnings: tuple[str, ...] = ()


def derive_validity(texts: Iterable[str], *, floor: str, context: str = "") -> Validity:
    """Pick the validity start from official page/workbook text; never earlier than ``floor``.

    ``floor`` is the configured start (e.g. 2026-01-01).  A later "… tarihinden
    itibaren" date wins with basis ``document``; a Resmî Gazete reference yields
    ``gazette_date``/``gazette_number`` and, when no explicit start is present,
    the gazette date itself (basis ``gazette``).  Unreadable text keeps the
    configured date and adds a parse warning so the review gate can see it.
    """
    floor_date = parse_iso_date(floor) or date.today()
    candidates: list[tuple[str, str]] = []  # (iso, basis)
    gazette_date = gazette_number = legal_act = None
    warnings: list[str] = []
    joined = "\n".join(t for t in texts if t)
    for match in _FROM_RE.finditer(joined):
        iso = _iso(match.group(1), match.group(2), match.group(3))
        if iso:
            candidates.append((iso, "document"))
    for match in _GAZETTE_RE.finditer(joined):
        iso = _iso(match.group(1), match.group(2), match.group(3))
        if iso and gazette_date is None:
            gazette_date, gazette_number = iso, match.group(4)
    for match in _ACT_RE.finditer(joined):
        iso = _iso(match.group(1), match.group(2), match.group(3))
        if iso and legal_act is None:
            legal_act = f"{match.group(1)}.{match.group(2)}.{match.group(3)} tarihli ve {match.group(4)} sayılı {match.group(5)}"
    chosen: tuple[str, str] | None = None
    # Only dates on/after the configured floor and not in the far future count.
    horizon = (floor_date.replace(year=floor_date.year + 2)).isoformat()
    valid = [(iso, basis) for iso, basis in candidates if floor_date.isoformat() <= iso <= horizon]
    if valid:
        chosen = max(valid)  # the latest explicit start is the one currently in force
    elif gazette_date and floor_date.isoformat() <= gazette_date <= horizon:
        chosen = (gazette_date, "gazette")
    if chosen is None:
        if candidates:
            warnings.append(
                f"{context or 'kaynak'}: metindeki yürürlük tarihi ({', '.join(sorted({c[0] for c in candidates}))}) "
                f"yapılandırılmış aralığın dışında; {floor_date.isoformat()} kullanıldı"
            )
        return Validity(floor_date.isoformat(), "config", gazette_date, gazette_number, legal_act, tuple(warnings))
    return Validity(chosen[0], chosen[1], gazette_date, gazette_number, legal_act, tuple(warnings))


def extract_effective_date(text: str, gazette_date: str | None, *, floor: str | None = None) -> tuple[str | None, str | None, str]:
    """Return (effective_date, clause, basis) from a communiqué's entry-into-force article.

    "… tarihinde yürürlüğe girer" → that date (basis ``document``);
    "yayımı tarihinde yürürlüğe girer" → the gazette date (basis ``gazette``);
    nothing readable → (None, None, "config").
    """
    if not text:
        return None, None, "config"
    gazette_iso = parse_iso_date(gazette_date)
    if gazette_iso is None and gazette_date:
        parts = re.split(r"[./]", str(gazette_date).strip())
        if len(parts) == 3 and len(parts[2]) == 4:
            gazette_iso = parse_iso_date(_iso(parts[0], parts[1], parts[2]))
    floor_date = parse_iso_date(floor)
    for match in _EFFECTIVE_RE.finditer(text):
        clause = match.group(0).strip()
        if match.group(3):
            iso = _iso(match.group(1), match.group(2), match.group(3))
            if iso and (floor_date is None or iso >= (floor_date.replace(year=floor_date.year - 1)).isoformat()):
                return iso, clause, "document"
        elif gazette_iso is not None:
            effective = gazette_iso
            if "izleyen" in match.group(4).lower():
                effective = date.fromordinal(gazette_iso.toordinal() + 1)
            return effective.isoformat(), clause, "gazette"
    return None, None, "config"


def close_previous(new_valid_from: str, previous_valid_from: str, new_retrieved_at: str) -> tuple[str, str]:
    """``valid_to`` of the superseded version and how that boundary was established."""
    if new_valid_from and previous_valid_from and new_valid_from > previous_valid_from:
        return new_valid_from[:10], "legal"
    return str(new_retrieved_at)[:10], "observed"


def covers(row: sqlite3.Row | dict[str, Any], as_of: str) -> bool:
    keys = row.keys() if hasattr(row, "keys") else ()
    valid_from = str(row["valid_from"] or "")[:10] if "valid_from" in keys else ""
    valid_to = str(row["valid_to"] or "")[:10] if "valid_to" in keys and row["valid_to"] else None
    return bool(valid_from) and valid_from <= as_of and (valid_to is None or valid_to > as_of)


def validity_basis(row: sqlite3.Row | dict[str, Any] | None, as_of: str | None) -> str:
    """Label the interval that answered a query (see module docstring)."""
    if row is None:
        return "unavailable"
    if not as_of or as_of >= today_iso():
        return "current"
    keys = row.keys() if hasattr(row, "keys") else ()
    valid_to = row["valid_to"] if "valid_to" in keys else None
    valid_to_basis = row["valid_to_basis"] if "valid_to_basis" in keys else None
    from_basis = row["valid_from_basis"] if "valid_from_basis" in keys else "config"
    if valid_to is None:
        return "current" if (from_basis in {"document", "gazette", "config", "admin"}) else "observed"
    return "legal" if valid_to_basis == "legal" else "observed"


VALIDITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("valid_to", "TEXT"),
    ("valid_from_basis", "TEXT NOT NULL DEFAULT 'config'"),
    ("valid_to_basis", "TEXT"),
    ("gazette_date", "TEXT"),
    ("gazette_number", "TEXT"),
    ("legal_act", "TEXT"),
)


def ensure_validity_columns(db: sqlite3.Connection, table: str, columns: Iterable[tuple[str, str]] = VALIDITY_COLUMNS) -> None:
    if not table.replace("_", "").isalnum():
        raise ValueError("Geçersiz tablo adı")
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, definition in columns:
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def snapshot_validity(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    keys = row.keys() if hasattr(row, "keys") else ()

    def _get(name: str) -> Any:
        return row[name] if name in keys else None

    return {
        "snapshot_id": _get("id"),
        "valid_from": _get("valid_from"),
        "valid_to": _get("valid_to"),
        "valid_from_basis": _get("valid_from_basis") or "config",
        "valid_to_basis": _get("valid_to_basis"),
        "gazette_date": _get("gazette_date") or _get("official_gazette_date"),
        "gazette_number": _get("gazette_number") or _get("official_gazette_number"),
        "legal_act": _get("legal_act"),
    }


def normalise_as_of(value: Any) -> str | None:
    """Validate a user-supplied as-of date; None when absent, ValueError when malformed."""
    if value is None or str(value).strip() == "":
        return None
    parsed = parse_iso_date(value)
    if parsed is None or len(str(value).strip()) != 10:
        raise ValueError("Yürürlük tarihi YYYY-AA-GG biçiminde olmalıdır.")
    if parsed.year < 2000 or parsed > date.today().replace(year=date.today().year + 1):
        raise ValueError("Yürürlük tarihi desteklenen aralığın dışında.")
    return parsed.isoformat()
