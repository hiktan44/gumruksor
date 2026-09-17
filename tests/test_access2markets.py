"""Access2Markets motoru: çözümleyici, arşiv, kapsam sınırları ve kaynak karşılaştırması.

Gerçek ağ erişimi yoktur; yanıtlar ``httpx.MockTransport`` ile üretilir ve gövdeler
17.09.2026'da canlı uçlardan alınan **gerçek** yanıtlardan kırpılmıştır.

Bu dosyanın koruduğu değişmezler:

* ücretsiz kaynak ücretli kaynakla **aynı özet şeklini** üretir (arayüz tek şekil okur),
* AB dışı varış ülkesinde oran **okunmaz** (gövde tamamen farklı bir yapı döndürüyor),
* arşiv tazeyse ağa çıkılmaz; kaynak düşerse bayat kayıt ``stale`` notuyla döner,
* karşılaştırma ücretli tarafta **hiçbir yeni sorgu yapmaz** (ücret doğmaz).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import access2markets as a2m


def _measure(
    *,
    type_: str = "Third country duty",
    origin: str = "ERGA OMNES",
    area: str = "TOUT",
    formula: str = "12.00%",
    regulation: str = "R9617340",
    conditions: list | None = None,
) -> dict:
    return {
        "id": 0,
        "hsCode": None,
        "description": None,
        "origin": origin,
        "type": type_,
        "measureType": "103",
        "geographicalArea": "1011",
        "geographicalSigl": area,
        "regulationRoleType": "4",
        "regulationId": regulation,
        "regulationOrderNumber": None,
        "startDate": "01/01/1997",
        "endDate": "01/01/3000",
        "additionalCodeId": None,
        "additionalCodeText": None,
        "exclusions": None,
        "tariffFormula": formula,
        "footnotes": [],
        "conditions": conditions or [],
    }


def _tariff_body() -> list:
    return [
        {
            "measures": [
                _measure(),
                _measure(
                    type_="Customs Union Duty",
                    origin="Türkiye",
                    area="TR",
                    formula="0%",
                    regulation="D9601421",
                    conditions=[{"description": "A.TR dolaşım belgesi", "documentCode": "N018"}],
                ),
                _measure(type_="Supplementary unit", formula="p/st", regulation="R8726581"),
            ],
            "description": "T-shirts, of cotton",
            "code": None,
        }
    ]


def _taxes_body() -> list:
    return [
        {"taxType": "EXC", "taxLabel": None, "taxRate": "-", "destinationCountry": "DE", "revisionDate": None},
        {"taxType": "VAT", "taxLabel": None, "taxRate": "19%", "destinationCountry": "DE", "revisionDate": "2026-07-01"},
    ]


def _documents_body() -> list:
    return [
        {"code": "overview", "type": "o"},
        {"code": "cernonpreforig", "label": "Certificate of non-preferential origin", "type": "g"},
        {"code": "cominvce", "label": "Commercial invoice", "type": "g"},
    ]


def _roo_body() -> list:
    return [
        {
            "code": "pem",
            "label": "Rules of Origin PEM Convention",
            "important": "<table>...</table>",
            "notes": "<div class=note>Note 2</div>",
            "howtoread": "<SUBNOTE>...</SUBNOTE>",
            "rules": "<table><tr><td>ex Chapter 61</td></tr></table>",
            "footnotes": None,
            "oj": None,
        }
    ]


# AB dışı varışta gövde bir **sözlük**; ölçü listesi yok.
_SCHEMA_BODY = {
    "schemas": [
        {"code": "GEN", "label": "GENERAL", "description": "<p>general</p>", "countries": [{"code": "BY", "label": "Belarus"}]},
        {"code": "MFN", "label": "MFN", "description": "<p>mfn</p>", "countries": []},
    ]
}


def _transport(
    *, calls: list | None = None, tariff=None, fail: bool = False, only_codes: set | None = None
) -> httpx.MockTransport:
    """``only_codes`` verilirse yalnız o kodlar veri döndürür; ötekiler boş liste.

    Gerçek kaynağın davranışı bu: AB'de karşılığı olmayan kod hata değil **boş liste**
    döndürüyor, daha kaba kod ise veri veriyor.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        path = request.url.path
        if fail:
            return httpx.Response(503, text="down")
        if "/api/tariffs/get/" in path:
            if only_codes is not None:
                queried = path.split("/api/tariffs/get/")[1].split("/")[0]
                if queried not in only_codes:
                    return httpx.Response(200, json=[])
            body = _tariff_body() if tariff is None else tariff
            return httpx.Response(200, json=body)
        if "/api/taxes/get/" in path:
            return httpx.Response(200, json=_taxes_body())
        if "/api/v2/document/list" in path:
            return httpx.Response(200, json=_documents_body())
        if "/roo/public/v1/classic/" in path:
            return httpx.Response(200, json=_roo_body())
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def _engine(tmp: str, **kwargs) -> a2m.Access2MarketsEngine:
    transport = kwargs.pop("transport", None) or _transport()
    return a2m.Access2MarketsEngine(
        tmp,
        http=httpx.AsyncClient(transport=transport, follow_redirects=False),
        enabled=kwargs.pop("enabled", True),
        # Testlerde geri çekilme beklemesi yok; yeniden deneme sayısı ayrı test edilir.
        retry_base_seconds=kwargs.pop("retry_base_seconds", 0),
        **kwargs,
    )


