"""Streaming (SSE) and shareable-session routes for the customs assistant.

No real LLM call: ``customs_assistant.ask`` is replaced with a scripted async
mock, exactly as the existing assistant route tests do.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService

PUBLIC_ORIGIN = "https://gumruksor.com"


def _answer_payload() -> dict[str, Any]:
    return {
        "answer": "Gümrük vergisi %12.",
        "claims": [{"text": "Oran %12.", "source_ids": ["tool_1"]}],
        "gtip_candidates": [],
        "rates": [{"name": "Gümrük vergisi", "value": "12", "source_ids": ["tool_1"]}],
        "next_steps": [],
        "tool_calls": [{"id": "tool_1", "name": "lookup_tariff_measures", "args": {"gtip": "610463000000"}, "summary": "matched"}],
        "sources": [{"id": "tool_1", "tool": "lookup_tariff_measures", "title": "matched"}],
        "unverified": [],
        "warnings": [],
        "model": "fake",
        "as_of": "2026-09-14T00:00:00+03:00",
        "legal_notice": "Bu bir ön değerlendirmedir.",
    }


class AssistantSharingRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client", client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.user = {"sub": "user-sub", "email": "user@example.com", "name": "user", "picture": ""}
        with sqlite3.connect(self.accounts.db_path) as connection:
            connection.execute(
                "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                (self.user["sub"], self.user["email"], self.user["name"], self.user["picture"]),
            )
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def _cookie(self) -> dict[str, str]:
        return {"Origin": PUBLIC_ORIGIN, "Cookie": f"{self.auth.session_cookie}={self.auth.create_session(self.user)}"}

    def _ask_mock(self) -> AsyncMock:
        from assistant import AssistantResponse
        return AsyncMock(return_value=AssistantResponse.model_validate(_answer_payload()))

    # ---- streaming ------------------------------------------------------
    def test_stream_requires_login(self) -> None:
        response = self.client.post("/api/customs/assistant/stream", json={"question": "Oran nedir?"}, headers={"Origin": PUBLIC_ORIGIN})
        self.assertEqual(response.status_code, 401)

    def test_stream_emits_verified_answer_as_sse(self) -> None:
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            response = self.client.post("/api/customs/assistant/stream", json={"question": "Çin menşeli şort için oran?"}, headers=self._cookie())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        body = response.text
        self.assertIn("event: start", body)
        self.assertIn("event: answer", body)
        self.assertIn("event: done", body)
        self.assertIn("Gümrük vergisi %12.", body)

    # ---- share + fetch --------------------------------------------------
    def test_share_persists_and_shared_link_returns_it(self) -> None:
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            created = self.client.post("/api/customs/assistant/share", json={"question": "Çin menşeli şort için oran?"}, headers=self._cookie())
        self.assertEqual(created.status_code, 200, created.text)
        token = created.json()["token"]
        self.assertTrue(token)
        self.assertIn(f"/chat/shared/{token}", created.json()["url"])

        fetched = self.client.get(f"/api/customs/assistant/shared/{token}", headers={"Origin": PUBLIC_ORIGIN})
        self.assertEqual(fetched.status_code, 200, fetched.text)
        payload = fetched.json()
        self.assertEqual(payload["question"], "Çin menşeli şort için oran?")
        self.assertEqual(payload["response"]["answer"], "Gümrük vergisi %12.")
        self.assertIn("teyit", payload["notice"])

    def test_share_requires_login(self) -> None:
        response = self.client.post("/api/customs/assistant/share", json={"question": "Oran nedir?"}, headers={"Origin": PUBLIC_ORIGIN})
        self.assertEqual(response.status_code, 401)

    def test_unknown_share_token_is_404(self) -> None:
        response = self.client.get("/api/customs/assistant/shared/does-not-exist", headers={"Origin": PUBLIC_ORIGIN})
        self.assertEqual(response.status_code, 404)

    def test_browser_supplied_answer_is_not_trusted(self) -> None:
        # Paylaşım yanıtı sunucuda yeniden üretilir; gövdedeki sahte "answer" yok sayılır.
        with patch.object(web_app.customs_assistant, "ask", new=self._ask_mock()):
            created = self.client.post(
                "/api/customs/assistant/share",
                json={"question": "Oran nedir?", "answer": "SAHTE %0", "rates": [{"name": "x", "value": "0"}]},
                headers=self._cookie(),
            )
        token = created.json()["token"]
        fetched = self.client.get(f"/api/customs/assistant/shared/{token}", headers={"Origin": PUBLIC_ORIGIN})
        self.assertEqual(fetched.json()["response"]["answer"], "Gümrük vergisi %12.")


if __name__ == "__main__":
    unittest.main()
