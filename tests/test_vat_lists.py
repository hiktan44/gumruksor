from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from starlette.testclient import TestClient

import vat_lists
from tariff_engine import TariffEngine, TariffLookupResult
from tax_lists import vat_rate_for
from vat_lists import MIN_ROWS_FOR_REPLACE, PARSER_VERSION, VatRateIndex, parse_vat_decision_text, summary_lines

DECISION_TEXT = """MAL VE HİZMETLERE UYGULANACAK KATMA DEĞER VERGİSİ ORANLARININ TESPİTİNE İLİŞKİN KARAR
MADDE 1 – (1) Mal teslimleri ile hizmet ifalarına uygulanacak katma değer vergisi oranları;
b) Ekli (I) sayılı listede yer alan teslim ve hizmetler için, % 1
c) Ekli (II) sayılı listede yer alan teslim ve hizmetler için, % 10
(I) SAYILI LİSTE
1- Kuru üzüm, kuru incir, kuru kayısı, ceviz, fındık,
antep fıstığı (0806.20 pozisyonu, toptan teslimi),
2- a) Mazı, palamut, kendir tohumu,
b) Meyan kökü, meyan balı,
5- Buğday, arpa, mısır ve çeltiğin sertifikalı tohumlukları (1001.11, 1003.10 pozisyonları),
9- Aşağıda tanımları yapılan motorlu taşıtlardan yalnız "kullanılmış" olanlar, "Türk Gümrük Tarife Cetvelinin 8701.90.50.00.00 Kullanılmış olanlar ile 87.03 pozisyonundaki binek otomobilleri (87.02 pozisyonuna girenler hariç) (steyşın vagonlar dahil)",
10- Türk Gümrük Tarife Cetvelinin 4902 pozisyonundaki gazete ve dergiler (21/6/1927 tarihli ve 1117 sayılı Kanun hükümlerine göre poşetlenerek satılanlar hariç),
(II) SAYILI LİSTE
A) GIDA MADDELERİ
1- Türk Gümrük Tarife Cetvelinin 2 no.lu faslında yer alan mallar,
2- 3 no.lu faslında yer alan mallar (0301.10 süs balıkları hariç),
3- Türk Gümrük Tarife Cetvelinin 4, 5 ve 6 no.lu fasıllarındaki mallar,
4- 0806.20 pozisyonundaki kuru üzümün perakende teslimi,
B) DİĞER MAL VE HİZMETLER
1- 50 ila 63. fasıllar arasındaki mensucat ile 6104.62 pozisyonu,
2- 84.32 pozisyonundaki tarım makineleri ile 8433 ila 8436 pozisyonlarındaki makineler,
3- Sinema, tiyatro, opera, bale giriş ücretleri,
4- 1905.90.30.00.00 pozisyonundaki ekmek (toptan ve perakende),
MADDE 2 – Bu Karar 1/1/2008 tarihinde yürürlüğe girer.
(1) 24/12/2007 tarihli dipnot 8703 pozisyonu
"""


def _write_index(directory: Path, rows: list[dict], **extra) -> VatRateIndex:
    payload = {"source": "test", "source_url": "https://www.mevzuat.gov.tr/test", "parser_version": PARSER_VERSION, "rows": rows, **extra}
    (directory / "seed").mkdir(exist_ok=True)
    (directory / "seed" / vat_lists.DATA_FILE).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return VatRateIndex(data_dir=directory / "seed", cache_dir=directory / "cache")


