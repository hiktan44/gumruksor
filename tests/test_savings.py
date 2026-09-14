"""Savings ranking across origin scenarios (PRD Faz 2.2)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService
from savings import LEGAL_NOTICE, evaluate_scenarios, rank_savings
from scenarios import build_origin_scenarios
from tariff_engine import LandedCostInput, TariffLookupResult

PUBLIC_ORIGIN = "https://gumruksor.com"


def cost_input(**overrides: Any) -> LandedCostInput:
    base: dict[str, Any] = {
        "invoice_value": 1000, "vat_rate": 20, "kkdf_rate": 0, "sct_amount": 0, "anti_dumping_amount": 0,
        "surveillance_unit_value": 0, "payment_method": "Peşin", "currency": "USD",
    }
    base.update(overrides)
    return LandedCostInput(**base)


def row(
    origin: str,
    *,
    status: str = "matched",
    rates: dict[str, float] | None = None,
    ambiguous: tuple[str, ...] = (),
    recognised: bool = True,
    atr_available: bool = False,
    atr_free: bool = False,
    proof: tuple[str, ...] = (),
    fallback: dict[str, float] | None = None,
    warnings: tuple[str, ...] = (),
    documents: tuple[str, ...] = ("CERT_ORIGIN",),
    dispatch: str | None = None,
) -> dict[str, Any]:
    return {
        "origin_country": origin,
        "dispatch_country": dispatch,
        "status": status,
        "origin_recognised": recognised,
        "resolved_country_group": "1" if recognised else None,
        "matched_gtip_count": 1,
        "unambiguous_rates": dict(rates or {}),
        "ambiguous_measure_types": list(ambiguous),
        "atr_free_circulation": atr_free,
        "atr_available": atr_available,
        "origin_proof_required": list(proof),
        "fallback_rates": dict(fallback or {}),
        "origin_documents": {
            "origin_country": origin, "regime": "mfn", "regime_name": "test",
            "documents": [{"code": code, "name": code, "applicability": ""} for code in documents],
        },
        "warnings": list(warnings),
    }


ALL_ZERO = {"customs_duty": 0.0, "additional_duty": 0.0, "additional_financial_liability": 0.0}
CHINA = {"customs_duty": 10.0, "additional_duty": 20.0, "additional_financial_liability": 0.0}


class EvaluateScenarioTests(unittest.TestCase):
    def test_ambiguous_measure_is_excluded_with_reason(self) -> None:
        rows = [row("Çin", rates={"customs_duty": 10.0, "additional_financial_liability": 0.0}, ambiguous=("additional_duty",)), row("Almanya", rates=ALL_ZERO)]
        outcomes = evaluate_scenarios(rows, cost_input())
        china = outcomes[0]
        self.assertFalse(china.comparable)
        self.assertTrue(any("İGV" in reason and "değişiyor" in reason for reason in china.reasons_not_comparable))
        ranking = rank_savings(outcomes, "Çin")
        self.assertEqual([item["origin_country"] for item in ranking["ranked"]], ["Almanya"])
        self.assertEqual(ranking["not_comparable"][0]["origin_country"], "Çin")
        self.assertIn("İGV", ranking["not_comparable"][0]["reasons"][0])

    def test_unrecognised_origin_is_excluded(self) -> None:
        rows = [row("Narnia", rates=ALL_ZERO, recognised=False), row("Almanya", rates=ALL_ZERO)]
        outcomes = evaluate_scenarios(rows, cost_input())
        self.assertFalse(outcomes[0].comparable)
        self.assertTrue(any("tanınmadı" in reason for reason in outcomes[0].reasons_not_comparable))
        self.assertTrue(outcomes[1].comparable)

    def test_non_matched_status_is_excluded(self) -> None:
        outcomes = evaluate_scenarios([row("Çin", status="partial", rates=CHINA)], cost_input())
        self.assertFalse(outcomes[0].comparable)
        self.assertIn("kısmi eşleşme", outcomes[0].reasons_not_comparable[0])

    def test_savings_arithmetic_against_baseline(self) -> None:
        outcomes = evaluate_scenarios([row("Çin", rates=CHINA), row("Almanya", rates=ALL_ZERO)], cost_input())
        china, germany = outcomes
        self.assertEqual((china.landed_total, china.total_taxes), (1560.0, 560.0))
        self.assertEqual((germany.landed_total, germany.total_taxes), (1200.0, 200.0))
        ranking = rank_savings(outcomes, "çin")  # harf duyarsız eşleşme
        self.assertEqual([item["origin_country"] for item in ranking["ranked"]], ["Almanya", "Çin"])
        best, base = ranking["ranked"]
        self.assertTrue(base["is_baseline"])
        self.assertEqual(base["savings_vs_baseline"], 0.0)
        self.assertEqual(best["savings_vs_baseline"], 360.0)
        self.assertEqual(best["savings_taxes_vs_baseline"], 360.0)
        self.assertAlmostEqual(best["savings_pct"], 23.08, places=2)
        self.assertEqual(ranking["best"]["origin_country"], "Almanya")
        self.assertEqual(ranking["baseline"]["origin_country"], "Çin")
        self.assertEqual(ranking["legal_notice"], LEGAL_NOTICE)

    def test_without_baseline_the_most_expensive_scenario_is_the_baseline(self) -> None:
        outcomes = evaluate_scenarios([row("Çin", rates=CHINA), row("Almanya", rates=ALL_ZERO)], cost_input())
        ranking = rank_savings(outcomes, None)
        self.assertEqual(ranking["baseline"]["origin_country"], "Çin")
        # Baseline not in the list → same fallback.
        self.assertEqual(rank_savings(outcomes, "Japonya")["baseline"]["origin_country"], "Çin")

    def test_atr_variant_ranks_first_when_duty_drops_to_zero(self) -> None:
        china = {"customs_duty": 10.0, "additional_duty": 0.0, "additional_financial_liability": 0.0}
        india = {"customs_duty": 5.0, "additional_duty": 0.0, "additional_financial_liability": 0.0}
        rows = [
            row("Çin", rates=china, atr_available=True, dispatch="Almanya", documents=("ATR", "CERT_ORIGIN")),
            row("Hindistan", rates=india, dispatch="Almanya"),
        ]
        atr_rows = [row("Çin", rates={**china, "customs_duty": 0.0}, atr_available=True, atr_free=True, dispatch="Almanya", documents=("ATR", "CERT_ORIGIN"))]
        outcomes = evaluate_scenarios(rows, cost_input(), atr_rows=atr_rows)
        self.assertEqual([(item.origin_country, item.variant) for item in outcomes], [("Çin", "base"), ("Çin", "atr"), ("Hindistan", "base")])
        ranking = rank_savings(outcomes, "Çin")
        first = ranking["ranked"][0]
        self.assertEqual((first["origin_country"], first["variant"], first["landed_total"]), ("Çin", "atr", 1200.0))
        self.assertTrue(first["atr_certificate"])
        self.assertTrue(any(text.startswith("A.TR dolaşım belgesi ibrazı") for text in first["conditions"]))
        base = next(item for item in ranking["ranked"] if item["origin_country"] == "Çin" and item["variant"] == "base")
        self.assertFalse(any(text.startswith("A.TR dolaşım belgesi") for text in base["conditions"]))
        self.assertTrue(any("A.TR ile" in text for text in base["warnings"]))
        self.assertEqual(first["savings_vs_baseline"], 120.0)

    def test_atr_variant_is_not_duplicated_when_atr_already_declared(self) -> None:
        rows = [row("Çin", rates=CHINA, atr_available=True, atr_free=True, dispatch="Almanya", documents=("ATR",)), row("Almanya", rates=ALL_ZERO)]
        outcomes = evaluate_scenarios(rows, cost_input(), atr_rows=rows[:1], atr_certificate=True)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(outcomes[0].atr_certificate)
        self.assertTrue(any(text.startswith("A.TR dolaşım belgesi ibrazı") for text in outcomes[0].conditions))

    def test_pessimistic_cost_uses_fallback_rates_when_origin_proof_required(self) -> None:
        rows = [row("Almanya", rates=ALL_ZERO, proof=("additional_duty",), fallback={"additional_duty": 20.0}, documents=("ATR", "SUPPLIER_DECLARATION")), row("Çin", rates=CHINA)]
        outcomes = evaluate_scenarios(rows, cost_input())
        germany = outcomes[0]
        self.assertEqual(germany.landed_total, 1200.0)
        self.assertIsNotNone(germany.cost_pessimistic)
        self.assertEqual(germany.cost_pessimistic.landed_total, 1440.0)
        self.assertTrue(any("tevsik" in text and "İGV %20" in text for text in germany.conditions))
        self.assertTrue(any(text.startswith("Tedarikçi beyanı") for text in germany.conditions))
        ranked = rank_savings(outcomes, "Çin")["ranked"][0]
        self.assertEqual(ranked["landed_total_pessimistic"], 1440.0)
        self.assertEqual(ranked["savings_pessimistic"], 120.0)
        self.assertEqual(ranked["savings_vs_baseline"], 360.0)

    def test_missing_measure_keeps_user_rate_and_notes_it(self) -> None:
        # EMY neither unambiguous nor ambiguous: the user's verified rate is kept, never a silent zero.
        rows = [row("Çin", rates={"customs_duty": 10.0, "additional_duty": 0.0})]
        with_user_rate = evaluate_scenarios(rows, cost_input(additional_financial_liability_rate=5))[0]
        self.assertTrue(with_user_rate.comparable)
        self.assertEqual(with_user_rate.rates_used["additional_financial_liability"], 5.0)
        self.assertEqual(with_user_rate.landed_total, 1380.0)
        self.assertTrue(any("girdiğiniz %5" in text for text in with_user_rate.conditions))
        without_rate = evaluate_scenarios(rows, cost_input())[0]
        self.assertFalse(without_rate.comparable)
        self.assertTrue(any("Ek mali yükümlülük oranı" in reason for reason in without_rate.reasons_not_comparable))

    def test_engine_not_applicable_warning_allows_zero_unless_user_entered_a_rate(self) -> None:
        warning = "Bu GTİP için ek mali yükümlülük (EMY) tespit edilmemiştir; EMY uygulanmaz (%0)."
        rows = [row("Çin", rates={"customs_duty": 10.0, "additional_duty": 0.0}, warnings=(warning,))]
        zero = evaluate_scenarios(rows, cost_input())[0]
        self.assertTrue(zero.comparable)
        self.assertEqual(zero.rates_used["additional_financial_liability"], 0.0)
        self.assertTrue(any("%0 alındı" in text for text in zero.conditions))
        user = evaluate_scenarios(rows, cost_input(additional_financial_liability_rate=5))[0]
        self.assertEqual(user.rates_used["additional_financial_liability"], 5.0)

    def test_official_rate_wins_over_user_rate_with_note(self) -> None:
        outcome = evaluate_scenarios([row("Çin", rates=CHINA)], cost_input(additional_financial_liability_rate=5))[0]
        self.assertEqual(outcome.rates_used["additional_financial_liability"], 0.0)
        self.assertTrue(any("girdiğiniz %5 yerine resmî sütundaki %0" in text for text in outcome.conditions))

    def test_ranking_is_json_serialisable(self) -> None:
        rows = [row("Almanya", rates=ALL_ZERO, proof=("additional_duty",), fallback={"additional_duty": 20.0}), row("Çin", rates=CHINA)]
        ranking = rank_savings(evaluate_scenarios(rows, cost_input()), "Çin")
        payload = json.loads(json.dumps(ranking))
        self.assertEqual(payload["ranked"][0]["cost"]["status"], "complete")
        self.assertEqual(payload["ranked"][0]["cost_pessimistic"]["landed_total"], 1440.0)
        self.assertEqual(payload["ranked"][0]["rank"], 1)


def _lookup(gtip: str, *, origin_country: str | None = None, dispatch_country: str | None = None, atr_certificate: bool | None = None, **_: Any) -> TariffLookupResult:
    origin = origin_country or ""
    atr_available = origin == "Çin" and dispatch_country == "Almanya"
    atr_route = atr_available and atr_certificate is True
    if origin == "Çin":
        rates = {**CHINA, "customs_duty": 0.0 if atr_route else 10.0}
        proof: list[str] = []
        fallback: dict[str, float] = {}
    elif origin == "Almanya":
        rates, proof, fallback = dict(ALL_ZERO), ["additional_duty"], {"additional_duty": 20.0}
    else:
        rates, proof, fallback = {}, [], {}
    return TariffLookupResult(
        status="matched", gtip="851712000000", as_of="2026-09-14T00:00:00+03:00", matched_gtip_count=1,
        origin_country=origin, dispatch_country=dispatch_country, origin_recognised=origin in {"Çin", "Almanya"},
        atr_available=atr_available, atr_free_circulation=atr_route, atr_certificate=atr_certificate,
        origin_proof_required=proof, fallback_rates=fallback, resolved_country_group="1" if origin == "Almanya" else "7",
        unambiguous_rates=rates, ambiguous_measure_types=[], measures=[],
    )


class BuildOriginScenariosTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_keep_the_scenario_shape_and_real_document_rules(self) -> None:
        engine = type("Engine", (), {"lookup": AsyncMock(side_effect=_lookup)})()
        rows = await build_origin_scenarios(engine, "851712", ["Çin", "Almanya"], dispatch_country="Almanya", atr_certificate=None)
        self.assertEqual([item["origin_country"] for item in rows], ["Çin", "Almanya"])
        self.assertTrue(rows[0]["atr_available"])
        self.assertEqual(rows[1]["origin_documents"]["regime"], "customs_union")
        self.assertIn("ATR", [doc["code"] for doc in rows[0]["origin_documents"]["documents"]])
        self.assertEqual(
            set(rows[0]),
            {"origin_country", "dispatch_country", "status", "origin_recognised", "resolved_country_group", "matched_gtip_count",
             "unambiguous_rates", "ambiguous_measure_types", "atr_free_circulation", "atr_available", "origin_proof_required",
             "fallback_rates", "origin_documents", "warnings"},
        )


class SavingsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client", client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.free = {"sub": "free-sub", "email": "free@example.com", "name": "free", "picture": ""}
        self.paid = {"sub": "paid-sub", "email": "paid@example.com", "name": "paid", "picture": ""}
        admin = {"sub": "admin-sub", "email": "admin@example.com", "name": "admin", "picture": ""}
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.free, self.paid, admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )
        self.accounts.admin_set_plan(admin, "paid-sub", "expert", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def post(self, body: dict[str, Any], user: dict[str, str] | None):
        headers = {"Origin": PUBLIC_ORIGIN}
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.post("/api/tariff/savings", json=body, headers=headers)

    @staticmethod
    def body(**overrides: Any) -> dict[str, Any]:
        payload = {
            "gtip": "851712", "origins": ["Çin", "Almanya"], "dispatch_country": "Almanya", "atr_certificate": None,
            "baseline_origin": "Çin",
            "cost": {"invoice_value": 1000, "vat_rate": 20, "kkdf_rate": 0, "sct_amount": 0, "anti_dumping_amount": 0,
                     "surveillance_unit_value": 0, "payment_method": "Peşin", "currency": "USD"},
        }
        payload.update(overrides)
        return payload

    def test_requires_the_scenario_compare_feature(self) -> None:
        with patch.object(web_app.tariff_engine, "lookup", new=AsyncMock(side_effect=_lookup)):
            anonymous = self.post(self.body(), None)
            self.assertEqual(anonymous.status_code, 401)
            locked = self.post(self.body(), self.free)
        self.assertEqual(locked.status_code, 403, locked.text)
        self.assertEqual(locked.json()["feature"], "scenario_compare")

    def test_ranks_scenarios_including_the_atr_variant(self) -> None:
        with patch.object(web_app.tariff_engine, "lookup", new=AsyncMock(side_effect=_lookup)) as lookup:
            response = self.post(self.body(), self.paid)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["baseline_origin"], "Çin")
        self.assertIsNone(data["baseline_note"])
        self.assertEqual(
            [(item["origin_country"], item["variant"], item["landed_total"]) for item in data["ranked"]],
            [("Almanya", "base", 1200.0), ("Çin", "atr", 1440.0), ("Çin", "base", 1560.0)],
        )
        self.assertEqual(data["best"]["origin_country"], "Almanya")
        self.assertEqual(data["best"]["savings_vs_baseline"], 360.0)
        self.assertEqual(data["ranked"][0]["landed_total_pessimistic"], 1440.0)
        self.assertTrue(any("tevsik" in text for text in data["ranked"][0]["conditions"]))
        self.assertTrue(any(text.startswith("A.TR dolaşım belgesi ibrazı") for text in data["ranked"][1]["conditions"]))
        self.assertEqual(data["ranked"][2]["is_baseline"], True)
        self.assertEqual(data["not_comparable"], [])
        self.assertEqual(data["legal_notice"], LEGAL_NOTICE)
        self.assertIn("generated_at", data)
        atr_calls = [call for call in lookup.await_args_list if call.kwargs.get("atr_certificate") is True]
        self.assertEqual([call.kwargs["origin_country"] for call in atr_calls], ["Çin"])
        self.assertEqual(lookup.await_count, 3)

    def test_unknown_baseline_falls_back_with_a_note(self) -> None:
        with patch.object(web_app.tariff_engine, "lookup", new=AsyncMock(side_effect=_lookup)):
            response = self.post(self.body(baseline_origin="Japonya"), self.paid)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("en yüksek maliyetli", data["baseline_note"])
        self.assertEqual(data["baseline"]["origin_country"], "Çin")

    def test_validation_errors_are_422(self) -> None:
        with patch.object(web_app.tariff_engine, "lookup", new=AsyncMock(side_effect=_lookup)):
            missing_cost = self.post(self.body(cost=None), self.paid)
            self.assertEqual(missing_cost.status_code, 422)
            self.assertIn("cost", missing_cost.json()["error"])
            typo = self.post(self.body(cost={"invoice_value": 1000, "customs_duty_rat": 5}), self.paid)
            self.assertEqual(typo.status_code, 422)
            self.assertIn("doğrulanamadı", typo.json()["error"])
            single = self.post(self.body(origins=["Çin"]), self.paid)
            self.assertEqual(single.status_code, 422)


if __name__ == "__main__":
    unittest.main()
