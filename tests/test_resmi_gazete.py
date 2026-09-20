"""Resmî Gazete arşivi: fihrist ayrıştırma, mükerrer tarama, metin doğrulama ve arama.

Gerçek ağ erişimi yoktur; yanıtlar ``httpx.MockTransport`` ile üretilir. Fihrist
fikstürleri 19.09.2026'da resmigazete.gov.tr'den alınan **gerçek** sayfalardan kırpılmıştır
(``tests/fixtures/resmi_gazete_index_20251231*.html``, cp1254 kodlu).

Bu dosyanın koruduğu değişmezler:

* **Mükerrer sayı taranmazsa yıllık gümrük rejimi kaçırılır.** Fikstürler bunu ölçülmüş
  gerçekle kanıtlıyor: 31.12.2025 normal fihristindeki belgelerin hiçbiri gümrükle ilgili
  değil, aynı günün 3. mükerrerindeki beş belgenin hepsi ilgili.
* Aynı günün normal sayısı ile mükerrerinin 1 numaralı belgeleri **ayrı** belgelerdir;
  kimlik mükerrer ekini taşımazsa biri diğerini siler.
* Başlık ``>`` ile ``</a>`` arasından okunur; biçim artığı ("text-decoration") başlığa
  karışmaz.
* Metni okunamayan belge künyesiyle saklanır ama **gövdesi saklanmaz ve aranmaz** —
  bozuk bir metni doğru hüküm gibi göstermek en ciddi hata olurdu.
* Aynı belge ikinci kez indirilmez; eksik kalan gün tamamlanmış sayılmaz.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resmi_gazete as rg

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DAY = date(2025, 12, 31)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _run(coro):
    return asyncio.run(coro)


def _html_document(title: str, body: str) -> bytes:
    """Okunabilir bir belge HTML'i (cp1254, kaynağın gerçek kodlaması)."""
    filler = " ".join([body] * 12)
    return (
        "<html><head><title>Resmî Gazete</title></head><body>"
        f"<p>31 Aralık 2025 Resmî Gazete</p><h1>{title}</h1><p>MADDE 1 – {filler}</p>"
        "</body></html>"
    ).encode("cp1254")


class IndexParsingTests(unittest.TestCase):
    """Gerçek fihrist sayfalarından belge satırı, başlık ve sayı numarası çıkarma."""

    def test_normal_index_yields_titles_without_markup_residue(self):
        index = rg.parse_day_index(_fixture("resmi_gazete_index_20251231.html"), DAY)
        self.assertEqual(index.issue, "33124")
        self.assertEqual(index.label, "")
        self.assertEqual(index.suffix, "")
        self.assertTrue(index.entries)
        for entry in index.entries:
            self.assertNotIn("text-decoration", entry.title)
            self.assertNotIn("<", entry.title)
            self.assertNotIn("&nbsp;", entry.title)
            self.assertFalse(entry.title.startswith(("-", "–", "—")))
            self.assertEqual(entry.issue_suffix, "")
        self.assertTrue(
            any("Hong Kong" in entry.title for entry in index.entries),
            [entry.title[:60] for entry in index.entries],
        )

    def test_mukerrer_index_reports_its_order_and_carries_the_suffix(self):
        index = rg.parse_day_index(
            _fixture("resmi_gazete_index_20251231M3.html"), DAY, suffix="M3"
        )
        self.assertEqual(index.issue, "33124")
        self.assertEqual(index.label, "3. Mükerrer")
        self.assertTrue(index.entries)
        self.assertEqual({entry.issue_suffix for entry in index.entries}, {"M3"})
        self.assertEqual(index.entries[0].title,
                         "İthalat Rejimi Kararında Değişiklik Yapılmasına İlişkin Karar (Karar Sayısı: 10790)")
        self.assertTrue(index.entries[0].url.endswith("/eskiler/2025/12/20251231M3-1.pdf"))

    def test_the_customs_regime_lives_in_the_mukerrer_not_the_normal_issue(self):
        """Bu modülün varlık sebebi: ölçülmüş gerçek, tasarımı belirledi."""
        normal = rg.parse_day_index(_fixture("resmi_gazete_index_20251231.html"), DAY)
        mukerrer = rg.parse_day_index(
            _fixture("resmi_gazete_index_20251231M3.html"), DAY, suffix="M3"
        )
        self.assertEqual(rg.select_documents(normal.entries), [])
        selected = rg.select_documents(mukerrer.entries)
        self.assertEqual(len(selected), len(mukerrer.entries))
        self.assertEqual(
            [item.kind for item in selected[:3]],
            ["import_regime", "additional_duty", "tariff_quota"],
        )

    def test_a_document_of_another_day_is_ignored(self):
        payload = b'<html><body><a href="20240101-1.htm">Ithalat Rejimi</a></body></html>'
        self.assertEqual(rg.parse_day_index(payload, DAY).entries, [])

    def test_html_is_preferred_over_pdf_for_the_same_document(self):
        payload = (
            '<html><body>'
            '<a href="20251231-7.pdf" style="x">–– İthalat Rejimi Kararı</a>'
            '<a href="20251231-7.htm" style="x">–– İthalat Rejimi Kararı</a>'
            "</body></html>"
        ).encode("cp1254")
        selected = rg.select_documents(rg.parse_day_index(payload, DAY).entries)
        self.assertEqual([(item.sequence, item.fmt) for item in selected], [(7, "htm")])

    def test_same_sequence_in_normal_and_mukerrer_are_distinct_documents(self):
        payload = (
            '<html><body>'
            '<a href="20251231-1.htm" style="x">–– İthalat Rejimi Kararı</a>'
            '<a href="20251231M3-1.pdf" style="x">–– İthalatta İlave Gümrük Vergisi Kararı</a>'
            "</body></html>"
        ).encode("cp1254")
        selected = rg.select_documents(rg.parse_day_index(payload, DAY).entries)
        self.assertEqual(len(selected), 2)
        self.assertEqual(
            {item.document_id for item in selected},
            {"2025-12-31:1", "2025-12-31M3:1"},
        )


