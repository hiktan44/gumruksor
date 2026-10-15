"""Risk ön kontrolü: kriter deposu, otomatik sinyaller ve HTTP sözleşmesi (yalnız geçici veri)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService
from risk_engine import RiskError, RiskStore, assess_level, automatic_signals, check

ORIGIN = "https://gumruksor.com"
OFFICIAL = {
    "gtip_prefix": "8517", "category": "tareks", "title": "Sentetik test kriteri", "direction": "import",
    "year_from": 2024, "source_label": "Test kaynağı", "source_url": "https://example.gov.tr/test",
}


class RiskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RiskStore(self.tmp.name)

    def test_official_requires_source(self):
        bad = {**OFFICIAL, "source_url": ""}
        with self.assertRaises(RiskError):
            self.store.add(bad, official=True, owner_sub=None)
        with self.assertRaises(RiskError):
            self.store.add({**OFFICIAL, "source_url": "http://insecure.example"}, official=True, owner_sub=None)

    def test_prefix_year_direction_matching(self):
        self.store.add(OFFICIAL, official=True, owner_sub=None)
        self.assertEqual(len(self.store.match("851713000000", "import", 2025, None)), 1)
        self.assertEqual(self.store.match("851713000000", "export", 2025, None), [])
        self.assertEqual(self.store.match("851713000000", "import", 2020, None), [])
        self.assertEqual(self.store.match("850000000000", "import", 2025, None), [])

    def test_user_note_is_private_and_labelled(self):
        note = self.store.add({**OFFICIAL, "source_url": "", "source_label": ""}, official=False, owner_sub="u1")
        self.assertFalse(note["official"])
        self.assertIn("resmî değildir", note["origin_label"])
        self.assertEqual(len(self.store.match("851713000000", "import", 2025, "u1")), 1)
        self.assertEqual(self.store.match("851713000000", "import", 2025, "u2"), [])
        self.assertEqual(self.store.match("851713000000", "import", 2025, None), [])
        self.assertFalse(self.store.delete_note(note["id"], "u2"))
        self.assertTrue(self.store.delete_note(note["id"], "u1"))

    def test_seed_file_is_idempotent_and_requires_sources(self):
        seed = Path(self.tmp.name) / "seed.json"
        seed.write_text(json.dumps({"criteria": [{**OFFICIAL, "id": "seed-1"}]}), encoding="utf-8")
        RiskStore(self.tmp.name, seed_path=seed)
        RiskStore(self.tmp.name, seed_path=seed)
        self.assertEqual(len(self.store.match("851713000000", "import", 2025, None)), 1)
        seed.write_text(json.dumps({"criteria": [{**OFFICIAL, "source_url": ""}]}), encoding="utf-8")
        with self.assertRaises(RiskError):
            RiskStore(self.tmp.name, seed_path=seed)

    def test_shipped_seed_loads(self):
        RiskStore(self.tmp.name, seed_path=Path(web_app.__file__).resolve().parent / "data" / "official" / "risk_criteria.json")


class SignalTests(unittest.TestCase):
    TRADE = {
        "anti_dumping": [
            {"status": "in_force", "origin_match": True, "product": "X", "country": "Çin", "rate_text": "%10", "legal_act": "Tebliğ 1"},
            {"status": "in_force", "origin_match": False, "product": "Y", "country": "Hindistan", "rate_text": "%5"},
            {"status": "expired", "origin_match": True, "product": "Z", "country": "Çin", "rate_text": "%1"},
        ],
        "safeguard": [], "surveillance": [{"status": "in_force", "unit_value_usd": 1500, "unit": "ton", "product": "Çelik"}], "tariff_quota": [],
    }

    def test_only_applicable_measures_become_signals(self):
        signals = automatic_signals(trade=self.TRADE, controls=None, origin_given=True)
        self.assertEqual([s["code"] for s in signals], ["anti_dumping", "surveillance"])
        self.assertEqual(signals[0]["level"], "high")

    def test_unknown_origin_downgrades_and_requests_input(self):
        trade = {"anti_dumping": [{"status": "in_force", "origin_match": None, "product": "X", "country": "Çin", "rate_text": "%10"}]}
        signals = automatic_signals(trade=trade, controls=None, origin_given=False)
        self.assertEqual(signals[0]["level"], "medium")
        self.assertEqual(signals[0]["needs_input"], "Menşe ülke")

    def test_control_match_is_medium(self):
        controls = {"status": "matched", "matches": [{"rule": {"title": "Test kontrolü", "authority": "TSE", "system": "TAREKS", "code": "2026/1"}}]}
        signals = automatic_signals(trade=None, controls=controls, origin_given=True)
        self.assertEqual(signals[0]["level"], "medium")

    def test_level_rule(self):
        self.assertEqual(assess_level([], []), "none")
        self.assertEqual(assess_level([], [{"official": False}]), "info")
        self.assertEqual(assess_level([], [{"official": True}]), "medium")
        self.assertEqual(assess_level([{"level": "high"}], []), "high")

    def test_empty_result_never_claims_no_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = check(RiskStore(tmp), gtip="8517130000", direction="import", year=2026, owner_sub=None,
                           trade=None, controls=None, origin_given=False)
        self.assertEqual(result["level"], "none")
        self.assertTrue(any("anlamına gelmez" in w for w in result["warnings"]))


class RiskHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=directory)
        self.accounts = AccountService(directory, admin_emails="admin@example.com")
        self.store = RiskStore(directory)
        for name, value in (("google_auth", self.auth), ("account_service", self.accounts), ("risk_store", self.store),
                            ("rate_limiter", web_app.FixedWindowRateLimiter())):
            patcher = patch.object(web_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(web_app.app, base_url=ORIGIN)
        self.addCleanup(self.client.close)

    def call(self, method, url, user=None, body=None):
        headers = {"Origin": ORIGIN}
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.request(method, url, headers=headers, json=body)

    def test_check_validates_gtip_and_returns_contract(self):
        self.assertEqual(self.call("POST", "/api/risk/check", body={"gtip": "1"}).status_code, 422)
        response = self.call("POST", "/api/risk/check", body={"gtip": "8517130000", "origin_country": "Çin", "year": 2026})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in ("level", "level_label", "signals", "criteria", "warnings"):
            self.assertIn(key, data)

    def test_user_cannot_add_official_but_admin_can(self):
        user = {"sub": "u1", "email": "user@example.com"}
        admin = {"sub": "a1", "email": "admin@example.com"}
        self.assertEqual(self.call("POST", "/api/risk/criteria", body=OFFICIAL).status_code, 401)
        self.assertEqual(self.call("POST", "/api/risk/criteria", user, {**OFFICIAL, "official": True}).status_code, 403)
        note = self.call("POST", "/api/risk/criteria", user, {**OFFICIAL, "source_url": "", "source_label": ""})
        self.assertEqual(note.status_code, 201)
        self.assertFalse(note.json()["official"])
        self.assertEqual(self.call("POST", "/api/risk/criteria", admin, {**OFFICIAL, "official": True}).status_code, 201)

    def test_notes_are_visible_only_to_owner_in_check(self):
        user = {"sub": "u1", "email": "user@example.com"}
        self.call("POST", "/api/risk/criteria", user, {**OFFICIAL, "source_url": "", "source_label": ""})
        body = {"gtip": "8517130000", "year": 2026}
        self.assertEqual(len(self.call("POST", "/api/risk/check", user, body).json()["criteria"]), 1)
        self.assertEqual(len(self.call("POST", "/api/risk/check", None, body).json()["criteria"]), 0)

    def test_delete_requires_ownership(self):
        owner, other = {"sub": "u1", "email": "a@example.com"}, {"sub": "u2", "email": "b@example.com"}
        note_id = self.call("POST", "/api/risk/criteria", owner, {**OFFICIAL, "source_url": "", "source_label": ""}).json()["id"]
        self.assertEqual(self.call("DELETE", f"/api/risk/criteria/{note_id}", other).status_code, 404)
        self.assertEqual(self.call("DELETE", f"/api/risk/criteria/{note_id}", owner).status_code, 200)


if __name__ == "__main__":
    unittest.main()
