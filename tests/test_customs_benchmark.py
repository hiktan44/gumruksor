import json
import tempfile
import unittest
from pathlib import Path

from customs_benchmark import evaluate_predictions, load_cases


ROOT = Path(__file__).resolve().parents[1]


class CustomsBenchmarkTests(unittest.TestCase):
    def test_eu_cases_remain_source_anchored_and_reproducible(self):
        cases = load_cases(ROOT / "benchmarks" / "customs_classification_v1.jsonl")
        self.assertGreaterEqual(len(cases), 10)
        predictions = [
            {"id": case["id"], "candidates": [case["accepted_hs6"][0]]}
            for case in cases
        ]
        result = evaluate_predictions(cases, predictions)
        self.assertEqual(result["metrics"]["top1_hs6"], 1.0)
        self.assertEqual(result["metrics"]["top1_cn8"], 0.0)
        self.assertIsNone(result["metrics"]["top1_gtip12"])
        self.assertIn("Türk GTİP12", result["warning"])

    def test_historical_turkish_btb_suite_loads_and_derives_cn8_hs6(self):
        cases = load_cases(ROOT / "benchmarks" / "turkish_btb_gtip12_historical_v1.jsonl")
        self.assertEqual(len(cases), 4)
        self.assertEqual(cases[0]["expected_gtip12"], ["630790100000"])
        self.assertEqual(cases[0]["expected_cn8"], ["63079010"])
        self.assertEqual(cases[0]["accepted_hs6"], ["630790"])

    def test_gtip12_metrics_use_only_turkish_cases_as_denominator(self):
        cases = load_cases(ROOT / "benchmarks" / "turkish_btb_gtip12_historical_v1.jsonl")
        predictions = [
            {"id": cases[0]["id"], "candidates": ["630790100000"]},
            {"id": cases[1]["id"], "candidates": ["847130000099", "847130000000"]},
            {"id": cases[2]["id"], "candidates": ["950300101900"]},
            {"id": cases[3]["id"], "candidates": []},
        ]
        result = evaluate_predictions(cases, predictions)
        self.assertEqual(result["gtip12_case_count"], 4)
        self.assertEqual(result["metrics"]["top1_gtip12"], 0.5)
        self.assertEqual(result["metrics"]["top3_gtip12"], 0.75)
        self.assertEqual(result["metrics"]["top1_hs6"], 0.75)
        self.assertEqual(result["metrics"]["abstained"], 0.25)

    def test_turkish_targets_require_explicit_jurisdiction_and_tariff_year(self):
        case = {
            "id": "bad",
            "description": "Eksik köken bilgili örnek",
            "expected_gtip12": ["630790100000"],
            "source_page": 1,
            "source_url": "https://ticaret.gov.tr/example.pdf",
            "source_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(json.dumps(case), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "jurisdiction=TR"):
                load_cases(path)


    def test_an_unrun_case_is_not_reported_as_an_abstention(self):
        """Koşulmamış vaka çekimser değildir: model konuşmadı çünkü sorulmadı.

        **Canlıda ölçüldü.** 16 vakanın 8'i koşulmuşken panel "Çekimser (aday üretmedi):
        %50" yazıyordu. O 8 vaka çekimser kalmamıştı, hiç koşulmamıştı; koşulan 8'in
        8'i de doğruydu. İki farklı olguyu tek sayaçta toplamak raporu yanlış yapar:
        "model bu ürünü sınıflandıramadı" ile "bu ürünü hiç sormadık" aynı şey değil ve
        farklı işler gerektirir.
        """
        cases = [
            {"id": "a", "description": "x", "expected_cn8": {"12345678"}, "accepted_hs6": {"123456"}},
            {"id": "b", "description": "y", "expected_cn8": {"87654321"}, "accepted_hs6": {"876543"}},
        ]
        # "a" koştu ve aday üretmedi (gerçek çekimser); "b" hiç koşulmadı.
        report = evaluate_predictions(cases, [{"id": "a", "candidates": []}])
        self.assertEqual(report["counts"]["abstained"], 1, "yalnız koşan ve susan vaka")
        self.assertEqual(report["counts"]["not_run"], 1, "koşulmayan ayrı sayılmalı")
        self.assertEqual(report["measured_case_count"], 1)

    def test_measured_only_accuracy_is_reported_beside_the_total(self):
        """Kısmi ölçümde iki oran gerekir: toplam ve koşulanlar.

        Toplam oran koşulmayanı başarısız sayar, bu kasıtlıdır ve veri setinin
        tamamındaki başarımı verir. Ama "sistem cevap verdiğinde ne kadar doğru"
        sorusunun cevabı ayrı bir orandır; ikisini karıştırmak yarım ölçümde sistemi
        olduğundan kötü gösterir. Canlıda tam bu oldu: gerçek 8/8 iken panel %50 dedi.
        """
        cases = [
            {"id": "a", "description": "x", "expected_cn8": {"12345678"}, "accepted_hs6": {"123456"}},
            {"id": "b", "description": "y", "expected_cn8": {"87654321"}, "accepted_hs6": {"876543"}},
        ]
        report = evaluate_predictions(cases, [{"id": "a", "candidates": ["12345678"]}])
        self.assertEqual(report["metrics"]["top1_cn8"], 0.5, "toplam: koşulmayan başarısız")
        self.assertEqual(report["metrics_measured"]["top1_cn8"], 1.0, "koşulanlarda tam isabet")

if __name__ == "__main__":
    unittest.main()
