"""Türkiye BTB toplayıcısı ve BTB / fihrist uçları: ağ yok, sentetik sayfalarla."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import app as web_app
import btb_client as btb
from auth_service import GoogleAuthService

ROW = (
    '<tr><td align="center"><a id="x" href="javascript:__doPostBack(&#39;a&#39;,&#39;&#39;)">{no}</a></td>'
    "<td>{gtip}</td><td style=\"font-size:11pt;\">{desc}</td><td style=\"width:20%;\">{date}</td></tr>"
)


def list_page(rows, *, next_page=None):
    body = "".join(ROW.format(**row) for row in rows)
    pager = f"<a href=\"javascript:__doPostBack(&#39;ctl00$ContentPlaceHolder1$GridView1&#39;,&#39;Page${next_page}&#39;)\">{next_page}</a>" if next_page else ""
    return f'<form><input type="hidden" name="__VIEWSTATE" value="vs" /><table>{body}</table>{pager}</form>'


DETAIL = (
    "<div>BAĞLAYICI TARİFE BİLGİSİ DETAY BİLGİSİ <b>BTB Numarası:</b> TR160000260042 Gtip No: 841430899000 "
    "Sınıflandırmanın Gerekçesi: Genel Kurallar (1) ve (6) <b>Eşyanın Tanımı:</b> Motor Tahrikli, Klima kompresörü. "
    "Sayfa En iyi 1280 x 1024 çözünürlükte görüntülenir.</div>"
)
R1 = {"no": "TR160000260042", "gtip": "841430899000", "desc": "Motor Tahrikli, Klima kompresö ...", "date": "09/10/2026"}
R2 = {"no": "TR350000260050", "gtip": "870195900000", "desc": "Sany marka, STT4501E2-D model ...", "date": "07/10/2026"}


class ParserTests(unittest.TestCase):
    def test_list_rows_are_parsed_with_iso_dates(self):
        rows = btb.parse_list(list_page([R1, R2], next_page=2))
        self.assertEqual([r["btb_no"] for r in rows], ["TR160000260042", "TR350000260050"])
        self.assertEqual(rows[0]["valid_from"], "2026-10-09")
        self.assertEqual(rows[0]["gtip"], "841430899000")

    def test_unexpected_page_yields_nothing_instead_of_guessing(self):
        self.assertEqual(btb.parse_list("<html><body>Bakım çalışması</body></html>"), [])

    def test_pager_end_detection(self):
        self.assertTrue(btb.has_next_page(list_page([R1], next_page=2), 1))
        self.assertFalse(btb.has_next_page(list_page([R1]), 1))

    def test_detail_parsing(self):
        detail = btb.parse_detail(DETAIL)
        self.assertEqual(detail["description_full"], "Motor Tahrikli, Klima kompresörü.")
        self.assertIn("Genel Kurallar", detail["reasoning"])
        self.assertIsNone(btb.parse_detail("<html>yok</html>"))

    def test_photo_url_validates_number(self):
        self.assertTrue(btb.photo_url("TR160000260042").endswith("ImID=TR160000260042"))
        with self.assertRaises(ValueError):
            btb.photo_url("../../etc/passwd")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = btb.BtbStore(self.tmp.name)
        self.rows = btb.parse_list(list_page([R1, R2]))

    def test_sync_search_and_prefix(self):
        self.assertEqual(self.store.apply_full_sync(self.rows), {"seen": 2, "added": 2})
        self.assertEqual(self.store.apply_full_sync(self.rows)["added"], 0)
        self.assertEqual(len(self.store.search("klima")), 1)
        self.assertEqual(len(self.store.search("", "8701")), 1)
        self.assertEqual(self.store.search("", ""), [])

    def test_record_missing_twice_becomes_inactive_but_is_kept(self):
        self.store.apply_full_sync(self.rows)
        self.store.apply_full_sync(self.rows[:1])
        self.assertTrue(self.store.get("TR350000260050")["active"])
        self.store.apply_full_sync(self.rows[:1])
        self.assertFalse(self.store.get("TR350000260050")["active"])
        self.assertEqual(self.store.search("sany"), [])

    def test_detail_replaces_excerpt(self):
        self.store.apply_full_sync(self.rows)
        self.assertTrue(self.store.get("TR160000260042")["description_is_excerpt"])
        self.store.save_detail("TR160000260042", btb.parse_detail(DETAIL))
        record = self.store.get("TR160000260042")
        self.assertFalse(record["description_is_excerpt"])
        self.assertEqual(len(self.store.search("kompresörü")), 1)


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.posts = pages, 0

    async def get(self, url, **kwargs):
        return type("R", (), {"text": self.pages[0], "raise_for_status": lambda s: None})()

    async def post(self, url, data=None, **kwargs):
        self.posts += 1
        page = int(str(data["__EVENTARGUMENT"]).split("$")[1])
        return type("R", (), {"text": self.pages[page - 1], "raise_for_status": lambda s: None})()

    async def aclose(self):
        pass


class SyncTests(unittest.TestCase):
    def test_chained_paging_and_shrink_guard(self):
        pages = [list_page([R1], next_page=2), list_page([R2])]
        with tempfile.TemporaryDirectory() as tmp:
            engine = btb.BtbEngine(tmp, http=FakeHttp(pages), delay=0)
            self.assertEqual(asyncio.run(engine.sync())["added"], 2)
            shrunk = btb.BtbEngine(tmp, http=FakeHttp([list_page([R1])]), delay=0)
            outcome = asyncio.run(shrunk.sync())
            self.assertEqual(outcome["status"], "error")
            self.assertEqual(engine.store.status()["active"], 2)

    def test_broken_page_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = btb.BtbEngine(tmp, http=FakeHttp(["<html>bakım</html>"]), delay=0)
            self.assertEqual(asyncio.run(engine.sync())["status"], "error")
            self.assertEqual(engine.store.status()["total"], 0)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = btb.BtbEngine(self.tmp.name, delay=0)
        self.engine.store.apply_full_sync(btb.parse_list(list_page([R1, R2])))
        auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=Path(self.tmp.name))
        for name, value in (("btb_engine", self.engine), ("google_auth", auth), ("rate_limiter", web_app.FixedWindowRateLimiter())):
            patcher = patch.object(web_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")
        self.addCleanup(self.client.close)

    def test_search_requires_input_and_returns_three_sources(self):
        self.assertEqual(self.client.get("/api/btb/search").status_code, 422)
        data = self.client.get("/api/btb/search", params={"q": "klima"}).json()
        self.assertEqual(len(data["turkey_btb"]), 1)
        for key in ("eu_ebti", "nomenclature", "notice"):
            self.assertIn(key, data)
        self.assertIn("bağlayıcı değildir", data["notice"])

    def test_detail_rejects_bad_number_and_unknown_record(self):
        self.assertEqual(self.client.get("/api/btb/not-a-number").status_code, 422)
        self.assertEqual(self.client.get("/api/btb/TR999999999999").status_code, 404)

    def test_detail_serves_cached_full_record_without_network(self):
        self.engine.store.save_detail("TR160000260042", btb.parse_detail(DETAIL))
        data = self.client.get("/api/btb/TR160000260042").json()
        self.assertFalse(data["description_is_excerpt"])

    def test_fihrist_endpoint_contract(self):
        data = self.client.get("/api/fihrist", params={"letter": "k", "limit": 5}).json()
        self.assertIn(data["status"], {"ok", "unavailable"})
        self.assertEqual(self.client.get("/api/fihrist", params={"limit": "x"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()


class TurkishOrderTests(unittest.TestCase):
    def test_turkish_alphabet_order(self):
        from tariff_nomenclature import _turkish_order

        words = ["zeytin", "çay", "ısı", "ipek", "şeker", "ceviz", "ütü", "oda", "öğrenci"]
        self.assertEqual(sorted(words, key=_turkish_order), ["ceviz", "çay", "ısı", "ipek", "oda", "öğrenci", "şeker", "ütü", "zeytin"])
