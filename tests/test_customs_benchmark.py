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


class Cn8AttemptTests(unittest.TestCase):
    """``top1_cn8`` iki ayrı olguyu topluyor; rapor ikisini ayırmalı.

    **Canlıda ölçüldü (16/16, sürüm 136c58a).** Altı vakada Top-1 CN8 başarısızdı ve
    altısının da tek sebebi vardı: model ilk adayı 6 hanede bırakmıştı. 8 haneye indiği
    10 vakanın 10'u doğruydu. Yani ``top1_cn8 = %62,5`` bir "8 hane doğruluğu" değil,
    büyük ölçüde bir "8 haneye inme oranı"ydı — ve öyle okundu, yanlış okundu.
    Ölçüt bu ayrımı kendisi göstermezse aynı yanılgı her turda tekrar eder.
    """

    def test_a_six_digit_first_candidate_is_reported_as_not_attempted(self):
        cases = [
            {"id": "a", "description": "x", "expected_cn8": {"12345678"}, "accepted_hs6": {"123456"}},
        ]
        # Doğru pozisyon (HS6) bulundu, ama 8 haneye inilmedi.
        report = evaluate_predictions(cases, [{"id": "a", "candidates": ["123456"]}])
        self.assertTrue(report["details"][0]["top1_hs6"], "pozisyon doğru")
        self.assertFalse(report["details"][0]["top1_cn8"], "8 hane yok, ölçüt başarısız sayar")
        self.assertFalse(report["details"][0]["cn8_attempted"], "8 haneye inilmedi")
        self.assertEqual(report["counts"]["cn8_attempted"], 0)
        self.assertEqual(report["cn8_attempted_case_count"], 0)
        self.assertIsNone(
            report["top1_cn8_when_attempted"],
            "hiç denenmemişken oran uydurulmamalı",
        )

    def test_accuracy_when_attempted_is_separated_from_the_attempt_rate(self):
        cases = [
            {"id": "a", "description": "x", "expected_cn8": {"12345678"}, "accepted_hs6": {"123456"}},
            {"id": "b", "description": "y", "expected_cn8": {"87654321"}, "accepted_hs6": {"876543"}},
            {"id": "c", "description": "z", "expected_cn8": {"11112222"}, "accepted_hs6": {"111122"}},
        ]
        report = evaluate_predictions(
            cases,
            [
                {"id": "a", "candidates": ["12345678"]},  # indi ve doğru
                {"id": "b", "candidates": ["876543"]},    # 6 hanede kaldı
                {"id": "c", "candidates": ["11119999"]},  # indi ama yanlış
            ],
        )
        self.assertEqual(report["cn8_attempted_case_count"], 2, "iki vakada 8 haneye inildi")
        self.assertEqual(report["top1_cn8_when_attempted"], 0.5, "indiği iki vakada 1 doğru")
        # Toplam oran değişmiyor: üç vakanın biri doğru.
        self.assertAlmostEqual(report["metrics"]["top1_cn8"], round(1 / 3, 4))
        # Bu ikisi farklı sorular; aynı sayı olmaları tesadüf olabilir, eşitlenmemeli.
        self.assertNotEqual(
            report["top1_cn8_when_attempted"],
            report["metrics"]["top1_cn8"],
        )

    def test_the_attempt_rate_is_also_reported_over_measured_cases_only(self):
        cases = [
            {"id": "a", "description": "x", "expected_cn8": {"12345678"}, "accepted_hs6": {"123456"}},
            {"id": "b", "description": "y", "expected_cn8": {"87654321"}, "accepted_hs6": {"876543"}},
        ]
        # "b" hiç koşulmadı: koşulmayan vaka "8 haneye inmedi" diye suçlanamaz.
        report = evaluate_predictions(cases, [{"id": "a", "candidates": ["12345678"]}])
        self.assertEqual(report["metrics"]["cn8_attempted"], 0.5, "tüm veri setine bölünmüş")
        self.assertEqual(
            report["metrics_measured"]["cn8_attempted"], 1.0, "koşulanların hepsinde inildi"
        )
