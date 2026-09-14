"""Deterministic 22+ step import workflow derived from precheck results (PRD Faz 2.4)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from control_engine import ControlScopeRow, ImportControlLookupResult, ImportControlMatch, ImportControlRule
from customs_advisor import CustomsInquiry, CustomsPrecheckResult, ExpertReviewPacket
from customs_workflow import WORKFLOW_VERSION, WorkflowStep, build_workflow, workflow_summary
from origin_documents import origin_document_requirements
from tariff_engine import MeasureCoverage, TariffLookupResult

AS_OF = "2026-09-14T10:00:00+03:00"


def packet(**overrides: Any) -> ExpertReviewPacket:
    base: dict[str, Any] = {
        "risk_level": "moderate", "escalation_required": False, "generated_at": AS_OF, "legal_notice": "not",
    }
    base.update(overrides)
    return ExpertReviewPacket(**base)


def inquiry(**overrides: Any) -> CustomsInquiry:
    base: dict[str, Any] = {"question": "Bu ürünün gümrük durumu nedir?"}
    base.update(overrides)
    return CustomsInquiry(**base)


def coverage(**statuses: str) -> dict[str, MeasureCoverage]:
    return {key: MeasureCoverage(status=value, note=f"{key} notu") for key, value in statuses.items()}  # type: ignore[arg-type]


def hit(kind: str, country: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "measure_type": kind, "matched_code": "7318.15", "country": country, "origin_match": True,
        "rate_text": "0,35 USD/kg", "unit_value_usd": None, "unit": None, "product": "civata", "legal_act": "2024/12",
        "gazette": "2024-05-01", "expires": None, "status": "in_force", "notes": "", "source": "", "provenance": None,
    }
    base.update(overrides)
    return base


def tariff(gtip: str, origin: str, **overrides: Any) -> TariffLookupResult:
    base: dict[str, Any] = {
        "status": "matched", "gtip": gtip, "matched_gtips": [gtip], "matched_gtip_count": 1, "origin_country": origin,
        "unambiguous_rates": {"customs_duty": 3.7, "additional_duty": 0.0, "additional_financial_liability": 0.0},
        "resolved_country_group": "DÜ", "as_of": AS_OF,
        "measure_coverage": coverage(customs_duty="verified_snapshot", additional_duty="verified_snapshot",
                                     additional_financial_liability="partial_snapshot", anti_dumping="partial_snapshot",
                                     surveillance="partial_snapshot", safeguard="partial_snapshot", tariff_quota="partial_snapshot"),
        "trade_measures": {"gtip": gtip, "origin_country": origin, "as_of": AS_OF, "anti_dumping": [], "safeguard": [],
                           "surveillance": [], "tariff_quota": [], "surveillance_unit_value": None, "sources": {}, "warnings": []},
        "excise_tax": {"gtip": gtip, "in_scope": False, "matches": [], "warnings": [], "legal_basis": "4760"},
    }
    base.update(overrides)
    return TariffLookupResult(**base)


def result(inq: CustomsInquiry, **overrides: Any) -> CustomsPrecheckResult:
    base: dict[str, Any] = {
        "status": "preliminary", "as_of": AS_OF, "summary": "özet", "legal_notice": "not", "inquiry": inq,
        "expert_review_packet": packet(),
    }
    base.update(overrides)
    return CustomsPrecheckResult(**base)


def step(steps: list[WorkflowStep], step_id: str) -> WorkflowStep:
    found = [item for item in steps if item.id == step_id]
    assert found, f"{step_id} adımı yok"
    return found[0]


def full_inquiry(**overrides: Any) -> CustomsInquiry:
    base: dict[str, Any] = dict(
        product_description="Paslanmaz çelik altıgen başlı civata M8", composition="Paslanmaz çelik",
        function_mechanism="Mekanik bağlantı elemanı", declared_product_type="civata",
        candidate_gtip="731815900011", tariff_selection_confirmed=True, exact_gtip_confirmed=True,
        classification_verification_status="dual_agreement", classification_confidence_score=90,
        origin_country="Almanya", dispatch_country="Almanya", atr_certificate=True,
        invoice_value=10000, freight=500, insurance=50, incoterm="FOB", payment_method="Peşin", currency="EUR",
        exchange_rate=36.5, exchange_rate_date="2026-09-10", vat_rate=20, kkdf_rate=0, sct_amount=0,
        stamp_duty_try=1500, port_storage_try=8000, gekap_try=0, trt_bandrol_rate=0,
    )
    base.update(overrides)
    return inquiry(**base)


class WorkflowShapeTests(unittest.TestCase):
    def test_step_order_and_ids_are_stable(self) -> None:
        steps = build_workflow(result(inquiry()))
        self.assertEqual([s.order for s in steps], list(range(1, len(steps) + 1)))
        self.assertGreaterEqual(len(steps), 22)
        self.assertEqual(steps[0].id, "product_definition")
        self.assertEqual(steps[-1].id, "expert_handoff")
        ids = [s.id for s in steps]
        self.assertEqual(len(ids), len(set(ids)))
        for expected in ("candidate_gtip", "gtip12_selection", "atr_certificate", "preferential_origin_proof",
                         "customs_value", "exchange_rate", "customs_duty", "additional_duty", "financial_liability",
                         "anti_dumping", "safeguard_quota", "surveillance", "sct", "vat", "kkdf", "tareks",
                         "prohibited_lists", "other_agency_permits", "document_checklist", "pre_declaration_payments"):
            self.assertIn(expected, ids)

    def test_empty_result_blocks_inputs_and_is_deterministic(self) -> None:
        empty = result(inquiry())
        first = build_workflow(empty)
        second = build_workflow(empty)
        self.assertEqual([s.model_dump() for s in first], [s.model_dump() for s in second])
        self.assertEqual(step(first, "product_definition").status, "blocked")
        self.assertEqual(step(first, "customs_value").status, "blocked")
        self.assertEqual(step(first, "origin_dispatch").status, "blocked")
        self.assertEqual(step(first, "vat").status, "pending")
        for item in first:
            if item.status in {"pending", "blocked"}:
                self.assertTrue(item.next_action, f"{item.id} için next_action yok")
            if item.status == "done":
                self.assertIsNone(item.next_action)
        summary = workflow_summary(first)
        self.assertEqual(summary["done"], 1)  # expert handoff: no escalation reason recorded
        self.assertLess(summary["completion_ratio"], 0.1)
        self.assertEqual(summary["version"], WORKFLOW_VERSION)

    def test_dict_input_matches_model_input(self) -> None:
        model_result = result(full_inquiry(), tariff_lookup=tariff("731815900011", "Almanya"))
        from_model = build_workflow(model_result)
        from_dict = build_workflow(model_result.model_dump(mode="json"))
        self.assertEqual([s.model_dump() for s in from_model], [s.model_dump() for s in from_dict])
        # Old dossiers may lack keys entirely; a minimal dict must still produce every step.
        minimal = build_workflow({"inquiry": {"question": "x"}})
        self.assertEqual(len(minimal), len(from_model))


class WorkflowBranchTests(unittest.TestCase):
    def test_eu_origin_with_atr_marks_eur1_not_applicable(self) -> None:
        inq = full_inquiry()
        docs = origin_document_requirements("Almanya", dispatch_country="Almanya", gtip=inq.candidate_gtip)
        steps = build_workflow(result(inq, tariff_lookup=tariff("731815900011", "Almanya", resolved_country_group="AB"),
                                      origin_documents=docs))
        self.assertEqual(step(steps, "atr_certificate").status, "done")
        eur1 = step(steps, "preferential_origin_proof")
        self.assertEqual(eur1.status, "not_applicable")
        self.assertIn("A.TR", eur1.summary)
        self.assertEqual(step(steps, "origin_dispatch").status, "done")

    def test_eu_origin_without_atr_answer_keeps_atr_pending(self) -> None:
        inq = full_inquiry(atr_certificate=None)
        docs = origin_document_requirements("Almanya", dispatch_country="Almanya", gtip=inq.candidate_gtip)
        steps = build_workflow(result(inq, origin_documents=docs))
        self.assertEqual(step(steps, "atr_certificate").status, "pending")
        self.assertIn("işaretleyin", step(steps, "atr_certificate").next_action or "")

    def test_fta_origin_requires_preferential_proof(self) -> None:
        inq = full_inquiry(origin_country="Güney Kore", dispatch_country="Güney Kore", atr_certificate=None)
        docs = origin_document_requirements("Güney Kore", gtip=inq.candidate_gtip)
        steps = build_workflow(result(inq, origin_documents=docs))
        self.assertEqual(step(steps, "atr_certificate").status, "not_applicable")
        proof = step(steps, "preferential_origin_proof")
        self.assertEqual(proof.status, "pending")
        self.assertIn("menşe", proof.summary.casefold())

    def test_chinese_origin_with_dumping_match_is_pending_then_done(self) -> None:
        inq = full_inquiry(origin_country="Çin", dispatch_country="Çin", atr_certificate=None)
        lookup = tariff("731815900011", "Çin", trade_measures={
            "gtip": "731815900011", "origin_country": "Çin", "as_of": AS_OF,
            "anti_dumping": [hit("anti_dumping", "Çin Halk Cumhuriyeti"), hit("anti_dumping", "Tayvan", origin_match=False)],
            "safeguard": [], "surveillance": [], "tariff_quota": [], "surveillance_unit_value": None, "sources": {}, "warnings": [],
        })
        docs = origin_document_requirements("Çin", gtip=inq.candidate_gtip)
        steps = build_workflow(result(inq, tariff_lookup=lookup, origin_documents=docs))
        dumping = step(steps, "anti_dumping")
        self.assertEqual(dumping.status, "pending")
        self.assertIn("1 önlem satırı", dumping.summary)
        self.assertIn("Çin Halk Cumhuriyeti", dumping.summary)
        self.assertIn("inquiry.anti_dumping_amount", dumping.evidence_refs)
        self.assertEqual(step(steps, "safeguard_quota").status, "not_applicable")
        self.assertEqual(step(steps, "preferential_origin_proof").status, "not_applicable")

        confirmed = build_workflow(result(inq.model_copy(update={"anti_dumping_amount": 1200.0}), tariff_lookup=lookup,
                                          origin_documents=docs))
        self.assertEqual(step(confirmed, "anti_dumping").status, "done")

    def test_no_dumping_match_is_not_applicable(self) -> None:
        steps = build_workflow(result(full_inquiry(), tariff_lookup=tariff("731815900011", "Almanya")))
        self.assertEqual(step(steps, "anti_dumping").status, "not_applicable")
        self.assertEqual(step(steps, "surveillance").status, "not_applicable")
        self.assertEqual(step(steps, "sct").status, "done")  # user entered 0

    def test_missing_value_blocks_value_rate_and_payment_steps(self) -> None:
        inq = full_inquiry(invoice_value=None, freight=None, insurance=None, exchange_rate=None, exchange_rate_date=None,
                           stamp_duty_try=None, port_storage_try=None)
        steps = build_workflow(result(inq, tariff_lookup=tariff("731815900011", "Almanya"),
                                      missing_information=["Fatura bedeli"]))
        value = step(steps, "customs_value")
        self.assertEqual(value.status, "blocked")
        self.assertIn("Fatura bedeli", value.next_action or "")
        self.assertEqual(step(steps, "exchange_rate").status, "blocked")
        self.assertEqual(step(steps, "pre_declaration_payments").status, "blocked")
        self.assertGreaterEqual(workflow_summary(steps)["blocked"], 3)

    def test_rate_steps_follow_official_snapshot_and_ambiguity(self) -> None:
        inq = full_inquiry(atr_certificate=None, origin_country="Çin", dispatch_country="Çin")
        lookup = tariff("731815900011", "Çin", status="partial", matched_gtip_count=3,
                        unambiguous_rates={"customs_duty": 3.7}, ambiguous_measure_types=["additional_duty"],
                        rate_variants={"additional_duty": [0.0, 20.0]})
        steps = build_workflow(result(inq.model_copy(update={"exact_gtip_confirmed": False}), tariff_lookup=lookup))
        self.assertEqual(step(steps, "customs_duty").status, "done")
        igv = step(steps, "additional_duty")
        self.assertEqual(igv.status, "pending")
        self.assertIn("%20", igv.summary)
        self.assertEqual(step(steps, "gtip12_selection").status, "pending")
        self.assertIn("3 alt satır", step(steps, "gtip12_selection").summary)
        # EMY: not in safe rates, partial coverage → pending with coverage note.
        self.assertEqual(step(steps, "financial_liability").status, "pending")

    def test_origin_proof_required_keeps_additional_duty_pending(self) -> None:
        lookup = tariff("731815900011", "Almanya", origin_proof_required=["additional_duty"], fallback_rates={"additional_duty": 20.0})
        steps = build_workflow(result(full_inquiry(), tariff_lookup=lookup))
        igv = step(steps, "additional_duty")
        self.assertEqual(igv.status, "pending")
        self.assertIn("%20", igv.summary)
        self.assertIn("tariff_lookup.origin_proof_required", igv.evidence_refs)

    def test_control_matches_split_into_tareks_prohibited_and_other_agency(self) -> None:
        def rule(code: str, category: str, system: str, authority: str) -> ImportControlRule:
            return ImportControlRule(
                code=code, title=f"{code} tebliği", category=category, authority=authority, system=system, risk_based=True,
                physical_inspection_possible=True, laboratory_test_possible=False, scope_count=10, mevzuat_id="1",
                source_url="https://www.resmigazete.gov.tr/x", document_sha256="a" * 64, retrieved_at=AS_OF, valid_from="2026-01-01",
                snapshot_id="snap",
            )

        def match(rule_: ImportControlRule, *, list_kind: str = "scope") -> ImportControlMatch:
            return ImportControlMatch(
                rule=rule_, matched_scope=ControlScopeRow(gtip_prefix="7318", source_line="7318", source_offset=0, list_kind=list_kind),  # type: ignore[arg-type]
                match_type="prefix", assessment="kapsam",
            )

        control = ImportControlLookupResult(
            status="matched", gtip="731815900011", scope_determination="annex_match", as_of=AS_OF,
            matches=[
                match(rule("2026/1", "sanayi girdileri", "TAREKS", "Ticaret Bakanlığı")),
                match(rule("2026/3", "atıklar", "Çevre, Şehircilik ve İklim Değişikliği Bakanlığı izni", "ÇŞİDB"), list_kind="prohibited"),
                match(rule("2026/5", "tarım ve gıda", "Tarım ve Orman Bakanlığı uygunluk yazısı", "Tarım ve Orman Bakanlığı")),
            ],
        )
        steps = build_workflow(result(full_inquiry(), tariff_lookup=tariff("731815900011", "Almanya"), control_lookup=control,
                                      expert_review_packet=packet(risk_level="critical", escalation_required=True,
                                                                  review_types=["yetkili_kurum"], reasons=["kontrol"])))
        self.assertEqual(step(steps, "tareks").status, "pending")
        self.assertIn("2026/1", step(steps, "tareks").summary)
        self.assertEqual(step(steps, "prohibited_lists").status, "blocked")
        self.assertEqual(step(steps, "other_agency_permits").status, "pending")
        self.assertIn("Tarım ve Orman Bakanlığı", step(steps, "other_agency_permits").summary)
        self.assertEqual(step(steps, "expert_handoff").status, "blocked")

    def test_controls_blocked_until_gtip12_confirmed(self) -> None:
        steps = build_workflow(result(full_inquiry(exact_gtip_confirmed=False)))
        for step_id in ("tareks", "prohibited_lists", "other_agency_permits"):
            self.assertEqual(step(steps, step_id).status, "blocked", step_id)

    def test_complete_result_reaches_high_completion_ratio(self) -> None:
        inq = full_inquiry()
        docs = origin_document_requirements("Almanya", dispatch_country="Almanya", gtip=inq.candidate_gtip)
        control = ImportControlLookupResult(status="not_found", gtip=inq.candidate_gtip or "", scope_determination="no_indexed_match", as_of=AS_OF)
        steps = build_workflow(result(
            inq, tariff_lookup=tariff("731815900011", "Almanya", resolved_country_group="AB"), origin_documents=docs,
            control_lookup=control,
            deterministic_cost={"customs_value_estimate": 10550.0, "lines": [{"code": "kkdf", "rate": 0.0, "amount": 0.0}], "status": "user_rates_complete"},
        ))
        summary = workflow_summary(steps)
        self.assertEqual(summary["blocked"], 0)
        self.assertGreaterEqual(summary["completion_ratio"], 0.85)
        self.assertEqual(summary["pending"], 1)  # document checklist is never auto-verified
        self.assertEqual(step(steps, "document_checklist").status, "pending")
        self.assertEqual(step(steps, "kkdf").status, "done")
        self.assertEqual(step(steps, "exchange_rate").status, "done")


class WorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyse_populates_workflow_and_old_records_validate(self) -> None:
        from customs_advisor import CustomsAdvisor

        class FakeRegistry:
            async def gather(self, inquiry):
                return []

            async def close(self):
                pass

        advisor = CustomsAdvisor(registry=FakeRegistry())
        try:
            with patch("customs_advisor._llm_api_key_value", return_value=""):
                outcome = await advisor.analyse(full_inquiry(candidate_gtip=None, tariff_selection_confirmed=False,
                                                             exact_gtip_confirmed=False))
        finally:
            await advisor.close()
        self.assertEqual(outcome.status, "evidence_only")
        self.assertGreaterEqual(len(outcome.workflow), 22)
        self.assertEqual(outcome.workflow[0].status, "done")
        payload = outcome.model_dump(mode="json")
        self.assertIn("workflow", payload)
        payload.pop("workflow")
        legacy = CustomsPrecheckResult.model_validate(payload)
        self.assertEqual(legacy.workflow, [])
        self.assertEqual(len(build_workflow(legacy)), len(outcome.workflow))


if __name__ == "__main__":
    unittest.main()
