"""Eğitim soru bankası, süreli sınav, yanlışlarım ve mütalaa paketi (yalnız geçici veri)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService
from education import EducationError, EducationStore, validate_question

ORIGIN = "https://gumruksor.com"
SEED = Path(web_app.__file__).resolve().parent / "data" / "official" / "question_bank.json"
GOOD = {
    "branch": "kiymet", "text": "Sentetik test sorusu metni nedir?", "options": ["A", "B", "C"], "answer_index": 1,
    "explanation": "B doğrudur.", "legal_basis": "Test mevzuatı md.1", "source_label": "Test", "source_url": "https://example.gov.tr/x",
}


class ValidationTests(unittest.TestCase):
    def test_source_and_shape_are_mandatory(self):
        validate_question(GOOD)
        for patch_ in ({"source_url": "http://x"}, {"legal_basis": ""}, {"answer_index": 5}, {"options": ["A", "a"]},
                       {"branch": "yok"}, {"text": "kısa"}, {"answer_index": True}):
            with self.assertRaises(EducationError, msg=str(patch_)):
                validate_question({**GOOD, **patch_})

    def test_shipped_seed_is_valid_and_sourced(self):
        data = json.loads(SEED.read_text(encoding="utf-8"))["questions"]
        self.assertGreaterEqual(len(data), 9)
        for item in data:
            record = validate_question(item)
            self.assertTrue(record["source_url"].startswith("https://"))
        self.assertEqual(len({item["id"] for item in data}), len(data))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EducationStore(self.tmp.name)
        self.ids = [self.store.add_question({**GOOD, "text": f"Sentetik soru numarası {i} nedir?"}) for i in range(3)]

    def test_practice_never_leaks_answers(self):
        for question in self.store.pick("kiymet", 10):
            self.assertNotIn("answer_index", question)
            self.assertNotIn("explanation", question)

    def test_answer_reveals_and_records_only_for_signed_in(self):
        anonymous = self.store.answer(self.ids[0], 1, None)
        self.assertTrue(anonymous["correct"])
        self.assertFalse(anonymous["recorded"])
        self.assertEqual(self.store.stats("u1")["attempts"], 0)
        self.store.answer(self.ids[0], 0, "u1")
        self.assertEqual(self.store.stats("u1"), {"attempts": 1, "correct": 0, "exams": 0})
        with self.assertRaises(EducationError):
            self.store.answer(self.ids[0], 9, "u1")
        with self.assertRaises(EducationError):
            self.store.answer("yok", 0, "u1")

    def test_wrong_list_uses_latest_attempt(self):
        self.store.answer(self.ids[0], 0, "u1")
        self.assertEqual([q["id"] for q in self.store.wrong_answers("u1")], [self.ids[0]])
        time.sleep(0.01)
        self.store.answer(self.ids[0], 1, "u1")
        self.assertEqual(self.store.wrong_answers("u1"), [])
        self.assertEqual(self.store.wrong_answers("u2"), [])

    def test_exam_scoring_unanswered_and_double_submit(self):
        exam = self.store.start_exam("u1", "kiymet", 3, 10)
        answers = {exam["questions"][0]["id"]: 1, exam["questions"][1]["id"]: 0}
        result = self.store.submit_exam("u1", exam["exam_id"], answers)
        self.assertEqual((result["score"], result["total"], result["overtime"]), (1, 3, False))
        self.assertEqual(sum(1 for r in result["results"] if r["chosen"] is None), 1)
        with self.assertRaises(EducationError):
            self.store.submit_exam("u1", exam["exam_id"], answers)
        with self.assertRaises(EducationError):
            self.store.submit_exam("u2", exam["exam_id"], answers)

    def test_late_submission_is_flagged(self):
        exam = self.store.start_exam("u1", "kiymet", 1, 1)
        with patch("education.time.time", return_value=time.time() + 3600):
            result = self.store.submit_exam("u1", exam["exam_id"], {})
        self.assertTrue(result["overtime"])

    def test_empty_branch_cannot_start_exam(self):
        with self.assertRaises(EducationError):
            self.store.start_exam("u1", "mense", 5, 5)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=directory)
        self.accounts = AccountService(directory, admin_emails="admin@example.com")
        self.store = EducationStore(directory, seed_path=SEED)
        for name, value in (("google_auth", self.auth), ("account_service", self.accounts), ("education_store", self.store),
                            ("rate_limiter", web_app.FixedWindowRateLimiter())):
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

    def test_status_lists_branches_with_counts(self):
        data = self.call("GET", "/api/education/status").json()
        self.assertGreaterEqual(data["branches"]["tarife"]["questions"], 9)
        self.assertEqual(data["branches"]["kiymet"]["questions"], 0)

    def test_practice_has_no_answers_and_answer_reveals_basis(self):
        questions = self.call("GET", "/api/education/practice?branch=tarife&count=3").json()["questions"]
        self.assertEqual(len(questions), 3)
        self.assertTrue(all("answer_index" not in q for q in questions))
        result = self.call("POST", "/api/education/answer", body={"question_id": questions[0]["id"], "chosen": 0}).json()
        self.assertIn("legal_basis", result["question"])
        self.assertTrue(result["question"]["source_url"].startswith("https://"))

    def test_exam_requires_login_and_full_flow(self):
        self.assertEqual(self.call("POST", "/api/education/exams", body={"branch": "tarife"}).status_code, 401)
        user = {"sub": "u1", "email": "u@example.com"}
        exam = self.call("POST", "/api/education/exams", user, {"branch": "tarife", "count": 4, "minutes": 5}).json()
        answers = {q["id"]: 0 for q in exam["questions"]}
        done = self.call("POST", f"/api/education/exams/{exam['exam_id']}/submit", user, {"answers": answers}).json()
        self.assertEqual(done["total"], 4)
        self.assertIn("legal_basis", done["results"][0]["question"])
        wrong = self.call("GET", "/api/education/wrong", user).json()["questions"]
        self.assertEqual(len(wrong), 4 - done["score"])
        self.assertEqual(self.call("GET", "/api/education/wrong").status_code, 401)

    def test_only_admin_can_add_questions(self):
        user = {"sub": "u1", "email": "u@example.com"}
        admin = {"sub": "a1", "email": "admin@example.com"}
        self.assertEqual(self.call("POST", "/api/education/questions", user, GOOD).status_code, 403)
        self.assertEqual(self.call("POST", "/api/education/questions", admin, {**GOOD, "source_url": ""}).status_code, 422)
        self.assertEqual(self.call("POST", "/api/education/questions", admin, GOOD).status_code, 201)


class MutalaaPacketTests(unittest.TestCase):
    def test_packet_keeps_only_whitelisted_mutalaa_fields(self):
        packet = AccountService._consultation_packet({
            "inquiry": {"question": "x"}, "mutalaa": {"urgency": "ACİL", "gtip": "8517.13.00.00", "document_note": "Fatura ve katalog", "iban": "TR00"},
        })
        self.assertEqual(packet["mutalaa"], {"urgency": "acil", "gtip": "8517130000", "document_note": "Fatura ve katalog"})
        self.assertNotIn("iban", json.dumps(packet))

    def test_unknown_urgency_defaults_to_normal(self):
        packet = AccountService._consultation_packet({"mutalaa": {"urgency": "hemen!!", "gtip": "", "document_note": ""}})
        self.assertEqual(packet["mutalaa"]["urgency"], "normal")


if __name__ == "__main__":
    unittest.main()


class SeedSyncTests(unittest.TestCase):
    def _seed(self, directory, questions):
        path = Path(directory) / "seed.json"
        path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
        return path

    def test_seed_corrections_and_removals_reach_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = {**GOOD, "id": "s-1"}
            second = {**GOOD, "id": "s-2", "text": "İkinci sentetik soru metni nedir?"}
            store = EducationStore(tmp, seed_path=self._seed(tmp, [first, second]))
            admin_id = store.add_question({**GOOD, "text": "Yöneticinin eklediği sorunun metni nedir?"})
            self.assertEqual(store.counts()["kiymet"], 3)
            fixed = {**first, "answer_index": 2, "legal_basis": "Düzeltilmiş madde"}
            store = EducationStore(tmp, seed_path=self._seed(tmp, [fixed]))
            self.assertTrue(store.answer("s-1", 2, None)["correct"])
            self.assertEqual(store.answer("s-1", 2, None)["question"]["legal_basis"], "Düzeltilmiş madde")
            with self.assertRaises(EducationError):
                store.answer("s-2", 0, None)  # tohumdan kaldırılan soru pasif
            self.assertTrue(store.answer(admin_id, 1, None)["correct"])  # yönetici sorusu korunur
            self.assertEqual(store.counts()["kiymet"], 2)

    def test_removed_seed_question_returns_when_seed_restores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = {**GOOD, "id": "s-1"}
            EducationStore(tmp, seed_path=self._seed(tmp, [item]))
            EducationStore(tmp, seed_path=self._seed(tmp, []))
            store = EducationStore(tmp, seed_path=self._seed(tmp, [item]))
            self.assertEqual(store.counts()["kiymet"], 1)

    def test_old_database_without_origin_column_is_migrated(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / "education.sqlite3")
            db.execute("CREATE TABLE questions (id TEXT PRIMARY KEY, branch TEXT NOT NULL, text TEXT NOT NULL, options_json TEXT NOT NULL, "
                       "answer_index INTEGER NOT NULL, explanation TEXT NOT NULL DEFAULT '', legal_basis TEXT NOT NULL, source_label TEXT NOT NULL, "
                       "source_url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)")
            db.execute("INSERT INTO questions VALUES ('s-1','kiymet','Eski sentetik soru metni?','[\"A\",\"B\"]',0,'','Eski','E','https://e.gov.tr',1,0)")
            db.commit()
            db.close()
            store = EducationStore(tmp, seed_path=self._seed(tmp, [{**GOOD, "id": "s-1"}]))
            self.assertEqual(store.answer("s-1", 1, None)["question"]["legal_basis"], GOOD["legal_basis"])
