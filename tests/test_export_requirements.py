"""İhracat hedef ülke kural tablosu: veri düzeyleri, belgeler, ipuçları ve beyanname kapısı.

Bu testlerin asıl işi tek bir değişmezi korumak: **hedef ülke için gerçek veri yoksa
hiçbir oran gösterilmez.** Hedef ülkede açılacak beyanname yanlış doldurulursa ciddi
zarar doğar; uydurulmuş bir oranın sessizce sonuca sızması en ağır hatadır.
"""

from __future__ import annotations

import unittest

from export_requirements import (
    assess_readiness,
    build_export_requirements,
    declaration_fields,
    destination_profile,
    downgrade_profile,
    export_proof_documents,
    market_hints,
    turkish_export_procedure,
)
from countries import find_country


class DestinationTierTests(unittest.TestCase):
    def test_eu_member_uses_the_taric_engine(self) -> None:
        profile = destination_profile("Almanya")
        self.assertEqual(profile.tier, "rates")
        self.assertEqual(profile.engine, "eu_taric")
        self.assertEqual(profile.regime, "eu")
        self.assertTrue(profile.recognised)

    def test_united_kingdom_uses_the_official_api(self) -> None:
        profile = destination_profile("Birleşik Krallık")
        self.assertEqual(profile.tier, "rates")
        self.assertEqual(profile.engine, "foreign_tariff_uk")

    def test_united_states_uses_the_usitc_engine(self) -> None:
        profile = destination_profile("Amerika Birleşik Devletleri")
        self.assertEqual(profile.tier, "rates")
        self.assertEqual(profile.engine, "foreign_tariff_us")
        self.assertIn("USITC", profile.badge_text)

    def test_the_us_badge_states_which_column_turkish_goods_take(self) -> None:
        # Türkiye'nin ABD ile anlaşması yok; kullanıcı hangi sütunun uygulandığını
        # tahmin etmek zorunda kalmamalı.
        badge = destination_profile("ABD").badge_text
        self.assertIn("General", badge)
        self.assertIn("tercihli ticaret anlaşması yoktur", badge)

    def test_switzerland_is_nomenclature_only(self) -> None:
        profile = destination_profile("İsviçre")
        self.assertEqual(profile.tier, "nomenclature")
        self.assertEqual(profile.engine, "foreign_tariff_ch")
        self.assertIn("vergi oranı yayımlamaz", profile.badge_text)

    def test_country_without_rate_data_says_so(self) -> None:
        profile = destination_profile("Çin")
        self.assertEqual(profile.tier, "agreement_only")
        self.assertEqual(profile.engine, "none")
        self.assertIn("vergi oranı verimiz yok", profile.badge_text)

    def test_unknown_country_is_not_silently_accepted(self) -> None:
        profile = destination_profile("Wakanda")
        self.assertEqual(profile.tier, "none")
        self.assertFalse(profile.recognised)
        self.assertIn("kayıtlı ülke listemizde yok", profile.badge_text)

    def test_english_alias_resolves(self) -> None:
        self.assertEqual(destination_profile("germany").country_name, "Almanya")

    def test_downgrade_replaces_the_badge_and_keeps_the_trace(self) -> None:
        profile = destination_profile("Almanya")
        downgraded = downgrade_profile(profile, reason="archive_miss", note="Arşivde yok.")
        self.assertEqual(downgraded.tier, "agreement_only")
        self.assertEqual(downgraded.downgraded_from, "rates")
        self.assertEqual(downgraded.badge_text, "Arşivde yok.")

    def test_downgrade_is_a_no_op_below_rates(self) -> None:
        profile = destination_profile("Çin")
        self.assertIs(downgrade_profile(profile, reason="x", note="y"), profile)


