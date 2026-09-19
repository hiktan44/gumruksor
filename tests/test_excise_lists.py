"""ÖTV (I)-(IV) sayılı liste okuyucusu.

İki katman ayrı ayrı sınanır:

* :func:`excise_lists.build_sections` — saf mantık, girdisi resmî PDF'ten alınmış
  gerçek hücre dökümleri (``tests/fixtures/excise_{i,ii,iii,iv}_cells.json``). Bu
  fikstürler mevzuat.gov.tr'deki 4760 sayılı Kanun metninden ``extract_pages`` ile
  üretilmiştir; tohumdaki her satır birebir bunlardan gelir ve bir test bunu kilitler.
* :func:`excise_lists.extract_pages` — PDF katmanı. Fikstür olarak sentetik ama gerçek
  PDF'in ölçülen özelliklerini taşıyan bir sayfa kurulur: kılavuz çizgili tablo,
  11 punto gövde metni ve 6,5 punto dipnot üstsimgesi.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import excise_lists
from excise_lists import build_sections, extract_pages
from tax_lists import ExciseTaxIndex

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SEED = ROOT / "data" / "official" / "excise_tax_lists.json"

#: Liste adı → fikstür dosyası eki.
SLUGS = {"I": "i", "II": "ii", "III": "iii", "IV": "iv"}


def _pages(list_name: str) -> list[dict]:
    return json.loads((FIXTURES / f"excise_{SLUGS[list_name]}_cells.json").read_text(encoding="utf-8"))


def _sections(list_name: str) -> dict[str | None, excise_lists.ExciseSection]:
    return {section.cetvel: section for section in build_sections(_pages(list_name), list_name)}


class ListIIITests(unittest.TestCase):
    """Alkollü içecekler, tütün mamulleri, kolalı gazozlar."""

    def setUp(self) -> None:
        self.sections = _sections("III")

    def test_both_cetvels_parse_without_warnings(self) -> None:
        self.assertEqual(sorted(self.sections, key=str), ["A", "B"])
        for cetvel, section in self.sections.items():
            self.assertEqual(section.warnings, [], f"({cetvel}) cetvelinde okuma uyarısı var")
            self.assertTrue(section.rates_verified)

    def test_column_layout_comes_from_the_official_header(self) -> None:
        self.assertEqual(
            self.sections["A"].value_columns,
            ["tax_rate", "minimum_specific_tax", "applied_tax_rate", "applied_minimum_specific_tax"],
        )
        # (B) cetvelinde maktu vergi sütunu da var: sigara/puro maktu vergisi.
        self.assertEqual(
            self.sections["B"].value_columns,
            [
                "tax_rate",
                "minimum_specific_tax",
                "applied_tax_rate",
                "applied_minimum_specific_tax",
                "applied_specific_tax",
            ],
        )

    def test_rows_keep_their_own_description_and_rate(self) -> None:
        """Eski metin-akışı çıkarımında eşya adı bir alt satıra kaymıştı."""
        rows = {row["code"]: row for row in self.sections["A"].rows}
        self.assertEqual(rows["2202.10.00.00.13"]["description"], "Kolalı gazozlar")
        self.assertEqual(rows["2202.10.00.00.13"]["applied_tax_rate"], "35")
        self.assertEqual(rows["2203.00"]["description"], "Malttan üretilen biralar")
        self.assertEqual(rows["2203.00"]["applied_minimum_specific_tax"], "12,4849")
        raki = rows["2208.90.48.00.11"]
        self.assertTrue(raki["description"].startswith("Rakı"))
        self.assertEqual(raki["applied_tax_rate"], "0")
        self.assertEqual(raki["applied_minimum_specific_tax"], "1.705,9025")

    def test_footnote_superscripts_are_not_glued_to_the_rate(self) -> None:
        """``4559`` resmî metinde ``45`` oranı ve ``59`` numaralı dipnottur."""
        rows = {row["code"]: row for row in self.sections["B"].rows}
        self.assertEqual(rows["2402.10.00.00.11"]["applied_tax_rate"], "45")
        self.assertEqual(rows["2402.10.00.00.11"]["applied_specific_tax"], "2,6909")
        self.assertEqual(rows["2402.20"]["applied_tax_rate"], "42")
        # 65,25 gerçek bir orandır; dipnot ayıklaması onu kesmemeli.
        self.assertEqual(rows["2402.20"]["tax_rate"], "65,25")

    def test_continuation_rows_extend_the_previous_description(self) -> None:
        rows = {row["code"]: row for row in self.sections["A"].rows}
        # 22.02'nin tanımı sayfa sonunda bölünüyor; devamı aynı satıra eklenmeli.
        self.assertIn("toptan teslime konu edilenler)", rows["22.02"]["description"])
        self.assertIn("2202.91.00.00.00 hariç)", rows["22.02"]["description"])

    def test_sub_variants_keep_their_own_rates(self) -> None:
        """``2402.90.00.00.00`` ana satırının oranı yok; iki varyantının farklı oranı var."""
        variants = [row for row in self.sections["B"].rows if row["code"] == "2402.90.00.00.00"]
        self.assertEqual(len(variants), 2)
        self.assertEqual(sorted(row["applied_tax_rate"] for row in variants), ["42", "45"])
        for row in variants:
            self.assertTrue(row["description"].startswith("Diğerleri (Tütün yerine geçen"))
        purolar = next(row for row in variants if row["applied_tax_rate"] == "45")
        self.assertIn("-Tütün yerine geçen maddelerden yapılmış purolar", purolar["description"])

    def test_exclusion_lists_inside_a_description_do_not_open_a_row(self) -> None:
        """Tanım içindeki "… hariç)" kod listesi ayrı satır sayılmamalı."""
        codes = [row["code"] for row in self.sections["B"].rows]
        self.assertNotIn("2403.99.90.00.00", codes)
        self.assertIn("2403.99.90.00.00 hariç)", next(
            row["description"] for row in self.sections["B"].rows if row["code"] == "24.03"
        ))


class ListITests(unittest.TestCase):
    """Petrol ürünleri: maktu tutar ve ölçü birimi sütunlu."""

    def setUp(self) -> None:
        self.sections = _sections("I")

    def test_both_cetvels_parse_without_warnings(self) -> None:
        self.assertEqual(sorted(self.sections, key=str), ["A", "B"])
        for cetvel, section in self.sections.items():
            self.assertEqual(section.warnings, [], f"({cetvel}) cetvelinde okuma uyarısı var")
            self.assertTrue(section.rates_verified)
            self.assertEqual(section.value_columns, ["tax_amount", "applied_tax_amount", "unit"])

    def test_fuel_rows_carry_their_own_amount_and_unit(self) -> None:
        rows = {row["code"]: row for row in self.sections["A"].rows}
        motorin = rows["2710.19.43.00.11"]
        self.assertTrue(motorin["description"].startswith("Motorin"))
        self.assertEqual(motorin["applied_tax_amount"], "13,9006")
        self.assertEqual(motorin["unit"], "Litre")
        self.assertEqual(rows["2710.12.49.00.11"]["applied_tax_amount"], "15,5437")

    def test_vertically_merged_code_cell_is_put_back_on_its_own_row(self) -> None:
        """``2710.12.45.00.13`` resmî tabloda kodu üstte, adı ve oranı altta basılı."""
        rows = {row["code"]: row for row in self.sections["A"].rows}
        e10 = rows["2710.12.45.00.13"]
        self.assertTrue(e10["description"].startswith("Kurşunsuz benzin 95 oktan (E10)"))
        self.assertEqual(e10["applied_tax_amount"], "14,8277")

    def test_gas_sub_variants_keep_both_amounts(self) -> None:
        """Doğal gazda "motorlu taşıt yakıtı" ile "diğerleri" farklı tutar taşır."""
        variants = [row for row in self.sections["A"].rows if row["code"] == "2711.11.00.00.00"]
        self.assertEqual(len(variants), 2)
        self.assertEqual(
            sorted(row["applied_tax_amount"] for row in variants), ["0,1468", "5,5049"]
        )
        for row in variants:
            self.assertEqual(row["unit"], "Standart Metreküp")

    def test_category_caption_before_the_first_code_is_dropped_quietly(self) -> None:
        """"(Hafif yağlar ve müstahzarları)" bir kategori başlığıdır, oran taşımaz."""
        rows = self.sections["A"].rows
        self.assertEqual(rows[0]["code"], "2710.12.11.00.00")
        self.assertEqual(self.sections["A"].warnings, [])


class ListIVTests(unittest.TestCase):
    """Lüks ve dayanıklı tüketim malları."""

    def setUp(self) -> None:
        self.section = _sections("IV")[None]

    def test_single_cetvel_without_warnings(self) -> None:
        self.assertIsNone(self.section.cetvel)
        self.assertEqual(self.section.warnings, [])
        self.assertTrue(self.section.rates_verified)
        self.assertEqual(self.section.value_columns, ["tax_rate", "applied_tax_rate"])

    def test_appliance_rows_read_cleanly(self) -> None:
        rows = {row["code"]: row for row in self.section.rows}
        self.assertEqual(rows["84.18"]["tax_rate"], "6,7")
        self.assertTrue(rows["84.18"]["description"].startswith("Buzdolapları"))
        self.assertEqual(rows["9405.10.50.10.11"]["description"], "Kristal avizeler")

    def test_merged_code_cells_are_repaired(self) -> None:
        """İki ayrı birleşme deseni: adı alta düşen kod ve adı olmayan kod."""
        rows = {row["code"]: row for row in self.section.rows}
        self.assertIn("Manikür ve pedikür", rows["8214.20.00.00.19"]["description"])
        self.assertEqual(rows["8214.20.00.00.19"]["tax_rate"], "20")
        self.assertIn("halk bandı (CB)", rows["8517.69.90.90.24"]["description"])
        self.assertEqual(rows["8517.69.90.90.24"]["tax_rate"], "20")
        # Oran bir önceki koda yapışmamalı: 8517.69.30 yalnız kendi satırını taşır.
        self.assertEqual(len([r for r in self.section.rows if r["code"] == "8517.69.30.00.00"]), 1)

    def test_rows_with_collapsed_rate_cells_are_flagged_individually(self) -> None:
        """Alt kırılımları tek hücrede birleşen satır oran göstermez; liste doğrulanmış kalır."""
        flagged = {row["code"] for row in self.section.rows if row.get("rates_verified") is False}
        self.assertEqual(flagged, {"33.07", "8517.12.00.00.11"})
        phone = next(row for row in self.section.rows if row["code"] == "8517.12.00.00.11")
        self.assertEqual(phone["tax_rate"], "25 40 50")
        self.assertTrue(self.section.rates_verified, "tek satır bütün listeyi düşürmemeli")


class ListIITests(unittest.TestCase):
    """Motorlu taşıtlar: makine tarafından güvenilir okunamıyor, dürüstçe öyle raporlanır."""

    def setUp(self) -> None:
        self.section = _sections("II")[None]

    def test_the_list_is_not_marked_verified(self) -> None:
        self.assertFalse(self.section.rates_verified)

    def test_every_reason_is_recorded(self) -> None:
        reasons = " ".join(self.section.warnings)
        # İki sayfada kılavuz çizgisi yok.
        self.assertIn("36. sayfada tablo kılavuz çizgisi bulunamadı", reasons)
        self.assertIn("37. sayfada tablo kılavuz çizgisi bulunamadı", reasons)
        # Başlıkta iki oran sütunu aynı metni taşıyor.
        self.assertIn("beklenen sütunlara oturmadı", reasons)

    def test_scope_survives_but_no_rate_is_carried(self) -> None:
        """Kapsam değerlidir: hangi kodun ÖTV'ye tabi olduğu yine bilinir."""
        codes = {row["code"] for row in self.section.rows}
        self.assertIn("87.03", codes)
        self.assertIn("87.11", codes)
        self.assertEqual(self.section.value_columns, [])
        for row in self.section.rows:
            self.assertEqual(set(row) - {"code", "description"}, set(), f"{row['code']} oran taşıyor")

    def test_descriptions_are_the_official_ones(self) -> None:
        rows = {row["code"]: row for row in self.section.rows}
        self.assertTrue(rows["87.03"]["description"].startswith("Binek otomobilleri"))
        self.assertTrue(rows["87.01"]["description"].startswith("Traktörler"))


