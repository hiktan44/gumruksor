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


if __name__ == "__main__":
    unittest.main()
