"""Onay akışı HTTP testleri için ortak kurulum (yalnız geçici veri)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService
from education import EducationStore
from risk_engine import RiskStore

ORIGIN = "https://gumruksor.com"
ADMIN = {"sub": "a1", "email": "admin@example.com"}
USER = {"sub": "u1", "email": "user@example.com"}
CONSULTANT = {"sub": "c1", "email": "danisman@example.com"}


class ReviewHttpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=directory)
        self.accounts = AccountService(directory, admin_emails="admin@example.com")
        self.risk = RiskStore(directory)
        self.education = EducationStore(directory)
        for person in (ADMIN, CONSULTANT, USER):
            self.auth.upsert_user(person)
        self.accounts.admin_set_role(ADMIN, CONSULTANT["sub"], "consultant")
        for name, value in (("google_auth", self.auth), ("account_service", self.accounts), ("risk_store", self.risk),
                            ("education_store", self.education), ("rate_limiter", web_app.FixedWindowRateLimiter())):
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
