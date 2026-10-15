"""Synthetic regressions for incomplete EMY, surveillance inputs and legal intervals."""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook

from bulk_costing import calculate_rows, rows_from_upload
from tariff_engine import LandedCostInput, TariffEngine, calculate_landed_cost
from trade_measures import TradeMeasureEngine, TradeMeasureStore

GTIP = "691110000011"
TODAY = "2026-10-15"


def insert_snapshot(db, snapshot_id, source_id="import_regime", *, start="2026-01-01", end=None, active=1, status="approved"):
    db.execute(
        """INSERT INTO tariff_snapshots
        (id,source_id,source_title,landing_url,archive_url,archive_sha256,retrieved_at,checked_at,
         valid_from,valid_to,measure_count,active,metadata_json,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,1,?,'{}',?)""",
        (snapshot_id, source_id, source_id, "https://example.invalid/t", "https://example.invalid/t.zip",
         "a" * 64, start + "T00:00:00+03:00", start + "T00:00:00+03:00", start, end, active, status),
    )


def insert_measure(db, row_id, snapshot_id, kind="customs_duty", rate=10, *, footnote=None):
    db.execute(
        """INSERT INTO tariff_measures
        (id,snapshot_id,gtip,measure_type,rate,rate_text,country_group,country_group_description,
         footnote,list_name,source_file,source_sheet,source_row,automatic_calculation_allowed)
        VALUES (?,?,?,?,?,?,'7','Other',?,'synthetic','synthetic.xlsx','1',1,1)""",
        (row_id, snapshot_id, GTIP, kind, rate, str(rate), footnote),
    )


def cost_input(**overrides):
    values = dict(invoice_value=1000, additional_duty_rate=0, anti_dumping_amount=0,
                  kkdf_rate=0, vat_rate=20, sct_amount=0, surveillance_unit_value=0)
    values.update(overrides)
    return LandedCostInput(**values)


class SyntheticEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TariffEngine(data_dir=self.temp.name)

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    def add_current_rates(self):
        with self.engine._connect() as db:
            insert_snapshot(db, "current")
            insert_snapshot(db, "igv", "additional_duty")
            insert_measure(db, "duty", "current")


