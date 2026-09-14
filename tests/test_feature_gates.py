"""Plan/role feature gates on HTTP routes (PRD: modüler erişim ve paket mimarisi)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService

PUBLIC_ORIGIN = "https://gumruksor.com"


def profile(sub: str, email: str) -> dict[str, str]:
    return {"sub": sub, "email": email, "name": sub, "picture": ""}


class FeatureGateRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client", client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.free = profile("free-sub", "free@example.com")
        self.paid = profile("paid-sub", "paid@example.com")
        self.admin = profile("admin-sub", "admin@example.com")
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.free, self.paid, self.admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )
        self.accounts.admin_set_plan(self.admin, "paid-sub", "expert", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def request(self, method: str, path: str, user: dict[str, str] | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Origin", PUBLIC_ORIGIN)
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.request(method, path, headers=headers, **kwargs)

    def test_scenarios_require_login_and_a_plan_with_the_feature(self) -> None:
        body = {"gtip": "851712", "origins": ["Çin", "Almanya"]}
        anonymous = self.request("POST", "/api/tariff/scenarios", json=body)
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.json()["code"], "authentication_required")

        locked = self.request("POST", "/api/tariff/scenarios", self.free, json=body)
        self.assertEqual(locked.status_code, 403, locked.text)
        payload = locked.json()
        self.assertEqual(payload["code"], "feature_required")
        self.assertEqual(payload["feature"], "scenario_compare")
        self.assertIn("Uzman", [item["name"] for item in payload["plans"]])
        self.assertIn("paketinizde yok", payload["error"])

        # Past the gate the route reaches the tariff engine (stubbed: no network in tests).
        with patch.object(web_app.tariff_engine, "lookup", new=AsyncMock(side_effect=RuntimeError("stub"))) as lookup:
            allowed = self.request("POST", "/api/tariff/scenarios", self.paid, json=body)
            self.assertNotIn(allowed.status_code, (401, 403), allowed.text)
            as_admin = self.request("POST", "/api/tariff/scenarios", self.admin, json=body)
            self.assertNotIn(as_admin.status_code, (401, 403), as_admin.text)
        self.assertGreaterEqual(lookup.await_count, 2)

    def test_bulk_costing_is_premium_only(self) -> None:
        body = {"rows": [{"gtip": "851712", "invoice_value": 100}]}
        locked = self.request("POST", "/api/tariff/bulk", self.paid, json=body)
        self.assertEqual(locked.status_code, 403, locked.text)
        self.assertEqual(locked.json()["feature"], "bulk_costing")
        self.accounts.admin_set_plan(self.admin, "paid-sub", "premium", "active")  # PRD alias → team
        with patch.object(web_app, "bulk_calculate_rows", new=AsyncMock(side_effect=RuntimeError("stub"))):
            allowed = self.request("POST", "/api/tariff/bulk", self.paid, json=body)
        self.assertNotIn(allowed.status_code, (401, 403), allowed.text)

    def test_gates_stay_open_without_google_oauth(self) -> None:
        web_app.google_auth = GoogleAuthService(data_dir=Path(self.temp.name))
        self.assertFalse(web_app.google_auth.configured)
        with patch.object(web_app.tariff_engine, "lookup", new=AsyncMock(side_effect=RuntimeError("stub"))):
            response = self.request("POST", "/api/tariff/scenarios", json={"gtip": "851712", "origins": ["Çin", "Almanya"]})
        self.assertNotIn(response.status_code, (401, 403), response.text)

    def test_admin_assigns_roles_and_account_exposes_capabilities(self) -> None:
        denied = self.request("PUT", "/api/admin/users/free-sub/role", self.paid, json={"role": "editor"})
        self.assertEqual(denied.status_code, 403)
        updated = self.request("PUT", "/api/admin/users/free-sub/role", self.admin, json={"role": "editor"})
        self.assertEqual(updated.status_code, 200, updated.text)
        invalid = self.request("PUT", "/api/admin/users/free-sub/role", self.admin, json={"role": "root"})
        self.assertEqual(invalid.status_code, 422)
        account = self.request("GET", "/api/account", self.free).json()
        self.assertEqual(account["role"], "editor")
        self.assertIn("data_review", account["capabilities"])
        self.assertNotIn("scenario_compare", account["capabilities"])
        self.assertEqual(account["plan"]["tier"], "essentials")
        overview = self.request("GET", "/api/admin/overview", self.admin).json()
        roles = {row["google_sub"]: row["role"] for row in overview["users"]}
        self.assertEqual(roles["free-sub"], "editor")
        plans = self.request("GET", "/api/plans").json()
        self.assertIn("scenario_compare", plans["features"])
        self.assertEqual(next(p for p in plans["plans"] if p["code"] == "expert")["tier"], "pro")


if __name__ == "__main__":
    unittest.main()
