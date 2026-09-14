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


CH_HEADER = (
    "tn2;tn2_txt_d;tn2_txt_e;tn2_txt_f;tn2_validfrom;tn2_validto;tn2_trafficdirection;"
    "tn4;tn4_txt_d;tn4_txt_e;tn4_txt_f;tn4_validfrom;tn4_validto;"
    "tn6;tn6_txt_d;tn6_txt_e;tn6_txt_f;tn6_validfrom;tn6_validto;"
    "tn8;tn8_istotalbusinesscycle;tn8_txt_d;tn8_txt_e;tn8_txt_f;tn8_validfrom;tn8_validto;Update"
)


def _ch_row(tn8: str, tn6: str, german: str, english: str) -> str:
    return (
        f"85;\"Elektrische Maschinen\";\"Electrical machinery\";\"Machines électriques\";"
        "1988-01-01T00:00:00.000+01:00;2999-12-31T00:00:00.000+01:00;B;"
        f"8517;\"Fernsprechapparate\";\"Telephone sets\";\"Postes téléphoniques\";"
        "1996-01-01T00:00:00.000+01:00;2999-12-31T00:00:00.000+01:00;"
        f"{tn6};\"{german}\";\"{english}\";\"Téléphones\";"
        "2022-01-01T00:00:00.000+01:00;2999-12-31T00:00:00.000+01:00;"
        f"{tn8};J;\"{german}\";\"{english}\";\"Téléphones\";"
        "2022-01-01T00:00:00.000+01:00;2999-12-31T00:00:00.000+01:00;2026-08-12"
    )


def _ch_csv() -> str:
    return "\n".join([
        CH_HEADER,
        _ch_row("8517.1300", "8517.13", "Smartphones für zellulare Netze", "Smartphones for cellular networks"),
        _ch_row("8517.1400", "8517.14", "Andere Telefone", "Other telephones"),
        _ch_row("8471.3000", "8471.30", "Tragbare Maschinen", "Portable data processing machines"),
    ]) + "\n"


def _section(section: int) -> dict:
    """``/goods_nomenclatures/section/{n}`` gövdesi: yalnız nomenklatür, oran yok."""
    base = 8500 + section
    data = []
    for index in range(600):  # 21 bölüm × 600 = eşik üstü
        code = f"{base}{index:06d}"[:10]
        data.append(
            {
                "id": str(index),
                "type": "goods_nomenclature",
                "attributes": {
                    "goods_nomenclature_item_id": code,
                    "formatted_description": f"Section {section} item {index}",
                    "declarable": True,
                    "validity_start_date": "2022-01-01T00:00:00.000Z",
                    "validity_end_date": None,
                },
                "relationships": {"parent": {"data": None}},
            }
        )
    if section == 16:
        data.append(
            {
                "id": "smart",
                "type": "goods_nomenclature",
                "attributes": {
                    "goods_nomenclature_item_id": "8517130000",
                    "formatted_description": "Smartphones",
                    "declarable": True,
                    "validity_start_date": "2022-01-01T00:00:00.000Z",
                    "validity_end_date": None,
                },
                "relationships": {"parent": {"data": None}},
            }
        )
    return {"data": data}


