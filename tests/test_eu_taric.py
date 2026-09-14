"""AB TARIC (Apify aktörü) motoru: çözümleyici, arşiv, ücret kontrolü ve jeton sızıntısı.

Gerçek ağ erişimi ve gerçek Apify çağrısı yoktur; yanıtlar ``httpx.MockTransport``
ile üretilir. Aktörün gerçek çıktı şeması esas alınmıştır.
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

import eu_taric as et


def _item(goods_code: str = "6109100010", *, with_preference: bool = True, with_antidumping: bool = False) -> dict:
    measures = [
        {
            "goodsCode": goods_code,
            "inherited": False,
            "partnerAreaCode": "1011",
            "partnerArea": "ERGA OMNES",
            "measureTypeCode": "103",
            "measureType": "Third country duty",
            "dutyText": "12.00 %",
            "conditions": [],
            "documents": [],
            "footnotes": [],
            "additionalCodes": [],
        }
    ]
    if with_preference:
        measures.append(
            {
                "goodsCode": goods_code,
                "inherited": False,
                "partnerAreaCode": "TR",
                "partnerArea": "Türkiye",
                "measureTypeCode": "106",
                "measureType": "Customs Union Duty",
                "dutyText": "0.00 %",
                "conditions": [{"description": "Presentation of A.TR movement certificate"}],
                "documents": [{"code": "N018"}],
                "footnotes": [{"code": "CD554"}],
                "additionalCodes": [],
                "legalBase": "R1234/2020",
            }
        )
    if with_antidumping:
        measures.append(
            {
                "goodsCode": goods_code,
                "partnerAreaCode": "CN",
                "partnerArea": "China",
                "measureTypeCode": "552",
                "measureType": "Definitive anti-dumping duty",
                "dutyText": "35.00 %",
                "conditions": [],
                "documents": [],
                "footnotes": [],
                "additionalCodes": ["A999"],
            }
        )
    return {
        "goodsCode": goods_code,
        "cnCode": goods_code[:8],
        "partnerCountry": "TR",
        "goodsDescription": "T-shirts, singlets and other vests, knitted or crocheted, of cotton",
        "importMeasureCount": len(measures),
        "importMeasures": measures,
        "sourceSnapshotMonth": "2026-09",
        "sourceSnapshotDate": "2026-09-01",
    }


class ParserTests(unittest.TestCase):
    def test_normalise_goods_code_pads_to_ten(self):
        self.assertEqual(et.normalise_goods_code("610910"), "6109100000")
        self.assertEqual(et.normalise_goods_code("6109.10.00.10"), "6109100010")
        self.assertEqual(et.normalise_goods_code("610910001099"), "6109100010")

    def test_classify_measure(self):
        self.assertEqual(et.classify_measure("Third country duty"), "third_country_duty")
        self.assertEqual(et.classify_measure("Customs Union Duty"), "customs_union_duty")
        self.assertEqual(et.classify_measure("Definitive anti-dumping duty"), "anti_dumping")
        self.assertEqual(et.classify_measure("Something"), "other")

    def test_parse_measures_reads_actor_fields(self):
        measures = et.parse_measures(_item())
        self.assertEqual(len(measures), 2)
        union = measures[1]
        self.assertEqual(union["kind"], "customs_union_duty")
        self.assertEqual(union["duty_text"], "0.00 %")
        self.assertEqual(union["documents"], ["N018"])
        self.assertEqual(union["conditions"], ["Presentation of A.TR movement certificate"])
        self.assertEqual(union["legal_basis"], "R1234/2020")

    def test_resolve_rates_separates_mfn_and_partner(self):
        summary = et.resolve_rates(_item(), "TR")
        self.assertEqual(summary["mfn_rate"], "12.00 %")
        self.assertEqual(summary["partner_rate"], "0.00 %")
        self.assertEqual(summary["partner_rate_kind"], "customs_union_duty")
        self.assertEqual(summary["required_documents"], ["N018"])
        self.assertEqual(summary["rate_status"], "conditional")
        self.assertEqual(summary["snapshot_month"], "2026-09")
        self.assertEqual(summary["cn_code"], "61091000")

    def test_resolve_rates_without_preference(self):
        summary = et.resolve_rates(_item(with_preference=False), "TR")
        self.assertEqual(summary["mfn_rate"], "12.00 %")
        self.assertIsNone(summary["partner_rate"])
        self.assertEqual(summary["required_documents"], [])

    def test_resolve_rates_lists_additional_duties(self):
        summary = et.resolve_rates(_item(with_antidumping=True), "CN")
        kinds = {item["kind"] for item in summary["additional_duties"]}
        self.assertIn("anti_dumping", kinds)


def _transport(calls: list[dict], *, status: int = 200, items: list[dict] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization", ""),
                "body": json.loads(request.content.decode("utf-8")) if request.content else {},
            }
        )
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json=items if items is not None else [_item()])

    return httpx.MockTransport(handler)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[dict] = []

    def _engine(self, *, status: int = 200, items: list[dict] | None = None, token: str = "test-token",
                enabled: bool = True) -> et.EuTaricEngine:
        client = httpx.AsyncClient(transport=_transport(self.calls, status=status, items=items))
        engine = et.EuTaricEngine(self._tmp.name, http=client, token=token, enabled=enabled)
        self.addCleanup(lambda: asyncio.run(engine.close()))
        return engine

    def test_lookup_returns_summary_and_sends_expected_input(self):
        engine = self._engine()
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        payload = result.as_dict()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["goods_code"], "6109100010")
        self.assertEqual(payload["summary"]["mfn_rate"], "12.00 %")
        self.assertEqual(payload["summary"]["partner_rate"], "0.00 %")
        self.assertFalse(payload["from_archive"])
        self.assertIn("A.TR", payload["customs_union_note"])
        self.assertIn("maliyet hesabına aktarılmaz", payload["conditional_note"])

        sent = self.calls[0]["body"]
        self.assertEqual(sent["goodsCodes"], ["6109100010"])
        self.assertEqual(sent["partnerCountries"], ["TR"])
        self.assertEqual(sent["direction"], "import")
        self.assertEqual(sent["snapshotMonth"], "latest")

    def test_second_lookup_is_served_from_archive_without_paying_again(self):
        engine = self._engine()
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        self.assertEqual(len(self.calls), 1)
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        self.assertEqual(len(self.calls), 1, "aynı kod × ülke için ikinci kez ücret ödenmemeli")
        self.assertTrue(result.from_archive)
        self.assertEqual(result.summary["mfn_rate"], "12.00 %")

    def test_refresh_forces_a_new_run(self):
        engine = self._engine()
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        asyncio.run(engine.lookup("610910001000", origin="TR", refresh=True))
        self.assertEqual(len(self.calls), 2)

    def test_disabled_engine_never_calls_the_paid_actor(self):
        engine = self._engine(enabled=False)
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        self.assertEqual(result.status, "disabled")
        self.assertEqual(self.calls, [])
        self.assertTrue(any("ücretlidir" in item for item in result.warnings))

    def test_missing_token_disables_lookup(self):
        engine = self._engine(token="")
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        self.assertEqual(result.status, "disabled")
        self.assertEqual(self.calls, [])

    def test_token_never_leaks_into_warnings(self):
        engine = self._engine(status=402, token="apify_api_SUPERSECRET")
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        blob = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertNotIn("SUPERSECRET", blob)
        self.assertTrue(any("402" in item for item in result.warnings))

    def test_non_ascii_token_is_rejected_instead_of_crashing(self):
        """Başlıkta taşınamayacak bir jeton sessiz çökme değil, temiz bir 'kapalı' durumu üretmeli."""
        engine = self._engine(token="süper-gizli-jeton")
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        self.assertEqual(result.status, "disabled")
        self.assertEqual(self.calls, [])
        self.assertFalse(engine.status()["configured"])
        self.assertNotIn("süper", json.dumps(result.as_dict(), ensure_ascii=False))

    def test_token_is_sent_as_bearer_header_not_in_url(self):
        engine = self._engine()
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        call = self.calls[0]
        self.assertEqual(call["auth"], "Bearer test-token")
        self.assertNotIn("test-token", call["url"])

    def test_failure_falls_back_to_archive(self):
        engine = self._engine()
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        engine._http = httpx.AsyncClient(transport=_transport(self.calls, status=500))
        result = asyncio.run(engine.lookup("610910001000", origin="TR", refresh=True))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.from_archive)
        self.assertTrue(any("son bilinen" in item for item in result.warnings))

    def test_short_code_rejected(self):
        engine = self._engine()
        with self.assertRaises(ValueError):
            asyncio.run(engine.lookup("6109"))

    def test_empty_actor_result_is_reported(self):
        engine = self._engine(items=[])
        result = asyncio.run(engine.lookup("610910001000", origin="TR"))
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.warnings)

    def test_status_reports_archive_and_configuration(self):
        engine = self._engine()
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        status = engine.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["archived_lookups"], 1)
        self.assertEqual(status["latest_snapshot_month"], "2026-09")


class CandidateTests(unittest.TestCase):
    def test_hs10_keeps_taric_subdivision_and_drops_national_digits(self):
        codes = et.candidate_codes(["610910001000", "6109.10.00.11.00", "851713000000"], level="hs10")
        self.assertEqual(codes, ["6109100010", "6109100011", "8517130000"])

    def test_hs6_collapses_to_chapter_subheading(self):
        codes = et.candidate_codes(["610910001000", "610910009000", "851713000000"], level="hs6")
        self.assertEqual(codes, ["6109100000", "8517130000"])

    def test_cn8_level_zeroes_taric_digits(self):
        self.assertEqual(et.candidate_codes(["610910001000"], level="cn8"), ["6109100000"])

    def test_short_and_empty_codes_are_skipped(self):
        self.assertEqual(et.candidate_codes(["6109", "", None, "61091000"], level="hs10"), ["6109100000"])

    def test_parse_origins_normalises(self):
        self.assertEqual(et._parse_origins("tr, cn ;tr"), ("TR", "CN"))
        self.assertEqual(et._parse_origins(["TR", "us"]), ("TR", "US"))
        self.assertEqual(et._parse_origins(None), ())


class FillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[dict] = []

    def _engine(self, *, codes=("610910001000", "851713000000"), origins="TR,CN", budget=10.0,
                batch=10, items=None, status=200, fill_enabled=True) -> et.EuTaricEngine:
        client = httpx.AsyncClient(transport=_transport(self.calls, status=status, items=items))
        engine = et.EuTaricEngine(
            self._tmp.name,
            http=client,
            token="test-token",
            enabled=True,
            code_source=lambda: list(codes),
            fill_enabled=fill_enabled,
            fill_level="hs10",
            fill_origins=origins,
            fill_batch=batch,
            monthly_budget_usd=budget,
            unit_cost_usd=0.015,
        )
        self.addCleanup(lambda: asyncio.run(engine.close()))
        return engine

    def test_plan_counts_pairs_and_estimates_cost(self):
        engine = self._engine()
        plan = engine.fill_plan()
        self.assertTrue(plan["enabled"])
        self.assertEqual(plan["candidate_codes"], 2)
        self.assertEqual(plan["origins"], ["TR", "CN"])
        self.assertEqual(plan["total_pairs"], 4)
        self.assertEqual(plan["pending_pairs"], 4)
        self.assertEqual(plan["estimated_total_usd"], 0.06)

    def test_fill_stores_results_and_records_spend(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["charged"], 1)
        self.assertEqual(report["spend"]["lookups"], 1)
        self.assertAlmostEqual(report["spend"]["spent_usd"], 0.015)
        archived = engine.archived("6109100010", "TR")
        self.assertIsNotNone(archived)
        self.assertEqual(archived["summary"]["mfn_rate"], "12.00 %")

    def test_second_run_does_not_pay_for_the_same_pair_again(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.fill_once())
        calls_after_first = len(self.calls)
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(self.calls), calls_after_first)
        self.assertEqual(engine.spend_status()["lookups"], 1)

    def test_budget_cap_stops_the_fill(self):
        # Tavan tek sorguya yeter: ikinci çift bu ay hiç denenmez.
        engine = self._engine(codes=("610910001000", "851713000000"), origins="TR", budget=0.015, batch=10)
        first = asyncio.run(engine.fill_once())
        self.assertEqual(first["requested"], 1)
        second = asyncio.run(engine.fill_once())
        self.assertEqual(second["status"], "budget_exhausted")
        self.assertEqual(second["requested"], 0)

    def test_zero_budget_never_spends(self):
        engine = self._engine(budget=0.0)
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "budget_exhausted")
        self.assertEqual(self.calls, [])
        self.assertFalse(engine.fill_plan()["enabled"])

    def test_disabled_fill_does_nothing(self):
        engine = self._engine(fill_enabled=False)
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(self.calls, [])

    def test_code_without_result_is_marked_not_declarable_and_not_charged(self):
        engine = self._engine(codes=("851713000000",), origins="TR", items=[])
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["not_declarable"], 1)
        self.assertEqual(report["charged"], 0)
        self.assertEqual(engine.spend_status()["lookups"], 0)
        # Aynı ay içinde tekrar denenmez (boşuna çağrı yapılmaz).
        again = asyncio.run(engine.fill_once())
        self.assertEqual(again["status"], "complete")

    def test_failed_chunk_is_retried_next_run(self):
        engine = self._engine(codes=("610910001000",), origins="TR", status=500)
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["failed"], 1)
        self.assertEqual(engine.spend_status()["lookups"], 0)
        self.assertEqual(engine.fill_plan()["pending_pairs"], 1)

    def test_pairs_already_fetched_on_demand_are_skipped_without_paying(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        calls_after_lookup = len(self.calls)
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(len(self.calls), calls_after_lookup)
        self.assertEqual(engine.spend_status()["lookups"], 0)

    def test_batch_limits_codes_per_actor_call(self):
        codes = [f"61091000{index:02d}00" for index in range(12)]
        engine = self._engine(codes=tuple(codes), origins="TR", batch=12)
        items = [_item(f"61091000{index:02d}") for index in range(12)]
        engine._http = httpx.AsyncClient(transport=_transport(self.calls, items=items))
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["requested"], 12)
        # Aktöre tek seferde en fazla EU_TARIC_MAX_CODES kod gönderilir.
        self.assertGreater(len(self.calls), 1)
        for call in self.calls:
            self.assertLessEqual(len(call["body"]["goodsCodes"]), et.EU_TARIC_MAX_CODES)

    def test_status_includes_fill_block(self):
        engine = self._engine()
        status = engine.status()
        self.assertIn("fill", status)
        self.assertEqual(status["fill"]["level"], "hs10")

    def test_missing_code_source_yields_no_candidates(self):
        client = httpx.AsyncClient(transport=_transport(self.calls))
        engine = et.EuTaricEngine(self._tmp.name, http=client, token="t", enabled=True,
                                  fill_enabled=True, monthly_budget_usd=5.0)
        self.addCleanup(lambda: asyncio.run(engine.close()))
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "no_candidates")

    def _age_rows(self, engine, days: int) -> None:
        """Arşiv ve deneme kayıtlarını geriye taşır (saat dondurmak yerine veriyi yaşlandırır)."""
        import sqlite3
        from datetime import UTC, datetime, timedelta

        moment = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        with sqlite3.connect(engine.db_path) as connection:
            connection.execute("UPDATE lookups SET fetched_at=?", (moment,))
            connection.execute("UPDATE fill_attempts SET attempted_at=?", (moment,))

    def test_new_calendar_month_does_not_re_buy_stored_data(self):
        # Asıl gerileme: dedupe takvim ayına bakarsa ayın 1'inde tüm katalog yeniden satın alınır.
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.fill_once())
        calls_after_first = len(self.calls)
        self._age_rows(engine, 40)  # bir sonraki takvim ayı, ama tazelik süresi dolmadı
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(self.calls), calls_after_first, "ay değişince aynı veri yeniden alınmamalı")
        self.assertEqual(engine.spend_status()["lookups"], 1)

    def test_pair_is_refreshed_once_after_the_refresh_window(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.fill_once())
        self._age_rows(engine, 91)
        report = asyncio.run(engine.fill_once())
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["charged"], 1)
        # Tazelendikten hemen sonra yeniden alınmaz.
        self.assertEqual(asyncio.run(engine.fill_once())["status"], "complete")

    def test_not_declarable_is_retried_only_after_the_longer_window(self):
        engine = self._engine(codes=("851713000000",), origins="TR", items=[])
        asyncio.run(engine.fill_once())
        calls_after_first = len(self.calls)
        self._age_rows(engine, 91)
        self.assertEqual(asyncio.run(engine.fill_once())["status"], "complete")
        self.assertEqual(len(self.calls), calls_after_first)
        self._age_rows(engine, 181)
        again = asyncio.run(engine.fill_once())
        self.assertEqual(again["not_declarable"], 1)
        self.assertGreater(len(self.calls), calls_after_first)

    def test_never_fetched_codes_come_before_refresh_due_ones(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.fill_once())
        self._age_rows(engine, 91)
        engine.code_source = lambda: ["610910001000", "851713000000"]
        queue, never, due, _ = engine._queue(engine.candidates())
        self.assertEqual(never, 1)
        self.assertEqual(due, 1)
        self.assertEqual(queue[0], ("8517130000", "TR"), "hiç alınmamış kod önce gelmeli")

    def test_plan_separates_pending_from_refresh_due_and_estimates_monthly_cost(self):
        engine = self._engine(
            codes=("610910001000", "851713000000"), origins="TR",
            items=[_item("6109100010"), _item("8517130000")],
        )
        plan = engine.fill_plan()
        self.assertEqual(plan["pending_pairs"], 2)
        self.assertEqual(plan["refresh_due_pairs"], 0)
        self.assertEqual(plan["refresh_days"], 90)
        # 2 çift x $0,015, 90 günde bir tazeleme -> aylık pay 30/90.
        self.assertAlmostEqual(plan["estimated_monthly_usd"], 0.01)
        asyncio.run(engine.fill_once())
        self._age_rows(engine, 91)
        plan = engine.fill_plan()
        self.assertEqual(plan["pending_pairs"], 0)
        self.assertEqual(plan["refresh_due_pairs"], 2)

    def test_lookup_flags_a_stale_archive_without_calling_the_actor(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        calls_after_lookup = len(self.calls)
        self._age_rows(engine, 120)
        payload = asyncio.run(engine.lookup("610910001000", origin="TR")).as_dict()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["from_archive"])
        self.assertTrue(payload["stale"])
        self.assertGreaterEqual(payload["age_days"], 119)
        self.assertTrue(any("gün önce" in item for item in payload["warnings"]))
        self.assertEqual(len(self.calls), calls_after_lookup, "bayat arşiv için ücretli çağrı yapılmamalı")

    def test_fresh_archive_is_not_flagged_stale(self):
        engine = self._engine(codes=("610910001000",), origins="TR")
        asyncio.run(engine.lookup("610910001000", origin="TR"))
        payload = asyncio.run(engine.lookup("610910001000", origin="TR")).as_dict()
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["age_days"], 0)

    def test_fill_tables_are_added_to_an_existing_database(self):
        # Eski şemalı veritabanı: ALTER TABLE korumalı göç sütunları eklemeli.
        import sqlite3

        path = Path(self._tmp.name) / "eu_taric.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE lookups (
                    goods_code TEXT NOT NULL, partner_country TEXT NOT NULL,
                    snapshot_month TEXT NOT NULL DEFAULT '', summary_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (goods_code, partner_country, snapshot_month)
                );
                CREATE TABLE fill_spend (period TEXT PRIMARY KEY, lookups INTEGER NOT NULL DEFAULT 0,
                    usd REAL NOT NULL DEFAULT 0);
                INSERT INTO fill_spend(period,lookups,usd) VALUES('2026-08', 3, 0.045);
                """
            )
        engine = self._engine(codes=("610910001000",), origins="TR")
        with sqlite3.connect(engine.db_path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(fill_spend)")}
            rows = connection.execute("SELECT lookups FROM fill_spend WHERE period='2026-08'").fetchall()
        self.assertIn("updated_at", columns)
        self.assertEqual(rows[0][0], 3)


if __name__ == "__main__":
    unittest.main()