class ProofDocumentTests(unittest.TestCase):
    def test_eu_industrial_goods_move_with_an_atr_issued_by_the_exporter(self) -> None:
        documents, _ = export_proof_documents(find_country("Almanya"), "610910000011")
        self.assertEqual(documents[0].code, "ATR")
        self.assertIn("ihracatçı düzenler", documents[0].applicability)

    def test_eu_agricultural_goods_need_eur1_not_atr(self) -> None:
        documents, caveats = export_proof_documents(find_country("Fransa"), "080810000000")
        self.assertEqual([item.code for item in documents][0], "EUR1")
        self.assertTrue(any("A.TR geçerli değildir" in note for note in caveats))

    def test_ecsc_goods_need_eur1(self) -> None:
        documents, caveats = export_proof_documents(find_country("İtalya"), "720851000000")
        self.assertEqual(documents[0].code, "EUR1")
        self.assertTrue(any("AKÇT" in note for note in caveats))

    def test_united_kingdom_uses_an_invoice_origin_declaration(self) -> None:
        documents, _ = export_proof_documents(find_country("Birleşik Krallık"), "610910000011")
        self.assertEqual(documents[0].code, "ORIGIN_DECLARATION")
        self.assertIn("EUR.1 düzenlenmez", documents[0].note)

    def test_korea_uses_an_origin_declaration(self) -> None:
        documents, _ = export_proof_documents(find_country("Güney Kore"), "610910000011")
        self.assertEqual(documents[0].code, "ORIGIN_DECLARATION")

    def test_uae_uses_the_agreement_specific_certificate(self) -> None:
        documents, _ = export_proof_documents(find_country("Birleşik Arap Emirlikleri"), "610910000011")
        self.assertEqual(documents[0].code, "AGREEMENT_CERT")

    def test_mfn_country_gets_no_preference_and_is_told_so(self) -> None:
        documents, caveats = export_proof_documents(find_country("Çin"), "610910000011")
        self.assertEqual(documents[0].code, "CERT_ORIGIN")
        self.assertTrue(any("tercihli ticaret anlaşması yoktur" in note for note in caveats))

    def test_unknown_destination_yields_no_proof_documents(self) -> None:
        documents, caveats = export_proof_documents(None, "610910000011")
        self.assertEqual(documents, [])
        self.assertEqual(caveats, [])

    def test_missing_gtip_is_flagged_rather_than_assumed_silently(self) -> None:
        _, caveats = export_proof_documents(find_country("Almanya"), None)
        self.assertTrue(any("sanayi ürünü varsayıldı" in note for note in caveats))


class MarketHintTests(unittest.TestCase):
    def test_turkish_diacritics_do_not_defeat_matching(self) -> None:
        profile = destination_profile("Almanya")
        hints = market_hints({"target_user": "kız çocuk 8-12 yaş"}, profile)
        self.assertEqual([hint.id for hint in hints], ["toy_safety"])
        self.assertIn("2009/48/AT", hints[0].detail)
        self.assertEqual(hints[0].trigger_field, "target_user")

    def test_united_kingdom_hint_names_ukca_not_ce(self) -> None:
        hints = market_hints({"target_user": "çocuk"}, destination_profile("Birleşik Krallık"))
        self.assertIn("UKCA", hints[0].detail)

    def test_country_without_a_conformity_regime_gets_a_generic_hint(self) -> None:
        hints = market_hints({"target_user": "çocuk"}, destination_profile("Çin"))
        self.assertNotIn("CE", hints[0].detail)
        self.assertIn("hedef ülkenin", hints[0].detail)

    def test_wooden_packaging_triggers_ispm15(self) -> None:
        hints = market_hints({"packaging": "ahşap palet üzerinde"}, destination_profile("Çin"))
        self.assertIn("ispm15", [hint.id for hint in hints])

    def test_no_confirmed_attributes_means_no_hints(self) -> None:
        self.assertEqual(market_hints({}, destination_profile("Almanya")), [])

    def test_hint_records_which_confirmed_attribute_produced_it(self) -> None:
        hints = market_hints({"composition": "%100 pamuk"}, destination_profile("Almanya"))
        self.assertEqual(hints[0].trigger_field, "composition")
        self.assertEqual(hints[0].trigger_value, "%100 pamuk")


