"""Danıştay gümrük içtihadı arşivi: ayrıştırma, GTİP çıkarımı, süzme, tekilleştirme, sorgu.

Gerçek ağ erişimi yoktur; yanıtlar ``httpx.MockTransport`` ile üretilir. Fikstürler
20.09.2026'da Bedesten'den alınan **gerçek** yanıtlardan kırpılmıştır:

* ``tests/fixtures/ictihat_search.json`` — arama yanıtı, üç satır, **olduğu gibi**
  (md5 ``f0b79fec5e44f8d41d679c620e7b61b2``),
* ``tests/fixtures/ictihat_content.json`` — karar içeriği; zarf biçimi gerçek yanıttan,
  gövde Danıştay 7. Daire E.1998/3138 K.1999/1046 kararının **gerçek metninin** ilk 1500
  karakteri (kısaltılmıştır).

Bu dosyanın koruduğu değişmezler — hepsi gerçek veride bulunmuş kusurlardan doğdu:

* **Karar tarihi bir gün geri kaymaz.** Kaynak ``kararTarihi`` alanını UTC'ye kaydırmış
  damgayla veriyor (``1999-03-10T22:00:00+00:00``) ama aynı satırdaki ``kararTarihiStr``
  ``11.03.1999`` diyor. İlk on karakteri almak her kararı bir gün geri alırdı.
* **``kesinlesmeDurumu`` kesinleşme durumu DEĞİLDİR.** İçinde konu anahtar kelimeleri var
  ya da düz ``"null"`` metni. Bir gümrük müşavirine kesinleşmemiş kararı "kesinleşmiş"
  diye sunmak ciddi bir yanlış olurdu.
* **Aynı kod iki biçimde yazılabiliyor** (``8471.60.90. 00.19`` ve ``8471.60.90.0019``);
  kanonikleştirme tek koda indirir.
* **Seri kararlar birebir yineleniyor**; ikinci kopya aramada görünmez.
* **Karar emsaldir, bağlayıcı değildir**: hiçbir sonuç oran, GTİP tespiti veya belge şartı
  taşımaz ve ``binding`` alanı her zaman ``False``'dur.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ictihat as ic

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _run(coro):
    return asyncio.run(coro)


def _decision_html(*, chamber: str = "7. Daire", codes: str = "8471.60.90.00.19") -> str:
    """Okunabilir, gümrükle ilgili sahte bir karar metni."""
    body = (
        f"T.C. DANIŞTAY {chamber} İstemin Özeti: Yükümlü şirket adına tescilli beyanname ile "
        f"{codes} tarife ve istatistik pozisyonunda beyan edilen eşyanın gümrük kıymeti ve "
        "ithalat vergileri yönünden yapılan ek tahakkuka vaki itirazın reddi yolundaki işlem "
        "incelenmiştir. Menşe şahadetnamesi ve gümrük beyannamesi birlikte değerlendirilmiştir. "
    ) * 3
    return f"<html><body><p>{body}</p></body></html>"


def _content_envelope(html: str) -> dict:
    import base64

    return {
        "data": {
            "content": base64.b64encode(html.encode("utf-8")).decode("ascii"),
            "mimeType": "text/html",
        },
        "metadata": {"FMTY": "SUCCESS"},
    }


def _search_envelope(rows: list[dict], total: int = 12345) -> dict:
    return {"data": {"emsalKararList": rows, "total": total, "start": 0},
            "metadata": {"FMTY": "SUCCESS"}}


def _row(document_id: str, *, tarih_str: str = "11.03.1999", chamber: str = "7. Daire",
         konular: str | None = None) -> dict:
    return {
        "documentId": document_id,
        "itemType": {"name": "DANISTAYKARAR", "description": "Danıştay Kararı"},
        "birimId": None,
        "birimAdi": chamber,
        "kararTuru": None,
        "kararTarihi": "1999-03-10T22:00:00.000+00:00",
        "kararTarihiStr": tarih_str,
        "kesinlesmeDurumu": konular if konular is not None else "null",
        "kararNo": "1999/1046",
        "esasNo": "1998/3138",
    }


class SearchParsingTests(unittest.TestCase):
    """Gerçek arama yanıtından künye çıkarma ve alan adlarının anlamı."""

    def setUp(self):
        self.refs = ic.parse_search_response(_fixture("ictihat_search.json"))

    def test_three_customs_chamber_decisions_are_read(self):
        self.assertEqual(len(self.refs), 3)
        self.assertEqual({ref.birim for ref in self.refs}, {"7. Daire"})
        self.assertEqual({ref.item_type for ref in self.refs}, {"DANISTAYKARAR"})
        self.assertEqual(self.refs[0].esas_no, "1998/3138")
        self.assertEqual(self.refs[0].karar_no, "1999/1046")

    def test_decision_date_is_the_local_authoritative_one(self):
        """Gerileme kilidi: damga UTC'ye kaydırılmış, yerel biçim yetkili."""
        self.assertEqual(self.refs[0].karar_tarihi, "1999-03-11")
        self.assertEqual(self.refs[1].karar_tarihi, "1996-10-15")
        self.assertEqual(self.refs[2].karar_tarihi, "2025-02-27")

    def test_the_misnamed_field_is_read_as_subject_keywords(self):
        self.assertEqual(
            self.refs[0].konular,
            ["BİLİRKİŞİ RAPORU", "GÜMRÜK TARFE İSTATİSTİK POZİSYONU", "GÜMRÜK VERGİSİ"],
        )

    def test_the_literal_null_string_never_reaches_the_result(self):
        self.assertEqual(self.refs[1].konular, [])
        for ref in self.refs:
            self.assertNotIn("null", [item.lower() for item in ref.konular])
            self.assertNotEqual(ref.karar_turu.lower(), "none")

    def test_public_url_points_at_the_official_viewer(self):
        self.assertEqual(
            self.refs[0].public_url, "https://mevzuat.adalet.gov.tr/ictihat/17411500"
        )

    def test_a_failed_response_raises_even_with_http_200(self):
        with self.assertRaises(ic.IctihatError):
            ic.parse_search_response({"metadata": {"FMTY": "ERROR", "FMTE": "hata"}})

    def test_rows_without_a_document_id_are_dropped(self):
        self.assertEqual(ic.parse_search_response(_search_envelope([{"birimAdi": "7. Daire"}])), [])


