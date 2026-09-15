"""Beyanname taslağı (Tek İdari Belge kutuları) — saf kural testleri.

En kritik değişmez: **hiçbir kutu uydurulmaz.** Değeri olmayan kutu `unavailable` olur
ve değer taşımaz; vergi tutarları hiçbir koşulda `verified` sayılmaz.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from declaration_draft import (  # noqa: E402
    DRAFT_VERSION,
    EXPORT_REGIME_CODE,
    IMPORT_REGIME_CODE,
    assess_draft_readiness,
    build_declaration_draft,
    draft_to_csv,
    draft_to_xml,
)


def _matched_lookup(gtip: str = "610910000011") -> dict:
    return {
        "status": "matched",
        "gtip": gtip,
        "match_mode": "exact",
        "measures": [{"description": "Tişört, pamuklu, örme"}],
        "snapshots": [
            {
                "active": True,
                "landing_url": "https://ticaret.gov.tr/ithalat-rejimi",
                "retrieved_at": "2026-01-02T00:00:00+00:00",
                "archive_sha256": "a" * 64,
            }
        ],
    }


def _full_inquiry(**overrides) -> dict:
    data = {
        "candidate_gtip": "610910000011",
        "exact_gtip_confirmed": True,
        "declared_product_type": "Pamuklu örme tişört",
        "origin_country": "Türkiye",
        "destination_country": "Almanya",
        "declarant_tax_id": "1234567890",
        "customs_office_code": "060500",
        "consignor_name": "Örnek Tekstil A.Ş.",
        "consignee_name": "Beispiel GmbH",
        "consignee_tax_id": "DE123456789",
        "incoterm": "FOB",
        "transport_mode": "Karayolu",
        "package_count": 40,
        "gross_weight_kg": 520.0,
        "net_weight_kg": 480.0,
        "invoice_number": "IHR-2026-001",
        "invoice_value": 12500.0,
        "currency": "EUR",
    }
    data.update(overrides)
    return data


def _fields(draft) -> dict:
    return {field.key: field for section in draft.sections for field in section.fields}


class StructureTests(unittest.TestCase):
    def test_import_is_the_default_direction(self) -> None:
        draft = build_declaration_draft({"inquiry": {}})
        self.assertEqual(draft.direction, "import")
        self.assertEqual(draft.regime_code, IMPORT_REGIME_CODE)
        self.assertEqual(draft.version, DRAFT_VERSION)

    def test_export_uses_the_export_regime_code(self) -> None:
        draft = build_declaration_draft({"direction": "export", "inquiry": {"destination_country": "Almanya"}})
        self.assertEqual(draft.direction, "export")
        self.assertEqual(draft.regime_code, EXPORT_REGIME_CODE)

    def test_every_section_is_present_in_both_directions(self) -> None:
        for direction in ("import", "export"):
            draft = build_declaration_draft({"direction": direction, "inquiry": {"destination_country": "Almanya"}})
            self.assertEqual(
                [section.id for section in draft.sections],
                ["declaration", "shipment", "goods", "value", "taxes", "documents"],
                direction,
            )

    def test_a_precheck_result_model_can_be_passed_directly(self) -> None:
        # Rota modeli doğruladıktan sonra nesneyi verir; dict yolu ile aynı sonucu vermeli.
        class _Fake:
            direction = "export"
            inquiry = _full_inquiry()
            tariff_lookup = _matched_lookup()
            deterministic_cost = None
            origin_documents = None
            control_lookup = None
            export_requirements = None

        draft = build_declaration_draft(_Fake())
        self.assertEqual(_fields(draft)["commodity_code"].value, "610910000011")


class HonestyTests(unittest.TestCase):
    def test_unavailable_boxes_never_carry_a_value(self) -> None:
        draft = build_declaration_draft({"direction": "export", "inquiry": {"destination_country": "Çin"}})
        for section in draft.sections:
            for field in section.fields:
                if field.certainty == "unavailable":
                    self.assertIsNone(field.value, f"{field.key} değer taşıyor")

    def test_an_empty_box_is_never_marked_check_required(self) -> None:
        draft = build_declaration_draft({"inquiry": {}})
        for section in draft.sections:
            for field in section.fields:
                if not field.value:
                    self.assertEqual(field.certainty, "unavailable", field.key)

    def test_consignee_tax_id_is_read_from_the_user_and_never_invented(self) -> None:
        empty = _fields(build_declaration_draft({"inquiry": {}}))["consignee_tax_id"]
        self.assertEqual(empty.certainty, "unavailable")
        self.assertIsNone(empty.value)
        filled = _fields(build_declaration_draft({"inquiry": {"consignee_tax_id": "DE123456789"}}))["consignee_tax_id"]
        self.assertEqual(filled.certainty, "check_required")
        self.assertEqual(filled.value, "DE123456789")

    def test_tax_amounts_are_never_verified(self) -> None:
        draft = build_declaration_draft(
            {
                "inquiry": _full_inquiry(destination_country=None),
                "tariff_lookup": _matched_lookup(),
                "deterministic_cost": {
                    "status": "complete",
                    "currency": "USD",
                    "total_taxes": 3100.0,
                    "lines": [
                        {"code": "customs_duty", "label": "Gümrük vergisi", "rate": 12.0, "amount": 1500.0},
                        {"code": "vat", "label": "KDV", "rate": 20.0, "amount": 1600.0},
                    ],
                },
            }
        )
        taxes = [field for section in draft.sections if section.id == "taxes" for field in section.fields]
        self.assertTrue(taxes)
        for field in taxes:
            self.assertNotEqual(field.certainty, "verified", field.key)
        self.assertTrue(any("tescil tarihindeki" in field.note for field in taxes))

    def test_export_never_carries_turkish_import_taxes(self) -> None:
        draft = build_declaration_draft(
            {
                "direction": "export",
                "inquiry": _full_inquiry(),
                "deterministic_cost": {
                    "status": "complete",
                    "currency": "USD",
                    "lines": [{"code": "vat", "label": "KDV", "rate": 20.0, "amount": 1600.0}],
                },
            }
        )
        keys = _fields(draft)
        self.assertNotIn("tax_vat", keys)
        self.assertNotIn("tax_total", keys)
        self.assertEqual(keys["export_no_import_tax"].certainty, "unavailable")


class CommodityCodeTests(unittest.TestCase):
    def test_a_confirmed_code_matched_in_the_snapshot_is_verified(self) -> None:
        field = _fields(
            build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": _matched_lookup()})
        )["commodity_code"]
        self.assertEqual(field.certainty, "verified")
        self.assertEqual(field.source_sha256, "a" * 64)
        self.assertEqual(field.source_url, "https://ticaret.gov.tr/ithalat-rejimi")

    def test_an_unconfirmed_code_is_only_check_required(self) -> None:
        field = _fields(
            build_declaration_draft(
                {"inquiry": _full_inquiry(exact_gtip_confirmed=False), "tariff_lookup": _matched_lookup()}
            )
        )["commodity_code"]
        self.assertEqual(field.certainty, "check_required")
        self.assertIsNone(field.source_sha256)

    def test_a_prefix_match_is_not_good_enough(self) -> None:
        lookup = _matched_lookup()
        lookup["match_mode"] = "prefix"
        field = _fields(build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": lookup}))["commodity_code"]
        self.assertEqual(field.certainty, "check_required")

    def test_a_code_the_snapshot_did_not_return_is_not_verified(self) -> None:
        lookup = _matched_lookup(gtip="610910000099")
        field = _fields(build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": lookup}))["commodity_code"]
        self.assertEqual(field.certainty, "check_required")


class ReadinessTests(unittest.TestCase):
    def test_an_empty_file_is_blocked(self) -> None:
        draft = build_declaration_draft({"inquiry": {}})
        self.assertEqual(draft.readiness.status, "blocked")
        self.assertTrue(draft.readiness.blocking)

    def test_missing_user_boxes_ask_for_completion_rather_than_blocking(self) -> None:
        inquiry = _full_inquiry()
        inquiry.pop("invoice_number")
        draft = build_declaration_draft({"inquiry": inquiry, "tariff_lookup": _matched_lookup()})
        self.assertEqual(draft.readiness.status, "needs_check")
        self.assertTrue(any("Fatura numarası" in item for item in draft.readiness.blocking))

    def test_an_unverified_commodity_code_blocks_even_when_everything_else_is_filled(self) -> None:
        draft = build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": None})
        self.assertEqual(draft.readiness.status, "blocked")
        self.assertTrue(any("GTİP" in item for item in draft.readiness.blocking))

    def test_a_complete_export_file_reaches_ready(self) -> None:
        draft = build_declaration_draft(
            {"direction": "export", "inquiry": _full_inquiry(), "tariff_lookup": _matched_lookup()}
        )
        self.assertEqual(draft.readiness.status, "ready", draft.readiness.blocking)
        self.assertEqual(draft.readiness.blocking, [])

    def test_readiness_counts_match_the_fields(self) -> None:
        draft = build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": _matched_lookup()})
        fields = [field for section in draft.sections for field in section.fields]
        total = draft.readiness.verified + draft.readiness.check_required + draft.readiness.unavailable
        self.assertEqual(total, len(fields))

    def test_assess_is_a_pure_function_of_the_fields(self) -> None:
        draft = build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": _matched_lookup()})
        fields = [field for section in draft.sections for field in section.fields]
        self.assertEqual(assess_draft_readiness(fields).status, draft.readiness.status)


class DocumentTests(unittest.TestCase):
    def test_export_lists_the_proof_document_from_export_requirements(self) -> None:
        draft = build_declaration_draft(
            {
                "direction": "export",
                "inquiry": _full_inquiry(),
                "export_requirements": {
                    "destination": {"badge_text": "Hedef ülke vergi verisi: var"},
                    "proof_documents": [{"name": "A.TR Dolaşım Belgesi"}],
                },
            }
        )
        field = _fields(draft)["origin_proof"]
        self.assertEqual(field.value, "A.TR Dolaşım Belgesi")
        self.assertFalse(field.mandatory)

    def test_export_control_lists_are_declared_missing_not_absent(self) -> None:
        draft = build_declaration_draft({"direction": "export", "inquiry": _full_inquiry()})
        field = _fields(draft)["export_control_documents"]
        self.assertEqual(field.certainty, "unavailable")
        self.assertIn("indekslenmemiştir", field.note)

    def test_import_lists_origin_and_control_documents(self) -> None:
        draft = build_declaration_draft(
            {
                "inquiry": _full_inquiry(destination_country=None),
                "origin_documents": {"documents": [{"name": "A.TR Dolaşım Belgesi"}]},
                "control_lookup": {"matches": [{"rule": {"title": "Oyuncak Denetimi Tebliği"}}]},
            }
        )
        keys = _fields(draft)
        self.assertEqual(keys["origin_proof"].value, "A.TR Dolaşım Belgesi")
        self.assertEqual(keys["control_documents"].value, "Oyuncak Denetimi Tebliği")

    def test_no_control_match_does_not_claim_out_of_scope(self) -> None:
        draft = build_declaration_draft({"inquiry": _full_inquiry(destination_country=None)})
        note = _fields(draft)["control_documents"].note
        self.assertIn("kapsam dışı olduğu anlamına gelmez", note)


class ExportFormatTests(unittest.TestCase):
    def test_csv_has_one_row_per_box_plus_a_header(self) -> None:
        draft = build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": _matched_lookup()})
        rows = draft_to_csv(draft).strip().split("\r\n")
        fields = [field for section in draft.sections for field in section.fields]
        self.assertEqual(len(rows), len(fields) + 1)
        self.assertTrue(rows[0].startswith("bolum;kutu;alan_kodu"))

    def test_xml_is_well_formed_and_carries_the_legal_notice(self) -> None:
        from xml.etree import ElementTree as ET

        draft = build_declaration_draft({"inquiry": _full_inquiry(), "tariff_lookup": _matched_lookup()})
        root = ET.fromstring(draft_to_xml(draft))
        self.assertEqual(root.tag, "beyanname-taslagi")
        self.assertIn("beyanname yerine geçmez", root.findtext("yasal-uyari") or "")
        self.assertEqual(len(root.findall("bolum")), len(draft.sections))

    def test_xml_escapes_hostile_text(self) -> None:
        from xml.etree import ElementTree as ET

        draft = build_declaration_draft(
            {"inquiry": _full_inquiry(declared_product_type="<script>alert(1)</script> & tişört")}
        )
        document = draft_to_xml(draft)
        self.assertNotIn("<script>", document)
        ET.fromstring(document)  # ayrıştırılabilir olmalı


class CaveatTests(unittest.TestCase):
    def test_the_legal_notice_says_it_is_not_a_declaration(self) -> None:
        draft = build_declaration_draft({"inquiry": {}})
        self.assertIn("beyanname yerine geçmez", draft.legal_notice)
        self.assertIn("beyan sahibinin sorumluluğundadır", draft.legal_notice)

    def test_export_caveats_admit_the_missing_control_index(self) -> None:
        draft = build_declaration_draft({"direction": "export", "inquiry": _full_inquiry()})
        self.assertTrue(any("ihracat kontrol listeleri" in note for note in draft.caveats))

    def test_single_item_assumption_is_stated(self) -> None:
        draft = build_declaration_draft({"inquiry": {}})
        self.assertTrue(any("tek kalem" in note for note in draft.caveats))


if __name__ == "__main__":
    unittest.main()
