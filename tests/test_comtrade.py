"""UN Comtrade motoru: satır süzme, hız sınırı, arşiv ve eğilim.

Gerçek ağ erişimi yoktur; yanıtlar ``httpx.MockTransport`` ile üretilir ve gövdeler
17.09.2026'da canlı uçtan alınan **gerçek** yanıtların biçimini taşır.

Bu dosyanın koruduğu değişmezler:

* **kırılım satırları toplama girmez** — Almanya 19.154.002 USD'dir, 55.768.235 değil
  (bu süzgeç olmadan aynı ticaret üç kez sayılıyordu, canlı ölçüldü),
* istek **her zaman** ``motCode=0`` ve ``customsCode=C00`` taşır,
* 500 satırlık yanıt ``truncated`` işaretler ve uyarı üretir,
* **429'da ısrar edilmez**: tek istek, soğuma penceresi açılır,
* taze arşiv ağa hiç çıkmaz,
* eğilim yıl başına **tek** istek yapar (kaynak çok dönemi 400 ile reddediyor).
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

import comtrade


def _row(
    *,
    partner: int,
    value: float,
    weight: float | None = None,
    mot: int = 0,
    customs: str = "C00",
    partner2: int = 0,
    year: int = 2024,
    flow: str = "X",
    code: str = "8517",
) -> dict:
    return {
        "typeCode": "C",
        "freqCode": "A",
        "refYear": year,
        "reporterCode": comtrade.TURKIYE_CODE,
        "flowCode": flow,
        "partnerCode": partner,
        "partner2Code": partner2,
        "classificationCode": "H6",
        "cmdCode": code,
        "motCode": mot,
        "customsCode": customs,
        "fobvalue": value,
        "primaryValue": value,
        "netWgt": weight,
        "cifvalue": None,
    }


# Almanya'nın canlı ölçülmüş gerçek rakamları: toplam satır 19.154.002 USD / 107.318 kg.
# Kırılım satırları eklenince körlemesine toplam 55.768.235 USD'ye çıkıyor (~3 katı).
GERMANY_TRUE_VALUE = 19_154_002.0
# Üç kırılım boyutu da toplanırsa (taşıma şekli ×2 + ikinci partner ×1):
GERMANY_NAIVE_SUM = 74_922_237.0

_GERMANY_ROWS = [
    _row(partner=276, value=GERMANY_TRUE_VALUE, weight=107_318.0),
    # Aynı ticaretin taşıma şekli kırılımları: toplam satırla ÇAKIŞIR, toplanmaz.
    _row(partner=276, value=20_000_000.0, weight=60_000.0, mot=1),
    _row(partner=276, value=16_614_233.0, weight=40_000.0, mot=5),
    # İkinci partner (sevk/menşe ülkesi) kırılımı: AYNI değeri taşır. Süzülmezse
    # Almanya tabloda iki kez çıkar ve pay yüzdeleri yarıya düşer (canlı ölçüldü).
    _row(partner=276, value=GERMANY_TRUE_VALUE, weight=107_318.0, partner2=276),
]

_MARKET_PAYLOAD = {
    "elapsedTime": "0.2 secs",
    "count": 6,
    "data": [
        *_GERMANY_ROWS,
        _row(partner=784, value=22_813_803.0, weight=77_943.0),
        _row(partner=616, value=10_186_617.0, weight=66_612.0),
        _row(partner=0, value=120_000_000.0, weight=900_000.0),
        # "Dünya" kodunun da ikinci partner kırılımı var: bu satır dünya toplamı DEĞİL.
        _row(partner=0, value=11_451_512.0, weight=12_409.0, partner2=528),
    ],
}

_AREAS_PAYLOAD = {
    "results": [
        {"id": "276", "text": "Germany"},
        {"id": "784", "text": "United Arab Emirates"},
        {"id": "616", "text": "Poland"},
        {"id": "0", "text": "World"},
    ]
}


def _engine(handler, *, tmp: Path, **kwargs) -> comtrade.ComtradeEngine:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    return comtrade.ComtradeEngine(
        tmp,
        http=client,
        enabled=True,
        delay_seconds=0.0,
        cooldown_seconds=kwargs.pop("cooldown_seconds", 30.0),
        **kwargs,
    )


class PureFunctionTests(unittest.TestCase):
    def test_hs_code_trims_the_national_tail(self) -> None:
        # GTİP'in 7. haneden sonrası ulusaldır; Comtrade'de karşılığı yok.
        self.assertEqual(comtrade.hs_code("851713000000"), "851713")
        self.assertEqual(comtrade.hs_code("8517"), "8517")
        self.assertEqual(comtrade.hs_code("85"), "85")
        self.assertEqual(comtrade.hs_code("8"), "")
        self.assertEqual(comtrade.hs_code(None), "")

    def test_only_aggregate_rows_are_parsed(self) -> None:
        rows = comtrade.parse_rows(_MARKET_PAYLOAD)
        self.assertEqual(len(rows), 4)  # 3 partner + Dünya; üç boyuttaki kırılımlar düştü
        germany = [row for row in rows if row["partner_code"] == 276]
        self.assertEqual(len(germany), 1)
        self.assertEqual(germany[0]["value_usd"], GERMANY_TRUE_VALUE)

    def test_naive_sum_would_have_multiplied_germany(self) -> None:
        # Asıl hatanın gerileme kilidi: kırılım satırları süzülmezse rakam şişiyor.
        # Canlıda ölçülen iki boyutlu hâli 55.768.235 USD idi (~3 kat); üçüncü boyut
        # (ikinci partner) da eklenince 74.922.237 USD'ye, yani ~3,9 katına çıkıyor.
        naive = sum(
            float(row["fobvalue"])
            for row in _MARKET_PAYLOAD["data"]
            if row["partnerCode"] == 276
        )
        self.assertEqual(naive, GERMANY_NAIVE_SUM)
        parsed = sum(
            row["value_usd"] for row in comtrade.parse_rows(_MARKET_PAYLOAD)
            if row["partner_code"] == 276
        )
        self.assertEqual(parsed, GERMANY_TRUE_VALUE)
        self.assertNotEqual(parsed, naive)

    def test_is_total_row_accepts_string_codes(self) -> None:
        self.assertTrue(comtrade.is_total_row({"motCode": "0", "customsCode": "C00", "partner2Code": "0"}))
        self.assertFalse(comtrade.is_total_row({"motCode": 1, "customsCode": "C00", "partner2Code": 0}))
        self.assertFalse(comtrade.is_total_row({"motCode": 0, "customsCode": "C01", "partner2Code": 0}))
        # Üçüncü boyut: ikinci partner kırılımı da toplam satırı değildir.
        self.assertFalse(comtrade.is_total_row({"motCode": 0, "customsCode": "C00", "partner2Code": 276}))

    def test_second_partner_breakdown_does_not_duplicate_a_country(self) -> None:
        """Canlı ölçümün gerileme kilidi: partner2 süzülmezse her ülke iki kez çıkıyordu."""
        ranked = comtrade.rank_partners(comtrade.parse_rows(_MARKET_PAYLOAD))
        codes = [item["partner_code"] for item in ranked]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(codes.count(276), 1)

    def test_world_row_is_not_taken_from_a_second_partner_breakdown(self) -> None:
        """``partnerCode=0`` satırı tek başına dünya toplamı DEĞİL; kırılımı da var."""
        world = comtrade.world_total(comtrade.parse_rows(_MARKET_PAYLOAD))
        self.assertEqual(world["value_usd"], 120_000_000.0)
        self.assertNotEqual(world["value_usd"], 11_451_512.0)

    def test_rank_partners_drops_world_and_derives_unit_price(self) -> None:
        rows = comtrade.parse_rows(_MARKET_PAYLOAD)
        ranked = comtrade.rank_partners(rows, names={"276": "Germany", "784": "BAE"})
        self.assertEqual([item["partner_code"] for item in ranked], [784, 276, 616])
        self.assertNotIn(comtrade.WORLD_CODE, [item["partner_code"] for item in ranked])
        germany = next(item for item in ranked if item["partner_code"] == 276)
        self.assertEqual(germany["partner"], "Germany")
        self.assertEqual(germany["unit_price_usd_per_kg"], round(GERMANY_TRUE_VALUE / 107_318.0, 2))
        self.assertAlmostEqual(sum(item["share"] for item in ranked), 1.0, places=3)

    def test_unit_price_is_not_invented_without_weight(self) -> None:
        ranked = comtrade.rank_partners([{"partner_code": 4, "value_usd": 10.0, "net_weight_kg": None}])
        self.assertIsNone(ranked[0]["unit_price_usd_per_kg"])

    def test_world_total_comes_from_the_source_row(self) -> None:
        world = comtrade.world_total(comtrade.parse_rows(_MARKET_PAYLOAD))
        self.assertEqual(world["value_usd"], 120_000_000.0)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _seed_areas(self, engine: comtrade.ComtradeEngine) -> None:
        with engine._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO reference_areas(code,name,fetched_at) VALUES(?,?,?)",
                ("276", "Germany", "2026-09-17T00:00:00+00:00"),
            )

    def test_request_always_carries_the_aggregate_filters(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "partnerAreas" in request.url.path:
                return httpx.Response(200, json=_AREAS_PAYLOAD)
            seen.append(parse_qs(urlparse(str(request.url)).query))
            return httpx.Response(200, json=_MARKET_PAYLOAD)

        engine = _engine(handler, tmp=self.tmp)
        report = asyncio.run(engine.markets("851713000000", year=2024))
        asyncio.run(engine.close())

        self.assertEqual(report.status, "ok")
        self.assertEqual(len(seen), 1)
        query = seen[0]
        # Bu üç süzgeç olmadan 500 satırlık pencere kırılımlarla dolar ve aynı ülke
        # birden çok kez listelenir.
        self.assertEqual(query["motCode"], ["0"])
        self.assertEqual(query["customsCode"], ["C00"])
        self.assertEqual(query["partner2Code"], ["0"])
        self.assertEqual(query["cmdCode"], ["851713"])
        self.assertEqual(query["period"], ["2024"])
        self.assertEqual(query["flowCode"], ["X"])
        self.assertEqual(query["reporterCode"], [str(comtrade.TURKIYE_CODE)])

    def test_germany_is_reported_once_and_not_tripled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "partnerAreas" in request.url.path:
                return httpx.Response(200, json=_AREAS_PAYLOAD)
            return httpx.Response(200, json=_MARKET_PAYLOAD)

        engine = _engine(handler, tmp=self.tmp)
        report = asyncio.run(engine.markets("8517", year=2024))
        asyncio.run(engine.close())

        germany = [item for item in report.partners if item["partner_code"] == 276]
        self.assertEqual(len(germany), 1)
        self.assertEqual(germany[0]["value_usd"], GERMANY_TRUE_VALUE)
        self.assertEqual(germany[0]["partner"], "Germany")
        payload = report.as_dict()
        self.assertEqual(payload["source"], "un_comtrade")
        self.assertIn("maliyet", payload["statistic_only_note"])

    def test_row_cap_is_reported_as_truncated(self) -> None:
        big = {
            "data": [
                _row(partner=100 + index, value=1000.0 - index)
                for index in range(comtrade.PREVIEW_ROW_CAP)
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "partnerAreas" in request.url.path:
                return httpx.Response(200, json=_AREAS_PAYLOAD)
            return httpx.Response(200, json=big)

        engine = _engine(handler, tmp=self.tmp)
        report = asyncio.run(engine.markets("8517", year=2024))
        asyncio.run(engine.close())

        self.assertTrue(report.truncated)
        self.assertTrue(any("500" in warning for warning in report.warnings))

    def test_rate_limit_is_not_retried_and_opens_a_cooldown(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(429, json={"message": "too many requests"})

        engine = _engine(handler, tmp=self.tmp)
        report = asyncio.run(engine.markets("8517", year=2024))

        # Tek istek: 429'da ısrar etmek kaynağı da bizi de yavaşlatıyor (ölçüldü).
        self.assertEqual(len(calls), 1)
        self.assertEqual(report.status, "rate_limited")
        self.assertGreater(engine.status()["cooldown_seconds"], 0)
        asyncio.run(engine.close())

    def test_rate_limit_falls_back_to_the_archive(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={})

        engine = _engine(handler, tmp=self.tmp)
        self._seed_areas(engine)
        engine._store(
            "8517", comtrade.TURKIYE_CODE, "X", 2024,
            comtrade.parse_rows(_MARKET_PAYLOAD), truncated=False, source_url="https://x",
        )
        report = asyncio.run(engine.markets("8517", year=2024, refresh=True))
        asyncio.run(engine.close())

        self.assertEqual(report.status, "ok")
        self.assertTrue(report.from_archive)
        self.assertTrue(any("hız sınırı" in warning for warning in report.warnings))

    def test_fresh_archive_never_touches_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"Taze arşivde ağa çıkılmamalıydı: {request.url}")

        engine = _engine(handler, tmp=self.tmp)
        self._seed_areas(engine)
        engine._store(
            "8517", comtrade.TURKIYE_CODE, "X", 2024,
            comtrade.parse_rows(_MARKET_PAYLOAD), truncated=False, source_url="https://x",
        )
        report = asyncio.run(engine.markets("8517", year=2024))
        asyncio.run(engine.close())

        self.assertEqual(report.status, "ok")
        self.assertTrue(report.from_archive)
        self.assertEqual(report.age_days, 0)
        self.assertEqual(report.partners[0]["partner_code"], 784)

    def test_disabled_engine_reports_instead_of_failing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Kapalı motor ağa çıkmamalı.")

        transport = httpx.MockTransport(handler)
        engine = comtrade.ComtradeEngine(
            self.tmp, http=httpx.AsyncClient(transport=transport), enabled=False, delay_seconds=0.0
        )
        report = asyncio.run(engine.markets("8517", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(report.status, "disabled")

    def test_invalid_code_is_rejected_before_any_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Geçersiz kodda istek yapılmamalı.")

        engine = _engine(handler, tmp=self.tmp)
        report = asyncio.run(engine.markets("8", year=2024))
        asyncio.run(engine.close())
        self.assertEqual(report.status, "not_found")

    def test_trend_issues_one_request_per_year(self) -> None:
        periods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "partnerAreas" in request.url.path:
                return httpx.Response(200, json=_AREAS_PAYLOAD)
            query = parse_qs(urlparse(str(request.url)).query)
            # Kaynak çok dönemi 400 ile reddediyor: tek dönem şart.
            self.assertEqual(len(query["period"][0].split(",")), 1)
            periods.append(query["period"][0])
            return httpx.Response(200, json=_MARKET_PAYLOAD)

        engine = _engine(handler, tmp=self.tmp)
        result = asyncio.run(engine.trend("8517", years=3))
        asyncio.run(engine.close())

        self.assertEqual(len(periods), 3)
        self.assertEqual(len(set(periods)), 3)
        self.assertEqual(len(result["series"]), 3)
        self.assertEqual(
            result["series"], sorted(result["series"], key=lambda item: item["year"])
        )
        self.assertEqual(result["series"][0]["value_usd"], 120_000_000.0)

    def test_trend_can_follow_a_single_partner(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "partnerAreas" in request.url.path:
                return httpx.Response(200, json=_AREAS_PAYLOAD)
            return httpx.Response(200, json=_MARKET_PAYLOAD)

        engine = _engine(handler, tmp=self.tmp)
        result = asyncio.run(engine.trend("8517", years=2, partner=276))
        asyncio.run(engine.close())
        self.assertTrue(all(item["value_usd"] == GERMANY_TRUE_VALUE for item in result["series"]))

    def test_outbound_url_is_validated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("İzin listesi dışı adrese istek yapılmamalı.")

        engine = _engine(handler, tmp=self.tmp)
        with self.assertRaises(Exception):
            asyncio.run(engine._get_json("https://example.com/preview"))
        asyncio.run(engine.close())

    def test_status_reports_the_archive(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_AREAS_PAYLOAD)

        engine = _engine(handler, tmp=self.tmp)
        engine._store(
            "8517", comtrade.TURKIYE_CODE, "X", 2024, [], truncated=False, source_url="https://x"
        )
        status = engine.status()
        asyncio.run(engine.close())
        self.assertEqual(status["archived_queries"], 1)
        self.assertEqual(status["row_cap"], comtrade.PREVIEW_ROW_CAP)
        self.assertTrue(status["enabled"])


class RouteTests(unittest.TestCase):
    """Uçlar motoru doğru çağırıyor mu; ağ yok, motor yerine sahte nesne konur."""

    def setUp(self) -> None:
        import app as web_app

        self.web_app = web_app
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.original_engine = web_app.comtrade_engine
        self.addCleanup(lambda: setattr(web_app, "rate_limiter", self.original_limiter))
        self.addCleanup(lambda: setattr(web_app, "comtrade_engine", self.original_engine))

    def _client(self):
        from starlette.testclient import TestClient

        return TestClient(self.web_app.app, base_url="https://gumruksor.com")

    def test_market_route_passes_the_query_through(self) -> None:
        seen: dict = {}

        class _Stub:
            async def markets(self, gtip, **kwargs):
                seen["gtip"] = gtip
                seen.update(kwargs)
                return comtrade.MarketReport(
                    hs_code="851713", reporter_code=792, flow="X", year=2024, status="ok"
                )

        self.web_app.comtrade_engine = _Stub()
        with self._client() as client:
            response = client.get("/api/foreign/comtrade?gtip=851713000000&year=2024&flow=M&limit=5")
        if response.status_code == 403:
            self.skipTest("Paket kilidi etkin; uç yetkiyle korunuyor.")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "un_comtrade")
        self.assertIn("maliyet", body["statistic_only_note"])
        self.assertEqual(seen["gtip"], "851713000000")
        self.assertEqual(seen["year"], 2024)
        self.assertEqual(seen["flow"], "M")
        self.assertEqual(seen["limit"], 5)

    def test_market_route_rejects_a_bad_year(self) -> None:
        class _Stub:
            async def markets(self, gtip, **kwargs):  # pragma: no cover - çağrılmamalı
                raise AssertionError("Geçersiz yılda motor çağrılmamalı.")

        self.web_app.comtrade_engine = _Stub()
        with self._client() as client:
            response = client.get("/api/foreign/comtrade?gtip=8517&year=abc")
        self.assertIn(response.status_code, (403, 422))

    def test_status_route_reports_the_engine(self) -> None:
        with self._client() as client:
            response = client.get("/api/foreign/comtrade/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("archived_queries", response.json())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