class ContentParsingTests(unittest.TestCase):
    """Gerçek karar metninden düz metin ve GTİP çıkarımı."""

    def setUp(self):
        self.text = ic.parse_content_response(_fixture("ictihat_content.json"))

    def test_real_decision_text_is_clean(self):
        self.assertGreater(len(self.text), 800)
        # Kaynak mahkeme adını harf aralı yazıyor ("D A N I Ş T A Y"); daire adı düz geçer.
        self.assertIn("YEDİNCİ", self.text)
        self.assertIn("7. Daire", self.text)
        self.assertEqual(ic.assess_text(self.text)[0], "clean")
        self.assertGreater(ic.diacritic_ratio(self.text), 0.05)

    def test_codes_written_two_ways_collapse_into_one(self):
        """Gerçek metinde kod hem ``8471.60.90. 00.19`` hem ``8471.60.90.0019`` biçiminde."""
        codes = ic.extract_gtip_codes(self.text)
        self.assertEqual(codes, ["847160900019", "852830100000"])

    def test_a_failed_content_response_raises(self):
        with self.assertRaises(ic.IctihatError):
            ic.parse_content_response({"metadata": {"FMTY": "ERROR", "FMTE": "yok"}})


class GtipExtractionTests(unittest.TestCase):
    def test_space_split_code_is_normalised(self):
        self.assertEqual(ic.normalise_gtip("8471.60.90. 00.19"), "847160900019")

    def test_invalid_chapters_are_refused(self):
        for raw in ("9812.34", "7712.34", "0012.34"):
            with self.subTest(raw=raw):
                self.assertEqual(ic.normalise_gtip(raw), "")

    def test_a_real_heading_that_looks_like_a_year_is_kept(self):
        """2009 hem yıl hem meyve suyu pozisyonu; yıl sanıp atmak gerçek kodu kaybettirirdi."""
        self.assertEqual(ic.normalise_gtip("2009.11"), "200911")

    def test_partial_lengths_are_refused(self):
        for raw in ("847.60", "84716.09", "8471609"):
            with self.subTest(raw=raw):
                self.assertEqual(ic.normalise_gtip(raw), "")

    def test_prefixes_cover_every_query_width(self):
        self.assertEqual(
            ic.gtip_prefixes("847160900019"),
            ["8471", "847160", "84716090", "8471609000", "847160900019"],
        )

    def test_extraction_is_sorted_and_unique(self):
        text = "8701.90.11, 8701.90.11 ve 8701.93.90.00.00 pozisyonları"
        self.assertEqual(ic.extract_gtip_codes(text), ["87019011", "870193900000"])


