from __future__ import annotations

import asyncio
import io
import tempfile
import unittest

from openpyxl import Workbook

from bulk_costing import calculate_rows, normalise_header, parse_number, rows_from_upload, template_csv
from tariff_engine import TariffEngine


class BulkParsingTests(unittest.TestCase):
    def test_turkish_and_plain_numbers(self) -> None:
        self.assertEqual(parse_number("1.234,56"), 1234.56)
        self.assertEqual(parse_number("12.500"), 12500.0)
        self.assertEqual(parse_number("12,5"), 12.5)
        self.assertEqual(parse_number("1234.56"), 1234.56)
        self.assertEqual(parse_number(" 20 % "), 20.0)
        self.assertIsNone(parse_number(""))
        self.assertIsNone(parse_number("abc"))
        self.assertEqual(parse_number(7), 7.0)

    def test_headers_are_matched_case_and_diacritic_insensitively(self) -> None:
        self.assertEqual(normalise_header("GTİP"), "gtip")
        self.assertEqual(normalise_header("Fatura Bedeli (USD)"), "fatura bedeli usd")
        rows = rows_from_upload("GTİP;Menşe;Fatura bedeli (USD);Navlun;KDV %\n6911.10.00.00.11;Çin;1.234,56;100;20\n".encode("utf-8"), "satirlar.csv")
        self.assertEqual(rows[0]["gtip"], "6911.10.00.00.11")
        self.assertEqual(rows[0]["origin_country"], "Çin")
        self.assertEqual(rows[0]["invoice_value"], 1234.56)
        self.assertEqual(rows[0]["freight"], 100.0)
        self.assertEqual(rows[0]["vat_rate"], 20.0)
        self.assertEqual(rows[0]["line"], 2)

    def test_template_round_trips_through_the_parser(self) -> None:
        rows = rows_from_upload(template_csv().encode("utf-8"), "sablon.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["dispatch_country"], "Almanya")
        self.assertEqual(rows[1]["invoice_value"], 25000.0)
        self.assertEqual(rows[1]["payment_method"], "peşin")

    def test_xlsx_upload_is_parsed(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["HS", "Origin", "Invoice", "Currency"])
        sheet.append(["691110000011", "China", 5000, "EUR"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        rows = rows_from_upload(buffer.getvalue(), "lines.xlsx")
        self.assertEqual(rows[0]["gtip"], "691110000011")
        self.assertEqual(rows[0]["origin_country"], "China")
        self.assertEqual(rows[0]["invoice_value"], 5000.0)
        self.assertEqual(rows[0]["currency"], "EUR")

    def test_missing_gtip_column_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rows_from_upload(b"Mense;Fatura\nCin;100\n", "x.csv")


class BulkCalculationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp_dir.name)
        with self.engine._connect() as db:
            for snapshot_id, source_id, title in (("import-2026", "import_regime", "İRK"), ("igv-2026", "additional_duty", "İGV")):
                db.execute(
                    """INSERT INTO tariff_snapshots (id,source_id,source_title,landing_url,archive_url,archive_sha256,
                       retrieved_at,checked_at,valid_from,measure_count,active,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,1,'{}')""",
                    (snapshot_id, source_id, title, "https://ticaret.gov.tr/t", "https://ticaret.gov.tr/t.zip", "a" * 64,
                     "2026-08-29T00:00:00+03:00", "2026-08-29T00:00:00+03:00", "2026-01-01", 2),
                )
            for row_id, snapshot, measure_type, rate, group in (
                ("c1", "import-2026", "customs_duty", 12, "7"), ("c2", "import-2026", "customs_duty", 0, "1"),
                ("i1", "igv-2026", "additional_duty", 19, "7"), ("i2", "igv-2026", "additional_duty", 0, "1"),
            ):
                db.execute(
                    """INSERT INTO tariff_measures (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,
                       country_group_description,footnote,description,condition_text,list_name,source_file,source_sheet,
                       source_row,automatic_calculation_allowed) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,1)""",
                    (row_id, snapshot, "691110000011", measure_type, rate, str(rate), group, "grup", "liste", "f.xlsx", "69", 3),
                )

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp_dir.cleanup()

    async def test_rows_are_calculated_independently_with_totals(self) -> None:
        rows = [
            {"line": 2, "gtip": "6911.10.00.00.11", "origin_country": "Çin", "invoice_value": "10.000", "freight": 1200, "insurance": 50, "other_costs": 300,
             "quantity": 2000, "currency": "USD", "vat_rate": 20, "payment_method": "peşin", "anti_dumping_amount": 0,
             "sct_amount": 0, "surveillance_unit_value": 0, "additional_financial_liability_rate": 0},
            {"line": 3, "gtip": "691110000011", "origin_country": "Almanya", "invoice_value": 5000, "currency": "USD", "vat_rate": 20},
            {"line": 4, "gtip": "", "origin_country": "Çin", "invoice_value": 100},
            {"line": 5, "gtip": "691110000011", "origin_country": "Çin", "invoice_value": -5},
        ]
        result = await calculate_rows(self.engine, rows)
        statuses = [item["status"] for item in result["rows"]]
        self.assertEqual(statuses, ["complete", "partial", "error", "error"])
        first = result["rows"][0]
        self.assertEqual(first["customs_value"], 11250.0)
        self.assertEqual(first["rates"]["customs_duty"], 12.0)
        self.assertEqual(first["landed_total"], 18045.0)
        # Kısmi EMY kapsamındaki satır yokluğu uygulanmazlık kanıtı değildir.
        self.assertIn("Ek mali yükümlülük oranı", result["rows"][1]["missing_rates"])
        self.assertIsNone(result["rows"][1]["rates"]["financial_liability"])
        # "10.000" Türkçe binlik okunur ve satıra açıklama notu düşer.
        self.assertTrue(any("binlik" in warning for warning in first["warnings"]))
        self.assertIn("GTİP boş", result["rows"][2]["error"])
        self.assertIn("doğrulanamadı", result["rows"][3]["error"])
        self.assertEqual(result["summary"], {"rows": 4, "complete": 1, "partial": 1, "errors": 2})
        usd = result["totals"][0]
        self.assertEqual((usd["currency"], usd["rows"], usd["complete_rows"]), ("USD", 2, 1))
        self.assertEqual(usd["landed_total"], 18045.0)
        self.assertEqual(usd["customs_value"], 16250.0)

    async def test_row_limit(self) -> None:
        with self.assertRaises(ValueError):
            await calculate_rows(self.engine, [{"gtip": "691110000011", "origin_country": "Çin", "invoice_value": 1}] * 201)


if __name__ == "__main__":
    unittest.main()
