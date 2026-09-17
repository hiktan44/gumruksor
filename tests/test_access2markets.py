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


def _transport(*, calls: list | None = None, tariff=None, fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        path = request.url.path
        if fail:
            return httpx.Response(503, text="down")
        if "/api/tariffs/get/" in path:
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
