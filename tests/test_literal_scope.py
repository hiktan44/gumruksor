"""Metin içinde tek tek sayılan GTİP'ler ve yön duyarlı değerlendirme cümlesi.

Bu paketin iki ayrı kusuru var; ikisi de canlı sistemde ölçülerek bulundu:

1. Doğal Çiçek Soğanları tebliğinin **eki yok**: Bedesten `ekler` alanını boş
   döndürüyor ve konsolide HTML yalnız göreli bir bağlantı taşıyor. Üstelik Ek-1
   botanik familya/cins/tür bazlıdır, GTİP içermez — indirilse bile GTİP
   sorgusuna cevap üretmez. Buna karşılık tebliğ metninin kendisi üç GTİP'i
   açıkça sayıyor. `scope_literal` bu kodları yapılandırmadan alır ve **resmî
   metne karşı doğrular**.
2. İhracat sonucunda "ithali yasak … ithalat izni verilmez" yazıyordu. Aynı
   liste türü iki yönde farklı bir hukuki sonuç doğurur; cümle yönden türetilir.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from control_engine import (
    ImportControlEngine,
    extract_literal_scope,
    literal_scope_plan,
    rule_direction,
)

# Resmî konsolide metinden (mevzuatId 350040) alınmış, kısaltılmış gerçek parça.
BULB_TEXT = """
DOĞAL ÇİÇEK SOĞANLARININ 2026 YILI İHRACAT LİSTESİ HAKKINDA TEBLİĞ
(TEBLİĞ NO: 2025/32)

MADDE 4- (1) ... türleri Ek-1'de yer alan tabloda gösterilmiştir.
a) Tablonun (I) numaralı sütununda yer alan türlerin ihracatı yapılamaz.
19/9/1996 tarihli ve 22762 sayılı Resmî Gazete'de yayımlanan İhracı Yasak ve Ön
İzne Bağlı Mallara İlişkin Tebliğ (İhracat 96/31) gereğince GTİP numarası
"0714.90.20.00.12" ve "1106.20.90.00.11" olan Orchidaceae (salepgiller)
familyası türlerinin yumru ve droglarının da (toz, tablet ve her türlü formda)
ihracatı yapılamaz.
b) Tablonun (II) numaralı sütununda yer alan türlerin firmalar bazında ihracat
kontenjanı, Teknik Komite tarafından belirlenir.

