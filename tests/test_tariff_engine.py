from __future__ import annotations

import io
import tempfile
import unittest
import zipfile

from openpyxl import Workbook

from tariff_engine import (
    LandedCostInput,
    TariffEngine,
    _number,
    _zip_name,
    calculate_landed_cost,
)


def _xlsx(rows: list[list[object]], title: str = "84") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


class TariffParsingTests(unittest.TestCase):
    def test_numeric_zero_is_a_real_rate(self) -> None:
        self.assertEqual(_number(0), (0.0, "0"))

    def test_turkish_dos_zip_name_is_decoded(self) -> None:
        info = zipfile.ZipInfo("ÿGV/EK-1.xlsx")
        self.assertEqual(_zip_name(info), "İGV/EK-1.xlsx")

    def test_official_industry_columns_are_parsed_with_provenance(self) -> None:
        workbook = _xlsx(
            [
                ["84. FASIL"],
                ["GTİP", "DİPNOT", "1", "2", "3", "4", "5", "6", "7"],
                ["840120001011", None, 0, 3.7, 0, 0, 0, 0, 3.7],
            ]
        )
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("II Sayılı Liste (84-85-90-97. Fasıllar).xlsx", workbook)
        parsed = TariffEngine._parse_archive(
            archive.getvalue(),
            {
                "id": "import_regime",
                "title": "İthalat Rejimi",
                "landing_url": "https://ticaret.gov.tr/test",
                "measure_family": "customs_duty",
                "valid_from": "2026-01-01",
            },
            "https://ticaret.gov.tr/data/test.zip",
            "a" * 64,
            "2026-08-28T00:00:00+03:00",
        )
        rows = [item for item in parsed.measures if item["gtip"] == "840120001011"]
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["rate"], 0.0)
        self.assertEqual(rows[-1]["country_group"], "7")
        self.assertEqual(rows[-1]["source_row"], 3)

    def test_country_column_resolution_is_deterministic(self) -> None:
        labels = {"1", "2", "3", "4", "5", "6", "7"}
        self.assertEqual(TariffEngine._matching_group("Almanya", labels, {}, "840120001011")[0], "1")
        self.assertEqual(TariffEngine._matching_group("Çin", labels, {}, "840120001011")[0], "7")
        self.assertEqual(TariffEngine._matching_group("Katar", labels, {}, "840120001011")[0], "2")
        self.assertEqual(TariffEngine._matching_group("Güney Kore", labels, {}, "840120001011")[0], "1")
        self.assertEqual(TariffEngine._matching_group("BAE", labels, {}, "840120001011")[0], "3")

    def test_labelled_columns_are_matched_on_whole_tokens_in_every_order(self) -> None:
        iv_list = ["EFTA/B-HER/F.ADA", "AB/BK", "G.KORE", "MLZ", "SNG", "KOS", "VNZ", "BAE", "TPS-OIC", "D-8", "DÜ"]
        expectations = {
            "Almanya": "AB/BK", "Birleşik Krallık": "AB/BK", "Norveç": "EFTA/B-HER/F.ADA",
            "Bosna-Hersek": "EFTA/B-HER/F.ADA", "Güney Kore": "G.KORE", "Malezya": "MLZ",
            "Singapur": "SNG", "Kosova": "KOS", "Venezuela": "VNZ", "BAE": "BAE", "Çin": "DÜ",
        }
        for ordering in (iv_list, list(reversed(iv_list)), sorted(iv_list, key=len)):
            labels = set(ordering)
            for origin, expected in expectations.items():
                with self.subTest(origin=origin):
                    self.assertEqual(TariffEngine._matching_group(origin, labels, {}, "030211000000")[0], expected)

    def test_own_column_wins_over_shared_eu_column(self) -> None:
        # In the III list Korea has its own column even though the shared column mentions G.KORE elsewhere.
        labels = {"AB/BK/B-HER/EFTA/F.ADA", "G.KORE", "MLZ", "SNG", "KOS", "İRAN", "VNZ", "BAE", "EAGÜ", "ÖTDÜ", "GYÜ", "DÜ"}
        self.assertEqual(TariffEngine._matching_group("Güney Kore", labels, {}, "170490100000")[0], "G.KORE")
        self.assertEqual(TariffEngine._matching_group("Bosna Hersek", labels, {}, "170490100000")[0], "AB/BK/B-HER/EFTA/F.ADA")
        self.assertEqual(TariffEngine._matching_group("İsviçre", labels, {}, "170490100000")[0], "AB/BK/B-HER/EFTA/F.ADA")
        self.assertEqual(TariffEngine._matching_group("İran", labels, {}, "170490100000")[0], "İRAN")

    def test_fta_country_without_a_column_is_not_attached_to_the_eu(self) -> None:
        agricultural = {"AB/BK", "GÜR", "B-HER", "G.KORE", "MLZ", "SNG", "KOS", "VNZ", "BAE", "TPS-OIC", "D-8", "DÜ"}
        group, warnings = TariffEngine._matching_group("Fas", agricultural, {}, "070200000011")
        self.assertEqual(group, "DÜ")
        self.assertTrue(any("ayrı tercihli sütun yok" in item for item in warnings))
        group, warnings = TariffEngine._matching_group("Norveç", agricultural, {}, "070200000011")
        self.assertEqual(group, "DÜ")
        self.assertTrue(warnings)

    def test_emy_column_is_only_selected_for_its_own_countries(self) -> None:
        emy_labels = {"EFTA/F.ADA"}
        self.assertEqual(
            TariffEngine._matching_group("Norveç", emy_labels, {}, "030211000000", measure_type="additional_financial_liability")[0],
            "EFTA/F.ADA",
        )
        group, warnings = TariffEngine._matching_group(
            "Almanya", emy_labels, {}, "030211000000", measure_type="additional_financial_liability"
        )
        self.assertIsNone(group)
        self.assertEqual(warnings, [])

    def test_roman_numeral_lists_are_told_apart(self) -> None:
        cases = {
            "I SAYILI LİSTE.xlsx": ("I Sayılı Liste", None),
            "II SAYILI LİSTE.xlsx": ("II Sayılı Liste (Sanayi)", None),
            "III SAYILI LİSTE.xlsx": ("III Sayılı Liste", None),
            "IV SAYILI LİSTE.xlsx": ("IV Sayılı Liste", None),
            "V SAYILI LİSTE.xlsx": ("V Sayılı Liste", "customs_duty_suspension"),
            "VI SAYILI LİSTE.xlsx": ("VI Sayılı Liste", "customs_duty_end_use"),
            "VII SAYILI LİSTE.xlsx": ("VII Sayılı Liste", "customs_duty_end_use"),
        }
        for file_name, (list_name, override) in cases.items():
            with self.subTest(file_name=file_name):
                _, groups, name, override_type = TariffEngine._group_map(file_name, "Sheet1", "customs_duty")
                self.assertEqual((name, override_type), (list_name, override))
                self.assertTrue(groups)
        _, groups, _, _ = TariffEngine._group_map("IV SAYILI LİSTE.xlsx", "Sheet1", "customs_duty")
        self.assertEqual(groups[13], "EFTA/F.ADA")

    def test_igv_annex_sheets_accept_official_sheet_names(self) -> None:
        for sheet in ("EK-2", "Ek 2", "EK2", "ek-2 tarım"):
            with self.subTest(sheet=sheet):
                self.assertEqual(TariffEngine._group_map("IGV Ekler.xlsx", sheet, "additional_duty")[2], "İGV Ek-2")
        self.assertEqual(TariffEngine._group_map("IGV Ekler.xlsx", "EK-3", "additional_duty")[2], "İGV Ek-3")
        self.assertEqual(TariffEngine._group_map("IGV Ekler.xlsx", "EK-20", "additional_duty")[2], "")
        self.assertEqual(TariffEngine._group_map("IGV Ekler.xlsx", "Çalışma", "additional_duty")[2], "")


