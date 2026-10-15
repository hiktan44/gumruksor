"""Read-only account counts; no token, role, or telemetry collector is created."""
from datetime import datetime, timezone


def report_period(start: str, end: str) -> dict:
    def parse(value):
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("INVALID_REPORT_PERIOD")
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("INVALID_REPORT_PERIOD")
        return date.astimezone(timezone.utc)
    a, b = parse(start), parse(end)
    if a >= b or (b - a).total_seconds() > 31 * 86400:
        raise ValueError("INVALID_REPORT_PERIOD")
    iso = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"start": iso(a), "end": iso(b), "timezone": "Europe/Istanbul", "bounds": "[start,end)"}


def account_summary(connection, start, end):
    p = report_period(start, end)
    a = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).timestamp()
    b = datetime.fromisoformat(p["end"].replace("Z", "+00:00")).timestamp()
    total, fresh = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN created_at >= ? AND created_at < ? THEN 1 ELSE 0 END),0) FROM users", (a, b)
    ).fetchone()
    return {"schemaVersion": 1, "kind": "subscriber-summary", "appId": "gumruksor", "period": p,
            "registeredAccounts": {"unit": "account", "total": total, "new": fresh, "status": "measured"},
            "paidSubscribers": {"unit": "subscription", "total": None, "active": None, "new": None, "status": "unavailable", "reason": "Plan veya active durumu tek başına ücretli abonelik kanıtı değildir."},
            "usage": {"status": "unavailable", "authenticatedAccounts": None, "anonymousVisitors": None, "sessions": None,
                      "totalMinutes": None, "averageMinutes": None, "reason": "İşlem/kota defteri gerçek oturum veya sayfada kalma süresi değildir."}}
