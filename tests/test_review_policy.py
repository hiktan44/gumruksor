"""Editorial review gate (PRD Faz 1.3): decision table, engine gating, service and HTTP routes."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from change_ledger import ChangeLedger
from classification_evidence import ClassificationEvidenceEngine
from control_engine import ImportControlEngine
from review_policy import DiffSummary, ReviewPolicy, ReviewService, decide, policy_from_env
from tariff_engine import TariffEngine, _ParsedArchive


def _measure(gtip: str, rate: float, *, group: str = "8", footnote: str | None = None) -> dict:
    return {
        "gtip": gtip, "measure_type": "customs_duty", "rate": rate, "rate_text": str(rate), "country_group": group,
        "country_group_description": "Diğer Ülkeler", "footnote": footnote, "description": None, "condition": None,
        "list_name": "Liste II", "source_file": "ek.xlsx", "source_sheet": "84", "source_row": 3,
        "automatic_calculation_allowed": True,
    }


class DecisionTableTests(unittest.TestCase):
    def test_off_mode_always_approves(self) -> None:
        summary = DiffSummary(total_rows=10, previous_rows=100, added=5, removed=5, modified=5)
        self.assertEqual(decide(ReviewPolicy(mode="off"), summary, parse_warnings=["x"]).status, "approved")

    def test_strict_mode_always_waits(self) -> None:
        summary = DiffSummary(total_rows=100, previous_rows=100)
        decision = decide(ReviewPolicy(mode="strict"), summary, first_snapshot=True)
        self.assertTrue(decision.pending)

    def test_auto_mode_thresholds(self) -> None:
        policy = ReviewPolicy(mode="auto", max_auto_rows=10, max_auto_ratio=0.05)
        small = DiffSummary(total_rows=1000, previous_rows=1000, modified=3)
        self.assertEqual(decide(policy, small).status, "approved")
        many_rows = DiffSummary(total_rows=1000, previous_rows=1000, modified=11)
        self.assertTrue(decide(policy, many_rows).pending)
        high_ratio = DiffSummary(total_rows=50, previous_rows=50, modified=4)  # 8 % > 5 %
        self.assertTrue(decide(policy, high_ratio).pending)
        drop = DiffSummary(total_rows=70, previous_rows=100, removed=3)
        reasons = " ".join(decide(policy, drop).reasons)
        self.assertIn("satır sayısı", reasons)

    def test_auto_mode_parse_warnings_block_unless_disabled(self) -> None:
        summary = DiffSummary(total_rows=100, previous_rows=100, modified=1)
        self.assertTrue(decide(ReviewPolicy(mode="auto"), summary, parse_warnings=["bilinmeyen sayfa"]).pending)
        relaxed = ReviewPolicy(mode="auto", block_on_warnings=False)
        self.assertEqual(decide(relaxed, summary, parse_warnings=["bilinmeyen sayfa"]).status, "approved")

    def test_first_snapshot_goes_live_in_auto_mode(self) -> None:
        summary = DiffSummary(total_rows=5000, previous_rows=0)
        self.assertEqual(decide(ReviewPolicy(mode="auto"), summary, first_snapshot=True).status, "approved")
        self.assertTrue(decide(ReviewPolicy(mode="auto"), summary, parse_warnings=["x"], first_snapshot=True).pending)

    def test_policy_from_env_defaults_and_validation(self) -> None:
        self.assertEqual(policy_from_env({}).mode, "off")
        policy = policy_from_env({"DATA_REVIEW_MODE": "AUTO", "DATA_REVIEW_MAX_AUTO_ROWS": "50", "DATA_REVIEW_MAX_AUTO_RATIO": "0.1", "DATA_REVIEW_BLOCK_ON_WARNINGS": "0"})
        self.assertEqual((policy.mode, policy.max_auto_rows, policy.max_auto_ratio, policy.block_on_warnings), ("auto", 50, 0.1, False))
        self.assertEqual(policy_from_env({"DATA_REVIEW_MODE": "weird", "DATA_REVIEW_MAX_AUTO_ROWS": "abc"}).max_auto_rows, 200)
        with self.assertRaises(ValueError):
            ReviewPolicy(mode="nope")


class TariffReviewGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp.name)
        self.engine.ledger = ChangeLedger(self.temp.name)
        self.engine.review_policy = ReviewPolicy(mode="auto", max_auto_rows=1, max_auto_ratio=0.5)

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp.cleanup()

    async def _sync_with(self, measures: list[dict], archive: bytes, warnings: list[str] | None = None) -> None:
        source = next(item for item in self.engine.sources if item["id"] == "import_regime")
        parsed = _ParsedArchive(measures=measures, metadata={})
        parsed.warnings.extend(warnings or [])
        with patch.object(self.engine, "_discover_archive", new=AsyncMock(return_value="https://ticaret.gov.tr/a.zip")), patch.object(
            self.engine, "_download_archive", new=AsyncMock(return_value=archive)
        ), patch.object(self.engine, "_parse_archive", return_value=parsed):
            await self.engine._sync_source(source)

    def _statuses(self) -> dict[str, tuple[str, int]]:
        with self.engine._connect() as db:
            return {row["id"]: (row["status"], int(row["active"])) for row in db.execute("SELECT id,status,active FROM tariff_snapshots")}

    async def test_large_diff_waits_and_lookup_keeps_previous_version(self) -> None:
        first = [_measure("851712000000", 0.0), _measure("847130000000", 0.0), _measure("940320000000", 3.0)]
        await self._sync_with(first, b"one")
        first_id = next(iter(self._statuses()))
        self.assertEqual(self._statuses()[first_id], ("approved", 1))

        second = [_measure("851712000000", 2.0), _measure("847130000000", 5.0), _measure("940320000000", 3.0)]
        await self._sync_with(second, b"two")
        statuses = self._statuses()
        pending_id = next(key for key, value in statuses.items() if value[0] == "pending_review")
        self.assertEqual(statuses[first_id], ("approved", 1))
        self.assertEqual(statuses[pending_id], ("pending_review", 0))
        self.assertEqual(self.engine.status().pending_review_count, 1)
        self.assertEqual(self.engine.status().active_snapshots[0].id, first_id)

        lookup = await self.engine.lookup("851712000000", origin_country="Çin", auto_sync=False)
        self.assertEqual([m.rate_text for m in lookup.alternatives], ["0.0"])  # still the approved version
        self.assertEqual([snap.id for snap in lookup.snapshots], [first_id])

        queue = self.engine.pending_reviews()
        self.assertEqual(queue[0]["snapshot_id"], pending_id)
        self.assertEqual(queue[0]["diff_summary"]["modified"], 2)
        self.assertTrue(queue[0]["diff_summary"]["reasons"])
        batch = self.engine.ledger.batch(queue[0]["ledger_batch"])
        self.assertEqual(batch["review_status"], "pending_review")

        # Re-downloading the same pending archive must not activate it.
        await self._sync_with(second, b"two")
        self.assertEqual(self._statuses()[pending_id], ("pending_review", 0))

        result = self.engine.review_snapshot(pending_id, "approve", reviewed_by="editor@example.com", note="RG kontrol edildi")
        self.assertEqual(result["status"], "approved")
        statuses = self._statuses()
        self.assertEqual(statuses[pending_id], ("approved", 1))
        self.assertEqual(statuses[first_id], ("approved", 0))
        lookup = await self.engine.lookup("851712000000", origin_country="Çin", auto_sync=False)
        self.assertEqual([m.rate_text for m in lookup.alternatives], ["2.0"])

    async def test_rejected_archive_never_activates_again(self) -> None:
        await self._sync_with([_measure("851712000000", 0.0)], b"one")
        await self._sync_with([_measure("851712000000", 9.0), _measure("1", 1.0)], b"bad", warnings=["bilinmeyen sayfa düzeni"])
        pending_id = next(key for key, value in self._statuses().items() if value[0] == "pending_review")
        self.assertEqual(self.engine.pending_reviews()[0]["parse_warnings"], ["bilinmeyen sayfa düzeni"])
        self.engine.review_snapshot(pending_id, "reject", reviewed_by="editor@example.com", note="hatalı ayrıştırma")
        self.assertEqual(self._statuses()[pending_id], ("rejected", 0))
        await self._sync_with([_measure("851712000000", 9.0), _measure("1", 1.0)], b"bad")
        self.assertEqual(self._statuses()[pending_id], ("rejected", 0))
        self.assertEqual(self.engine.pending_reviews(), [])
        self.assertEqual(self.engine.status().pending_review_count, 0)
        # The next good archive compares against the approved version, not the rejected one.
        await self._sync_with([_measure("851712000000", 0.0)], b"one")
        self.assertEqual(self.engine.status().active_snapshots[0].archive_sha256[:4], self.engine.status().active_snapshots[0].archive_sha256[:4])
        self.assertEqual(sum(1 for value in self._statuses().values() if value[1] == 1), 1)

    async def test_off_mode_keeps_immediate_activation(self) -> None:
        self.engine.review_policy = ReviewPolicy()
        await self._sync_with([_measure("851712000000", 0.0)], b"one")
        await self._sync_with([_measure("851712000000", 7.0)], b"two", warnings=["uyarı"])
        self.assertEqual({value for value in self._statuses().values()}, {("approved", 0), ("approved", 1)})
        self.assertEqual(self.engine.status().review_mode, "off")

    async def test_legacy_rows_without_review_columns_are_approved(self) -> None:
        with self.engine._connect() as db:
            db.execute(
                "INSERT INTO tariff_snapshots (id,source_id,source_title,landing_url,archive_url,archive_sha256,retrieved_at,checked_at,valid_from,measure_count,active,metadata_json) "
                "VALUES ('import_regime:legacy','import_regime','t','https://ticaret.gov.tr','https://ticaret.gov.tr/a.zip','c','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2026-01-01',1,1,'{}')"
            )
        self.assertEqual(self.engine.pending_reviews(), [])
        with self.engine._connect() as db:
            self.assertEqual(db.execute("SELECT status FROM tariff_snapshots WHERE id='import_regime:legacy'").fetchone()[0], "approved")


class ControlReviewGateTests(unittest.TestCase):
    def _insert(self, engine: ImportControlEngine, snapshot_id: str, *, status: str, active: int, rows: list[tuple[str, str]]) -> None:
        with engine._connect() as db:
            db.execute(
                """INSERT INTO control_snapshots (id, code, title, category, mevzuat_id, source_url,
                official_gazette_date, official_gazette_number, document_sha256, retrieved_at, valid_from,
                scope_count, authority, system, risk_based, physical_inspection_possible,
                laboratory_test_possible, required_documents_excerpt, active, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, "2026/3", "Atık Tebliği", "atıklar", "3", "https://mevzuat.gov.tr/x", "2025-12-31", "33124",
                 f"sha-{snapshot_id}", f"2026-0{len(snapshot_id)}-01T00:00:00+03:00", "2026-01-01", len(rows), "Bakanlık", "Bakanlık", 0, 1, 0, None, active, status),
            )
            db.executemany(
                "INSERT INTO control_scope (snapshot_id, gtip_prefix, description, source_line, source_offset, excluded, list_kind) VALUES (?,?,?,?,?,?,?)",
                [(snapshot_id, prefix, description, "l", index, 0, "scope") for index, (prefix, description) in enumerate(rows, start=1)],
            )

    def test_pending_snapshot_is_invisible_until_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ImportControlEngine(data_dir=directory)
            engine.ledger = ChangeLedger(directory)
            self._insert(engine, "old", status="approved", active=1, rows=[("3915", "Plastik döküntü")])
            self._insert(engine, "newer", status="pending_review", active=0, rows=[("3915", "Plastik döküntü"), ("4707", "Kâğıt")])
            self.assertEqual([item["snapshot_id"] for item in engine.pending_reviews()], ["newer"])
            self.assertEqual(engine.status().pending_review_count, 1)
            self.assertEqual([snap.id for snap in engine.status().active_snapshots], ["old"])
            result = engine.review_snapshot("newer", "approve", reviewed_by="e@example.com")
            self.assertEqual((result["status"], result["reviewed_by"]), ("approved", "e@example.com"))
            self.assertEqual([snap.id for snap in engine.status().active_snapshots], ["newer"])
            with self.assertRaises(KeyError):
                engine.review_snapshot("missing", "reject", reviewed_by="e")
            with self.assertRaises(ValueError):
                engine.review_snapshot("newer", "maybe", reviewed_by="e")

    def test_rejecting_the_active_snapshot_falls_back_to_previous_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ImportControlEngine(data_dir=directory)
            self._insert(engine, "old", status="approved", active=0, rows=[("3915", "Plastik")])
            self._insert(engine, "newer", status="approved", active=1, rows=[("3915", "Plastik"), ("4707", "Kâğıt")])
            engine.review_snapshot("newer", "reject", reviewed_by="admin@example.com", note="yanlış ek")
            self.assertEqual([snap.id for snap in engine.status().active_snapshots], ["old"])
            with engine._connect() as db:
                row = db.execute("SELECT status, review_note FROM control_snapshots WHERE id='newer'").fetchone()
            self.assertEqual(tuple(row), ("rejected", "yanlış ek"))


class ClassificationReviewGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_mode_holds_second_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ClassificationEvidenceEngine(data_dir=directory)
            engine.ledger = ChangeLedger(directory)
            engine.review_policy = ReviewPolicy(mode="strict")
            try:
                # strict: even the first snapshot waits.
                with patch.object(engine, "_download", new=AsyncMock(return_value=b"%PDF-one")), patch.object(
                    engine, "_extract_pages", return_value=["Regulation (EU) No 679/72 CN code 6911 10 00."]
                ):
                    status = await engine.sync(force=True)
                self.assertFalse(status.ready)
                self.assertEqual(status.pending_review_count, 1)
                pending = engine.pending_reviews()[0]
                self.assertEqual(pending["kind"], "classification")
                engine.review_snapshot(pending["snapshot_id"], "approve", reviewed_by="e@example.com")
                self.assertTrue(engine.status().ready)
                with patch.object(engine, "_download", new=AsyncMock(return_value=b"%PDF-two")), patch.object(
                    engine, "_extract_pages", return_value=["Regulation (EU) 2021/1 CN code 6104 63 00."]
                ):
                    status = await engine.sync(force=True)
                self.assertEqual(status.active_sha256, engine.pending_reviews() and status.active_sha256)
                self.assertEqual(status.pending_review_count, 1)
                self.assertNotEqual(status.active_sha256, engine.pending_reviews()[0]["sha256"])
                batch = engine.ledger.batches(kind="classification")[0]
                self.assertEqual(batch["review_status"], "pending_review")
            finally:
                await engine.close()


