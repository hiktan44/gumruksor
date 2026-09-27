"""``POST /api/customs/precheck`` — yalnız kanıt paketi dönen istek kotadan düşülmez."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as web_app  # noqa: E402
from account_service import AccountService  # noqa: E402
from auth_service import GoogleAuthService  # noqa: E402

PUBLIC_ORIGIN = "https://gumruksor.com"
BODY = {
    "question": "Porselen tabak ithalatında vergi ve belgeler nelerdir?",
    "product_description": "Porselen yemek tabağı, 27 cm, ev kullanımı",
    "candidate_gtip": "691110000011",
    "origin_country": "Çin",
    "tariff_selection_confirmed": True,
}


class _FakeResult(SimpleNamespace):
    def model_dump(self, **_: object) -> dict:
        return {"status": self.status, "summary": "test"}


class _FakeAdvisor:
    def __init__(self, status: str) -> None:
        self.status = status

    async def analyse(self, _inquiry):
        return _FakeResult(status=self.status)


class PrecheckQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client",
            client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough",
            data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="")
        self.user = {"sub": "u-sub", "email": "u@example.com", "name": "U", "picture": ""}
        with sqlite3.connect(self.accounts.db_path) as connection:
            connection.execute(
                "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                (self.user["sub"], self.user["email"], self.user["name"], ""),
            )
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.customs_advisor_service)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        (web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.customs_advisor_service) = self.original
        self.temp.cleanup()

    def used(self) -> int:
        return self.accounts.account(self.user)["quotas"]["precheck"]["used"]

    def post(self, status: str):
        web_app.customs_advisor_service = _FakeAdvisor(status)
        headers = {
            "Origin": PUBLIC_ORIGIN,
            "Cookie": f"{self.auth.session_cookie}={self.auth.create_session(self.user)}",
        }
        return self.client.post("/api/customs/precheck", json=BODY, headers=headers)

    def test_evidence_only_result_is_not_charged(self) -> None:
        response = self.post("evidence_only")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.used(), 0)

    def test_interpreted_result_is_charged(self) -> None:
        response = self.post("preliminary")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.used(), 1)


if __name__ == "__main__":
    unittest.main()
