"""Eğitim: kaynaklı soru bankası, alıştırma, süreli deneme sınavı ve "yanlışlarım".

Kurallar:

* Her sorunun mevzuat/kaynak dayanağı zorunludur (``legal_basis`` ve https ``source_url``).
  Dayanaksız soru eklenemez; tohum dosyası bunu yükleme anında doğrular.
* Cevap anahtarı hiçbir listeleme/deneme yanıtında görünmez; yalnız cevaplandıktan veya sınav
  teslim edildikten sonra açıklamayla birlikte döner.
* Sınav süresi sunucuda tutulur (istemci saatine güvenilmez); süre aşımı sonuca "süre aşıldı"
  olarak yazılır, yanıtlar yine de not alınır ama sonuç etiketlenir.
* "Yanlışlarım": kullanıcının bir sorudaki **son** denemesi yanlışsa listelenir; sonra doğru
  cevaplanırsa listeden düşer.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

BRANCHES = {
    "tarife": "Tarife",
    "kiymet": "Kıymet",
    "mense": "Menşe",
    "rejimler": "Rejimler",
    "mevzuat": "Mevzuat",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, branch TEXT NOT NULL, text TEXT NOT NULL, options_json TEXT NOT NULL,
    answer_index INTEGER NOT NULL, explanation TEXT NOT NULL DEFAULT '', legal_basis TEXT NOT NULL,
    source_label TEXT NOT NULL, source_url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_branch ON questions(branch, active);
CREATE TABLE IF NOT EXISTS exams (
    id TEXT PRIMARY KEY, user_sub TEXT NOT NULL, branch TEXT NOT NULL, question_ids_json TEXT NOT NULL,
    started_at REAL NOT NULL, deadline REAL NOT NULL, submitted_at REAL, score INTEGER, overtime INTEGER
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, user_sub TEXT NOT NULL, question_id TEXT NOT NULL, chosen INTEGER NOT NULL,
    correct INTEGER NOT NULL, exam_id TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user_question ON attempts(user_sub, question_id, created_at);
"""
GRACE_SECONDS = 20


class EducationError(ValueError):
    """Geçersiz soru, sınav veya cevap."""