def _transport(calls: list[str], chapters: dict | None = None, swiss: str | None = None,
               commodity_status: int | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if "/goods_nomenclatures/section/" in path:
            return httpx.Response(200, json=_section(int(path.rsplit("/", 1)[-1])))
        if commodity_status and "/commodities/" in path:
            return httpx.Response(commodity_status, json={"error": "down"})
        if "TN_STRUCTURE" in path:
            body = _ch_csv() if swiss is None else swiss
            return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": "text/csv"})
        if path.endswith("/chapters"):
            return httpx.Response(200, json=chapters if chapters is not None else _chapters())
        if "/headings/8517" in path:
            return httpx.Response(200, json=_heading())
        if "/commodities/" in path:
            return httpx.Response(200, json=_commodity())
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


class SchemaMigrationTests(unittest.TestCase):
    """Canlıda yakalandı: mevcut kurulumda tablo zaten vardı ve yeni sütunlar eklenmemişti."""

    def test_existing_database_gains_new_columns(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "foreign_tariff.sqlite3"
            # PR #25'teki eski şema: description_alt / valid_from / valid_to yok.
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE nomenclature (
                        snapshot_id TEXT NOT NULL, jurisdiction TEXT NOT NULL, kind TEXT NOT NULL,
                        code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                        source_url TEXT NOT NULL DEFAULT '', PRIMARY KEY (snapshot_id, kind, code)
                    );
                    """
                )
            store = ft.ForeignTariffStore(tmp)
            with store.connect() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(nomenclature)").fetchall()}
            self.assertIn("description_alt", columns)
            self.assertIn("valid_from", columns)
            self.assertIn("valid_to", columns)

    def test_migration_preserves_existing_rows(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "foreign_tariff.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE nomenclature (
                        snapshot_id TEXT NOT NULL, jurisdiction TEXT NOT NULL, kind TEXT NOT NULL,
                        code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                        source_url TEXT NOT NULL DEFAULT '', PRIMARY KEY (snapshot_id, kind, code)
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO nomenclature(snapshot_id,jurisdiction,kind,code,description) VALUES('s','uk','chapter','01','Live animals')"
                )
            store = ft.ForeignTariffStore(tmp)
            with store.connect() as connection:
                row = connection.execute("SELECT description, description_alt FROM nomenclature WHERE code='01'").fetchone()
            self.assertEqual(row["description"], "Live animals")
            self.assertEqual(row["description_alt"], "")


class SwissParserTests(unittest.TestCase):
    def test_parse_swiss_nomenclature(self):
        rows = ft.parse_swiss_nomenclature(_ch_csv())
        self.assertEqual([row["code"] for row in rows], ["84713000", "85171300", "85171400"])
        smartphone = next(row for row in rows if row["code"] == "85171300")
        self.assertEqual(smartphone["description"], "Smartphones for cellular networks")
        self.assertEqual(smartphone["description_alt"], "Smartphones für zellulare Netze")
        self.assertEqual(smartphone["hs6"], "851713")
        self.assertEqual(smartphone["valid_from"], "2022-01-01")

    def test_parse_swiss_rejects_wrong_columns(self):
        with self.assertRaises(ValueError):
            ft.parse_swiss_nomenclature("a;b;c\n1;2;3\n")

    def test_parse_swiss_rejects_empty(self):
        with self.assertRaises(ValueError):
            ft.parse_swiss_nomenclature(CH_HEADER + "\n")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[str] = []

    def _engine(self, *, chapters: dict | None = None, swiss: str | None = None, policy: ReviewPolicy | None = None,
                commodity_status: int | None = None) -> ft.ForeignTariffEngine:
        client = httpx.AsyncClient(
            transport=_transport(self.calls, chapters, swiss, commodity_status),
            base_url="https://www.trade-tariff.service.gov.uk",
        )
        engine = ft.ForeignTariffEngine(
            self._tmp.name,
            http=client,
            review_policy=policy or ReviewPolicy(),
            base_url="https://www.trade-tariff.service.gov.uk/api/v2",
        )
        engine.measures_delay_seconds = 0.0
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
        self.assertFalse(status["swiss_ready"])
        # Üç veri seti, üç bekleyen anlık görüntü.
        self.assertEqual(status["pending_review_count"], 3)
        pending = engine.pending_reviews()
        self.assertEqual({item["kind"] for item in pending}, {"foreign_tariff"})
        uk = next(item for item in pending if item["source_id"] == ft.UK_DATASET)
        reviewed = engine.review_snapshot(uk["snapshot_id"], "approve", reviewed_by="editor@example.com")
        self.assertEqual(reviewed["status"], "approved")
        self.assertTrue(engine.status()["ready"])
        # İsviçre hâlâ onay bekler; biri diğerini aktifleştirmez.
        self.assertFalse(engine.status()["swiss_ready"])

    def test_rejected_snapshot_stays_inactive(self):
        engine = self._engine(policy=ReviewPolicy(mode="strict"))
        asyncio.run(engine.sync(force=True))
        for item in engine.pending_reviews():
            engine.review_snapshot(item["snapshot_id"], "reject", reviewed_by="editor@example.com", note="hatalı")
        status = engine.status()
        self.assertFalse(status["ready"])
        self.assertFalse(status["swiss_ready"])
        self.assertEqual(engine.pending_reviews(), [])

    def test_corpus_rows_after_sync(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        rows = engine.corpus_rows()
        # UK faslı + UK tam nomenklatür + İsviçre tarife numarası.
        self.assertEqual({row["corpus"] for row in rows}, {"foreign_tariff"})
        self.assertTrue(all(row["source_url"].startswith("https://") for row in rows))
        prefixes = {row["id"].rsplit("-", 1)[0] for row in rows}
        self.assertEqual(prefixes, {ft.UK_DATASET, ft.UK_NOMENCLATURE_DATASET, ft.CH_DATASET})

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

    def test_swiss_sync_returns_real_codes(self):
        engine = self._engine()
        status = asyncio.run(engine.sync(force=True))
        self.assertTrue(status["swiss_ready"])
        self.assertEqual(status["swiss_code_count"], 3)

        result = asyncio.run(engine.lookup("851713000000", jurisdiction="ch"))
        swiss = result.results[0]
        self.assertEqual(swiss.data_kind, "nomenclature")
        self.assertEqual(swiss.match_quality, "exact_hs6")
        self.assertEqual(swiss.matched_code, "85171300")
        self.assertEqual(swiss.description, "Smartphones for cellular networks")
        self.assertTrue(swiss.links, "resmî Tares bağlantısı yine verilmeli")
        self.assertTrue(any("vergi oranı yayımlanmaz" in note for note in swiss.notes))

    def test_swiss_unknown_code_keeps_links(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        swiss = asyncio.run(engine.lookup("999999000000", jurisdiction="ch")).results[0]
        self.assertEqual(swiss.match_quality, "unavailable")
        self.assertIsNone(swiss.matched_code)
        self.assertTrue(swiss.links)

    def test_new_dataset_is_not_blocked_by_the_other_stamp(self):
        """Yeni eklenen bir veri seti, diğerinin taze damgası yüzünden beklememeli."""
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        # İsviçre verisini sıfırla: kurulumda UK zaten eşitlenmiş gibi davran.
        with engine.store.connect() as connection:
            connection.execute("DELETE FROM snapshots WHERE dataset=?", (ft.CH_DATASET,))
            connection.execute("DELETE FROM metadata WHERE key LIKE 'last_checked_at%'")
        engine.store.set_metadata(f"last_checked_at:{ft.UK_DATASET}", ft._now())
        engine.store.set_metadata("last_checked_at", ft._now())
        status = asyncio.run(engine.sync())  # force YOK
        self.assertTrue(status["swiss_ready"], "hiç eşitlenmemiş veri seti hemen çekilmeli")

    def test_recent_stamp_skips_resync(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        before = len(self.calls)
        asyncio.run(engine.sync())  # force YOK, ikisi de taze
        self.assertEqual(len(self.calls), before, "aralık dolmadan yeniden indirilmemeli")

    def test_swiss_corpus_rows_included(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        corpora = {row["id"].split("-")[0] for row in engine.corpus_rows()}
        self.assertIn("uk_chapters", corpora)
        self.assertIn("ch_nomenclature", corpora)

    def test_uk_failure_does_not_block_swiss(self):
        engine = self._engine(chapters={"data": []})
        status = asyncio.run(engine.sync(force=True))
        self.assertFalse(status["ready"])
        self.assertTrue(status["swiss_ready"], "UK hatası İsviçre eşitlemesini engellememeli")
        self.assertTrue(any(item.startswith("UK:") for item in status["errors"]))

    def test_full_nomenclature_is_stored_locally(self):
        engine = self._engine()
        status = asyncio.run(engine.sync(force=True))
        # 21 bölüm × 600 satır + 8517130000 (bölüm 16), kod çakışmaları tekilleştirilir.
        self.assertGreater(status["uk_code_count"], 1000)

    def test_lookup_uses_local_nomenclature_without_heading_call(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        self.calls.clear()
        result = asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        uk = result.results[0]
        self.assertEqual(uk.matched_code, "8517130000")
        self.assertEqual(uk.third_country_duty, "0.00%")
        self.assertFalse(any("/headings/" in call for call in self.calls), "aday kodlar yerelden gelmeli")

    def test_measures_are_archived_and_reused_without_network(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        self.assertEqual(engine.status()["archived_commodities"], 1)
        self.assertGreater(engine.status()["archived_measures"], 0)
        self.calls.clear()
        result = asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        self.assertEqual(self.calls, [], "taze arşiv varken ağa çıkılmamalı")
        self.assertEqual(result.results[0].third_country_duty, "0.00%")

    def test_archive_serves_last_known_rates_when_uk_is_down(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        asyncio.run(engine.lookup("851713000000", jurisdiction="uk"))
        # Arşivi yaşlandır ve kaynağı düşür.
        with engine.store.connect() as connection:
            connection.execute("UPDATE commodity_meta SET fetched_at='2020-01-01T00:00:00+00:00'")
        engine._http = httpx.AsyncClient(
            transport=_transport(self.calls, commodity_status=503),
            base_url="https://www.trade-tariff.service.gov.uk",
        )
        uk = asyncio.run(engine.lookup("851713000000", jurisdiction="uk")).results[0]
        self.assertEqual(uk.third_country_duty, "0.00%", "son bilinen oran sunulmalı")
        self.assertTrue(any("son bilinen oranlar" in note for note in uk.notes))

    def test_warm_archive_fills_missing_codes(self):
        engine = self._engine()
        asyncio.run(engine.sync(force=True))
        written = asyncio.run(engine.warm_measures_archive(limit=3))
        self.assertGreaterEqual(written, 1)
        self.assertGreaterEqual(engine.status()["archived_commodities"], 1)

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
        # Her veri seti kendi defter kaydını yazar.
        self.assertEqual(
            {item["source_id"] for item in recorded},
            {ft.UK_DATASET, ft.UK_NOMENCLATURE_DATASET, ft.CH_DATASET},
        )
        self.assertTrue(all(item["kind"] == "foreign_tariff" for item in recorded))
        self.assertTrue(all(item["review_status"] == "approved" for item in recorded))


if __name__ == "__main__":
    unittest.main()
