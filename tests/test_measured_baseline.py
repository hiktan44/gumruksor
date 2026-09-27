"""Kaydedilen ölçüm tabanı puanlayıcının hesabından sapmasın.

Ölçümün kendisi canlı model çağrısı gerektirir, bu yüzden CI'da koşulamaz ve taban elle
kaydedilir. Elle kaydedilen bir rakam ise sessizce yanlışa dönüşebilir: ya kayıt sırasında
yazım hatası olur, ya ileride puanlayıcının tanımı değişir ve dosyadaki rakam eski tanımı
anlatmaya devam eder. Bu testler tabanı kendi adaylarına karşı yeniden hesaplar; kayıt ile
hesap ayrışırsa kırmızıya döner. Böylece dosya bir iddia değil, doğrulanabilir bir kayıt olur.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import customs_benchmark
from customs_benchmark import evaluate_predictions, load_cases

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "benchmarks" / "measured_baseline.json"
_METRIC_KEYS = ("top1_hs6", "top3_hs6", "top1_cn8", "top3_cn8")


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _cases() -> list[dict]:
    payload = _baseline()
    cases: list[dict] = []
    for relative in payload["datasets"]:
        cases.extend(load_cases(ROOT / relative))
    return cases


class MeasuredBaselineTests(unittest.TestCase):
    def test_the_recorded_metrics_match_a_fresh_scoring_of_the_recorded_candidates(self):
        """Asıl kilit: dosyadaki rakam, dosyadaki adaylardan yeniden türetilebilmeli."""
        cases = _cases()
        for run in _baseline()["runs"]:
            with self.subTest(measured_at=run["measured_at"]):
                report = evaluate_predictions(
                    cases,
                    [{"id": cid, "candidates": codes} for cid, codes in run["candidates"].items()],
                )
                for key in _METRIC_KEYS:
                    self.assertEqual(
                        report["metrics"][key],
                        run["metrics"][key],
                        f"{key}: kayıt ile hesap ayrıştı",
                    )
                self.assertEqual(
                    report["cn8_attempted_case_count"], run["cn8_attempted_case_count"]
                )
                self.assertEqual(
                    report["top1_cn8_when_attempted"], run["top1_cn8_when_attempted"]
                )
                self.assertEqual(report["measured_case_count"], run["measured_case_count"])

    def test_every_case_in_the_datasets_is_covered_by_every_run(self):
        """Yarım koşu taban olamaz: koşulmayan vaka başarısız sayılır ve rakamı bozar."""
        ids = {str(case["id"]) for case in _cases()}
        for run in _baseline()["runs"]:
            with self.subTest(measured_at=run["measured_at"]):
                self.assertEqual(set(run["candidates"]), ids)
                self.assertEqual(run["measured_case_count"], len(ids))

    def test_the_two_recorded_runs_agree_on_every_headline_metric(self):
        """Tabanın değeri bu tutarlılıktan gelir.

        16 vaka küçük bir örneklem; rakamların gürültü olmadığını gösteren şey, aynı
        sürümde bağımsız iki koşunun aynı yere düşmesidir. Bu eşitlik bozulursa taban
        artık "kararlı ölçüm" iddiasını taşıyamaz ve dosyanın o cümlesi güncellenmelidir.
        """
        runs = _baseline()["runs"]
        self.assertGreaterEqual(len(runs), 2, "kararlılık iddiası en az iki koşu ister")
        first, *rest = runs
        for run in rest:
            self.assertEqual(run["code_version"], first["code_version"])
            for key in _METRIC_KEYS:
                self.assertEqual(run["metrics"][key], first["metrics"][key], key)

    def test_known_failures_name_a_real_case_and_stay_failing_in_the_record(self):
        """Bilinen kusur listesi gerçek vakaya işaret etmeli ve hâlâ kusurlu olmalı.

        Düzelen bir vaka bu listede kalırsa dosya yanlış bilgi verir; olmayan bir vaka
        kimliği ise listeyi kontrol edilemez hâle getirir.
        """
        cases = _cases()
        ids = {str(case["id"]) for case in cases}
        payload = _baseline()
        self.assertTrue(payload["known_reproducible_failures"], "liste boş olmamalı")
        for failure in payload["known_reproducible_failures"]:
            with self.subTest(case=failure["id"]):
                self.assertIn(failure["id"], ids, "bilinmeyen vaka kimliği")
                self.assertGreaterEqual(failure["seen_in_runs"], 2, "tekrarlanabilir denemek için")
                for run in payload["runs"]:
                    report = evaluate_predictions(
                        cases,
                        [
                            {"id": cid, "candidates": codes}
                            for cid, codes in run["candidates"].items()
                        ],
                    )
                    detail = next(d for d in report["details"] if d["id"] == failure["id"])
                    self.assertFalse(
                        detail["top1_cn8"],
                        "kusurlu diye kaydedilen vaka kayıtlı koşuda Top-1 CN8 geçiyor",
                    )

    def test_the_baseline_is_scored_by_the_shared_scorer_not_its_own_arithmetic(self):
        """Dosya kendi ölçüt tanımını uydurmasın; tek doğruluk tanımı korunur."""
        self.assertTrue(hasattr(customs_benchmark, "evaluate_predictions"))
        recorded = set(_baseline()["runs"][0]["metrics"])
        self.assertEqual(recorded, set(_METRIC_KEYS), "taban yalnız bilinen ölçütleri kaydeder")


BASELINE_V2_PATH = ROOT / "benchmarks" / "measured_baseline_v2.json"
BASELINE_V3_PATH = ROOT / "benchmarks" / "measured_baseline_v3.json"


class _GuardedBaselineChecks:
    """Kopya kalkanlı tabanlar: aynı kilitler, tek koşu açıkça beyan edilir."""

    PATH: Path
    CASE_COUNT: int

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(cls.PATH.read_text(encoding="utf-8"))
        cls.cases = [case for relative in cls.payload["datasets"] for case in load_cases(ROOT / relative)]

    def _report(self, run: dict, cases: list[dict] | None = None) -> dict:
        cases = self.cases if cases is None else cases
        ids = {str(case["id"]) for case in cases}
        return evaluate_predictions(
            cases,
            [{"id": cid, "candidates": codes} for cid, codes in run["candidates"].items() if cid in ids],
        )

    def test_the_recorded_metrics_match_a_fresh_scoring(self):
        for run in self.payload["runs"]:
            report = self._report(run)
            for key in _METRIC_KEYS:
                self.assertEqual(report["metrics"][key], run["metrics"][key], key)
            self.assertEqual(report["cn8_attempted_case_count"], run["cn8_attempted_case_count"])
            self.assertEqual(report["top1_cn8_when_attempted"], run["top1_cn8_when_attempted"])

    def test_holdout_and_seen_metrics_match_a_fresh_scoring(self):
        """Saklı sınav ayrı raporlanıyorsa o rakam da dosyadaki adaylardan türetilebilmeli."""
        holdout = self.payload.get("holdout_dataset")
        if not holdout:
            return
        held = load_cases(ROOT / holdout)
        held_ids = {str(case["id"]) for case in held}
        seen = [case for case in self.cases if str(case["id"]) not in held_ids]
        for run in self.payload["runs"]:
            for cases, key in ((held, "holdout_metrics"), (seen, "seen_metrics")):
                report = self._report(run, cases)
                for metric in _METRIC_KEYS:
                    self.assertEqual(report["metrics"][metric], run[key][metric], f"{key}.{metric}")

    def test_every_case_of_every_dataset_is_covered(self):
        ids = {str(case["id"]) for case in self.cases}
        self.assertEqual(len(ids), self.CASE_COUNT)
        for run in self.payload["runs"]:
            self.assertEqual(set(run["candidates"]), ids)
            self.assertEqual(run["measured_case_count"], len(ids))

    def test_a_single_run_is_declared_not_passed_off_as_stable(self):
        runs = self.payload["runs"]
        if len(runs) < 2:
            self.assertTrue(self.payload.get("single_run"))
            self.assertTrue(any("Tek koşu" in item for item in self.payload["limits"]))
        else:
            first, *rest = runs
            for run in rest:
                self.assertEqual(run["code_version"], first["code_version"])

    def test_the_run_was_measured_under_the_evidence_guard(self):
        self.assertTrue(self.payload["evidence_guard"])

    def test_findings_name_real_cases_in_the_state_they_claim(self):
        ids = {str(case["id"]) for case in self.cases}
        report = self._report(self.payload["runs"][0])
        passing = {d["id"] for d in report["details"] if d["top1_cn8"]}
        for finding in self.payload["findings"]:
            expect_pass = finding.get("expect", "fail") == "pass"
            for case_id in finding["ids"]:
                self.assertIn(case_id, ids)
                self.assertEqual(case_id in passing, expect_pass, f"{case_id} kayıtta iddia edilen durumda değil")


class GuardedBaselineV2Tests(_GuardedBaselineChecks, unittest.TestCase):
    PATH = BASELINE_V2_PATH
    CASE_COUNT = 55


class GuardedBaselineV3Tests(_GuardedBaselineChecks, unittest.TestCase):
    PATH = BASELINE_V3_PATH
    CASE_COUNT = 85