class LandedCostTests(unittest.TestCase):
    def test_unknown_input_fields_are_rejected(self) -> None:
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            LandedCostInput.model_validate({"invoice_value": 1000, "customs_duty_rat": 30})

    def test_missing_rates_block_the_total(self) -> None:
        result = calculate_landed_cost(LandedCostInput(invoice_value=1000, vat_rate=20))
        self.assertEqual(result.status, "partial")
        self.assertIsNone(result.landed_total)
        self.assertIn("Gümrük vergisi oranı", result.missing_rates)
        self.assertIn("Damping/sübvansiyon önlemi (uygulanmıyorsa 0)", result.missing_rates)
        self.assertIn("Gözetim birim kıymeti (uygulanmıyorsa 0)", result.missing_rates)

    def test_full_formula_is_reproducible(self) -> None:
        result = calculate_landed_cost(
            LandedCostInput(
                invoice_value=1000,
                freight=100,
                insurance=10,
                other_costs=20,
                quantity=10,
                customs_duty_rate=10,
                additional_duty_rate=5,
                additional_financial_liability_rate=0,
                anti_dumping_amount=0,
                kkdf_rate=0,
                vat_rate=20,
                sct_amount=0,
                surveillance_unit_value=0,
            )
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.customs_value, 1110)
        self.assertEqual(result.landed_total, 1555.8)
        self.assertEqual(result.unit_landed_cost, 155.58)

    def test_surveillance_uplift_requires_declared_certificate_state(self) -> None:
        result = calculate_landed_cost(
            LandedCostInput(
                invoice_value=1000,
                quantity=100,
                surveillance_unit_value=20,
                has_surveillance_certificate=False,
                quantity_unit="kg",
                surveillance_unit="kg",
                customs_duty_rate=0,
                additional_duty_rate=0,
                additional_financial_liability_rate=0,
                anti_dumping_amount=0,
                kkdf_rate=0,
                vat_rate=20,
                sct_amount=0,
            )
        )
        self.assertEqual(result.customs_value, 2000)
        self.assertTrue(result.warnings)


class TariffPrefixLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp_dir.name)
        with self.engine._connect() as db:
            for snapshot_id, source_id, title in (
                ("import-2026", "import_regime", "İthalat Rejimi"),
                ("igv-2026", "additional_duty", "İlave Gümrük Vergisi"),
            ):
                db.execute(
                    """
                    INSERT INTO tariff_snapshots
                    (id,source_id,source_title,landing_url,archive_url,archive_sha256,retrieved_at,
                     checked_at,valid_from,measure_count,active,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,'{}')
                    """,
                    (
                        snapshot_id, source_id, title, "https://ticaret.gov.tr/test",
                        "https://ticaret.gov.tr/test.zip", "a" * 64,
                        "2026-08-29T00:00:00+03:00", "2026-08-29T00:00:00+03:00",
                        "2026-01-01", 2,
                    ),
                )

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp_dir.cleanup()

    def _insert_measure(
        self,
        row_id: str,
        snapshot_id: str,
        gtip: str,
        measure_type: str,
        rate: float,
        country_group: str,
    ) -> None:
        with self.engine._connect() as db:
            db.execute(
                """
                INSERT INTO tariff_measures
                (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,
                 country_group_description,footnote,description,condition_text,list_name,
                 source_file,source_sheet,source_row,automatic_calculation_allowed)
                VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,1)
                """,
                (
                    row_id, snapshot_id, gtip, measure_type, rate, str(rate), country_group,
                    "Diğer Ülkeler", "Test listesi", "test.xlsx", "84", 3,
                ),
            )

    async def test_six_digit_prefix_uses_only_rates_shared_by_every_subline(self) -> None:
        self._insert_measure("c1", "import-2026", "123456001111", "customs_duty", 10, "7")
        self._insert_measure("c2", "import-2026", "123456002222", "customs_duty", 10, "7")
        self._insert_measure("a1", "igv-2026", "123456001111", "additional_duty", 20, "DÜ")

        result = await self.engine.lookup("123456", origin_country="Çin", auto_sync=False)

        self.assertEqual(result.match_mode, "prefix")
        self.assertEqual(result.matched_gtip_count, 2)
        self.assertEqual(result.unambiguous_rates["customs_duty"], 10)
        self.assertNotIn("additional_duty", result.unambiguous_rates)
        self.assertIn("additional_duty", result.ambiguous_measure_types)
        self.assertTrue(any(item.country_group == "DÜ" for item in result.measures))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.measure_coverage["anti_dumping"].status, "not_integrated")
        self.assertIn("anti_dumping", result.unresolved_measure_types)

    async def test_eight_digit_prefix_calculates_when_all_sublines_share_rates(self) -> None:
        for suffix, row in (("0011", "1"), ("0022", "2")):
            gtip = f"87654321{suffix}"
            self._insert_measure(f"c{row}", "import-2026", gtip, "customs_duty", 5, "7")
            self._insert_measure(f"a{row}", "igv-2026", gtip, "additional_duty", 12, "DÜ")

        result = await self.engine.lookup("87654321", origin_country="Çin", auto_sync=False)

        self.assertEqual(result.unambiguous_rates, {"customs_duty": 5.0, "additional_duty": 12.0})
        self.assertEqual(result.ambiguous_measure_types, [])
        self.assertEqual(result.status, "matched")

    async def test_duty_and_emy_columns_are_both_primary_for_the_same_gtip(self) -> None:
        with self.engine._connect() as db:
            for row_id, group, measure_type, rate in (
                ("f1", "EFTA/B-HER/F.ADA", "customs_duty", 0), ("f2", "AB/BK", "customs_duty", 0),
                ("f3", "DÜ", "customs_duty", 30), ("f4", "EFTA/F.ADA", "additional_financial_liability", 15),
            ):
                db.execute(
                    """INSERT INTO tariff_measures
                    (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,country_group_description,
                     footnote,description,condition_text,list_name,source_file,source_sheet,source_row,
                     automatic_calculation_allowed) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,1)""",
                    (row_id, "import-2026", "030211000000", measure_type, rate, str(rate), group, group,
                     "IV Sayılı Liste", "IV SAYILI LİSTE.xlsx", "03", 4),
                )
        norway = await self.engine.lookup("030211000000", origin_country="Norveç")
        self.assertEqual(norway.unambiguous_rates, {"customs_duty": 0.0, "additional_financial_liability": 15.0})
        germany = await self.engine.lookup("030211000000", origin_country="Almanya")
        self.assertEqual(germany.unambiguous_rates, {"customs_duty": 0.0})
        self.assertEqual(germany.resolved_country_group, "AB/BK")
        china = await self.engine.lookup("030211000000", origin_country="Çin")
        self.assertEqual(china.unambiguous_rates, {"customs_duty": 30.0})

    async def test_third_country_goods_from_the_eu_take_atr_duty_but_origin_igv(self) -> None:
        self._insert_measure("d1", "import-2026", "691110000011", "customs_duty", 0, "1")
        self._insert_measure("d2", "import-2026", "691110000011", "customs_duty", 12, "7")
        self._insert_measure("i1", "igv-2026", "691110000011", "additional_duty", 0, "1")
        self._insert_measure("i2", "igv-2026", "691110000011", "additional_duty", 19, "7")
        direct = await self.engine.lookup("691110000011", origin_country="Çin")
        self.assertEqual(direct.unambiguous_rates, {"customs_duty": 12.0, "additional_duty": 19.0})
        self.assertFalse(direct.atr_free_circulation)
        # A.TR teyit edilmeden yalnız AB'den sevk edilmiş olmak serbest dolaşım sütununu açmaz.
        unconfirmed = await self.engine.lookup("691110000011", origin_country="Çin", dispatch_country="Almanya")
        self.assertFalse(unconfirmed.atr_free_circulation)
        self.assertTrue(unconfirmed.atr_available)
        self.assertEqual(unconfirmed.unambiguous_rates, {"customs_duty": 12.0, "additional_duty": 19.0})
        self.assertTrue(any("A.TR teyit edilmediği için" in item for item in unconfirmed.warnings))
        via_eu = await self.engine.lookup(
            "691110000011", origin_country="Çin", dispatch_country="Almanya", atr_certificate=True
        )
        self.assertTrue(via_eu.atr_free_circulation)
        self.assertEqual(via_eu.unambiguous_rates, {"customs_duty": 0.0, "additional_duty": 19.0})
        self.assertTrue(any("A.TR beyan edildiği için" in item for item in via_eu.warnings))
        declined = await self.engine.lookup(
            "691110000011", origin_country="Çin", dispatch_country="Almanya", atr_certificate=False
        )
        self.assertFalse(declined.atr_free_circulation)
        self.assertEqual(declined.unambiguous_rates["customs_duty"], 12.0)

    async def test_agricultural_goods_from_the_eu_get_no_atr_relief(self) -> None:
        self._insert_measure("a1", "import-2026", "070200000011", "customs_duty", 0, "1")
        self._insert_measure("a2", "import-2026", "070200000011", "customs_duty", 48.6, "7")
        result = await self.engine.lookup(
            "070200000011", origin_country="Çin", dispatch_country="Almanya", atr_certificate=True
        )
        self.assertFalse(result.atr_free_circulation)
        self.assertFalse(result.atr_available)
        self.assertEqual(result.unambiguous_rates, {"customs_duty": 48.6})
        self.assertTrue(any("A.TR düzenlenmez" in item for item in result.warnings))

    async def test_eu_origin_igv_relief_is_flagged_as_proof_dependent(self) -> None:
        self._insert_measure("e1", "igv-2026", "691110000011", "additional_duty", 0, "1")
        self._insert_measure("e2", "igv-2026", "691110000011", "additional_duty", 19, "7")
        result = await self.engine.lookup("691110000011", origin_country="Almanya")
        self.assertEqual(result.unambiguous_rates, {"additional_duty": 0.0})
        self.assertEqual(result.origin_proof_required, ["additional_duty"])
        self.assertEqual(result.fallback_rates, {"additional_duty": 19.0})
        self.assertTrue(any("tedarikçi beyanı" in item for item in result.warnings))

    async def test_unknown_origin_is_warned_not_silently_residual(self) -> None:
        self._insert_measure("u1", "import-2026", "691110000011", "customs_duty", 12, "7")
        result = await self.engine.lookup("691110000011", origin_country="Almanyaa")
        self.assertFalse(result.origin_recognised)
        self.assertTrue(any("bilinen ülke listesinde bulunamadı" in item for item in result.warnings))
        english = await self.engine.lookup("691110000011", origin_country="Germany")
        self.assertTrue(english.origin_recognised)

    async def test_user_rate_differing_from_official_rate_is_reported(self) -> None:
        self._insert_measure("o1", "import-2026", "691110000011", "customs_duty", 12, "7")
        result = await self.engine.calculate(
            "691110000011", "Çin",
            LandedCostInput(invoice_value=1000, customs_duty_rate=0, additional_duty_rate=0, additional_financial_liability_rate=0,
                            vat_rate=20, anti_dumping_amount=0, sct_amount=0, surveillance_unit_value=0, payment_method="peşin"),
        )
        cost = result["cost"]
        self.assertEqual(cost["rate_overrides"], [{"measure_type": "customs_duty", "user_rate": 0.0, "official_rate": 12.0}])
        self.assertTrue(any("resmî satırdaki %12" in item for item in cost["warnings"]))
        self.assertEqual(cost["lines"][1]["rate"], 0.0)

    async def test_decision_tree_exposes_each_level_without_auto_selecting(self) -> None:
        for suffix, rate in (("001111", 5), ("002222", 7), ("991111", 9)):
            gtip = f"123456{suffix}"
            self._insert_measure(f"tree-{suffix}", "import-2026", gtip, "customs_duty", rate, "7")

        hs6 = await self.engine.decision_tree("123456", origin_country="Çin", auto_sync=False)
        self.assertEqual([child.code for child in hs6.children], ["12345600", "12345699"])
        self.assertTrue(hs6.requires_user_selection)
        self.assertFalse(hs6.exact_gtip_selected)
        self.assertEqual(hs6.children[0].descendant_count, 2)

        cn8 = await self.engine.decision_tree("12345600", origin_country="Çin", auto_sync=False)
        self.assertEqual([child.code for child in cn8.children], ["1234560011", "1234560022"])
        self.assertEqual(cn8.next_level, "TR10")

        tr10 = await self.engine.decision_tree("1234560011", origin_country="Çin", auto_sync=False)
        self.assertEqual([child.code for child in tr10.children], ["123456001111"])
        self.assertTrue(tr10.children[0].final)

        exact = await self.engine.decision_tree("123456001111", origin_country="Çin", auto_sync=False)
        self.assertTrue(exact.exact_gtip_selected)
        self.assertFalse(exact.requires_user_selection)
        self.assertEqual(exact.children, [])

    async def test_four_digit_heading_decision_tree(self) -> None:
        self._insert_measure("h1", "import-2026", "691110000011", "customs_duty", 12, "7")
        self._insert_measure("h2", "import-2026", "691190000012", "customs_duty", 12, "7")
        tree = await self.engine.decision_tree("6911", origin_country="Çin", auto_sync=False)
        self.assertEqual(tree.level, "HS4")
        self.assertEqual(tree.next_level, "HS6")
        self.assertEqual(tree.total_children, 2)
        child_codes = [child.code for child in tree.children]
        self.assertEqual(child_codes, ["691110", "691190"])
        self.assertEqual(tree.children[0].level, "HS6")

    async def test_unlisted_igv_defaults_to_zero_in_calculate(self) -> None:
        self._insert_measure("u1", "import-2026", "847130000000", "customs_duty", 0, "7")
        # No additional_duty measure row exists for this GTIP in igv-2026 snapshot
        inputs = LandedCostInput(
            invoice_value=1000,
            freight=50,
            insurance=10,
            vat_rate=20,
            payment_method="peşin",
            additional_financial_liability_rate=0,
            anti_dumping_amount=0,
            sct_amount=0,
            surveillance_unit_value=0,
        )
        calc = await self.engine.calculate("847130000000", "Çin", inputs)
        cost = calc["cost"]
        # Because it's not in the IGV decree, IGV defaults to 0% and does not block calculation
        self.assertEqual(cost["status"], "complete")
        igv_line = next(line for line in cost["lines"] if line["code"] == "additional_duty")
        self.assertEqual(igv_line["rate"], 0.0)
        self.assertEqual(igv_line["amount"], 0.0)