def _row(list_name: str, text: str, exprs=(), chapters=(), excluded=(), conditions=(), conditional=False, verified=True, row_no="1"):
    return {
        "list": list_name,
        "rate": 1.0 if list_name == "I" else 10.0,
        "section": None,
        "row_no": row_no,
        "text": text,
        "gtip_expressions": list(exprs),
        "excluded_expressions": list(excluded),
        "chapter_ranges": [list(item) for item in chapters],
        "conditions": list(conditions),
        "conditional": conditional,
        "legal_basis": f"2007/13033 s. BKK eki ({list_name}) sayılı liste, {row_no}",
        "verified": verified,
    }


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = parse_vat_decision_text(DECISION_TEXT)
        self.by_key = {(row["list"], row["section"], row["row_no"]): row for row in self.rows}

    def test_rows_lists_and_rates(self) -> None:
        self.assertEqual(len(self.rows), 13)
        self.assertEqual({row["rate"] for row in self.rows if row["list"] == "I"}, {1.0})
        self.assertEqual({row["rate"] for row in self.rows if row["list"] == "II"}, {10.0})
        # Karar maddelerindeki "(I) sayılı listede" ifadesi liste başlığı sayılmaz; dipnot satıra eklenmez.
        self.assertNotIn("8703", json.dumps(self.rows, ensure_ascii=False))
        self.assertTrue(all(row["verified"] for row in self.rows))

    def test_wrapped_lines_and_conditions(self) -> None:
        row = self.by_key[("I", None, "1")]
        self.assertIn("antep fıstığı", row["text"])
        self.assertEqual(row["gtip_expressions"], ["0806.20"])
        self.assertEqual(row["conditions"], ["toptan"])
        self.assertTrue(row["conditional"])
        self.assertEqual(row["legal_basis"], "2007/13033 s. BKK eki (I) sayılı liste, 1")
        sub_items = self.by_key[("I", None, "2")]
        self.assertIn("b) Meyan kökü", sub_items["text"])

    def test_codes_positions_and_exclusions(self) -> None:
        used_cars = self.by_key[("I", None, "9")]
        self.assertEqual(used_cars["gtip_expressions"], ["8701.90.50.00.00", "87.03"])
        self.assertEqual(used_cars["excluded_expressions"], ["87.02"])
        self.assertIn("kullanılmış", used_cars["conditions"])
        self.assertIn("hariç", used_cars["conditions"])
        newspapers = self.by_key[("I", None, "10")]
        self.assertEqual(newspapers["gtip_expressions"], ["4902"])  # 1927 / 1117 sayılı gibi sayılar kod değildir
        self.assertIn("poşetlenerek", newspapers["conditions"])
        seeds = self.by_key[("I", None, "5")]
        self.assertEqual(seeds["gtip_expressions"], ["1001.11", "1003.10"])
        self.assertEqual(seeds["conditions"], ["tohumluk", "sertifikalı"])
        self.assertFalse(seeds["conditional"])

    def test_chapter_references(self) -> None:
        self.assertEqual(self.by_key[("II", "A", "1")]["chapter_ranges"], [[2, 2]])
        fish = self.by_key[("II", "A", "2")]
        self.assertEqual(fish["chapter_ranges"], [[3, 3]])
        self.assertEqual(fish["excluded_expressions"], ["0301.10"])
        self.assertEqual(self.by_key[("II", "A", "3")]["chapter_ranges"], [[4, 4], [5, 5], [6, 6]])
        textiles = self.by_key[("II", "B", "1")]
        self.assertEqual(textiles["chapter_ranges"], [[50, 63]])
        self.assertEqual(textiles["gtip_expressions"], ["6104.62"])
        self.assertEqual(textiles["legal_basis"], "2007/13033 s. BKK eki (II) sayılı liste, B/1")
        machines = self.by_key[("II", "B", "2")]
        self.assertEqual(machines["gtip_expressions"], ["8433", "8434", "8435", "8436", "84.32"])

    def test_service_rows_are_kept_without_codes(self) -> None:
        cinema = self.by_key[("II", "B", "3")]
        self.assertEqual(cinema["gtip_expressions"], [])
        self.assertEqual(cinema["chapter_ranges"], [])
        bread = self.by_key[("II", "B", "4")]
        self.assertEqual(bread["gtip_expressions"], ["1905.90.30.00.00"])
        self.assertFalse(bread["conditional"])  # toptan ve perakende birlikte -> şart ayırt edici değil

    def test_html_to_text(self) -> None:
        html = "<html><style>p{}</style><body><p>(II) SAYILI L&#304;STE</p><p>1- Kuru<br>üzüm</p><table><tr><td>2-</td><td>Buğday 1001 pozisyonu</td></tr></table></body></html>"
        rows = parse_vat_decision_text(vat_lists._html_to_text(html))
        self.assertEqual([row["row_no"] for row in rows], ["1", "2"])
        self.assertEqual(rows[0]["text"], "Kuru üzüm")
        self.assertEqual(rows[1]["gtip_expressions"], ["1001"])


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.rows = [
            _row("II", "50-63. fasıllar mensucat", chapters=[(50, 63)], row_no="1"),
            _row("I", "6104.62 pamuklu pantolon (özel indirim)", exprs=["6104.62"], row_no="2"),
            _row("I", "Kuru üzüm toptan", exprs=["0806.20"], conditions=["toptan"], row_no="3"),
            _row("II", "Kuru üzüm perakende", exprs=["0806.20"], conditions=["perakende"], row_no="4"),
            _row("I", "Kullanılmış binek otomobil", exprs=["87.03"], excluded=["87.02"], conditions=["kullanılmış"], conditional=True, row_no="9", verified=False),
            _row("I", "Balıklar", chapters=[(3, 3)], excluded=["0301.11"], conditions=["hariç"], row_no="5"),
            _row("I", "Tohumluk buğday", exprs=["1001.11"], conditions=["tohumluk"], row_no="6"),
            _row("I", "Hububat", chapters=[(10, 10)], row_no="7"),
        ]
        self.index = _write_index(self.tmp, self.rows, retrieved_at="2026-09-01T00:00:00+00:00", sha256="abc")

    def test_longest_prefix_beats_chapter_range(self) -> None:
        result = self.index.lookup("6104.62.00.00.11")
        self.assertEqual(result["basis"], "official_list")
        self.assertEqual(result["rate"], 1.0)
        self.assertEqual(result["matched_expression"], "6104.62")
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["legal_basis"], "2007/13033 s. BKK eki (I) sayılı liste, 2")
        self.assertEqual(result["source_url"], "https://www.mevzuat.gov.tr/test")
        self.assertEqual(result["retrieved_at"], "2026-09-01T00:00:00+00:00")

    def test_chapter_range_match(self) -> None:
        result = self.index.lookup("5513110000")
        self.assertEqual(result["rate"], 10.0)
        self.assertEqual(result["list"], "II")
        self.assertEqual(result["matched_expression"], "50-63. fasıllar")
        self.assertEqual(result["basis"], "official_list")

    def test_same_specificity_different_rates_is_ambiguous(self) -> None:
        result = self.index.lookup("080620")
        self.assertTrue(result["ambiguous"])
        self.assertIsNone(result["rate"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual({item["rate"] for item in result["candidates"]}, {1.0, 10.0})
        self.assertEqual(sorted(result["conditions"]), ["perakende", "toptan"])
        self.assertIn("toptan", summary_lines(result)[0])

    def test_conditional_row_adds_general_rate_candidate(self) -> None:
        result = self.index.lookup("8703.23.19.00.00")
        self.assertTrue(result["ambiguous"])
        self.assertEqual([item["rate"] for item in result["candidates"]], [1.0, 20.0])
        self.assertEqual(result["candidates"][0]["conditions"], ["kullanılmış"])
        self.assertFalse(result["verified"])

    def test_excluded_expression_returns_general_rate(self) -> None:
        result = self.index.lookup("0301.11.00.00.00")
        self.assertEqual(result["basis"], "official_list")
        self.assertEqual(result["rate"], 20.0)
        self.assertEqual(result["matched_expression"], "0301.11")
        self.assertIn("hariç", result["legal_basis"])
        self.assertEqual(self.index.lookup("0302.11")["rate"], 1.0)
        self.assertEqual(self.index.lookup("8702.10")["basis"], "heuristic")  # hariç satırı olumlu eşleşme değildir

    def test_heuristic_fallback_when_no_row_matches(self) -> None:
        result = self.index.lookup("8471300000")
        self.assertEqual(result["basis"], "heuristic")
        self.assertEqual(result["rate"], 20.0)
        self.assertIsNone(result["matched_expression"])
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["ambiguous"])
        self.assertEqual(vat_rate_for("8471300000", None)["basis"], "heuristic")
        self.assertEqual(vat_rate_for("6104.62", self.index)["basis"], "official_list")

    def test_short_query_reports_partial_coverage(self) -> None:
        result = self.index.lookup("1001")
        self.assertEqual(result["rate"], 1.0)
        # 1001.11 satırı (6 hane) ile 10. fasıl aynı oranı verir; en özel eşleşme ön ektir
        self.assertEqual(result["matched_expression"], "1001.11")
        self.assertEqual(result.get("coverage"), "partial")

    def test_status(self) -> None:
        status = self.index.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["row_count"], 8)
        self.assertEqual(status["row_counts"], {"I": 6, "II": 2})
        self.assertEqual(status["origin"], "seed")
        self.assertEqual(status["sha256"], "abc")
        self.assertIsNone(status["last_error"])

    def test_empty_gtip_falls_back(self) -> None:
        self.assertEqual(self.index.lookup("")["basis"], "heuristic")