class RelevanceTests(unittest.TestCase):
    """Süzme bizim tarafta: kaynağın sayacı kelimeleri VEYA'lıyor, güvenilmez."""

    def test_customs_chamber_passes_without_reading_the_text(self):
        self.assertTrue(ic.assess_relevance("", "7. Daire")[0])
        self.assertTrue(ic.assess_relevance("", "Vergi Dava Daireleri Kurulu")[0])

    def test_another_chamber_passes_only_on_customs_wording(self):
        self.assertTrue(ic.assess_relevance("gümrük beyanname tarife ithalat", "3. Daire")[0])
        relevant, reason = ic.assess_relevance("imar planının iptali istemi", "6. Daire")
        self.assertFalse(relevant)
        self.assertIn("gümrük", reason.lower())

    def test_subject_keywords_count_towards_relevance(self):
        relevant, _ = ic.assess_relevance(
            "dava konusu işlem", "6. Daire",
            ["GÜMRÜK VERGİSİ", "TARİFE POZİSYONU", "İTHALAT"],
        )
        self.assertTrue(relevant)


class TextQualityTests(unittest.TestCase):
    def test_garbled_text_is_unreadable(self):
        quality, note = ic.assess_text("1E4JHIC (J>JFGD6DF " * 40)
        self.assertEqual(quality, "unreadable")
        self.assertIn("Türkçe'ye özgü harf oranı", note)

    def test_short_text_is_suspect(self):
        self.assertEqual(ic.assess_text("Danıştay kararı özeti")[0], "suspect")

    def test_empty_text_is_unreadable(self):
        self.assertEqual(ic.assess_text("   ")[0], "unreadable")