class CodeAndCountryTests(unittest.TestCase):
    def test_a_turkish_gtip_is_cut_to_the_eu_ten_digits(self) -> None:
        # 11-12. haneler ulusaldır; AB'ye sorulursa kod hiç bulunamaz.
        self.assertEqual(a2m.normalise_code("610910000011"), "6109100000")

    def test_a_short_code_is_refused_rather_than_padded(self) -> None:
        self.assertEqual(a2m.normalise_code("61"), "")

    def test_greece_is_resolved_from_the_taric_spelling(self) -> None:
        self.assertEqual(a2m.normalise_iso2("EL"), "GR")
        self.assertTrue(a2m.is_eu_member("EL"))

    def test_the_united_kingdom_is_not_an_eu_member(self) -> None:
        self.assertFalse(a2m.is_eu_member("GB"))
        self.assertFalse(a2m.is_eu_member("UK"))


class ParserTests(unittest.TestCase):
    def test_a_measure_row_becomes_the_shared_shape(self) -> None:
        parsed = a2m.measure_from_payload(_tariff_body()[0]["measures"][1])
        self.assertEqual(parsed["kind"], "customs_union_duty")
        self.assertEqual(parsed["duty_text"], "0%")
        self.assertEqual(parsed["partner_area"], "Türkiye")
        self.assertEqual(parsed["documents"], ["N018"])
        self.assertEqual(parsed["legal_basis"], "D9601421")

    def test_the_schema_body_of_a_non_eu_destination_yields_no_groups(self) -> None:
        self.assertEqual(a2m.parse_tariff_groups(_SCHEMA_BODY), [])

    def test_a_dash_tax_rate_is_read_as_missing_not_as_zero(self) -> None:
        rows = a2m.parse_taxes(_taxes_body())
        excise = next(row for row in rows if row["tax_type"] == "EXC")
        vat = next(row for row in rows if row["tax_type"] == "VAT")
        self.assertIsNone(excise["rate"], "'-' veri yokluğudur; sıfır oran değildir")
        self.assertEqual(vat["rate"], "19%")
        self.assertEqual(vat["revision_date"], "2026-07-01")

    def test_navigation_rows_are_not_listed_as_documents(self) -> None:
        rows = a2m.parse_documents(_documents_body())
        self.assertEqual([row["code"] for row in rows], ["cernonpreforig", "cominvce"])

    def test_rules_of_origin_html_is_kept_but_not_interpreted(self) -> None:
        sections = a2m.parse_rules_of_origin(_roo_body())
        self.assertEqual(sections[0]["code"], "pem")
        self.assertTrue(sections[0]["has_rules"])
        self.assertIn("ex Chapter 61", sections[0]["rules_html"])


