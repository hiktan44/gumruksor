"""Depolama uçlarının sözleşmesi: yalnız yönetici, ve yedek dizininin dışına çıkış yok.

Yedek dosyası kullanıcı hesaplarını, kanıt dosyalarını ve ücretli AB TARIC
arşivini içerir; indirme ucunun yetki kontrolü ile ad doğrulaması bu paketin en
kritik iki kilididir.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import app as web_app
import storage

PUBLIC_ORIGIN = "https://gumruksor.com"
ADMIN = {"sub": "admin", "email": "admin@example.com"}


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
