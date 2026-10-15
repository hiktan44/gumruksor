"""Soru durumu (draft/approved/rejected), eski veritabanı migrasyonu ve uzman onay uçları."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from education import EducationError, EducationStore
from tests._review_http import ADMIN, CONSULTANT, USER, ReviewHttpCase

GOOD = {
    "branch": "kiymet", "text": "Gümrük kıymetinin tespitinde ilk uygulanacak yöntem hangisidir?",
    "options": ["Benzer eşya değeri", "İşlem değeri", "Hesaplanmış değer"], "answer_index": 1,
    "explanation": "İşlem değeri esastır.", "legal_basis": "Gümrük Kanunu md.23", "source_label": "mevzuat.gov.tr",
    "source_url": "https://www.mevzuat.gov.tr/x",
}


class StatusStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EducationStore(self.tmp.name)

    def test_draft_is_invisible_everywhere_for_students(self):
        draft = self.store.add_question({**GOOD, "text": GOOD["text"] + " (taslak)"}, status="draft", origin="script")
        approved = self.store.add_question(GOOD)
        self.assertEqual(self.store.counts()["kiymet"], 1)
        self.assertEqual([q["id"] for q in self.store.pick("kiymet", 10)], [approved])
        self.assertEqual([q["id"] for q in self.store.pick("", 10)], [approved])
        with self.assertRaises(EducationError):
            self.store.answer(draft, 1, "u1")  # kimlikle bile cevaplanamaz
        self.assertEqual(self.store.stats("u1")["attempts"], 0)
        exam = self.store.start_exam("u1", "kiymet", 10, 5)
        self.assertEqual([q["id"] for q in exam["questions"]], [approved])
        self.assertEqual(self.store.wrong_answers("u1"), [])

    def test_rejected_is_invisible_and_approval_publishes(self):
        draft = self.store.add_question(GOOD, status="draft")
        self.store.set_status(draft, "rejected", "c1", "kaynakla uyuşmuyor")
        self.assertEqual(self.store.counts()["kiymet"], 0)
        self.assertEqual(self.store.list_for_review("rejected")[0]["review_note"], "kaynakla uyuşmuyor")
        self.store.set_status(draft, "approved", "c1")
        self.assertEqual(self.store.counts()["kiymet"], 1)
        self.assertEqual(self.store.answer(draft, 1, "u1")["correct"], True)

    def test_reject_requires_reason_and_unknown_id_fails(self):
        draft = self.store.add_question(GOOD, status="draft")
        with self.assertRaises(EducationError):
            self.store.set_status(draft, "rejected", "c1", "")
        with self.assertRaises(EducationError):
            self.store.set_status("yok", "approved", "c1")
        with self.assertRaises(EducationError):
            self.store.set_status(draft, "draft", "c1")

    def test_review_list_reveals_key_and_precedent_notice(self):
        self.store.add_question({**GOOD, "source_kind": "karar", "source_ref": "Danıştay 7. Daire E.1 K.2", "evidence_quote": "x" * 50},
                                status="draft", origin="script")
        item = self.store.list_for_review("draft")[0]
        self.assertEqual((item["answer_index"], item["status"], item["origin"]), (1, "draft", "script"))
        self.assertIn("bağlayıcı olmayabilir", item["notice"])
        self.assertEqual(self.store.pending_counts()["draft"], 1)

    def test_exam_submission_survives_rejection_after_start(self):
        approved = self.store.add_question(GOOD)
        exam = self.store.start_exam("u1", "kiymet", 1, 5)
        self.store.set_status(approved, "rejected", "c1", "hatalı")
        result = self.store.submit_exam("u1", exam["exam_id"], {approved: 1})
        self.assertEqual((result["score"], result["total"]), (1, 1))

    def test_seed_questions_stay_approved(self):
        import json
        seed = Path(self.tmp.name) / "seed.json"
        seed.write_text(json.dumps({"questions": [{"id": "s1", **GOOD}]}), encoding="utf-8")
        store = EducationStore(self.tmp.name, seed_path=seed)
        self.assertEqual(store.counts()["kiymet"], 1)
        store.set_status("s1", "rejected", "c1", "gerekçe")
        EducationStore(self.tmp.name, seed_path=seed)  # yeniden eşitleme reddi geri almaz
        self.assertEqual(EducationStore(self.tmp.name).counts()["kiymet"], 0)


class MigrationTests(unittest.TestCase):
    def test_old_database_without_status_columns_keeps_questions_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "education.sqlite3")
            db.executescript("""
                CREATE TABLE questions (id TEXT PRIMARY KEY, branch TEXT NOT NULL, text TEXT NOT NULL, options_json TEXT NOT NULL,
                    answer_index INTEGER NOT NULL, explanation TEXT NOT NULL DEFAULT '', legal_basis TEXT NOT NULL,
                    source_label TEXT NOT NULL, source_url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
                INSERT INTO questions VALUES ('eski','tarife','Eski soru metni burada yazılıdır','["a","b"]',0,'','Dayanak','K','https://x.gov.tr',1,1);
            """)
            db.commit()
            db.close()
            store = EducationStore(tmp)
            self.assertEqual(store.counts()["tarife"], 1)
            self.assertEqual(store.list_for_review("approved")[0]["id"], "eski")
            self.assertEqual(store.answer("eski", 0, "u1")["correct"], True)
            columns = {row[1] for row in sqlite3.connect(Path(tmp) / "education.sqlite3").execute("PRAGMA table_info(questions)")}
            self.assertTrue({"status", "origin", "source_ref", "source_kind", "review_note"} <= columns)
            EducationStore(tmp)  # ikinci açılış migrasyonu tekrarlamaz


class ReviewHttpTests(ReviewHttpCase):
    def setUp(self):
        super().setUp()
        self.draft = self.education.add_question(GOOD, status="draft", origin="script")

    def test_only_admin_and_active_consultant_can_review(self):
        self.assertEqual(self.call("GET", "/api/education/review").status_code, 401)
        self.assertEqual(self.call("GET", "/api/education/review", USER).status_code, 403)
        for person in (CONSULTANT, ADMIN):
            response = self.call("GET", "/api/education/review", person)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"][0]["answer_index"], 1)

    def test_consultant_without_active_profile_is_a_plain_user(self):
        self.auth.upsert_user({"sub": "c2", "email": "baska@example.com"})
        self.assertEqual(self.call("GET", "/api/education/review", {"sub": "c2", "email": "baska@example.com"}).status_code, 403)

    def test_approve_and_reject_endpoints(self):
        self.assertEqual(self.call("POST", f"/api/education/review/{self.draft}/approve", USER, {}).status_code, 403)
        self.assertEqual(self.call("GET", "/api/education/practice?branch=kiymet&count=5").json()["questions"], [])
        self.assertEqual(self.call("POST", f"/api/education/review/{self.draft}/reject", CONSULTANT, {}).status_code, 422)
        self.assertEqual(self.call("POST", f"/api/education/review/{self.draft}/approve", CONSULTANT, {}).status_code, 200)
        practice = self.call("GET", "/api/education/practice?branch=kiymet&count=5").json()["questions"]
        self.assertEqual([q["id"] for q in practice], [self.draft])
        self.assertNotIn("answer_index", practice[0])
        self.assertEqual(self.call("POST", "/api/education/review/yok/approve", CONSULTANT, {}).status_code, 404)

    def test_draft_cannot_be_answered_over_http(self):
        response = self.call("POST", "/api/education/answer", USER, {"question_id": self.draft, "chosen": 1})
        self.assertEqual(response.status_code, 422)

    def test_status_exposes_review_flag_only_to_reviewers(self):
        self.assertTrue(self.call("GET", "/api/education/status", CONSULTANT).json()["can_review"])
        self.assertNotIn("can_review", self.call("GET", "/api/education/status", USER).json())
        self.assertEqual(self.call("GET", "/api/education/status", CONSULTANT).json()["review_counts"]["draft"], 1)
        self.assertEqual(self.call("GET", "/api/education/status", USER).json()["branches"]["kiymet"]["questions"], 0)

    def test_drafts_import_is_admin_only_and_never_writes_approved(self):
        payload = {"questions": [{**GOOD, "id": "g1", "text": "Yeni taslak sorusu için yeterince uzun bir metin?"}, {**GOOD, "id": "bad", "branch": "yok"}]}
        self.assertEqual(self.call("POST", "/api/education/drafts/import", CONSULTANT, payload).status_code, 403)
        response = self.call("POST", "/api/education/drafts/import", ADMIN, payload)
        self.assertEqual((response.status_code, response.json()["imported"], len(response.json()["errors"])), (201, 1, 1))
        self.assertEqual(self.education.list_for_review("draft", limit=10)[0]["origin"], "script")
        self.assertEqual(self.education.counts()["kiymet"], 0)
        again = self.call("POST", "/api/education/drafts/import", ADMIN, payload)
        self.assertEqual(again.json()["duplicates"], 1)


if __name__ == "__main__":
    unittest.main()
