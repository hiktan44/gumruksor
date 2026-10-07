import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pydantic import ValidationError
from assistant import TariffComparisonArgs, build_default_tools
from tariff_comparison import compare_tariff_dates


def snapshot(day, rates, *, status="matched", basis="legal"):
    return {"status":status, "gtip":"610463000000", "as_of_date":day, "validity_basis":basis,
            "unambiguous_rates":rates, "warnings":[], "snapshots":[{"archive_url":"https://example.gov.tr/source",
            "archive_sha256":"a"*64}], "unresolved_measure_types":[]}


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def run_comparison(self, before, after):
        engine=SimpleNamespace(lookup=AsyncMock(side_effect=[before,after]))
        result=await compare_tariff_dates(engine,gtip="610463000000",origin_country="Çin",from_date="2025-01-01",to_date="2025-06-01")
        self.assertEqual(engine.lookup.await_count,2)
        self.assertTrue(all(call.kwargs["auto_sync"] is False for call in engine.lookup.await_args_list))
        return result

    async def test_difference_is_percentage_points_and_preserves_zero(self):
        result=await self.run_comparison(snapshot("2025-01-01",{"customs_duty":12,"additional_duty":0}),
            snapshot("2025-06-01",{"customs_duty":15,"additional_duty":0}))
        self.assertEqual(result["status"],"compared")
        changes={row["measure"]:row for row in result["changes"]}
        self.assertEqual(changes["customs_duty"]["difference_percentage_points"],3)
        self.assertEqual(changes["additional_duty"]["status"],"unchanged")
        self.assertEqual(result["before"]["sources"][0]["archive_sha256"],"a"*64)

    async def test_missing_old_record_is_not_zero_or_unchanged(self):
        result=await self.run_comparison(snapshot("2025-01-01",{},status="unavailable",basis="unavailable"),
            snapshot("2025-06-01",{"customs_duty":0}))
        self.assertEqual(result["status"],"partial")
        self.assertIsNone(result["changes"][0]["before_rate"])
        self.assertIsNone(result["changes"][0]["difference_percentage_points"])
        self.assertEqual(result["changes"][0]["status"],"unavailable")

    async def test_observed_date_is_not_confirmed_legal_change(self):
        result=await self.run_comparison(snapshot("2025-01-01",{"customs_duty":12},basis="observed"),
            snapshot("2025-06-01",{"customs_duty":15}))
        self.assertEqual(result["status"],"partial")
        self.assertTrue(any("yasal yürürlük" in warning for warning in result["warnings"]))

    async def test_wrong_date_or_code_does_not_supply_old_rates(self):
        before=snapshot("2025-01-01",{"customs_duty":12});before["gtip"]="999999999999"
        result=await self.run_comparison(before,snapshot("2025-06-01",{"customs_duty":15}))
        self.assertIsNone(result["changes"][0]["before_rate"])

    async def test_invalid_and_future_dates_are_rejected_before_any_read(self):
        engine=SimpleNamespace(lookup=AsyncMock())
        for start,end in [("2025-02-30","2025-06-01"),("2025-06-01","2025-01-01"),("2025-01-01","2099-01-01")]:
            with self.assertRaises(ValueError):
                await compare_tariff_dates(engine,gtip="610463000000",origin_country="Çin",from_date=start,to_date=end)
        engine.lookup.assert_not_awaited()
        with self.assertRaises(ValidationError):
            TariffComparisonArgs(gtip="610463",origin_country="Çin",from_date="2025-01-01",to_date="2025-06-01")

    async def test_registered_assistant_tool_invokes_the_existing_engine(self):
        engine=SimpleNamespace(lookup=AsyncMock(side_effect=[snapshot("2025-01-01",{"customs_duty":12}),snapshot("2025-06-01",{"customs_duty":15})]))
        tools=build_default_tools(tariff_engine=engine,control_engine=SimpleNamespace(),classification_engine=SimpleNamespace(),
            trade_measure_engine=SimpleNamespace(),excise_tax_index=SimpleNamespace(),exchange_rate_service=SimpleNamespace())
        tool=next(t for t in tools if t.name=="compare_tariff_dates")
        result=await tool.run(TariffComparisonArgs(gtip="610463000000",origin_country="Çin",from_date="2025-01-01",to_date="2025-06-01"))
        self.assertEqual(result["status"],"compared")