class ArchiveEngineTests(unittest.TestCase):
    """Motorun ağ davranışı: pencere taraması, süzme, tekilleştirme, idempotentlik, sorgu."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[tuple[str, dict]] = []
        self.search_rows: list[dict] = [_row("1001", konular="GÜMRÜK VERGİSİ")]
        self.contents: dict[str, dict] = {}
        self.failing: set[str] = set()
        transport = httpx.MockTransport(self._handler)
        client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self.archive = ic.IctihatArchive(
            self._tmp.name, http=client, delay_seconds=0.0,
            floor="2024-01-01", window_days=365, phrases=("gümrük tarife istatistik pozisyonu",),
        )
        self.addCleanup(lambda: _run(self.archive.close()))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inner = body.get("data") or {}
        self.calls.append((request.url.path, inner))
        if request.url.path.endswith("searchDocuments"):
            page = int(inner.get("pageNumber") or 1)
            rows = self.search_rows if page == 1 else []
            return httpx.Response(200, json=_search_envelope(rows))
        document_id = str(inner.get("documentId"))
        if document_id in self.failing:
            return httpx.Response(500, json={"metadata": {"FMTY": "ERROR", "FMTE": "arıza"}})
        html = self.contents.get(document_id, _decision_html())
        return httpx.Response(200, json=_content_envelope(html))

    # -------------------------------------------------- pencere ve dolum
    def test_a_window_is_scanned_with_a_bounded_date_range(self):
        report = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31),
                                                 "gümrük tarife istatistik pozisyonu"))
        self.assertEqual((report["listed"], report["stored"]), (1, 1))
        self.assertTrue(report["complete"])
        search_call = next(inner for path, inner in self.calls if path.endswith("searchDocuments"))
        self.assertEqual(search_call["kararTarihiStart"], "2024-01-01T00:00:00.000Z")
        self.assertEqual(search_call["kararTarihiEnd"], "2024-12-31T23:59:59.000Z")
        self.assertEqual(search_call["itemTypeList"], ["DANISTAYKARAR"])

    def test_pending_windows_walk_backwards_and_stop_at_the_floor(self):
        windows = self.archive.pending_windows(10, today=date(2024, 12, 31))
        self.assertTrue(windows)
        self.assertEqual(windows[0][1], date(2024, 12, 31))
        for start, end, _ in windows:
            self.assertGreaterEqual(start, date(2024, 1, 1))
            self.assertLessEqual(start, end)

    def test_each_round_moves_to_a_new_window_never_repeating_one(self):
        def ranges() -> list[tuple[str, str]]:
            return [
                (inner["kararTarihiStart"], inner["kararTarihiEnd"])
                for path, inner in self.calls
                if path.endswith("searchDocuments")
            ]

        _run(self.archive.backfill(limit=1, today=date(2024, 12, 31)))
        first = ranges()
        _run(self.archive.backfill(limit=1, today=date(2024, 12, 31)))
        second = ranges()[len(first):]
        self.assertTrue(first and second)
        self.assertFalse(set(first) & set(second), "aynı pencere iki kez tarandı")

    def test_a_decision_is_never_downloaded_twice(self):
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        fetches = sum(1 for path, _ in self.calls if path.endswith("getDocumentContent"))
        second = _run(self.archive.ingest_window(date(2023, 1, 1), date(2023, 12, 31), "x"))
        self.assertEqual(
            sum(1 for path, _ in self.calls if path.endswith("getDocumentContent")), fetches
        )
        self.assertEqual((second["stored"], second["skipped"]), (0, 1))

    def test_a_window_with_a_failed_decision_stays_pending_and_recovers(self):
        self.failing.add("1001")
        report = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertEqual(report["stored"], 0)
        self.assertFalse(report["complete"])
        self.assertIn(("2024-01-01", "2024-12-31", "x"), {
            (s, e, p) for s, e, p in [("2024-01-01", "2024-12-31", "x")]
        })
        self.assertNotIn(
            ("2024-01-01", "2024-12-31", "x"), self.archive.scanned_windows()
        )
        self.failing.clear()
        again = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertEqual(again["stored"], 1)
        self.assertIn(("2024-01-01", "2024-12-31", "x"), self.archive.scanned_windows())

    # -------------------------------------------------- süzme ve tekilleştirme
    def test_an_unrelated_decision_is_not_stored_at_all(self):
        self.search_rows = [_row("2001", chamber="6. Daire")]
        self.contents["2001"] = (
            "<html><body><p>" + "İmar planının iptali istemiyle açılan dava. " * 20
            + "</p></body></html>"
        )
        report = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertEqual((report["stored"], report["skipped"]), (0, 1))
        self.assertEqual(self.archive.status()["decisions"], 0)

    def test_identical_serial_decisions_appear_once(self):
        self.search_rows = [_row("3001"), _row("3002"), _row("3003")]
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        status = self.archive.status()
        self.assertEqual(status["decisions"], 3)
        self.assertEqual(status["duplicates"], 2)
        self.assertEqual(status["unique_decisions"], 1)
        self.assertEqual(len(self.archive.search("gümrük").hits), 1)
        self.assertEqual(len(self.archive.lookup("847160900019").hits), 1)

    def test_a_garbled_decision_is_stored_without_a_body_and_without_codes(self):
        self.search_rows = [_row("4001")]
        self.contents["4001"] = "<html><body>" + ("1E4JHIC (J>JFGD6DF " * 60) + "</body></html>"
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        status = self.archive.status()
        self.assertEqual(status["unreadable"], 1)
        self.assertEqual(status["with_gtip"], 0)
        self.assertEqual(self.archive.lookup("847160900019").hits, [])

    # -------------------------------------------------- sorgu
    def test_lookup_prefers_the_most_specific_code_match(self):
        self.search_rows = [_row("5001"), _row("5002")]
        self.contents["5001"] = _decision_html(codes="8471.60.90.00.19")
        self.contents["5002"] = _decision_html(codes="8471.30.00.00.00")
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        result = self.archive.lookup("847160900019")
        self.assertTrue(result.hits)
        best = result.hits[0]
        self.assertEqual(best.document_id, "5001")
        self.assertEqual(best.matched_gtip, "847160900019")
        self.assertEqual(best.match_width, 12)
        # 4 haneli sorgu iki kararı da yakalar, ama yalnız fasıl düzeyinde.
        wide = self.archive.lookup("8471", limit=5)
        self.assertEqual({hit.document_id for hit in wide.hits}, {"5001", "5002"})

    def test_lookup_refuses_a_too_short_code(self):
        result = self.archive.lookup("84")
        self.assertEqual(result.hits, [])
        self.assertTrue(any("4 haneli" in warning for warning in result.warnings))

    def test_a_miss_says_the_archive_is_selective_not_that_no_decision_exists(self):
        result = self.archive.lookup("610910000011")
        self.assertEqual(result.hits, [])
        joined = " ".join(result.warnings)
        self.assertIn("seçicidir", joined)
        self.assertIn("üçte birinde", joined)

    def test_every_result_is_marked_non_binding(self):
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        for result in (self.archive.lookup("847160900019"), self.archive.search("gümrük")):
            self.assertTrue(result.hits)
            for hit in result.hits:
                payload = hit.as_dict()
                self.assertFalse(payload["binding"])
                self.assertIn("emsaldir", payload["note"])
                self.assertNotIn("kesinlesme_durumu", payload)

    def test_an_old_decision_is_flagged_as_dated(self):
        self.search_rows = [_row("6001", tarih_str="11.03.1999")]
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        result = self.archive.lookup("847160900019")
        self.assertTrue(result.hits[0].dated)
        self.assertTrue(any("mevzuat değişmiş" in warning for warning in result.warnings))

    def test_full_text_search_filters_by_date(self):
        self.search_rows = [_row("7001", tarih_str="15.06.2024")]
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertTrue(self.archive.search("kıymet", since="2024-01-01").hits)
        self.assertEqual(self.archive.search("kıymet", since="2025-01-01").hits, [])
        self.assertTrue(self.archive.search("kıymet", until="2024-12-31").hits)

    def test_search_carries_the_citation(self):
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        hit = self.archive.search("beyanname").hits[0]
        self.assertEqual(len(hit.sha256), 64)
        self.assertTrue(hit.url.startswith("https://mevzuat.adalet.gov.tr/ictihat/"))
        self.assertEqual(hit.birim, "7. Daire")
        self.assertTrue(hit.retrieved_at)

    # -------------------------------------------------- durum ve güvenlik
    def test_status_never_reports_the_unreliable_source_total(self):
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        status = self.archive.status()
        self.assertEqual(status["decisions"], 1)
        self.assertEqual(status["with_gtip"], 1)
        self.assertEqual(status["gtip_coverage_pct"], 100.0)
        self.assertNotIn(12345, status.values())
        self.assertIn("güvenilmez", status["note"])

    def test_status_works_on_an_empty_archive(self):
        status = self.archive.status()
        self.assertEqual(status["decisions"], 0)
        self.assertEqual(status["gtip_coverage_pct"], 0.0)
        self.assertTrue(status["has_pending_windows"])
        self.assertEqual(status["floor"], "2024-01-01")

    def test_requests_stay_on_the_official_host(self):
        archive = ic.IctihatArchive(
            self._tmp.name,
            http=httpx.AsyncClient(transport=httpx.MockTransport(self._handler)),
            base_url="https://evil.example",
            delay_seconds=0.0,
        )
        self.addCleanup(lambda: _run(archive.close()))
        with self.assertRaises(Exception):
            _run(archive._post("/emsal-karar/searchDocuments", {}))
        self.assertFalse(any("evil" in path for path, _ in self.calls))

    def test_summary_lines_describe_the_decision(self):
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        lines = ic.summary_lines(self.archive.lookup("847160900019"))
        self.assertTrue(lines)
        self.assertIn("7. Daire", lines[0])
        self.assertIn("E. 1998/3138", lines[0])


class RateLimitTests(unittest.TestCase):
    """429: kaynağın "dur" demesi tek bir kararın hatası gibi ele alınamaz.

    Canlıda ölçülen kusur: istek arası bekleme 0,5 saniyeyken 9 pencerenin 2'si
    ``429 Too Many Requests`` yüzünden eksik kaldı ve motor sıradaki kararı denemeye devam
    ederek ısrar etti. Bu sınıf düzeltmeyi kilitler.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[tuple[str, dict]] = []
        self.rows = [_row("9001"), _row("9002"), _row("9003")]
        self.rate_limited: set[str] = set()
        self.countdown: dict[str, int] = {}
        self.retry_after: str | None = None
        self.slept: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            inner = json.loads(request.content).get("data") or {}
            self.calls.append((request.url.path, inner))
            if request.url.path.endswith("searchDocuments"):
                rows = self.rows if int(inner.get("pageNumber") or 1) == 1 else []
                return httpx.Response(200, json=_search_envelope(rows))
            document_id = str(inner.get("documentId"))
            if document_id in self.rate_limited:
                left = self.countdown.get(document_id)
                if left is None or left > 0:
                    if left is not None:
                        self.countdown[document_id] = left - 1
                    headers = {"retry-after": self.retry_after} if self.retry_after else {}
                    return httpx.Response(429, json={}, headers=headers)
            return httpx.Response(200, json=_content_envelope(_decision_html()))

        self.archive = ic.IctihatArchive(
            self._tmp.name,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
            delay_seconds=0.0, floor="2020-01-01", window_days=365,
            phrases=("gümrük tarife istatistik pozisyonu",),
            rate_limit_backoff=(7.0, 11.0),
        )
        self.addCleanup(lambda: _run(self.archive.close()))

        async def fake_sleep(seconds: float) -> None:
            self.slept.append(seconds)

        patcher = unittest.mock.patch.object(ic.asyncio, "sleep", fake_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetch_count(self, document_id: str) -> int:
        return sum(
            1 for path, inner in self.calls
            if path.endswith("getDocumentContent") and str(inner.get("documentId")) == document_id
        )

    def test_a_transient_429_is_retried_with_increasing_backoff(self):
        self.rate_limited.add("9001")
        self.countdown["9001"] = 1  # bir kez 429, sonra başarılı
        report = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertEqual(report["stored"], 3)
        self.assertTrue(report["complete"])
        self.assertFalse(report["rate_limited"])
        self.assertIn(7.0, self.slept)

    def test_the_retry_after_header_is_obeyed_when_longer(self):
        self.rate_limited.add("9001")
        self.countdown["9001"] = 1
        self.retry_after = "30"
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertIn(30.0, self.slept)

    def test_an_absurd_retry_after_is_capped(self):
        self.rate_limited.add("9001")
        self.countdown["9001"] = 1
        self.retry_after = "99999"
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertIn(ic._MAX_RETRY_AFTER_SECONDS, self.slept)
        self.assertNotIn(99999.0, self.slept)

    def test_an_http_date_retry_after_falls_back_to_the_schedule(self):
        """``Retry-After`` saniye yerine HTTP-tarih de olabilir; o biçim ayrıştırılmıyor
        ve kendi takvimimize düşülüyor — güvenli taraf, çünkü takvim zaten bekliyor."""
        self.rate_limited.add("9001")
        self.countdown["9001"] = 1
        self.retry_after = "Wed, 21 Oct 2026 07:28:00 GMT"
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertIn(7.0, self.slept)

    def test_a_persistent_429_stops_the_window_instead_of_insisting(self):
        """Asıl düzeltme: sıradaki kararı denemek ısrar etmektir."""
        self.rate_limited.add("9001")  # geri sayım yok → hep 429
        report = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertTrue(report["rate_limited"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["stored"], 0)
        self.assertEqual(self._fetch_count("9002"), 0)
        self.assertEqual(self._fetch_count("9003"), 0)
        # 9001 yalnız geri çekilme takvimi kadar denendi (1 ilk istek + 2 yeniden deneme).
        self.assertEqual(self._fetch_count("9001"), 3)

    def test_the_window_stays_pending_and_recovers_later(self):
        self.rate_limited.add("9001")
        _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertNotIn(("2024-01-01", "2024-12-31", "x"), self.archive.scanned_windows())
        self.assertEqual(self.archive.status()["rate_limited_windows"], 1)
        self.rate_limited.clear()
        again = _run(self.archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertEqual(again["stored"], 3)
        self.assertTrue(again["complete"])
        self.assertIn(("2024-01-01", "2024-12-31", "x"), self.archive.scanned_windows())
        self.assertEqual(self.archive.status()["rate_limited_windows"], 0)

    def test_a_rate_limited_round_does_not_start_another_window(self):
        self.rate_limited.add("9001")
        report = _run(self.archive.backfill(limit=5, today=date(2024, 12, 31)))
        self.assertTrue(report["rate_limited"])
        self.assertEqual(report["processed_windows"], 1)
        searches = [inner for path, inner in self.calls if path.endswith("searchDocuments")]
        self.assertEqual(len(searches), 1)

    def test_a_rate_limited_search_is_reported_as_such(self):
        archive = ic.IctihatArchive(
            self._tmp.name,
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(429, json={})),
                follow_redirects=False,
            ),
            delay_seconds=0.0, rate_limit_backoff=(0.0,),
        )
        self.addCleanup(lambda: _run(archive.close()))
        report = _run(archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertTrue(report["rate_limited"])
        self.assertFalse(report["complete"])

    def test_a_server_error_is_not_treated_as_a_rate_limit(self):
        """Gerileme kilidi: 500 tek bir kararın hatasıdır, tur devam eder."""

        def handler(request: httpx.Request) -> httpx.Response:
            inner = json.loads(request.content).get("data") or {}
            if request.url.path.endswith("searchDocuments"):
                rows = self.rows if int(inner.get("pageNumber") or 1) == 1 else []
                return httpx.Response(200, json=_search_envelope(rows))
            if str(inner.get("documentId")) == "9001":
                return httpx.Response(500, json={})
            return httpx.Response(200, json=_content_envelope(_decision_html()))

        archive = ic.IctihatArchive(
            self._tmp.name,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
            delay_seconds=0.0, floor="2020-01-01", window_days=365,
            phrases=("x",), rate_limit_backoff=(0.0,),
        )
        self.addCleanup(lambda: _run(archive.close()))
        report = _run(archive.ingest_window(date(2024, 1, 1), date(2024, 12, 31), "x"))
        self.assertFalse(report["rate_limited"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["stored"], 2)  # 9002 ve 9003 alındı


class DefaultsTests(unittest.TestCase):
    def test_the_request_delay_default_is_not_aggressive(self):
        """Canlıda 0,5 saniye 429 üretti; varsayılan ölçüme göre yükseltildi."""
        self.assertGreaterEqual(ic.REQUEST_DELAY_SECONDS, 1.0)

    def test_the_backoff_schedule_increases(self):
        schedule = list(ic.RATE_LIMIT_BACKOFF)
        self.assertTrue(schedule)
        self.assertEqual(schedule, sorted(schedule))
        self.assertGreaterEqual(schedule[0], 1.0)


if __name__ == "__main__":
    unittest.main()