class DeclarationFieldTests(unittest.TestCase):
    """Beyanname alanlarının emin olma düzeyi — bu paketin en kritik değişmezi."""

    def _inquiry(self) -> dict:
        return {
            "destination_country": "Almanya",
            "candidate_gtip": "610910000011",
            "origin_country": "Türkiye",
            "incoterm": "FOB",
            "invoice_value": 12000,
            "currency": "EUR",
        }

    def test_without_duty_data_no_rate_field_carries_a_value(self) -> None:
        profile = destination_profile("Almanya")
        fields = declaration_fields(self._inquiry(), profile)
        rates = {item.key: item for item in fields if item.key in {"third_country_duty", "preferential_duty"}}
        for field in rates.values():
            self.assertEqual(field.certainty, "unavailable")
            self.assertIsNone(field.value)

    def test_duty_data_is_marked_verified_with_its_source(self) -> None:
        from datetime import date

        profile = destination_profile("Almanya")
        fields = declaration_fields(
            self._inquiry(),
            profile,
            destination_duty={"mfn_rate": "12.00 %", "cn_code": "61091000"},
            duty_source={"url": "https://example.test/taric", "retrieved_at": date.today().isoformat()},
        )
        duty = next(item for item in fields if item.key == "third_country_duty")
        self.assertEqual(duty.certainty, "verified")
        self.assertEqual(duty.value, "12.00 %")
        self.assertEqual(duty.source_url, "https://example.test/taric")

    def test_stale_source_is_downgraded_to_check_required(self) -> None:
        profile = destination_profile("Almanya")
        fields = declaration_fields(
            self._inquiry(),
            profile,
            destination_duty={"mfn_rate": "12.00 %"},
            duty_source={"retrieved_at": "2020-01-01"},
        )
        duty = next(item for item in fields if item.key == "third_country_duty")
        self.assertEqual(duty.certainty, "check_required")
        self.assertIn("gün önce alınmış", duty.note)

    def test_destination_vat_is_never_guessed(self) -> None:
        fields = declaration_fields(self._inquiry(), destination_profile("Almanya"))
        vat = next(item for item in fields if item.key == "destination_vat")
        self.assertEqual(vat.certainty, "unavailable")
        self.assertIsNone(vat.value)
        self.assertIn("veri kaynaklarımızda yok", vat.note)

    def test_importer_identity_is_never_invented(self) -> None:
        fields = declaration_fields(self._inquiry(), destination_profile("Almanya"))
        importer = next(item for item in fields if item.key == "importer_identity")
        self.assertEqual(importer.certainty, "unavailable")
        self.assertIsNone(importer.value)

    def test_unavailable_fields_never_carry_a_value(self) -> None:
        fields = declaration_fields(self._inquiry(), destination_profile("Çin"))
        for field in fields:
            if field.certainty == "unavailable":
                self.assertIsNone(field.value, f"{field.key} değer taşıyor")


class ReadinessTests(unittest.TestCase):
    def test_a_country_without_rate_data_blocks_declaration_use(self) -> None:
        profile = destination_profile("Çin")
        fields = declaration_fields({"destination_country": "Çin"}, profile)
        readiness = assess_readiness(fields, profile)
        self.assertEqual(readiness.status, "blocked")
        self.assertIn("beyanname doldurulmamalı", readiness.summary)
        self.assertTrue(readiness.blocking)

    def test_partial_data_asks_for_checks_rather_than_claiming_ready(self) -> None:
        profile = destination_profile("Almanya")
        fields = declaration_fields({"destination_country": "Almanya"}, profile)
        readiness = assess_readiness(fields, profile)
        self.assertEqual(readiness.status, "needs_check")
        self.assertIn("tek başına beyanname yerine geçmez", readiness.summary)

    def test_ready_needs_official_fields_verified_and_user_fields_filled(self) -> None:
        # Kapı iki ayrı ölçüt uygular: resmî veriden gelmesi gereken alan `verified`
        # olmalı, yalnız beyan sahibinin bilebileceği alan ise dolu olmalıdır.
        profile = destination_profile("Almanya")
        fields = declaration_fields({"destination_country": "Almanya"}, profile)
        for field in fields:
            if not field.mandatory:
                continue
            if field.user_supplied:
                field.value = "beyan sahibi tarafından dolduruldu"
            else:
                field.certainty = "verified"
        readiness = assess_readiness(fields, profile)
        self.assertEqual(readiness.status, "ready")
        self.assertEqual(readiness.blocking, [])

    def test_an_empty_user_field_still_blocks_even_when_marked_verified(self) -> None:
        # Gerileme kilidi: kullanıcı alanına "verified" yazmak onu doldurmuş saymaz.
        profile = destination_profile("Almanya")
        fields = declaration_fields({"destination_country": "Almanya"}, profile)
        for field in fields:
            if field.mandatory:
                field.certainty = "verified"
                if field.user_supplied:
                    field.value = None
        readiness = assess_readiness(fields, profile)
        self.assertNotEqual(readiness.status, "ready")
        self.assertIn("Eşya tanımı (ticari)", readiness.blocking)

    def test_importer_identity_is_fillable_by_the_user(self) -> None:
        # Yapısal çıkmaz düzeltmesi: alan artık kalıcı olarak `unavailable` değil.
        profile = destination_profile("Almanya")
        fields = declaration_fields(
            {"destination_country": "Almanya", "consignee_tax_id": "DE123456789"}, profile
        )
        importer = next(item for item in fields if item.key == "importer_identity")
        self.assertEqual(importer.certainty, "check_required")
        self.assertEqual(importer.value, "DE123456789")
        self.assertTrue(importer.user_supplied)

    def test_fields_that_can_never_be_verified_are_not_mandatory(self) -> None:
        # Hedef ülke KDV'si tasarım gereği asla `verified` olamaz; zorunlu sayılsaydı
        # hiçbir dosya asla 'hazır' olamaz ve kapı anlamsız kalırdı.
        fields = declaration_fields({"destination_country": "Almanya"}, destination_profile("Almanya"))
        vat = next(item for item in fields if item.key == "destination_vat")
        self.assertFalse(vat.mandatory)


