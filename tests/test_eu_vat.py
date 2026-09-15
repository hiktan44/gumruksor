"""AB üye devletlerinin KDV oranları: tohum, fasıl kuralı ve TEDB'den otomatik yükseltme.

Bu paketin koruduğu değişmez: **indirimli oran seçimi bir öneridir.** Fasıl kuralı
tetiklendiğinde sonuç her zaman belirsiz işaretlenir, standart oran adaylar arasında
kalır ve beyanname alanı hiçbir koşulda "doğrulandı" olamaz. Hedef ülkede yanlış oranla
açılan bir beyanname ciddi zarar doğurur.
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx

import eu_vat
from eu_vat import EU27, EuVatRates, match_chapter_rule, normalise_iso2


def _workbook(rows: list[tuple[str, str, str, str]]) -> bytes:
    """TEDB dışa aktarımına benzeyen bir çalışma kitabı üretir (Results sayfası)."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Results"
    sheet.append(
        ["Country", "Type", "Rate Type", "Rate", "Situation On", "CN Code",
         "CN Code Description", "CPA Code", "CPA Code Description", "Category", "Comments"]
    )
    for country, kind, rate_type, rate in rows:
        sheet.append([country, kind, rate_type, rate, "", "", "", "", "", "", ""])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _full_export() -> bytes:
    rows: list[tuple[str, str, str, str]] = []
    for iso in sorted(EU27):
        code = "EL" if iso == "GR" else iso
        rows.append((code, "VAT", "Standard rate", "20.0"))
        rows.append((code, "VAT", "Reduced rate", "5.0"))
    return _workbook(rows)


class IsoTests(unittest.TestCase):
    def test_greece_is_el_in_eu_documents_and_gr_in_iso(self) -> None:
        # TEDB "EL" kullanıyor, countries.py ve ihracat akışı "GR"; eşleme iki yönlü olmalı.
        self.assertEqual(normalise_iso2("EL"), "GR")
        self.assertEqual(normalise_iso2("gr"), "GR")
        self.assertIn("GR", EU27)
        self.assertNotIn("EL", EU27)

    def test_unknown_input_does_not_crash(self) -> None:
        self.assertEqual(normalise_iso2(None), "")
        self.assertEqual(normalise_iso2("  de  "), "DE")


class ChapterRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = EuVatRates()
        self.rules = self.index._rules

    def test_food_chapters_match(self) -> None:
        rule = match_chapter_rule(self.rules, "0401100000")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["id"], "food")

    def test_alcohol_is_excluded_from_the_food_rule(self) -> None:
        # Ek-III gıda kalemi alkollü içkiyi kapsamaz; 2204 (şarap) indirimli orana girmemeli.
        self.assertIsNone(match_chapter_rule(self.rules, "2204100000"))
        self.assertIsNone(match_chapter_rule(self.rules, "2208201200"))

    def test_tobacco_is_excluded(self) -> None:
        self.assertIsNone(match_chapter_rule(self.rules, "2402200000"))

    def test_books_and_pharma_match(self) -> None:
        self.assertEqual(match_chapter_rule(self.rules, "4901100000")["id"], "books")
        self.assertEqual(match_chapter_rule(self.rules, "3004900000")["id"], "pharma")

    def test_clothing_has_no_reduced_rule(self) -> None:
        self.assertIsNone(match_chapter_rule(self.rules, "610910000011"))

    def test_short_code_never_matches(self) -> None:
        self.assertIsNone(match_chapter_rule(self.rules, "04"))
        self.assertIsNone(match_chapter_rule(self.rules, ""))


class SeedLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = EuVatRates()

    def test_seed_carries_all_twenty_seven_member_states(self) -> None:
        self.assertTrue(self.index.ready)
        self.assertEqual(self.index.status()["row_count"], 27)
        self.assertEqual(set(self.index._rows), set(EU27))

    def test_every_seed_row_is_marked_unverified(self) -> None:
        # Tohum elle derlenmiştir; hiçbir satır doğrulanmış sayılamaz.
        self.assertEqual(sorted(self.index.status()["unverified"]), sorted(EU27))

    def test_recently_changed_rates_are_flagged_for_verification_first(self) -> None:
        verify_first = set(self.index.status()["verify_first"])
        self.assertTrue(verify_first)
        self.assertLess(len(verify_first), 27, "hepsi işaretliyse işaret anlamını yitirir")

    def test_without_a_gtip_the_standard_rate_applies(self) -> None:
        report = self.index.lookup("DE")
        self.assertEqual(report["applicable_basis"], "standard")
        self.assertFalse(report["ambiguous"])
        self.assertEqual(report["applicable"], report["standard"])

    def test_chapter_rule_never_resolves_to_a_single_certain_rate(self) -> None:
        # Kullanıcı fasıl bazlı otomatik seçim istedi; seçim yapılıyor ama ASLA kesin değil.
        report = self.index.lookup("DE", gtip="0401100000")
        self.assertEqual(report["applicable_basis"], "chapter_rule")
        self.assertTrue(report["ambiguous"], "fasıl tahmini kesin sayılamaz")
        self.assertIn(report["standard"], report["candidates"], "standart oran aday kalmalı")
        self.assertLess(report["applicable"], report["standard"])

    def test_country_without_reduced_rates_stays_on_standard(self) -> None:
        report = self.index.lookup("DK", gtip="0401100000")
        self.assertEqual(report["applicable"], report["standard"])
        self.assertFalse(report["ambiguous"])
        self.assertIn("indirimli oran uygulamıyor", report["note"])

    def test_note_names_the_authority_to_check(self) -> None:
        report = self.index.lookup("DE", gtip="0401100000")
        self.assertIn("doğrulanmamış", report["note"])
        self.assertIn(report["authority_url"], report["note"])

    def test_non_member_state_gets_no_rate(self) -> None:
        report = self.index.lookup("CN")
        self.assertIsNone(report["standard"])
        self.assertIsNone(report["applicable"])
        self.assertEqual(report["applicable_basis"], "unavailable")

    def test_greece_resolves_through_the_eu_code(self) -> None:
        self.assertEqual(self.index.lookup("EL")["iso2"], "GR")
        self.assertIsNotNone(self.index.lookup("EL")["standard"])

    def test_summary_lines_mention_the_rule_when_one_fired(self) -> None:
        lines = " ".join(self.index.summary_lines(self.index.lookup("DE", gtip="0401100000")))
        self.assertIn("standart KDV", lines)
        self.assertIn("indirimli", lines)