class WarningPathTests(unittest.TestCase):
    def test_missing_table_lines_are_reported_not_swallowed(self) -> None:
        pages = _pages("III")
        pages[1]["tables"] = []
        section = _sections_from(pages, "III")["A"]
        self.assertTrue(any("kılavuz çizgisi" in warning for warning in section.warnings))
        self.assertFalse(section.rates_verified)

    def test_unknown_header_column_blocks_verification(self) -> None:
        pages = _pages("III")
        pages[0]["tables"][0][0][2] = "Beklenmeyen Sütun"
        section = _sections_from(pages, "III")["A"]
        self.assertTrue(any("beklenen sütunlara oturmadı" in warning for warning in section.warnings))
        self.assertFalse(section.rates_verified)

    def test_the_same_warning_is_not_repeated_per_row(self) -> None:
        section = _sections("II")[None]
        self.assertEqual(len(section.warnings), len(set(section.warnings)))

    def test_orphan_row_carrying_a_rate_is_reported(self) -> None:
        """Kodsuz ama oranlı bir parça listenin başındaysa kaybolan satırdır."""
        pages = [{
            "page": 1,
            "cetvel_b": False,
            "tables": [[
                ["G.T.İ.P. NO", "Mal İsmi", "Vergi Oranı (%)"],
                ["", "Kodsuz ama oranlı", "20"],
            ]],
        }]
        section = _sections_from(pages, "IV")[None]
        self.assertTrue(any("hiçbir koda bağlanamadı" in warning for warning in section.warnings))