Doğal çiçek soğanlarının GTİP numarası
MADDE 5- (1) Doğal çiçek soğanlarının GTİP numarası "0601.10.90.10.00"dır.
"""

SNAPSHOT_COLUMNS = (
    "id, code, title, category, mevzuat_id, source_url, official_gazette_date, official_gazette_number, "
    "document_sha256, retrieved_at, valid_from, scope_count, authority, system, risk_based, "
    "physical_inspection_possible, laboratory_test_possible, required_documents_excerpt, active, direction"
)


def shipped_rule(code: str) -> dict:
    config = json.loads(Path("control_sources.json").read_text(encoding="utf-8"))
    return next(rule for rule in config["rules"] if rule["code"] == code)


class LiteralPlanTests(unittest.TestCase):
    def test_a_missing_block_yields_no_rows(self) -> None:
        self.assertEqual(literal_scope_plan({}), [])

    def test_codes_are_normalised_to_twelve_digits(self) -> None:
        plan = literal_scope_plan(
            {"scope_literal": {"rows": [{"gtip": "0601.10.90.10.00", "list_kind": "licence_required"}]}}
        )
        self.assertEqual(plan[0]["gtip"], "060110901000")
        self.assertEqual(plan[0]["list_kind"], "licence_required")

    def test_an_unknown_kind_falls_back_to_the_weakest_claim(self) -> None:
        # "kapsamda" demek "yasak" demekten daha zayıf bir iddiadır.
        plan = literal_scope_plan({"scope_literal": {"rows": [{"gtip": "0601.10", "list_kind": "uydurma"}]}})
        self.assertEqual(plan[0]["list_kind"], "scope")

    def test_an_unparsable_code_is_dropped_rather_than_guessed(self) -> None:
        plan = literal_scope_plan({"scope_literal": {"rows": [{"gtip": "bilinmiyor"}]}})
        self.assertEqual(plan, [])


class LiteralVerificationTests(unittest.TestCase):
    """Yapılandırma bir iddiadır; kaynak resmî metindir."""

    def test_declared_codes_present_in_the_official_text_become_rows(self) -> None:
        plan = literal_scope_plan(shipped_rule("IHR/CICEK-SOGANI"))
        rows, errors = extract_literal_scope(BULB_TEXT, plan)
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(row.gtip_prefix for row in rows),
            ["060110901000", "071490200012", "110620900011"],
        )

    def test_the_two_orchid_codes_are_prohibited_and_the_bulb_code_is_not(self) -> None:
        # Madde 4/1-a "ihracatı yapılamaz" diyor; Madde 5 yalnız kapsam GTİP'ini veriyor.
        rows, _ = extract_literal_scope(BULB_TEXT, literal_scope_plan(shipped_rule("IHR/CICEK-SOGANI")))
        kinds = {row.gtip_prefix: row.list_kind for row in rows}
        self.assertEqual(kinds["071490200012"], "prohibited")
        self.assertEqual(kinds["110620900011"], "prohibited")
        self.assertEqual(kinds["060110901000"], "licence_required")

    def test_a_code_absent_from_the_text_is_refused_and_reported(self) -> None:
        # Tebliğ değişip kod çıkarıldığında ürün eski kodu göstermeye devam edemez.
        plan = literal_scope_plan(
            {"scope_literal": {"rows": [{"gtip": "9999.99.99.99.99", "list_kind": "prohibited"}]}}
        )
        rows, errors = extract_literal_scope(BULB_TEXT, plan)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("999999999999", errors[0])

    def test_every_row_carries_the_article_reference_it_came_from(self) -> None:
        rows, _ = extract_literal_scope(BULB_TEXT, literal_scope_plan(shipped_rule("IHR/CICEK-SOGANI")))
        for row in rows:
            self.assertTrue(row.description, row.gtip_prefix)
            self.assertIn("Madde", row.description)


class ShippedBulbRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = shipped_rule("IHR/CICEK-SOGANI")

    def test_the_broken_attachment_path_is_gone(self) -> None:
        # Bedesten bu belgede `ekler` vermiyor; ek yolu her turda sessizce sıfır satır üretiyordu.
        self.assertNotIn("scope_attachment", self.rule)
        self.assertNotIn("scope_attachment_list_kind", self.rule)

    def test_the_record_says_plainly_that_the_species_table_is_not_indexed(self) -> None:
        note = self.rule["annex_note"]
        self.assertIn("İNDEKSLENMEMİŞTİR", note)
        self.assertIn("Ek-1", note)

    def test_the_note_promises_nothing_about_the_future(self) -> None:
        # "yakında eklenecek" demek, olmayan bir taahhüttür.
        self.assertNotIn("yakında", self.rule["annex_note"].casefold())

    def test_every_export_record_still_has_a_working_scope_source(self) -> None:
        rules = json.loads(Path("control_sources.json").read_text(encoding="utf-8"))["rules"]
        for rule in rules:
            if rule_direction(rule) != "export":
                continue
            self.assertTrue(
                rule.get("scope_table") or rule.get("scope_attachment") or rule.get("scope_literal"),
                rule["code"],
            )


class DirectionWordingTests(unittest.TestCase):
    """İhracat dosyasında "ithalat izni verilmez" demek yanlış işleme sevk eder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = ImportControlEngine(data_dir=self.tmp.name)
        self.engine.rules_config = [
            {"code": "2026/9", "title": "Oyuncak İthalat Denetimi Tebliği", "category": "oyuncak"},
            {
                "code": "IHR/TEST",
                "direction": "export",
                "optional": True,
                "title": "Test İhracat Tebliği",
                "category": "ihracat",
                "discovery": {"terms": ["Test"]},
            },
        ]
        row = (
            "2025-12-31", "33124", "2026-01-01T00:00:00+03:00", "2026-01-01",
            1, "Ticaret Bakanlığı", "TAREKS", 0, 1, 0, None, 1,
        )
        with self.engine._connect() as db:
            db.execute(
                f"INSERT INTO control_snapshots ({SNAPSHOT_COLUMNS}) VALUES ({','.join('?' * 20)})",
                ("imp", "2026/9", "Oyuncak İthalat Denetimi Tebliği", "test", "m-imp",
                 "https://mevzuat.adalet.gov.tr/", *row[:2], "sha-imp", *row[2:], "import"),
            )
            db.execute(
                f"INSERT INTO control_snapshots ({SNAPSHOT_COLUMNS}) VALUES ({','.join('?' * 20)})",
                ("exp", "IHR/TEST", "Test İhracat Tebliği", "test", "m-exp",
                 "https://mevzuat.adalet.gov.tr/", *row[:2], "sha-exp", *row[2:], "export"),
            )
            db.executemany(
                "INSERT INTO control_scope (snapshot_id, gtip_prefix, description, source_line, "
                "source_offset, excluded, list_kind) VALUES (?,?,?,?,?,?,?)",
                [
                    ("imp", "290314", "Karbon tetraklorür", "2903.14", 1, 0, "prohibited"),
                    ("exp", "290314", "Karbon tetraklorür", "2903.14", 1, 0, "prohibited"),
                    ("exp", "060110", "Doğal çiçek soğanları", "0601.10", 1, 0, "licence_required"),
                ],
            )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self.tmp.cleanup()

    def _assessment(self, gtip: str, direction: str) -> str:
        result = asyncio.run(self.engine.lookup(gtip, direction=direction))
        self.assertTrue(result.matches, f"{gtip} / {direction}")
        return result.matches[0].assessment

    def test_an_export_prohibition_never_talks_about_importing(self) -> None:
        text = self._assessment("290314000000", "export")
        self.assertIn("ihracı yasak", text)
        self.assertNotIn("ithal", text)

    def test_the_import_sentence_is_unchanged(self) -> None:
        # Gerileme kilidi: ithalat tarafındaki metin bugünküyle birebir aynı kalmalı.
        self.assertEqual(
            self._assessment("290314000000", "import"),
            "GTİP tebliğin ithali yasak eşya listesinde yer alıyor; kapsam istisnası yoksa ithalat izni verilmez.",
        )

    def test_a_licence_requirement_is_reported_as_a_permit_not_as_mere_scope(self) -> None:
        # Önceden bu satır "işlem sonucu yetkili kurumun incelemesine bağlıdır" oluyordu:
        # ön izin şartını kapsam bilgisine indirgemek yükümlülüğü gizler.
        text = self._assessment("060110000000", "export")
        self.assertIn("ön izne", text)
        self.assertIn("izne bağlıdır", text)

    def test_a_licence_match_warns_that_the_permit_terms_must_be_read(self) -> None:
        result = asyncio.run(self.engine.lookup("060110000000", direction="export"))
        self.assertTrue(
            any("Ön izin listesi eşleşmesi" in caution for caution in result.matches[0].cautions)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
