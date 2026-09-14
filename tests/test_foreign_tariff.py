"""Yurt dışı tarife motoru: ayrıştırma, menşe eşlemesi, önbellek, inceleme kapısı ve bağlantılar.

Hiçbir test gerçek ağa çıkmaz; UK JSON:API gövdeleri ``httpx.MockTransport`` ile taklit edilir.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import foreign_tariff as ft
from review_policy import ReviewPolicy


def _chapters(count: int = 3, first_description: str = "Live animals") -> dict:
    data = []
    for index in range(count):
        code = f"{index + 1:02d}"
        data.append(
            {
                "id": str(27600 + index),
                "type": "chapter",
                "attributes": {
                    "goods_nomenclature_item_id": f"{code}00000000",
                    "formatted_description": first_description if index == 0 else f"Chapter {code} goods",
                },
            }
        )
    return {"data": data}


def _heading() -> dict:
    return {
        "data": {
            "id": "1",
            "type": "heading",
            "attributes": {"goods_nomenclature_item_id": "8517000000", "formatted_description": "Telephone sets"},
            "relationships": {},
        },
        "included": [
            {
                "id": "60456",
                "type": "commodity",
                "attributes": {
                    "goods_nomenclature_item_id": "8517110000",
                    "formatted_description": "<span>Line telephone sets</span>",
                    "leaf": True,
                    "declarable": True,
                    "number_indents": 2,
                    "producline_suffix": "80",
                },
            },
            {
                "id": "107183",
                "type": "commodity",
                "attributes": {
                    "goods_nomenclature_item_id": "8517130000",
                    "formatted_description": "Smartphones",
                    "leaf": True,
                    "declarable": True,
                    "number_indents": 2,
                    "producline_suffix": "80",
                },
            },
            {
                "id": "60455",
                "type": "commodity",
                "attributes": {
                    "goods_nomenclature_item_id": "8517100000",
                    "formatted_description": "Parent line",
                    "leaf": False,
                    "declarable": False,
                    "number_indents": 1,
                    "producline_suffix": "10",
                },
            },
        ],
    }


def _commodity() -> dict:
    def measure(identifier: str, type_id: str, area: str, duty_id: str, excluded: list[str] | None = None) -> dict:
        return {
            "id": identifier,
            "type": "measure",
            "attributes": {"import": True, "effective_start_date": "2022-01-01T00:00:00.000Z", "effective_end_date": None},
            "relationships": {
                "duty_expression": {"data": {"id": duty_id, "type": "duty_expression"}},
                "measure_type": {"data": {"id": type_id, "type": "measure_type"}},
                "geographical_area": {"data": {"id": area, "type": "geographical_area"}},
                "order_number": {"data": None},
                "excluded_countries": {"data": [{"id": code, "type": "geographical_area"} for code in (excluded or [])]},
            },
        }

    return {
        "data": {
            "id": "107183",
            "type": "commodity",
            "attributes": {
                "goods_nomenclature_item_id": "8517130000",
                "formatted_description": "Smartphones",
                "bti_url": "https://www.gov.uk/guidance/check-what-youll-need",
                "validity_start_date": "2022-01-01T00:00:00.000Z",
                "validity_end_date": None,
                "declarable": True,
            },
            "relationships": {},
        },
        "included": [
            measure("1", "103", "1011", "d1"),
            measure("2", "142", "TR", "d2"),
            measure("3", "142", "1013", "d2"),
            measure("4", "551", "BY", "d3"),
            measure("5", "109", "1011", "d4"),
            {"id": "d1", "type": "duty_expression", "attributes": {"verbose_duty": "0.00%", "base": "0.00 %"}},
            {"id": "d2", "type": "duty_expression", "attributes": {"verbose_duty": "0.00%", "base": "0.00 %"}},
            {"id": "d3", "type": "duty_expression", "attributes": {"verbose_duty": "35.00%", "base": "35.00 %"}},
            {"id": "d4", "type": "duty_expression", "attributes": {"verbose_duty": "items (p/st)", "base": ""}},
            {"id": "103", "type": "measure_type", "attributes": {"description": "Third country duty"}},
            {"id": "142", "type": "measure_type", "attributes": {"description": "Tariff preference"}},
            {"id": "551", "type": "measure_type", "attributes": {"description": "Additional duties"}},
            {"id": "109", "type": "measure_type", "attributes": {"description": "Supplementary unit"}},
            {
                "id": "1011",
                "type": "geographical_area",
                "attributes": {"description": "ERGA OMNES"},
                "relationships": {
                    "children_geographical_areas": {
                        "data": [{"id": code, "type": "geographical_area"} for code in ("TR", "CN", "BY", "DE")]
                    }
                },
            },
            {"id": "TR", "type": "geographical_area", "attributes": {"description": "Turkey"}},
            {"id": "BY", "type": "geographical_area", "attributes": {"description": "Belarus"}},
            {
                "id": "1013",
                "type": "geographical_area",
                "attributes": {"description": "European Union"},
                "relationships": {
                    "children_geographical_areas": {"data": [{"id": "DE", "type": "geographical_area"}]}
                },
            },
        ],
    }


class ParserTests(unittest.TestCase):
    def test_parse_chapters_normalises_codes(self):
        rows = ft.parse_chapters(_chapters())
        self.assertEqual(rows[0], {"code": "01", "description": "Live animals"})
        self.assertEqual(len(rows), 3)

    def test_parse_heading_keeps_only_ten_digit_leaves(self):
        parsed = ft.parse_heading(_heading())
        self.assertEqual(parsed["heading"], "8517")
        codes = [row["code"] for row in parsed["commodities"]]
        self.assertEqual(codes, ["8517100000", "8517110000", "8517130000"])
        # HTML işaretlemesi düz metne indirgenir.
        self.assertEqual(parsed["commodities"][1]["description"], "Line telephone sets")

    def test_parse_commodity_reads_measures_and_groups(self):
        parsed = ft.parse_commodity(_commodity())
        self.assertEqual(parsed["code"], "8517130000")
        self.assertEqual(parsed["bti_url"], "https://www.gov.uk/guidance/check-what-youll-need")
        kinds = {row["kind"] for row in parsed["measures"]}
        self.assertIn("third_country_duty", kinds)
        self.assertIn("preference", kinds)
        self.assertEqual(parsed["geo_children"]["1013"], ["DE"])

    def test_classify_measure(self):
        self.assertEqual(ft.classify_measure("Third country duty"), "third_country_duty")
        self.assertEqual(ft.classify_measure("Definitive anti-dumping duty"), "anti_dumping")
        self.assertEqual(ft.classify_measure("Import prohibition"), "prohibition")
        self.assertEqual(ft.classify_measure("Something else"), "other")

    def test_measure_applies_respects_groups_and_exclusions(self):
        parsed = ft.parse_commodity(_commodity())
        children = parsed["geo_children"]
        erga = next(row for row in parsed["measures"] if row["geographical_area_id"] == "1011" and row["kind"] == "third_country_duty")
        self.assertTrue(ft.measure_applies(erga, "TR", children))
        self.assertFalse(ft.measure_applies(erga, "US", children))
        excluded = dict(erga, excluded_countries=["TR"])
        self.assertFalse(ft.measure_applies(excluded, "TR", children))

    def test_summarise_picks_origin_preference(self):
        parsed = ft.parse_commodity(_commodity())
        summary = ft.summarise_commodity(parsed, "TR")
        self.assertEqual(summary["third_country_duty"], "0.00%")
        self.assertIsNotNone(summary["origin_preference"])
        self.assertEqual(summary["origin_preference"]["geographical_area_id"], "TR")
        # Tamamlayıcı ölçü birimi listeye girmez.
        self.assertNotIn("supplementary_unit", {row["kind"] for row in summary["measures"]})

    def test_origin_iso2(self):
        self.assertEqual(ft.origin_iso2("Çin"), "CN")
        self.assertEqual(ft.origin_iso2("de"), "DE")
        self.assertIsNone(ft.origin_iso2(""))


class LinkCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ft.load_link_catalog()

    def test_catalog_has_three_jurisdictions(self):
        self.assertEqual(set(self.catalog), set(ft.JURISDICTIONS))

    def test_eu_links_carry_code_and_date(self):
        links = ft.build_links(self.catalog["eu"], "851713000000", "TR", "2026-09-14")
        by_id = {item["id"]: item["url"] for item in links}
        self.assertIn("eu_taric_measures", by_id)
        self.assertIn("Taric=8517130000", by_id["eu_taric_measures"])
        self.assertIn("SimDate=20260914", by_id["eu_taric_measures"])
        self.assertIn("Area=TR", by_id["eu_taric_measures"])

    def test_links_requiring_origin_are_skipped_without_origin(self):
        links = ft.build_links(self.catalog["eu"], "851713000000", None, "2026-09-14")
        self.assertNotIn("eu_taric_measures", {item["id"] for item in links})
        self.assertIn("eu_taric_consultation", {item["id"] for item in links})

    def test_swiss_links_are_official_hosts(self):
        links = ft.build_links(self.catalog["ch"], "851713000000", "TR", None)
        self.assertTrue(links)
        for item in links:
            self.assertTrue(item["url"].startswith("https://"))
            self.assertIn("admin.ch", item["url"])

    def test_link_outside_allow_list_is_dropped(self):
        record = {"hosts": ["example.gov"], "links": [{"id": "bad", "title": "x", "url": "https://evil.test/{hs6}"}]}
        self.assertEqual(ft.build_links(record, "851713", None, None), [])


def _transport(calls: list[str], chapters: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/chapters"):
            return httpx.Response(200, json=chapters if chapters is not None else _chapters())
        if "/headings/8517" in path:
            return httpx.Response(200, json=_heading())
        if "/commodities/8517130000" in path:
            return httpx.Response(200, json=_commodity())
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[str] = []

    def _engine(self, *, chapters: dict | None = None, policy: ReviewPolicy | None = None) -> ft.ForeignTariffEngine:
        client = httpx.AsyncClient(transport=_transport(self.calls, chapters), base_url="https://www.trade-tariff.service.gov.uk")
        engine = ft.ForeignTariffEngine(
            self._tmp.name,
            http=client,
            review_policy=policy or ReviewPolicy(),
            base_url="https://www.trade-tariff.service.gov.uk/api/v2",
        )
        self.addCleanup(lambda: asyncio.run(engine.close()))
        return engine

    def test_lookup_returns_uk_rates_and_link_only_jurisdictions(self):
        engine = self._engine()
        result = asyncio.run(engine.lookup("851713000000", origin="TR"))
        payload = result.as_dict()
        self.assertEqual(payload["hs6"], "851713")
        self.assertEqual(payload["origin_code"], "TR")
        results = {item["jurisdiction"]: item for item in payload["results"]}
        self.assertEqual(set(results), {"uk", "eu", "ch"})

        uk = results["uk"]
        self.assertEqual(uk["data_kind"], "api")
        self.assertEqual(uk["match_quality"], "exact_hs6")
        self.assertEqual(uk["matched_code"], "8517130000")
        self.assertEqual(uk["third_country_duty"], "0.00%")
        self.assertEqual(uk["origin_preference"]["geographical_area_id"], "TR")
        self.assertTrue(uk["source_url"].startswith("https://www.trade-tariff.service.gov.uk/"))

        for code in ("eu", "ch"):
            self.assertEqual(results[code]["data_kind"], "links")
            self.assertIsNone(results[code]["third_country_duty"])
            self.assertTrue(results[code]["links"])

    def test_group_membership_matches_preference(self):
        engine = self._engine()
        result = asyncio.run(engine.lookup("851713000000", origin="Almanya", jurisdiction="uk"))
        uk = result.results[0]
        self.assertEqual(uk.origin_preference["geographical_area_id"], "1013")

    def test_unknown_origin_adds_warning(self):
        engine = self._engine()
        result = asyncio.run(engine.lookup("851713000000", origin="Vakanda", jurisdiction="uk"))
        self.assertTrue(any("tanınmadı" in item for item in result.warnings))

    def test_single_jurisdiction_selection_skips_network(self):
        engine = self._engine()
        result = asyncio.run(engine.lookup("851713000000", jurisdiction="ch"))
        self.assertEqual([item.jurisdiction for item in result.results], ["ch"])
        self.assertEqual(self.calls, [])

    def test_second_lookup_uses_cache(self):
        engine = self._engine()
        asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        first = len(self.calls)
        asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        self.assertEqual(len(self.calls), first, "önbellek ikinci sorguda yeniden indirmemeli")

    def test_short_code_rejected(self):
        engine = self._engine()
        with self.assertRaises(ValueError):
            asyncio.run(engine.lookup("8517"))

    def test_unknown_jurisdiction_rejected(self):
        engine = self._engine()
        with self.assertRaises(ValueError):
            asyncio.run(engine.lookup("851713000000", jurisdiction="us"))

    def test_sync_activates_first_snapshot_and_is_idempotent(self):
        engine = self._engine()
        status = asyncio.run(engine.sync(force=True))
        self.assertTrue(status["ready"])
        self.assertEqual(status["chapter_count"], 3)
        self.assertEqual(status["pending_review_count"], 0)
        first = status["active_snapshot"]
        again = asyncio.run(engine.sync(force=True))
        self.assertEqual(again["active_snapshot"], first, "aynı sha yeni anlık görüntü üretmemeli")

    def test_strict_review_mode_keeps_snapshot_inactive(self):
        engine = self._engine(policy=ReviewPolicy(mode="strict"))
        status = asyncio.run(engine.sync(force=True))
        self.assertFalse(status["ready"])
        self.assertEqual(status["pending_review_count"], 1)
        pending = engine.pending_reviews()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "foreign_tariff")
        reviewed = engine.review_snapshot(pending[0]["snapshot_id"], "approve", reviewed_by="editor@example.com")
        self.assertEqual(reviewed["status"], "approved")
        self.assertTrue(engine.status()["ready"])

    def test_rejected_snapshot_stays_inactive(self):
        engine = self._engine(policy=ReviewPolicy(mode="strict"))
        asyncio.run(engine.sync(force=True))
        snapshot_id = engine.pending_reviews()[0]["snapshot_id"]
        engine.review_snapshot(snapshot_id, "reject", reviewed_by="editor@example.com", note="hatalı")
        self.assertFalse(engine.status()["ready"])
        self.assertEqual(engine.pending_reviews(), [])

    def test_corpus_rows_after_sync(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        rows = engine.corpus_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["corpus"], "foreign_tariff")
        self.assertTrue(rows[0]["source_url"].startswith("https://"))

    def test_sync_error_is_recorded_not_raised(self):
        engine = self._engine(chapters={"data": []})
        status = asyncio.run(engine.sync(force=True))
        self.assertFalse(status["ready"])
        self.assertTrue(status["errors"])

    def test_non_json_response_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>bot wall</html>", headers={"content-type": "text/html"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine = ft.ForeignTariffEngine(self._tmp.name, http=client, base_url="https://www.trade-tariff.service.gov.uk/api/v2")
        self.addCleanup(lambda: asyncio.run(engine.close()))
        result = asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        self.assertEqual(result.results[0].match_quality, "unavailable")

    def test_redirect_outside_allow_list_is_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://evil.test/data.json"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine = ft.ForeignTariffEngine(self._tmp.name, http=client, base_url="https://www.trade-tariff.service.gov.uk/api/v2")
        self.addCleanup(lambda: asyncio.run(engine.close()))
        result = asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        self.assertEqual(result.results[0].match_quality, "unavailable")

    def test_ledger_batch_recorded(self):
        recorded: list[dict] = []

        class FakeLedger:
            def record_batch(self, **kwargs):
                recorded.append(kwargs)
                return "batch-1"

            def has_batch(self, batch_id):
                return False

        engine = self._engine()
        engine.ledger = FakeLedger()
        asyncio.run(engine.sync(force=True))
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["kind"], "foreign_tariff")
        self.assertEqual(recorded[0]["review_status"], "approved")


if __name__ == "__main__":
    unittest.main()
