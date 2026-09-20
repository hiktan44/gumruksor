"""Resmî eşya tanımı cetveli: ayrıştırma, depo ve "oran değil" rayı.

Bu dosyanın koruduğu değişmezler:

* **Taşan tanım kesilmez.** Resmî cetvel tanımı alt satıra taşırır ve kelimeyi
  tireyle böler; birleştirme yanlış yapılırsa tanımların yarısı bozulur.
* **Tanım tek başına saklanmaz.** 12 haneli kodun tam yolu ata satırlardan kurulur;
  "Cihazlar" tek başına hiçbir sınıflandırma sorusunu cevaplamaz.
* **``474 Vergi Haddi`` bir oran değildir.** Kanuni azami hadd, uygulanan gümrük
  vergisi değil. Hiçbir sorgu onu oran adıyla döndürmez.
* **Yarım cetvel eskisinin yerine geçmez.** Kaynağın düzeni değişip ayrıştırma
  çökerse mevcut anlık görüntü korunur.

Fikstür satırları 20.09.2026'da resmî 2026 TGTC arşivinden (sha256
073bef48ff2bcaf5f04bf2f48e17b26a4062b06ece30067249d635afb90a55f3) ölçülen gerçek
satırlardır; ikili dosya yerine hücre değerleri olarak yazıya geçirilmiştir.
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

import httpx
from openpyxl import Workbook

import tariff_nomenclature as nom
from security_firewall import SecurityViolation

# 84. fasıl başı — gerçek satırlar: taşan başlık, tireli kırılım, kodsuz ağaç düğümü.
CHAPTER_84_ROWS: list[list[Any]] = [
    ["", "", "ÖLÇÜ\nBİRİMİ", "474\nVERGİ\nHADDİ"],
    ["", "", "", ""],
    ["POZİSYON NO", "EŞYANIN TANIMI", "", ""],
    ["", "", "", ""],
    [1.0, 2.0, 3.0, 4.0],
    ["84.01", "Nükleer reaktörler; nükleer reaktörler için ışınlanmamış yakıt ele-", "", ""],
    ["", "manları (kartuşlar); izotopik ayırım için makina ve cihazlar :", "", ""],
    ["8401.10.00.00.00", " - Nükleer reaktörler", "-", 15.0],
    ["8401.20", " - İzotopik ayırım için makina ve cihazlar, bunların aksam ve", "", ""],
    ["", "   parçaları :", "", ""],
    ["", " - - Uranyum izotoplarının ayırımına mahsus olanlar, bunların aksam ve", "", ""],
    ["", "      parçaları", "", ""],
    ["8401.20.00.10.11", " - - - Cihazlar", "-", 30.0],
    ["8401.20.00.10.15", " - - - Aksam ve parçalar", "-", 30.0],
    ["", " - - Diğerleri", "", ""],
    ["8401.20.00.90.11", " - - - Cihazlar", "-", 50.0],
]

# 63. fasıl başı — gerçek satırlar: bölüm başlığı (I.) ve ölçü birimi dolu satırlar.
CHAPTER_63_ROWS: list[list[Any]] = [
    ["", "", "ÖLÇÜ\nBİRİMİ", "474\nVERGİ\nHADDİ"],
    ["", "", "", ""],
    ["POZİSYON NO", "EŞYANIN TANIMI", "", ""],
    ["", "", "", ""],
    [1.0, 2.0, 3.0, 4.0],
    ["", "I. DOKUMAYA ELVERİŞLİ MADDELERDEN DİĞER HAZIR EŞYA", "", ""],
    ["", "", "", ""],
    ["63.01", "Battaniyeler ve seyahat battaniyeleri:", "", ""],
    ["6301.10.00.00.00", " - Elektrikli battaniyeler", "Adet", 100.0],
    ["6301.20", " - Yünden veya ince hayvan kılından battaniyeler (elektrikli olanlar", "", ""],
    ["", "  hariç) ve seyahat battaniyeleri:", "", ""],
    ["6301.20.10.00.00", " - - Örme veya kroşe", "Adet", 100.0],
    ["6301.20.90.00.00", " - - Diğerleri  ", "Adet", 100.0],
]

NOTES_84_ROWS: list[list[Any]] = [
    ["BÖLÜM XVI", ""],
    ["", ""],
    ["Notlar", ""],
    ["1. Aşağıda yazılı olanlar bu bölüme dahil değildir:", ""],
    ["(a) 39. fasıldaki plastik maddelerden taşıyıcı kolanlar (40.10 pozisyonu);", ""],
]

GRI_ROWS: list[list[Any]] = [
    ["TARİFENİN YORUMU İLE İLGİLİ GENEL KURALLAR"],
    [""],
    ["1. Bölüm, fasıl ve tali fasıl başlıkları sadece gösterici niteliktedir;"],
]


def _xlsx(rows: list[list[Any]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def build_archive(*, duplicate: bool = False, chapters: dict[str, list[list[Any]]] | None = None) -> bytes:
    """Gerçek arşivin yapısını taklit eden sentetik ZIP (fasıl + notlar + yorum kuralları)."""
    members = chapters if chapters is not None else {"84": CHAPTER_84_ROWS, "63": CHAPTER_63_ROWS}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for chapter, rows in members.items():
            archive.writestr(f"2026 TGTC/2026 TGTC/{chapter} fasıl 2026.xlsx", _xlsx(rows))
        if duplicate:
            archive.writestr("2026 TGTC/2026 TGTC/84 fasıl 2025.xlsx", _xlsx(CHAPTER_84_ROWS))
        archive.writestr("2026 TGTC/2026 FASIL NOTLARI/Fasıl 84.xlsx", _xlsx(NOTES_84_ROWS))
        archive.writestr("2026 TGTC/yorum kuralları.xlsx", _xlsx(GRI_ROWS))
    return buffer.getvalue()


class CodeNormalisationTests(unittest.TestCase):
    def test_dotted_official_positions_become_digits(self):
        self.assertEqual(nom.normalise_code("8401.10.00.00.00"), "840110000000")
        self.assertEqual(nom.normalise_code("84.01"), "8401")
        self.assertEqual(nom.normalise_code("8401.20"), "840120")

    def test_invalid_values_are_rejected_rather_than_guessed(self):
        # Tek haneli, tek sayıda haneli ve geçersiz fasıllı değer kod değildir.
        for value in ("", None, "1", "841", "0012", "sekiz", "-"):
            self.assertEqual(nom.normalise_code(value), "", repr(value))

    def test_a_twelve_digit_code_is_never_truncated_silently(self):
        self.assertEqual(len(nom.normalise_code("6301.20.10.00.00")), 12)


class ContinuationTests(unittest.TestCase):
    def test_a_wrapped_line_is_a_continuation_but_a_dashed_line_is_not(self):
        self.assertTrue(nom.is_continuation("   parçaları :"))
        self.assertFalse(nom.is_continuation(" - - Diğerleri"))
        self.assertFalse(nom.is_continuation(""))

    def test_a_roman_numeral_section_heading_is_not_a_continuation(self):
        # "I. DOKUMAYA ELVERİŞLİ…" bir bölüm başlığıdır; önceki tanıma eklenmemeli.
        self.assertFalse(nom.is_continuation("I. DOKUMAYA ELVERİŞLİ MADDELERDEN DİĞER HAZIR EŞYA"))

    def test_a_word_split_by_the_line_break_is_rejoined(self):
        # "yakıt ele-" + "manları" → "elemanları"; boşlukla birleştirmek iki bozuk
        # parça üretir ve "eleman" arayan kullanıcı satırı bulamaz.
        self.assertEqual(nom.join_continuation("yakıt ele-", "manları (kartuşlar)"), "yakıt elemanları (kartuşlar)")

    def test_a_normal_wrap_joins_with_a_space(self):
        self.assertEqual(nom.join_continuation("makina ve", "cihazlar"), "makina ve cihazlar")

    def test_dash_depth_is_the_nomenclature_level(self):
        self.assertEqual(nom.depth_of("Battaniyeler"), 0)
        self.assertEqual(nom.depth_of(" - Elektrikli battaniyeler"), 1)
        self.assertEqual(nom.depth_of(" - - - Cihazlar"), 3)


class ChapterParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = nom.parse_chapter_rows(CHAPTER_84_ROWS, chapter="84")
        self.by_code = {row.code: row for row in self.rows}

    def test_header_and_column_numbering_rows_are_not_data(self):
        self.assertNotIn("", self.by_code)
        self.assertEqual(len(self.rows), 6)

    def test_the_wrapped_heading_is_whole(self):
        heading = self.by_code["8401"].description
        self.assertIn("ışınlanmamış yakıt elemanları (kartuşlar)", heading)
        self.assertNotIn("ele- manları", heading)

    def test_the_full_path_walks_every_ancestor_including_uncoded_nodes(self):
        path = self.by_code["840120001011"].full_path
        # "Cihazlar" tek başına anlamsız; yol aileyi, alt aileyi ve kodsuz ağaç
        # düğümünü ("Uranyum izotoplarının…") taşımalı.
        self.assertTrue(path.endswith("> Cihazlar"))
        self.assertIn("İzotopik ayırım için makina", path)
        self.assertIn("Uranyum izotoplarının", path)

    def test_two_leaves_with_the_same_terse_name_get_different_paths(self):
        # Her ikisinin tanımı "Cihazlar"; ayırt eden şey yoldur.
        first = self.by_code["840120001011"]
        second = self.by_code["840120009011"]
        self.assertEqual(first.description, second.description)
        self.assertNotEqual(first.full_path, second.full_path)
        self.assertIn("Diğerleri", second.full_path)

    def test_parent_code_skips_uncoded_tree_nodes(self):
        self.assertEqual(self.by_code["840120001011"].parent_code, "840120")
        self.assertEqual(self.by_code["840110000000"].parent_code, "8401")

    def test_unit_and_statutory_rate_are_captured_as_text(self):
        row = self.by_code["840110000000"]
        self.assertEqual(row.unit, "-")
        self.assertEqual(row.statutory_rate_text, "15")

    def test_a_section_heading_does_not_pollute_the_first_real_heading(self):
        rows = {row.code: row for row in nom.parse_chapter_rows(CHAPTER_63_ROWS, chapter="63")}
        self.assertNotIn("DOKUMAYA ELVERİŞLİ", rows["6301"].description)
        self.assertIn("hariç) ve seyahat battaniyeleri", rows["630120"].description)
        self.assertEqual(rows["6301100000000"[:12]].unit, "Adet")


class ArchiveParsingTests(unittest.TestCase):
    def test_chapters_notes_and_interpretation_rules_are_all_read(self):
        rows, notes, warnings = nom.NomenclatureEngine.parse_archive(build_archive())
        self.assertGreaterEqual(len(rows), 8)
        kinds = {note["kind"] for note in notes}
        self.assertEqual(kinds, {"chapter", "gri"})
        chapter_note = next(note for note in notes if note["kind"] == "chapter")
        self.assertEqual(chapter_note["chapter"], "84")
        self.assertIn("bu bölüme dahil değildir", chapter_note["body"])
        gri = next(note for note in notes if note["kind"] == "gri")
        self.assertIn("GENEL KURALLAR", gri["body"])
        self.assertEqual(warnings, [])

    def test_a_duplicate_code_is_reported_and_the_first_file_wins(self):
        rows, _, warnings = nom.NomenclatureEngine.parse_archive(build_archive(duplicate=True))
        codes = [row.code for row in rows]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(any("yinelenen" in item for item in warnings))

    def test_a_chapter_file_with_no_codes_is_a_warning_not_a_silent_skip(self):
        empty = [["POZİSYON NO", "EŞYANIN TANIMI", "", ""], [1.0, 2.0, 3.0, 4.0]]
        _, _, warnings = nom.NomenclatureEngine.parse_archive(
            build_archive(chapters={"84": CHAPTER_84_ROWS, "99": empty})
        )
        self.assertTrue(any("kod satırı bulunamadı" in item for item in warnings))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = nom.NomenclatureStore(self._tmp.name)
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="a" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/duyurular/x",
            archive_url="https://ggm.ticaret.gov.tr/data/x/2026%20TGTC.zip",
            legal_act="Cumhurbaşkanlığı Kararı 10781", valid_from="2026-01-01",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_snapshot_is_active_and_countable(self):
        snapshot = self.store.active_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["legal_act"], "Cumhurbaşkanlığı Kararı 10781")
        self.assertGreater(snapshot["code_count"], 0)
        self.assertIn("12", self.store.counts("nomenclature:test"))

    def test_children_are_read_from_the_parent_link(self):
        children = {row["code"] for row in self.store.children("840120", "nomenclature:test")}
        self.assertEqual(children, {"840120001011", "840120001015", "840120009011"})

    def test_search_finds_a_turkish_suffixed_word(self):
        # "battaniye" araması "Battaniyeler"i bulmalı: Türkçe eklemeli bir dil ve tam
        # sözcük eşleşmesi burada sessizce başarısız olurdu.
        hits = self.store.search("battaniye", "nomenclature:test")
        self.assertTrue(hits)
        self.assertTrue(any(hit["code"].startswith("6301") for hit in hits))

    def test_search_matches_words_from_the_ancestor_path(self):
        # "uranyum" yalnız kodsuz ata satırında geçiyor; yol indekslendiği için
        # yaprak kod bulunabilir olmalı.
        hits = self.store.search("uranyum", "nomenclature:test")
        self.assertTrue(any(hit["code"] == "840120001011" for hit in hits))

    def test_search_can_be_limited_to_a_code_prefix(self):
        hits = self.store.search("cihazlar", "nomenclature:test", code_prefix="6301")
        self.assertEqual(hits, [])

    def test_a_second_snapshot_takes_over_active_without_deleting_the_first(self):
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.store.save_snapshot(
            snapshot_id="nomenclature:next", sha256="b" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/duyurular/y", archive_url="https://ggm.ticaret.gov.tr/z.zip",
        )
        self.assertEqual(self.store.active_snapshot()["id"], "nomenclature:next")
        # Eski anlık görüntü silinmez: geçmiş sorgu ve denetim için durur.
        self.assertIsNotNone(self.store.snapshot_by_sha("a" * 64))
        self.assertIsNotNone(self.store.code_row("840120001011", "nomenclature:test"))

    def test_database_file_is_not_world_readable(self):
        self.assertEqual(Path(self.store.db_path).stat().st_mode & 0o077, 0)


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = nom.NomenclatureEngine(self._tmp.name, http=httpx.AsyncClient())
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.engine.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="a" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/duyurular/x", archive_url="https://ggm.ticaret.gov.tr/z.zip",
            legal_act="Cumhurbaşkanlığı Kararı 10781",
        )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self._tmp.cleanup()

    def test_an_exact_code_returns_its_full_path_and_chapter_note(self):
        result = self.engine.lookup("840120001011")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matched_code, "840120001011")
        self.assertEqual(result.description, "Cihazlar")
        self.assertIn("Uranyum", result.full_path)
        self.assertIn("bu bölüme dahil değildir", result.chapter_note)
        self.assertEqual(result.warnings, [])

    def test_a_code_missing_from_the_schedule_falls_back_to_its_ancestor_and_says_so(self):
        # Tarihsel bir karardan gelen kod cetvelde olmayabilir; sessizce "tanımsız"
        # demek yerine üst pozisyondan cevap verilir ve bu uyarıyla belirtilir.
        result = self.engine.lookup("840120001199")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matched_code, "840120")
        self.assertTrue(any("üst pozisyonundan" in item for item in result.warnings))

    def test_an_unknown_chapter_is_not_found(self):
        self.assertEqual(self.engine.lookup("281820000000").status, "not_found")

    def test_an_invalid_code_is_reported_as_invalid_not_missing(self):
        self.assertEqual(self.engine.lookup("abc").status, "invalid_code")

    def test_children_are_returned_for_tree_navigation(self):
        codes = {child["code"] for child in self.engine.lookup("840120").children}
        self.assertEqual(codes, {"840120001011", "840120001015", "840120009011"})

    def test_describe_many_enriches_a_batch_of_tree_children(self):
        described = self.engine.describe_many(["840120001011", "6301.10.00.00.00", "999999"])
        self.assertIn("840120001011", described)
        self.assertIn("630110000000", described)
        self.assertNotIn("999999", described)

    def test_search_returns_codes_and_no_rate_field(self):
        report = self.engine.search("battaniye")
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["hits"])
        for hit in report["hits"]:
            self.assertNotIn("rate", " ".join(hit).lower())

    def test_an_empty_store_says_unavailable_instead_of_not_found(self):
        with tempfile.TemporaryDirectory() as empty:
            engine = nom.NomenclatureEngine(empty, http=httpx.AsyncClient())
            try:
                self.assertEqual(engine.lookup("840120").status, "unavailable")
                self.assertEqual(engine.search("battaniye")["status"], "unavailable")
                self.assertFalse(engine.status()["ready"])
            finally:
                asyncio.run(engine.close())


class StatutoryRateRailTests(unittest.TestCase):
    """474 sütunu asla oran olarak sunulmaz — bu paketin en kritik değişmezi.

    Ölçüm: 8401.10 için cetvel 15 der, İthalat Rejimi I sayılı liste AB menşe için 0
    der. İkisini karıştırmak gümrük müşavirine yanlış vergi göstermek olurdu.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = nom.NomenclatureEngine(self._tmp.name, http=httpx.AsyncClient())
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.engine.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="a" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/x", archive_url="https://ggm.ticaret.gov.tr/z.zip",
        )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self._tmp.cleanup()

    def test_the_value_is_named_statutory_and_carries_its_warning(self):
        data = self.engine.lookup("840110000000").to_dict()
        self.assertEqual(data["statutory_rate_text"], "15")
        self.assertIn("kanuni azami hadd", data["statutory_rate_note"])
        self.assertIn("maliyet hesabına girmez", data["statutory_rate_note"])

    def test_no_lookup_field_is_named_like_an_applied_duty_rate(self):
        keys = set(self.engine.lookup("840110000000").to_dict())
        for forbidden in ("rate", "customs_duty", "customs_duty_rate", "duty", "vergi_orani"):
            self.assertNotIn(forbidden, keys)

    def test_the_rate_note_is_absent_when_the_column_is_empty(self):
        self.assertEqual(self.engine.lookup("8401").statutory_rate_note, "")

    def test_status_repeats_the_rail_so_the_admin_panel_shows_it(self):
        self.assertIn("kanuni azami hadd", self.engine.status()["statutory_rate_note"])