class ClassificationTests(unittest.TestCase):
    def test_customs_families_are_recognised(self):
        cases = {
            "İthalat Rejimi Kararında Değişiklik Yapılmasına İlişkin Karar": "import_regime",
            "İthalatta İlave Gümrük Vergisi Uygulanmasına İlişkin Karar": "additional_duty",
            "İthalat: 2026/1 Sayılı Tebliğ": "import_communique",
            "Ürün Güvenliği ve Denetimi: 2026/9 Sayılı Tebliğ": "product_safety",
            "İthalatta Haksız Rekabetin Önlenmesine İlişkin Tebliğ (Damping)": "anti_dumping",
            "İthalatta Korunma Önlemlerine İlişkin Tebliğ": "safeguard",
            "İthalatta Gözetim Uygulanmasına İlişkin Tebliğ": "surveillance",
            "Bazı Sanayi Ürünlerinin İthalatında Tarife Kontenjanı": "tariff_quota",
            "Özel Tüketim Vergisi Genel Tebliği": "excise",
            "Katma Değer Vergisi Genel Uygulama Tebliği": "vat",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                classified = rg.classify_title(title)
                self.assertIsNotNone(classified, title)
                self.assertEqual(classified[0], expected)

    def test_unrelated_documents_are_dropped(self):
        for title in (
            "Abant İzzet Baysal Üniversitesi Yaz Öğretimi Yönetmeliği",
            "Bazı Kamu Kurum ve Kuruluşlarına Ait Kadrolarda Değişiklik Yapılması",
            "",
        ):
            with self.subTest(title=title):
                self.assertIsNone(rg.classify_title(title))


class TextQualityTests(unittest.TestCase):
    """Bozuk PDF metni sessizce kabul edilmez; ölçülen gerçek örnekle sınanır."""

    def test_garbled_pdf_text_is_unreadable(self):
        # 20251231M3-6.pdf'ten ölçülen gerçek çıktı: gömülü font, ToUnicode yok.
        garbled = "1E4JHIC (J>JFGD6DF " * 30
        quality, note = rg.assess_text(garbled)
        self.assertEqual(quality, "unreadable")
        self.assertIn("Türkçe'ye özgü harf oranı", note)

    def test_empty_text_is_unreadable(self):
        self.assertEqual(rg.assess_text("   ")[0], "unreadable")

    def test_turkish_text_without_anchor_is_suspect(self):
        quality, note = rg.assess_text("Bu sayfa yalnızca bir çerçeve içeriyor " * 10)
        self.assertEqual(quality, "suspect")
        self.assertIn("çapa", note)

    def test_short_gazette_text_is_suspect(self):
        self.assertEqual(rg.assess_text("Resmî Gazete tebliğ madde 1")[0], "suspect")

    def test_real_gazette_text_is_clean(self):
        text = rg.html_to_text(_html_document("TEBLİĞ", "eşyanın gümrük kıymeti beyan edilir"))
        self.assertEqual(rg.assess_text(text)[0], "clean")

    def test_english_extraction_of_a_turkish_document_is_unreadable(self):
        """Türkçe bir belgeden İngilizce metin çıkması da çıkarımın başarısızlığıdır."""
        english = "This regulation shall enter into force on the date of publication " * 10
        self.assertEqual(rg.assess_text(english)[0], "unreadable")

    def test_diacritic_ratio_separates_real_text_from_the_measured_garbled_output(self):
        real = rg.html_to_text(_fixture("resmi_gazete_index_20251231M3.html"))
        self.assertGreater(rg.diacritic_ratio(real), 0.10)
        self.assertEqual(rg.diacritic_ratio("1E4JHIC (J>JFGD6DF"), 0.0)

    def test_a_short_title_is_not_called_unreadable_for_lack_of_diacritics(self):
        """Kısa metinde Türkçe'ye özgü harf hiç geçmeyebilir; "okunamadı" demek yanlış olur."""
        self.assertEqual(rg.assess_text("KARAR madde 1")[0], "suspect")


class DecodeTests(unittest.TestCase):
    def test_cp1254_index_decodes_turkish_letters(self):
        text = rg.decode_html("İthalat Rejimi şğüöç".encode("cp1254"))
        self.assertIn("İthalat Rejimi şğüöç", text)

    def test_entities_are_resolved_in_titles(self):
        self.assertEqual(rg.entry_title(' style="x">––&nbsp;&nbsp; A&amp;B Tebliği</a>'), "A&B Tebliği")

    def test_title_stops_at_the_anchor_close(self):
        title = rg.entry_title(' style="x">–– İthalat Rejimi</a></span><p>Sonraki satır')
        self.assertEqual(title, "İthalat Rejimi")


class ArchiveEngineTests(unittest.TestCase):
    """Motorun ağ davranışı: mükerrer tarama, idempotentlik, tamamlanma ve arama."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.requests: list[str] = []
        self.pages: dict[str, tuple[int, bytes, str]] = {}
        transport = httpx.MockTransport(self._handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self.archive = rg.ResmiGazeteArchive(
            self._tmp.name, http=client, delay_seconds=0.0, floor="2025-12-30"
        )
        self.addCleanup(lambda: _run(self.archive.close()))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path not in self.pages:
            return httpx.Response(404, text="yok")
        status, body, media = self.pages[path]
        return httpx.Response(status, content=body, headers={"content-type": media})

    def _publish(self, name: str, body: bytes, media: str = "text/html") -> None:
        self.pages[f"/eskiler/2025/12/{name}"] = (200, body, media)

    def _publish_plain_mukerrer(self, name: str, order: int) -> None:
        """Gümrükle ilgisi olmayan bir mükerrer sayı (31.12.2025'te M1 ve M2 böyleydi)."""
        body = (
            f"<html><body><p>31 Aralık 2025 Tarihli ve 33124 Sayılı Resmî Gazete - {order}. "
            f'Mükerrer</p><a href="20251231M{order}-1.htm" style="x">'
            "–– 2026 Yılı Merkezi Yönetim Bütçe Kanunu</a></body></html>"
        ).encode("cp1254")
        self._publish(name, body)

    def _standard_day(self) -> None:
        self._publish("20251231.htm", _fixture("resmi_gazete_index_20251231.html"))
        # Mükerrer numaraları gün içinde sıralı verilir; gümrük belgeleri M3'tedir.
        self._publish_plain_mukerrer("20251231M1.htm", 1)
        self._publish_plain_mukerrer("20251231M2.htm", 2)
        self._publish("20251231M3.htm", _fixture("resmi_gazete_index_20251231M3.html"))
        for sequence in range(1, 6):
            self._publish(
                f"20251231M3-{sequence}.pdf",
                _html_document("KARAR", "ithalat rejimi kararı gümrük vergisi oranı"),
            )

    def test_mukerrer_issues_are_scanned_after_the_normal_one(self):
        self._standard_day()
        report = _run(self.archive.ingest_day(DAY))
        self.assertTrue(report["published"])
        self.assertEqual(report["issue"], "33124")
        self.assertEqual(report["issues"], 4)
        self.assertEqual(report["extra_issues"], ["1. Mükerrer", "2. Mükerrer", "3. Mükerrer"])
        self.assertEqual(report["stored"], 5)
        self.assertIn("/eskiler/2025/12/20251231M3.htm", self.requests)
        # M3 bulunduğu için M4 de yoklanır, M4 yoksa orada durulur (M5 istenmez).
        self.assertIn("/eskiler/2025/12/20251231M4.htm", self.requests)
        self.assertNotIn("/eskiler/2025/12/20251231M5.htm", self.requests)

    def test_scanning_stops_at_the_first_missing_mukerrer(self):
        self._publish("20251231.htm", _fixture("resmi_gazete_index_20251231.html"))
        _run(self.archive.ingest_day(DAY))
        self.assertIn("/eskiler/2025/12/20251231M1.htm", self.requests)
        self.assertNotIn("/eskiler/2025/12/20251231M2.htm", self.requests)

    def test_an_unpublished_day_is_recorded_once_and_not_retried(self):
        report = _run(self.archive.ingest_day(DAY))
        self.assertFalse(report["published"])
        self.assertEqual(report["listed"], 0)
        self.assertNotIn(DAY, self.archive.pending_days(5, today=DAY))

    def test_a_document_is_never_downloaded_twice(self):
        self._standard_day()
        _run(self.archive.ingest_day(DAY))
        first = self.requests.count("/eskiler/2025/12/20251231M3-1.pdf")
        second = _run(self.archive.ingest_day(DAY))
        self.assertEqual(self.requests.count("/eskiler/2025/12/20251231M3-1.pdf"), first)
        self.assertEqual(second["stored"], 0)
        self.assertEqual(second["skipped"], 5)

    def test_a_day_with_a_failed_document_stays_pending_and_recovers(self):
        self._standard_day()
        self.pages["/eskiler/2025/12/20251231M3-2.pdf"] = (500, b"hata", "text/html")
        report = _run(self.archive.ingest_day(DAY))
        self.assertEqual(report["stored"], 4)
        self.assertIn(DAY, self.archive.pending_days(5, today=DAY))
        # Kaynak düzelince aynı gün tamamlanır ve zaten alınan belgeler yeniden inmez.
        self._publish("20251231M3-2.pdf", _html_document("KARAR", "ilave gümrük vergisi oranı"))
        again = _run(self.archive.ingest_day(DAY))
        self.assertEqual((again["stored"], again["skipped"]), (1, 4))
        self.assertNotIn(DAY, self.archive.pending_days(5, today=DAY))

    def test_garbled_document_is_cited_but_not_searchable(self):
        self._publish("20251231.htm", _fixture("resmi_gazete_index_20251231.html"))
        self._publish("20251231M1.htm", _fixture("resmi_gazete_index_20251231M3.html"))
        for sequence in range(1, 6):
            # Fihrist M3 eki taşıyan bağlantılar veriyor; kimlik bağlantıdan okunur.
            self._publish(
                f"20251231M3-{sequence}.pdf",
                ("1E4JHIC (J>JFGD6DF " * 40).encode("cp1254"),
            )
        _run(self.archive.ingest_day(DAY))
        status = self.archive.status()
        self.assertEqual(status["unreadable"], 5)
        self.assertEqual(status["clean"], 0)
        # Künye aranabilir (başlık), gövde değil.
        by_title = self.archive.search("İthalat Rejimi")
        self.assertTrue(by_title.hits)
        self.assertTrue(any("okunamadı" in warning for warning in by_title.warnings))
        for hit in by_title.hits:
            self.assertEqual(hit.text_quality, "unreadable")
        self.assertEqual(self.archive.search("JFGD6DF").hits, [])

    def test_full_text_search_filters_by_date_and_kind(self):
        self._standard_day()
        _run(self.archive.ingest_day(DAY))
        hit = self.archive.search("gümrük vergisi").hits[0]
        self.assertEqual(hit.date, "2025-12-31")
        self.assertEqual(hit.issue, "33124")
        self.assertEqual(hit.issue_suffix, "M3")
        self.assertEqual(hit.as_dict()["issue_label"], "3. Mükerrer")
        self.assertEqual(len(hit.sha256), 64)
        self.assertTrue(hit.url.startswith("https://www.resmigazete.gov.tr/"))
        self.assertTrue(self.archive.search("gümrük", kind="import_regime").hits)
        self.assertEqual(self.archive.search("gümrük", kind="anti_dumping").hits, [])
        self.assertEqual(self.archive.search("gümrük", since="2026-01-01").hits, [])
        self.assertTrue(self.archive.search("gümrük", until="2026-01-01").hits)

    def test_search_without_query_returns_the_newest_documents(self):
        self._standard_day()
        _run(self.archive.ingest_day(DAY))
        result = self.archive.search("", limit=3)
        self.assertEqual(len(result.hits), 3)
        self.assertEqual(result.total, 5)
        self.assertIn("seçicidir", result.as_dict()["source_note"])

    def test_backfill_walks_backwards_from_today_and_reports(self):
        self._standard_day()
        report = _run(self.archive.backfill(limit=2, today=DAY))
        self.assertEqual(report["processed_days"], 2)
        self.assertEqual(report["published_days"], 1)
        self.assertEqual(report["stored_documents"], 5)
        self.assertEqual([item["date"] for item in report["days"]], ["2025-12-31", "2025-12-30"])

    def test_pending_days_stops_at_the_configured_floor(self):
        days = self.archive.pending_days(50, today=DAY)
        self.assertEqual([day.isoformat() for day in days], ["2025-12-31", "2025-12-30"])

    def test_status_reports_scope_without_any_ingest(self):
        status = self.archive.status()
        self.assertEqual(status["documents"], 0)
        self.assertEqual(status["floor"], "2025-12-30")
        self.assertTrue(status["has_pending_days"])
        self.assertEqual(
            {item["kind"] for item in status["interests"]},
            {key for key, _, _ in rg.INTEREST_RULES},
        )

    def test_outbound_requests_stay_on_the_official_host(self):
        with self.assertRaises(Exception) as caught:
            _run(self.archive._fetch("https://example.com/eskiler/x.htm", limit=1024))
        self.assertNotIn("example.com", self.requests)
        self.assertTrue(caught.exception)

    def test_a_redirect_to_another_host_is_refused(self):
        self.pages["/eskiler/2025/12/20251231.htm"] = (200, b"", "text/html")
        self.pages["/eskiler/2025/12/20251231.htm"] = (200, b"x", "text/html")

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request.url.path)
            return httpx.Response(302, headers={"location": "https://evil.example/doc.htm"})

        archive = rg.ResmiGazeteArchive(
            self._tmp.name,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
            delay_seconds=0.0,
        )
        self.addCleanup(lambda: _run(archive.close()))
        with self.assertRaises(Exception):
            _run(archive._fetch(rg.day_index_url(DAY), limit=1024))


class UrlTests(unittest.TestCase):
    def test_index_urls(self):
        self.assertEqual(
            rg.day_index_url(DAY, base_url="https://www.resmigazete.gov.tr"),
            "https://www.resmigazete.gov.tr/eskiler/2025/12/20251231.htm",
        )
        self.assertEqual(
            rg.day_index_url(DAY, suffix="M3", base_url="https://www.resmigazete.gov.tr"),
            "https://www.resmigazete.gov.tr/eskiler/2025/12/20251231M3.htm",
        )


if __name__ == "__main__":
    unittest.main()