def _decision_html(row_count: int) -> str:
    lines = ["<html><body><p>(I) SAYILI LİSTE</p>"]
    for number in range(1, row_count // 2 + 1):
        lines.append(f"<p>{number}- Türk Gümrük Tarife Cetvelinin {number:02d}01.10 pozisyonundaki mallar,</p>")
    lines.append("<p>(II) SAYILI LİSTE</p><p>A) GIDA MADDELERİ</p>")
    for number in range(1, row_count - row_count // 2 + 1):
        lines.append(f"<p>{number}- Türk Gümrük Tarife Cetvelinin {number + 40} no.lu faslında yer alan mallar,</p>")
    lines.append("</body></html>")
    return "".join(lines)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.index = _write_index(self.tmp, [_row("I", "tohum satırı", exprs=["0806.20"])])

    def _client(self, handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_successful_sync_replaces_rows_and_writes_cache(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=_decision_html(40))

        report = asyncio.run(self.index.sync(self._client(handler)))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["row_count"], 40)
        self.assertTrue(seen[0].startswith("https://www.mevzuat.gov.tr/"))
        status = self.index.status()
        self.assertEqual(status["origin"], "synced")
        self.assertEqual(status["row_count"], 40)
        self.assertEqual(len(status["sha256"]), 64)
        self.assertTrue(status["retrieved_at"])
        cache = json.loads((self.tmp / "cache" / vat_lists.DATA_FILE).read_text(encoding="utf-8"))
        self.assertEqual(cache["parser_version"], PARSER_VERSION)
        self.assertEqual(len(cache["rows"]), 40)
        self.assertEqual(self.index.lookup("4101.20")["rate"], 10.0)
        # Yeni bir dizin önbellekten açılır
        reloaded = VatRateIndex(data_dir=self.tmp / "seed", cache_dir=self.tmp / "cache")
        self.assertEqual(reloaded.status()["origin"], "synced")
        self.assertEqual(reloaded.status()["row_count"], 40)

    def test_failed_download_keeps_seed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="hata")

        report = asyncio.run(self.index.sync(self._client(handler)))
        self.assertFalse(report["ok"])
        status = self.index.status()
        self.assertEqual(status["origin"], "seed")
        self.assertEqual(status["row_count"], 1)
        self.assertIn("500", status["last_error"])
        self.assertFalse((self.tmp / "cache" / vat_lists.DATA_FILE).exists())

    def test_too_few_rows_keeps_seed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_decision_html(MIN_ROWS_FOR_REPLACE - 2))

        report = asyncio.run(self.index.sync(self._client(handler)))
        self.assertFalse(report["ok"])
        self.assertIn("satır", report["error"])
        self.assertEqual(self.index.status()["origin"], "seed")
        self.assertEqual(self.index.lookup("0806.20")["rate"], 1.0)

    def test_disallowed_host_is_refused(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, text=_decision_html(40))

        original = (vat_lists.IFRAME_URL, vat_lists.SOURCE_URL)
        vat_lists.IFRAME_URL = "https://evil.example.com/x"
        vat_lists.SOURCE_URL = "http://www.mevzuat.gov.tr/plain"
        try:
            report = asyncio.run(self.index.sync(self._client(handler)))
        finally:
            vat_lists.IFRAME_URL, vat_lists.SOURCE_URL = original
        self.assertFalse(report["ok"])
        self.assertEqual(calls, [])
        self.assertEqual(self.index.status()["origin"], "seed")


class ShippedSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = VatRateIndex(cache_dir=tempfile.mkdtemp())

    def test_seed_loads_both_lists(self) -> None:
        status = self.index.status()
        self.assertTrue(status["ready"])
        self.assertGreaterEqual(status["row_counts"]["I"], 20)
        self.assertGreaterEqual(status["row_counts"]["II"], 15)
        self.assertEqual(status["origin"], "seed")
        self.assertIn("2007/13033", status["source"])

    def test_seed_rows_have_required_shape(self) -> None:
        for row in self.index._rows:
            self.assertIn(row["list"], ("I", "II"))
            self.assertIn("2007/13033 s. BKK eki", row["legal_basis"])
            self.assertIsInstance(row["verified"], bool)
            self.assertTrue(row["gtip_expressions"] or row["chapter_ranges"], row["text"])

    def test_known_items(self) -> None:
        wheat = self.index.lookup("100111")
        self.assertEqual((wheat["list"], wheat["rate"], wheat["basis"]), ("I", 1.0, "official_list"))
        medicine = self.index.lookup("300490")
        self.assertEqual((medicine["list"], medicine["rate"]), ("II", 10.0))
        trousers = self.index.lookup("6104620000")
        self.assertEqual((trousers["list"], trousers["rate"]), ("II", 10.0))
        computer = self.index.lookup("847130")
        self.assertEqual((computer["basis"], computer["rate"]), ("heuristic", 20.0))
        used_car = self.index.lookup("870323")
        self.assertTrue(used_car["ambiguous"])
        self.assertIn("kullanılmış", used_car["conditions"])


