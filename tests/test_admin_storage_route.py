"""Depolama uçlarının sözleşmesi: yalnız yönetici, ve yedek dizininin dışına çıkış yok.

Yedek dosyası kullanıcı hesaplarını, kanıt dosyalarını ve ücretli AB TARIC
arşivini içerir; indirme ucunun yetki kontrolü ile ad doğrulaması bu paketin en
kritik iki kilididir.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
import storage
from account_service import AccountService
from auth_service import GoogleAuthService

PUBLIC_ORIGIN = "https://gumruksor.com"
ADMIN = {"sub": "admin", "email": "admin@example.com"}


class AdminStorageAuthorizationTests(unittest.TestCase):
    """Exercise the real session parser and admin gate using only temporary data."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.auth = GoogleAuthService(
            client_id="test-client", client_secret="test-client-secret",
            session_secret="test-session-secret-that-is-long-enough", data_dir=self.dir,
        )
        self.accounts = AccountService(self.dir, admin_emails="admin@example.com")
        for name, value in (
            ("google_auth", self.auth), ("account_service", self.accounts),
            ("rate_limiter", web_app.FixedWindowRateLimiter()),
        ):
            patcher = patch.object(web_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict("os.environ", {"MEVZUAT_DATA_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        backups = self.dir / storage.BACKUP_DIR_NAME
        backups.mkdir()
        self.backup_name = "users.20260916T120000Z.sqlite3"
        self.backup_bytes = b"synthetic backup fixture; no real user data"
        (backups / self.backup_name).write_bytes(self.backup_bytes)
        # No TestClient context manager: application background jobs do not start.
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)
        self.addCleanup(self.client.close)

    def request(self, method, url, token=None):
        headers = {"Origin": PUBLIC_ORIGIN}
        if token is not None:
            headers["Cookie"] = f"{self.auth.session_cookie}={token}"
        return self.client.request(method, url, headers=headers)

    def assert_storage_denied(self, token=None):
        with patch.object(web_app.storage_service, "report", return_value={"backups": []}) as report, patch.object(
            web_app.storage_service, "backup_now", new_callable=AsyncMock
        ) as backup, patch.object(web_app, "resolve_backup_file", wraps=storage.resolve_backup_file) as resolve:
            for method, url in (
                ("GET", "/api/admin/storage"),
                ("POST", "/api/admin/storage"),
                ("GET", f"/api/admin/storage/backup/{self.backup_name}"),
            ):
                with self.subTest(method=method, url=url):
                    # Each case must reach the authorization gate, not a rate limit.
                    web_app.rate_limiter = web_app.FixedWindowRateLimiter()
                    response = self.request(method, url, token)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertIn("error", response.json())
                    self.assertNotIn(self.backup_bytes, response.content)
            report.assert_not_called()
            backup.assert_not_awaited()
            resolve.assert_not_called()

    def test_exact_admin_sessions_can_report_create_and_download_backups(self):
        for email in (
            "admin@example.com", " ADMIN@EXAMPLE.COM ",
            "hikmet044@gmail.com", "hiktan44@gmail.com",
        ):
            with self.subTest(email=email):
                token = self.auth.create_session({"sub": "test-admin", "email": email})
                web_app.rate_limiter = web_app.FixedWindowRateLimiter()
                with patch.object(web_app.storage_service, "report", return_value={"backups": []}) as report, patch.object(
                    web_app.storage_service, "backup_now", new_callable=AsyncMock
                ) as backup:
                    response = self.request("GET", "/api/admin/storage", token)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {"backups": []})
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
                    response = self.request("POST", "/api/admin/storage", token)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers.get("cache-control"), "no-store")
                    backup.assert_awaited_once()
                    self.assertEqual(report.call_count, 2)
                    response = self.request("GET", f"/api/admin/storage/backup/{self.backup_name}", token)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.content, self.backup_bytes)
                    self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_signed_nonadmins_cannot_access_any_backup_operation(self):
        for email in (
            "user@example.com", "admin@other.example", "admin@example.com.other.example",
            "hikmet044@other.example", "hiktan44@other.example",
            "hikmet044", "hikmet044@gmail", "hiktan44@@gmail.com", "", "@gmail.com",
        ):
            with self.subTest(email=email):
                self.assert_storage_denied(self.auth.create_session({"sub": "test-user", "email": email}))

    def test_anonymous_invalid_tampered_and_expired_sessions_are_denied(self):
        token = self.auth.create_session(ADMIN)
        body, signature = token.rsplit(".", 1)
        # Change significant signature bits so the HMAC verification rejects it.
        changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = f"{body}.{changed_signature}"
        with patch("auth_service.time.time", return_value=1):
            expired = self.auth.create_session(ADMIN)
        for candidate in (None, "invalid-session", tampered, expired):
            with self.subTest(token_kind=candidate if candidate is None else candidate[:12]):
                self.assert_storage_denied(candidate)


class AdminStorageRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.addCleanup(setattr, web_app, "rate_limiter", self.original_limiter)

    def test_anonymous_request_is_rejected(self) -> None:
        for method, url in (
            ("get", "/api/admin/storage"),
            ("post", "/api/admin/storage"),
            ("get", "/api/admin/storage/backup/users.20260916T120000Z.sqlite3"),
        ):
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertIn(response.status_code, (401, 403))
                self.assertIn("error", response.json())

    def test_admin_gets_the_storage_report(self) -> None:
        report = {"disk": {"percent_used": 42.0}, "databases": [], "backups": [], "warnings": []}
        with patch.object(web_app, "_require_admin", return_value=ADMIN), patch.object(
            web_app.storage_service, "report", return_value=report
        ):
            response = self.client.get("/api/admin/storage")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["disk"]["percent_used"], 42.0)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_post_runs_a_backup_and_returns_the_fresh_report(self) -> None:
        async def fake_backup():
            return {"created": [{"dataset": "users"}], "errors": []}

        with patch.object(web_app, "_require_admin", return_value=ADMIN), patch.object(
            web_app.storage_service, "backup_now", side_effect=fake_backup
        ) as backup, patch.object(web_app.storage_service, "report", return_value={"backups": [{"name": "x"}]}):
            response = self.client.post("/api/admin/storage")
        self.assertEqual(response.status_code, 200, response.text)
        backup.assert_called_once()
        self.assertEqual(response.json()["backups"][0]["name"], "x")


class AdminBackupDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.addCleanup(setattr, web_app, "rate_limiter", self.original_limiter)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "sir.txt").write_text("gizli", encoding="utf-8")
        backups = self.dir / storage.BACKUP_DIR_NAME
        backups.mkdir()
        self.backup_name = "users.20260916T120000Z.sqlite3"
        (backups / self.backup_name).write_bytes(b"SQLite format 3\x00 test")

    def _patched_dir(self):
        # Rota veri dizinini ortamdan çözüyor; testte geçici dizine yönlendirilir.
        return patch.dict("os.environ", {"MEVZUAT_DATA_DIR": str(self.dir)})

    def test_admin_downloads_an_existing_backup(self) -> None:
        with patch.object(web_app, "_require_admin", return_value=ADMIN), self._patched_dir():
            response = self.client.get(f"/api/admin/storage/backup/{self.backup_name}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(b"SQLite format 3", response.content)

    def test_a_path_outside_the_backup_directory_is_refused(self) -> None:
        # Asıl kilit: yedek dizininin dışındaki hiçbir dosya indirilememeli.
        for name in ("sir.txt", "users.sqlite3", "..%2F..%2Fetc%2Fpasswd", "users.20200101T000000Z.sqlite3"):
            with self.subTest(name=name), patch.object(
                web_app, "_require_admin", return_value=ADMIN
            ), self._patched_dir():
                response = self.client.get(f"/api/admin/storage/backup/{name}")
            self.assertNotEqual(response.status_code, 200, name)
            self.assertNotIn(b"gizli", response.content, name)


class HealthStorageFieldsTests(unittest.TestCase):
    """Sağlık ucu depolamayı gösterir ama dosya adı/yol sızdırmaz ve düşmez."""

    def setUp(self) -> None:
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def test_health_exposes_only_aggregate_storage_numbers(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("disk_percent_used", body)
        self.assertIn("data_bytes", body)
        text = response.text
        for leak in ("users.sqlite3", "eu_taric.sqlite3", "/data", "MEVZUAT_DATA_DIR"):
            self.assertNotIn(leak, text, leak)

    def test_a_broken_storage_report_does_not_break_the_health_check(self) -> None:
        # Coolify bu ucu canlılık kontrolü için okuyor: 200 dönmeye devam etmeli.
        with patch.object(web_app.storage_service, "report", side_effect=OSError("disk yok")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "healthy")
        self.assertIsNone(response.json()["disk_percent_used"])


if __name__ == "__main__":
    unittest.main()