class ReviewServiceTests(unittest.TestCase):
    def test_service_aggregates_queues_and_audits_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = ChangeLedger(directory)
            engine = ImportControlEngine(data_dir=directory)
            engine.ledger = ledger
            ControlReviewGateTests._insert(ControlReviewGateTests(), engine, "old", status="approved", active=1, rows=[("3915", "a")])
            ControlReviewGateTests._insert(ControlReviewGateTests(), engine, "newer", status="pending_review", active=0, rows=[("3915", "b")])
            ledger.record_batch(kind="controls", source_id="2026/3", new_snapshot_id="newer", old_snapshot_id="old", source_url="", sha256="x", changes=[], total_rows=1, review_status="pending_review")
            audits: list[tuple] = []
            service = ReviewService(policy=ReviewPolicy(mode="auto"), engines={"controls": engine}, ledger=ledger, audit=lambda *args: audits.append(args))
            overview = service.overview()
            self.assertEqual(overview["pending_count"], 1)
            self.assertEqual(overview["policy"]["mode"], "auto")
            result = service.review("controls", "newer", "reject", actor={"sub": "s", "email": "e@example.com"}, note="n")
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(ledger.batch(result["ledger_batch"])["review_status"], "rejected")
            self.assertEqual(audits[0][1:4], ("data_review", "controls", "newer"))
            self.assertEqual(service.pending_count(), 0)
            with self.assertRaises(KeyError):
                service.review("tariff", "x", "approve", actor={})
            with self.assertRaises(ValueError):
                service.review("controls", "old", "maybe", actor={})


class ReviewRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        from starlette.testclient import TestClient

        import app as web_app
        from account_service import AccountService
        from auth_service import GoogleAuthService

        self.web_app = web_app
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir)
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        with sqlite3.connect(self.accounts.db_path) as db:
            for sub, email in (("u", "u@example.com"), ("e", "e@example.com"), ("admin", "admin@example.com")):
                db.execute("INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)", (sub, email, sub, ""))
        self.accounts.admin_set_role({"sub": "admin", "email": "admin@example.com"}, "e", "editor")
        self.engine = ImportControlEngine(data_dir=data_dir)
        ControlReviewGateTests._insert(ControlReviewGateTests(), self.engine, "old", status="approved", active=1, rows=[("3915", "a")])
        ControlReviewGateTests._insert(ControlReviewGateTests(), self.engine, "newer", status="pending_review", active=0, rows=[("3915", "b")])
        self.service = ReviewService(policy=ReviewPolicy(mode="auto"), engines={"controls": self.engine}, audit=lambda *args: self.accounts.record_audit(*args))
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.review_service)
        web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.review_service = (
            self.auth, self.accounts, web_app.FixedWindowRateLimiter(), self.service,
        )
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")

    def tearDown(self) -> None:
        self.client.close()
        (self.web_app.google_auth, self.web_app.account_service, self.web_app.rate_limiter, self.web_app.review_service) = self.original
        self.temp.cleanup()

    def _headers(self, sub: str | None) -> dict[str, str]:
        headers = {"Origin": "https://gumruksor.com"}
        if sub:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session({'sub': sub, 'email': f'{sub}@example.com', 'name': sub})}"
        return headers

    def test_queue_requires_editor_and_decisions_are_audited(self) -> None:
        self.assertEqual(self.client.get("/api/admin/reviews", headers=self._headers(None)).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/reviews", headers=self._headers("u")).status_code, 403)
        queue = self.client.get("/api/admin/reviews", headers=self._headers("e"))
        self.assertEqual(queue.status_code, 200, queue.text)
        self.assertEqual(queue.json()["pending"][0]["snapshot_id"], "newer")
        self.assertEqual(queue.json()["policy"]["mode"], "auto")

        bad = self.client.post("/api/admin/reviews/controls/newer", headers=self._headers("e"), json={"action": "maybe"})
        self.assertEqual(bad.status_code, 422)
        missing = self.client.post("/api/admin/reviews/controls/nope", headers=self._headers("e"), json={"action": "approve"})
        self.assertEqual(missing.status_code, 404)
        unknown_kind = self.client.post("/api/admin/reviews/weird/newer", headers=self._headers("e"), json={"action": "approve"})
        self.assertEqual(unknown_kind.status_code, 422)
        denied = self.client.post("/api/admin/reviews/controls/newer", headers=self._headers("u"), json={"action": "approve"})
        self.assertEqual(denied.status_code, 403)

        approved = self.client.post("/api/admin/reviews/controls/newer", headers=self._headers("e"), json={"action": "approve", "note": "ok"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["result"]["status"], "approved")
        self.assertEqual(approved.json()["pending_count"], 0)
        self.assertEqual([snap.id for snap in self.engine.status().active_snapshots], ["newer"])
        with sqlite3.connect(self.accounts.db_path) as db:
            row = db.execute("SELECT action, target_type, target_id, details_json FROM audit_log WHERE action='data_review'").fetchone()
        self.assertEqual(row[:3], ("data_review", "controls", "newer"))
        self.assertEqual(json.loads(row[3])["action"], "approve")

    def test_health_reports_pending_reviews(self) -> None:
        with patch.object(self.web_app, "control_engine", self.engine):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_reviews"], 1)
        self.assertIn(response.json()["review_mode"], {"off", "auto", "strict"})


if __name__ == "__main__":
    unittest.main()
