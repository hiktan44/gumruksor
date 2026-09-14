"""Unified change ledger: persistence, engine hooks, lineage and the HTTP surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trade_measures as tm
from change_ledger import ChangeLedger, batch_id_for, diff_rows
from classification_evidence import ClassificationEvidenceEngine
from control_engine import ImportControlEngine
from tariff_engine import TariffEngine, _ParsedArchive


def _measure(gtip: str, rate: float, *, group: str = "8", footnote: str | None = None) -> dict:
    return {
        "gtip": gtip, "measure_type": "customs_duty", "rate": rate, "rate_text": str(rate), "country_group": group,
        "country_group_description": "Diğer Ülkeler", "footnote": footnote, "description": None, "condition": None,
        "list_name": "Liste II", "source_file": "ek.xlsx", "source_sheet": "84", "source_row": 3,
        "automatic_calculation_allowed": True,
    }


class LedgerCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = ChangeLedger(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_diff_rows_reports_added_removed_and_modified(self) -> None:
        previous = {"a": {"gtip": "8471", "rate_text": "0"}, "b": {"gtip": "8517", "rate_text": "5"}}
        current = {"b": {"gtip": "8517", "rate_text": "6"}, "c": {"gtip": "9018", "rate_text": "1"}}
        changes = diff_rows(current, previous, fields=("rate_text",))
        self.assertEqual([(c["entity_key"], c["change_type"]) for c in changes], [("a", "removed"), ("b", "modified"), ("c", "added")])
        self.assertEqual(changes[1]["before"]["rate_text"], "5")
        self.assertEqual(changes[1]["gtip"], "8517")

    def test_record_batch_is_idempotent_and_queryable(self) -> None:
        rows = [
            {"entity_key": "851712|customs_duty|8", "gtip": "851712000000", "change_type": "modified", "before": {"rate_text": "0"}, "after": {"rate_text": "2"}},
            {"entity_key": "847130|customs_duty|8", "gtip": "847130000000", "change_type": "added", "before": None, "after": {"rate_text": "1"}},
        ]
        batch_id = self.ledger.record_batch(
            kind="tariff", source_id="import_regime", title="İthalat Rejimi", new_snapshot_id="import_regime:new",
            old_snapshot_id="import_regime:old", source_url="https://ticaret.gov.tr/x.zip", sha256="f" * 64,
            changes=rows, total_rows=2, parse_warnings=["satır sayısı düştü"],
        )
        self.assertEqual(batch_id, batch_id_for("tariff", "import_regime", "import_regime:new"))
        again = self.ledger.record_batch(
            kind="tariff", source_id="import_regime", new_snapshot_id="import_regime:new", old_snapshot_id=None,
            source_url="", sha256="", changes=[], total_rows=0,
        )
        self.assertEqual(again, batch_id)
        batch = self.ledger.latest_batch("tariff", "import_regime")
        self.assertEqual((batch["added"], batch["removed"], batch["modified"]), (1, 0, 1))
        self.assertEqual(batch["parse_warnings"], ["satır sayısı düştü"])
        self.assertEqual(batch["label"], "Tarife cetveli")
        self.assertEqual(len(self.ledger.changes(kind="tariff")), 2)
        by_prefix = self.ledger.changes(gtip_prefix="8517")
        self.assertEqual([c["gtip"] for c in by_prefix], ["851712000000"])
        self.assertEqual(by_prefix[0]["before"], {"rate_text": "0"})
        # A watched 12-digit code also matches the shorter official row that contains it.
        self.assertEqual(len(self.ledger.changes(gtip_prefix="847130000000")), 1)
        self.assertEqual(self.ledger.summary()["kinds"]["tariff"]["batches"], 1)
        with self.assertRaises(ValueError):
            self.ledger.record_batch(kind="weird", source_id="x", new_snapshot_id="1", old_snapshot_id=None, source_url="", sha256="", changes=[], total_rows=0)


class TariffLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp.name)
        self.engine.ledger = ChangeLedger(self.temp.name)

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp.cleanup()

    async def _sync_with(self, measures: list[dict], archive: bytes) -> None:
        source = next(item for item in self.engine.sources if item["id"] == "import_regime")
        parsed = _ParsedArchive(measures=measures, metadata={})
        with patch.object(self.engine, "_discover_archive", new=AsyncMock(return_value="https://ticaret.gov.tr/a.zip")), patch.object(
            self.engine, "_download_archive", new=AsyncMock(return_value=archive)
        ), patch.object(self.engine, "_parse_archive", return_value=parsed):
            await self.engine._sync_source(source)

    async def test_sync_writes_row_level_batches_and_changes_reads_the_ledger(self) -> None:
        first = [_measure("851712000000", 0.0), _measure("847130000000", 0.0), _measure("940320000000", 3.0)]
        await self._sync_with(first, b"archive-one")
        second = [_measure("851712000000", 2.0, footnote="(1)"), _measure("847130000000", 0.0)]
        await self._sync_with(second, b"archive-two")

        batches = self.engine.ledger.batches(kind="tariff", source_id="import_regime")
        self.assertEqual(len(batches), 2)
        latest = batches[0]
        self.assertEqual(latest["old_snapshot_id"], batches[1]["new_snapshot_id"])
        self.assertEqual(latest["sha256"], hashlib.sha256(b"archive-two").hexdigest())
        self.assertEqual((latest["added"], latest["removed"], latest["modified"]), (0, 1, 1))
        self.assertTrue(any("%20" in warning for warning in latest["parse_warnings"]))

        report = self.engine.changes("import_regime")
        self.assertEqual(report["status"], "compared")
        self.assertEqual(report["ledger_batch"], latest["id"])
        self.assertEqual(report["total_changes"], 2)
        modified = next(item for item in report["changes"] if item["gtip"] == "851712000000")
        self.assertEqual((modified["before"], modified["after"], modified["after_footnote"]), ("0.0", "2.0", "(1)"))
        removed = next(item for item in report["changes"] if item["gtip"] == "940320000000")
        self.assertEqual((removed["before"], removed["after"]), ("3.0", None))
        # Watched-code helper shape used by the e-mail notifier stays intact.
        self.assertEqual(set(modified) >= {"gtip", "measure_type", "country_group", "before", "after"}, True)

    async def test_backfill_records_history_without_duplicates(self) -> None:
        with self.engine._connect() as db:
            for snapshot_id, retrieved in (("import_regime:aaa", "2026-01-05T00:00:00+00:00"), ("import_regime:bbb", "2026-02-05T00:00:00+00:00")):
                db.execute(
                    "INSERT INTO tariff_snapshots (id,source_id,source_title,landing_url,archive_url,archive_sha256,retrieved_at,checked_at,valid_from,measure_count,active,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'{}')",
                    (snapshot_id, "import_regime", "İthalat Rejimi", "https://ticaret.gov.tr", "https://ticaret.gov.tr/a.zip", "c" * 64, retrieved, retrieved, "2026-01-01", 1, int(snapshot_id.endswith("bbb"))),
                )
                db.execute(
                    "INSERT INTO tariff_measures (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,country_group_description,footnote,description,condition_text,list_name,source_file,source_sheet,source_row,automatic_calculation_allowed) "
                    "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,'L','f','s',1,1)",
                    (f"m-{snapshot_id}", snapshot_id, "851712000000", "customs_duty", 1.0 if snapshot_id.endswith("aaa") else 4.0, "1" if snapshot_id.endswith("aaa") else "4", "8", "Diğer Ülkeler"),
                )
        self.assertEqual(self.engine.backfill_ledger(), 2)
        self.assertEqual(self.engine.backfill_ledger(), 0)
        latest = self.engine.ledger.latest_batch("tariff", "import_regime")
        self.assertTrue(latest["backfilled"])
        self.assertEqual(latest["modified"], 1)
        self.assertEqual(latest["detected_at"], "2026-02-05T00:00:00+00:00")


class ControlLedgerTests(unittest.TestCase):
    def test_backfill_diffs_scope_rows_between_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ImportControlEngine(data_dir=directory)
            engine.ledger = ChangeLedger(directory)
            with engine._connect() as db:
                for snapshot_id, retrieved, active in (("old", "2026-01-01T00:00:00+03:00", 0), ("new", "2026-03-01T00:00:00+03:00", 1)):
                    db.execute(
                        """INSERT INTO control_snapshots (id, code, title, category, mevzuat_id, source_url,
                        official_gazette_date, official_gazette_number, document_sha256, retrieved_at, valid_from,
                        scope_count, authority, system, risk_based, physical_inspection_possible,
                        laboratory_test_possible, required_documents_excerpt, active)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (snapshot_id, "2026/3", "Atık Tebliği", "atıklar", "3", "https://mevzuat.gov.tr/x", "2025-12-31", "33124",
                         f"sha-{snapshot_id}", retrieved, "2026-01-01", 2, "Bakanlık", "Bakanlık", 0, 1, 0, None, active),
                    )
                db.executemany(
                    "INSERT INTO control_scope (snapshot_id, gtip_prefix, description, source_line, source_offset, excluded, list_kind) VALUES (?,?,?,?,?,?,?)",
                    [
                        ("old", "3915", "Plastik döküntü", "l", 1, 0, "scope"),
                        ("old", "271099", "Atık yağ", "l", 2, 0, "prohibited"),
                        ("new", "3915", "Plastik döküntü ve hurda", "l", 1, 0, "scope"),
                        ("new", "4707", "Kâğıt döküntü", "l", 2, 0, "scope"),
                    ],
                )
            self.assertEqual(engine.backfill_ledger(), 2)
            batch = engine.ledger.latest_batch("controls", "2026/3")
            self.assertEqual((batch["added"], batch["removed"], batch["modified"]), (1, 1, 1))
            self.assertEqual(batch["gazette_number"], "33124")
            self.assertEqual(batch["sha256"], "sha-new")
            removed = [c for c in engine.ledger.changes(kind="controls", batch_id=batch["id"]) if c["change_type"] == "removed"]
            self.assertEqual(removed[0]["gtip"], "271099")
            self.assertEqual(removed[0]["before"]["list_kind"], "prohibited")
            asyncio.run(engine.close())


class ClassificationLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_sync_records_changed_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ClassificationEvidenceEngine(data_dir=directory)
            engine.ledger = ChangeLedger(directory)
            try:
                for payload, pages in (
                    (b"%PDF-one", ["Regulation (EU) No 679/72 CN code 6911 10 00.", "Regulation (EU) 2020/1577 CN code 6104 63 00."]),
                    (b"%PDF-two", ["Regulation (EU) No 679/72 CN code 6911 10 00.", "Regulation (EU) 2021/1 CN code 6104 63 00 amended."]),
                ):
                    with patch.object(engine, "_download", new=AsyncMock(return_value=payload)), patch.object(engine, "_extract_pages", return_value=pages):
                        status = await engine.sync(force=True)
                    self.assertTrue(status.ready)
                batches = engine.ledger.batches(kind="classification")
                self.assertEqual(len(batches), 2)
                self.assertEqual((batches[0]["added"], batches[0]["removed"], batches[0]["modified"]), (0, 0, 1))
                self.assertEqual(batches[0]["sha256"], hashlib.sha256(b"%PDF-two").hexdigest())
                change = engine.ledger.changes(kind="classification", batch_id=batches[0]["id"])[0]
                self.assertEqual(change["entity_key"], "p2")
                self.assertEqual(engine.backfill_ledger(), 0)
            finally:
                await engine.close()


class TradeMeasureLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        seed = Path(self.temp.name) / "seed"
        seed.mkdir()
        (seed / "safeguard_measures.json").write_text(json.dumps([
            {"seq": 1, "file_no": "250", "product": "Fırça", "gtip": "9603.21.00.00.19", "country": "Tüm Ülkeler", "stage": "İLK",
             "original_start": "20.09.2023", "expires": "2026-09-20", "amounts": ["% 12"], "acts": [{"kind": "Karar", "number": "1", "rg_date": "20/09/2023", "rg_no": "32315"}]},
        ]), encoding="utf-8")
        for name in ("antidumping_measures.json", "surveillance_measures.json", "agricultural_quotas.json"):
            (seed / name).write_text("[]" if name != "antidumping_measures.json" else '{"definitive": [], "provisional": []}', encoding="utf-8")
        self.engine = tm.TradeMeasureEngine(self.temp.name)
        self.engine.store = tm.TradeMeasureStore(self.temp.name, seed_dir=seed)
        self.engine.store.ledger = ChangeLedger(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_save_keeps_row_lineage_and_hits_carry_provenance(self) -> None:
        store = self.engine.store
        self.assertGreater(store.ensure_measure_rows(), 0)
        seeded = self.engine.lookup("960321000019", None).safeguard[0]
        self.assertEqual(seeded.provenance["valid_from"], "2023-09-20")
        self.assertEqual(seeded.provenance["valid_to"], "2026-09-20")
        self.assertIsNone(seeded.provenance["removed_at"])

        updated = json.loads(json.dumps(store.load("safeguard")))
        updated[0]["expires"] = "2029-09-20"
        updated.append({"seq": 2, "file_no": "260", "product": "Etil asetat", "gtip": "2915.31.00.00.00", "country": "Tüm Ülkeler", "stage": "İLK", "original_start": "", "expires": "2028-06-22", "amounts": ["% 10"], "acts": []})
        diff = store.save("safeguard", updated, source_url="https://ticaret.gov.tr/k.xlsx", source_label="test", sha256="e" * 64)
        self.assertEqual((diff["added_count"], diff["modified_count"]), (1, 1))
        self.assertEqual(store.metadata("safeguard")["sha256"], "e" * 64)
        hit = self.engine.lookup("960321000019", None).safeguard[0]
        self.assertEqual(hit.provenance["source_sha256"], "e" * 64)
        self.assertEqual(hit.provenance["valid_to"], "2029-09-20")
        self.assertIn("provenance", hit.as_dict())

        removed_all = [row for row in updated if row["file_no"] == "260"]
        store.save("safeguard", removed_all, source_url="https://ticaret.gov.tr/k2.xlsx", source_label="test")
        with sqlite3.connect(store.db_path) as db:
            removed_at = db.execute("SELECT removed_at FROM measure_rows WHERE kind='safeguard' AND row_key LIKE '250|%'").fetchone()[0]
        self.assertIsNotNone(removed_at)

        batches = store.ledger.batches(kind="trade_measures", source_id="safeguard")
        self.assertEqual(len(batches), 2)
        self.assertEqual((batches[1]["added"], batches[1]["modified"]), (1, 1))
        self.assertEqual(batches[0]["removed"], 1)
        by_code = store.ledger.changes(gtip_prefix="960321")
        self.assertEqual({c["change_type"] for c in by_code}, {"modified", "removed"})

    def test_legacy_changes_are_backfilled_once(self) -> None:
        store = self.engine.store
        store.ledger = None
        updated = json.loads(json.dumps(store.load("safeguard")))
        updated[0]["expires"] = "2030-01-01"
        store.save("safeguard", updated, source_url="https://ticaret.gov.tr/k.xlsx", source_label="test")
        store.ledger = ChangeLedger(self.temp.name)
        self.assertEqual(store.backfill_ledger(), 1)
        self.assertEqual(store.backfill_ledger(), 0)
        batch = store.ledger.batches(kind="trade_measures")[0]
        self.assertTrue(batch["backfilled"])
        self.assertEqual(batch["modified"], 1)


class ChangeRoutesTests(unittest.TestCase):
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
        self.ledger = ChangeLedger(data_dir)
        self.ledger.record_batch(
            kind="tariff", source_id="import_regime", title="İthalat Rejimi", new_snapshot_id="new", old_snapshot_id="old",
            source_url="https://ticaret.gov.tr/a.zip", sha256="a" * 64, total_rows=1, parse_warnings=["uyarı"],
            changes=[{"entity_key": "851712|customs_duty|8", "gtip": "851712000000", "change_type": "modified", "before": {"rate_text": "0"}, "after": {"rate_text": "2"}}],
        )
        with sqlite3.connect(self.accounts.db_path) as db:
            for sub, email in (("u", "u@example.com"), ("e", "e@example.com"), ("admin", "admin@example.com")):
                db.execute("INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)", (sub, email, sub, ""))
        self.accounts.admin_set_role({"sub": "admin", "email": "admin@example.com"}, "e", "editor")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.change_ledger)
        web_app.google_auth, web_app.account_service, web_app.rate_limiter, web_app.change_ledger = (
            self.auth, self.accounts, web_app.FixedWindowRateLimiter(), self.ledger,
        )
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")

    def tearDown(self) -> None:
        self.client.close()
        (self.web_app.google_auth, self.web_app.account_service, self.web_app.rate_limiter, self.web_app.change_ledger) = self.original
        self.temp.cleanup()

    def _get(self, path: str, sub: str | None = None):
        headers = {"Origin": "https://gumruksor.com"}
        if sub:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session({'sub': sub, 'email': f'{sub}@example.com', 'name': sub})}"
        return self.client.get(path, headers=headers)

    def test_public_changes_expose_ledger_with_filters(self) -> None:
        response = self._get("/api/changes?gtip=8517&kind=tariff")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["ledger"][0]["gtip"], "851712000000")
        self.assertEqual(payload["batches"][0]["source_id"], "import_regime")
        self.assertEqual(payload["ledger_summary"]["kinds"]["tariff"]["batches"], 1)
        self.assertEqual(self._get("/api/changes?gtip=9999").json()["ledger"], [])
        self.assertEqual(self._get("/api/changes?kind=nope").status_code, 422)

    def test_admin_changes_require_editor_or_admin(self) -> None:
        self.assertEqual(self._get("/api/admin/changes").status_code, 403)
        self.assertEqual(self._get("/api/admin/changes", "u").status_code, 403)
        as_editor = self._get("/api/admin/changes", "e")
        self.assertEqual(as_editor.status_code, 200, as_editor.text)
        batch = as_editor.json()["batches"][0]
        self.assertEqual(batch["parse_warnings"], ["uyarı"])
        detail = self._get(f"/api/admin/changes?batch={batch['id']}", "admin").json()
        self.assertEqual(detail["changes"][0]["after"], {"rate_text": "2"})
        self.assertEqual(self._get("/api/admin/changes?batch=missing", "admin").status_code, 404)


if __name__ == "__main__":
    unittest.main()