def _sections_from(pages: list[dict], list_name: str) -> dict[str | None, excise_lists.ExciseSection]:
    return {section.cetvel: section for section in build_sections(pages, list_name)}


class ExtractPagesTests(unittest.TestCase):
    """PDF katmanı: kılavuz çizgileri ve font boyutu süzgeci.

    Sayfa metni bilinçli olarak ASCII'dir ve liste işaretleri ASCII'ye yamanır:
    PyMuPDF'in gömülü Helvetica'sı ``İ`` harfini taşımaz (Latin-1 dışı) ve sentetik
    fikstür bir sistem fontunun kurulu olmasına bağlı kalmamalıdır. Gerçek Türkçe
    başlıklar zaten resmî hücre dökümü fikstürleriyle ve canlı okumayla sınanıyor.
    """

    START = "LIST III MARKER"
    CETVEL_B = "CETVEL B MARKER"

    def _pdf(self) -> bytes:
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((60, 60), self.START, fontsize=12)
        page.insert_text((60, 80), self.CETVEL_B, fontsize=12)
        columns = [50, 150, 330, 400, 470]
        rows = [100, 130, 160]
        for x in columns:
            page.draw_line((x, rows[0]), (x, rows[-1]))
        for y in rows:
            page.draw_line((columns[0], y), (columns[-1], y))
        cells = [
            ["GTIP NO", "Mal Ismi", "Vergi Orani (%)", "Uygulanacak Vergi Orani (%)"],
            ["2402.20", "Tutun iceren sigaralar", "65,25", "42"],
        ]
        for row_index, row in enumerate(cells):
            for column_index, text in enumerate(row):
                page.insert_text(
                    (columns[column_index] + 3, rows[row_index] + 14), text, fontsize=11
                )
        # Uygulanacak oranın yanına 6,5 puntoluk dipnot üstsimgesi: ayıklanmalı.
        page.insert_text((columns[3] + 18, rows[1] + 12), "59", fontsize=6.5)
        payload = document.tobytes()
        document.close()
        return payload

    def test_cells_come_from_the_ruling_lines_and_drop_footnotes(self) -> None:
        bounds = dict(excise_lists.LIST_BOUNDS, III=(self.START, "BITIS MARKER"))
        with mock.patch.object(excise_lists, "LIST_BOUNDS", bounds), mock.patch.object(
            excise_lists, "CETVEL_B_MARKER", self.CETVEL_B
        ):
            pages = extract_pages(self._pdf())
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["cetvel_b"])
        table = pages[0]["tables"][0]
        self.assertEqual(table[0][0], "GTIP NO")
        self.assertEqual(table[1][0], "2402.20")
        self.assertEqual(table[1][1], "Tutun iceren sigaralar")
        self.assertEqual(table[1][2], "65,25")
        self.assertEqual(table[1][3], "42", "dipnot üstsimgesi orana yapışmış")

    def test_missing_list_heading_raises(self) -> None:
        import pymupdf

        document = pymupdf.open()
        document.new_page().insert_text((60, 60), "Baska bir belge", fontsize=12)
        payload = document.tobytes()
        document.close()
        with self.assertRaises(excise_lists.ExciseParseError):
            extract_pages(payload)

    def test_unknown_list_name_raises(self) -> None:
        with self.assertRaises(excise_lists.ExciseParseError):
            extract_pages(b"%PDF-1.5", "V")