class BuildTests(unittest.TestCase):
    def test_duty_cannot_survive_a_non_rate_tier(self) -> None:
        # Yapısal güvence: çağıran yanlışlıkla oran enjekte etse bile düşük kademede taşınmaz.
        result = build_export_requirements(
            {"destination_country": "Çin", "candidate_gtip": "610910000011"},
            destination_duty={"mfn_rate": "12 %"},
            duty_source={"url": "https://example.test"},
        )
        self.assertIsNone(result.destination_duty)
        self.assertIsNone(result.duty_source)

    def test_turkish_procedure_is_always_present(self) -> None:
        result = build_export_requirements({"destination_country": "Wakanda"})
        codes = [item.code for item in result.turkish_procedure]
        self.assertIn("EXPORT_DECLARATION", codes)
        self.assertIn("VAT_EXEMPTION", codes)
        self.assertIn("EXPORT_PROHIBITION", codes)

    def test_unknown_country_still_gets_commercial_documents_and_no_proof(self) -> None:
        result = build_export_requirements({"destination_country": "Wakanda"})
        self.assertEqual(result.proof_documents, [])
        self.assertTrue(result.commercial_documents)
        self.assertEqual(result.readiness.status, "blocked")

    def test_caveats_repeat_the_tier_sentence_when_there_is_no_rate_data(self) -> None:
        result = build_export_requirements({"destination_country": "Çin"})
        self.assertTrue(any("vergi oranı verimiz yok" in note for note in result.caveats))

    def test_destination_market_is_carried_but_never_touches_fields_or_readiness(self) -> None:
        """İstatistik bloğu bilgi amaçlıdır: beyanname alanları ve hazırlık kapısı değişmez."""
        base = build_export_requirements({"destination_country": "Almanya", "candidate_gtip": "610910000011"})
        market = {
            "reporter": "DE", "reporter_name": "Almanya", "product": "61091000", "match_level": "cn8",
            "year": 2024, "total_value": 1_000_000.0,
            "focus": {"present": True, "value_eur": 50_000.0, "share": 0.05, "rank": 4},
            "top_partners": [{"partner": "Bangladeş", "partner_code": "BD", "value_eur": 400_000.0, "share": 0.4, "rank": 1}],
        }
        with_market = build_export_requirements(
            {"destination_country": "Almanya", "candidate_gtip": "610910000011"}, destination_market=market
        )
        self.assertEqual(with_market.destination_market.reporter, "DE")
        self.assertEqual(with_market.destination_market.focus["rank"], 4)
        self.assertEqual(
            [f.model_dump() for f in with_market.declaration_fields],
            [f.model_dump() for f in base.declaration_fields],
        )
        self.assertEqual(with_market.readiness, base.readiness)
        self.assertEqual(with_market.cost, base.cost)

    def test_malformed_market_block_is_dropped_not_fatal(self) -> None:
        result = build_export_requirements(
            {"destination_country": "Almanya"}, destination_market={"reporter": None, "top_partners": "x"}
        )
        self.assertIsNone(result.destination_market)

    def test_procedure_list_is_stable(self) -> None:
        self.assertEqual(len(turkish_export_procedure({})), 8)


if __name__ == "__main__":
    unittest.main()


class CountriesRouteTierTests(unittest.TestCase):
    """Arayüz kota harcamadan uyarabilsin diye kademe ülke listesinde geliyor."""

    def test_route_exposes_the_export_tier_for_every_country(self) -> None:
        from starlette.testclient import TestClient
        import app as web_app

        with TestClient(web_app.app, base_url="https://gumruksor.com") as client:
            body = client.get("/api/tariff/countries").json()
        items = {item["name"]: item for item in body["items"]}
        self.assertTrue(all("export_data_tier" in item for item in body["items"]))
        self.assertEqual(items["Almanya"]["export_data_tier"], "rates")
        self.assertEqual(items["İsviçre"]["export_data_tier"], "nomenclature")
        self.assertEqual(items["Çin"]["export_data_tier"], "agreement_only")
        self.assertIn("vergi oranı verimiz yok", items["Çin"]["export_data_note"])
