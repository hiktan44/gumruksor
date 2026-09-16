"""İhracat dosyasında hedef ülke oranı hangi motordan okunuyor?

`destination_profile` bir ülkeyi bir motora bağlar, `CustomsAdvisor._export_requirements`
o motoru çağırır. İkisi arasındaki eşleşme bozulursa ürün sessizce "bu ülke için vergi
verimiz yok" der — oysa veri vardır. ABD motoru eklendiğinde tam olarak bu durumdaydı:
motor kurulmuştu ama ihracat akışı onu hiç çağırmıyordu.

Buradaki değişmez: oran **yalnız** motor kesin eşleşme döndürdüğünde taşınır; ıskada
kademe düşer ve sayı yerine dürüst bir cümle kalır.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import customs_advisor
from customs_advisor import CustomsInquiry


class _FakeForeignTariffEngine:
    """Çağrıyı kaydeder ve istenen sonucu döndürür; ağa çıkmaz."""

    def __init__(self, result: SimpleNamespace | None) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def lookup(self, code, *, origin=None, jurisdiction="all", as_of=None):
        self.calls.append({"code": code, "origin": origin, "jurisdiction": jurisdiction})
        return SimpleNamespace(results=[self.result] if self.result is not None else [])


def _hit(**overrides) -> SimpleNamespace:
    base = {
        "match_quality": "exact_hs6",
        "third_country_duty": "Free",
        "origin_preference": None,
        "matched_code": "8517130000",
        "description": "Smartphones",
        "measures": [],
        "source_url": "https://hts.usitc.gov/reststop/exportList",
        "retrieved_at": "2026-09-16T20:12:08+00:00",
        "sha256": "a" * 64,
        "notes": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _inquiry(destination: str) -> CustomsInquiry:
    return CustomsInquiry(
        question="Bu ürünü ihraç edersem hedef ülkede hangi vergi uygulanır?",
        product_description="Akıllı telefon",
        direction="export",
        destination_country=destination,
        origin_country="Türkiye",
        candidate_gtip="851713000000",
        # GTİP yalnız kullanıcı karar ağacında seçtiyse kabul edilir (model kuralı).
        tariff_selection_confirmed=True,
    )


class UsExportDestinationTests(unittest.TestCase):
    def _run(self, engine: _FakeForeignTariffEngine, destination: str = "Amerika Birleşik Devletleri"):
        service = customs_advisor.CustomsAdvisor()
        service.foreign_tariff_engine = engine
        return asyncio.run(service._export_requirements(_inquiry(destination)))

    def test_a_us_destination_reaches_the_us_jurisdiction_with_tr_origin(self) -> None:
        engine = _FakeForeignTariffEngine(_hit())
        self._run(engine)
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0]["jurisdiction"], "us")
        self.assertEqual(engine.calls[0]["origin"], "TR", "AB'ye olduğu gibi ABD'ye de menşe TR sorulur")

    def test_the_official_rate_reaches_the_declaration(self) -> None:
        result = self._run(_FakeForeignTariffEngine(_hit()))
        self.assertEqual(result.destination.tier, "rates")
        self.assertEqual(result.destination_duty["third_country_duty"], "Free")
        self.assertEqual(result.destination_duty["matched_code"], "8517130000")

    def test_the_source_credentials_travel_with_the_rate(self) -> None:
        # Oranın yanında kaynak künyesi yoksa kullanıcı onu doğrulayamaz.
        result = self._run(_FakeForeignTariffEngine(_hit()))
        self.assertIn("usitc.gov", result.duty_source["url"])
        self.assertEqual(result.duty_source["retrieved_at"], "2026-09-16T20:12:08+00:00")

    def test_a_miss_downgrades_the_tier_instead_of_inventing_a_rate(self) -> None:
        engine = _FakeForeignTariffEngine(_hit(match_quality="not_found", third_country_duty=None))
        result = self._run(engine)
        self.assertEqual(result.destination.tier, "agreement_only")
        self.assertEqual(result.destination.downgraded_from, "rates")
        self.assertIsNone(result.destination_duty)

    def test_an_empty_engine_answer_is_not_treated_as_zero_duty(self) -> None:
        result = self._run(_FakeForeignTariffEngine(None))
        self.assertIsNone(result.destination_duty)
        self.assertEqual(result.destination.tier, "agreement_only")

    def test_an_engine_failure_downgrades_rather_than_breaking_the_file(self) -> None:
        class Broken:
            async def lookup(self, *args, **kwargs):
                raise RuntimeError("USITC yanıt vermedi")

        service = customs_advisor.CustomsAdvisor()
        service.foreign_tariff_engine = Broken()
        result = asyncio.run(service._export_requirements(_inquiry("ABD")))
        self.assertIsNone(result.destination_duty)
        self.assertEqual(result.destination.tier, "agreement_only")

    def test_the_us_rate_never_enters_a_turkish_cost_calculation(self) -> None:
        # Yapısal kural: ihracatta Türkiye maliyet hesabı yoktur, yabancı oran oraya akamaz.
        result = self._run(_FakeForeignTariffEngine(_hit()))
        self.assertIsNone(getattr(result, "deterministic_cost", None))


class OtherJurisdictionsStillRouteCorrectlyTests(unittest.TestCase):
    """Motor adından yargı alanı türetmeye geçildi; eski yollar aynı kalmalı."""

    def _jurisdiction_for(self, destination: str) -> str:
        engine = _FakeForeignTariffEngine(_hit())
        service = customs_advisor.CustomsAdvisor()
        service.foreign_tariff_engine = engine
        asyncio.run(service._export_requirements(_inquiry(destination)))
        return engine.calls[0]["jurisdiction"]

    def test_united_kingdom_still_asks_uk(self) -> None:
        self.assertEqual(self._jurisdiction_for("Birleşik Krallık"), "uk")

    def test_switzerland_still_asks_ch(self) -> None:
        self.assertEqual(self._jurisdiction_for("İsviçre"), "ch")

    def test_a_country_without_an_engine_never_calls_one(self) -> None:
        engine = _FakeForeignTariffEngine(_hit())
        service = customs_advisor.CustomsAdvisor()
        service.foreign_tariff_engine = engine
        result = asyncio.run(service._export_requirements(_inquiry("Çin")))
        self.assertEqual(engine.calls, [])
        self.assertEqual(result.destination.tier, "agreement_only")


class UsSourceHostAllowListTests(unittest.TestCase):
    def test_the_usitc_host_is_allowed_or_its_sources_vanish_silently(self) -> None:
        for host in ("hts.usitc.gov", "usitc.gov", "rulings.cbp.gov"):
            self.assertIn(host, customs_advisor._ALLOWED_SOURCE_HOSTS, host)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
