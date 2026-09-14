"""Deterministic interactive decision questions (PRD Faz 2.3)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
from customs_advisor import CustomsInquiry
from decision_questions import (
    DecisionQuestion,
    apply_decision_answers,
    build_decision_questions,
)
from tariff_engine import LandedCostInput, MeasureCoverage, TariffLookupResult

AS_OF = "2026-09-14T10:00:00+03:00"
PUBLIC_ORIGIN = "https://gumruksor.com"


def coverage(**statuses: str) -> dict[str, MeasureCoverage]:
    return {key: MeasureCoverage(status=value, note=f"{key} notu") for key, value in statuses.items()}  # type: ignore[arg-type]


def tariff(**overrides: Any) -> TariffLookupResult:
    base: dict[str, Any] = {
        "status": "matched",
        "gtip": "731815900000",
        "matched_gtips": ["731815900000"],
        "matched_gtip_count": 1,
        "origin_country": "Almanya",
        "unambiguous_rates": {"customs_duty": 3.7},
        "resolved_country_group": "AB",
        "as_of": AS_OF,
        "measure_coverage": coverage(customs_duty="verified_snapshot"),
    }
    base.update(overrides)
    return TariffLookupResult(**base)


def trade(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "gtip": "731815900000",
        "origin_country": "Çin",
        "as_of": AS_OF,
        "anti_dumping": [],
        "safeguard": [],
        "surveillance": [],
        "tariff_quota": [],
        "surveillance_unit_value": None,
        "sources": {},
        "warnings": [],
    }
    base.update(overrides)
    return base


def hit(measure_type: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "measure_type": measure_type,
        "matched_code": "7318.15",
        "country": "Çin",
        "origin_match": True,
        "rate_text": "3,5",
        "unit_value_usd": 3.5,
        "unit": "USD/kg",
        "product": "civata",
        "legal_act": "2024/12 sayılı Tebliğ",
        "gazette": "2024-05-01",
        "expires": None,
        "status": "in_force",
        "notes": "",
        "source": "",
        "provenance": None,
    }
    base.update(overrides)
    return base


def inquiry(**overrides: Any) -> CustomsInquiry:
    base: dict[str, Any] = {"question": "Bu eşyanın ithalat maliyeti nedir?"}
    base.update(overrides)
    return CustomsInquiry(**base)


def ids(questions: list[DecisionQuestion]) -> list[str]:
    return [item.id for item in questions]


def by_id(questions: list[DecisionQuestion], question_id: str) -> DecisionQuestion:
    for item in questions:
        if item.id == question_id:
            return item
    raise AssertionError(f"{question_id} sorusu üretilmedi: {ids(questions)}")


AMBIGUOUS_VAT = {
    "gtip": "300490",
    "basis": "official_list",
    "ambiguous": True,
    "rate": None,
    "legal_basis": "2007/13033 s. BKK II sayılı liste",
    "row_text": "Beşerî tıbbi ürünler",
    "candidates": [
        {"rate": 10.0, "conditions": ["ruhsatlı beşerî tıbbi ürün"], "legal_basis": "II sayılı liste 17. sıra",
         "matched_expression": "3004", "coverage": "full", "row_text": "Beşerî tıbbi ürünler", "verified": True},
        {"rate": 20.0, "conditions": [], "legal_basis": "Genel oran – satırdaki şart sağlanmazsa",
         "matched_expression": None, "coverage": "conditional", "row_text": None, "verified": True},
    ],
}

SINGLE_VAT = {
    "gtip": "731815",
    "basis": "official_list",
    "ambiguous": False,
    "rate": 20.0,
    "legal_basis": "3065 s. Kanun md. 28 – genel oran",
    "matched_expression": "7318",
    "conditions": [],
    "candidates": [],
    "row_text": None,
}


class BuildDecisionQuestionTests(unittest.TestCase):
    def test_ambiguous_vat_produces_a_condition_question_with_rate_options(self) -> None:
        questions = build_decision_questions(
            gtip="300490900000", tariff_lookup=tariff(), vat_lookup=AMBIGUOUS_VAT, inquiry=inquiry()
        )
        question = by_id(questions, "vat_rate")
        self.assertIn("koşulunu karşılıyor mu", question.text)
        self.assertIn("ruhsatlı beşerî tıbbi ürün", question.text)
        self.assertEqual(["10", "20"], [option.value for option in question.options])
        self.assertEqual(["vat_rate"], question.affects)
        self.assertIsNone(question.default)
        self.assertIn("2007/13033", question.legal_basis or "")
        self.assertNotIn("vat_rate_confirm", ids(questions))

    def test_single_vat_suggestion_becomes_a_confirmation_question(self) -> None:
        questions = build_decision_questions(
            gtip="731815900000", tariff_lookup=tariff(), vat_lookup=SINGLE_VAT, inquiry=inquiry()
        )
        question = by_id(questions, "vat_rate_confirm")
        self.assertEqual(["20", "reddet"], [option.value for option in question.options])
        self.assertIn("%20", question.text)
        self.assertEqual(["vat_rate"], question.affects)

    def test_answered_vat_rate_produces_no_vat_question(self) -> None:
        for vat in (AMBIGUOUS_VAT, SINGLE_VAT):
            with self.subTest(vat=vat["gtip"]):
                questions = build_decision_questions(
                    gtip="731815900000", tariff_lookup=tariff(), vat_lookup=vat, inquiry=inquiry(vat_rate=10)
                )
                self.assertNotIn("vat_rate", ids(questions))
                self.assertNotIn("vat_rate_confirm", ids(questions))

    def test_surveillance_hit_asks_for_the_certificate(self) -> None:
        report = trade(surveillance=[hit("surveillance")], surveillance_unit_value=3.5)
        questions = build_decision_questions(gtip="731815900000", tariff_lookup=tariff(), trade_measures=report, inquiry=inquiry())
        question = by_id(questions, "surveillance_certificate")
        self.assertEqual(["true", "false"], [option.value for option in question.options])
        self.assertEqual(["has_surveillance_certificate"], question.affects)
        self.assertIn("3,5", question.text.replace(".", ","))
        answered = build_decision_questions(
            gtip="731815900000", tariff_lookup=tariff(), trade_measures=report,
            inquiry=inquiry(has_surveillance_certificate=True),
        )
        self.assertNotIn("surveillance_certificate", ids(answered))

    def test_expired_surveillance_and_foreign_origin_hits_are_ignored(self) -> None:
        report = trade(surveillance=[hit("surveillance", status="expired"), hit("surveillance", origin_match=False)])
        questions = build_decision_questions(gtip="731815900000", tariff_lookup=tariff(), trade_measures=report, inquiry=inquiry())
        self.assertNotIn("surveillance_certificate", ids(questions))

    def test_unknown_payment_method_asks_the_kkdf_question(self) -> None:
        questions = build_decision_questions(gtip="731815900000", tariff_lookup=tariff(), inquiry=inquiry())
        question = by_id(questions, "payment_method")
        self.assertEqual(["pesin", "vadeli"], [option.value for option in question.options])
        self.assertEqual(["payment_method", "kkdf_rate"], question.affects)
        for answered in (inquiry(payment_method="Peşin"), inquiry(payment_method="Vadeli akreditif"), inquiry(kkdf_rate=6)):
            with self.subTest(answered=answered.payment_method or answered.kkdf_rate):
                self.assertNotIn("payment_method", ids(build_decision_questions(gtip="7318", tariff_lookup=tariff(), inquiry=answered)))

    def test_atr_question_only_when_the_route_is_available_and_unanswered(self) -> None:
        lookup = tariff(atr_available=True, dispatch_country="Almanya")
        question = by_id(build_decision_questions(gtip="7318", tariff_lookup=lookup, inquiry=inquiry()), "atr_certificate")
        self.assertEqual(["atr_certificate"], question.affects)
        self.assertIn("A.TR", question.text)
        self.assertNotIn(
            "atr_certificate",
            ids(build_decision_questions(gtip="7318", tariff_lookup=lookup, inquiry=inquiry(atr_certificate=True))),
        )
        self.assertNotIn(
            "atr_certificate",
            ids(build_decision_questions(gtip="7318", tariff_lookup=tariff(atr_available=False), inquiry=inquiry())),
        )

    def test_origin_proof_requirement_asks_for_the_supplier_declaration(self) -> None:
        lookup = tariff(origin_proof_required=["customs_duty", "additional_duty"], fallback_rates={"customs_duty": 3.7})
        question = by_id(build_decision_questions(gtip="7318", tariff_lookup=lookup, inquiry=inquiry()), "origin_proof")
        self.assertIn("menşe tevsiki", question.text)
        self.assertIn("gümrük vergisi", question.text)
        self.assertEqual(["origin_proof_required"], question.affects)
        self.assertNotIn("origin_proof", ids(build_decision_questions(gtip="7318", tariff_lookup=tariff(), inquiry=inquiry())))

    def test_tariff_quota_match_asks_for_the_quota_certificate(self) -> None:
        report = trade(tariff_quota=[hit("tariff_quota", product="süt tozu", legal_act="2025/3 s. Karar")])
        question = by_id(
            build_decision_questions(gtip="0402", tariff_lookup=tariff(), trade_measures=report, inquiry=inquiry()),
            "tariff_quota_certificate",
        )
        self.assertIn("süt tozu", question.text)
        self.assertEqual("2025/3 s. Karar", question.legal_basis)
        self.assertNotIn(
            "tariff_quota_certificate",
            ids(build_decision_questions(gtip="0402", tariff_lookup=tariff(), trade_measures=trade(), inquiry=inquiry())),
        )

    def test_used_goods_question_disappears_once_the_condition_is_declared(self) -> None:
        question = by_id(build_decision_questions(gtip="8471", tariff_lookup=tariff(), inquiry=inquiry()), "used_goods")
        self.assertEqual(["condition"], question.affects)
        for condition in ("new", "used"):
            with self.subTest(condition=condition):
                questions = build_decision_questions(gtip="8471", tariff_lookup=tariff(), inquiry=inquiry(condition=condition))
                self.assertNotIn("used_goods", ids(questions))

    def test_fully_answered_inquiry_leaves_only_untouched_topics(self) -> None:
        lookup = tariff(atr_available=True, vat_rate=SINGLE_VAT, trade_measures=trade(surveillance=[hit("surveillance")]))
        answered = inquiry(
            vat_rate=20, payment_method="Peşin", atr_certificate=True,
            has_surveillance_certificate=False, condition="new",
        )
        self.assertEqual([], build_decision_questions(gtip="731815900000", tariff_lookup=lookup, inquiry=answered))

    def test_lookup_payload_supplies_vat_and_trade_measures_without_extra_arguments(self) -> None:
        lookup = tariff(vat_rate=AMBIGUOUS_VAT, trade_measures=trade(surveillance=[hit("surveillance")]))
        questions = ids(build_decision_questions(gtip="731815900000", tariff_lookup=lookup, inquiry=inquiry()))
        self.assertIn("vat_rate", questions)
        self.assertIn("surveillance_certificate", questions)

    def test_questions_are_unique_and_serialise_as_json(self) -> None:
        lookup = tariff(atr_available=True, origin_proof_required=["customs_duty"], vat_rate=AMBIGUOUS_VAT)
        questions = build_decision_questions(gtip="731815900000", tariff_lookup=lookup, inquiry=inquiry())
        self.assertEqual(len(questions), len(set(ids(questions))))
        payload = [item.model_dump(mode="json") for item in questions]
        self.assertTrue(all({"id", "text", "options", "affects", "reason"} <= set(item) for item in payload))
        self.assertTrue(all("731815900000" in item["reason"] for item in payload))


class ApplyDecisionAnswerTests(unittest.TestCase):
    def test_answers_map_onto_the_cost_input_fields(self) -> None:
        applied = apply_decision_answers(
            {
                "vat_rate": "10",
                "payment_method": "vadeli",
                "surveillance_certificate": "false",
                "atr_certificate": "true",
            },
            {"invoice_value": 1000},
        )
        self.assertEqual(10.0, applied["vat_rate"])
        self.assertEqual(6.0, applied["kkdf_rate"])
        self.assertEqual("Vadeli / kredili", applied["payment_method"])
        self.assertIs(False, applied["has_surveillance_certificate"])
        self.assertIs(True, applied["atr_certificate"])
        self.assertEqual(1000, applied["invoice_value"])

    def test_cash_payment_sets_the_zero_kkdf_the_user_chose(self) -> None:
        applied = apply_decision_answers({"payment_method": "pesin"}, {})
        self.assertEqual(0.0, applied["kkdf_rate"])
        self.assertEqual("Peşin", applied["payment_method"])

    def test_unknown_ids_values_and_empty_answers_are_ignored(self) -> None:
        applied = apply_decision_answers(
            {
                "unknown_question": "20",
                "vat_rate": "yüzde on",
                "vat_rate_confirm": "reddet",
                "surveillance_certificate": "belki",
                "payment_method": "takas",
                "atr_certificate": "",
            },
            {"invoice_value": 500},
        )
        self.assertEqual({"invoice_value": 500}, applied)

    def test_out_of_range_rate_and_non_dict_answers_change_nothing(self) -> None:
        self.assertEqual({}, apply_decision_answers({"vat_rate": "180"}, {}))
        self.assertEqual({"vat_rate": None}, apply_decision_answers(["vat_rate"], {"vat_rate": None}))  # type: ignore[arg-type]

    def test_a_value_the_user_already_typed_is_never_overwritten(self) -> None:
        applied = apply_decision_answers(
            {"vat_rate": "10", "payment_method": "vadeli"},
            {"vat_rate": 20.0, "payment_method": "Peşin", "kkdf_rate": 0.0},
        )
        self.assertEqual(20.0, applied["vat_rate"])
        self.assertEqual("Peşin", applied["payment_method"])
        self.assertEqual(0.0, applied["kkdf_rate"])

    def test_the_source_dict_is_not_mutated(self) -> None:
        original = {"invoice_value": 100}
        apply_decision_answers({"vat_rate": "20"}, original)
        self.assertEqual({"invoice_value": 100}, original)

    def test_applied_answers_validate_as_landed_cost_input(self) -> None:
        applied = apply_decision_answers({"vat_rate": "20", "payment_method": "pesin"}, {"invoice_value": 1000})
        applied.pop("atr_certificate", None)
        model = LandedCostInput.model_validate(applied)
        self.assertEqual(20.0, model.vat_rate)
        self.assertEqual(0.0, model.kkdf_rate)


class InquiryDecisionAnswerTests(unittest.TestCase):
    def test_inquiry_accepts_and_cleans_decision_answers(self) -> None:
        item = inquiry(decision_answers={"vat_rate": " 10 ", "used_goods": ""})
        self.assertEqual({"vat_rate": "10"}, item.decision_answers)

    def test_oversized_keys_values_and_counts_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            inquiry(decision_answers={"x" * 61: "10"})
        with self.assertRaises(Exception):
            inquiry(decision_answers={"vat_rate": "1" * 81})
        with self.assertRaises(Exception):
            inquiry(decision_answers={f"q{index}": "true" for index in range(21)})


class TariffCostRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_limiter = web_app.rate_limiter
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.rate_limiter = self.original_limiter

    def post(self, body: dict[str, Any]):
        return self.client.post("/api/tariff/cost", json=body, headers={"Origin": PUBLIC_ORIGIN})

    def engine_payload(self) -> dict[str, Any]:
        lookup = tariff(atr_available=True, vat_rate=AMBIGUOUS_VAT, trade_measures=trade(surveillance=[hit("surveillance")]))
        return {"tariff": lookup.model_dump(mode="json"), "cost": {"status": "partial", "currency": "USD", "lines": []}}

    def test_cost_response_carries_decision_questions(self) -> None:
        with patch.object(web_app.tariff_engine, "calculate", new=AsyncMock(return_value=self.engine_payload())):
            response = self.post({"gtip": "731815900000", "origin_country": "Almanya", "invoice_value": 1000})
        self.assertEqual(200, response.status_code, response.text)
        questions = response.json()["decision_questions"]
        found = {item["id"] for item in questions}
        self.assertIn("vat_rate", found)
        self.assertIn("surveillance_certificate", found)
        self.assertIn("atr_certificate", found)
        self.assertIn("payment_method", found)

    def test_decision_answers_reach_the_cost_input_and_silence_their_question(self) -> None:
        engine = AsyncMock(return_value=self.engine_payload())
        with patch.object(web_app.tariff_engine, "calculate", new=engine):
            response = self.post({
                "gtip": "731815900000",
                "origin_country": "Almanya",
                "invoice_value": 1000,
                "decision_answers": {"vat_rate": "10", "payment_method": "pesin", "atr_certificate": "true"},
            })
        self.assertEqual(200, response.status_code, response.text)
        inputs = engine.await_args.args[2]
        self.assertEqual(10.0, inputs.vat_rate)
        self.assertEqual(0.0, inputs.kkdf_rate)
        self.assertEqual("Peşin", inputs.payment_method)
        self.assertIs(True, engine.await_args.kwargs["atr_certificate"])
        found = {item["id"] for item in response.json()["decision_questions"]}
        self.assertNotIn("vat_rate", found)
        self.assertNotIn("payment_method", found)
        self.assertNotIn("atr_certificate", found)

    def test_unknown_answer_ids_do_not_break_the_forbidden_extra_field_guard(self) -> None:
        engine = AsyncMock(return_value=self.engine_payload())
        with patch.object(web_app.tariff_engine, "calculate", new=engine):
            response = self.post({
                "gtip": "731815900000",
                "origin_country": "Almanya",
                "invoice_value": 1000,
                "decision_answers": {"customs_duty_rate": "0", "unknown": "true"},
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(engine.await_args.args[2].customs_duty_rate)


if __name__ == "__main__":
    unittest.main()
