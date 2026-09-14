"""Temporal validity (PRD Faz 1.4): date derivation, interval rule, as-of selection and the HTTP gate."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_engine import ImportControlEngine
from tariff_engine import TariffEngine, _ParsedArchive
from temporal import close_previous, covers, derive_validity, extract_effective_date, normalise_as_of, validity_basis


def _measure(gtip: str, rate: float, *, group: str = "8") -> dict:
    return {
        "gtip": gtip, "measure_type": "customs_duty", "rate": rate, "rate_text": str(rate), "country_group": group,
        "country_group_description": "Diğer Ülkeler", "footnote": None, "description": None, "condition": None,
        "list_name": "Liste II", "source_file": "ek.xlsx", "source_sheet": "84", "source_row": 3,
        "automatic_calculation_allowed": True,
    }


class DerivationTests(unittest.TestCase):
    def test_from_phrase_wins_and_gazette_reference_is_kept(self) -> None:
        text = (
            "İthalat Rejimi Kararı 31.12.2025 tarihli ve 33124 (mükerrer) sayılı Resmî Gazete'de yayımlanmıştır; "
            "ekli listeler 15.02.2026 tarihinden itibaren uygulanır. 30.12.2025 tarihli ve 10567 sayılı Cumhurbaşkanı Kararı"
        )
        validity = derive_validity([text], floor="2026-01-01", context="import_regime")
        self.assertEqual((validity.valid_from, validity.basis), ("2026-02-15", "document"))
        self.assertEqual((validity.gazette_date, validity.gazette_number), ("2025-12-31", "33124"))
        self.assertIn("10567 sayılı Cumhurbaşkanı Kararı", validity.legal_act or "")
        self.assertEqual(validity.warnings, ())

    def test_gazette_date_used_when_no_explicit_start(self) -> None:
        validity = derive_validity(["3/1/2026 tarihli ve 33127 sayılı Resmî Gazete"], floor="2026-01-01")
        self.assertEqual((validity.valid_from, validity.basis), ("2026-01-03", "gazette"))

    def test_dates_before_floor_fall_back_to_config_with_warning(self) -> None:
        validity = derive_validity(["01.01.2024 tarihinden itibaren"], floor="2026-01-01", context="x")
        self.assertEqual((validity.valid_from, validity.basis), ("2026-01-01", "config"))
        self.assertTrue(validity.warnings and "aralığın dışında" in validity.warnings[0])
        silent = derive_validity(["{}", ""], floor="2026-01-01")
        self.assertEqual((silent.valid_from, silent.basis, silent.warnings), ("2026-01-01", "config", ()))

    def test_effective_clause_extraction(self) -> None:
        self.assertEqual(
            extract_effective_date("Bu Tebliğ 1/1/2026 tarihinde yürürlüğe girer.", "31/12/2025", floor="2026-01-01")[::2],
            ("2026-01-01", "document"),
        )
        self.assertEqual(
            extract_effective_date("Bu Tebliğ yayımı tarihinde yürürlüğe girer.", "15/03/2026")[::2], ("2026-03-15", "gazette")
        )
        self.assertEqual(
            extract_effective_date("yayımını izleyen günden itibaren yürürlüğe girer", "2026-03-15")[0], "2026-03-16"
        )
        self.assertEqual(extract_effective_date("Yürütme maddesi", "2026-03-15"), (None, None, "config"))

    def test_interval_rule_and_basis_labels(self) -> None:
        self.assertEqual(close_previous("2026-02-01", "2026-01-01", "2026-02-03T10:00:00"), ("2026-02-01", "legal"))
        self.assertEqual(close_previous("2026-01-01", "2026-01-01", "2026-02-03T10:00:00"), ("2026-02-03", "observed"))
        row = {"valid_from": "2026-01-01", "valid_to": "2026-02-01", "valid_to_basis": "legal", "valid_from_basis": "document"}
        self.assertTrue(covers(row, "2026-01-15"))
        self.assertFalse(covers(row, "2026-02-01"))
        self.assertEqual(validity_basis(row, "2026-01-15"), "legal")
        row["valid_to_basis"] = "observed"
        self.assertEqual(validity_basis(row, "2026-01-15"), "observed")
        self.assertEqual(validity_basis(row, None), "current")
        self.assertEqual(validity_basis(None, "2026-01-15"), "unavailable")

    def test_as_of_validation(self) -> None:
        self.assertIsNone(normalise_as_of(""))
        self.assertEqual(normalise_as_of("2026-03-01"), "2026-03-01")
        for bad in ("01.03.2026", "2026-13-01", "1999-01-01", "2026-03-01T00:00:00"):
            with self.assertRaises(ValueError):
                normalise_as_of(bad)


class TariffTemporalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp.name)

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp.cleanup()

    async def _sync_with(self, measures: list[dict], archive: bytes, landing: str) -> None:
        source = next(item for item in self.engine.sources if item["id"] == "import_regime")
        parsed = _ParsedArchive(measures=measures, metadata={})
        self.engine._landing_text[source["id"]] = landing
        with patch.object(self.engine, "_discover_archive", new=AsyncMock(return_value="https://ticaret.gov.tr/a.zip")), patch.object(
            self.engine, "_download_archive", new=AsyncMock(return_value=archive)
        ), patch.object(self.engine, "_parse_archive", return_value=parsed):
            await self.engine._sync_source(source)

    def _rows(self) -> list[sqlite3.Row]:
        with self.engine._connect() as db:
            return db.execute("SELECT * FROM tariff_snapshots ORDER BY retrieved_at").fetchall()

    async def test_as_of_selects_the_version_in_force_on_that_day(self) -> None:
        year = date.today().year
        await self._sync_with([_measure("851712000000", 0.0)], b"one", f"01.01.{year} tarihinden itibaren")
        await self._sync_with([_measure("851712000000", 5.0)], b"two", f"01.03.{year} tarihinden itibaren")
        first, second = self._rows()
        self.assertEqual((first["valid_from"], first["valid_from_basis"]), (f"{year}-01-01", "document"))
        self.assertEqual((first["valid_to"], first["valid_to_basis"]), (f"{year}-03-01", "legal"))
        self.assertEqual((second["valid_from"], second["valid_to"], int(second["active"])), (f"{year}-03-01", None, 1))

        january = await self.engine.lookup("851712000000", origin_country="Çin", auto_sync=False, as_of=f"{year}-01-20")
        self.assertEqual([m.rate_text for m in january.alternatives], ["0.0"])
        self.assertEqual((january.validity_basis, january.as_of_date), ("legal", f"{year}-01-20"))
        self.assertEqual(january.snapshot_validity[0]["snapshot_id"], first["id"])
        self.assertTrue(any("yürürlükte olan" in w for w in january.warnings))

        current = await self.engine.lookup("851712000000", origin_country="Çin", auto_sync=False)
        self.assertEqual([m.rate_text for m in current.alternatives], ["5.0"])
        self.assertEqual(current.validity_basis, "current")

        before = await self.engine.lookup("851712000000", origin_country="Çin", auto_sync=False, as_of=f"{year - 1}-12-15")
        self.assertEqual((before.status, before.validity_basis), ("unavailable", "unavailable"))
        with self.assertRaises(ValueError):
            await self.engine.lookup("851712000000", auto_sync=False, as_of="15/01/2026")

        tree = await self.engine.decision_tree("8517", auto_sync=False, as_of=f"{year}-01-20")
        self.assertEqual(tree.validity_basis, "legal")

    async def test_same_start_date_yields_observed_boundary(self) -> None:
        year = date.today().year
        await self._sync_with([_measure("851712000000", 0.0)], b"one", "")
        await self._sync_with([_measure("851712000000", 7.0)], b"two", "")
        first, second = self._rows()
        self.assertEqual(first["valid_from"], second["valid_from"])
        self.assertEqual((first["valid_to"], first["valid_to_basis"]), (str(second["retrieved_at"])[:10], "observed"))
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        if yesterday >= first["valid_from"]:
            lookup = await self.engine.lookup("851712000000", origin_country="Çin", auto_sync=False, as_of=yesterday)
            self.assertEqual(lookup.validity_basis, "observed")
            self.assertTrue(any("indirme tarihlerinden" in w for w in lookup.warnings))
        self.assertEqual(self.engine.status().active_snapshots[0].valid_to, None)

    async def test_backfill_closes_open_intervals(self) -> None:
        with self.engine._connect() as db:
            for snapshot_id, retrieved, active in (("import_regime:aaa", "2026-01-05T00:00:00+00:00", 0), ("import_regime:bbb", "2026-02-05T00:00:00+00:00", 1)):
                db.execute(
                    "INSERT INTO tariff_snapshots (id,source_id,source_title,landing_url,archive_url,archive_sha256,retrieved_at,checked_at,valid_from,measure_count,active,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'{}')",
                    (snapshot_id, "import_regime", "İthalat Rejimi", "https://ticaret.gov.tr", "https://ticaret.gov.tr/a.zip", "c" * 64, retrieved, retrieved, "2026-01-01", 1, active),
                )
        self.assertEqual(self.engine.backfill_validity(), 1)
        self.assertEqual(self.engine.backfill_validity(), 0)
        older = self._rows()[0]
        self.assertEqual((older["valid_to"], older["valid_to_basis"]), ("2026-02-05", "observed"))


class ControlTemporalTests(unittest.TestCase):
    def _insert(self, engine: ImportControlEngine, snapshot_id: str, *, retrieved: str, valid_from: str, valid_to: str | None, active: int, rows: list[tuple[str, str]]) -> None:
        with engine._connect() as db:
            db.execute(
                """INSERT INTO control_snapshots (id, code, title, category, mevzuat_id, source_url,
                official_gazette_date, official_gazette_number, document_sha256, retrieved_at, valid_from,
                scope_count, authority, system, risk_based, physical_inspection_possible,
                laboratory_test_possible, required_documents_excerpt, active, status, valid_to, valid_to_basis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'approved',?,?)""",
                (snapshot_id, "2026/3", "Atık Tebliği", "atıklar", "3", "https://mevzuat.gov.tr/x", "2025-12-31", "33124",
                 f"sha-{snapshot_id}", retrieved, valid_from, len(rows), "Bakanlık", "Bakanlık", 0, 1, 0, None, active, valid_to,
                 "legal" if valid_to else None),
            )
            db.executemany(
                "INSERT INTO control_scope (snapshot_id, gtip_prefix, description, source_line, source_offset, excluded, list_kind) VALUES (?,?,?,?,?,?,?)",
                [(snapshot_id, prefix, description, "l", index, 0, "scope") for index, (prefix, description) in enumerate(rows, start=1)],
            )

    def test_lookup_honours_as_of_and_activation_closes_previous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = ImportControlEngine(data_dir=directory)
            year = date.today().year
            self._insert(engine, "old", retrieved=f"{year}-01-02T00:00:00+03:00", valid_from=f"{year}-01-01", valid_to=f"{year}-03-01", active=0, rows=[("3915", "Plastik döküntü")])
            self._insert(engine, "new", retrieved=f"{year}-03-02T00:00:00+03:00", valid_from=f"{year}-03-01", valid_to=None, active=1, rows=[("4707", "Kâğıt döküntü")])
            with patch.object(engine, "status") as status:
                status.return_value.ready = True
                loop = __import__("asyncio").new_event_loop()
                try:
                    past = loop.run_until_complete(engine.lookup("391510000000", as_of=f"{year}-01-15"))
                    current = loop.run_until_complete(engine.lookup("391510000000"))
                    none = loop.run_until_complete(engine.lookup("391510000000", as_of=f"{year - 1}-06-01"))
                finally:
                    loop.close()
            self.assertEqual((past.status, past.validity_basis, past.as_of_date), ("matched", "legal", f"{year}-01-15"))
            self.assertEqual(past.matches[0].rule.snapshot_id, "old")
            self.assertEqual((current.status, current.validity_basis), ("not_found", "current"))
            self.assertEqual((none.status, none.validity_basis), ("unavailable", "unavailable"))

            # Approving a third version closes the live one with the legal boundary.
            self._insert(engine, "newer", retrieved=f"{year}-04-02T00:00:00+03:00", valid_from=f"{year}-04-01", valid_to=None, active=0, rows=[("4707", "Kâğıt")])
            with engine._connect() as db:
                db.execute("UPDATE control_snapshots SET status='pending_review' WHERE id='newer'")
            engine.review_snapshot("newer", "approve", reviewed_by="e@example.com")
            with engine._connect() as db:
                closed = db.execute("SELECT valid_to, valid_to_basis, active FROM control_snapshots WHERE id='new'").fetchone()
            self.assertEqual(tuple(closed), (f"{year}-04-01", "legal", 0))
            self.assertEqual(engine.backfill_validity(), 0)


class TemporalRouteGateTests(unittest.TestCase):
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
            for sub, email in (("free", "free@example.com"), ("team", "team@example.com"), ("admin", "admin@example.com")):
                db.execute("INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)", (sub, email, sub, ""))
        self.accounts.admin_set_plan({"sub": "admin", "email": "admin@example.com"}, "team", "team", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.auth, self.accounts, web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")

    def tearDown(self) -> None:
        self.client.close()
        self.web_app.google_auth, self.web_app.account_service, self.web_app.rate_limiter = self.original
        self.temp.cleanup()

    def _post(self, path: str, body: dict, sub: str | None = None):
        headers = {"Origin": "https://gumruksor.com"}
        if sub:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session({'sub': sub, 'email': f'{sub}@example.com', 'name': sub})}"
        return self.client.post(path, json=body, headers=headers)

    def test_past_as_of_requires_temporal_feature_but_today_stays_open(self) -> None:
        lookup = AsyncMock(side_effect=RuntimeError("stub"))
        with patch.object(self.web_app.tariff_engine, "lookup", new=lookup):
            denied = self._post("/api/tariff/lookup", {"gtip": "851712000000", "as_of": "2026-01-15"}, "free")
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["feature"], "temporal_query")
            anonymous = self._post("/api/tariff/lookup", {"gtip": "851712000000", "as_of": "2026-01-15"})
            self.assertEqual(anonymous.status_code, 401)
            malformed = self._post("/api/tariff/lookup", {"gtip": "851712000000", "as_of": "15.01.2026"}, "team")
            self.assertEqual(malformed.status_code, 422)
            allowed = self._post("/api/tariff/lookup", {"gtip": "851712000000", "as_of": "2026-01-15"}, "team")
            self.assertEqual(allowed.status_code, 502)  # stub engine reached: gate passed
            self.assertEqual(lookup.await_args.kwargs["as_of"], "2026-01-15")
            today = self._post("/api/tariff/lookup", {"gtip": "851712000000", "as_of": date.today().isoformat()}, "free")
            self.assertEqual(today.status_code, 502)
        with patch.object(self.web_app.control_engine, "lookup", new=AsyncMock(side_effect=RuntimeError("stub"))) as control:
            self.assertEqual(self._post("/api/controls/lookup", {"gtip": "851712000000", "as_of": "2026-01-15"}, "free").status_code, 403)
            self.assertEqual(self._post("/api/controls/lookup", {"gtip": "851712000000", "as_of": "2026-01-15"}, "admin").status_code, 502)
            self.assertEqual(control.await_args.kwargs["as_of"], "2026-01-15")


if __name__ == "__main__":
    unittest.main()