class FinancialLiabilitySafetyTests(SyntheticEngineTests):
    async def assert_unknown_emy(self):
        lookup = await self.engine.lookup(GTIP, origin_country="Çin", auto_sync=False)
        self.assertNotIn("additional_financial_liability", lookup.not_applicable_measures)
        self.assertIn("additional_financial_liability", lookup.unresolved_measure_types)
        result = await self.engine.calculate(GTIP, "Çin", cost_input())
        cost = result["cost"]
        line = next(line for line in cost["lines"] if line["code"] == "financial_liability")
        self.assertIsNone(line["rate"])
        self.assertIsNone(line["amount"])
        self.assertIn("Ek mali yükümlülük oranı", cost["missing_rates"])
        self.assertEqual(cost["status"], "partial")
        self.assertIsNone(cost["landed_total"])

    async def test_no_trade_engine_is_unknown(self):
        self.add_current_rates()
        await self.assert_unknown_emy()

    async def test_missing_trade_dataset_is_unknown(self):
        self.add_current_rates()
        store = TradeMeasureStore(data_dir=self.temp.name, seed_dir=Path(self.temp.name) / "no-seeds")
        self.engine.trade_measures = TradeMeasureEngine(data_dir=self.temp.name)
        self.engine.trade_measures.store = store
        await self.assert_unknown_emy()

    async def test_partial_trade_snapshot_without_hits_is_unknown(self):
        self.add_current_rates()
        store = TradeMeasureStore(data_dir=self.temp.name, seed_dir=Path(self.temp.name) / "no-seeds")
        store.save("safeguard", [], source_url="https://example.invalid", source_label="synthetic")
        self.engine.trade_measures = TradeMeasureEngine(data_dir=self.temp.name)
        self.engine.trade_measures.store = store
        await self.assert_unknown_emy()

    async def test_trade_lookup_error_is_unknown(self):
        self.add_current_rates()
        self.engine.trade_measures = Mock()
        self.engine.trade_measures.store.metadata.return_value = {"origin": "synced"}
        self.engine.trade_measures.lookup.side_effect = RuntimeError("synthetic failure")
        await self.assert_unknown_emy()
        self.assertEqual(self.engine.trade_measures.lookup.call_count, 2)
        result = await self.engine.lookup(GTIP, origin_country="Çin", auto_sync=False)
        self.assertEqual(result.measure_coverage["safeguard"].status, "not_integrated")
        self.assertTrue(any("sorgulanamadı" in warning for warning in result.warnings))

    async def test_trade_metadata_error_does_not_crash_or_imply_zero(self):
        self.add_current_rates()
        self.engine.trade_measures = Mock()
        self.engine.trade_measures.store.metadata.side_effect = RuntimeError("synthetic failure")
        self.engine.trade_measures.lookup.side_effect = RuntimeError("synthetic failure")
        await self.assert_unknown_emy()

    async def test_only_igv_snapshot_does_not_prove_emy_absence(self):
        with self.engine._connect() as db:
            insert_snapshot(db, "igv", "additional_duty")
            insert_measure(db, "igv-row", "igv", "additional_duty", 0)
        await self.assert_unknown_emy()

    async def test_verified_official_zero_and_user_exemption_are_preserved(self):
        self.add_current_rates()
        for rate in (0, 5):
            with self.subTest(user_rate=rate):
                result = await self.engine.calculate(GTIP, "Çin", cost_input(additional_financial_liability_rate=rate))
                self.assertEqual(result["cost"]["status"], "complete")
                self.assertEqual(next(x["rate"] for x in result["cost"]["lines"] if x["code"] == "financial_liability"), rate)
        with self.engine._connect() as db:
            insert_measure(db, "zero-emy", "current", "additional_financial_liability", 0)
        result = await self.engine.calculate(GTIP, "Çin", cost_input())
        self.assertEqual(result["cost"]["status"], "complete")
        self.assertEqual(result["cost"]["landed_total"], 1320)

    async def test_footnoted_emy_stays_unknown(self):
        self.add_current_rates()
        with self.engine._connect() as db:
            insert_measure(db, "conditional-emy", "current", "additional_financial_liability", 0, footnote="verify condition")
        await self.assert_unknown_emy()

    async def test_trade_query_uses_past_today_and_future_request_date(self):
        self.add_current_rates()
        trade = TradeMeasureEngine(data_dir=self.temp.name)
        self.engine.trade_measures = trade
        with patch.object(trade, "lookup", wraps=trade.lookup) as lookup, patch("tariff_engine.today_iso", return_value=TODAY):
            for as_of in ("2026-01-01", None, TODAY, "2026-11-01"):
                with self.subTest(as_of=as_of):
                    result = await self.engine.lookup(GTIP, origin_country="Çin", as_of=as_of, auto_sync=False)
                    self.assertEqual(lookup.call_args.kwargs.get("today"), date.fromisoformat(as_of or TODAY))
                    self.assertEqual(result.trade_measures["as_of"], as_of or TODAY)


