"""Brezilya ComexStat motoru: NCM eşlemesi, süzgeç seçimi, arşiv, hız sınırı, odak.

Gerçek ağ erişimi yoktur; gövdeler 18.09.2026'da canlı ölçülen biçimi taşır
(``{"data":{"list":[{"year","country","metricFOB","metricKG"}]}}``, değerler dizge,
boş sonuç ``{"list":[]}``).

Korunan değişmezler:

* 8 haneli kod NCM tablosunda varsa tek NCM; yoksa HS6 altındaki NCM listesi; NCM tablosu
  yoksa ``heading``; 2 hane ``chapter`` — ve her düşme sonuçta yazılır,
* NCM tablosu bir kez indirilir, arşivden yeniden kullanılır,
* istek ``language: en`` taşır; İngilizce ad Türkçeye çevrilir, bilinmeyen ad olduğu gibi kalır,
* Türkiye odağı ad eşleşmesiyle bulunur, listede yoksa ``present: False`` denir,
* 429'da ısrar yok; taze arşiv ağa çıkmaz.
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

import comexstat as cs

_NCM_TABLE = {"data": {"list": [
    {"noNCM": "Smartphones", "unit": "UNIDADE", "coNcm": "85171300"},
    {"noNCM": "Other telephones", "unit": "UNIDADE", "coNcm": "85171400"},
    {"noNCM": "T-shirts cotton", "unit": "KG", "coNcm": "61091000"},
    {"noNCM": "T-shirts other", "unit": "KG", "coNcm": "61099000"},
] + [{"noNCM": f"x{i}", "unit": "KG", "coNcm": f"{10000000 + i}"} for i in range(1000)]}}

_MARKET = {"data": {"list": [
    {"year": "2024", "country": "China", "metricFOB": "441309928", "metricKG": "398847"},
    {"year": "2024", "country": "Germany", "metricFOB": "2289791", "metricKG": "2093"},
    {"year": "2024", "country": "Turkey", "metricFOB": "120000", "metricKG": "300"},
    {"year": "2024", "country": "Ruritania", "metricFOB": "5000", "metricKG": "10"},
]}, "success": True, "message": None, "language": "en"}

_EMPTY = {"data": {"list": []}, "success": True, "message": None, "language": "en"}


def _engine(handler, tmp: Path, **kwargs) -> cs.ComexStatEngine:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return cs.ComexStatEngine(tmp, http=client, enabled=True, delay_seconds=0.0,
                              cooldown_seconds=kwargs.pop("cooldown_seconds", 30.0), **kwargs)


def _handler(calls: list, market=_MARKET, table=_NCM_TABLE):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tables/ncm"):
            calls.append(("ncm", None))
            return httpx.Response(200, json=table)
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append(("general", body))
        return httpx.Response(200, json=market)
    return handler


class PureFunctionTests(unittest.TestCase):
    def test_product_query_levels(self) -> None:
        codes = ["85171300", "85171400", "61091000"]
        self.assertEqual(cs.product_query("851713000000", codes)[1:], ("ncm8", "85171300"))
        # 8 hane tabloda yoksa HS6 altındaki NCM'ler.
        f, level, code = cs.product_query("85179900", codes)
        self.assertEqual((level, code), ("hs4", "8517"))
        f, level, code = cs.product_query("851714", codes)
        self.assertEqual((f["filter"], f["values"], level), ("ncm", ["85171400"], "hs6"))
        self.assertEqual(cs.product_query("6109", codes)[1:], ("hs4", "6109"))
        self.assertEqual(cs.product_query("61", codes)[1:], ("hs2", "61"))
        self.assertEqual(cs.product_query("6", codes)[0], None)

    def test_parse_rows_translates_known_names_only(self) -> None:
        rows = cs.parse_rows(_MARKET)
        names = {row["partner_source"]: row["partner"] for row in rows}
        self.assertEqual(names["Germany"], "Almanya")
        self.assertEqual(names["Turkey"], "Türkiye")
        self.assertEqual(names["Ruritania"], "Ruritania")  # bilinmeyen ad uydurulmaz
        self.assertEqual(rows[0]["value_usd"], 441309928.0)

    def test_rank_partners(self) -> None:
        ranked = cs.rank_partners(cs.parse_rows(_MARKET), limit=10)
        self.assertEqual([r["partner_source"] for r in ranked][:2], ["China", "Germany"])
        self.assertAlmostEqual(sum(r["share"] for r in ranked), 1.0, places=4)
        self.assertEqual(ranked[0]["unit_price_usd_per_kg"], round(441309928 / 398847, 2))

    def test_empty_and_malformed(self) -> None:
        self.assertEqual(cs.parse_rows(_EMPTY), [])
        self.assertEqual(cs.parse_rows({"error": {"code": 400}}), [])
        self.assertEqual(cs.parse_rows(None), [])


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_request_shape_and_ncm_table_once(self) -> None:
        calls: list = []
        engine = _engine(_handler(calls), self.tmp)
        first = asyncio.run(engine.markets("851713000000", flow="M", year=2024))
        second = asyncio.run(engine.markets("610910", flow="X", year=2023))
        asyncio.run(engine.close())
        kinds = [k for k, _ in calls]
        self.assertEqual(kinds.count("ncm"), 1)  # tablo bir kez indirildi
        bodies = [b for k, b in calls if k == "general"]
        self.assertEqual(bodies[0]["flow"], "import")
        self.assertEqual(bodies[0]["language"], "en")
        self.assertEqual(bodies[0]["period"], {"from": "2024-01", "to": "2024-12"})
        self.assertEqual(bodies[0]["filters"], [{"filter": "ncm", "values": ["85171300"]}])
        self.assertEqual(bodies[0]["details"], ["country"])
        self.assertEqual(bodies[1]["flow"], "export")
        self.assertEqual(bodies[1]["filters"], [{"filter": "ncm", "values": ["61091000"]}])
        self.assertEqual(first.match_level, "ncm8")
        self.assertEqual(second.match_level, "hs6")
        self.assertEqual(first.status, "ok")

    def test_unknown_ncm_falls_back_to_hs6_and_says_so(self) -> None:
        calls: list = []
        engine = _engine(_handler(calls), self.tmp)
        report = asyncio.run(engine.markets("85171399", year=2024))
        asyncio.run(engine.close())
        body = [b for k, b in calls if k == "general"][0]
        self.assertEqual(body["filters"][0]["filter"], "ncm")
        self.assertEqual(sorted(body["filters"][0]["values"]), ["85171300"])
        self.assertEqual(report.match_level, "hs6")
        self.assertTrue(any("NCM listesinde yok" in w for w in report.warnings))

    def test_without_ncm_table_falls_back_to_heading(self) -> None:
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tables/ncm"):
                return httpx.Response(500, json={"error": {"code": 500}})
            calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json=_MARKET)

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("851713", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(calls[0]["filters"], [{"filter": "heading", "values": ["8517"]}])
        self.assertEqual(report.match_level, "hs4")
        self.assertTrue(any("NCM tablosu alınamadı" in w for w in report.warnings))

    def test_focus_finds_turkiye_by_name(self) -> None:
        engine = _engine(_handler([]), self.tmp)
        report = asyncio.run(engine.markets("85171300", year=2024, limit=1))
        asyncio.run(engine.close())
        self.assertEqual(len(report.partners), 1)
        self.assertTrue(report.focus["present"])
        self.assertEqual(report.focus["partner"], "Türkiye")
        self.assertEqual(report.focus["rank"], 3)

    def test_focus_absent_is_stated(self) -> None:
        market = {"data": {"list": [{"year": "2024", "country": "China", "metricFOB": "10", "metricKG": "1"}]}}
        engine = _engine(_handler([], market=market), self.tmp)
        report = asyncio.run(engine.markets("85171300", year=2024))
        asyncio.run(engine.close())
        self.assertFalse(report.focus["present"])
        self.assertIsNone(report.focus["value_usd"])

    def test_fresh_archive_never_touches_the_network(self) -> None:
        calls: list = []
        engine = _engine(_handler(calls), self.tmp)
        asyncio.run(engine.markets("85171300", year=2024))
        n = len(calls)
        second = asyncio.run(engine.markets("85171300", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(len(calls), n)
        self.assertTrue(second.from_archive)
        self.assertEqual(second.match_level, "ncm8")

    def test_rate_limit_is_not_retried_and_opens_cooldown(self) -> None:
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tables/ncm"):
                return httpx.Response(200, json=_NCM_TABLE)
            calls.append(1)
            return httpx.Response(429, json={"error": {"code": 429, "message": "limite"}})

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("85171300", year=2024))
        self.assertEqual(len(calls), 1)
        self.assertEqual(report.status, "rate_limited")
        self.assertGreater(engine.status()["cooldown_seconds"], 0)
        asyncio.run(engine.close())

    def test_empty_result_is_not_found(self) -> None:
        engine = _engine(_handler([], market=_EMPTY), self.tmp)
        report = asyncio.run(engine.markets("85171300", year=2027))
        asyncio.run(engine.close())
        self.assertEqual(report.status, "not_found")
        self.assertEqual(report.partners, [])

    def test_suspiciously_small_ncm_table_is_rejected(self) -> None:
        tiny = {"data": {"list": [{"coNcm": "85171300", "noNCM": "x"}]}}
        calls: list = []
        engine = _engine(_handler(calls, table=tiny), self.tmp)
        asyncio.run(engine.markets("851713", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(engine.status()["ncm_codes"], 0)
        body = [b for k, b in calls if k == "general"][0]
        self.assertEqual(body["filters"][0]["filter"], "heading")

    def test_outbound_host_is_validated(self) -> None:
        engine = _engine(_handler([]), self.tmp)
        with self.assertRaises(Exception):
            asyncio.run(engine._request("https://example.com/general", body={}))
        asyncio.run(engine.close())


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as web_app

        self.web_app = web_app
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.original_engine = web_app.comexstat_engine
        self.addCleanup(lambda: setattr(web_app, "rate_limiter", self.original_limiter))
        self.addCleanup(lambda: setattr(web_app, "comexstat_engine", self.original_engine))

    def _client(self):
        from starlette.testclient import TestClient

        return TestClient(self.web_app.app, base_url="https://gumruksor.com")

    def test_market_route_passes_the_query_through(self) -> None:
        seen: dict = {}

        class _Stub:
            async def markets(self, gtip, **kwargs):
                seen["gtip"] = gtip
                seen.update(kwargs)
                return cs.ComexStatReport(product="85171300", flow="M", year=2024, status="ok")

        self.web_app.comexstat_engine = _Stub()
        with self._client() as client:
            response = client.get("/api/foreign/comexstat?gtip=851713000000&flow=X&year=2024&limit=7")
        if response.status_code == 403:
            self.skipTest("Paket kilidi etkin; uç yetkiyle korunuyor.")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "comexstat")
        self.assertEqual(body["reporter"], "BR")
        self.assertEqual(seen["flow"], "X")
        self.assertEqual(seen["year"], 2024)
        self.assertEqual(seen["limit"], 7)

    def test_status_route(self) -> None:
        with self._client() as client:
            response = client.get("/api/foreign/comexstat/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ncm_codes", response.json())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
