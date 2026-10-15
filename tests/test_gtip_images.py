"""GTİP örnek görselleri: yalnız resmî kaynak adresleri, dosya saklanmaz (ağ yok)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import app as web_app
import btb_client as btb
from auth_service import GoogleAuthService

from tests.test_btb_client import list_page


def rows():
    return btb.parse_list(list_page([
        {"no": "TR160000260001", "gtip": "841430899000", "desc": "Klima kompresörü A", "date": "01/10/2026"},
        {"no": "TR160000260002", "gtip": "841430890000", "desc": "Kompresör B", "date": "05/10/2026"},
        {"no": "TR160000260003", "gtip": "841430100000", "desc": "Kompresör C", "date": "09/10/2026"},
        {"no": "TR160000260004", "gtip": "850000000000", "desc": "Başka eşya", "date": "09/10/2026"},
    ]))


class ByGtipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = btb.BtbStore(self.tmp.name)
        self.store.apply_full_sync(rows())

    def test_exact_match_comes_first_then_shorter_prefixes(self):
        result = self.store.by_gtip("841430899000")
        self.assertEqual([r["btb_no"] for r in result][:3], ["TR160000260001", "TR160000260002", "TR160000260003"])
        self.assertEqual(result[0]["match_digits"], 12)
        self.assertNotIn("TR160000260004", [r["btb_no"] for r in result])

    def test_short_or_garbage_gtip_returns_nothing(self):
        self.assertEqual(self.store.by_gtip("8414"), [])
        self.assertEqual(self.store.by_gtip("abc"), [])

    def test_limit_and_inactive_records(self):
        self.assertEqual(len(self.store.by_gtip("841430899000", limit=2)), 2)
        self.store.apply_full_sync(rows()[:1])
        self.store.apply_full_sync(rows()[:1])
        self.assertEqual([r["btb_no"] for r in self.store.by_gtip("841430899000")], ["TR160000260001"])

    def test_photo_urls_point_to_the_ministry_only(self):
        for item in self.store.by_gtip("841430899000"):
            self.assertTrue(item["photo_url"].startswith("https://uygulama.gtb.gov.tr/BTBBasvuru/grdpht.ashx?ImID=TR"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        engine = btb.BtbEngine(self.tmp.name, delay=0)
        engine.store.apply_full_sync(rows())
        auth = GoogleAuthService(client_id="c", client_secret="s", session_secret="test-session-secret-that-is-long-enough", data_dir=Path(self.tmp.name))
        for name, value in (("btb_engine", engine), ("google_auth", auth), ("rate_limiter", web_app.FixedWindowRateLimiter())):
            patcher = patch.object(web_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")
        self.addCleanup(self.client.close)

    def test_requires_six_digits(self):
        self.assertEqual(self.client.get("/api/gtip/images", params={"gtip": "8414"}).status_code, 422)

    def test_contract_and_notices(self):
        data = self.client.get("/api/gtip/images", params={"gtip": "8414.30.89.90.00"}).json()
        self.assertEqual(data["gtip"], "841430899000")
        self.assertEqual(len(data["turkey_btb"]), 3)
        self.assertIn("kopyalamaz veya saklamaz", data["notices"]["turkey"])
        self.assertIn("bağlayıcı değildir", data["notices"]["eu"])
        self.assertEqual(data["eu_ebti"]["status"], "locked")

    def test_no_image_url_is_ever_invented_for_eu(self):
        from unittest.mock import MagicMock

        hit = {"reference": "DEBTI1/24-1", "issuing_country": "DE", "code": "8414308990", "description": "x", "url": "https://ec.europa.eu/x"}
        result = MagicMock()
        result.as_dict.return_value = {"status": "ok", "hits": [hit]}
        with patch.object(web_app, "require_feature", return_value=None), patch.object(web_app.ebti_engine, "search", return_value=result):
            data = self.client.get("/api/gtip/images", params={"gtip": "841430899000"}).json()
        decision = data["eu_ebti"]["decisions"][0]
        self.assertIsNone(decision["image_url"])
        self.assertEqual(decision["official_url"], "https://ec.europa.eu/x")
        self.assertIn("doğrulanamadığı", data["eu_ebti"]["image_note"])


if __name__ == "__main__":
    unittest.main()
