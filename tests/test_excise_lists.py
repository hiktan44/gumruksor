"""ÖTV (III) sayılı liste okuyucusu.

İki katman ayrı ayrı sınanır:

* :func:`excise_lists.build_sections` — saf mantık, girdisi resmî PDF'ten alınmış
  gerçek hücre dökümü (``tests/fixtures/excise_iii_cells.json``). Bu fikstür
  mevzuat.gov.tr'deki 4760 sayılı Kanun metninin 41-46. sayfalarından
  ``extract_pages`` ile üretilmiştir; tohumdaki satırlar birebir bundan gelir.
* :func:`excise_lists.extract_pages` — PDF katmanı. Fikstür olarak sentetik ama
  gerçek PDF'in ölçülen özelliklerini taşıyan bir sayfa kurulur: kılavuz çizgili
  tablo, 11 punto gövde metni ve 6,5 punto dipnot üstsimgesi.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from pathlib import Path

import excise_lists
from excise_lists import build_sections, extract_pages
from tax_lists import ExciseTaxIndex

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "excise_iii_cells.json"
SEED = ROOT / "data" / "official" / "excise_tax_lists.json"


def _pages() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class BuildSectionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sections = {section.cetvel: section for section in build_sections(_pages())}

    def test_both_cetvels_parse_without_warnings(self) -> None:
        self.assertEqual(sorted(self.sections), ["A", "B"])
        for cetvel, section in self.sections.items():
            self.assertEqual(section.warnings, [], f"({cetvel}) cetvelinde okuma uyarısı var")
            self.assertTrue(section.as_seed()["rates_verified"])

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
        rates = sorted(row["applied_tax_rate"] for row in variants)
        self.assertEqual(rates, ["42", "45"])
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

    def test_missing_table_lines_are_reported_not_swallowed(self) -> None:
        pages = _pages()
        pages[1]["tables"] = []
        section = {s.cetvel: s for s in build_sections(pages)}["A"]
        self.assertTrue(any("kılavuz çizgisi" in warning for warning in section.warnings))
        self.assertFalse(section.as_seed()["rates_verified"])

    def test_unknown_header_column_blocks_verification(self) -> None:
        pages = _pages()
        pages[0]["tables"][0][0][2] = "Beklenmeyen Sütun"
        section = {s.cetvel: s for s in build_sections(pages)}["A"]
        self.assertTrue(any("beklenen sütunlara oturmadı" in warning for warning in section.warnings))
        self.assertFalse(section.as_seed()["rates_verified"])


class ExtractPagesTests(unittest.TestCase):
    """PDF katmanı: kılavuz çizgileri ve font boyutu süzgeci.

    Sayfa metni bilinçli olarak ASCII'dir ve liste işaretleri ASCII'ye yamanır:
    PyMuPDF'in gömülü Helvetica'sı ``İ`` harfini taşımaz (Latin-1 dışı) ve sentetik
    fikstür bir sistem fontunun kurulu olmasına bağlı kalmamalıdır. Gerçek Türkçe
    başlıklar zaten resmî hücre dökümü fikstürüyle ve canlı okumayla sınanıyor.
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
        with mock.patch.object(excise_lists, "LIST_START", self.START), mock.patch.object(
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


class SeedMatchesParserTests(unittest.TestCase):
    """Tohumdaki (III) satırları depodaki okuyucunun çıktısıyla birebir aynı olmalı."""

    def test_seed_rows_are_the_parser_output(self) -> None:
        produced = {(s.list_name, s.cetvel): s.as_seed() for s in build_sections(_pages())}
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        found = 0
        for section in seed["sections"]:
            key = (section.get("list"), section.get("cetvel"))
            if key not in produced:
                continue
            found += 1
            expected = produced[key]
            self.assertEqual(section["rows"], expected["rows"], f"{key} satırları ayrıştırıcıdan farklı")
            self.assertEqual(section["value_columns"], expected["value_columns"])
            self.assertEqual(section["row_count"], expected["row_count"])
            self.assertTrue(section["rates_verified"])
            self.assertEqual(section["source_url"], excise_lists.SOURCE_URL)
            self.assertEqual(len(section["source_sha256"]), 64)
        self.assertEqual(found, 2, "(III) sayılı listenin iki cetveli de tohumda olmalı")

    def test_other_lists_keep_their_text_flow_rows(self) -> None:
        """(I), (II) ve (IV) bu işin kapsamı dışındadır; dokunulmadığı kilitlenir."""
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        others = {
            (section["list"], section.get("cetvel")): section
            for section in seed["sections"]
            if section["list"] != "III"
        }
        self.assertEqual(
            sorted(others), [("I", "A"), ("I", "B"), ("II", None), ("IV", None)]
        )
        for key, section in others.items():
            self.assertNotIn("parsed_from", section, f"{key} bu PR'da değişmemeliydi")


class ExciseLookupTests(unittest.TestCase):
    """Sorgu katmanı artık (III) sayılı listede oran döndürüyor."""

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

    def test_verified_section_warns_about_presidential_decrees_not_about_parsing(self) -> None:
        warnings = " ".join(self.index.lookup("2208.90.48.00.11")["warnings"])
        self.assertIn("Cumhurbaşkanı kararlarıyla değiştirilebilir", warnings)
        self.assertNotIn("otomatik okunmadı", warnings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
