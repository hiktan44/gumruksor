"""Arka plan işleri kütüğü: kapalı iş görünür, çöken döngü sessizce kaybolmaz.

Bu dosyanın koruduğu değişmezler:

* **Kapalı iş listeden düşmez.** ``if FLAG: append`` deseninde bayrak kapalıyken iş
  hiçbir ekranda görünmüyordu; artık sebebiyle birlikte kütükte durur.
* **Çöken döngü işaretlenir.** ``asyncio.create_task`` ile başlatılan bir görev
  istisnayla biterse süreç çalışmaya devam eder, yalnız o iş durur — kütük bunu
  ``failed`` olarak ve istisnanın türüyle gösterir.
* Hata kaydı gizli değer taşımaz: yalnız sınıf adı ve kısaltılmış mesaj.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from background_jobs import JOB_CATALOGUE, JobRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = JobRegistry()

    def test_disabled_job_stays_visible_with_its_reason(self) -> None:
        self.registry.declare(
            "eu-taric-fill", enabled=False, reason="EU_TARIC_FILL_ENABLED kapalı."
        )
        row = self.registry.snapshot()[0]
        self.assertEqual(row["name"], "eu-taric-fill")
        self.assertEqual(row["state"], "disabled")
        self.assertIn("EU_TARIC_FILL_ENABLED", row["disabled_reason"])
        # Ücretli iş panelde ayrıca işaretlenir.
        self.assertEqual(row["cost"], "paid")

    def test_running_job_is_reported_as_running(self) -> None:
        async def scenario() -> list[dict]:
            started = asyncio.Event()

            async def loop() -> None:
                started.set()
                await asyncio.sleep(30)

            task = asyncio.create_task(loop(), name="trade-measures-sync")
            self.registry.track("trade-measures-sync", task)
            await started.wait()
            rows = self.registry.snapshot()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return rows

        rows = asyncio.run(scenario())
        self.assertEqual(rows[0]["state"], "running")
        self.assertIsNotNone(rows[0]["started_at"])

    def test_a_crashed_loop_is_recorded_with_its_exception_type(self) -> None:
        async def scenario() -> None:
            async def loop() -> None:
                raise RuntimeError("kaynak yanıt vermedi")

            task = asyncio.create_task(loop(), name="ebti-sync")
            self.registry.track("ebti-sync", task)
            with self.assertRaises(RuntimeError):
                await task
            # Bitiş geri çağrısı bir sonraki döngü turunda koşar.
            await asyncio.sleep(0)

        asyncio.run(scenario())
        row = self.registry.snapshot()[0]
        self.assertEqual(row["state"], "failed")
        self.assertIn("RuntimeError", row["error"])
        self.assertIsNotNone(row["error_at"])

    def test_cancelled_loop_is_not_reported_as_a_crash(self) -> None:
        async def scenario() -> None:
            async def loop() -> None:
                await asyncio.sleep(30)

            task = asyncio.create_task(loop(), name="vat-lists-sync")
            self.registry.track("vat-lists-sync", task)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0)

        asyncio.run(scenario())
        row = self.registry.snapshot()[0]
        self.assertEqual(row["state"], "cancelled")
        self.assertIsNone(row["error"])

    def test_error_text_is_truncated(self) -> None:
        async def scenario() -> None:
            async def loop() -> None:
                raise ValueError("x" * 5000)

            task = asyncio.create_task(loop(), name="eu-vat-sync")
            self.registry.track("eu-vat-sync", task)
            with self.assertRaises(ValueError):
                await task
            await asyncio.sleep(0)

        asyncio.run(scenario())
        row = self.registry.snapshot()[0]
        self.assertLess(len(row["error"]), 260)

    def test_failed_jobs_sort_first(self) -> None:
        async def scenario() -> None:
            async def broken() -> None:
                raise RuntimeError("bozuk")

            self.registry.declare("trade-measures-sync")
            task = asyncio.create_task(broken(), name="ebti-sync")
            self.registry.track("ebti-sync", task)
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)

        asyncio.run(scenario())
        rows = self.registry.snapshot()
        self.assertEqual(rows[0]["name"], "ebti-sync")

    def test_summary_counts_and_flags_paid_work(self) -> None:
        self.registry.declare("eu-taric-fill")
        self.registry.declare("access2markets-fill", enabled=False, reason="kapalı")

        async def scenario() -> None:
            async def loop() -> None:
                await asyncio.sleep(30)

            task = asyncio.create_task(loop(), name="eu-taric-fill")
            self.registry.track("eu-taric-fill", task)
            await asyncio.sleep(0)
            summary = self.registry.summary()
            self.assertEqual(summary["running"], 1)
            self.assertEqual(summary["disabled"], 1)
            # Ücretli bir iş açıksa bu ayrıca bildirilir: para harcayan işi görmeden bırakma.
            self.assertEqual(summary["paid"], ["eu-taric-fill"])
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())

    def test_unknown_job_is_listed_rather_than_hidden(self) -> None:
        self.registry.declare("yeni-is")
        row = self.registry.snapshot()[0]
        self.assertEqual(row["label"], "yeni-is")
        self.assertIn("künye", row["purpose"])

    def test_catalogue_covers_every_registered_loop(self) -> None:
        """Sunucudaki her döngünün Türkçe künyesi olmalı; kimliksiz iş panelde anlamsızdır."""
        import mevzuat_mcp_server  # noqa: PLC0415

        for name, _factory in mevzuat_mcp_server.BACKGROUND_LOOPS:
            self.assertIn(name, JOB_CATALOGUE, f"{name} için künye tanımlı değil")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as web_app

        self.web_app = web_app
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.addCleanup(lambda: setattr(web_app, "rate_limiter", self.original_limiter))

    def test_route_requires_an_administrator(self) -> None:
        from starlette.testclient import TestClient

        with TestClient(self.web_app.app, base_url="https://gumruksor.com") as client:
            response = client.get("/api/admin/background-jobs")
        # Yönetici olmayan istek veriyi görmez; açık uç değildir.
        self.assertIn(response.status_code, (401, 403))


    def test_route_returns_jobs_with_their_state_for_an_administrator(self) -> None:
        from starlette.testclient import TestClient

        original = self.web_app._require_admin
        self.web_app._require_admin = lambda request: {"email": "admin@example.com"}
        self.addCleanup(lambda: setattr(self.web_app, "_require_admin", original))
        with TestClient(self.web_app.app, base_url="https://gumruksor.com") as client:
            response = client.get("/api/admin/background-jobs")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        names = {job["name"] for job in body["jobs"]}
        # Kapalı ücretli dolum listede olmalı: görünmeyen iş teşhis edilemez.
        self.assertIn("eu-taric-fill", names)
        self.assertIn("trade-measures-sync", names)
        # Döngüsü olmayan ama sorguda çalışan kaynak da listelenir.
        self.assertIn("comtrade", names)
        for job in body["jobs"]:
            self.assertIn("state", job)
            self.assertIn("purpose", job)
        self.assertIn("running", body["summary"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
