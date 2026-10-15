"""Risk kriterleri: CSV içe aktarma, taslak/onay, eskime uyarısı, emsal ayrımı, eski veritabanı migrasyonu."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from risk_engine import RiskError, RiskStore, assess_level, check, staleness
from risk_import import COLUMNS, import_csv, template_csv
from tests._review_http import ADMIN, CONSULTANT, USER, ReviewHttpCase

TODAY = date.today().strftime("%d.%m.%Y")
HEADER = ";".join(COLUMNS)
ROW = "8517;ithalat;2024;;Kıymet;Sentetik test kriteri;Açıklama;Test kaynağı;https://example.gov.tr/k;" + TODAY


def csv_of(*rows: str, header: str = HEADER) -> str:
    return "\r\n".join([header, *rows]) + "\r\n"


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RiskStore(self.tmp.name)

    def test_template_is_header_only_with_bom(self):
        text = template_csv()
        self.assertTrue(text.startswith("﻿"))
        self.assertEqual(text.strip().lstrip("﻿").split(";"), list(COLUMNS))

    def test_valid_rows_become_drafts_and_do_not_reach_check(self):
        result = import_csv(self.store, csv_of(ROW), created_by="a1")
        self.assertTrue(result["committed"])
        self.assertEqual((result["imported"], result["errors"]), (1, 0))
        self.assertEqual(self.store.match("851713", "import", 2025, None), [])
        outcome = check(self.store, gtip="8517130000", direction="import", year=2025, owner_sub=None,
                        trade=None, controls=None, origin_given=False)
        self.assertEqual(outcome["criteria"], [])
        draft = self.store.list_for_review("draft")[0]
        self.assertEqual((draft["status"], draft["category"], draft["official"]), ("draft", "kiymet", True))

    def test_one_bad_row_blocks_the_whole_file_with_line_numbers(self):
        bad = ROW.replace("https://example.gov.tr/k", "http://insecure.example")
        result = import_csv(self.store, csv_of(ROW, bad), created_by="a1")
        self.assertFalse(result["committed"])
        self.assertEqual([row["ok"] for row in result["rows"]], [True, False])
        self.assertEqual(result["rows"][1]["line"], 3)
        self.assertIn("https", result["rows"][1]["error"])
        self.assertEqual(self.store.list_for_review("draft"), [])

    def test_missing_source_and_missing_verification_date_are_rejected(self):
        no_source = ROW.replace("Test kaynağı", "")
        no_date = ROW.rsplit(";", 1)[0] + ";"
        future = ROW.rsplit(";", 1)[0] + ";" + (date.today() + timedelta(days=3)).strftime("%d.%m.%Y")
        result = import_csv(self.store, csv_of(no_source, no_date, future), created_by="a1", dry_run=True)
        self.assertEqual(result["errors"], 3)
        self.assertIn("doğrulama tarihi", result["rows"][1]["error"])
        self.assertIn("gelecekte", result["rows"][2]["error"])

    def test_bad_category_direction_prefix_and_year(self):
        cases = [ROW.replace("Kıymet", "uydurma"), ROW.replace("ithalat", "transit"), ROW.replace("8517", "8"), ROW.replace("2024", "1800")]
        result = import_csv(self.store, csv_of(*cases), created_by="a1", dry_run=True)
        self.assertEqual([row["ok"] for row in result["rows"]], [False] * 4)

    def test_dry_run_writes_nothing_and_reimport_skips_duplicates(self):
        self.assertFalse(import_csv(self.store, csv_of(ROW), created_by="a1", dry_run=True)["committed"])
        self.assertEqual(self.store.list_for_review("draft"), [])
        import_csv(self.store, csv_of(ROW), created_by="a1")
        again = import_csv(self.store, csv_of(ROW), created_by="a1")
        self.assertEqual((again["imported"], again["skipped_duplicates"]), (0, 1))
        self.assertEqual(len(self.store.list_for_review("draft")), 1)

    def test_duplicate_rows_inside_one_file_are_an_error(self):
        result = import_csv(self.store, csv_of(ROW, ROW), created_by="a1", dry_run=True)
        self.assertEqual(result["errors"], 1)

    def test_comma_delimiter_bom_and_blank_lines(self):
        text = "﻿" + ",".join(COLUMNS) + "\n" + ROW.replace(";", ",", 9).replace("Açıklama", "Açıklama") + "\n\n"
        result = import_csv(self.store, text, created_by="a1", dry_run=True)
        self.assertEqual((result["total"], result["errors"]), (1, 0))

    def test_header_problems(self):
        with self.assertRaises(RiskError):
            import_csv(self.store, csv_of(ROW, header="gtip_prefix;title"), created_by="a1")
        with self.assertRaises(RiskError):
            import_csv(self.store, "", created_by="a1")
        with self.assertRaises(RiskError):
            import_csv(self.store, HEADER + "\r\n", created_by="a1")


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RiskStore(self.tmp.name)
        import_csv(self.store, csv_of(ROW), created_by="a1")
        self.draft_id = self.store.list_for_review("draft")[0]["id"]

    def match(self):
        return self.store.match("851713", "import", 2025, None)

    def test_approve_publishes_and_reject_requires_reason(self):
        with self.assertRaises(RiskError):
            self.store.review(self.draft_id, approve=False, reviewer_sub="c1")
        approved = self.store.review(self.draft_id, approve=True, reviewer_sub="c1")
        self.assertEqual((approved["status"], len(self.match())), ("approved", 1))
        self.assertEqual(self.store.summary()["approved"], 1)

    def test_rejected_never_matches(self):
        self.store.review(self.draft_id, approve=False, reviewer_sub="c1", note="kaynak güncel değil")
        self.assertEqual(self.match(), [])
        self.assertEqual(self.store.list_for_review("rejected")[0]["review_note"], "kaynak güncel değil")

    def test_unknown_id_and_user_notes_are_outside_the_flow(self):
        note = self.store.add({"gtip_prefix": "8517", "category": "kiymet", "title": "Not"}, official=False, owner_sub="u1")
        with self.assertRaises(RiskError):
            self.store.review(note["id"], approve=True, reviewer_sub="c1", verified_at=TODAY)
        with self.assertRaises(RiskError):
            self.store.review("yok", approve=True, reviewer_sub="c1")
        self.assertNotIn(note["id"], [item["id"] for item in self.store.list_for_review("approved")])

    def test_refresh_updates_verification_date(self):
        self.store.review(self.draft_id, approve=True, reviewer_sub="c1", verified_at="01.02.2025")
        self.assertEqual(self.store.get(self.draft_id)["verified_at"], "2025-02-01")
        self.store.review(self.draft_id, approve=True, reviewer_sub="c1", verified_at=TODAY)
        self.assertEqual(self.store.get(self.draft_id)["verified_at"], date.today().isoformat())
        self.assertFalse(self.store.get(self.draft_id)["stale"])


class StalenessTests(unittest.TestCase):
    def test_twelve_month_rule(self):
        today = date(2026, 10, 15)
        self.assertFalse(staleness("2025-10-16", True, today=today)[0])
        self.assertTrue(staleness("2025-10-14", True, today=today)[0])
        self.assertTrue(staleness(None, True, today=today)[0])
        self.assertFalse(staleness(None, False, today=today)[0])

    def test_check_warns_about_stale_official_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RiskStore(tmp)
            old = (date.today() - timedelta(days=500)).isoformat()
            store.add({"gtip_prefix": "8517", "category": "kiymet", "title": "Eski", "source_label": "K",
                       "source_url": "https://example.gov.tr/x", "verified_at": old}, official=True, owner_sub=None)
            result = check(store, gtip="8517130000", direction="import", year=2026, owner_sub=None,
                           trade=None, controls=None, origin_given=False)
            self.assertTrue(result["criteria"][0]["stale"])
            self.assertTrue(any("12 aydan eski" in warning for warning in result["warnings"]))
            self.assertEqual(len(store.list_for_review("approved", stale_only=True)), 1)


class PrecedentTests(unittest.TestCase):
    PAYLOAD = {"gtip_prefix": "8517", "title": "Emsal karar", "source_label": "Danıştay 7. Daire", "source_url": "https://mevzuat.adalet.gov.tr/ictihat/1"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RiskStore(self.tmp.name)

    def test_draft_precedent_is_hidden_and_approved_one_is_labelled_and_never_raises_level(self):
        record = self.store.add_precedent(self.PAYLOAD, created_by="script")
        self.assertEqual((record["category"], record["official"], record["precedent"]), ("emsal", False, True))
        self.assertEqual(self.store.match("851713", "import", 2026, None), [])
        self.store.review(record["id"], approve=True, reviewer_sub="c1", verified_at=TODAY)
        matched = self.store.match("851713", "import", 2026, None)
        self.assertEqual(len(matched), 1)
        self.assertIn("resmî kriter değildir", matched[0]["origin_label"])
        self.assertEqual(assess_level([], matched), "info")
        outcome = check(self.store, gtip="8517130000", direction="import", year=2026, owner_sub=None,
                        trade=None, controls=None, origin_given=False)
        self.assertEqual(outcome["level"], "info")
        self.assertTrue(any("bağlayıcı olmayabilir" in w for w in outcome["warnings"]))

    def test_precedent_needs_source_and_cannot_use_official_categories(self):
        with self.assertRaises(RiskError):
            self.store.add_precedent({**self.PAYLOAD, "source_url": ""}, created_by="s")
        with self.assertRaises(RiskError):
            self.store.add_precedent({**self.PAYLOAD, "category": "kiymet"}, created_by="s")
        with self.assertRaises(RiskError):
            self.store.add({**self.PAYLOAD, "category": "emsal"}, official=True, owner_sub=None)


class MigrationTests(unittest.TestCase):
    def test_old_database_rows_stay_approved_official_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "risk_criteria.sqlite3")
            db.executescript("""
                CREATE TABLE risk_criteria (id TEXT PRIMARY KEY, gtip_prefix TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'both',
                    year_from INTEGER, year_to INTEGER, category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    source_label TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '', official INTEGER NOT NULL DEFAULT 0,
                    owner_sub TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                INSERT INTO risk_criteria VALUES ('eski','8517','both',NULL,NULL,'kiymet','Eski resmi','','K','https://example.gov.tr/x',1,NULL,1,1);
                INSERT INTO risk_criteria VALUES ('not','8517','both',NULL,NULL,'kiymet','Eski not','','','',0,'u1',1,1);
            """)
            db.commit()
            db.close()
            store = RiskStore(tmp)
            official = store.get("eski")
            self.assertEqual((official["status"], official["kind"]), ("approved", "criterion"))
            self.assertTrue(official["stale"])  # tarihsiz resmî kayıt uyarı taşır
            self.assertEqual([item["id"] for item in store.match("851713", "import", 2026, "u1")], ["eski", "not"])
            RiskStore(tmp)  # ikinci açılış migrasyonu tekrarlamaz


class RiskReviewHttpTests(ReviewHttpCase):
    def setUp(self):
        super().setUp()
        self.csv = csv_of(ROW)

    def test_template_and_import_are_admin_only(self):
        self.assertEqual(self.call("GET", "/api/risk/review/template").status_code, 401)
        self.assertEqual(self.call("GET", "/api/risk/review/template", CONSULTANT).status_code, 403)
        template = self.call("GET", "/api/risk/review/template", ADMIN)
        self.assertEqual(template.status_code, 200)
        self.assertIn("text/csv", template.headers["content-type"])
        self.assertEqual(self.call("POST", "/api/risk/review/import", CONSULTANT, {"csv": self.csv}).status_code, 403)
        self.assertEqual(self.call("POST", "/api/risk/review/import", USER, {"csv": self.csv}).status_code, 403)

    def test_admin_imports_consultant_approves_and_check_sees_it(self):
        body = {"gtip": "8517130000", "year": 2025}
        imported = self.call("POST", "/api/risk/review/import", ADMIN, {"csv": self.csv})
        self.assertEqual((imported.status_code, imported.json()["imported"]), (200, 1))
        self.assertEqual(self.call("POST", "/api/risk/check", None, body).json()["criteria"], [])
        listing = self.call("GET", "/api/risk/review?status=draft", CONSULTANT)
        self.assertEqual(listing.status_code, 200)
        record_id = listing.json()["items"][0]["id"]
        self.assertEqual(self.call("POST", f"/api/risk/review/{record_id}/approve", USER, {}).status_code, 403)
        self.assertEqual(self.call("POST", f"/api/risk/review/{record_id}/approve", None, {}).status_code, 401)
        self.assertEqual(self.call("POST", f"/api/risk/review/{record_id}/approve", CONSULTANT, {}).status_code, 200)
        criteria = self.call("POST", "/api/risk/check", None, body).json()["criteria"]
        self.assertEqual([item["id"] for item in criteria], [record_id])

    def test_bad_csv_returns_422_with_row_report(self):
        bad = self.call("POST", "/api/risk/review/import", ADMIN, {"csv": csv_of(ROW.replace("https://", "ftp://"))})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["rows"][0]["line"], 2)
        self.assertEqual(self.call("POST", "/api/risk/review/import", ADMIN, {"csv": "x"}).status_code, 422)

    def test_reject_needs_reason_and_official_api_add_creates_draft(self):
        official = {"gtip_prefix": "8517", "category": "tareks", "title": "T", "source_label": "K",
                    "source_url": "https://example.gov.tr/x", "official": True, "verified_at": TODAY}
        created = self.call("POST", "/api/risk/criteria", ADMIN, official)
        self.assertEqual((created.status_code, created.json()["status"]), (201, "draft"))
        record_id = created.json()["id"]
        self.assertEqual(self.call("POST", "/api/risk/check", None, {"gtip": "8517130000", "year": 2026}).json()["criteria"], [])
        self.assertEqual(self.call("POST", f"/api/risk/review/{record_id}/reject", CONSULTANT, {}).status_code, 422)
        self.assertEqual(self.call("POST", f"/api/risk/review/{record_id}/reject", CONSULTANT, {"note": "güncel değil"}).status_code, 200)
        self.assertEqual(self.call("POST", "/api/risk/review/yok/approve", CONSULTANT, {}).status_code, 404)

    def test_auth_me_exposes_can_review(self):
        self.assertTrue(self.call("GET", "/api/auth/me", CONSULTANT).json()["user"]["can_review"])
        self.assertTrue(self.call("GET", "/api/auth/me", ADMIN).json()["user"]["can_review"])
        self.assertFalse(self.call("GET", "/api/auth/me", USER).json()["user"]["can_review"])


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RiskStore(self.tmp.name)

    def test_same_criterion_for_different_years_is_not_a_duplicate(self):
        first = "8517;ithalat;2022;2023;Kıymet;Aynı başlık;Açıklama;Test kaynağı;https://example.gov.tr/k;" + TODAY
        second = "8517;ithalat;2024;2025;Kıymet;Aynı başlık;Açıklama;Test kaynağı;https://example.gov.tr/k;" + TODAY
        result = import_csv(self.store, csv_of(first, second), created_by="a1")
        self.assertTrue(result["committed"], result)
        self.assertEqual(result["imported"], 2)
        # Aynı dosya tekrar yüklenirse yine çoğalmaz.
        again = import_csv(self.store, csv_of(first, second), created_by="a1")
        self.assertEqual(len(self.store.list_for_review("draft")), 2, again)

    def test_stale_filter_runs_before_the_limit(self):
        old = (date.today() - timedelta(days=800)).isoformat()
        base = {"gtip_prefix": "8517", "category": "kiymet", "source_label": "K", "source_url": "https://example.gov.tr/k", "direction": "import"}
        for index in range(205):
            record = self.store.add({**base, "title": f"Güncel kriter {index:03d}"}, official=True, owner_sub=None, status="draft", created_by="a1")
            self.store.review(record["id"], approve=True, reviewer_sub="a1", verified_at=TODAY)
        stale = self.store.add({**base, "title": "Eski kriter"}, official=True, owner_sub=None, status="draft", created_by="a1")
        self.store.review(stale["id"], approve=True, reviewer_sub="a1", verified_at=old)
        with sqlite3.connect(self.store.path) as db:  # eski kayıt en eski oluşturulmuş sayılsın
            db.execute("UPDATE risk_criteria SET created_at=1 WHERE id=?", (stale["id"],))
        listed = self.store.list_for_review("approved", stale_only=True, limit=200)
        self.assertEqual([item["title"] for item in listed], ["Eski kriter"])
        self.assertEqual(self.store.summary()["stale"], 1)