class SeedMatchesParserTests(unittest.TestCase):
    """Tohumdaki her satır depodaki okuyucunun fikstür üzerindeki çıktısı olmalı."""

    def setUp(self) -> None:
        self.seed = json.loads(SEED.read_text(encoding="utf-8"))

    def test_every_section_is_the_parser_output(self) -> None:
        produced: dict[tuple[str, str | None], dict] = {}
        for list_name in SLUGS:
            for section in build_sections(_pages(list_name), list_name):
                produced[(list_name, section.cetvel)] = section.as_seed()
        self.assertEqual(len(self.seed["sections"]), len(produced))
        for section in self.seed["sections"]:
            key = (section["list"], section.get("cetvel"))
            self.assertIn(key, produced, f"{key} ayrıştırıcıda yok")
            expected = produced[key]
            self.assertEqual(section["rows"], expected["rows"], f"{key} satırları farklı")
            self.assertEqual(section["value_columns"], expected["value_columns"])
            self.assertEqual(section["row_count"], expected["row_count"])
            self.assertEqual(section["rates_verified"], expected["rates_verified"])
            self.assertEqual(section["source_url"], excise_lists.SOURCE_URL)
            self.assertEqual(len(section["source_sha256"]), 64)

    def test_unverified_sections_record_their_reason_in_the_seed(self) -> None:
        for section in self.seed["sections"]:
            if section["rates_verified"]:
                self.assertNotIn("parse_warnings", section)
            else:
                self.assertTrue(section.get("parse_warnings"), f"{section['list']} sebebini yazmıyor")

    def test_only_list_two_is_unverified(self) -> None:
        unverified = {
            (section["list"], section.get("cetvel"))
            for section in self.seed["sections"]
            if not section["rates_verified"]
        }
        self.assertEqual(unverified, {("II", None)})