class LiraSummaryTests(unittest.TestCase):
    def _inputs(self, **overrides):
        base = dict(
            invoice_value=10000, freight=1200, insurance=50, other_costs=300, quantity=2000, currency="USD",
            customs_duty_rate=12, additional_duty_rate=19, additional_financial_liability_rate=0, anti_dumping_amount=0,
            sct_amount=0, surveillance_unit_value=0, vat_rate=20, payment_method="mal mukabili",
        )
        base.update(overrides)
        return LandedCostInput(**base)

    def test_lira_summary_adds_lira_only_costs_to_the_vat_base(self) -> None:
        result = calculate_landed_cost(self._inputs(exchange_rate=40, exchange_rate_date="2026-09-05", stamp_duty_try=1000, port_storage_try=20000, gekap_try=500))
        self.assertEqual(result.landed_total, 18765.0)
        summary = result.try_summary
        self.assertEqual(summary["status"], "complete")
        # vat_base 15637.5 USD × 40 = 625500 + 1000 + 20000
        self.assertEqual(summary["vat_base_try"], 646500.0)
        self.assertEqual(summary["vat_try"], 129300.0)
        # taxes excl. VAT: 1350 + 2137.5 + 600 = 4087.5 × 40 = 163500 + stamp 1000 + VAT
        self.assertEqual(summary["total_taxes_try"], 293800.0)
        self.assertEqual(summary["landed_total_try"], 646500.0 + 129300.0 + 500)
        self.assertEqual(summary["unit_landed_cost_try"], round((646500.0 + 129300.0 + 500) / 2000, 4))

    def test_trt_bandrol_enters_the_vat_base(self) -> None:
        without = calculate_landed_cost(self._inputs())
        with_bandrol = calculate_landed_cost(self._inputs(trt_bandrol_rate=10))
        bandrol_line = next(line for line in with_bandrol.lines if line["code"] == "trt_bandrol")
        # base: 11250 + 1350 + 2137.5 + 0 + 0 = 14737.5 → %10
        self.assertEqual(bandrol_line["amount"], 1473.75)
        self.assertEqual(with_bandrol.vat_base, round(without.vat_base + 1473.75, 2))
        self.assertEqual(with_bandrol.total_taxes, round(without.total_taxes + 1473.75 * 1.2, 2))

    def test_lira_costs_without_a_rate_only_warn(self) -> None:
        result = calculate_landed_cost(self._inputs(stamp_duty_try=1000))
        self.assertIsNone(result.try_summary)
        self.assertTrue(any("kur da girilmelidir" in item for item in result.warnings))

    def test_partial_ledger_gives_partial_lira_summary(self) -> None:
        result = calculate_landed_cost(self._inputs(vat_rate=None, exchange_rate=40))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.try_summary["status"], "partial")
        self.assertIsNone(result.try_summary["landed_total_try"])
        self.assertEqual(result.try_summary["customs_value_try"], 450000.0)


if __name__ == "__main__":
    unittest.main()
