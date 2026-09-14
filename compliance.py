"""Deterministic compliance dashboard and early-warning engine (PRD Faz 2.6).

No language model is involved: the report is derived only from the user's own
stored evidence dossiers, their watch-list, the unified change ledger, the
trade-measure lists and the import-control communiqué catalogue. Every alert
names its source so the user can verify it against the official record.

Score components (weights sum to 100):

* ``gtip_precision``   – dossiers whose tariff code is 12 digits *and* confirmed.
* ``rate_certainty``   – dossiers without ambiguous/unresolved rates and no tax
                         rate still waiting for user confirmation (KDV).
* ``measure_validity`` – no applicable anti-dumping/safeguard measure that has
                         expired or expires within 90 days.
* ``change_exposure``  – no official change in the last 90 days on tracked codes.
* ``escalation``       – no used-goods dossier and no expert-escalation flag.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

CHANGE_WINDOW_DAYS = 90
RECENT_DAYS = 30
EXPIRY_HIGH_DAYS = 30
EXPIRY_MEDIUM_DAYS = 90
MAX_DOSSIERS = 100
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

COMPONENTS: tuple[tuple[str, str, int], ...] = (
    ("gtip_precision", "12 haneli onaylı GTİP", 25),
    ("rate_certainty", "Belirsiz oran kalmayan dosyalar", 20),
    ("measure_validity", "Süresi dolan/dolacak önlem yok", 20),
    ("change_exposure", "İzlenen kodlarda yakın değişiklik yok", 20),
    ("escalation", "Kullanılmış eşya / eskalasyon işareti yok", 15),
)

_CHANGE_TITLES: dict[tuple[str, str | None], str] = {
    ("trade_measures", "surveillance"): "gözetim tebliği değişti",
    ("trade_measures", "anti_dumping"): "damping/sübvansiyon listesi değişti",
    ("trade_measures", "safeguard"): "korunma önlemi listesi değişti",
    ("trade_measures", "tariff_quota"): "tarife kontenjanı listesi değişti",
    ("trade_measures", None): "ticaret politikası önlemi değişti",
    ("tariff", None): "tarife satırı değişti",
    ("controls", None): "kontrol tebliği kapsamı değişti",
    ("classification", None): "AB sınıflandırma tüzüğü değişti",
}
_MEASURE_LABELS = {
    "anti_dumping": "Damping önlemi",
    "countervailing": "Sübvansiyon (telafi edici) önlemi",
    "safeguard": "Korunma önlemi",
    "surveillance": "Gözetim uygulaması",
}
_CODE_YEAR_RE = re.compile(r"^(20\d{2})\s*/\s*\d+")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _short(gtip: str) -> str:
    return f"{gtip[:6]}…" if len(gtip) > 6 else gtip


def _iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    match = re.match(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    return {}


def _hit_dict(hit: Any) -> dict[str, Any]:
    if isinstance(hit, dict):
        return hit
    as_dict = getattr(hit, "as_dict", None)
    return as_dict() if callable(as_dict) else _as_dict(hit)


def _alert_key(alert: dict[str, Any]) -> str:
    raw = "|".join(str(alert.get(field) or "") for field in ("source", "title", "gtip", "dossier_id", "due_date"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------- dossier facts

def _dossier_facts(dossier: dict[str, Any]) -> dict[str, Any]:
    """Reduce a stored dossier (row + CustomsPrecheckResult payload) to scoring facts."""
    payload = dossier.get("payload") if isinstance(dossier.get("payload"), dict) else {}
    inquiry = payload.get("inquiry") if isinstance(payload.get("inquiry"), dict) else {}
    packet = payload.get("expert_review_packet") if isinstance(payload.get("expert_review_packet"), dict) else {}
    tariff = payload.get("tariff_lookup") if isinstance(payload.get("tariff_lookup"), dict) else {}
    control = payload.get("control_lookup") if isinstance(payload.get("control_lookup"), dict) else {}

    gtip = _digits(dossier.get("gtip")) or _digits(packet.get("selected_tariff_code")) or _digits(inquiry.get("candidate_gtip"))
    confirmed = bool(inquiry.get("exact_gtip_confirmed"))
    ambiguous = sorted(
        {str(item) for item in (tariff.get("ambiguous_measure_types") or [])}
        | {str(item) for item in (tariff.get("unresolved_measure_types") or [])}
        | {str(item) for item in (packet.get("unresolved_measure_types") or [])}
    )
    kdv_pending = False
    kdv_seen = False
    for finding in payload.get("taxes") or []:
        if not isinstance(finding, dict):
            continue
        if "kdv" in str(finding.get("name", "")).lower():
            kdv_seen = True
            if str(finding.get("status")) in {"possible", "unknown"}:
                kdv_pending = True
    if not kdv_seen and inquiry and inquiry.get("vat_rate") is None and payload.get("deterministic_cost"):
        kdv_pending = True

    control_codes: list[dict[str, Any]] = []
    for match in control.get("matches") or []:
        rule = match.get("rule") if isinstance(match, dict) and isinstance(match.get("rule"), dict) else {}
        code = str(rule.get("code") or "").strip()
        if code:
            control_codes.append({"code": code, "gazette_date": rule.get("official_gazette_date"), "title": rule.get("title")})

    return {
        "id": str(dossier.get("id") or ""),
        "title": str(dossier.get("title") or "Kanıt dosyası"),
        "gtip": gtip,
        "origin": str(dossier.get("origin_country") or inquiry.get("origin_country") or "").strip() or None,
        "checked_at": _iso_date(dossier.get("checked_at")),
        "confirmed_12": len(gtip) == 12 and confirmed,
        "is_12": len(gtip) == 12,
        "ambiguous": ambiguous,
        "kdv_pending": kdv_pending,
        "used": str(inquiry.get("condition") or "").lower() == "used",
        "escalation": bool(packet.get("escalation_required")),
        "escalation_reasons": [str(item) for item in (packet.get("reasons") or [])][:3],
        "control_codes": control_codes,
    }


def _load_dossiers(account_service: Any, user: dict[str, Any]) -> list[dict[str, Any]]:
    items = account_service.list_dossiers(user, limit=MAX_DOSSIERS) or []
    facts: list[dict[str, Any]] = []
    for item in items[:MAX_DOSSIERS]:
        try:
            full = account_service.get_dossier(user, str(item.get("id")))
        except Exception:  # noqa: BLE001 – a broken row must not hide the rest
            full = dict(item)
        facts.append(_dossier_facts(full))
    return facts


# ------------------------------------------------------------------ analysers

def _ratio_score(good: int, total: int) -> int:
    if total <= 0:
        return 100
    return int(round(100 * good / total))


def _change_alerts(ledger: Any, tracked: dict[str, dict[str, Any]], today: date) -> tuple[list[dict[str, Any]], set[str]]:
    """One alert per (code, kind, source) with a change in the last 90 days."""
    alerts: list[dict[str, Any]] = []
    affected: set[str] = set()
    if ledger is None:
        return alerts, affected
    since = (today - timedelta(days=CHANGE_WINDOW_DAYS)).isoformat()
    for gtip, info in tracked.items():
        try:
            rows = ledger.changes(gtip_prefix=gtip, since=since, limit=500)
        except Exception:  # noqa: BLE001
            rows = []
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            kind = str(row.get("kind") or "")
            source_id = str(row.get("source_id") or "")
            detected = _iso_date(row.get("detected_at"))
            group = groups.setdefault((kind, source_id), {"count": 0, "latest": None, "types": set()})
            group["count"] += 1
            group["types"].add(str(row.get("change_type") or ""))
            if detected and (group["latest"] is None or detected > group["latest"]):
                group["latest"] = detected
        if not groups:
            continue
        affected.add(gtip)
        for (kind, source_id), group in sorted(groups.items()):
            latest: date | None = group["latest"]
            age = (today - latest).days if latest else CHANGE_WINDOW_DAYS
            what = _CHANGE_TITLES.get((kind, source_id)) or _CHANGE_TITLES.get((kind, None)) or "resmî veri değişti"
            in_dossier = bool(info["dossiers"])
            if in_dossier:
                severity = "high" if age <= RECENT_DAYS else "medium"
            else:
                severity = "medium" if age <= RECENT_DAYS else "low"
            for dossier in info["dossiers"] or [None]:
                title = (
                    f"{dossier['title']} dosyasındaki {_short(gtip)} için {what}"
                    if dossier else f"İzlenen {_short(gtip)} için {what}"
                )
                alerts.append(
                    {
                        "severity": severity,
                        "title": title,
                        "detail": (
                            f"{group['count']} satır ({', '.join(sorted(t for t in group['types'] if t))}); "
                            f"son tespit {latest.isoformat() if latest else 'bilinmiyor'}. Değişiklikler sekmesinde satır farkını doğrulayın."
                        ),
                        "gtip": gtip,
                        "dossier_id": dossier["id"] if dossier else None,
                        "due_date": None,
                        "source": f"ledger:{kind}:{source_id}",
                    }
                )
    return alerts, affected


def _measure_alerts(trade_engine: Any, tracked: dict[str, dict[str, Any]], today: date) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """Expired / expiring anti-dumping and safeguard measures for tracked codes."""
    alerts: list[dict[str, Any]] = []
    affected: set[str] = set()
    warnings: list[str] = []
    if trade_engine is None:
        return alerts, affected, warnings
    seen_pairs: set[tuple[str, str | None]] = set()
    for gtip, info in tracked.items():
        origins = {d["origin"] for d in info["dossiers"]} | set(info["watch_origins"]) or {None}
        for origin in sorted(origins, key=lambda value: value or ""):
            if (gtip, origin) in seen_pairs:
                continue
            seen_pairs.add((gtip, origin))
            try:
                report = trade_engine.lookup(gtip, origin, today=today)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{_short(gtip)} için önlem listeleri sorgulanamadı: {exc}")
                continue
            report_dict = _as_dict(report) if not isinstance(report, dict) else report
            hits = []
            for section in ("anti_dumping", "safeguard"):
                hits.extend(_hit_dict(hit) for hit in (getattr(report, section, None) or report_dict.get(section) or []))
            for hit in hits:
                if hit.get("origin_match") is False:
                    continue
                expires = _iso_date(hit.get("expires"))
                if expires is None:
                    continue
                days = (expires - today).days
                label = _MEASURE_LABELS.get(str(hit.get("measure_type")), "Ticaret politikası önlemi")
                if days < 0:
                    severity, verdict = "low", f"süresi {expires.isoformat()} tarihinde doldu; uzatma/gözden geçirme kararını kontrol edin"
                elif days <= EXPIRY_HIGH_DAYS:
                    severity, verdict = "high", f"{days} gün içinde bitiyor ({expires.isoformat()})"
                elif days <= EXPIRY_MEDIUM_DAYS:
                    severity, verdict = "medium", f"{days} gün içinde bitiyor ({expires.isoformat()})"
                else:
                    continue
                affected.add(gtip)
                dossiers = [d for d in info["dossiers"] if d["origin"] == origin] or info["dossiers"] or [None]
                for dossier in dossiers:
                    alerts.append(
                        {
                            "severity": severity,
                            "title": f"{label} {verdict}",
                            "detail": (
                                f"{_short(gtip)} · {hit.get('country') or 'ülke belirtilmemiş'} · {hit.get('legal_act') or ''} "
                                f"({hit.get('gazette') or 'RG bilgisi yok'}). Oran: {hit.get('rate_text') or '—'}."
                            ).strip(),
                            "gtip": gtip,
                            "dossier_id": dossier["id"] if dossier else None,
                            "due_date": expires.isoformat(),
                            "source": f"trade_measures:{hit.get('measure_type')}",
                        }
                    )
    return alerts, affected, warnings


def _control_catalog_codes(control_engine: Any) -> set[str]:
    if control_engine is None:
        return set()
    catalog = getattr(control_engine, "get_communiques_catalog", None)
    if not callable(catalog):
        return set()
    try:
        return {str(item.get("code") or "").strip() for item in catalog() if isinstance(item, dict)}
    except Exception:  # noqa: BLE001
        return set()


def _control_alerts(control_engine: Any, dossiers: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Communiqué year rollover: the dossier cites a previous-year control communiqué."""
    alerts: list[dict[str, Any]] = []
    catalog = _control_catalog_codes(control_engine)
    for dossier in dossiers:
        for item in dossier["control_codes"]:
            match = _CODE_YEAR_RE.match(item["code"])
            year = int(match.group(1)) if match else None
            if year is None:
                gazette = _iso_date(item.get("gazette_date"))
                year = gazette.year if gazette else None
            if year is None or year >= today.year:
                continue
            successor = item["code"].replace(str(year), str(today.year), 1)
            successor_known = successor in catalog
            alerts.append(
                {
                    "severity": "medium" if successor_known else "low",
                    "title": f"{dossier['title']} dosyasındaki kontrol tebliği ({item['code']}) yıl geçişi",
                    "detail": (
                        f"Dosya {year} yılı tebliğine dayanıyor; {today.year} yılı tebliği "
                        + (f"({successor}) resmî katalogda mevcut, dosyayı yenileyin." if successor_known else "henüz katalogda görünmüyor; yayımlanınca dosyayı yenileyin.")
                    ),
                    "gtip": dossier["gtip"] or None,
                    "dossier_id": dossier["id"],
                    "due_date": f"{today.year}-01-01",
                    "source": "controls:year_rollover",
                }
            )
    return alerts