class TariffAttachmentTests(unittest.TestCase):
    def test_attach_vat_rate_sets_field_and_warning(self) -> None:
        engine = TariffEngine.__new__(TariffEngine)
        engine.trade_measures = None
        engine.excise_tax = None
        engine.vat_rates = VatRateIndex(cache_dir=tempfile.mkdtemp())
        result = TariffLookupResult(status="matched", gtip="6104620000", as_of="2026-09-14")
        engine._attach_trade_measures(result)
        self.assertIsNotNone(result.vat_rate)
        self.assertEqual(result.vat_rate["rate"], 10.0)
        self.assertTrue(any("KDV önerisi" in warning for warning in result.warnings))

    def test_missing_index_attribute_is_tolerated(self) -> None:
        engine = TariffEngine.__new__(TariffEngine)
        engine.trade_measures = None
        engine.excise_tax = None
        result = TariffLookupResult(status="matched", gtip="6104620000", as_of="2026-09-14")
        engine._attach_trade_measures(result)
        self.assertIsNone(result.vat_rate)


class VatRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as web_app

        self.web_app = web_app
        self.client = TestClient(web_app.app, base_url="https://gumruksor.com")
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.addCleanup(setattr, web_app, "rate_limiter", self.original_limiter)

    def test_lookup_route(self) -> None:
        response = self.client.get("/api/tariff/vat?gtip=6104620000")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["rate"], 10.0)
        self.assertEqual(body["basis"], "official_list")
        self.assertIn("status", body)
        self.assertTrue(body["status"]["ready"])
        self.assertTrue(body["summary"][0].startswith("KDV önerisi"))
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_missing_gtip_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/tariff/vat").status_code, 422)
        self.assertEqual(self.client.get("/api/tariff/vat?gtip=abc").status_code, 422)


if __name__ == "__main__":
    unittest.main()
