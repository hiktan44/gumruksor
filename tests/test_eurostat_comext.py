"""Eurostat Comext motoru: JSON-stat çözümü, toplam kodları, CN8→HS6 düşme, arşiv, 429.

Gerçek ağ erişimi yoktur; gövdeler 18.09.2026'da canlı uçtan ölçülen yapıyı taşır
(``id``/``size`` sırası, ``category.index``/``label``, ağırlık 100 kg biriminde).

Korunan değişmezler:

* sıralamaya yalnız **ISO2** partnerler girer; ``WORLD``/``EXT_EU27_2020`` gibi toplam
  kodlar listeye girmez (ISO2 toplamı zaten WORLD'e eşit — ikisini de saymak çift sayım),
* ağırlık ``QUANTITY_IN_100KG`` × 100 ile kilograma çevrilir,
* istek CN8 ile başlar, boş dönerse HS6'ya düşer ve sonuç işaretlenir,
* taze arşiv ağa hiç çıkmaz; 429'da ısrar edilmez,
* Türkiye odağı listede yoksa ``present: False`` ile açıkça söylenir.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eurostat_comext as cx

# Ölçülen boyut sırası: freq, reporter, partner, product, flow, indicators, time.
# Düz indeks = partner_konumu × 2 + gösterge_konumu.
_PARTNERS = ["CN", "CZ", "EXT_EU27_2020", "TR", "VN", "WORLD"]
_LABELS = {"CN": "China", "CZ": "Czechia", "EXT_EU27_2020": "Extra-EU27 (from 2020)",
           "TR": "Türkiye", "VN": "Vietnam", "WORLD": "All countries of the world"}
_VALUES = {"CZ": 5_228_678_636.0, "CN": 1_163_099_781.0, "VN": 946_246_952.0, "TR": 84_467.0}
_WORLD = sum(_VALUES.values())
_HKG = {"CZ": 123_456.0, "CN": 98_765.0, "VN": 55_555.0, "TR": 2.18, "WORLD": 277_778.18,
        "EXT_EU27_2020": 154_322.18}


def _payload(product: str = "851713", *, empty: bool = False) -> dict:
    value: dict[str, float] = {}
    if not empty:
        for code, number in {**_VALUES, "WORLD": _WORLD, "EXT_EU27_2020": _VALUES["CN"] + _VALUES["VN"] + _VALUES["TR"]}.items():
            position = _PARTNERS.index(code)
            value[str(position * 2)] = number
            value[str(position * 2 + 1)] = _HKG[code]
    return {
        "version": "2.0", "class": "dataset", "label": "EU trade since 1988 by HS2-4-6 and CN8",
        "updated": "2026-09-15T11:00:00+0200",
        "value": value,
        "id": ["freq", "reporter", "partner", "product", "flow", "indicators", "time"],
        "size": [1, 1, len(_PARTNERS), 1, 1, 2, 1],
        "dimension": {
            "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
            "reporter": {"category": {"index": {"DE": 0}, "label": {"DE": "Germany"}}},
            "partner": {"category": {"index": {c: i for i, c in enumerate(_PARTNERS)}, "label": _LABELS}},
            "product": {"category": {"index": {product: 0}, "label": {product: "Telephones"}}},
            "flow": {"category": {"index": {"1": 0}, "label": {"1": "IMPORT"}}},
            "indicators": {"category": {"index": {"VALUE_IN_EUROS": 0, "QUANTITY_IN_100KG": 1}}},
            "time": {"category": {"index": {"2024": 0}}},
        },
    }


def _engine(handler, tmp: Path, **kwargs) -> cx.ComextEngine:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return cx.ComextEngine(tmp, http=client, enabled=True, delay_seconds=0.0,
                           cooldown_seconds=kwargs.pop("cooldown_seconds", 30.0), **kwargs)


class PureFunctionTests(unittest.TestCase):
    def test_product_codes_start_from_cn8(self) -> None:
        self.assertEqual(cx.product_codes("851713000000"), ["85171300", "851713", "8517", "85"])
        self.assertEqual(cx.product_codes("8517"), ["8517", "85"])
        self.assertEqual(cx.product_codes("8"), [])

    def test_reporter_code_maps_greece_and_rejects_non_eu(self) -> None:
        self.assertEqual(cx.reporter_code("gr"), "EL")
        self.assertEqual(cx.reporter_code("DE"), "DE")
        self.assertIsNone(cx.reporter_code("TR"))
        self.assertIsNone(cx.reporter_code("GB"))

    def test_parse_keeps_only_iso2_partners_and_world(self) -> None:
        parsed = cx.parse_jsonstat(_payload())
        codes = sorted(item["partner_code"] for item in parsed["partners"])
        self.assertEqual(codes, ["CN", "CZ", "TR", "VN"])
        # Toplam kodu sıralamaya girmez; ISO2 toplamı zaten dünyaya eşit.
        self.assertNotIn("EXT_EU27_2020", codes)
        self.assertEqual(parsed["world"]["value_eur"], _WORLD)
        self.assertAlmostEqual(sum(p["value_eur"] for p in parsed["partners"]), _WORLD)

    def test_weight_is_converted_from_hundred_kg(self) -> None:
        parsed = cx.parse_jsonstat(_payload())
        turkiye = next(p for p in parsed["partners"] if p["partner_code"] == "TR")
        self.assertEqual(turkiye["net_weight_kg"], 218.0)
        self.assertEqual(turkiye["partner"], "Türkiye")

    def test_rank_partners_uses_world_as_denominator(self) -> None:
        parsed = cx.parse_jsonstat(_payload())
        ranked = cx.rank_partners(parsed["partners"], world=parsed["world"], limit=10)
        self.assertEqual([p["partner_code"] for p in ranked], ["CZ", "CN", "VN", "TR"])
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertAlmostEqual(sum(p["share"] for p in ranked), 1.0, places=3)
        self.assertEqual(ranked[3]["unit_price_eur_per_kg"], round(84_467.0 / 218.0, 2))

    def test_malformed_payload_yields_nothing(self) -> None:
        self.assertEqual(cx.parse_jsonstat({"value": "x"})["partners"], [])
        self.assertEqual(cx.parse_jsonstat(None)["partners"], [])


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_request_shape(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(parse_qs(urlparse(str(request.url)).query))
            return httpx.Response(200, json=_payload("85171300"))

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("851713000000", reporter="de", flow="M", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(report.status, "ok")
        self.assertEqual(len(seen), 1)
        q = seen[0]
        self.assertEqual(q["product"], ["85171300"])
        self.assertEqual(q["reporter"], ["DE"])
        self.assertEqual(q["flow"], ["1"])
        self.assertEqual(q["time"], ["2024"])
        self.assertEqual(sorted(q["indicators"]), ["QUANTITY_IN_100KG", "VALUE_IN_EUROS"])
        self.assertEqual(report.match_level, "cn8")
        self.assertEqual(report.as_dict()["reporter_name"], "Almanya")

    def test_export_flow_maps_to_two(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(parse_qs(urlparse(str(request.url)).query))
            return httpx.Response(200, json=_payload("85171300"))

        engine = _engine(handler, self.tmp)
        asyncio.run(engine.markets("85171300", reporter="FR", flow="X", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(seen[0]["flow"], ["2"])

    def test_empty_cn8_falls_back_to_hs6_and_says_so(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlparse(str(request.url)).query)
            seen.append(q["product"][0])
            if q["product"][0] == "85171300":
                return httpx.Response(200, json=_payload("85171300", empty=True))
            return httpx.Response(200, json=_payload("851713"))

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("851713000000", reporter="DE", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(seen, ["85171300", "851713"])
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.product, "851713")
        self.assertEqual(report.match_level, "hs6")
        self.assertTrue(any("HS6" in w for w in report.warnings))

    def test_focus_reports_turkiye_rank_and_share(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload("85171300"))

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("85171300", reporter="DE", year=2024, limit=2))
        asyncio.run(engine.close())
        # Liste 2 ile kırpılsa da odak partner tam sıralamadan bulunur.
        self.assertEqual(len(report.partners), 2)
        self.assertTrue(report.focus["present"])
        self.assertEqual(report.focus["rank"], 4)
        self.assertAlmostEqual(report.focus["share"], 84_467.0 / _WORLD, places=6)

    def test_focus_absent_is_stated_not_invented(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload("85171300"))

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("85171300", reporter="DE", year=2024, focus="BR"))
        asyncio.run(engine.close())
        self.assertFalse(report.focus["present"])
        self.assertIsNone(report.focus["value_eur"])

    def test_fresh_archive_never_touches_the_network(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=_payload("85171300"))

        engine = _engine(handler, self.tmp)
        asyncio.run(engine.markets("85171300", reporter="DE", year=2024))
        second = asyncio.run(engine.markets("85171300", reporter="DE", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(len(calls), 1)
        self.assertTrue(second.from_archive)
        self.assertEqual(second.age_days, 0)
        self.assertEqual(second.status, "ok")

    def test_rate_limit_is_not_retried_and_opens_cooldown(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={})

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("85171300", reporter="DE", year=2024))
        self.assertEqual(len(calls), 1)
        self.assertEqual(report.status, "rate_limited")
        self.assertGreater(engine.status()["cooldown_seconds"], 0)
        asyncio.run(engine.close())

    def test_non_eu_reporter_is_rejected_before_any_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("AB dışı raporlayan için istek yapılmamalı.")

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("85171300", reporter="TR", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(report.status, "not_found")

    def test_nothing_at_any_level_is_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_payload("x", empty=True))

        engine = _engine(handler, self.tmp)
        report = asyncio.run(engine.markets("85171300", reporter="DE", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(report.status, "not_found")
        self.assertEqual(report.partners, [])

    def test_outbound_host_is_validated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("İzin listesi dışı adrese istek yapılmamalı.")

        engine = _engine(handler, self.tmp)
        with self.assertRaises(Exception):
            asyncio.run(engine._get_json("https://example.com/x"))
        asyncio.run(engine.close())


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as web_app

        self.web_app = web_app
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.original_engine = web_app.comext_engine
        self.addCleanup(lambda: setattr(web_app, "rate_limiter", self.original_limiter))
        self.addCleanup(lambda: setattr(web_app, "comext_engine", self.original_engine))

    def _client(self):
        from starlette.testclient import TestClient

        return TestClient(self.web_app.app, base_url="https://gumruksor.com")

    def test_market_route_passes_the_query_through(self) -> None:
        seen: dict = {}

        class _Stub:
            async def markets(self, gtip, **kwargs):
                seen["gtip"] = gtip
                seen.update(kwargs)
                return cx.ComextReport(product="85171300", reporter="DE", flow="M", year=2024, status="ok")

        self.web_app.comext_engine = _Stub()
        with self._client() as client:
            response = client.get("/api/foreign/comext?gtip=851713000000&reporter=DE&flow=M&year=2024&limit=5")
        if response.status_code == 403:
            self.skipTest("Paket kilidi etkin; uç yetkiyle korunuyor.")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "eurostat_comext")
        self.assertEqual(body["currency"], "EUR")
        self.assertEqual(seen["reporter"], "DE")
        self.assertEqual(seen["flow"], "M")
        self.assertEqual(seen["year"], 2024)
        self.assertEqual(seen["limit"], 5)

    def test_status_route_lists_reporters(self) -> None:
        with self._client() as client:
            response = client.get("/api/foreign/comext/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("DE", response.json()["reporters"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
