import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService


PUBLIC_ORIGIN = "https://gumruksor.com"


def profile(sub: str, email: str, name: str) -> dict[str, str]:
    return {"sub": sub, "email": email, "name": name, "picture": ""}


class ConsultationApiE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client",
            client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough",
            data_dir=data_dir,
        )
        self.accounts = AccountService(data_dir, admin_emails="admin@example.com")
        self.requester = profile("requester-sub", "requester@example.com", "Talep Sahibi")
        self.consultant = profile("consultant-sub", "consultant@example.com", "Ayşe Uzman")
        self.admin = profile("admin-sub", "admin@example.com", "Yönetici")
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.requester, self.consultant, self.admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) "
                    "VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )

        self.original_auth = web_app.google_auth
        self.original_accounts = web_app.account_service
        self.original_limiter = web_app.rate_limiter
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.original_marketplace_flag = os.environ.get("CONSULTANTS_MARKETPLACE_ENABLED")
        os.environ.pop("CONSULTANTS_MARKETPLACE_ENABLED", None)
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self):
        self.client.close()
        web_app.google_auth = self.original_auth
        web_app.account_service = self.original_accounts
        web_app.rate_limiter = self.original_limiter
        if self.original_marketplace_flag is None:
            os.environ.pop("CONSULTANTS_MARKETPLACE_ENABLED", None)
        else:
            os.environ["CONSULTANTS_MARKETPLACE_ENABLED"] = self.original_marketplace_flag
        self.temp.cleanup()

    def request(self, method: str, path: str, user: dict[str, str] | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Origin", PUBLIC_ORIGIN)
        if user:
            token = self.auth.create_session(user)
            headers["Cookie"] = f"{self.auth.session_cookie}={token}"
        return self.client.request(method, path, headers=headers, **kwargs)

    def test_phone_like_consultant_id_survives_contact_redaction(self):
        # A UUID4 whose digit run matches the Turkish phone pattern used to be mangled
        # by request redaction, so the consultant lookup failed for ~1 in 80 profiles.
        phone_like = uuid.UUID("9ca1aaed-962f-47ac-b587-4130658b472b")
        os.environ["CONSULTANTS_MARKETPLACE_ENABLED"] = "1"
        with patch("account_service.uuid.uuid4", return_value=phone_like):
            application = self.request(
                "POST",
                "/api/consultants/me",
                self.consultant,
                json={
                    "display_name": "Ayşe Uzman",
                    "title": "Tarife sınıflandırma danışmanı",
                    "bio": "Tarife kararları ve ürün evsafı üzerinden bağımsız sınıflandırma görüşü veriyorum.",
                    "expertise": ["GTİP ve tarife sınıflandırma"],
                    "city": "İstanbul",
                    "service_mode": "online",
                    "experience_years": 8,
                    "advisory_only_accepted": True,
                },
            )
        self.assertEqual(application.status_code, 201, application.text)
        approval = self.request("PUT", f"/api/admin/consultants/{self.consultant['sub']}", self.admin, json={"status": "active"})
        self.assertEqual(approval.status_code, 200, approval.text)
        listed = self.request("GET", "/api/consultants").json()["items"]
        self.assertEqual(listed[0]["id"], str(phone_like))
        handoff = self.request(
            "POST",
            "/api/consultation-requests",
            self.requester,
            json={
                "consultant_id": str(phone_like),
                "subject": "Porselen fincan sınıflandırması",
                "message": "Aday kodu ve kontrol kapsamını değerlendirir misiniz?",
                "share_consent": True,
                "result": {"summary": "Ön değerlendirme", "inquiry": {"product_description": "Porselen fincan"}},
            },
        )
        self.assertEqual(handoff.status_code, 201, handoff.text)
        # Free text is still redacted; a phone number in the message never reaches storage.
        handoff2 = self.request(
            "POST",
            "/api/consultation-requests",
            self.requester,
            json={
                "consultant_id": str(phone_like),
                "subject": "İkinci soru hakkında",
                "message": "Beni 0532 123 45 67 numarasından arayın lütfen.",
                "share_consent": True,
                "result": {"summary": "Ön değerlendirme"},
            },
        )
        self.assertEqual(handoff2.status_code, 201, handoff2.text)
        incoming = self.request("GET", "/api/consultation-requests", self.consultant).json()["incoming"]
        self.assertNotIn("0532", str(incoming))

    def test_application_approval_packet_handoff_acceptance_and_messaging(self):
        # Varsayılan: pazaryeri kapalıdır; dizin profilleri herkese görünmez.
        self.assertEqual(self.request("GET", "/api/consultants").json(), {"items": [], "enabled": False})
        os.environ["CONSULTANTS_MARKETPLACE_ENABLED"] = "1"
        application = self.request(
            "POST",
            "/api/consultants/me",
            self.consultant,
            json={
                "display_name": "Ayşe Uzman",
                "title": "Tarife sınıflandırma danışmanı",
                "bio": "Tarife kararları ve ürün evsafı üzerinden bağımsız sınıflandırma görüşü veriyorum.",
                "expertise": ["GTİP ve tarife sınıflandırma"],
                "city": "İstanbul",
                "service_mode": "online",
                "experience_years": 8,
                "advisory_only_accepted": True,
            },
        )
        self.assertEqual(application.status_code, 201, application.text)
        self.assertEqual(application.json()["profile"]["status"], "pending")
        self.assertEqual(self.request("GET", "/api/consultants").json(), {"items": [], "enabled": True})

        approval = self.request(
            "PUT",
            f"/api/admin/consultants/{self.consultant['sub']}",
            self.admin,
            json={"status": "active"},
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        public_consultants = self.request("GET", "/api/consultants").json()["items"]
        self.assertEqual(len(public_consultants), 1)
        self.assertNotIn("google_sub", public_consultants[0])
        consultant_id = public_consultants[0]["id"]

        handoff = self.request(
            "POST",
            "/api/consultation-requests",
            self.requester,
            json={
                "consultant_id": consultant_id,
                "subject": "Porselen fincan sınıflandırması",
                "message": "Aday kodu ve kontrol kapsamını değerlendirir misiniz?",
                "share_consent": True,
                "result": {
                    "summary": "Ön değerlendirme",
                    "as_of": "2026-09-01",
                    "inquiry": {
                        "product_description": "Porselen fincan",
                        "candidate_gtip": "691110",
                        "origin_country": "Çin",
                        "invoice_value": 9999,
                    },
                    "image_data": "data:image/png;base64,SECRET",
                    "deterministic_cost": {"total": 9999},
                    "expert_review_packet": {"risk_level": "high"},
                    "sources": [{"url": "https://ticaret.gov.tr/gumruk"}],
                },
            },
        )
        self.assertEqual(handoff.status_code, 201, handoff.text)
        request_id = handoff.json()["id"]

        incoming = self.request("GET", "/api/consultation-requests", self.consultant).json()["incoming"][0]
        packet_text = str(incoming["packet"])
        self.assertNotIn("SECRET", packet_text)
        self.assertNotIn("invoice_value", packet_text)
        self.assertNotIn("deterministic_cost", packet_text)

        accepted = self.request(
            "PATCH",
            f"/api/consultation-requests/{request_id}",
            self.consultant,
            json={"status": "accepted"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        consultant_message = self.request(
            "POST",
            f"/api/consultation-requests/{request_id}/messages",
            self.consultant,
            json={"body": "Teknik bileşim belgesiyle 6911 pozisyonunu doğrulayın."},
        )
        self.assertEqual(consultant_message.status_code, 201, consultant_message.text)
        requester_message = self.request(
            "POST",
            f"/api/consultation-requests/{request_id}/messages",
            self.requester,
            json={"body": "Belgeyi temin edip dosyaya ekleyeceğim."},
        )
        self.assertEqual(requester_message.status_code, 201, requester_message.text)

        outgoing = self.request("GET", "/api/consultation-requests", self.requester).json()["outgoing"][0]
        self.assertEqual(outgoing["status"], "accepted")
        self.assertEqual([message["mine"] for message in outgoing["messages"]], [False, True])
        self.assertEqual(len(outgoing["messages"]), 2)

    def test_mutating_routes_reject_foreign_origins_and_missing_sessions(self):
        unauthenticated = self.request("POST", "/api/consultants/me", json={})
        self.assertEqual(unauthenticated.status_code, 401)

        foreign_origin = self.request(
            "POST",
            "/api/consultants/me",
            self.consultant,
            headers={"Origin": "https://evil.example"},
            json={},
        )
        self.assertEqual(foreign_origin.status_code, 403)
        self.assertEqual(foreign_origin.json()["code"], "origin_denied")


if __name__ == "__main__":
    unittest.main()