class LookupTests(unittest.TestCase):
    def test_the_turkish_preference_row_reaches_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp)
            result = asyncio.run(engine.lookup("610910000011", origin="TR", destination="DE"))
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.summary["mfn_rate"], "12.00%")
            self.assertEqual(result.summary["partner_rate"], "0%")
            self.assertEqual(result.summary["partner_rate_kind"], "customs_union_duty")
            self.assertEqual(result.summary["required_documents"], ["N018"])

    def test_the_summary_shape_matches_the_paid_source(self) -> None:
        # Arayüz ve ihracat dosyası tek şekil okur; iki kaynak ayrışırsa sessizce bozulur.
        import eu_taric as et

        with tempfile.TemporaryDirectory() as tmp:
            free = asyncio.run(_engine(tmp).lookup("6109100000")).summary
        paid = et.resolve_rates({"importMeasures": [], "cnCode": "61091000"}, "TR")
        self.assertTrue(set(paid).issubset(set(free)), set(paid) - set(free))

    def test_taxes_and_documents_travel_with_the_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(_engine(tmp).lookup("6109100000"))
            self.assertEqual([row["tax_type"] for row in result.taxes], ["EXC", "VAT"])
            self.assertEqual(len(result.documents), 2)

    def test_a_non_eu_destination_never_reads_a_rate(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls))
            result = asyncio.run(engine.lookup("6109100000", destination="JP"))
        self.assertEqual(result.status, "non_eu_destination")
        self.assertEqual(result.summary, {})
        self.assertEqual(calls, [], "AB dışı varışta hiç istek yapılmamalı")

    def test_a_dictionary_body_is_not_mistaken_for_measures(self) -> None:
        # Kaynak AB üyesi sanılan bir kodda şema gövdesi döndürürse de oran okunmaz.
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(tariff=_SCHEMA_BODY))
            result = asyncio.run(engine.lookup("6109100000", destination="DE"))
        self.assertEqual(result.status, "non_eu_destination")
        self.assertEqual(result.summary, {})

    def test_an_empty_list_is_recorded_as_not_found_rather_than_free_of_duty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(tariff=[]))
            result = asyncio.run(engine.lookup("9999999999"))
            self.assertEqual(result.status, "not_found")
            self.assertEqual(result.summary, {})
            self.assertIsNotNone(engine.archived("9999999999", "TR", engine.destination))

    def test_a_fresh_archive_row_is_served_without_touching_the_network(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls))
            asyncio.run(engine.lookup("6109100000"))
            first = len(calls)
            result = asyncio.run(engine.lookup("6109100000"))
        self.assertEqual(len(calls), first, "taze arşiv ağa çıkmamalı")
        self.assertTrue(result.from_archive)
        self.assertEqual(result.summary["partner_rate"], "0%")

    def test_a_failing_source_falls_back_to_the_stale_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp)
            asyncio.run(engine.lookup("6109100000"))
            engine._http = httpx.AsyncClient(transport=_transport(fail=True), follow_redirects=False)
            result = asyncio.run(engine.lookup("6109100000", refresh=True))
        self.assertTrue(result.stale)
        self.assertTrue(result.from_archive)
        self.assertEqual(result.summary["mfn_rate"], "12.00%")

    def test_a_failing_source_without_an_archive_does_not_invent_a_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(fail=True))
            result = asyncio.run(engine.lookup("6109100000"))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.summary, {})

    def test_the_request_goes_to_the_commission_host_in_product_origin_destination_order(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls))
            asyncio.run(engine.lookup("6109100000", origin="TR", destination="DE"))
        self.assertIn("trade.ec.europa.eu/access-to-markets/api/tariffs/get/6109100000/TR/DE", calls[0])


