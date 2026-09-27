"""Ölçüm kalkanı: vakanın kendi AB tüzüğü sınıflandırma kanıtına girmez.

Ölçüm vakaları AB sınıflandırma tüzüklerinden alınır ve aynı tüzükler hatta kanıt
olarak çekilir. Kalkan olmadan model cevabı kaynaktan okuyabilir ve skor sınıflandırmayı
değil aramayı ölçer. Testler ağa çıkmaz ve model çağırmaz.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import classification_benchmark as bench
import customs_advisor
from customs_advisor import (
    CustomsAdvisor,
    _excluded_by_benchmark,
    _hybrid_entry_excluded,
    excluding_classification_evidence,
)
from tests.test_rag_evidence import FakeHybridIndex, FakeTariffEngine, _document


class GuardTests(unittest.TestCase):
    def test_nothing_is_excluded_outside_a_benchmark_run(self):
        self.assertFalse(
            _excluded_by_benchmark(regulations=["2023/1131"], page=695, text="2023/1131")
        )

    def test_the_case_regulation_is_excluded_and_others_are_kept(self):
        with excluding_classification_evidence(regulations=["2023/1131"]):
            self.assertTrue(_excluded_by_benchmark(regulations=["(EU) 2023/1131"]))
            self.assertTrue(_excluded_by_benchmark(text="Regulation (EU) 2023/1131 of 5 June"))
            self.assertFalse(_excluded_by_benchmark(regulations=["2022/1524"]))
            # Rakam sınırı: başka bir tüzüğün numarası içinde geçen dize eşleşmez.
            self.assertFalse(_excluded_by_benchmark(text="12023/11310"))

    def test_consolidated_list_pages_are_excluded(self):
        with excluding_classification_evidence(consolidated_pages=[695, 696]):
            self.assertTrue(_excluded_by_benchmark(page=696))
            self.assertFalse(_excluded_by_benchmark(page=697))

    def test_the_guard_is_reset_after_the_block(self):
        with excluding_classification_evidence(regulations=["2023/1131"]):
            pass
        self.assertFalse(_excluded_by_benchmark(regulations=["2023/1131"]))

    def test_only_eu_classification_entries_are_filtered(self):
        with excluding_classification_evidence(consolidated_pages=[695]):
            self.assertTrue(
                _hybrid_entry_excluded(
                    {"corpus": "eu_classification", "title": "AB sınıflandırma tüzükleri – sayfa 695 (1)"}
                )
            )
            self.assertFalse(
                _hybrid_entry_excluded(
                    {"corpus": "tariff_descriptions", "title": "sayfa 695", "excerpt": ""}
                )
            )


class HybridFilterTests(unittest.TestCase):
    def test_the_case_page_is_dropped_from_hybrid_evidence(self):
        own = _document(
            "eu-classification:12:1",
            corpus="eu_classification",
            codes=["210690"],
            snippet="Vitamin gummies in the form of orange bears.",
            title="AB sınıflandırma tüzükleri – sayfa 695 (1)",
        )
        other = _document(
            "eu-classification:40:1",
            corpus="eu_classification",
            codes=["691110"],
            snippet="Bone china cups.",
            title="AB sınıflandırma tüzükleri – sayfa 696 (1)",
        )
        advisor = CustomsAdvisor(
            tariff_engine=FakeTariffEngine(), hybrid_index=FakeHybridIndex([own, other])
        )

        async def fetch() -> list[dict[str, Any]]:
            return await advisor._hybrid_evidence(
                "vitamin jelibon", limit=8, corpora=["eu_classification"]
            )

        self.assertEqual(len(asyncio.run(fetch())), 2)
        with excluding_classification_evidence(consolidated_pages=[695]):
            kept = asyncio.run(fetch())
        self.assertEqual([entry["document_id"] for entry in kept], ["eu-classification:40:1"])


class RunnerTests(unittest.TestCase):
    def test_consolidated_list_cases_exclude_their_page_and_the_next(self):
        case = {
            "regulation_references": ["2023/1131"],
            "source_page": 695,
            "source_url": "https://taxation-customs.ec.europa.eu/document/download/x.pdf",
        }
        self.assertEqual(bench.evidence_exclusion(case), (["2023/1131"], [695, 696]))

    def test_eur_lex_cases_exclude_only_their_regulation(self):
        case = {
            "regulation_references": ["2025/289"],
            "source_page": 1,
            "source_url": "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32025R0289",
        }
        self.assertEqual(bench.evidence_exclusion(case), (["2025/289"], []))

    def test_every_case_runs_under_its_own_guard_even_concurrently(self):
        """Eşzamanlı vakalar birbirinin kalkanını görmez; koşu bitince kalkan kalkar."""

        seen: dict[str, Any] = {}

        class Recorder:
            async def classify_product(self, request: Any) -> Any:
                await asyncio.sleep(0)
                refs, pages = customs_advisor._EVIDENCE_EXCLUSION.get()
                seen[str(request.product_description)] = (set(refs), set(pages))
                raise RuntimeError("yalnız kalkanı gözlemler")

        cases = [
            case for case in bench.load_cases("eu")
            if case["id"] in {"eu-2023-1131-p695", "eu-2023-2451-p699"}
        ]
        asyncio.run(bench.run_cases(Recorder(), cases))
        by_id = {case["id"]: seen[case["description"]] for case in cases}
        self.assertEqual(by_id["eu-2023-1131-p695"], ({"2023/1131"}, {695, 696}))
        self.assertEqual(by_id["eu-2023-2451-p699"], ({"2023/2451"}, {699, 700}))
        self.assertEqual(customs_advisor._EVIDENCE_EXCLUSION.get(), (frozenset(), frozenset()))


if __name__ == "__main__":
    unittest.main()
