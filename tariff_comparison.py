"""Read two archived tariff lookups; never infer a missing historical rate."""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any

from temporal import normalise_as_of, today_iso


def _snapshot(result: Any) -> dict[str, Any]:
    data = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    rates = {key: value for key, value in (data.get("unambiguous_rates") or {}).items()
             if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
    sources = [{key: row.get(key) for key in ("source_id", "archive_url", "archive_sha256", "valid_from", "valid_to")}
               for row in (data.get("snapshots") or [])[:6]]
    return {"status": data.get("status", "unavailable"), "gtip": data.get("gtip"),
            "as_of_date": data.get("as_of_date"), "validity_basis": data.get("validity_basis", "unavailable"),
            "rates": rates, "unresolved_measure_types": data.get("unresolved_measure_types") or [],
            "warnings": (data.get("warnings") or [])[:8], "sources": sources}


async def compare_tariff_dates(engine: Any, *, gtip: str, origin_country: str, from_date: str, to_date: str,
                               dispatch_country: str | None = None, atr_certificate: bool | None = None) -> dict[str, Any]:
    start, end = normalise_as_of(from_date), normalise_as_of(to_date)
    if not start or not end or date.fromisoformat(start) >= date.fromisoformat(end):
        raise ValueError("İlk tarih ikinci tarihten önce olmalı.")
    if end > today_iso():
        raise ValueError("Gelecek tarih için tarife karşılaştırılamaz.")
    common = {"origin_country": origin_country, "dispatch_country": dispatch_country,
              "atr_certificate": atr_certificate, "auto_sync": False}
    before = _snapshot(await engine.lookup(gtip, as_of=start, **common))
    after = _snapshot(await engine.lookup(gtip, as_of=end, **common))
    # Only exact, date-stamped records can contribute numeric comparisons.
    for snapshot, expected in ((before, start), (after, end)):
        if snapshot["gtip"] != gtip or snapshot["as_of_date"] != expected or snapshot["status"] not in ("matched", "partial"):
            snapshot["rates"] = {}
    changes = []
    for name in sorted(set(before["rates"]) | set(after["rates"]) |
                       set(before["unresolved_measure_types"]) | set(after["unresolved_measure_types"])):
        old, new = before["rates"].get(name), after["rates"].get(name)
        verified = old is not None and new is not None
        changes.append({"measure": name, "before_rate": old, "after_rate": new,
                        "difference_percentage_points": float(Decimal(str(new)) - Decimal(str(old))) if verified else None,
                        "status": "unchanged" if verified and old == new else "changed" if verified else "unavailable"})
    warnings = list(dict.fromkeys(before["warnings"] + after["warnings"]))
    if "observed" in (before["validity_basis"], after["validity_basis"]):
        warnings.append("En az bir tarih sınırı indirme gözlemine dayanıyor; fark yasal yürürlük değişikliği olarak kesinleştirilemez.")
    complete = bool(changes) and all(row["status"] != "unavailable" for row in changes) and all(
        snapshot["status"] == "matched" and snapshot["validity_basis"] in ("legal", "current")
        for snapshot in (before, after))
    if not complete:
        warnings.append("Karşılaştırma kapsamı eksik veya tarih dayanağı belirsiz. Eksik eski oran sıfır kabul edilmez.")
    return {"gtip": gtip, "origin_country": origin_country, "from_date": start, "to_date": end,
            "status": "compared" if complete else "partial", "before": before, "after": after,
            "changes": changes, "warnings": warnings}