class CodeLevelFallbackTests(unittest.TestCase):
    """10 hanenin AB'de karşılığı yoksa oran CN8'den okunur — ölçümün zorunlu kıldığı dal.

    40 fasla yayılmış 120 gerçek GTİP denendiğinde 51'i (%42,5) 10 hanede boş döndü ama
    CN8'de veri verdi. Düşme olmasaydı bu kodlar "AB'de bulunamadı" diye raporlanacaktı.
    """

    def test_a_ten_digit_miss_falls_back_to_cn8(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls, only_codes={"52054100"}))
            result = asyncio.run(engine.lookup("520541009000"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.summary["match_level"], "cn8")
        self.assertEqual(result.summary["matched_code"], "52054100")
        self.assertEqual(result.summary["queried_code"], "5205410090")
        tariff_calls = [c for c in calls if "/api/tariffs/get/" in c]
        self.assertEqual(len(tariff_calls), 2, "önce 10 hane, sonra CN8 denenir")

    def test_the_user_is_told_the_rate_came_from_a_broader_code(self) -> None:
        # Sessizce daha kaba bir oran vermek, bulunamadı demekten daha tehlikelidir.
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(only_codes={"52054100"}))
            result = asyncio.run(engine.lookup("520541009000"))
        self.assertTrue(any("CN8" in w for w in result.warnings), result.warnings)
        self.assertTrue(any("5205410090" in w for w in result.warnings), result.warnings)

    def test_an_exact_ten_digit_hit_does_not_fall_back_or_warn(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls, only_codes={"6109100000"}))
            result = asyncio.run(engine.lookup("610910000011"))
        self.assertEqual(result.summary["match_level"], "hs10")
        tariff_calls = [c for c in calls if "/api/tariffs/get/" in c]
        self.assertEqual(len(tariff_calls), 1, "tam isabet varsa daha kaba kod denenmez")
        self.assertEqual(result.warnings, [])

    def test_hs6_is_the_last_resort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(only_codes={"610910"}))
            result = asyncio.run(engine.lookup("610910000011"))
        self.assertEqual(result.summary["match_level"], "hs6")
        self.assertEqual(result.summary["matched_code"], "610910")

    def test_a_code_absent_at_every_level_is_still_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(only_codes=set()))
            result = asyncio.run(engine.lookup("999999999900"))
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.summary, {})


class OutboundGuardTests(unittest.TestCase):
    def test_a_foreign_host_is_refused_before_any_request(self) -> None:
        from security_firewall import SecurityViolation

        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls))
            with self.assertRaises(SecurityViolation):
                asyncio.run(engine._get_json("https://example.invalid/api", allowed_hosts=a2m._A2M_HOSTS))
        self.assertEqual(calls, [])

    def test_the_rules_of_origin_host_is_not_accepted_for_tariff_calls(self) -> None:
        from security_firewall import SecurityViolation

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp)
            with self.assertRaises(SecurityViolation):
                asyncio.run(
                    engine._get_json(
                        "https://webgate.ec.europa.eu/roo/public/v1/classic", allowed_hosts=a2m._A2M_HOSTS
                    )
                )


class RulesOfOriginTests(unittest.TestCase):
    def test_a_chapter_is_derived_from_a_gtip_and_archived(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(calls=calls))
            result = asyncio.run(engine.rules_of_origin("610910000011"))
            self.assertEqual(result["chapter"], "61")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["sha256"])
            again = asyncio.run(engine.rules_of_origin("61"))
        self.assertTrue(again["from_archive"])
        self.assertEqual(len(calls), 1, "arşivdeki fasıl yeniden indirilmemeli")

    def test_an_unusable_chapter_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(_engine(tmp).rules_of_origin("abc"))
        self.assertEqual(result["status"], "not_found")


class FillTests(unittest.TestCase):
    def test_the_module_default_keeps_the_bulk_fill_off(self) -> None:
        """Varsayılan kapalı: sınırlı kota kullanıcı sorgularına ayrılır.

        Canlı ölçüm dolum hızını 0,24 kod/dakika gösterdi (kalan katalog ~34 gün) ve
        dolum bu sınırlı kotayı kullanıcı sorgularıyla paylaşıyordu. Bir ay sürecek
        arka plan işi uğruna gerçek bir ihracat sorgusunun 429 yemesi kabul edilemez.
        """
        self.assertFalse(a2m.A2M_FILL_ENABLED)

    def test_the_fill_is_off_until_it_is_switched_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, code_source=lambda: ["6109100000"], fill_enabled=False)
            report = asyncio.run(engine.fill_once())
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(report["processed"], 0)

    def test_never_fetched_codes_come_before_refresh_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp, code_source=lambda: ["6109100000", "8517130000"], fill_enabled=True, delay_seconds=0
            )
            asyncio.run(engine.lookup("8517130000"))
            pending = engine._pending_codes(10, "TR", engine.destination)
        self.assertEqual(pending, ["6109100000"])

    def test_a_fill_round_records_every_code_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp, code_source=lambda: ["6109100000", "6109901000"], fill_enabled=True, delay_seconds=0
            )
            report = asyncio.run(engine.fill_once())
            self.assertEqual(report["processed"], 2)
            self.assertEqual(report["ok"], 2)
            self.assertEqual(report["remaining"], 0)
            plan = engine.fill_plan()
        self.assertEqual(plan["stored"], 2)
        self.assertEqual(plan["cost_usd"], 0.0, "bu kaynak ücretsizdir")


