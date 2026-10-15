"""Soru taslağı üretim betiği ve emsal notu aktarımı: ağ yok, sahte istemciler."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from education import EducationStore
from risk_engine import RiskStore

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen = load("soru_taslagi_uret")
emsal = load("emsal_notu_aktar")

ARTICLE = (
    "**MADDE 23 –** Eşyanın gümrük kıymeti, bu eşyanın işlem değeridir. İşlem değeri, eşyanın Türkiye'ye ihracat amacıyla "
    "satışında fiilen ödenen veya ödenecek bedeldir. Bu madde kapsamında 30 gün içinde ödeme yapılması örnektir. "
    "Satıcı ile alıcı arasındaki ilişki işlem değerini etkilemişse ayrıca incelenir ve belgelenir."
)
QUOTE = "Eşyanın gümrük kıymeti, bu eşyanın işlem değeridir."


def raw(**over):
    base = {"text": "Gümrük kıymeti hangi değere göre belirlenir?", "options": ["İşlem değeri", "Piyasa değeri", "Sigorta değeri"],
            "answer_index": 0, "explanation": "Kıymet işlem değeridir.", "evidence_quote": QUOTE + " İşlem değeri, eşyanın"}
    base.update(over)
    return base


class FakeWriter:
    def __init__(self, *batches):
        self.batches, self.calls = list(batches), []

    async def write(self, unit):
        self.calls.append(unit.ref)
        return self.batches.pop(0) if self.batches else []


class FakeMevzuat:
    def __init__(self, markdown=None, error=None):
        self.markdown, self.error, self.calls = markdown, error, []

    async def get_content(self, no, tur, tertip):
        self.calls.append((no, tur, tertip))
        return SimpleNamespace(markdown_content=self.markdown or "", error_message=self.error)


class FakeDecisions:
    def __init__(self, text):
        self.text, self.fetched = text, []
        self.refs = [SimpleNamespace(document_id="d1", birim="Danıştay 7. Daire", esas_no="2019/10", karar_no="2020/20",
                                     karar_tarihi="2020-05-01", konular=["GÜMRÜK KIYMETİ"], public_url="https://mevzuat.adalet.gov.tr/ictihat/d1"),
                     SimpleNamespace(document_id="d2", birim="Danıştay 7. Daire", esas_no="", karar_no="", karar_tarihi="",
                                     konular=[], public_url="https://mevzuat.adalet.gov.tr/ictihat/d2")]

    async def search(self, phrase):
        return self.refs

    async def fetch_text(self, ref):
        self.fetched.append(ref.document_id)
        return self.text


class GroundingTests(unittest.TestCase):
    def test_quote_must_appear_in_source_with_turkish_case_folding(self):
        source = "GÜMRÜK KIYMETİ, EŞYANIN İŞLEM DEĞERİDİR. Bu değer fiilen ödenen bedeldir ve belgelenir."
        draft = {"evidence_quote": "gümrük kıymeti, eşyanın işlem değeridir. Bu değer fiilen", "options": ["a", "b"], "answer_index": 0}
        self.assertEqual(gen.grounding_errors(draft, source), [])  # büyük/küçük harf ve İ/I katlandı
        self.assertTrue(any("birebir geçmiyor" in e for e in gen.grounding_errors({**draft, "evidence_quote": "Kaynakta olmayan uzun bir cümle uyduruldu burada."}, source)))

    def test_short_or_missing_quote_and_invented_numbers_are_rejected(self):
        self.assertTrue(gen.grounding_errors(raw(evidence_quote="kısa"), ARTICLE))
        invented = gen.grounding_errors(raw(options=["45 gün", "x"], explanation="45 gün içinde"), ARTICLE)
        self.assertTrue(any("sayı" in e and "45" in e for e in invented))
        self.assertEqual(gen.grounding_errors(raw(options=["30 gün", "x"]), ARTICLE), [])

    def test_markdown_and_dash_variants_do_not_break_matching(self):
        source = "**Madde 5** – Sınıflandırma  yapılırken\n eşyanın niteliği esas alınır ve yorum kuralları sırasıyla uygulanır."
        quote = "Sınıflandırma yapılırken eşyanın niteliği esas alınır ve yorum kuralları"
        self.assertEqual(gen.grounding_errors({"evidence_quote": quote, "options": ["a", "b"], "answer_index": 0}, source), [])


class PipelineTests(unittest.TestCase):
    def run_gen(self, writer, mevzuat=None, decisions=None, branches=("mevzuat",), target=2):
        sources = [{"name": "Gümrük Kanunu", "mevzuat_no": "4458", "mevzuat_tur": 1, "mevzuat_tertip": "5", "branch": "mevzuat"}]
        return asyncio.run(gen.generate(branches=list(branches), target=target, writer=writer, mevzuat_client=mevzuat,
                                        decisions=decisions, sources=sources, delay=0))

    def test_law_source_produces_draft_with_article_ref_and_url(self):
        result = self.run_gen(FakeWriter([raw()]), FakeMevzuat(ARTICLE))
        self.assertEqual(len(result.drafts), 1)
        draft = result.drafts[0]
        self.assertEqual((draft["status"], draft["source_kind"], draft["branch"]), ("draft", "mevzuat", "mevzuat"))
        self.assertEqual(draft["source_ref"], "Gümrük Kanunu md.23")
        self.assertIn("MevzuatNo=4458", draft["source_url"])
        self.assertNotIn("bağlayıcı olmayabilir", draft["explanation"])

    def test_ungrounded_draft_is_auto_rejected_and_logged_not_returned(self):
        bad = raw(evidence_quote="Bu cümle kaynak metinde hiçbir yerde geçmiyor ve uydurulmuştur.")
        result = self.run_gen(FakeWriter([bad]), FakeMevzuat(ARTICLE))
        self.assertEqual(result.drafts, [])
        self.assertIn("birebir geçmiyor", result.rejected[0]["reason"])
        self.assertEqual(result.rejected[0]["ref"], "Gümrük Kanunu md.23")

    def test_invalid_format_is_rejected_not_crashing(self):
        result = self.run_gen(FakeWriter([raw(options=["tek"]), raw(answer_index=9)]), FakeMevzuat(ARTICLE))
        self.assertEqual(len(result.rejected), 2)
        self.assertEqual(result.drafts, [])

    def test_target_per_branch_and_dedupe(self):
        two = "\n".join(f"**MADDE {n} –** " + ARTICLE.split("–** ")[1] for n in (23, 24))
        writer = FakeWriter([raw()], [raw()])
        result = self.run_gen(writer, FakeMevzuat(two), target=1)
        self.assertEqual(len(result.drafts), 1)
        self.assertEqual(writer.calls, ["Gümrük Kanunu md.23"])  # hedefe ulaşınca durur

    def test_law_download_failure_is_reported_and_other_work_continues(self):
        result = self.run_gen(FakeWriter(), FakeMevzuat(error="403"))
        self.assertIn("Gümrük Kanunu", result.skipped_sources[0])

    def test_decision_source_is_labelled_precedent_with_citation_and_public_url(self):
        text = "Davacı şirket, ithal ettiği eşyanın gümrük kıymetinin işlem değeri yöntemiyle belirlenmesi gerektiğini ileri sürmüştür. " * 8
        quote = "ithal ettiği eşyanın gümrük kıymetinin işlem değeri yöntemiyle belirlenmesi gerektiğini ileri sürmüştür"
        decisions = FakeDecisions(text + " Gümrük idaresi tarife ithalat beyanname kıymet kararını vermiştir.")
        result = self.run_gen(FakeWriter([raw(evidence_quote=quote, options=["Davacı", "Davalı"], explanation="Davacı iddiası")]),
                              decisions=decisions, branches=["kiymet"], target=1)
        self.assertEqual(len(result.drafts), 1)
        draft = result.drafts[0]
        self.assertEqual((draft["source_kind"], draft["status"]), ("karar", "draft"))
        self.assertIn("E.2019/10", draft["source_ref"])
        self.assertIn("K.2020/20", draft["source_ref"])
        self.assertEqual(draft["source_url"], "https://mevzuat.adalet.gov.tr/ictihat/d1")
        self.assertIn("bağlayıcı olmayabilir", draft["legal_basis"])
        self.assertTrue(draft["explanation"].startswith("Emsal karar; bağlayıcı olmayabilir."))
        self.assertEqual(decisions.fetched, ["d1"])  # künyesi eksik karar için metin bile çekilmez

    def test_irrelevant_decision_is_skipped(self):
        decisions = FakeDecisions("Konu ile ilgisiz bir uyuşmazlık metni. " * 30)
        result = self.run_gen(FakeWriter([raw()]), decisions=decisions, branches=["kiymet"])
        self.assertEqual(result.drafts, [])

    def test_outputs_and_store_write_drafts_only(self):
        result = self.run_gen(FakeWriter([raw()]), FakeMevzuat(ARTICLE))
        with tempfile.TemporaryDirectory() as tmp:
            out, rej = Path(tmp) / "t.json", Path(tmp) / "r.json"
            gen.write_outputs(result, out, rej)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["questions"][0]["status"], "draft")
            self.assertEqual(gen.store_drafts(result, tmp), 1)
            self.assertEqual(gen.store_drafts(result, tmp), 0)  # yeniden çalıştırma çoğaltmaz
            store = EducationStore(tmp)
            self.assertEqual(store.counts()["mevzuat"], 0)  # onaylanana kadar öğrenciye görünmez
            item = store.list_for_review("draft")[0]
            self.assertEqual((item["origin"], item["source_ref"]), ("script", "Gümrük Kanunu md.23"))
            self.assertTrue(item["evidence_quote"])

    def test_default_sources_file_is_valid_and_names_no_articles(self):
        sources = gen.load_sources()
        self.assertTrue(sources)
        self.assertTrue(all("articles" not in item for item in sources))

    def test_missing_model_config_refuses_to_run(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"QUESTION_LLM_BASE_URL": "", "QUESTION_LLM_API_KEY": "", "QUESTION_LLM_MODEL": ""}):
            self.assertEqual(asyncio.run(gen.main_async(gen.parse_args([]))), 2)

    def test_openai_writer_parses_model_json(self):
        class Resp:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": json.dumps({"questions": [raw(), "x"]})}}]}

        class Client:
            async def post(self, url, json=None):
                self.url, self.body = url, json
                return Resp()

        client = Client()
        writer = gen.OpenAICompatibleWriter("https://llm.example/v1/", "k", "m", client=client)
        unit = gen.SourceUnit(kind="mevzuat", ref="r", url="https://x", branch="mevzuat", text=ARTICLE)
        self.assertEqual(len(asyncio.run(writer.write(unit))), 1)
        self.assertTrue(client.url.endswith("/chat/completions"))
        self.assertIn("KAYNAK", client.body["messages"][1]["content"])


class EmsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "arsiv"
        self.archive.mkdir()
        db = sqlite3.connect(self.archive / "ictihat.sqlite3")
        db.execute("""CREATE TABLE decisions (document_id TEXT PRIMARY KEY, item_type TEXT, birim TEXT, esas_no TEXT, karar_no TEXT,
            karar_tarihi TEXT, karar_turu TEXT, konular TEXT, url TEXT, sha256 TEXT, retrieved_at TEXT, text_quality TEXT, quality_note TEXT,
            char_count INTEGER, gtip_json TEXT, duplicate_of TEXT, body TEXT)""")
        rows = [
            ("k1", "Danıştay 7. Daire", "2019/1", "2020/2", "2020-03-01", ["GÜMRÜK KIYMETİ"], ["8517.13.00.00.00"], "", "ok"),
            ("t1", "Danıştay 7. Daire", "2018/5", "2019/6", "2019-03-01", ["GÜMRÜK TARİFE İSTATİSTİK POZİSYONU"], ["8471.30.00.00.00", "8471.41.00.00.00"], "", "ok"),
            ("x1", "Danıştay 7. Daire", "2018/7", "2019/8", "2019-04-01", ["DAMPİNG"], ["3926.90.97.00.00"], "", "ok"),
            ("n1", "Danıştay 7. Daire", "2018/9", "2019/9", "2019-05-01", ["GÜMRÜK KIYMETİ"], [], "", "ok"),
            ("d1", "Danıştay 7. Daire", "2018/9", "2019/9", "2019-05-01", ["GÜMRÜK KIYMETİ"], ["8517.13.00.00.00"], "k1", "ok"),
            ("u1", "Danıştay 7. Daire", "2018/9", "2019/9", "2019-05-01", ["GÜMRÜK KIYMETİ"], ["8517.13.00.00.00"], "", "unreadable"),
        ]
        for document_id, birim, esas, karar, tarih, konular, codes, dup, quality in rows:
            db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (document_id, "", birim, esas, karar, tarih, "", json.dumps(konular, ensure_ascii=False),
                        f"https://mevzuat.adalet.gov.tr/ictihat/{document_id}", "", "2026-10-01", quality, "", 100,
                        json.dumps(codes), dup, "metin"))
        db.commit()
        db.close()
        self.risk_dir = Path(self.tmp.name) / "risk"

    def test_imports_valuation_and_classification_disputes_as_draft_precedents(self):
        summary = emsal.run(self.archive, self.risk_dir, ["kiymet", "tarife"], 50)
        self.assertEqual((summary["decisions"], summary["created"], summary["errors"]), (2, 2, []))
        store = RiskStore(self.risk_dir)
        items = store.list_for_review("draft", kind="precedent")
        self.assertEqual({item["gtip_prefix"] for item in items}, {"8517", "8471"})
        for item in items:
            self.assertEqual((item["category"], item["official"], item["precedent"], item["status"]), ("emsal", False, True, "draft"))
            self.assertIn("resmî kriter değildir", item["origin_label"])
            self.assertIn("bağlayıcı olmayabilir", item["description"])
            self.assertTrue(item["source_url"].startswith("https://mevzuat.adalet.gov.tr/ictihat/"))
        self.assertEqual(store.match("851713", "import", 2026, None), [])  # taslak risk kontrolüne girmez

    def test_rerun_does_not_duplicate_and_dry_run_writes_nothing(self):
        self.assertEqual(emsal.run(self.archive, self.risk_dir, ["kiymet"], 50, dry_run=True)["created"], 1)
        self.assertFalse((self.risk_dir / "risk_criteria.sqlite3").exists())
        emsal.run(self.archive, self.risk_dir, ["kiymet"], 50)
        again = emsal.run(self.archive, self.risk_dir, ["kiymet"], 50)
        self.assertEqual((again["created"], again["existing"]), (0, 1))

    def test_missing_archive_fails_clearly_and_bad_type_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            emsal.run(Path(self.tmp.name) / "yok", self.risk_dir, ["kiymet"], 5)
        self.assertEqual(emsal.main(["--ictihat-dir", str(self.archive), "--tur", "uydurma"]), 2)


if __name__ == "__main__":
    unittest.main()