class ExciseLookupTests(unittest.TestCase):
    """Sorgu katmanı: doğrulanmış satırda oran gösterilir, doğrulanmamışta gösterilmez."""

    def setUp(self) -> None:
        self.index = ExciseTaxIndex()

    def test_raki_returns_its_minimum_specific_tax(self) -> None:
        report = self.index.lookup("2208.90.48.00.11")
        self.assertTrue(report["in_scope"])
        match = report["matches"][0]
        self.assertTrue(match["rates_verified"])
        self.assertEqual(match["values"]["Uygulanacak asgari maktu vergi tutarı (TL)"], "1.705,9025")
        self.assertEqual(match["values"]["Uygulanacak vergi oranı (%)"], "0")

    def test_cigarettes_return_rate_and_specific_tax(self) -> None:
        match = self.index.lookup("2402.20.10.00.11")["matches"][0]
        self.assertEqual(match["values"]["Uygulanacak vergi oranı (%)"], "42")
        self.assertEqual(match["values"]["Uygulanacak maktu vergi tutarı (TL)"], "23,7404")

    def test_cola_keeps_the_higher_applied_rate(self) -> None:
        match = self.index.lookup("2202.10.00.00.13")["matches"][0]
        self.assertEqual(match["values"]["Kanuni vergi oranı (%)"], "25")
        self.assertEqual(match["values"]["Uygulanacak vergi oranı (%)"], "35")

    def test_diesel_returns_its_amount_per_litre(self) -> None:
        match = self.index.lookup("2710.19.43.00.11")["matches"][0]
        self.assertTrue(match["rates_verified"])
        self.assertEqual(match["values"]["Uygulanacak vergi tutarı (TL)"], "13,9006")
        self.assertEqual(match["values"]["Birim"], "Litre")

    def test_refrigerator_returns_its_rate(self) -> None:
        match = self.index.lookup("8418.10.20.00.00")["matches"][0]
        self.assertTrue(match["rates_verified"])
        self.assertEqual(match["values"]["Kanuni vergi oranı (%)"], "6,7")

    def test_passenger_car_reports_scope_without_a_rate(self) -> None:
        """(II) sayılı liste doğrulanmadı: kapsam evet, oran hayır."""
        report = self.index.lookup("8703.23.19.00.00")
        self.assertTrue(report["in_scope"])
        match = report["matches"][0]
        self.assertEqual(match["matched_code"], "87.03")
        self.assertFalse(match["rates_verified"])
        self.assertEqual(match["values"], {})
        self.assertTrue(any("güvenilir okunamıyor" in warning for warning in report["warnings"]))
        self.assertTrue(any("kılavuz çizgisi" in warning for warning in report["warnings"]))

    def test_mobile_phone_row_is_suppressed_but_the_list_is_not(self) -> None:
        phone = self.index.lookup("8517.12.00.00.11")["matches"][0]
        self.assertFalse(phone["rates_verified"])
        self.assertEqual(phone["values"], {})
        warnings = " ".join(self.index.lookup("8517.12.00.00.11")["warnings"])
        self.assertIn("alt kırılımlarla birleşik", warnings)
        self.assertIn("diğer satırları doğrulanmıştır", warnings)

    def test_verified_section_warns_about_presidential_decrees_not_about_parsing(self) -> None:
        warnings = " ".join(self.index.lookup("2208.90.48.00.11")["warnings"])
        self.assertIn("Cumhurbaşkanı kararlarıyla değiştirilebilir", warnings)
        self.assertNotIn("okunamadı", warnings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