# ------------------------------------------------------------------ main entry

def compliance_report(
    account_service: Any,
    google_sub: str,
    *,
    ledger: Any = None,
    trade_engine: Any = None,
    control_engine: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Deterministic compliance score, component breakdown and early-warning alerts for one user."""
    today = today or date.today()
    user = {"sub": str(google_sub)}
    dossiers = _load_dossiers(account_service, user)
    watches = list(account_service.list_watchlist(user) or [])

    tracked: dict[str, dict[str, Any]] = {}
    for dossier in dossiers:
        if dossier["gtip"]:
            tracked.setdefault(dossier["gtip"], {"dossiers": [], "watch_origins": []})["dossiers"].append(dossier)
    for watch in watches:
        gtip = _digits(watch.get("gtip"))
        if gtip:
            entry = tracked.setdefault(gtip, {"dossiers": [], "watch_origins": []})
            if watch.get("origin_country"):
                entry["watch_origins"].append(str(watch["origin_country"]))

    alerts: list[dict[str, Any]] = []
    warnings: list[str] = []

    # --- component 1: 12-digit confirmed codes
    confirmed = sum(1 for d in dossiers if d["confirmed_12"])
    half = sum(1 for d in dossiers if d["is_12"] and not d["confirmed_12"])
    gtip_score = int(round((confirmed + 0.5 * half) / len(dossiers) * 100)) if dossiers else 100
    for dossier in dossiers:
        if not dossier["gtip"]:
            alerts.append({"severity": "medium", "title": f"{dossier['title']} dosyasında GTİP yok", "detail": "Dosyaya tarife kodu kaydedilmemiş; beyan için 12 haneli kod gerekir.", "gtip": None, "dossier_id": dossier["id"], "due_date": None, "source": "dossier:gtip"})
        elif not dossier["is_12"]:
            alerts.append({"severity": "medium", "title": f"GTİP {len(dossier['gtip'])} haneli; 12 hane onaylanmadı", "detail": f"{dossier['title']} dosyasındaki {dossier['gtip']} kodu beyan için 12 haneye tamamlanmalı ve onaylanmalıdır.", "gtip": dossier["gtip"], "dossier_id": dossier["id"], "due_date": None, "source": "dossier:gtip"})
        elif not dossier["confirmed_12"]:
            alerts.append({"severity": "low", "title": f"{dossier['title']} dosyasındaki 12 haneli kod kullanıcı onayı bekliyor", "detail": f"{dossier['gtip']} kodu için 'kesin GTİP onaylandı' işareti yok.", "gtip": dossier["gtip"], "dossier_id": dossier["id"], "due_date": None, "source": "dossier:gtip"})

    # --- component 2: rate certainty
    certain = 0
    for dossier in dossiers:
        clean = not dossier["ambiguous"] and not dossier["kdv_pending"]
        certain += int(clean)
        if dossier["ambiguous"]:
            alerts.append({"severity": "medium", "title": f"{dossier['title']} dosyasında belirsiz oran: {', '.join(dossier['ambiguous'])}", "detail": "Alt tarife satırlarında oran değişiyor veya resmî listede çözülemedi; 12 haneli satıra göre doğrulayın.", "gtip": dossier["gtip"] or None, "dossier_id": dossier["id"], "due_date": None, "source": "dossier:rates"})
        if dossier["kdv_pending"]:
            alerts.append({"severity": "low", "title": "KDV oranı kullanıcı onayı bekliyor", "detail": f"{dossier['title']} dosyasında KDV oranı resmî listeden kesinleşmedi; oranı doğrulayıp dosyayı güncelleyin.", "gtip": dossier["gtip"] or None, "dossier_id": dossier["id"], "due_date": None, "source": "dossier:kdv"})
    rate_score = _ratio_score(certain, len(dossiers))

    # --- component 3: measure validity
    measure_alerts, measure_affected, measure_warnings = _measure_alerts(trade_engine, tracked, today)
    alerts.extend(measure_alerts)
    warnings.extend(measure_warnings)
    measure_score = _ratio_score(len(tracked) - len(measure_affected), len(tracked))

    # --- component 4: change exposure
    change_alerts, change_affected = _change_alerts(ledger, tracked, today)
    alerts.extend(change_alerts)
    change_score = _ratio_score(len(tracked) - len(change_affected), len(tracked))

    # --- component 5: used goods / escalation
    clean_count = 0
    for dossier in dossiers:
        flagged = dossier["used"] or dossier["escalation"]
        clean_count += int(not flagged)
        if dossier["used"]:
            alerts.append({"severity": "medium", "title": f"{dossier['title']} dosyası kullanılmış eşya içeriyor", "detail": "Kullanılmış eşya ithalatı izne tabi olabilir (İthalat Rejimi Kararı, Eski/Kullanılmış Eşya tebliği); izin ve CE/TAREKS koşullarını doğrulayın.", "gtip": dossier["gtip"] or None, "dossier_id": dossier["id"], "due_date": None, "source": "dossier:used_goods"})
        if dossier["escalation"]:
            alerts.append({"severity": "medium", "title": f"{dossier['title']} dosyası uzman incelemesi istiyor", "detail": "; ".join(dossier["escalation_reasons"]) or "Ön değerlendirme uzman eskalasyonu işaretledi.", "gtip": dossier["gtip"] or None, "dossier_id": dossier["id"], "due_date": None, "source": "dossier:escalation"})
    escalation_score = _ratio_score(clean_count, len(dossiers))

    # --- control communiqué year rollover (feeds the rate/measure narrative, not a weight of its own)
    alerts.extend(_control_alerts(control_engine, dossiers, today))

    scores = {
        "gtip_precision": gtip_score,
        "rate_certainty": rate_score,
        "measure_validity": measure_score,
        "change_exposure": change_score,
        "escalation": escalation_score,
    }
    details = {
        "gtip_precision": f"{confirmed}/{len(dossiers)} dosyada 12 haneli onaylı kod" if dossiers else "Kayıtlı kanıt dosyası yok",
        "rate_certainty": f"{certain}/{len(dossiers)} dosyada belirsiz oran veya bekleyen KDV onayı yok" if dossiers else "Kayıtlı kanıt dosyası yok",
        "measure_validity": f"{len(tracked) - len(measure_affected)}/{len(tracked)} izlenen kodda süresi dolan/dolacak önlem yok" if tracked else "İzlenen kod yok",
        "change_exposure": f"{len(tracked) - len(change_affected)}/{len(tracked)} izlenen kodda son {CHANGE_WINDOW_DAYS} günde değişiklik yok" if tracked else "İzlenen kod yok",
        "escalation": f"{clean_count}/{len(dossiers)} dosyada kullanılmış eşya veya eskalasyon işareti yok" if dossiers else "Kayıtlı kanıt dosyası yok",
    }
    total = sum(weight for _, _, weight in COMPONENTS)
    score = int(round(sum(scores[key] * weight for key, _, weight in COMPONENTS) / total))
    score = max(0, min(100, score))

    for alert in alerts:
        alert["key"] = _alert_key(alert)
    alerts.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), item.get("due_date") or "9999", item["title"]))

    return {
        "score": score,
        "status": "good" if score >= 85 else "watch" if score >= 60 else "risk",
        "components": [
            {"key": key, "label": label, "weight": weight, "score": scores[key], "detail": details[key]}
            for key, label, weight in COMPONENTS
        ],
        "alerts": alerts,
        "alert_counts": {level: sum(1 for item in alerts if item["severity"] == level) for level in ("high", "medium", "low")},
        "dossier_count": len(dossiers),
        "watch_count": len(watches),
        "tracked_gtip_count": len(tracked),
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": today.isoformat(),
    }


def high_alert_digest(report: dict[str, Any]) -> str:
    """Stable fingerprint of the high-severity alerts (for once-per-change e-mail dedupe)."""
    keys = sorted(item["key"] for item in report.get("alerts", []) if item.get("severity") == "high")
    return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:24]