class SyncTests(unittest.TestCase):
    """Eşitleme: aynı sha yeni kopya üretmez, yarım cetvel eskisini ezmez, SSRF kapalı."""

    LANDING = "https://ggm.ticaret.gov.tr/duyurular/tgtc-2026"
    ARCHIVE = "https://ggm.ticaret.gov.tr/data/abc/2026%20TGTC.zip"
    SOURCES = {
        "source": {
            "id": "tgtc", "index_url": "", "landing_url": LANDING,
            "announcement_match": "tarife cetveli", "legal_act": "Cumhurbaşkanlığı Kararı 10781",
            "gazette_date": "2025-12-30", "valid_from": "2026-01-01",
        }
    }

    def _engine(self, handler, tmp: str, *, min_codes: int = 1) -> nom.NomenclatureEngine:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        return nom.NomenclatureEngine(tmp, http=client, sources=self.SOURCES, min_codes=min_codes)

    def setUp(self) -> None:
        self.archive_bytes = build_archive()
        self.calls: list[str] = []

    def _handler(self, archive: bytes | None = None):
        payload = self.archive_bytes if archive is None else archive

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.calls.append(url)
            if url == self.LANDING:
                html = f'<a href="{self.ARCHIVE}">Excel formatı için tıklayınız</a>'
                return httpx.Response(200, text=html)
            if url == self.ARCHIVE:
                return httpx.Response(200, content=payload)
            return httpx.Response(404)

        return handler

    def test_a_first_sync_stores_the_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(self._handler(), tmp)
            try:
                report = asyncio.run(engine.sync())
                self.assertEqual(report["status"], "updated")
                self.assertGreater(report["code_count"], 0)
                self.assertEqual(report["discovery"], "pinned")
                self.assertTrue(engine.status()["ready"])
            finally:
                asyncio.run(engine.close())

    def test_the_same_checksum_does_not_create_a_second_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(self._handler(), tmp)
            try:
                first = asyncio.run(engine.sync())
                second = asyncio.run(engine.sync())
                self.assertEqual(second["status"], "unchanged")
                self.assertEqual(second["snapshot_id"], first["snapshot_id"])
            finally:
                asyncio.run(engine.close())

    def test_a_short_parse_is_refused_so_a_half_schedule_never_replaces_a_good_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(self._handler(), tmp)
            try:
                asyncio.run(engine.sync())
                good = engine.status()["code_count"]
            finally:
                asyncio.run(engine.close())
            # Kaynağın düzeni bozulmuş gibi davran: aynı depoya yetersiz bir cetvel gelir.
            broken = self._engine(self._handler(build_archive(chapters={"84": CHAPTER_84_ROWS})), tmp, min_codes=10_000)
            try:
                report = asyncio.run(broken.sync())
                self.assertEqual(report["status"], "error")
                self.assertIn("kod ayrıştırıldı", report["error"])
                # Çalışan cetvel yerinde kalır.
                self.assertEqual(broken.status()["code_count"], good)
            finally:
                asyncio.run(broken.close())

    def test_a_redirect_to_another_domain_is_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == self.LANDING:
                return httpx.Response(302, headers={"location": "https://evil.example.com/x.zip"})
            return httpx.Response(200, content=self.archive_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(handler, tmp)
            try:
                with self.assertRaises(SecurityViolation):
                    asyncio.run(engine.discover_archive())
            finally:
                asyncio.run(engine.close())

    def test_a_non_zip_body_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == self.LANDING:
                return httpx.Response(200, text=f'<a href="{self.ARCHIVE}">Excel</a>')
            return httpx.Response(200, content=b"<html>Internal Server Error</html>")

        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(handler, tmp)
            try:
                report = asyncio.run(engine.sync())
                self.assertEqual(report["status"], "error")
                self.assertIn("ZIP", report["error"])
            finally:
                asyncio.run(engine.close())

    def test_the_announcement_index_is_preferred_and_reported(self):
        index = "https://ggm.ticaret.gov.tr/duyurular"
        sources = {"source": dict(self.SOURCES["source"], index_url=index)}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == index:
                return httpx.Response(
                    200,
                    text='<a href="/duyurular/tgtc-2027">TÜRK GÜMRÜK TARİFE CETVELİ YAYIMLANMIŞTIR</a>',
                )
            if url.endswith("/duyurular/tgtc-2027"):
                return httpx.Response(200, text=f'<a href="{self.ARCHIVE}">Excel</a>')
            if url == self.ARCHIVE:
                return httpx.Response(200, content=self.archive_bytes)
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmp:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
            engine = nom.NomenclatureEngine(tmp, http=client, sources=sources, min_codes=1)
            try:
                archive, landing, discovery = asyncio.run(engine.discover_archive())
                self.assertEqual(discovery, "index")
                self.assertTrue(landing.endswith("/duyurular/tgtc-2027"))
                self.assertEqual(archive, self.ARCHIVE)
            finally:
                asyncio.run(engine.close())


if __name__ == "__main__":
    unittest.main()


class TariffTreeDescriptionTests(unittest.TestCase):
    """Ölçülen boşluğun kapandığını kilitler: ağaç çocukları artık tanım taşıyor.

    Canlı ölçüm (20.09.2026): ``POST /api/tariff/tree {"gtip":"847160"}`` çocukları
    ``['ambiguous_measure_types','code','descendant_count','final','level','rate_status',
    'rate_variants','unambiguous_rates','warnings']`` döndürüyordu — ``description``
    **yoktu**. Kullanıcı iki alt kodu ayırt edemiyordu.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = nom.NomenclatureEngine(self._tmp.name, http=httpx.AsyncClient())
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.engine.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="a" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/x", archive_url="https://ggm.ticaret.gov.tr/z.zip",
        )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self._tmp.cleanup()

    def _nodes(self) -> list[Any]:
        from tariff_engine import TariffTreeNode

        return [
            TariffTreeNode(
                code=code, level="GTIP12", final=True, descendant_count=1,
                rate_status="origin_required",
            )
            for code in ("840120001011", "840120009011")
        ]

    def test_children_get_their_official_description_and_full_path(self):
        from tariff_engine import TariffEngine

        engine = TariffEngine.__new__(TariffEngine)
        engine.nomenclature = self.engine
        nodes = self._nodes()
        engine._describe_children(nodes)
        self.assertEqual([node.description for node in nodes], ["Cihazlar", "Cihazlar"])
        # İki dalın tanımı aynı; ayırt eden şey tam yol — asıl kazanç bu.
        self.assertNotEqual(nodes[0].full_path, nodes[1].full_path)
        self.assertIn("Uranyum", nodes[0].full_path)
        self.assertIn("Diğerleri", nodes[1].full_path)

    def test_without_the_engine_the_tree_behaves_exactly_as_before(self):
        from tariff_engine import TariffEngine

        engine = TariffEngine.__new__(TariffEngine)
        engine.nomenclature = None
        nodes = self._nodes()
        engine._describe_children(nodes)
        self.assertEqual([node.description for node in nodes], ["", ""])

    def test_a_failing_nomenclature_engine_does_not_break_the_tree(self):
        from tariff_engine import TariffEngine

        class Broken:
            def describe_many(self, codes):
                raise RuntimeError("depo kapalı")

        engine = TariffEngine.__new__(TariffEngine)
        engine.nomenclature = Broken()
        nodes = self._nodes()
        engine._describe_children(nodes)  # sessizce tanımsız kalır, istisna sızmaz
        self.assertEqual(nodes[0].description, "")


class RouteTests(unittest.TestCase):
    """Rotalar: tanım döner, oran dönmez, yetki gerektirmez."""

    def setUp(self) -> None:
        from starlette.testclient import TestClient

        import app as web_app

        self.web_app = web_app
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = nom.NomenclatureEngine(self._tmp.name, http=httpx.AsyncClient())
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.engine.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="c" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/x", archive_url="https://ggm.ticaret.gov.tr/z.zip",
            legal_act="Cumhurbaşkanlığı Kararı 10781",
        )
        self._original = (web_app.nomenclature_engine, web_app.rate_limiter)
        web_app.nomenclature_engine = self.engine
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")

    def tearDown(self) -> None:
        self.client.close()
        self.web_app.nomenclature_engine, self.web_app.rate_limiter = self._original
        asyncio.run(self.engine.close())
        self._tmp.cleanup()

    def test_lookup_returns_the_description_and_the_statutory_rate_warning(self):
        response = self.client.get("/api/tariff/nomenclature?gtip=8401.10.00.00.00")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "matched")
        self.assertEqual(body["description"], "Nükleer reaktörler")
        self.assertEqual(body["statutory_rate_text"], "15")
        self.assertIn("maliyet hesabına girmez", body["statutory_rate_note"])

    def test_lookup_without_a_code_is_422(self):
        self.assertEqual(self.client.get("/api/tariff/nomenclature").status_code, 422)

    def test_search_returns_candidate_codes(self):
        response = self.client.get("/api/tariff/nomenclature/search?q=battaniye")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(any(hit["code"].startswith("6301") for hit in body["hits"]))

    def test_a_one_character_query_is_refused(self):
        self.assertEqual(self.client.get("/api/tariff/nomenclature/search?q=a").status_code, 422)

    def test_export_carries_the_full_path_and_the_source_credentials(self):
        response = self.client.get("/api/tariff/nomenclature/export")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["sha256"], "c" * 64)
        self.assertEqual(body["legal_act"], "Cumhurbaşkanlığı Kararı 10781")
        leaf = next(row for row in body["rows"] if row["code"] == "840120001011")
        # Yol kodsuz ağaç düğümünden geçer; tüketici tarafta kodlu satırlardan
        # yeniden kurulamaz, bu yüzden taşınmak zorunda.
        self.assertIn("Uranyum", leaf["full_path"])

    def test_export_never_carries_a_rate(self):
        body = self.client.get("/api/tariff/nomenclature/export").json()
        for row in body["rows"]:
            self.assertNotIn("statutory_rate_text", row)
            self.assertNotIn("rate", row)
        self.assertIn("kanuni azami hadd", body["statutory_rate_note"])

    def test_export_of_an_empty_store_is_503_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as empty:
            engine = nom.NomenclatureEngine(empty, http=httpx.AsyncClient())
            self.web_app.nomenclature_engine = engine
            try:
                response = self.client.get("/api/tariff/nomenclature/export")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["status"], "unavailable")
            finally:
                asyncio.run(engine.close())

    def test_status_reports_the_source_credentials(self):
        body = self.client.get("/api/tariff/nomenclature/status").json()
        self.assertTrue(body["ready"])
        self.assertEqual(body["legal_act"], "Cumhurbaşkanlığı Kararı 10781")
        self.assertEqual(body["sha256"], "c" * 64)
        self.assertIn("kanuni azami hadd", body["statutory_rate_note"])


class ClassifierEvidenceTests(unittest.TestCase):
    """Cetvel kanıtı modele **gömme sağlayıcısı olmadan** ulaşmalı.

    Canlı ölçüm (20.09.2026): ``/api/search/hybrid?q=kablosuz kulaklık`` → ``mode:
    lexical``, korpuslar ``{}``. Yani modele giden ``official_evidence`` bloğu üretimde
    boştu ve ``nomenclature_matches`` + ``+10`` puanı besleyecek veri hiç yoktu. Bu
    testler o yolun hibrit indekse bağlı olmadığını kilitler.
    """

    def setUp(self) -> None:
        from customs_advisor import CustomsAdvisor

        self._tmp = tempfile.TemporaryDirectory()
        self.nomenclature = nom.NomenclatureEngine(self._tmp.name, http=httpx.AsyncClient())
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.nomenclature.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="d" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/x", archive_url="https://ggm.ticaret.gov.tr/z.zip",
        )
        self.advisor = CustomsAdvisor()
        self.advisor.nomenclature_engine = self.nomenclature

    def tearDown(self) -> None:
        asyncio.run(self.nomenclature.close())
        self._tmp.cleanup()

    def test_evidence_comes_from_the_schedule_text_with_no_index(self):
        # hybrid_index bağlı değil: kanıt yine gelmeli.
        self.assertIsNone(getattr(self.advisor, "hybrid_index", None))
        entries = self.advisor._nomenclature_evidence("elektrikli battaniye", limit=5)
        self.assertTrue(entries)
        self.assertIn("630110000000", {code for entry in entries for code in entry["gtip_codes"]})

    def test_the_excerpt_is_the_full_ancestor_path_not_the_terse_leaf(self):
        entries = self.advisor._nomenclature_evidence("uranyum izotop cihaz", limit=5)
        self.assertTrue(entries)
        leaf = next(entry for entry in entries if "840120001011" in entry["gtip_codes"])
        # "Cihazlar" tek başına modele hiçbir şey anlatmaz; yol aileyi taşır.
        self.assertIn("İzotopik ayırım", leaf["excerpt"])

    def test_entries_feed_the_deterministic_nomenclature_match_bonus(self):
        from customs_advisor import _nomenclature_matches

        entries = self.advisor._nomenclature_evidence("battaniye", limit=8)
        matched = _nomenclature_matches("630110", entries)
        self.assertTrue(matched, "aday GTİP ön ekiyle eşleşen kanıt bulunmalı")

    def test_entry_ids_share_the_hybrid_id_space_so_they_never_collide(self):
        from customs_advisor import _hybrid_evidence_id

        entries = self.advisor._nomenclature_evidence("battaniye", limit=3)
        for entry in entries:
            self.assertEqual(entry["id"], _hybrid_evidence_id(entry["document_id"]))
            self.assertTrue(entry["document_id"].startswith("tariff:"))

    def test_no_engine_means_no_evidence_and_no_error(self):
        from customs_advisor import CustomsAdvisor

        bare = CustomsAdvisor()
        self.assertEqual(bare._nomenclature_evidence("battaniye", limit=5), [])

    def test_a_failing_engine_does_not_break_classification(self):
        class Broken:
            def search(self, *args, **kwargs):
                raise RuntimeError("depo kapalı")

        self.advisor.nomenclature_engine = Broken()
        self.assertEqual(self.advisor._nomenclature_evidence("battaniye", limit=5), [])

    def test_a_too_short_query_is_not_sent_to_the_index(self):
        self.assertEqual(self.advisor._nomenclature_evidence("ab", limit=5), [])


class HybridCorpusTests(unittest.TestCase):
    """Korpus besleyicisi: metin **tam yol** olmalı, yoksa "Diğerleri" indekslenir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = nom.NomenclatureEngine(self._tmp.name, http=httpx.AsyncClient())
        rows, notes, _ = nom.NomenclatureEngine.parse_archive(build_archive())
        self.engine.store.save_snapshot(
            snapshot_id="nomenclature:test", sha256="e" * 64, rows=rows, notes=notes,
            source_url="https://ggm.ticaret.gov.tr/x", archive_url="https://ggm.ticaret.gov.tr/z.zip",
        )

    def tearDown(self) -> None:
        asyncio.run(self.engine.close())
        self._tmp.cleanup()

    def test_documents_carry_the_full_path_as_text(self):
        import hybrid_corpora

        docs = hybrid_corpora.nomenclature_documents(self.engine)
        self.assertTrue(docs)
        leaf = next(doc for doc in docs if doc["id"] == "tariff:840120001011")
        self.assertIn("İzotopik ayırım", leaf["text"])
        self.assertEqual(leaf["corpus"], hybrid_corpora.CORPUS_TARIFF)
        self.assertEqual(leaf["gtip_codes"], ["840120001011"])

    def test_the_id_space_matches_the_previous_feeder_so_the_index_is_not_rebuilt(self):
        import hybrid_corpora

        docs = hybrid_corpora.nomenclature_documents(self.engine)
        self.assertTrue(all(doc["id"].startswith("tariff:") for doc in docs))

    def test_an_unsynced_engine_yields_nothing_rather_than_raising(self):
        import hybrid_corpora

        with tempfile.TemporaryDirectory() as empty:
            engine = nom.NomenclatureEngine(empty, http=httpx.AsyncClient())
            try:
                self.assertEqual(hybrid_corpora.nomenclature_documents(engine), [])
            finally:
                asyncio.run(engine.close())


class EvidencePackIntegrationTests(unittest.TestCase):
    """Emsal karar ve Resmî Gazete metni kanıt paketinde — ama bağlayıcı olmadan.

    Ölçülen boşluk: iki arşiv de aylardır yalnız API/MCP üzerinden erişilebiliyordu
    (`grep -c ictihat web/app.js` → 0, kanıt paketinde → 0). Ürün bunları hiç
    göstermiyordu.

    Kilitlenen değişmez: ikisi de `binding: false` taşır ve hiçbir oran alanına
    dokunmaz. Emsal bir kararın oran üretmesi, gümrük müşavirine yargı kararını
    yürürlükteki tarife gibi göstermek olurdu.
    """

    def setUp(self) -> None:
        from customs_advisor import CustomsAdvisor

        self.advisor = CustomsAdvisor()

    def test_no_archive_means_no_block_and_no_error(self):
        self.assertIsNone(self.advisor._case_law_block("847130000000", "dizüstü bilgisayar"))
        self.assertIsNone(self.advisor._gazette_block("847130000000", "dizüstü bilgisayar"))

    def test_case_law_block_is_marked_non_binding(self):
        class FakeArchive:
            def lookup(self, code, limit=5):
                return type(
                    "R", (), {
                        "hits": [object()],
                        "as_dict": lambda self: {
                            "gtip": code, "total": 1, "source_note": "emsaldir",
                            "hits": [{"birim": "7. Daire", "esas_no": "2020/1", "karar_no": "2021/2",
                                      "karar_tarihi": "2021-03-04", "binding": False}],
                        },
                    },
                )()

        self.advisor.ictihat_archive = FakeArchive()
        block = self.advisor._case_law_block("847130000000", "dizüstü bilgisayar")
        self.assertIsNotNone(block)
        self.assertIs(block["binding"], False)
        self.assertIs(block["hits"][0]["binding"], False)
        # Blok hiçbir oran alanı taşımaz.
        self.assertNotIn("customs_duty", block)
        self.assertNotIn("rate", " ".join(block))

    def test_case_law_falls_back_to_full_text_when_the_code_finds_nothing(self):
        calls: list[str] = []

        class FakeArchive:
            def lookup(self, code, limit=5):
                calls.append("lookup")
                return type("R", (), {"hits": [], "as_dict": lambda self: {"hits": []}})()

            def search(self, query, limit=5):
                calls.append("search")
                return type(
                    "R", (), {
                        "hits": [object()],
                        "as_dict": lambda self: {"hits": [{"birim": "VDDK"}], "source_note": "x"},
                    },
                )()

        self.advisor.ictihat_archive = FakeArchive()
        block = self.advisor._case_law_block("847130000000", "dizüstü bilgisayar")
        # Arşivdeki kararların üçte biri hiç kod anmıyor; kod ıskalarsa metne düşülür.
        self.assertEqual(calls, ["lookup", "search"])
        self.assertIsNotNone(block)

    def test_a_short_code_skips_the_code_lookup_entirely(self):
        class FakeArchive:
            def lookup(self, code, limit=5):
                raise AssertionError("4 haneden kısa kodla kod sorgusu yapılmamalı")

            def search(self, query, limit=5):
                return type("R", (), {"hits": [], "as_dict": lambda self: {"hits": []}})()

        self.advisor.ictihat_archive = FakeArchive()
        self.assertIsNone(self.advisor._case_law_block("84", "dizüstü bilgisayar"))

    def test_a_failing_archive_never_breaks_the_precheck(self):
        class Broken:
            def lookup(self, *args, **kwargs):
                raise RuntimeError("depo kapalı")

            def search(self, *args, **kwargs):
                raise RuntimeError("depo kapalı")

        self.advisor.ictihat_archive = Broken()
        self.advisor.gazette_archive = Broken()
        self.assertIsNone(self.advisor._case_law_block("847130000000", "x ürünü"))
        self.assertIsNone(self.advisor._gazette_block("847130000000", "x ürünü"))

    def test_gazette_block_is_marked_non_binding(self):
        class FakeArchive:
            def search(self, query, limit=5):
                return type(
                    "R", (), {
                        "as_dict": lambda self: {
                            "hits": [{"title": "İthalat Tebliği", "date": "2026-01-01"}],
                            "source_note": "kanıt metni",
                        },
                    },
                )()

        self.advisor.gazette_archive = FakeArchive()
        block = self.advisor._gazette_block("847130000000", "dizüstü bilgisayar")
        self.assertIs(block["binding"], False)

    def test_an_empty_result_yields_no_section_rather_than_an_empty_one(self):
        class Empty:
            def lookup(self, *args, **kwargs):
                return type("R", (), {"hits": [], "as_dict": lambda self: {"hits": []}})()

            def search(self, *args, **kwargs):
                return type("R", (), {"hits": [], "as_dict": lambda self: {"hits": []}})()

        self.advisor.ictihat_archive = Empty()
        self.advisor.gazette_archive = Empty()
        # Boş sonuç "böyle bir karar yok" demek değil; bölüm hiç basılmaz.
        self.assertIsNone(self.advisor._case_law_block("847130000000", "x ürünü"))
        self.assertIsNone(self.advisor._gazette_block("847130000000", "x ürünü"))


class ArbitrationImageTests(unittest.TestCase):
    """Ayrıştırma turu: resmî tanımlar + görsel, ama görsel metin istemine dökülmeden.

    Kilitlenen iki değişmez:

    * **Görsel ilk tur isteminde yer almaz.** ``model_dump_json`` çıktısı istemin gövdesi
      olduğu için base64 görselin oraya sızması istemi milyonlarca karaktere çıkarırdı.
    * **Ayrıştırma turu resmî eşya tanımını görür.** "8471.60.60 mı 8471.60.70 mi"
      sorusu ancak iki tanım yan yana konursa cevaplanır.
    """

    def test_the_image_never_lands_in_the_first_round_text_prompt(self):
        from customs_advisor import ProductClassificationRequest

        request = ProductClassificationRequest(
            product_description="kablosuz kulaklık, şarj kutulu",
            image_data_url="data:image/png;base64," + ("A" * 5000),
        )
        dumped = request.model_dump_json(indent=2, exclude={"origin_country", "image_data_url"})
        self.assertNotIn("base64", dumped)
        self.assertIn("kablosuz kulaklık", dumped)

    def test_the_request_accepts_no_image_and_stays_backward_compatible(self):
        from customs_advisor import ProductClassificationRequest

        # Görsel alanı olmayan eski gövde hâlâ doğrulanmalı.
        request = ProductClassificationRequest(product_description="pamuklu tişört, örme")
        self.assertEqual(request.image_data_url, "")

    def test_only_a_real_image_data_url_is_attached(self):
        from customs_advisor import ProductClassificationRequest

        # Gönderilen değer bir görsel data URL'si değilse ek içerik parçası kurulmaz;
        # `startswith("data:image/")` kapısı bunu sağlar.
        for value in ("", "https://ornek.test/a.png", "data:text/html;base64,AAA"):
            request = ProductClassificationRequest(
                product_description="pamuklu tişört, örme", image_data_url=value
            )
            self.assertFalse(request.image_data_url.startswith("data:image/"), value)