class FillThroughputTests(unittest.TestCase):
    """Kataloğun tamamı makul sürede dolmalı: 11.997 kod sıralı ~7,7 saat sürüyor.

    Sabit uzun aralık + sıralı istek, kataloğu günlerce yarım bırakıyordu. Bu sınıf
    iki kaldıracı kilitler: eş zamanlılık ve iş varken kısa bekleme.
    """

    def test_a_round_runs_codes_concurrently(self) -> None:
        import time as _time

        seen: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(_time.monotonic())
            return httpx.Response(200, json=_tariff_body())

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp,
                transport=httpx.MockTransport(handler),
                code_source=lambda: [f"61091000{i:02d}" for i in range(6)],
                fill_enabled=True,
                delay_seconds=0.05,
                concurrency=3,
            )
            started = _time.monotonic()
            report = asyncio.run(engine.fill_once())
            elapsed = _time.monotonic() - started
        self.assertEqual(report["ok"], 6)
        # Sıralı olsaydı 6 × 0,05 = 0,30 sn'nin altına inemezdi.
        self.assertLess(elapsed, 0.25, f"eş zamanlılık uygulanmıyor ({elapsed:.3f} sn)")

    def test_concurrency_is_capped_so_the_source_is_not_flooded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, concurrency=99)
        self.assertLessEqual(engine.concurrency, 6)

    def test_the_loop_comes_back_quickly_while_work_remains(self) -> None:
        # İş varken uzun aralık beklemek kataloğu günlerce yarım bırakıyordu.
        sleeps: list = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp,
                code_source=lambda: [f"61091000{i:02d}" for i in range(4)],
                fill_enabled=True,
                delay_seconds=0,
                fill_batch=1,
            )
            with unittest.mock.patch.object(a2m.asyncio, "sleep", _fake_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(engine.periodic_fill_loop(initial_delay=0))
        # sleeps[0] açılış gecikmesi; sonrakiler tur arası beklemeler.
        self.assertEqual(sleeps[1], a2m.A2M_FILL_BUSY_SECONDS)

    def test_the_loop_rests_long_once_the_queue_is_empty(self) -> None:
        sleeps: list = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, code_source=lambda: [], fill_enabled=True, delay_seconds=0)
            with unittest.mock.patch.object(a2m.asyncio, "sleep", _fake_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(engine.periodic_fill_loop(initial_delay=0))
        self.assertEqual(sleeps[1], a2m.A2M_FILL_INTERVAL_SECONDS)

    def test_a_failing_code_does_not_take_the_round_down(self) -> None:
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "99999999" in str(request.url):
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=_tariff_body())

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp,
                transport=httpx.MockTransport(handler),
                code_source=lambda: ["6109100000", "999999990000", "6109901000"],
                fill_enabled=True,
                delay_seconds=0,
            )
            report = asyncio.run(engine.fill_once())
        self.assertEqual(report["ok"], 2)
        self.assertEqual(report["failed"], 1)