def validate_question(payload: dict[str, Any]) -> dict[str, Any]:
    branch = str(payload.get("branch", "")).strip()
    if branch not in BRANCHES:
        raise EducationError("Branş geçersiz: " + ", ".join(BRANCHES))
    text = str(payload.get("text", "")).strip()
    if not (10 <= len(text) <= 600):
        raise EducationError("Soru metni 10-600 karakter olmalıdır.")
    options = payload.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 6 or not all(isinstance(o, str) and o.strip() for o in options):
        raise EducationError("2-6 dolu seçenek gerekir.")
    options = [o.strip()[:300] for o in options]
    if len({o.casefold() for o in options}) != len(options):
        raise EducationError("Seçenekler birbirinden farklı olmalıdır.")
    answer = payload.get("answer_index")
    if not isinstance(answer, int) or isinstance(answer, bool) or not 0 <= answer < len(options):
        raise EducationError("Doğru seçenek numarası geçersiz.")
    legal_basis = str(payload.get("legal_basis", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    source_label = str(payload.get("source_label", "")).strip()
    if not legal_basis or not source_label or not re.match(r"^https://", source_url):
        raise EducationError("Her soru için mevzuat dayanağı, kaynak adı ve https kaynak bağlantısı zorunludur.")
    return {
        "branch": branch, "text": text, "options": options, "answer_index": answer,
        "explanation": str(payload.get("explanation", "")).strip()[:1000], "legal_basis": legal_basis[:400],
        "source_label": source_label[:200], "source_url": source_url,
    }


class EducationStore:
    def __init__(self, data_dir: str | Path, seed_path: str | Path | None = None) -> None:
        self.path = Path(data_dir) / "education.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(questions)").fetchall()}
            if "origin" not in columns:
                db.execute("ALTER TABLE questions ADD COLUMN origin TEXT NOT NULL DEFAULT 'admin'")
        if seed_path and Path(seed_path).exists():
            self._sync_seed(json.loads(Path(seed_path).read_text(encoding="utf-8")).get("questions", []))

    def _sync_seed(self, items: list[dict[str, Any]]) -> None:
        """Tohum dosyası kalıcı veritabanını yönetir: düzeltmeler uygulanır, kaldırılan sorular pasifleşir.

        Yönetici eklediği sorulara (``origin='admin'``) dokunulmaz.
        """
        seen = []
        for item in items:
            record = validate_question(item)
            identifier = str(item["id"])
            seen.append(identifier)
            with self._connect() as db:
                db.execute(
                    "INSERT INTO questions (id,branch,text,options_json,answer_index,explanation,legal_basis,source_label,source_url,"
                    "active,origin,created_at) VALUES (?,?,?,?,?,?,?,?,?,1,'seed',?) "
                    "ON CONFLICT(id) DO UPDATE SET branch=excluded.branch, text=excluded.text, options_json=excluded.options_json, "
                    "answer_index=excluded.answer_index, explanation=excluded.explanation, legal_basis=excluded.legal_basis, "
                    "source_label=excluded.source_label, source_url=excluded.source_url, active=1, origin='seed'",
                    (identifier, record["branch"], record["text"], json.dumps(record["options"], ensure_ascii=False),
                     record["answer_index"], record["explanation"], record["legal_basis"], record["source_label"],
                     record["source_url"], time.time()),
                )
        with self._connect() as db:
            if seen:
                marks = ",".join("?" for _ in seen)
                db.execute(f"UPDATE questions SET active=0 WHERE origin='seed' AND id NOT IN ({marks})", seen)
            else:
                db.execute("UPDATE questions SET active=0 WHERE origin='seed'")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    # ---- sorular
    def add_question(self, payload: dict[str, Any], *, identifier: str | None = None) -> str:
        record = validate_question(payload)
        identifier = identifier or uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO questions (id,branch,text,options_json,answer_index,explanation,legal_basis,source_label,source_url,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, record["branch"], record["text"], json.dumps(record["options"], ensure_ascii=False),
                 record["answer_index"], record["explanation"], record["legal_basis"], record["source_label"],
                 record["source_url"], time.time()),
            )
        return identifier

    @staticmethod
    def _public(row: sqlite3.Row, *, reveal: bool = False) -> dict[str, Any]:
        item = {
            "id": row["id"], "branch": row["branch"], "branch_label": BRANCHES.get(row["branch"], row["branch"]),
            "text": row["text"], "options": json.loads(row["options_json"]),
        }
        if reveal:
            item.update({
                "answer_index": row["answer_index"], "explanation": row["explanation"], "legal_basis": row["legal_basis"],
                "source_label": row["source_label"], "source_url": row["source_url"],
            })
        return item

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT branch, COUNT(*) n FROM questions WHERE active=1 GROUP BY branch").fetchall()
        counts = {key: 0 for key in BRANCHES}
        counts.update({row["branch"]: row["n"] for row in rows})
        return counts

    def pick(self, branch: str, count: int) -> list[dict[str, Any]]:
        if branch and branch not in BRANCHES:
            raise EducationError("Branş geçersiz.")
        count = max(1, min(int(count), 50))
        with self._connect() as db:
            if branch:
                rows = db.execute("SELECT * FROM questions WHERE active=1 AND branch=?", (branch,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM questions WHERE active=1").fetchall()
        random.shuffle(rows)
        return [self._public(row) for row in rows[:count]]

    def answer(self, question_id: str, chosen: Any, user_sub: str | None, *, exam_id: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM questions WHERE id=? AND active=1", (question_id,)).fetchone()
            if row is None:
                raise EducationError("Soru bulunamadı.")
            options = json.loads(row["options_json"])
            if not isinstance(chosen, int) or isinstance(chosen, bool) or not 0 <= chosen < len(options):
                raise EducationError("Seçenek numarası geçersiz.")
            correct = chosen == row["answer_index"]
            if user_sub:
                db.execute(
                    "INSERT INTO attempts (id,user_sub,question_id,chosen,correct,exam_id,created_at) VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_sub, question_id, chosen, 1 if correct else 0, exam_id, time.time()),
                )
        return {"correct": correct, "recorded": bool(user_sub), "question": self._public(row, reveal=True), "chosen": chosen}

    # ---- sınav
    def start_exam(self, user_sub: str, branch: str, count: int, minutes: int) -> dict[str, Any]:
        questions = self.pick(branch, count)
        if not questions:
            raise EducationError("Bu branşta henüz soru yok.")
        minutes = max(1, min(int(minutes), 180))
        now = time.time()
        exam_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO exams (id,user_sub,branch,question_ids_json,started_at,deadline) VALUES (?,?,?,?,?,?)",
                (exam_id, user_sub, branch or "karma", json.dumps([q["id"] for q in questions]), now, now + minutes * 60),
            )
        return {"exam_id": exam_id, "branch": branch or "karma", "questions": questions, "started_at": now,
                "deadline": now + minutes * 60, "minutes": minutes}

    def submit_exam(self, user_sub: str, exam_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(answers, dict):
            raise EducationError("Cevaplar geçersiz.")
        with self._connect() as db:
            exam = db.execute("SELECT * FROM exams WHERE id=? AND user_sub=?", (exam_id, user_sub)).fetchone()
        if exam is None:
            raise EducationError("Sınav bulunamadı.")
        if exam["submitted_at"] is not None:
            raise EducationError("Bu sınav daha önce teslim edildi.")
        now = time.time()
        overtime = now > exam["deadline"] + GRACE_SECONDS
        ids = json.loads(exam["question_ids_json"])
        results, score = [], 0
        for question_id in ids:
            chosen = answers.get(question_id)
            if chosen is None:
                with self._connect() as db:
                    row = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
                results.append({"correct": False, "recorded": False, "question": self._public(row, reveal=True), "chosen": None})
                continue
            outcome = self.answer(question_id, chosen, user_sub, exam_id=exam_id)
            score += 1 if outcome["correct"] else 0
            results.append(outcome)
        with self._connect() as db:
            db.execute("UPDATE exams SET submitted_at=?, score=?, overtime=? WHERE id=?", (now, score, 1 if overtime else 0, exam_id))
        return {"exam_id": exam_id, "score": score, "total": len(ids), "overtime": overtime,
                "elapsed_seconds": round(now - exam["started_at"]), "results": results}

    # ---- yanlışlarım
    def wrong_answers(self, user_sub: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Her soruda kullanıcının son denemesi yanlışsa listelenir."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT q.*, a.chosen AS chosen FROM questions q JOIN attempts a ON a.question_id=q.id "
                "WHERE a.user_sub=? AND a.created_at=(SELECT MAX(created_at) FROM attempts WHERE user_sub=a.user_sub AND question_id=a.question_id) "
                "AND a.correct=0 ORDER BY a.created_at DESC LIMIT ?",
                (user_sub, max(1, min(int(limit), 200))),
            ).fetchall()
        return [{**self._public(row, reveal=True), "chosen": row["chosen"]} for row in rows]

    def stats(self, user_sub: str) -> dict[str, Any]:
        with self._connect() as db:
            total, correct = db.execute("SELECT COUNT(*), COALESCE(SUM(correct),0) FROM attempts WHERE user_sub=?", (user_sub,)).fetchone()
            exams = db.execute("SELECT COUNT(*) FROM exams WHERE user_sub=? AND submitted_at IS NOT NULL", (user_sub,)).fetchone()[0]
        return {"attempts": total, "correct": int(correct), "exams": exams}
