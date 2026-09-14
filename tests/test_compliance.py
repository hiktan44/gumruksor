"""Deterministic compliance dashboard and early warnings (PRD Faz 2.6)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService
from compliance import COMPONENTS, compliance_report, high_alert_digest
from email_service import render_compliance_email

TODAY = date(2026, 9, 14)
USER = {"sub": "sub-1", "email": "a@example.com", "name": "A"}
OTHER = {"sub": "sub-2", "email": "b@example.com", "name": "B"}


def _service(temp: str, *users: dict) -> AccountService:
    service = AccountService(temp)
    with sqlite3.connect(service.db_path) as db:
        for user in users:
            db.execute(
                "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                (user["sub"], user["email"], user["name"], ""),
            )
    return service


def _payload(
    *, gtip: str | None = "851712000011", confirmed: bool = True, ambiguous: list[str] | None = None,
    kdv_status: str = "applicable", condition: str = "new", escalation: bool = False,
    control_code: str | None = None, origin: str = "Çin",
) -> dict:
    """A trimmed CustomsPrecheckResult-shaped payload; only the fields the engine reads."""
    payload = {
        "status": "preliminary",
        "inquiry": {"candidate_gtip": gtip, "exact_gtip_confirmed": confirmed, "condition": condition, "origin_country": origin, "vat_rate": 20},
        "tariff_lookup": {"status": "matched", "ambiguous_measure_types": ambiguous or [], "unresolved_measure_types": []},
        "taxes": [{"name": "KDV", "status": kdv_status, "rate": "20", "explanation": "x"}],
        "expert_review_packet": {"escalation_required": escalation, "reasons": ["Oran belirsiz"] if escalation else [], "selected_tariff_code": gtip},
        "deterministic_cost": {"total": 1},
    }
    if control_code:
        payload["control_lookup"] = {"status": "matched", "matches": [{"rule": {"code": control_code, "title": "ÜGD", "official_gazette_date": f"{control_code[:4]}-12-31"}}]}
    return payload


def _dossier(service: AccountService, user: dict, *, title: str = "Telefon", gtip: str | None = "851712000011", origin: str = "Çin", **kwargs) -> dict:
    return service.create_dossier(
        user, title=title, product_name="ürün", gtip=gtip, origin_country=origin, effective_date=None,
        checked_at="2026-09-01T00:00:00+00:00", payload=_payload(gtip=gtip, origin=origin, **kwargs), evidence={},
    )


class FakeLedger:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict] = []

    def changes(self, *, gtip_prefix=None, since=None, limit=200, **_ignored):
        self.calls.append({"gtip_prefix": gtip_prefix, "since": since, "limit": limit})
        digits = "".join(ch for ch in str(gtip_prefix or "") if ch.isdigit())
        return [
            row for row in self.rows
            if (not digits or str(row["gtip"]).startswith(digits) or digits.startswith(str(row["gtip"])))
            and (not since or row["detected_at"] >= since)
        ][:limit]


def _hit(measure_type: str = "anti_dumping", expires: str | None = None, origin_match=True, status: str = "in_force") -> dict:
    return {
        "measure_type": measure_type, "matched_code": "8517", "country": "ÇHC", "origin_match": origin_match,
        "rate_text": "%25", "unit_value_usd": None, "unit": None, "product": "telefon", "legal_act": "Tebliğ 2023/1",
        "gazette": "RG 01.01.2023 / 1", "expires": expires, "status": status, "notes": "", "source": "test", "provenance": None,
    }


class FakeTradeEngine:
    def __init__(self, hits: dict[str, list[dict]] | None = None, *, fail: bool = False) -> None:
        self.hits = hits or {}
        self.fail = fail
        self.calls: list[tuple] = []

    def lookup(self, gtip, origin_country=None, *, today=None):
        self.calls.append((gtip, origin_country, today))
        if self.fail:
            raise RuntimeError("liste yok")
        return {"gtip": gtip, "anti_dumping": self.hits.get("anti_dumping", []), "safeguard": self.hits.get("safeguard", []), "surveillance": [], "tariff_quota": []}


class FakeControlEngine:
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes

    def get_communiques_catalog(self):
        return [{"code": code, "title": "ÜGD"} for code in self.codes]


def _report(service: AccountService, sub: str = "sub-1", **kwargs) -> dict:
    kwargs.setdefault("ledger", FakeLedger())
    kwargs.setdefault("trade_engine", FakeTradeEngine())
    kwargs.setdefault("control_engine", FakeControlEngine(["2026/9"]))
    return compliance_report(service, sub, today=TODAY, **kwargs)


def _titles(report: dict, severity: str | None = None) -> list[str]:
    return [item["title"] for item in report["alerts"] if severity is None or item["severity"] == severity]


class ComplianceScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = _service(self.temp.name, USER, OTHER)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_empty_account_scores_full_without_alerts(self) -> None:
        report = _report(self.service)
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["status"], "good")
        self.assertEqual(report["alerts"], [])
        self.assertEqual((report["dossier_count"], report["watch_count"], report["tracked_gtip_count"]), (0, 0, 0))
        self.assertEqual([item["key"] for item in report["components"]], [key for key, _, _ in COMPONENTS])
        self.assertEqual(sum(item["weight"] for item in report["components"]), 100)
        self.assertTrue(report["generated_at"])

    def test_short_gtip_lowers_precision_and_warns(self) -> None:
        _dossier(self.service, USER, gtip="85171200", confirmed=False)
        report = _report(self.service)
        precision = next(item for item in report["components"] if item["key"] == "gtip_precision")
        self.assertEqual(precision["score"], 0)
        self.assertIn("GTİP 8 haneli; 12 hane onaylanmadı", _titles(report, "medium"))
        self.assertEqual(report["alerts"][0]["gtip"], "85171200")

    def test_unconfirmed_twelve_digit_code_gets_half_credit(self) -> None:
        _dossier(self.service, USER, title="Onaylı", confirmed=True)
        _dossier(self.service, USER, title="Onaysız", gtip="851712000019", confirmed=False)
        report = _report(self.service)
        precision = next(item for item in report["components"] if item["key"] == "gtip_precision")
        self.assertEqual(precision["score"], 75)
        self.assertIn("Onaysız dosyasındaki 12 haneli kod kullanıcı onayı bekliyor", _titles(report, "low"))
        self.assertNotIn("Onaylı", " ".join(_titles(report)))

    def test_ambiguous_rates_and_pending_kdv_confirmation(self) -> None:
        _dossier(self.service, USER, ambiguous=["additional_duty"], kdv_status="possible")
        report = _report(self.service)
        certainty = next(item for item in report["components"] if item["key"] == "rate_certainty")
        self.assertEqual(certainty["score"], 0)
        self.assertIn("KDV oranı kullanıcı onayı bekliyor", _titles(report, "low"))
        self.assertTrue(any(title.startswith("Telefon dosyasında belirsiz oran: additional_duty") for title in _titles(report, "medium")))

    def test_expiring_measures_by_horizon(self) -> None:
        _dossier(self.service, USER)
        soon = (TODAY + timedelta(days=20)).isoformat()
        later = (TODAY + timedelta(days=60)).isoformat()
        far = (TODAY + timedelta(days=200)).isoformat()
        gone = (TODAY - timedelta(days=5)).isoformat()
        engine = FakeTradeEngine({
            "anti_dumping": [_hit(expires=soon), _hit(expires=far), _hit(expires=soon, origin_match=False)],
            "safeguard": [_hit("safeguard", expires=later), _hit("safeguard", expires=gone, status="expired")],
        })
        report = _report(self.service, trade_engine=engine)
        self.assertEqual(engine.calls, [("851712000011", "Çin", TODAY)])
        highs = [item for item in report["alerts"] if item["severity"] == "high"]
        self.assertEqual(len(highs), 1)
        self.assertEqual(highs[0]["title"], f"Damping önlemi 20 gün içinde bitiyor ({soon})")
        self.assertEqual((highs[0]["due_date"], highs[0]["gtip"], highs[0]["source"]), (soon, "851712000011", "trade_measures:anti_dumping"))
        self.assertIn(f"Korunma önlemi 60 gün içinde bitiyor ({later})", _titles(report, "medium"))
        self.assertTrue(any("süresi" in title and gone in title for title in _titles(report, "low")))
        validity = next(item for item in report["components"] if item["key"] == "measure_validity")
        self.assertEqual(validity["score"], 0)
        self.assertEqual(report["alerts"][0]["severity"], "high")  # sorted by severity first

    def test_trade_engine_failure_is_reported_not_fatal(self) -> None:
        _dossier(self.service, USER)
        report = _report(self.service, trade_engine=FakeTradeEngine(fail=True))
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("önlem listeleri sorgulanamadı", report["warnings"][0])
        self.assertEqual(next(item for item in report["components"] if item["key"] == "measure_validity")["score"], 100)

    def test_ledger_changes_on_dossier_and_watched_codes(self) -> None:
        _dossier(self.service, USER)
        self.service.add_watch(USER, gtip="6911", label="Porselen")
        recent = (TODAY - timedelta(days=3)).isoformat() + "T08:00:00+00:00"
        old = (TODAY - timedelta(days=70)).isoformat() + "T08:00:00+00:00"
        stale = (TODAY - timedelta(days=120)).isoformat() + "T08:00:00+00:00"
        ledger = FakeLedger([
            {"kind": "trade_measures", "source_id": "surveillance", "gtip": "851712000011", "change_type": "modified", "detected_at": recent},
            {"kind": "trade_measures", "source_id": "surveillance", "gtip": "851712000011", "change_type": "added", "detected_at": recent},
            {"kind": "tariff", "source_id": "import_regime", "gtip": "691110000011", "change_type": "modified", "detected_at": old},
            {"kind": "tariff", "source_id": "import_regime", "gtip": "691110000011", "change_type": "modified", "detected_at": stale},
        ])
        report = _report(self.service, ledger=ledger)
        self.assertEqual({call["since"] for call in ledger.calls}, {(TODAY - timedelta(days=90)).isoformat()})
        self.assertIn("Telefon dosyasındaki 851712… için gözetim tebliği değişti", _titles(report, "high"))
        self.assertIn("İzlenen 6911 için tarife satırı değişti", _titles(report, "low"))
        high = next(item for item in report["alerts"] if item["severity"] == "high")
        self.assertEqual(high["source"], "ledger:trade_measures:surveillance")
        self.assertIn("2 satır", high["detail"])
        exposure = next(item for item in report["components"] if item["key"] == "change_exposure")
        self.assertEqual(exposure["score"], 0)
        self.assertEqual(report["tracked_gtip_count"], 2)

    def test_used_goods_and_escalation_flags(self) -> None:
        _dossier(self.service, USER, title="Eski", condition="used", escalation=True)
        _dossier(self.service, USER, title="Yeni", gtip="851712000012")
        report = _report(self.service)
        escalation = next(item for item in report["components"] if item["key"] == "escalation")
        self.assertEqual(escalation["score"], 50)
        self.assertIn("Eski dosyası kullanılmış eşya içeriyor", _titles(report, "medium"))
        detail = next(item for item in report["alerts"] if item["title"] == "Eski dosyası uzman incelemesi istiyor")
        self.assertEqual(detail["detail"], "Oran belirsiz")

    def test_control_communique_year_rollover(self) -> None:
        _dossier(self.service, USER, title="Kataloglu", control_code="2025/9")
        _dossier(self.service, USER, title="Bekleyen", gtip="851712000012", control_code="2025/31")
        _dossier(self.service, USER, title="Güncel", gtip="851712000013", control_code="2026/9")
        report = _report(self.service, control_engine=FakeControlEngine(["2026/9"]))
        rollovers = [item for item in report["alerts"] if item["source"] == "controls:year_rollover"]
        self.assertEqual({item["severity"] for item in rollovers}, {"medium", "low"})
        known = next(item for item in rollovers if item["severity"] == "medium")
        self.assertEqual(known["title"], "Kataloglu dosyasındaki kontrol tebliği (2025/9) yıl geçişi")
        self.assertIn("2026/9", known["detail"])
        self.assertEqual(known["due_date"], "2026-01-01")
        self.assertNotIn("Güncel", " ".join(item["title"] for item in rollovers))

    def test_score_is_weighted_sum_and_status_bands(self) -> None:
        _dossier(self.service, USER, gtip="85171200", confirmed=False, ambiguous=["customs_duty"], condition="used")
        report = _report(self.service)
        scores = {item["key"]: item["score"] for item in report["components"]}
        self.assertEqual(scores, {"gtip_precision": 0, "rate_certainty": 0, "measure_validity": 100, "change_exposure": 100, "escalation": 0})
        self.assertEqual(report["score"], 40)
        self.assertEqual(report["status"], "risk")
        self.assertEqual(report["alert_counts"], {"high": 0, "medium": 3, "low": 0})

    def test_alerts_are_isolated_per_user_and_digest_is_stable(self) -> None:
        _dossier(self.service, OTHER, title="Başkasının", gtip="85171200", confirmed=False)
        self.assertEqual(_report(self.service, "sub-1")["alerts"], [])
        _dossier(self.service, USER)
        soon = (TODAY + timedelta(days=10)).isoformat()
        engine = FakeTradeEngine({"anti_dumping": [_hit(expires=soon)]})
        first, second = _report(self.service, trade_engine=engine), _report(self.service, trade_engine=engine)
        self.assertEqual(high_alert_digest(first), high_alert_digest(second))
        self.assertNotEqual(high_alert_digest(first), high_alert_digest(_report(self.service)))
        self.assertEqual(first["alerts"][0]["key"], second["alerts"][0]["key"])

    def test_compliance_email_lists_only_high_alert_headlines(self) -> None:
        report = {
            "score": 41, "dossier_count": 2, "watch_count": 1, "alert_counts": {"high": 1, "medium": 2, "low": 0},
            "alerts": [
                {"severity": "high", "title": "Damping önlemi 10 gün içinde bitiyor <b>", "gtip": "851712000011", "due_date": "2026-09-24"},
                {"severity": "medium", "title": "Gizli orta uyarı", "gtip": "6911", "due_date": None},
            ],
        }
        html_body = render_compliance_email(report, "https://gumruksor.com")
        self.assertIn("Uyum puanı 41/100", html_body)
        self.assertIn("&lt;b&gt;", html_body)
        self.assertIn("851712000011", html_body)
        self.assertNotIn("Gizli orta uyarı", html_body)
        self.assertIn("dosya içeriği e-postayla gönderilmez", html_body)


class ComplianceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir)
        self.accounts = _service(self.temp.name, USER, OTHER, {"sub": "admin", "email": "admin@example.com", "name": "admin"})
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        _dossier(self.accounts, USER, gtip="85171200", confirmed=False)
        _dossier(self.accounts, OTHER, title="Başkasının", gtip="6911", confirmed=False)
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.change_ledger, web_app.trade_measure_engine, web_app.control_engine)
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.auth, self.accounts, web_app.FixedWindowRateLimiter()
        web_app.change_ledger, web_app.trade_measure_engine, web_app.control_engine = FakeLedger(), FakeTradeEngine(), FakeControlEngine([])
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")

    def tearDown(self) -> None:
        self.client.close()
        (web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.change_ledger, web_app.trade_measure_engine, web_app.control_engine) = self.original
        self.temp.cleanup()

    def _get(self, user: dict | None = None):
        headers = {"Origin": "https://gumruksor.com"}
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.get("/api/account/compliance", headers=headers)

    def test_route_requires_login_and_returns_own_report_only(self) -> None:
        anonymous = self._get()
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.json()["code"], "authentication_required")

        response = self._get(USER)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        payload = response.json()
        self.assertEqual(payload["dossier_count"], 1)
        self.assertFalse(payload["email_alerts"])  # starter plan has no change_alerts
        self.assertIn("GTİP 8 haneli; 12 hane onaylanmadı", [item["title"] for item in payload["alerts"]])
        self.assertNotIn("Başkasının", response.text)
        self.assertLess(payload["score"], 100)

    def test_route_flags_email_alerts_for_change_alert_plans(self) -> None:
        self.accounts.admin_set_plan({"sub": "admin", "email": "admin@example.com"}, "sub-1", "team", "active")
        self.assertTrue(self._get(USER).json()["email_alerts"])

    def test_route_is_rate_limited(self) -> None:
        # Sayaç sabit pencerelidir (now // 60); saat dakika sınırını geçerse sıfırlanır.
        # Test bu sınıra denk gelip kırılmasın diye saat dondurulur.
        with mock.patch("time.time", return_value=1_770_000_000.0):
            statuses = [self._get(USER).status_code for _ in range(31)]
        self.assertEqual(statuses[:30], [200] * 30)
        self.assertEqual(statuses[30], 429)


class ComplianceNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_high_alert_digest_is_sent_once_per_day_and_per_change(self) -> None:
        temp = tempfile.TemporaryDirectory()
        service = _service(temp.name, USER, OTHER, {"sub": "admin", "email": "admin@example.com", "name": "admin"})
        service = AccountService(temp.name, admin_emails="admin@example.com")
        admin = {"sub": "admin", "email": "admin@example.com"}
        service.admin_set_plan(admin, "sub-1", "team", "active")  # change_alerts capability
        _dossier(service, USER)
        _dossier(service, OTHER, title="Starter")  # no capability -> never e-mailed
        sent: list[dict] = []

        class FakeSender:
            configured = True

            async def send(self, *, to, subject, html_body):
                sent.append({"to": to, "subject": subject, "html": html_body})
                return "id"

        reports = {"high": 1}

        def fake_report(google_sub: str) -> dict:
            alerts = [{"severity": "high", "title": f"Damping önlemi bitiyor {index}", "gtip": "851712000011", "due_date": "2026-10-01", "key": f"k{index}"} for index in range(reports["high"])]
            return {"score": 55, "dossier_count": 1, "watch_count": 0, "alerts": alerts, "alert_counts": {"high": len(alerts), "medium": 0, "low": 0}}

        with mock.patch.object(web_app, "account_service", service), mock.patch.object(web_app, "email_sender", FakeSender()), \
             mock.patch.object(web_app, "_build_compliance_report", fake_report):
            day_one = date(2026, 9, 14)
            first = await web_app.notify_compliance_alerts(today=day_one)
            again_same_day = await web_app.notify_compliance_alerts(today=day_one)
            next_day_unchanged = await web_app.notify_compliance_alerts(today=day_one + timedelta(days=1))
            reports["high"] = 2
            next_day_changed = await web_app.notify_compliance_alerts(today=day_one + timedelta(days=1))
            reports["high"] = 0
            later_no_highs = await web_app.notify_compliance_alerts(today=day_one + timedelta(days=2))
        self.assertEqual(first, {"users": 1, "sent": 1, "skipped": 0})
        self.assertEqual(again_same_day, {"users": 0, "sent": 0, "skipped": 0})
        self.assertEqual(next_day_unchanged, {"users": 1, "sent": 0, "skipped": 1})
        self.assertEqual(next_day_changed, {"users": 1, "sent": 1, "skipped": 0})
        self.assertEqual(later_no_highs, {"users": 0, "sent": 0, "skipped": 0})
        self.assertEqual([item["to"] for item in sent], ["a@example.com", "a@example.com"])
        self.assertIn("1 yüksek öncelikli uyarı", sent[0]["subject"])
        self.assertIn("Damping önlemi bitiyor 0", sent[0]["html"])
        temp.cleanup()


if __name__ == "__main__":
    unittest.main()