class ComparisonTests(unittest.TestCase):
    def test_formatting_differences_are_not_counted_as_disagreement(self) -> None:
        verdict = a2m.compare_summaries({"mfn_rate": "12.00 %"}, {"mfn_rate": "12%"})
        self.assertTrue(verdict["agree"])
        self.assertTrue(verdict["fields"]["mfn_rate"]["match"])

    def test_a_real_difference_is_reported(self) -> None:
        verdict = a2m.compare_summaries({"mfn_rate": "12.00 %"}, {"mfn_rate": "6.5%"})
        self.assertFalse(verdict["agree"])

    def test_one_sided_data_is_a_coverage_gap_not_a_wrong_rate(self) -> None:
        verdict = a2m.compare_summaries({"mfn_rate": "12.00 %"}, {"mfn_rate": None})
        self.assertTrue(verdict["fields"]["mfn_rate"]["coverage_gap"])
        self.assertTrue(verdict["agree"], "eksik veri 'yanlış veri' sayılmaz")

    def test_the_comparison_never_calls_the_paid_actor(self) -> None:
        class _PaidArchiveOnly:
            """Aktörü çağıran her yol patlar: karşılaştırma ücret doğurursa test kırmızı olur."""

            def _stored_pairs(self):
                return {("6109100000", "TR")}

            def archived(self, code, partner):
                return {"summary": {"mfn_rate": "12.00 %", "partner_rate": "0.00 %", "rate_status": "conditional"}}

            async def lookup(self, *args, **kwargs):  # pragma: no cover - çağrılmamalı
                raise AssertionError("karşılaştırma ücretli sorgu yapmamalı")

            async def fill_once(self, *args, **kwargs):  # pragma: no cover - çağrılmamalı
                raise AssertionError("karşılaştırma dolum tetiklememeli")

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp)
            report = asyncio.run(a2m.compare_sources(_PaidArchiveOnly(), engine, limit=5))
        self.assertEqual(report["compared"], 1)
        self.assertEqual(report["agree"], 1)
        self.assertEqual(report["agreement_rate"], 1.0)
        self.assertEqual(report["cost_usd"], 0.0)

    def test_a_code_the_free_source_cannot_serve_is_counted_separately(self) -> None:
        class _Paid:
            def _stored_pairs(self):
                return {("9999999999", "TR")}

            def archived(self, code, partner):
                return {"summary": {"mfn_rate": "12.00 %"}}

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(tariff=[]))
            report = asyncio.run(a2m.compare_sources(_Paid(), engine, limit=5))
        self.assertEqual(report["free_missing"], 1)
        self.assertEqual(report["compared"], 0)
        self.assertIsNone(report["agreement_rate"])


class StatusTests(unittest.TestCase):
    def test_the_status_reports_the_archive_and_the_zero_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp)
            asyncio.run(engine.lookup("6109100000"))
            status = engine.status()
        self.assertEqual(status["archived"], 1)
        self.assertEqual(status["archived_ok"], 1)
        self.assertEqual(status["fill"]["cost_usd"], 0.0)
        self.assertIn("trade.ec.europa.eu", status["base_url"])


