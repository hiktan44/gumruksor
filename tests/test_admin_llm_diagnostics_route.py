from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app

PUBLIC_ORIGIN = "https://gumruksor.com"


class AdminLlmDiagnosticsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.addCleanup(setattr, web_app, "rate_limiter", self.original_limiter)

    def test_anonymous_request_is_rejected(self) -> None:
        response = self.client.get("/api/admin/llm-diagnostics?vision=1")
        self.assertIn(response.status_code, (401, 403))
        self.assertIn("error", response.json())

    def test_admin_gets_report(self) -> None:
        report = {"mode": "vision", "primary": "gemini", "keys": {"gemini": True}, "chains": {}, "checks": [], "healthy": False}
        with patch.object(web_app, "_require_admin", return_value={"sub": "admin", "email": "admin@example.com"}), patch(
            "customs_advisor.diagnose_llm_providers", new=AsyncMock(return_value=report)
        ) as diagnose:
            response = self.client.get("/api/admin/llm-diagnostics?vision=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["primary"], "gemini")
        diagnose.assert_awaited_once_with(vision=True)
        self.assertEqual(response.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