class SurveillanceSafetyTests(SyntheticEngineTests):
    def surveillance_input(self, **overrides):
        values = dict(customs_duty_rate=10, additional_financial_liability_rate=0, quantity=100,
                      surveillance_unit_value=20000, has_surveillance_certificate=False)
        values.update(overrides)
        return cost_input(**values)

    def test_missing_or_incompatible_units_do_not_infer_a_floor_or_total(self):
        for units in ({}, {"quantity_unit": "kg"}, {"surveillance_unit": "ton"},
                      {"quantity_unit": "adet", "surveillance_unit": "kg"}):
            with self.subTest(units=units):
                result = calculate_landed_cost(self.surveillance_input(**units))
                self.assertEqual(result.status, "partial")
                self.assertEqual(result.customs_value, 1000)
                self.assertIsNone(result.vat_base)
                self.assertIsNone(result.landed_total)
                self.assertTrue(any("birim" in x for x in result.missing_rates))

    def test_missing_fx_or_certificate_does_not_emit_a_total(self):
        for overrides in ({"currency": "EUR"}, {"has_surveillance_certificate": None}):
            with self.subTest(overrides=overrides):
                result = calculate_landed_cost(self.surveillance_input(quantity_unit="kg", surveillance_unit="ton", **overrides))
                self.assertEqual(result.status, "partial")
                self.assertIsNone(result.landed_total)
                self.assertIsNone(result.try_summary)

    def test_confirmed_certificate_or_zero_threshold_needs_no_conversion(self):
        for overrides in ({"has_surveillance_certificate": True}, {"surveillance_unit_value": 0}):
            with self.subTest(overrides=overrides):
                result = calculate_landed_cost(self.surveillance_input(**overrides))
                self.assertEqual(result.status, "complete")
                self.assertEqual(result.customs_value, 1000)

    async def test_json_csv_and_xlsx_surveillance_conversion_reaches_real_calculation(self):
        self.add_current_rates()
        headers = ["GTİP", "Menşe", "Fatura", "Para birimi", "Miktar", "Miktar birimi", "Gözetim",
                   "Gözetim dövizi", "Gözetim birimi", "Gözetim çapraz kuru", "Gözetim belgesi",
                   "EMY", "Damping", "ÖTV", "KKDF", "KDV"]
        # Synthetic arithmetic examples, not legal rates.
        for currency, threshold_currency, fx, quantity, quantity_unit, threshold, threshold_unit, floor in (
            ("USD", "USD", None, 100, "kg", 20000, "ton", 2000),
            ("EUR", "USD", "0.900", 100, "kg", 20000, "ton", 1800),
            ("USD", "EUR", "1.250", 100, "kg", 20000, "ton", 2500),
            ("USD", "USD", None, 0.1, "ton", 20, "kg", 2000),
            ("USD", "USD", None, 100, "kg", 20, "kg", 2000),
        ):
            raw = [GTIP, "Çin", 1000, currency, quantity, quantity_unit, threshold, threshold_currency,
                   threshold_unit, fx, "hayır", 0, 0, 0, 0, 20]
            json_row = dict(gtip=GTIP, origin_country="Çin", **self.surveillance_input(
                currency=currency, surveillance_currency=threshold_currency, surveillance_cross_rate=float(fx) if fx else None,
                quantity=quantity, quantity_unit=quantity_unit, surveillance_unit_value=threshold, surveillance_unit=threshold_unit,
            ).model_dump())
            # Leave GV/İGV to the real snapshot lookup.
            json_row.pop("customs_duty_rate")
            json_row.pop("additional_duty_rate")
            csv_text = ";".join(headers) + "\n" + ";".join("" if x is None else str(x) for x in raw) + "\n"
            workbook = Workbook()
            workbook.active.append(headers)
            workbook.active.append(raw)
            buffer = io.BytesIO()
            workbook.save(buffer)
            workbook.close()
            for kind, rows in (("json", [json_row]), ("csv", rows_from_upload(csv_text.encode(), "x.csv")),
                               ("xlsx", rows_from_upload(buffer.getvalue(), "x.xlsx"))):
                with self.subTest(kind=kind, currency=currency, threshold_currency=threshold_currency, unit=quantity_unit):
                    result = await calculate_rows(self.engine, rows)
                    row = result["rows"][0]
                    self.assertEqual(row["status"], "complete", row)
                    self.assertAlmostEqual(row["customs_value"], floor)
                    self.assertAlmostEqual(row["landed_total"], floor * 1.1 * 1.2)
                    self.assertAlmostEqual(result["totals"][0]["landed_total"], row["landed_total"])

    async def test_bulk_missing_units_does_not_count_as_a_complete_total(self):
        self.add_current_rates()
        row = dict(gtip=GTIP, origin_country="Çin", **self.surveillance_input().model_dump())
        result = await calculate_rows(self.engine, [row])
        self.assertEqual(result["rows"][0]["status"], "partial")
        self.assertIsNone(result["rows"][0]["landed_total"])
        self.assertEqual(result["totals"][0]["complete_rows"], 0)


