"""İhracatta hedef ülke maliyeti (FAZ 8.4).

İhracatçı bir sayı görürse ona göre fiyat verir. Bu yüzden testler "hesap doğru mu"
kadar **"ne zaman hesap yapılmamalı"** sorusunu da kilitler:

* oran verisi olmayan ülkede sayı üretilmez;
* bileşik/spesifik oran ifadesi yüzde sanılıp çarpılmaz (gerçek yükü düşük gösterirdi);
* tercihli oran, ispat onaylanmadan uygulanmaz;
* hedef ülke KDV'si — bir fasıl kuralı tahmini — toplama girmez.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from export_costing import build_export_cost, parse_ad_valorem  # noqa: E402
from export_requirements import destination_profile  # noqa: E402

GERMANY = destination_profile("Almanya")
CHINA = destination_profile("Çin")

INQUIRY = {"invoice_value": 10_000.0, "freight": 800.0, "insurance": 200.0, "currency": "EUR"}
DUTY = {"third_country_duty": "3.7 %", "matched_code": "61091000"}
SOURCE = {"url": "https://ec.europa.eu/taric", "retrieved_at": "2026-09-16", "sha256": "a" * 64}


class RateParsingTests(unittest.TestCase):
    def test_a_plain_percentage_is_parsed(self) -> None:
        self.assertEqual(parse_ad_valorem("3.7 %"), 3.7)
        self.assertEqual(parse_ad_valorem("12%"), 12.0)
        self.assertEqual(parse_ad_valorem("6,5 %"), 6.5)

    def test_duty_free_is_zero_not_unknown(self) -> None:
        for text in ("Free", "free", "0", "0 %", "Muaf"):
            self.assertEqual(parse_ad_valorem(text), 0.0, text)

    def test_a_compound_rate_is_refused(self) -> None:
        """Asıl korunan şey: 10.2 sayısını alıp çarpmak yükü olduğundan DÜŞÜK gösterirdi."""
        for text in ("10.2 % MIN 1.6 EUR/kg", "8 % + 2 EUR/100 kg", "12 % MAX 17 EUR/hl"):
            self.assertIsNone(parse_ad_valorem(text), text)

    def test_a_specific_rate_is_refused(self) -> None:
        for text in ("1.6 EUR/kg", "27.5 EUR/100 kg/net", "5 EUR/adet"):
            self.assertIsNone(parse_ad_valorem(text), text)

    def test_empty_and_garbage_are_refused(self) -> None:
        for text in (None, "", "   ", "bilinmiyor", "%"):
            self.assertIsNone(parse_ad_valorem(text), repr(text))


class GateTests(unittest.TestCase):
    """Hesap yapılmaması gereken durumlar."""

    def test_a_country_without_rate_data_gets_no_number(self) -> None:
        result = build_export_cost(INQUIRY, CHINA, destination_duty={"third_country_duty": "5 %"})
        self.assertEqual(result.status, "rates_unavailable")
        self.assertIsNone(result.total_duties)
        self.assertIn("oran verimiz yok", result.reason)
        # Kullanıcı hangi ülkeden bahsedildiğini görmeli; "hedef ülke" demek yetmez.
        self.assertIn("Çin", result.reason)

    def test_a_rates_country_without_a_snapshot_gets_no_number(self) -> None:
        # AB arşiv ıskası: kademe "rates" olsa da elimizde satır yoksa hesap yok.
        result = build_export_cost(INQUIRY, GERMANY, destination_duty=None)
        self.assertEqual(result.status, "rates_unavailable")
        self.assertIsNone(result.total_duties)

    def test_no_invoice_value_means_no_calculation(self) -> None:
        result = build_export_cost({"currency": "EUR"}, GERMANY, destination_duty=DUTY)
        self.assertEqual(result.status, "input_missing")
        self.assertIsNone(result.customs_value)

    def test_a_compound_rate_stops_the_calculation_and_says_why(self) -> None:
        result = build_export_cost(
            INQUIRY, GERMANY, destination_duty={"third_country_duty": "10.2 % MIN 1.6 EUR/kg"}
        )
        self.assertEqual(result.status, "rate_not_calculable")
        self.assertIsNone(result.total_duties)
        self.assertIn("olduğundan düşük", result.reason)
        # Kıymet yine de hesaplanır; kullanıcı matrahı bilir, oranı kendisi uygular.
        self.assertEqual(result.customs_value, 11_000.0)

    def test_a_missing_rate_expression_stops_the_calculation(self) -> None:
        result = build_export_cost(INQUIRY, GERMANY, destination_duty={"matched_code": "61091000"})
        self.assertEqual(result.status, "rate_not_calculable")


class CalculationTests(unittest.TestCase):
    def _result(self, **kwargs):
        return build_export_cost(INQUIRY, GERMANY, destination_duty=DUTY, duty_source=SOURCE, **kwargs)

    def test_the_customs_value_is_cif(self) -> None:
        self.assertEqual(self._result().customs_value, 11_000.0)

    def test_the_duty_is_the_rate_on_the_customs_value(self) -> None:
        result = self._result()
        self.assertEqual(result.status, "calculated")
        self.assertEqual(result.total_duties, 407.0)  # 11.000 × %3,7
        self.assertEqual(result.landed_before_vat, 11_407.0)

    def test_the_duty_line_carries_its_source(self) -> None:
        duty_line = next(line for line in self._result().lines if line.key == "customs_duty")
        self.assertEqual(duty_line.source_url, SOURCE["url"])
        self.assertEqual(duty_line.basis, "3.7 %")

    def test_a_shipment_without_freight_is_warned_about_the_cif_basis(self) -> None:
        result = build_export_cost(
            {"invoice_value": 10_000.0, "currency": "EUR"}, GERMANY, destination_duty=DUTY
        )
        self.assertEqual(result.customs_value, 10_000.0)
        self.assertTrue(any("CIF" in warning for warning in result.warnings))

    def test_a_duty_free_line_calculates_to_zero_rather_than_failing(self) -> None:
        result = build_export_cost(INQUIRY, GERMANY, destination_duty={"third_country_duty": "Free"})
        self.assertEqual(result.status, "calculated")
        self.assertEqual(result.total_duties, 0.0)


class PreferenceTests(unittest.TestCase):
    DUTY_WITH_PREFERENCE = {"third_country_duty": "12 %", "origin_preference": "0 %"}

    def test_without_confirmed_proof_the_pessimistic_rate_is_used(self) -> None:
        """İthalat tarafındaki 'tevsik yoksa Diğer Ülkeler oranı' kuralının aynası."""
        result = build_export_cost(INQUIRY, GERMANY, destination_duty=self.DUTY_WITH_PREFERENCE)
        self.assertEqual(result.duty_basis, "third_country")
        self.assertEqual(result.total_duties, 1_320.0)  # 11.000 × %12
        self.assertTrue(any("onaylamadınız" in warning for warning in result.warnings))

    def test_with_confirmed_proof_the_preferential_rate_is_used(self) -> None:
        result = build_export_cost(
            INQUIRY, GERMANY, destination_duty=self.DUTY_WITH_PREFERENCE, preference_proof_confirmed=True
        )
        self.assertEqual(result.duty_basis, "preferential")
        self.assertEqual(result.total_duties, 0.0)

    def test_confirming_proof_does_nothing_when_no_preference_exists(self) -> None:
        result = build_export_cost(
            INQUIRY, GERMANY, destination_duty=DUTY, preference_proof_confirmed=True
        )
        self.assertEqual(result.duty_basis, "third_country")


class VatTests(unittest.TestCase):
    VAT = {"applicable": 19.0, "applicable_basis": "standard"}

    def _result(self, vat=None):
        return build_export_cost(
            INQUIRY, GERMANY, destination_duty=DUTY, destination_vat=vat if vat is not None else self.VAT
        )

    def test_vat_is_shown_but_never_added_to_the_total(self) -> None:
        """Bu paketin en kritik değişmezi: tahmin, toplamın içine gizlenmez."""
        result = self._result()
        vat_line = next(line for line in result.lines if line.key == "destination_vat")
        self.assertFalse(vat_line.included_in_total)
        self.assertEqual(vat_line.amount, 2_167.33)  # 11.407 × %19
        # Toplam KDV'siz kalır.
        self.assertEqual(result.total_duties, 407.0)
        self.assertEqual(result.landed_before_vat, 11_407.0)

    def test_the_total_never_equals_a_vat_inclusive_figure(self) -> None:
        result = self._result()
        vat_line = next(line for line in result.lines if line.key == "destination_vat")
        self.assertNotEqual(result.landed_before_vat, result.landed_before_vat + (vat_line.amount or 0))

    def test_a_chapter_rule_vat_says_it_is_a_suggestion(self) -> None:
        result = self._result({"applicable": 7.0, "applicable_basis": "chapter_rule"})
        vat_line = next(line for line in result.lines if line.key == "destination_vat")
        self.assertIn("öneridir", vat_line.note)

    def test_vat_carries_the_recoverability_note(self) -> None:
        vat_line = next(line for line in self._result().lines if line.key == "destination_vat")
        self.assertIn("iade edilebilir", vat_line.note)

    def test_without_vat_data_no_vat_line_appears(self) -> None:
        result = build_export_cost(INQUIRY, GERMANY, destination_duty=DUTY)
        self.assertFalse([line for line in result.lines if line.key == "destination_vat"])


class AdditionalMeasureTests(unittest.TestCase):
    def test_a_calculable_additional_measure_joins_the_total(self) -> None:
        duty = dict(DUTY, additional_duties=[{"label": "Damping", "rate": "8 %"}])
        result = build_export_cost(INQUIRY, GERMANY, destination_duty=duty)
        self.assertEqual(result.total_duties, 407.0 + 880.0)

    def test_an_uncalculable_additional_measure_is_listed_but_not_counted(self) -> None:
        duty = dict(DUTY, additional_duties=[{"label": "Damping", "rate": "62.6 EUR/ton"}])
        result = build_export_cost(INQUIRY, GERMANY, destination_duty=duty)
        self.assertEqual(result.total_duties, 407.0)
        line = next(line for line in result.lines if line.label == "Damping")
        self.assertFalse(line.included_in_total)
        self.assertTrue(any("dahil edilmedi" in warning for warning in result.warnings))


class LegalNoteTests(unittest.TestCase):
    def test_every_result_carries_the_legal_note(self) -> None:
        for result in (
            build_export_cost(INQUIRY, GERMANY, destination_duty=DUTY),
            build_export_cost(INQUIRY, CHINA),
            build_export_cost({}, GERMANY, destination_duty=DUTY),
        ):
            self.assertIn("bağlayıcı", result.legal_note)


if __name__ == "__main__":
    unittest.main()
