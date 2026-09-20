"""Ölçüm koşucusu: model çağırmadan, sahte sınıflandırıcıyla kilitlenir.

Bu testlerin hiçbiri ağa çıkmaz ve LLM çağırmaz; ölçülen şey koşucunun kendisidir —
vakaları doğru yüklüyor mu, hatayı yanlış cevap saymıyor mu, partileri tek skora
topluyor mu.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

import classification_benchmark as bench


class FakeCandidate:
    def __init__(self, code: str, level: str = "CN8", confidence_score: int = 70) -> None:
        self.code = code
        self.level = level
        self.confidence_score = confidence_score


class FakeResult:
    def __init__(self, codes: list[str]) -> None:
        self.status = "candidates_found" if codes else "insufficient_information"
        self.verification_status = "dual_agreement"
        self.models = ["fake-primary", "fake-verifier"]
        self.candidates = [
            FakeCandidate(code, level="CN8" if len(code) == 8 else "HS6") for code in codes
        ]


class FakeService:
    """İstenen kodları döndüren sahte hat; istek metnine göre cevap verir."""

    def __init__(self, answers: dict[str, list[str]], *, fail_on: set[str] | None = None) -> None:
        self.answers = answers
        self.fail_on = fail_on or set()
        self.seen: list[str] = []

    async def classify_product(self, request: Any) -> FakeResult:
        description = str(request.product_description)
        self.seen.append(description)
        for needle, codes in self.answers.items():
            if needle in description:
                if needle in self.fail_on:
                    raise RuntimeError("sahte sağlayıcı hatası")
                return FakeResult(codes)
        return FakeResult([])


class CaseLoadingTests(unittest.TestCase):
    def test_both_datasets_load_and_are_tagged(self):
        cases = bench.load_cases("all")
        self.assertEqual(len(cases), 16)
        datasets = {case["dataset"] for case in cases}
        self.assertEqual(datasets, {"eu", "tr"})
        self.assertEqual(sum(1 for case in cases if case["dataset"] == "tr"), 4)

    def test_single_dataset_can_be_selected(self):
        self.assertTrue(all(case["dataset"] == "tr" for case in bench.load_cases("tr")))

    def test_unknown_dataset_is_rejected(self):
        with self.assertRaises(ValueError):
            bench.load_cases("mars")

    def test_every_case_yields_a_valid_classification_request(self):
        for case in bench.load_cases("all"):
            request = bench.request_from_case(case)
            self.assertEqual(request.product_description, case["description"])
            # Ölçüm yalnız eşya tanımıyla yapılır; ek alan uydurulmaz.
            self.assertEqual(request.product_category, "")
            self.assertEqual(request.composition, "")

    def test_selection_skips_stored_cases_and_honours_limit(self):
        cases = bench.load_cases("all")
        first = str(cases[0]["id"])
        batch = bench.select_cases(cases, limit=2, skip_ids={first})
        self.assertEqual(len(batch), 2)
        self.assertNotIn(first, {str(case["id"]) for case in batch})


class RunTests(unittest.TestCase):
    def test_a_correct_answer_scores_and_a_wrong_one_does_not(self):
        cases = bench.load_cases("tr")
        # Gerçek etiketler: ayak ısıtıcı 630790…, demo telefon 847130…
        service = FakeService(
            {
                "ayak ısıtma": ["63079010"],
                "demo cep telefonu": ["85171300"],  # yanlış: beklenen 847130
            }
        )
        predictions = asyncio.run(bench.run_cases(service, cases, concurrency=2))
        report = bench.evaluate(cases, [item.to_dict() for item in predictions])
        details = {item["id"]: item for item in report["details"]}
        self.assertTrue(details["tr-istanbul-btb-2016-ayak-isitici"]["top1_cn8"])
        self.assertFalse(details["tr-istanbul-btb-2016-demo-telefon"]["top1_cn8"])
        self.assertFalse(details["tr-istanbul-btb-2016-demo-telefon"]["top1_hs6"])
        # Cevapsız iki vaka çekimser sayılır, yanlış sayılmaz.
        self.assertEqual(report["counts"]["abstained"], 2)

    def test_a_pipeline_error_is_recorded_not_scored_as_wrong(self):
        cases = bench.load_cases("tr")
        service = FakeService({"ayak ısıtma": ["63079010"]}, fail_on={"ayak ısıtma"})
        predictions = asyncio.run(bench.run_cases(service, cases, concurrency=1))
        report = bench.evaluate(cases, [item.to_dict() for item in predictions])
        errors = {item["id"] for item in report["errors"]}
        self.assertIn("tr-istanbul-btb-2016-ayak-isitici", errors)
        self.assertEqual(report["measured_cases"], len(cases) - 1)

    def test_a_timeout_is_reported_as_unmeasured(self):
        class Slow:
            async def classify_product(self, request: Any) -> FakeResult:
                await asyncio.sleep(5)
                return FakeResult(["63079010"])

        case = bench.load_cases("tr")[0]
        prediction = asyncio.run(bench.run_case(Slow(), case, timeout=0.05))
        self.assertIn("timeout", prediction.error)
        self.assertEqual(prediction.candidates, [])

    def test_report_always_explains_the_structural_gtip12_zero(self):
        cases = bench.load_cases("tr")
        service = FakeService({"ayak ısıtma": ["63079010"]})
        predictions = asyncio.run(bench.run_cases(service, cases))
        report = bench.evaluate(cases, [item.to_dict() for item in predictions])
        # Hat 12 hane üretmez; 0 değeri doğruluk değil, katman sınırıdır.
        self.assertEqual(report["metrics"]["top1_gtip12"], 0.0)
        self.assertIn("yapısal olarak 0", report["gtip12_note"])
        self.assertIn("görselden evsaf", report["runner_warning"])

    def test_datasets_are_scored_separately(self):
        cases = bench.load_cases("all")
        service = FakeService({"ayak ısıtma": ["63079010"]})
        predictions = asyncio.run(bench.run_cases(service, cases))
        report = bench.evaluate(cases, [item.to_dict() for item in predictions])
        self.assertEqual(set(report["by_dataset"]), {"eu", "tr"})
        self.assertGreater(report["by_dataset"]["tr"]["metrics"]["top1_cn8"], 0.0)
        self.assertEqual(report["by_dataset"]["eu"]["metrics"]["top1_cn8"], 0.0)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = bench.BenchmarkStore(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_batches_accumulate_into_one_score(self):
        service = FakeService(
            {
                "ayak ısıtma": ["63079010"],
                "demo cep telefonu": ["84713000"],
                "iki tekerlekli oyuncak": ["95030010"],
                "sivrisinek": ["38089490"],
            }
        )
        first = asyncio.run(
            bench.run_and_score(service, dataset="tr", limit=2, store=self.store)
        )
        self.assertEqual(first["batch"]["requested"], 2)
        self.assertEqual(len(first["missing_predictions"]), 2)
        second = asyncio.run(
            bench.run_and_score(service, dataset="tr", limit=2, store=self.store)
        )
        # İkinci parti ilkini yeniden koşmaz ve skor birikir.
        self.assertEqual(second["batch"]["requested"], 2)
        self.assertNotEqual(set(first["batch"]["case_ids"]), set(second["batch"]["case_ids"]))
        self.assertEqual(second["missing_predictions"], [])
        self.assertEqual(second["measured_cases"], 4)

    def test_a_failed_case_is_retried_on_the_next_batch(self):
        service = FakeService({"ayak ısıtma": ["63079010"]}, fail_on={"ayak ısıtma"})
        asyncio.run(bench.run_and_score(service, dataset="tr", limit=1, store=self.store))
        self.assertEqual(self.store.stored_ids("tr"), set())
        service.fail_on = set()
        again = asyncio.run(bench.run_and_score(service, dataset="tr", limit=1, store=self.store))
        self.assertEqual(again["batch"]["case_ids"], ["tr-istanbul-btb-2016-ayak-isitici"])
        self.assertEqual(self.store.stored_ids("tr"), {"tr-istanbul-btb-2016-ayak-isitici"})

    def test_saved_rows_survive_a_round_trip(self):
        prediction = bench.CasePrediction(
            id="tr-x",
            dataset="tr",
            candidates=["63079010"],
            levels=["CN8"],
            confidence_scores=[70],
            status="candidates_found",
            verification_status="dual_agreement",
            models=["a", "b"],
            elapsed_ms=1234,
            code_version="abc123",
            recorded_at="2026-09-20T00:00:00+00:00",
        )
        self.store.save([prediction])
        rows = self.store.load("tr")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidates"], ["63079010"])
        self.assertEqual(rows[0]["models"], ["a", "b"])
        self.assertEqual(rows[0]["elapsed_ms"], 1234)
        # Aynı vaka yeniden kaydedilince satır çoğalmaz, güncellenir.
        prediction.candidates = ["63079090"]
        self.store.save([prediction])
        rows = self.store.load("tr")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidates"], ["63079090"])

    def test_clear_removes_only_the_named_dataset(self):
        self.store.save(
            [
                bench.CasePrediction(id="tr-x", dataset="tr", candidates=["63079010"]),
                bench.CasePrediction(id="eu-x", dataset="eu", candidates=["63069000"]),
            ]
        )
        self.assertEqual(self.store.clear("tr"), 1)
        self.assertEqual({item["id"] for item in self.store.load()}, {"eu-x"})

    def test_database_file_is_not_world_readable(self):
        self.assertEqual(Path(self.store.db_path).stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()


class RouteTests(unittest.TestCase):
    """Yönetim rotaları: yetkisiz erişim yok, puanlama ücretsiz, koşu elle tetiklenir."""

    def setUp(self) -> None:
        import sqlite3

        from starlette.testclient import TestClient

        import app as web_app
        from account_service import AccountService
        from auth_service import GoogleAuthService

        self.web_app = web_app
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        self.origin = "https://gumruksor.com"
        self.auth = GoogleAuthService(
            client_id="test-client",
            client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough",
            data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.admin = {"sub": "admin-sub", "email": "admin@example.com", "name": "admin", "picture": ""}
        self.user = {"sub": "user-sub", "email": "user@example.com", "name": "user", "picture": ""}
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.admin, self.user):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at)"
                    " VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )
        self.store = bench.BenchmarkStore(data_dir)
        self._original = (
            web_app.google_auth,
            web_app.account_service,
            web_app.rate_limiter,
            web_app.classification_benchmark_store,
        )
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        web_app.classification_benchmark_store = self.store
        self.client = TestClient(web_app.app, base_url=self.origin)

    def tearDown(self) -> None:
        self.client.close()
        (
            self.web_app.google_auth,
            self.web_app.account_service,
            self.web_app.rate_limiter,
            self.web_app.classification_benchmark_store,
        ) = self._original
        self._tmp.cleanup()

    def request(self, method: str, path: str, user: dict[str, str] | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Origin", self.origin)
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.request(method, path, headers=headers, **kwargs)

    def test_scoring_requires_an_admin(self):
        for user in (None, self.user):
            response = self.request("GET", "/api/admin/classification-benchmark", user)
            self.assertEqual(response.status_code, 403, response.text)

    def test_running_requires_an_admin(self):
        response = self.request("POST", "/api/admin/classification-benchmark", self.user, json={})
        self.assertEqual(response.status_code, 403, response.text)

    def test_empty_ledger_scores_zero_and_lists_every_pending_case(self):
        response = self.request("GET", "/api/admin/classification-benchmark", self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["case_count"], 16)
        self.assertEqual(len(body["pending_cases"]), 16)
        self.assertEqual(body["measured_cases"], 0)
        self.assertIn("yapısal olarak 0", body["gtip12_note"])

    def test_stored_predictions_are_scored_without_calling_a_model(self):
        self.store.save(
            [
                bench.CasePrediction(
                    id="tr-istanbul-btb-2016-ayak-isitici",
                    dataset="tr",
                    candidates=["63079010"],
                    levels=["CN8"],
                )
            ]
        )
        with unittest.mock.patch.object(
            self.web_app.customs_advisor_service,
            "classify_product",
            side_effect=AssertionError("puanlama model çağırmamalı"),
        ):
            response = self.request(
                "GET", "/api/admin/classification-benchmark?dataset=tr", self.admin
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["measured_cases"], 1)
        self.assertEqual(len(body["pending_cases"]), 3)
        self.assertGreater(body["metrics"]["top1_cn8"], 0.0)

    def test_unknown_dataset_is_rejected_with_422(self):
        response = self.request(
            "GET", "/api/admin/classification-benchmark?dataset=mars", self.admin
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_run_stores_predictions_and_returns_the_running_total(self):
        service = FakeService({"ayak ısıtma": ["63079010"]})
        with unittest.mock.patch.object(
            self.web_app, "customs_advisor_service", service
        ):
            response = self.request(
                "POST",
                "/api/admin/classification-benchmark",
                self.admin,
                json={"dataset": "tr", "limit": 1},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["batch"]["requested"], 1)
        self.assertEqual(len(service.seen), 1)
        self.assertEqual(self.store.stored_ids("tr"), {"tr-istanbul-btb-2016-ayak-isitici"})

    def test_batch_size_is_capped(self):
        service = FakeService({})
        with unittest.mock.patch.object(self.web_app, "customs_advisor_service", service):
            response = self.request(
                "POST",
                "/api/admin/classification-benchmark",
                self.admin,
                json={"limit": 999},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLessEqual(
            response.json()["batch"]["requested"], self.web_app._BENCHMARK_MAX_BATCH
        )

    def test_reset_clears_without_spending_quota(self):
        self.store.save(
            [bench.CasePrediction(id="tr-istanbul-btb-2016-ayak-isitici", dataset="tr", candidates=["63079010"])]
        )
        service = FakeService({})
        with unittest.mock.patch.object(self.web_app, "customs_advisor_service", service):
            response = self.request(
                "POST",
                "/api/admin/classification-benchmark",
                self.admin,
                json={"limit": 0, "reset": True},
            )
        self.assertEqual(response.status_code, 200, response.text)
        # Sıfırlama modeli hiç çağırmaz ve defteri boşaltır.
        self.assertEqual(service.seen, [])
        self.assertEqual(response.json()["batch"]["requested"], 0)
        self.assertEqual(self.store.load(), [])
