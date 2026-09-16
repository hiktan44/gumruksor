"""``POST /api/customs/declaration-draft`` — yetki kapısı, biçimler ve kalıcılık.

Arayüzün güvendiği sözleşme burada kilitlenir: kota tüketilmez, `declaration_draft`
yeteneği olmayan paket 403 alır ve taslak yalnız doğrulanmış bir ön değerlendirme
sonucundan üretilir.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as web_app  # noqa: E402
from account_service import AccountService  # noqa: E402
from auth_service import GoogleAuthService  # noqa: E402

from customs_advisor import _legal_notice  # noqa: E402

PUBLIC_ORIGIN = "https://gumruksor.com"
AS_OF = "2026-09-04T10:00:00+00:00"

# Gerçek bir ön değerlendirme sonucunun en küçük geçerli hâli; taslak yalnız bundan üretilir.
SAMPLE_RESULT = {
    "status": "preliminary",
    "as_of": AS_OF,
    "summary": "Porselen kahve fincanı için ön değerlendirme",
    "candidate_gtips": [
        {"code": "691110", "explanation": "Porselen sofra eşyası", "confidence": "medium", "citations": ["S1"]},
    ],
    "missing_information": ["Fatura bedeli"],
    "next_steps": ["GTİP12 satırını resmî tarifede doğrulayın."],
    "sources": [
        {
            "id": "S1",
            "title": "İthalat Rejimi Kararı",
            "authority": "Ticaret Bakanlığı",
            "url": "https://www.ticaret.gov.tr/ithalat/rejim",
            "excerpt": "…",
            "retrieved_at": AS_OF,
            "sha256": "a" * 64,
        },
    ],
    "legal_notice": _legal_notice(AS_OF),
    "safety_notes": ["Fotoğraf tek başına GTİP değildir."],
    "inquiry": {
        "question": "Porselen fincan ithalatı için hangi vergiler uygulanır?",
        "product_description": "Porselen kahve fincanı",
        "candidate_gtip": "691110",
        "tariff_selection_confirmed": True,
        "origin_country": "Çin",
    },
    "expert_review_packet": {
        "risk_level": "high",
        "escalation_required": True,
        "review_types": ["BTB"],
        "reasons": ["Oran belirsiz"],
        "questions_for_reviewer": ["Sır altı dekor var mı?"],
        "generated_at": AS_OF,
        "legal_notice": "Uzman incelemesi önerilir.",
    },
}


def profile(sub: str, email: str) -> dict[str, str]:
    return {"sub": sub, "email": email, "name": f"Kullanıcı {sub}", "picture": ""}


class DeclarationDraftRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client",
            client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough",
            data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.free = profile("free-sub", "free@example.com")
        self.paid = profile("paid-sub", "paid@example.com")
        self.admin = profile("admin-sub", "admin@example.com")
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.free, self.paid, self.admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )
        # `declaration_draft` Ekip (premium) ve üstünde açıktır; Uzman paketinde yoktur.
        self.accounts.admin_set_plan(self.admin, "paid-sub", "team", "active")
        self.accounts.admin_set_plan(self.admin, "free-sub", "expert", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def post(self, body: dict, user: dict[str, str] | None = None):
        headers = {"Origin": PUBLIC_ORIGIN}
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.post("/api/customs/declaration-draft", json=body, headers=headers)

    def test_anonymous_request_is_rejected(self) -> None:
        response = self.post({"result": SAMPLE_RESULT})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_a_plan_without_the_feature_is_told_which_plans_have_it(self) -> None:
        response = self.post({"result": SAMPLE_RESULT}, self.free)
        self.assertEqual(response.status_code, 403, response.text)
        payload = response.json()
        self.assertEqual(payload["code"], "feature_required")
        self.assertEqual(payload["feature"], "declaration_draft")
        self.assertIn("Ekip", [item["name"] for item in payload["plans"]])

    def test_json_draft_carries_boxes_and_the_legal_notice(self) -> None:
        response = self.post({"result": SAMPLE_RESULT}, self.paid)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["direction"], "import")
        self.assertEqual(payload["regime_code"], "4000")
        self.assertIn("beyanname yerine geçmez", payload["legal_notice"])
        keys = {field["key"] for section in payload["sections"] for field in section["fields"]}
        self.assertIn("commodity_code", keys)
        self.assertIn("consignee_tax_id", keys)

    def test_no_box_is_invented_from_a_thin_result(self) -> None:
        payload = self.post({"result": SAMPLE_RESULT}, self.paid).json()
        for section in payload["sections"]:
            for field in section["fields"]:
                if field["certainty"] == "unavailable":
                    self.assertIsNone(field["value"], field["key"])

    def test_csv_and_xml_downloads(self) -> None:
        csv_response = self.post({"result": SAMPLE_RESULT, "format": "csv"}, self.paid)
        self.assertEqual(csv_response.status_code, 200, csv_response.text)
        self.assertIn("text/csv", csv_response.headers["content-type"])
        self.assertIn("beyanname-taslagi-", csv_response.headers["content-disposition"])
        self.assertTrue(csv_response.text.startswith("bolum;kutu;alan_kodu"))

        xml_response = self.post({"result": SAMPLE_RESULT, "format": "xml"}, self.paid)
        self.assertEqual(xml_response.status_code, 200, xml_response.text)
        self.assertIn("application/xml", xml_response.headers["content-type"])
        self.assertEqual(ET.fromstring(xml_response.text).tag, "beyanname-taslagi")

    def test_an_unknown_format_is_rejected(self) -> None:
        response = self.post({"result": SAMPLE_RESULT, "format": "pdf"}, self.paid)
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_body_that_is_not_a_precheck_result_is_rejected(self) -> None:
        response = self.post({"result": {"foo": "bar"}}, self.paid)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("doğrulanamadı", response.json()["error"])

    def test_a_missing_result_is_rejected(self) -> None:
        self.assertEqual(self.post({}, self.paid).status_code, 422)

    def test_an_unknown_dossier_is_not_found(self) -> None:
        response = self.post({"dossier_id": "00000000-0000-0000-0000-000000000000"}, self.paid)
        self.assertEqual(response.status_code, 404, response.text)

    def test_the_draft_never_consumes_quota(self) -> None:
        def ledger_rows() -> int:
            with sqlite3.connect(self.accounts.db_path) as connection:
                return int(
                    connection.execute(
                        "SELECT COALESCE(SUM(quantity),0) FROM usage_ledger WHERE google_sub=?",
                        (self.paid["sub"],),
                    ).fetchone()[0]
                )

        before = ledger_rows()
        self.assertEqual(self.post({"result": SAMPLE_RESULT}, self.paid).status_code, 200)
        self.assertEqual(ledger_rows(), before)


class DossierDraftPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.accounts = AccountService(Path(self.temp.name), admin_emails="admin@example.com")
        self.user = profile("owner-sub", "owner@example.com")
        with sqlite3.connect(self.accounts.db_path) as connection:
            connection.execute(
                "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                (self.user["sub"], self.user["email"], self.user["name"], self.user["picture"]),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(self, draft):
        return self.accounts.create_dossier(
            self.user,
            title="Test",
            product_name="Porselen fincan",
            gtip="691110",
            origin_country="Çin",
            effective_date=None,
            checked_at="2026-09-15T00:00:00+00:00",
            payload=SAMPLE_RESULT,
            evidence={},
            draft=draft,
        )

    def test_a_saved_draft_comes_back_with_the_dossier(self) -> None:
        from declaration_draft import build_declaration_draft

        draft = build_declaration_draft(SAMPLE_RESULT).model_dump(mode="json")
        dossier = self._create(draft)
        reopened = self.accounts.get_dossier(self.user, dossier["id"])
        self.assertEqual(reopened["draft"]["regime_code"], "4000")
        self.assertNotIn("draft_json", reopened)

    def test_a_dossier_saved_without_a_draft_still_opens(self) -> None:
        # Göç öncesi kaydedilmiş dosyalarda sütun NULL'dur; dosya yine açılmalıdır.
        dossier = self._create(None)
        self.assertIsNone(self.accounts.get_dossier(self.user, dossier["id"])["draft"])

    def test_a_corrupt_draft_does_not_lock_the_dossier(self) -> None:
        dossier = self._create({"direction": "import"})
        with sqlite3.connect(self.accounts.db_path) as connection:
            connection.execute("UPDATE dossiers SET draft_json='{bozuk' WHERE id=?", (dossier["id"],))
        self.assertIsNone(self.accounts.get_dossier(self.user, dossier["id"])["draft"])


if __name__ == "__main__":
    unittest.main()