class RetryTests(unittest.TestCase):
    """Geçici kodlarda (429/5xx) yeniden denenir; kalıcı kodlarda denenmez.

    Canlı ölçüm sunucunun aralıklı hata aldığını, aynı isteklerin başka bir ağdan
    %100 başarılı olduğunu gösterdi. Teşhisin kör kalmaması için durum kodu hata
    metnine yazılır ve geçici kodlar kısa bir geri çekilmeyle tekrarlanır.
    """

    def _counting_transport(self, status: int, calls: list) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(status)
            if len(calls) >= 3:
                return httpx.Response(200, json=_tariff_body())
            return httpx.Response(status, text="busy")

        return httpx.MockTransport(handler)

    def test_a_rate_limit_is_not_retried_inline(self) -> None:
        """429 tur içinde yeniden DENENMEZ — canlı ölçüm bunun zarar verdiğini gösterdi.

        Yeniden deneme eklendiğinde her kod 3 deneme × geri çekilme ile ~30 saniyeye
        çıktı, tur saatlerce sürdü, hiçbir şey kaydedilmedi ve hız ayarı tur bitene
        kadar güncellenmediği için sistem kendini düzeltemedi. Doğrusu: hemen yavaşla,
        kodu bırak, bir sonraki turda yeniden dene.
        """
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=self._counting_transport(429, calls))
            result = asyncio.run(engine.lookup("6109100000", with_extras=False))
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(len(calls), 1, "429'da ısrar etmek sınırı daha da kapatır")
            self.assertTrue(engine._rate_limited)
            self.assertGreater(engine._delay_seconds, 0.5, "429 anında yavaşlatmalı")
            self.assertEqual(engine._effective_concurrency, 1)

    def test_a_server_error_is_retried(self) -> None:
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=self._counting_transport(503, calls))
            result = asyncio.run(engine.lookup("6109100000", with_extras=False))
        self.assertEqual(result.status, "ok")

    def test_a_cooldown_window_holds_every_worker_not_just_one(self) -> None:
        # Tek tek beklemek, diğer işçilerin aynı anda sınırı zorlamasını engellemiyordu.
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport())
            engine._cooldown_until = 0.0
            engine._note_rate_limit(SimpleNamespace(headers={"retry-after": "3"}))
            first = engine._cooldown_until
            engine._note_rate_limit(SimpleNamespace(headers={}))
        self.assertGreater(first, 0.0)
        self.assertGreaterEqual(engine._cooldown_until, first)

    def test_an_ordinary_server_error_is_still_retried(self) -> None:
        # 429 dışındaki geçici hatalarda yeniden deneme hâlâ doğru davranış.
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=self._counting_transport(503, calls))
            result = asyncio.run(engine.lookup("6109100000", with_extras=False))
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(calls), 3)

    def test_a_permanent_error_is_not_retried(self) -> None:
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(403)
            return httpx.Response(403, text="forbidden")

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=httpx.MockTransport(handler))
            result = asyncio.run(engine.lookup("6109100000", with_extras=False))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(len(calls), 1, "403 geçici değildir; kaynağı boşuna zorlamayız")

    def test_the_status_code_reaches_the_error_line(self) -> None:
        # "HTTPStatusError" tek başına 403 mü 429 mu söylemiyordu; teşhis kör kalıyordu.
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, transport=_transport(fail=True))
            asyncio.run(engine.lookup("6109100000", with_extras=False))
            status = engine.status()
        self.assertTrue(any("503" in line for line in status["errors"]), status["errors"])

    def test_retry_after_is_obeyed_when_the_source_sends_one(self) -> None:
        response = SimpleNamespace(headers={"retry-after": "7"})
        self.assertEqual(a2m._retry_delay(response, 0, base=1.0), 7.0)

    def test_an_unreadable_retry_after_falls_back_to_backoff(self) -> None:
        response = SimpleNamespace(headers={"retry-after": "yarın"})
        self.assertEqual(a2m._retry_delay(response, 1, base=1.0), 2.0)



class AdaptivePaceTests(unittest.TestCase):
    """429 görünce yavaşla, temiz turlarda geri aç.

    Sabit hız canlıda işe yaramadı: dolum 124 kodda tamamen durdu, her istek 429
    aldı. Kaynağın tepkisine göre kendini ayarlamayan bir dolum ya kataloğu hiç
    bitirmez ya da kaynağı boşuna zorlar.
    """

    @staticmethod
    def _no_sleep():
        """Testte gerçekten beklemeyiz; ölçtüğümüz şey süre değil, ayarın kendisi."""

        async def _sleep(_seconds):
            return None

        return unittest.mock.patch.object(a2m.asyncio, "sleep", _sleep)

    def _rate_limited_engine(self, tmp: str, *, always: bool = True):
        state = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            if always:
                return httpx.Response(429, text="slow down")
            return httpx.Response(200, json=_tariff_body())

        return _engine(
            tmp,
            transport=httpx.MockTransport(handler),
            code_source=lambda: ["6109100000", "6109901000"],
            fill_enabled=True,
            delay_seconds=0.5,
            concurrency=3,
        )

    def test_a_rate_limited_round_slows_down_and_drops_to_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._rate_limited_engine(tmp)
            with self._no_sleep():
                report = asyncio.run(engine.fill_once())
        self.assertTrue(report["rate_limited"])
        self.assertEqual(report["concurrency"], 1, "429 görülünce tek işçiye inilir")
        self.assertGreater(report["delay_seconds"], 0.5, "bekleme artmalı")

    def test_the_delay_doubles_but_never_passes_the_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._rate_limited_engine(tmp)
            with self._no_sleep():
                for _ in range(12):
                    asyncio.run(engine.fill_once())
        self.assertLessEqual(engine._delay_seconds, a2m.A2M_MAX_DELAY_SECONDS)

    def test_clean_rounds_open_the_pace_back_up(self) -> None:
        # Kalıcı olarak en yavaş ayara mahkûm kalmak da kataloğu bitirmezdi.
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._rate_limited_engine(tmp)
            with self._no_sleep():
                asyncio.run(engine.fill_once())
                self.assertEqual(engine._effective_concurrency, 1)
                engine._http = httpx.AsyncClient(transport=_transport(), follow_redirects=False)
                for _ in range(a2m.A2M_PACE_RECOVER_ROUNDS * 3):
                    asyncio.run(engine.fill_once())
        self.assertGreater(engine._effective_concurrency, 1, "temiz turlarda hız geri açılmalı")

    def test_a_rate_limit_waits_longer_than_an_ordinary_retry(self) -> None:
        # "Yavaşla" diyen bir sunucuya 1 saniye sonra dönmek yavaşlamak değildir.
        self.assertGreater(a2m._RATE_LIMIT_BASE_SECONDS, a2m._RETRY_BASE_SECONDS)

    def test_the_status_shows_the_current_pace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._rate_limited_engine(tmp)
            with self._no_sleep():
                asyncio.run(engine.fill_once())
            status = engine.status()
        self.assertEqual(status["fill"]["effective_concurrency"], 1)
        self.assertGreater(status["fill"]["delay_seconds"], 0.5)



