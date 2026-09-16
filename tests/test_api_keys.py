"""ERP / dış sistem API anahtarları (FAZ 8.3).

Dört değişmez burada kilitlenir:

1. **Gizli değer bir kez görünür.** Veritabanında yalnız sha256 özeti durur; listeleme,
   denetim günlüğü ve hata gövdeleri anahtarı asla taşımaz.
2. **Anahtar yeni anahtar üretemez.** Anahtar üretme rotası bilerek çerez oturumuna
   bağlıdır; çalınan bir anahtar kendini kalıcı hâle getiremez.
3. **Beyaz liste dışına çıkamaz.** `_api_key_identity` çağırmayan bir rota anahtarı
   hiç görmez — hesap silme, ödeme ve yönetim rotaları buna dahildir.
4. **Kilit her istekte yeniden okunur.** Anahtar üretildikten sonra paket düşerse
   anahtar aynı anda geçersizleşir.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as web_app  # noqa: E402
from account_service import AccountError, AccountService  # noqa: E402
from auth_service import GoogleAuthService  # noqa: E402

PUBLIC_ORIGIN = "https://gumruksor.com"


def profile(sub: str, email: str) -> dict[str, str]:
    return {"sub": sub, "email": email, "name": f"Kullanıcı {sub}", "picture": ""}


class ApiKeyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.accounts = AccountService(Path(self.temp.name), admin_emails="admin@example.com")
        self.user = profile("erp-sub", "erp@example.com")
        self.other = profile("other-sub", "other@example.com")
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.user, self.other):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_the_plaintext_key_is_never_stored(self) -> None:
        created = self.accounts.create_api_key(self.user, label="ERP")
        secret = created["secret"]
        with sqlite3.connect(self.accounts.db_path) as connection:
            dumped = "\n".join(str(row) for row in connection.execute("SELECT * FROM api_keys"))
        self.assertNotIn(secret, dumped)
        self.assertIn(created["prefix"], dumped)

    def test_the_audit_entry_does_not_leak_the_key(self) -> None:
        created = self.accounts.create_api_key(self.user, label="ERP")
        with sqlite3.connect(self.accounts.db_path) as connection:
            details = "\n".join(str(row[0]) for row in connection.execute("SELECT details_json FROM audit_log"))
        self.assertNotIn(created["secret"], details)
        self.assertIn(created["prefix"], details)

    def test_listing_never_returns_the_secret(self) -> None:
        self.accounts.create_api_key(self.user, label="ERP")
        for item in self.accounts.list_api_keys(self.user):
            self.assertNotIn("secret", item)

    def test_a_valid_key_resolves_to_its_owner(self) -> None:
        created = self.accounts.create_api_key(self.user, label="ERP")
        resolved = self.accounts.authenticate_api_key(created["secret"])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["user"]["sub"], "erp-sub")
        self.assertEqual(resolved["user"]["email"], "erp@example.com")

    def test_using_a_key_records_last_used_at(self) -> None:
        created = self.accounts.create_api_key(self.user)
        self.assertIsNone(self.accounts.list_api_keys(self.user)[0]["last_used_at"])
        self.accounts.authenticate_api_key(created["secret"])
        self.assertIsNotNone(self.accounts.list_api_keys(self.user)[0]["last_used_at"])

    def test_a_revoked_key_stops_working(self) -> None:
        created = self.accounts.create_api_key(self.user)
        self.accounts.revoke_api_key(self.user, created["id"])
        self.assertIsNone(self.accounts.authenticate_api_key(created["secret"]))

    def test_revoking_twice_is_not_an_error(self) -> None:
        created = self.accounts.create_api_key(self.user)
        first = self.accounts.revoke_api_key(self.user, created["id"])
        second = self.accounts.revoke_api_key(self.user, created["id"])
        self.assertEqual(first["revoked_at"], second["revoked_at"])
        self.assertFalse(second["active"])

    def test_one_account_cannot_revoke_another_accounts_key(self) -> None:
        created = self.accounts.create_api_key(self.user)
        with self.assertRaises(AccountError):
            self.accounts.revoke_api_key(self.other, created["id"])
        self.assertIsNotNone(self.accounts.authenticate_api_key(created["secret"]))

    def test_a_tampered_secret_is_rejected(self) -> None:
        created = self.accounts.create_api_key(self.user)
        prefix = created["prefix"]
        # Doğru ön ek, yanlış gizli değer: özet karşılaştırması tutmaz.
        self.assertIsNone(self.accounts.authenticate_api_key(f"gsk_{prefix}_yanlis-deger"))

    def test_malformed_values_are_rejected_without_touching_the_database(self) -> None:
        for candidate in ("", "gsk_", "gsk_abc", "Bearer xyz", "gsk__", "gsk_a_b_c", "x" * 200):
            self.assertIsNone(self.accounts.authenticate_api_key(candidate), candidate)

    def test_the_active_key_limit_is_enforced_and_revoking_frees_a_slot(self) -> None:
        keys = [self.accounts.create_api_key(self.user, label=f"k{i}") for i in range(AccountService.API_KEY_LIMIT)]
        with self.assertRaises(AccountError):
            self.accounts.create_api_key(self.user, label="fazla")
        self.accounts.revoke_api_key(self.user, keys[0]["id"])
        self.assertTrue(self.accounts.create_api_key(self.user, label="yerine")["active"])

    def test_keys_of_two_accounts_do_not_mix(self) -> None:
        mine = self.accounts.create_api_key(self.user, label="benim")
        self.accounts.create_api_key(self.other, label="digeri")
        self.assertEqual([item["id"] for item in self.accounts.list_api_keys(self.user)], [mine["id"]])


class ApiCallQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.accounts = AccountService(Path(self.temp.name), admin_emails="admin@example.com")
        self.admin = profile("admin-sub", "admin@example.com")
        self.user = profile("erp-sub", "erp@example.com")
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.user, self.admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_the_institutional_plan_has_an_unlimited_api_call_meter(self) -> None:
        self.accounts.admin_set_plan(self.admin, "erp-sub", "institutional", "active")
        result = self.accounts.consume(self.user, "api_call")
        self.assertIsNone(result["limit"])
        self.assertEqual(self.accounts.account(self.user)["quotas"]["api_call"]["used"], 1)

    def test_a_plan_without_the_meter_reports_zero_quota_instead_of_crashing(self) -> None:
        # `api_call` yalnız Kurumsal pakette tanımlı; başka pakette KeyError değil,
        # yükseltme seçenekleri taşıyan dürüst bir kota hatası dönmeli.
        with self.assertRaises(Exception) as caught:
            self.accounts.consume(self.user, "api_call")
        self.assertEqual(getattr(caught.exception, "operation", None), "api_call")

    def test_the_meter_stays_out_of_other_plans_account_panel(self) -> None:
        self.assertNotIn("api_call", self.accounts.account(self.user)["quotas"])


class ApiKeyRouteTests(unittest.TestCase):
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
        self.admin = profile("admin-sub", "admin@example.com")
        self.erp = profile("erp-sub", "erp@example.com")
        self.team = profile("team-sub", "team@example.com")
        with sqlite3.connect(self.accounts.db_path) as connection:
            for item in (self.erp, self.team, self.admin):
                connection.execute(
                    "INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,?,?,1,1)",
                    (item["sub"], item["email"], item["name"], item["picture"]),
                )
        self.accounts.admin_set_plan(self.admin, "erp-sub", "institutional", "active")
        self.accounts.admin_set_plan(self.admin, "team-sub", "team", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)

    def tearDown(self) -> None:
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def cookie(self, user: dict[str, str]) -> dict[str, str]:
        return {"Cookie": f"{self.auth.session_cookie}={self.auth.create_session(user)}"}

    def headers(self, user: dict[str, str] | None = None) -> dict[str, str]:
        head = {"Origin": PUBLIC_ORIGIN}
        if user:
            head.update(self.cookie(user))
        return head

    def mint(self, label: str = "ERP") -> str:
        response = self.client.post("/api/account/api-keys", json={"label": label}, headers=self.headers(self.erp))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["secret"]

    def test_anonymous_callers_cannot_mint_a_key(self) -> None:
        response = self.client.post("/api/account/api-keys", json={}, headers=self.headers())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")

    def test_a_plan_without_api_access_is_told_which_plan_has_it(self) -> None:
        response = self.client.post("/api/account/api-keys", json={}, headers=self.headers(self.team))
        self.assertEqual(response.status_code, 403, response.text)
        payload = response.json()
        self.assertEqual(payload["feature"], "api_access")
        self.assertIn("Kurumsal", [item["name"] for item in payload["plans"]])

    def test_the_secret_is_returned_once_and_never_again(self) -> None:
        secret = self.mint()
        self.assertTrue(secret.startswith("gsk_"))
        listed = self.client.get("/api/account/api-keys", headers=self.headers(self.erp)).json()
        self.assertEqual(len(listed["items"]), 1)
        self.assertNotIn(secret, listed.text if hasattr(listed, "text") else str(listed))

    def test_a_key_cannot_mint_another_key(self) -> None:
        # Asıl korunan şey bu: çalınan bir anahtar kendini yenileyemez.
        secret = self.mint()
        response = self.client.post(
            "/api/account/api-keys", json={}, headers={"Origin": PUBLIC_ORIGIN, "X-API-Key": secret}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_key_cannot_list_keys(self) -> None:
        secret = self.mint()
        response = self.client.get("/api/account/api-keys", headers={"X-API-Key": secret})
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_key_cannot_reach_a_route_outside_the_allow_list(self) -> None:
        secret = self.mint()
        response = self.client.delete("/api/account", headers={"Origin": PUBLIC_ORIGIN, "X-API-Key": secret})
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_key_authenticates_an_allow_listed_route(self) -> None:
        secret = self.mint()
        response = self.client.get("/api/dossiers", headers={"X-API-Key": secret})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])

    def test_the_same_route_still_rejects_an_anonymous_caller(self) -> None:
        self.assertEqual(self.client.get("/api/dossiers").status_code, 401)

    def test_the_key_may_also_travel_as_a_bearer_token(self) -> None:
        secret = self.mint()
        response = self.client.get("/api/dossiers", headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_an_unrelated_bearer_token_is_not_treated_as_an_api_key(self) -> None:
        # Kısa ömürlü ajan JWT'si anahtar sanılmamalı; rota kimliksiz sayıp 401 vermeli.
        response = self.client.get("/api/dossiers", headers={"Authorization": "Bearer eyJhbGciOi.sahte.jwt"})
        self.assertEqual(response.status_code, 401, response.text)

    def test_an_invalid_key_is_rejected(self) -> None:
        response = self.client.get("/api/dossiers", headers={"X-API-Key": "gsk_deadbeef_yok"})
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_revoked_key_stops_authenticating(self) -> None:
        secret = self.mint()
        key_id = self.client.get("/api/account/api-keys", headers=self.headers(self.erp)).json()["items"][0]["id"]
        revoked = self.client.delete(f"/api/account/api-keys/{key_id}", headers=self.headers(self.erp))
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertFalse(revoked.json()["active"])
        self.assertEqual(self.client.get("/api/dossiers", headers={"X-API-Key": secret}).status_code, 401)

    def test_losing_the_plan_disables_the_key_immediately(self) -> None:
        secret = self.mint()
        self.assertEqual(self.client.get("/api/dossiers", headers={"X-API-Key": secret}).status_code, 200)
        self.accounts.admin_set_plan(self.admin, "erp-sub", "team", "active")
        response = self.client.get("/api/dossiers", headers={"X-API-Key": secret})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["feature"], "api_access")

    def test_each_api_request_is_metered(self) -> None:
        secret = self.mint()
        for _ in range(3):
            self.assertEqual(self.client.get("/api/dossiers", headers={"X-API-Key": secret}).status_code, 200)
        self.assertEqual(self.accounts.account(self.erp)["quotas"]["api_call"]["used"], 3)

    def test_revoking_an_unknown_key_is_a_404(self) -> None:
        response = self.client.delete("/api/account/api-keys/yok", headers=self.headers(self.erp))
        self.assertEqual(response.status_code, 404, response.text)

    def test_one_account_cannot_revoke_another_accounts_key_over_http(self) -> None:
        self.mint()
        key_id = self.client.get("/api/account/api-keys", headers=self.headers(self.erp)).json()["items"][0]["id"]
        response = self.client.delete(f"/api/account/api-keys/{key_id}", headers=self.headers(self.team))
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
