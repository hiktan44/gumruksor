"""Faz 0 vergi hesabı düzeltmeleri: her hata için önce başarısız olan test.

1. MCP maliyet aracı tanımsız ``as_of`` yüzünden her çağrıda NameError veriyordu.
2. "akreditif" metni "kredi" alt dizesine takılıp KKDF %6 öneriyordu.
3. İleri tarihli kararname snapshot'ı indirildiği an bugünkü hesaba giriyordu; "bugün" UTC idi.
4. Kısmi EMY kapsamında satır yokluğu muafiyet sayılmaz; kullanıcı oranı korunur.
5. Kullanıcı oranı / resmî oran çakışması ön kontrol yolunda sessizdi.
6. Gözetim eşiği (USD/kg) fatura dövizi ve birim çevrilmeden karşılaştırılıyordu.
7. Toplu hesap tescil tarihini ve A.TR beyanını iletmiyordu; FOB sütunu sessizdi.
8. Toplu Excel'de "41.250" kur/oran alanında 41250 okunuyordu.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulk_costing import calculate_rows, parse_number, rows_from_table  # noqa: E402
from customs_advisor import CustomsInquiry, _deterministic_cost  # noqa: E402
from decision_questions import _payment_known  # noqa: E402
from payment_terms import classify_payment_method  # noqa: E402
from tariff_engine import LandedCostInput, TariffEngine, calculate_landed_cost  # noqa: E402
from temporal import ISTANBUL, today_iso, today_local  # noqa: E402

_GTIP = "691110000011"


def _base_cost(**overrides) -> LandedCostInput:
    values = dict(
        invoice_value=1000, customs_duty_rate=0, additional_duty_rate=0, additional_financial_liability_rate=0,
        anti_dumping_amount=0, vat_rate=20, sct_amount=0, surveillance_unit_value=0,
    )
    values.update(overrides)
    return LandedCostInput(**values)


def _insert_snapshot(db, snapshot_id: str, source_id: str, valid_from: str, *, active: int, retrieved_at: str) -> None:
    db.execute(
        """INSERT INTO tariff_snapshots (id,source_id,source_title,landing_url,archive_url,archive_sha256,
           retrieved_at,checked_at,valid_from,measure_count,active,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,'{}')""",
        (snapshot_id, source_id, source_id, "https://ticaret.gov.tr/t", "https://ticaret.gov.tr/t.zip", "a" * 64,
         retrieved_at, retrieved_at, valid_from, 1, active),
    )


def _insert_measure(db, row_id: str, snapshot_id: str, measure_type: str, rate: float, group: str) -> None:
    db.execute(
        """INSERT INTO tariff_measures (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,
           country_group_description,footnote,description,condition_text,list_name,source_file,source_sheet,
           source_row,automatic_calculation_allowed) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,1)""",
        (row_id, snapshot_id, _GTIP, measure_type, rate, str(rate), group, "grup", "liste", "f.xlsx", "69", 3),
    )


# ----------------------------------------------------------------------------- 1. MCP aracı


class McpCostToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_runs_and_forwards_as_of(self) -> None:
        import mevzuat_mcp_server
        from fastmcp import Client

        fake = AsyncMock(return_value={"tariff": {}, "cost": {}, "as_of_date": "2026-03-01"})
        with patch.object(mevzuat_mcp_server.tariff_engine, "calculate", fake):
            async with Client(mevzuat_mcp_server.app) as client:
                await client.call_tool(
                    "calculate_import_landed_cost",
                    {"gtip": _GTIP, "origin_country": "Çin", "invoice_value": 1000, "as_of": "2026-03-01"},
                )
                await client.call_tool(
                    "calculate_import_landed_cost",
                    {"gtip": _GTIP, "origin_country": "Çin", "invoice_value": 1000},
                )
        self.assertEqual(fake.await_args_list[0].kwargs["as_of"], "2026-03-01")
        self.assertIsNone(fake.await_args_list[1].kwargs["as_of"])


# ----------------------------------------------------------------------------- 2. KKDF ödeme şekli


class PaymentClassificationTests(unittest.TestCase):
    def test_classification(self) -> None:
        cases = {
            "akreditif": None,
            "Görüldüğünde ödemeli akreditif": None,
            "vesaik mukabili": None,
            "Vadeli akreditif": "deferred",
            "mal mukabili": "deferred",
            "Kabul kredili": "deferred",
            "kredili": "deferred",
            "Peşin": "cash",
            "PEŞİN ÖDEME": "cash",
            "2": "cash",
            "1 - Mal mukabili": "deferred",
            "6": "deferred",
            "12": None,
            "": None,
            None: None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_payment_method(text), expected)

    def test_letter_of_credit_no_longer_gets_six_percent(self) -> None:
        result = calculate_landed_cost(_base_cost(payment_method="akreditif"))
        kkdf = next(line for line in result.lines if line["code"] == "kkdf")
        self.assertIsNone(kkdf["rate"])
        self.assertIn("KKDF oranı", result.missing_rates)
        self.assertTrue(any("akreditif" in warning for warning in result.warnings))
        # Belirsiz ödeme şeklinde karar sorusu sorulur.
        self.assertFalse(_payment_known("akreditif"))
        self.assertTrue(_payment_known("vadeli akreditif"))

    def test_deferred_and_cash_keep_previous_suggestions(self) -> None:
        deferred = calculate_landed_cost(_base_cost(payment_method="vadeli akreditif"))
        cash = calculate_landed_cost(_base_cost(payment_method="peşin"))
        self.assertEqual(next(l for l in deferred.lines if l["code"] == "kkdf")["rate"], 6.0)
        self.assertEqual(next(l for l in cash.lines if l["code"] == "kkdf")["rate"], 0.0)


# ----------------------------------------------------------------------------- 3. İleri tarihli snapshot


class FutureDatedSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp_dir.name)
        self.future = (today_local() + timedelta(days=10)).isoformat()
        with self.engine._connect() as db:
            _insert_snapshot(db, "old", "import_regime", "2026-01-01", active=1, retrieved_at="2026-01-02T00:00:00+03:00")
            _insert_measure(db, "o1", "old", "customs_duty", 10, "7")
            _insert_snapshot(db, "new", "import_regime", self.future, active=0, retrieved_at=datetime.now(ISTANBUL).isoformat())
            _insert_measure(db, "n1", "new", "customs_duty", 20, "7")
            TariffEngine._activate(db, "import_regime", "new")

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp_dir.cleanup()

    async def test_new_rates_wait_for_their_legal_start(self) -> None:
        today = await self.engine.lookup(_GTIP, origin_country="Çin")
        self.assertEqual(today.unambiguous_rates.get("customs_duty"), 10.0)
        on_start = await self.engine.lookup(_GTIP, origin_country="Çin", as_of=self.future)
        self.assertEqual(on_start.unambiguous_rates.get("customs_duty"), 20.0)


class IstanbulTodayTests(unittest.TestCase):
    def test_today_follows_turkey_time(self) -> None:
        self.assertEqual(ISTANBUL.utcoffset(None), timedelta(hours=3))
        expected = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
        self.assertIn(today_local(), {expected, expected - timedelta(days=1), expected + timedelta(days=1)})
        self.assertEqual(today_iso(), today_local().isoformat())
        late_utc = datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(late_utc.astimezone(ISTANBUL).date(), date(2026, 10, 15))


# ----------------------------------------------------------------------------- 4. EMY belirsizliği


class FinancialLiabilityInferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp_dir.name)
        with self.engine._connect() as db:
            for snapshot_id, source_id in (("irk", "import_regime"), ("igv", "additional_duty")):
                _insert_snapshot(db, snapshot_id, source_id, "2026-01-01", active=1, retrieved_at="2026-01-02T00:00:00+03:00")
            _insert_measure(db, "c7", "irk", "customs_duty", 12, "7")
            _insert_measure(db, "i7", "igv", "additional_duty", 19, "7")

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        self.temp_dir.cleanup()

    async def test_absent_emy_in_partial_coverage_requires_confirmation(self) -> None:
        lookup = await self.engine.lookup(_GTIP, origin_country="Çin")
        self.assertNotIn("additional_financial_liability", lookup.not_applicable_measures)
        result = await self.engine.calculate(_GTIP, "Çin", LandedCostInput(invoice_value=1000))
        cost = result["cost"]
        emy = next(line for line in cost["lines"] if line["code"] == "financial_liability")
        self.assertIsNone(emy["rate"])
        self.assertIn("Ek mali yükümlülük oranı", cost["missing_rates"])
        self.assertEqual(cost["status"], "partial")
        self.assertIsNone(cost["landed_total"])

    async def test_user_rate_is_not_overwritten(self) -> None:
        result = await self.engine.calculate(
            _GTIP, "Çin", LandedCostInput(invoice_value=1000, additional_financial_liability_rate=5)
        )
        emy = next(line for line in result["cost"]["lines"] if line["code"] == "financial_liability")
        self.assertEqual(emy["rate"], 5.0)


# ----------------------------------------------------------------------------- 5. Oran çakışması


class PrecheckOverrideTests(unittest.TestCase):
    def test_precheck_reports_user_rate_override(self) -> None:
        inquiry = CustomsInquiry(question="Maliyet nedir?", invoice_value=1000, customs_duty_rate=5)
        result = _deterministic_cost(inquiry, customs_duty_rate=12)
        self.assertEqual(result["rate_overrides"][0]["measure_type"], "customs_duty")
        self.assertTrue(any("%12" in warning and "%5" in warning for warning in result["warnings"]))

    def test_matching_rates_stay_quiet(self) -> None:
        inquiry = CustomsInquiry(question="Maliyet nedir?", invoice_value=1000, customs_duty_rate=12)
        result = _deterministic_cost(inquiry, customs_duty_rate=12)
        self.assertEqual(result["rate_overrides"], [])


# ----------------------------------------------------------------------------- 6. Gözetim


class SurveillanceConversionTests(unittest.TestCase):
    def _cost(self, **overrides):
        values = dict(invoice_value=1000, quantity=100, surveillance_unit_value=20, has_surveillance_certificate=False,
                      kkdf_rate=0, quantity_unit="kg", surveillance_unit="kg")
        values.update(overrides)
        return calculate_landed_cost(_base_cost(**values))

    def test_foreign_invoice_currency_requires_cross_rate(self) -> None:
        result = self._cost(currency="EUR")
        self.assertEqual(result.customs_value, 1000)
        self.assertTrue(any("çapraz kur" in item for item in result.missing_rates))

    def test_cross_rate_converts_threshold(self) -> None:
        result = self._cost(currency="EUR", surveillance_cross_rate=0.9)  # 1 USD = 0,9 EUR
        self.assertAlmostEqual(result.customs_value, 1800.0)

    def test_ton_threshold_against_kg_quantity(self) -> None:
        # 100 kg = 0,1 ton; eşik 20.000 USD/ton → taban 2.000 USD
        result = self._cost(surveillance_unit_value=20000, quantity_unit="kg", surveillance_unit="ton")
        self.assertAlmostEqual(result.customs_value, 2000.0)

    def test_incompatible_units_do_not_uplift(self) -> None:
        result = self._cost(quantity_unit="adet", surveillance_unit="kg")
        self.assertEqual(result.customs_value, 1000)
        self.assertTrue(any("uyumsuz" in item for item in result.missing_rates))

    def test_value_above_floor_is_kept(self) -> None:
        result = self._cost(invoice_value=5000)
        self.assertEqual(result.customs_value, 5000)


# ----------------------------------------------------------------------------- 7-8. Toplu hesap


class BulkForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_as_of_and_atr_are_forwarded(self) -> None:
        engine = type("Engine", (), {})()
        engine.calculate = AsyncMock(
            return_value={
                "tariff": {"gtip": _GTIP, "status": "matched", "warnings": []},
                "cost": {"status": "partial", "currency": "USD", "customs_value": 100.0, "lines": [], "warnings": []},
            }
        )
        rows = [
            {"gtip": _GTIP, "origin_country": "Çin", "invoice_value": 100},
            {"gtip": _GTIP, "origin_country": "Çin", "invoice_value": 100, "atr_certificate": "hayır"},
        ]
        await calculate_rows(engine, rows, as_of="2026-03-01", atr_certificate=True)
        first, second = engine.calculate.await_args_list
        self.assertEqual(first.kwargs["as_of"], "2026-03-01")
        self.assertIs(first.kwargs["atr_certificate"], True)
        self.assertIs(second.kwargs["atr_certificate"], False)


class BulkNumberTests(unittest.TestCase):
    def test_rate_fields_read_dot_as_decimal(self) -> None:
        table = [
            ["GTİP", "Menşe", "FOB", "Kur", "KDV", "Damga vergisi"],
            [_GTIP, "Çin", "10.000", "41.250", "20", "1.250"],
        ]
        row = rows_from_table(table)[0]
        self.assertEqual(row["exchange_rate"], 41.25)
        self.assertEqual(row["invoice_value"], 10000.0)
        self.assertEqual(row["stamp_duty_try"], 1250.0)
        notes = " ".join(row["_notes"])
        self.assertIn("binlik", notes)
        self.assertIn("FOB", notes)

    def test_parse_number_contract_is_unchanged_by_default(self) -> None:
        self.assertEqual(parse_number("1.234,56"), 1234.56)
        self.assertEqual(parse_number("12.500"), 12500.0)
        self.assertEqual(parse_number("12,5"), 12.5)
        self.assertEqual(parse_number("41.250", dot_is_decimal=True), 41.25)


if __name__ == "__main__":
    unittest.main()