class CircuitBreakerTests(unittest.TestCase):
    """Tamamen engellenmiş kaynağı dövmeye devam etme.

    Canlıda hız tavana vurduğu hâlde (30 sn'de bir istek, tek işçi) arşive tek kayıt
    eklenmedi; hepsi 429 döndü. O noktada doğru davranış durmak ve sonra yeniden
    yoklamaktır — ısrar yasağı uzatmaktan başka işe yaramaz.
    """

    @staticmethod
    def _no_sleep():
        async def _sleep(_seconds):
            return None

        return unittest.mock.patch.object(a2m.asyncio, "sleep", _sleep)

    def _blocked_engine(self, tmp: str):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="slow down")

        return _engine(
            tmp,
            transport=httpx.MockTransport(handler),
            code_source=lambda: ["6109100000", "6109901000"],
            fill_enabled=True,
            delay_seconds=0,
        )

    def test_the_fill_pauses_after_repeated_fully_blocked_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._blocked_engine(tmp)
            with self._no_sleep():
                for _ in range(a2m.A2M_BLOCK_ROUNDS):
                    report = asyncio.run(engine.fill_once())
                self.assertEqual(report["ok"], 0)
                paused = asyncio.run(engine.fill_once())
        self.assertEqual(paused["status"], "paused")
        self.assertGreater(paused["paused_seconds"], 0)

    def test_a_paused_fill_sends_no_request_at_all(self) -> None:
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(429, text="slow down")

        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp,
                transport=httpx.MockTransport(handler),
                code_source=lambda: ["6109100000"],
                fill_enabled=True,
                delay_seconds=0,
            )
            with self._no_sleep():
                for _ in range(a2m.A2M_BLOCK_ROUNDS):
                    asyncio.run(engine.fill_once())
                before = len(calls)
                asyncio.run(engine.fill_once())
        self.assertEqual(len(calls), before, "duraklatılmış dolum kaynağa dokunmamalı")

    def test_one_successful_lookup_lifts_the_pause(self) -> None:
        # Kullanıcı sorgusu yolu açık kalır ve yasağın kalkıp kalkmadığını ölçen
        # sonda görevi görür; tek bir başarı dolumu geri açar.
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._blocked_engine(tmp)
            with self._no_sleep():
                for _ in range(a2m.A2M_BLOCK_ROUNDS):
                    asyncio.run(engine.fill_once())
                self.assertEqual(asyncio.run(engine.fill_once())["status"], "paused")
                engine._http = httpx.AsyncClient(transport=_transport(), follow_redirects=False)
                result = asyncio.run(engine.lookup("6109100000", with_extras=False))
                self.assertEqual(result.status, "ok")
                resumed = asyncio.run(engine.fill_once())
        self.assertNotEqual(resumed["status"], "paused")

    def test_a_productive_round_never_arms_the_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(
                tmp, code_source=lambda: ["6109100000"], fill_enabled=True, delay_seconds=0
            )
            with self._no_sleep():
                for _ in range(a2m.A2M_BLOCK_ROUNDS + 2):
                    report = asyncio.run(engine.fill_once())
        self.assertNotEqual(report["status"], "paused")
        self.assertEqual(engine._fill_paused_until, 0.0)



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