class SyncTests(unittest.TestCase):
    """TEDB bugün boş şablon veriyor; eşitleme tohumu ASLA bununla ezmemeli."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)
        self.calls: list[str] = []

    def _index(self) -> EuVatRates:
        return EuVatRates(cache_dir=self.cache)

    def _client(self, *, export: bytes, config_ok: bool = True) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            if "configurations" in request.url.path:
                if not config_ok:
                    return httpx.Response(500)
                countries = [{"id": i, "defaultCountryCode": ("EL" if iso == "GR" else iso)}
                             for i, iso in enumerate(sorted(EU27), start=1)]
                return httpx.Response(200, json={"countries": countries})
            return httpx.Response(200, content=export)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_empty_export_keeps_the_seed(self) -> None:
        index = self._index()
        before = index.status()["row_count"]
        client = self._client(export=_workbook([]))
        result = asyncio.run(index.sync(http=client))
        asyncio.run(client.aclose())
        self.assertFalse(result["ok"])
        self.assertIn("tohum korunuyor", result["error"])
        self.assertEqual(index.status()["row_count"], before)
        self.assertEqual(index.status()["origin"], "seed")

    def test_a_full_export_upgrades_the_seed_to_official_data(self) -> None:
        index = self._index()
        client = self._client(export=_full_export())
        result = asyncio.run(index.sync(http=client))
        asyncio.run(client.aclose())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["row_count"], 27)
        status = index.status()
        self.assertEqual(status["origin"], "synced")
        self.assertEqual(status["unverified"], [], "TEDB'den gelen satırlar doğrulanmış sayılır")
        self.assertTrue(status["sha256"])
        # Önbellek kalıcı: yeni bir örnek resmî veriyi okur.
        self.assertEqual(EuVatRates(cache_dir=self.cache).status()["origin"], "synced")

    def test_chapter_rules_survive_a_sync(self) -> None:
        index = self._index()
        client = self._client(export=_full_export())
        asyncio.run(index.sync(http=client))
        asyncio.run(client.aclose())
        self.assertTrue(index._rules, "fasıl kuralları eşitlemeden sonra da durmalı")
        self.assertTrue(index.lookup("DE", gtip="0401100000")["ambiguous"])

    def test_a_broken_config_keeps_the_seed_and_records_the_error(self) -> None:
        index = self._index()
        client = self._client(export=_full_export(), config_ok=False)
        result = asyncio.run(index.sync(http=client))
        asyncio.run(client.aclose())
        self.assertFalse(result["ok"])
        self.assertEqual(index.status()["origin"], "seed")
        self.assertTrue(index.status()["last_error"])

    def test_a_disallowed_host_never_issues_a_request(self) -> None:
        index = self._index()
        original = eu_vat.TEDB_BASE
        eu_vat.TEDB_BASE = "https://evil.example.com/rest-api"
        self.addCleanup(lambda: setattr(eu_vat, "TEDB_BASE", original))
        client = self._client(export=_full_export())
        result = asyncio.run(index.sync(http=client))
        asyncio.run(client.aclose())
        self.assertFalse(result["ok"])
        self.assertEqual(self.calls, [], "izin listesi dışı adrese istek gitmemeli")

    def test_non_workbook_payload_is_rejected(self) -> None:
        index = self._index()
        client = self._client(export=b"<html>not a workbook</html>")
        result = asyncio.run(index.sync(http=client))
        asyncio.run(client.aclose())
        self.assertFalse(result["ok"])
        self.assertEqual(index.status()["origin"], "seed")


class DeclarationFieldTests(unittest.TestCase):
    """Beyanname alanı: KDV asla 'doğrulandı' olamaz."""

    def _field(self, iso2: str, gtip: str | None):
        from export_requirements import build_export_requirements

        index = EuVatRates()
        vat = index.lookup(iso2, gtip=gtip)
        requirements = build_export_requirements(
            {"destination_country": "Almanya", "candidate_gtip": gtip},
            destination_vat=vat if vat.get("standard") is not None else None,
        )
        return next(item for item in requirements.declaration_fields if item.key == "destination_vat")

    def test_eu_destination_no_longer_reports_no_data(self) -> None:
        field = self._field("DE", "610910000011")
        self.assertEqual(field.certainty, "check_required")
        self.assertIn("Standart", field.value)

    def test_vat_is_never_marked_verified(self) -> None:
        for gtip in ("610910000011", "0401100000", "4901100000", None):
            self.assertNotEqual(self._field("DE", gtip).certainty, "verified", gtip)

    def test_reduced_rate_is_shown_next_to_the_standard_rate(self) -> None:
        # Standart oran görünmeden indirimli oran tek başına gösterilmez.
        field = self._field("DE", "0401100000")
        self.assertIn("Standart", field.value)
        self.assertIn("indirimli", field.value)

    def test_non_eu_destination_still_reports_no_data(self) -> None:
        from export_requirements import build_export_requirements

        requirements = build_export_requirements({"destination_country": "Çin", "candidate_gtip": "0401100000"})
        field = next(item for item in requirements.declaration_fields if item.key == "destination_vat")
        self.assertEqual(field.certainty, "unavailable")
        self.assertIsNone(field.value)

    def test_vat_never_makes_the_file_declaration_ready(self) -> None:
        from export_requirements import build_export_requirements

        index = EuVatRates()
        requirements = build_export_requirements(
            {"destination_country": "Almanya", "candidate_gtip": "0401100000"},
            destination_vat=index.lookup("DE", gtip="0401100000"),
        )
        self.assertNotEqual(requirements.readiness.status, "ready")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as web_app

        self.web_app = web_app
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = type(self.original_limiter)()
        self.addCleanup(lambda: setattr(web_app, "rate_limiter", self.original_limiter))

    def _get(self, query: str):
        from starlette.testclient import TestClient

        with TestClient(self.web_app.app, base_url="https://gumruksor.com") as client:
            return client.get(f"/api/foreign/vat{query}")

    def test_route_returns_the_rate_and_its_status(self) -> None:
        body = self._get("?iso2=DE").json()
        self.assertEqual(body["iso2"], "DE")
        self.assertIsNotNone(body["standard"])
        self.assertIn("summary", body)
        self.assertEqual(body["status"]["row_count"], 27)

    def test_route_applies_the_chapter_rule(self) -> None:
        body = self._get("?iso2=DE&gtip=0401100000").json()
        self.assertEqual(body["applicable_basis"], "chapter_rule")
        self.assertTrue(body["ambiguous"])

    def test_bad_country_code_is_rejected(self) -> None:
        self.assertEqual(self._get("?iso2=D").status_code, 422)
        self.assertEqual(self._get("").status_code, 422)

    def test_bad_gtip_is_rejected(self) -> None:
        self.assertEqual(self._get("?iso2=DE&gtip=1").status_code, 422)


if __name__ == "__main__":
    unittest.main()