class SnapshotIntervalSafetyTests(SyntheticEngineTests):
    async def test_future_only_source_never_answers_before_start(self):
        with self.engine._connect() as db:
            insert_snapshot(db, "future", start="2026-11-01")
            insert_measure(db, "future-duty", "future", rate=20)
        with patch("tariff_engine.today_iso", return_value=TODAY):
            for as_of in ("2026-01-01", None, TODAY, "2026-10-31"):
                with self.subTest(as_of=as_of):
                    result = await self.engine.lookup(GTIP, origin_country="Çin", as_of=as_of, auto_sync=False)
                    self.assertEqual(result.status, "unavailable")
                    self.assertEqual(result.validity_basis, "unavailable")
                    self.assertEqual(result.unambiguous_rates, {})
                    self.assertEqual(result.snapshots, [])
                    self.assertIn(as_of or TODAY, " ".join(result.warnings))
            result = await self.engine.lookup(GTIP, origin_country="Çin", as_of="2026-11-01", auto_sync=False)
            self.assertEqual(result.unambiguous_rates["customs_duty"], 20)

    async def test_old_version_is_used_only_inside_its_interval(self):
        with self.engine._connect() as db:
            insert_snapshot(db, "old", start="2026-01-01", end="2026-11-01", active=0)
            insert_measure(db, "old-duty", "old", rate=10)
            insert_snapshot(db, "future", start="2026-11-01")
            insert_measure(db, "future-duty", "future", rate=20)
            insert_snapshot(db, "pending", start="2026-10-14", active=0, status="pending_review")
            insert_measure(db, "pending-duty", "pending", rate=99)
        with patch("tariff_engine.today_iso", return_value=TODAY):
            for as_of, expected in (("2026-01-01", 10), (None, 10), (TODAY, 10), ("2026-10-31", 10),
                                    ("2026-11-01", 20), ("2026-12-01", 20)):
                with self.subTest(as_of=as_of):
                    result = await self.engine.lookup(GTIP, origin_country="Çin", as_of=as_of, auto_sync=False)
                    self.assertEqual(result.unambiguous_rates["customs_duty"], expected)

    async def test_expired_active_and_noncovering_fallback_are_unavailable(self):
        with self.engine._connect() as db:
            insert_snapshot(db, "expired", end="2026-10-01")
            insert_measure(db, "expired-duty", "expired")
        with patch("tariff_engine.today_iso", return_value=TODAY):
            for as_of in (None, TODAY, "2026-11-01"):
                with self.subTest(as_of=as_of):
                    result = await self.engine.lookup(GTIP, origin_country="Çin", as_of=as_of, auto_sync=False)
                    self.assertEqual(result.status, "unavailable")

    async def test_future_active_rejects_expired_and_unapproved_fallbacks(self):
        with self.engine._connect() as db:
            for snapshot_id, start, end, active, status in (
                ("expired", "2026-01-01", "2026-10-01", 0, "approved"),
                ("pending", "2026-10-01", None, 0, "pending_review"),
                ("future", "2026-11-01", None, 1, "approved"),
            ):
                insert_snapshot(db, snapshot_id, start=start, end=end, active=active, status=status)
                insert_measure(db, snapshot_id + "-duty", snapshot_id, rate=20)
        with patch("tariff_engine.today_iso", return_value=TODAY):
            result = await self.engine.calculate(GTIP, "Çin", cost_input(additional_financial_liability_rate=0))
        self.assertEqual(result["tariff"]["status"], "unavailable")
        self.assertEqual(result["tariff"]["unambiguous_rates"], {})
        self.assertIn("Gümrük vergisi oranı", result["cost"]["missing_rates"])
        self.assertEqual(result["cost"]["status"], "partial")
        self.assertIsNone(result["cost"]["landed_total"])

    async def test_missing_one_source_keeps_valid_other_source_and_missing_rate(self):
        with self.engine._connect() as db:
            insert_snapshot(db, "future", start="2026-11-01")
            insert_measure(db, "future-duty", "future", rate=20)
            insert_snapshot(db, "igv", "additional_duty")
            insert_measure(db, "igv-row", "igv", "additional_duty", 5)
        with patch("tariff_engine.today_iso", return_value=TODAY):
            result = await self.engine.calculate(GTIP, "Çin", cost_input(additional_duty_rate=None, additional_financial_liability_rate=0))
        self.assertEqual(result["tariff"]["unambiguous_rates"], {"additional_duty": 5})
        self.assertEqual(result["tariff"]["measure_coverage"]["customs_duty"]["status"], "not_integrated")
        self.assertEqual(result["cost"]["status"], "partial")
        self.assertIsNone(result["cost"]["landed_total"])


if __name__ == "__main__":
    unittest.main()
