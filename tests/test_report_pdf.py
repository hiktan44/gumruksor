"""Server-side PDF precheck report and its mandatory legal footer (PRD Faz 2.1)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import app as web_app
import report_pdf
from account_service import AccountService
from auth_service import GoogleAuthService
from customs_advisor import CustomsPrecheckResult, _legal_notice
from email_service import precheck_sections, render_precheck_email

PUBLIC_ORIGIN = "https://gumruksor.com"
AS_OF = "2026-09-04T10:00:00+00:00"

SAMPLE_RESULT = {
    "status": "preliminary",
    "as_of": AS_OF,
    "summary": "Porselen kahve fincanı için ön değerlendirme <script>alert(1)</script>",
    "candidate_gtips": [
        {"code": "691110", "explanation": "Porselen sofra eşyası", "confidence": "medium", "citations": ["S1"]},
    ],
    "missing_information": ["Fatura bedeli", "Menşe belgesi türü"],
    "controls": [{"name": "TAREKS", "status": "likely", "explanation": "Ürün güvenliği denetimi olası.", "citations": ["S1"]}],
    "required_documents": [{"name": "Menşe şahadetnamesi", "status": "required", "explanation": "İGV için tevsik gerekir."}],
    "taxes": [{"name": "KDV", "status": "applicable", "rate": "20", "explanation": "Genel oran."}],
    "deterministic_cost": {"currency": "EUR", "customs_value_estimate": 1250.5, "vat": 250.1, "status": "rates_missing", "missing_rates": ["İGV"]},
    "next_steps": ["GTİP12 satırını resmî tarifede doğrulayın.", "Menşe belgesini tedarikçiden isteyin."],
    "sources": [
        {
            "id": "S1", "title": "İthalat Rejimi Kararı", "authority": "Ticaret Bakanlığı",
            "url": "https://www.ticaret.gov.tr/ithalat/rejim", "excerpt": "…", "retrieved_at": AS_OF, "sha256": "a" * 64,
        },
        {
            "id": "S2", "title": "Ürün Güvenliği Tebliği", "authority": "Ticaret Bakanlığı",
            "url": "https://www.mevzuat.gov.tr/tebligler", "excerpt": "…", "retrieved_at": AS_OF,
        },
    ],
    "legal_notice": _legal_notice(AS_OF),
    "safety_notes": ["Fotoğraf tek başına GTİP değildir."],
    "inquiry": {"question": "Porselen fincan ithalatı için hangi vergiler uygulanır?", "product_description": "Porselen kahve fincanı", "candidate_gtip": "691110", "tariff_selection_confirmed": True, "origin_country": "Çin"},
    "expert_review_packet": {
        "risk_level": "high", "escalation_required": True, "review_types": ["BTB"], "reasons": ["Oran belirsiz"],
        "questions_for_reviewer": ["Sır altı dekor var mı?"], "generated_at": AS_OF, "legal_notice": "Uzman incelemesi önerilir.",
    },
}


def profile(sub: str, email: str) -> dict[str, str]:
    return {"sub": sub, "email": email, "name": f"Kullanıcı {sub}", "picture": ""}


def sample_result(**overrides) -> CustomsPrecheckResult:
    return CustomsPrecheckResult.model_validate({**SAMPLE_RESULT, **overrides})


def report_html(result: CustomsPrecheckResult | None = None) -> str:
    result = result or sample_result()
    return report_pdf.render_precheck_report_html(
        result, base_url=PUBLIC_ORIGIN, generated_for="Test Kullanıcı", generated_at="2026-09-14T09:00:00+00:00"
    )


class ReportHtmlTests(unittest.TestCase):
    def test_contains_mandatory_legal_wording_and_ledger_without_scripts(self) -> None:
        html = report_html()
        self.assertIn(report_pdf.DECISION_SUPPORT_SENTENCE, html)
        self.assertIn("Nihai tarife tespiti bağlayıcı karar yerine geçmez", html)
        self.assertIn(html_escape(_legal_notice(AS_OF)), html)
        self.assertIn(report_pdf.REPORT_TITLE.replace("'", "&#x27;"), html)
        self.assertNotIn("<script", html.lower())
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        # Source ledger: id, authority, title, url, retrieved_at, short sha256.
        for needle in ("<code>S1</code>", "Ticaret Bakanlığı", "İthalat Rejimi Kararı", "https://www.ticaret.gov.tr/ithalat/rejim", AS_OF, "aaaaaaaaaaaa"):
            self.assertIn(needle, html)
        self.assertNotIn("a" * 20, html)  # only the short hash is printed
        # Header identity and detailed sections.
        for needle in ("691110", "Çin", "Test Kullanıcı", "2026-09-14T09:00:00+00:00", "Aday GTİP / CN kodları", "TAREKS", "Menşe şahadetnamesi", "Uzman inceleme paketi", "Sonraki güvenli adımlar", "1.250,50 EUR"):
            self.assertIn(needle, html)
        self.assertNotIn("data:image", html)

    def test_footer_has_page_tokens_and_notice(self) -> None:
        footer = report_pdf.report_footer_html(sample_result())
        self.assertIn(report_pdf.PAGE_TOKEN, footer)
        self.assertIn(report_pdf.PAGES_TOKEN, footer)
        self.assertIn("gumruksor.com", footer)
        self.assertIn(html_escape(_legal_notice(AS_OF)), footer)
        self.assertIn(report_pdf.DECISION_SUPPORT_SENTENCE, footer)

    def test_email_shares_sections_and_stays_compact(self) -> None:
        result = sample_result()
        compact = precheck_sections(result, PUBLIC_ORIGIN)
        detailed = precheck_sections(result, PUBLIC_ORIGIN, detailed=True)
        self.assertLess(len(compact), len(detailed))
        email = render_precheck_email(result, PUBLIC_ORIGIN)
        for section in compact:
            self.assertIn(section, email)
        self.assertNotIn("Uzman inceleme paketi", email)
        self.assertIn("Eksik veya teyit gereken bilgiler", email)
        self.assertIn("Resmî kaynaklar", email)


def html_escape(value: str) -> str:
    import html

    return html.escape(value)


def _pymupdf():
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:
        try:
            import fitz  # type: ignore

            return fitz
        except ImportError:
            return None


class PymupdfRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        if _pymupdf() is None:
            self.skipTest("pymupdf kurulu değil")

    def test_renders_pdf_with_footer_on_every_page(self) -> None:
        result = sample_result(next_steps=[f"Adım {i}: resmî kaynağı doğrulayın." for i in range(1, 120)])
        with patch.dict(os.environ, {"PDF_RENDERER": "pymupdf"}):
            data = asyncio.run(report_pdf.render_pdf(report_html(result), footer_html=report_pdf.report_footer_html(result)))
        self.assertTrue(data.startswith(b"%PDF-"))
        fitz = _pymupdf()
        document = fitz.open("pdf", data)
        self.assertGreaterEqual(len(document), 2)
        for index, page in enumerate(document, start=1):
            text = " ".join(page.get_text().split())  # MuPDF wraps the footer over several lines
            self.assertIn("Nihai tarife tespiti", text)
            self.assertIn(f"Sayfa {index} / {len(document)}", text)
            self.assertIn("gumruksor.com", text)
        document.close()

    def test_invalid_renderer_value_falls_back_to_playwright_mode(self) -> None:
        with patch.dict(os.environ, {"PDF_RENDERER": "nonsense"}):
            self.assertEqual(report_pdf.renderer_mode(), "playwright")
        with patch.dict(os.environ, {"PDF_RENDERER": "auto"}):
            self.assertEqual(report_pdf.renderer_mode(), "auto")


class PlaywrightRendererTests(unittest.TestCase):
    def test_renders_pdf_in_headless_chromium(self) -> None:
        browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if not browsers or not Path(browsers).is_dir():
            self.skipTest("Playwright tarayıcı dizini yok")
        result = sample_result()
        try:
            with patch.dict(os.environ, {"PDF_RENDERER": "playwright"}):
                data = asyncio.run(report_pdf.render_pdf(report_html(result), footer_html=report_pdf.report_footer_html(result)))
        except report_pdf.PdfRenderError as exc:
            self.skipTest(f"Chromium başlatılamadı: {exc.__cause__ and type(exc.__cause__).__name__}")
        self.assertTrue(data.startswith(b"%PDF-"))
        fitz = _pymupdf()
        if fitz is not None:
            document = fitz.open("pdf", data)
            self.assertGreaterEqual(len(document), 1)
            self.assertIn("Nihai tarife tespiti", document[0].get_text())
            document.close()


class ReportRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.auth = GoogleAuthService(
            client_id="test-client", client_secret="test-secret",
            session_secret="test-session-secret-that-is-long-enough", data_dir=data_dir,
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
        self.accounts.admin_set_plan(self.admin, "paid-sub", "expert", "active")
        self.original = (web_app.google_auth, web_app.account_service, web_app.rate_limiter)
        web_app.google_auth = self.auth
        web_app.account_service = self.accounts
        web_app.rate_limiter = web_app.FixedWindowRateLimiter()
        self.client = TestClient(web_app.app, base_url=PUBLIC_ORIGIN)
        self.render = AsyncMock(return_value=b"%PDF-1.4 stub")
        self.render_patch = patch.object(report_pdf, "render_pdf", new=self.render)
        self.render_patch.start()

    def tearDown(self) -> None:
        self.render_patch.stop()
        self.client.close()
        web_app.google_auth, web_app.account_service, web_app.rate_limiter = self.original
        self.temp.cleanup()

    def request(self, method: str, path: str, user: dict[str, str] | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Origin", PUBLIC_ORIGIN)
        if user:
            headers["Cookie"] = f"{self.auth.session_cookie}={self.auth.create_session(user)}"
        return self.client.request(method, path, headers=headers, **kwargs)

    def test_anonymous_is_rejected(self) -> None:
        response = self.request("POST", "/api/customs/report.pdf", json={"result": SAMPLE_RESULT})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "authentication_required")
        self.render.assert_not_awaited()

    def test_starter_plan_is_feature_locked(self) -> None:
        response = self.request("POST", "/api/customs/report.pdf", self.free, json={"result": SAMPLE_RESULT})
        self.assertEqual(response.status_code, 403, response.text)
        payload = response.json()
        self.assertEqual(payload["code"], "feature_required")
        self.assertEqual(payload["feature"], "pdf_report")
        self.assertIn("Uzman", [item["name"] for item in payload["plans"]])
        self.render.assert_not_awaited()

    def test_expert_plan_receives_pdf_with_footer_and_no_quota_use(self) -> None:
        before = self.accounts.account(self.paid)["quotas"]
        response = self.request("POST", "/api/customs/report.pdf", self.paid, json={"result": SAMPLE_RESULT})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertRegex(response.headers["content-disposition"], r'^attachment; filename="gumruksor-on-degerlendirme-[a-f0-9]{8}\.pdf"$')
        self.assertEqual(response.content, b"%PDF-1.4 stub")
        self.render.assert_awaited_once()
        html_arg = self.render.await_args.args[0]
        footer_arg = self.render.await_args.kwargs["footer_html"]
        self.assertIn(report_pdf.DECISION_SUPPORT_SENTENCE, html_arg)
        self.assertIn("Kullanıcı paid-sub", html_arg)
        self.assertIn(report_pdf.PAGE_TOKEN, footer_arg)
        self.assertEqual(self.accounts.account(self.paid)["quotas"], before)

    def test_saved_dossier_can_be_rendered_by_id_only_for_its_owner(self) -> None:
        dossier = self.accounts.create_dossier(
            self.paid, title="Fincan", product_name="Porselen fincan", gtip="691110", origin_country="Çin",
            effective_date=AS_OF, checked_at=AS_OF, payload=SAMPLE_RESULT, evidence={},
        )
        response = self.request("POST", "/api/customs/report.pdf", self.paid, json={"dossier_id": dossier["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(dossier["id"].replace("-", "")[:8], response.headers["content-disposition"])
        self.accounts.admin_set_plan(self.admin, "free-sub", "expert", "active")
        stranger = self.request("POST", "/api/customs/report.pdf", self.free, json={"dossier_id": dossier["id"]})
        self.assertEqual(stranger.status_code, 404)

    def test_invalid_payloads_are_rejected(self) -> None:
        invalid = self.request("POST", "/api/customs/report.pdf", self.paid, json={"result": {"status": "nonsense"}})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("doğrulanamadı", invalid.json()["error"])
        missing = self.request("POST", "/api/customs/report.pdf", self.paid, json={})
        self.assertEqual(missing.status_code, 422)
        not_json = self.request("POST", "/api/customs/report.pdf", self.paid, content=b"{", headers={"Content-Type": "application/json"})
        self.assertEqual(not_json.status_code, 422)
        too_big = self.request(
            "POST", "/api/customs/report.pdf", self.paid, content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(3 * 1024 * 1024)},
        )
        self.assertEqual(too_big.status_code, 422)
        self.render.assert_not_awaited()

    def test_renderer_failure_maps_to_503(self) -> None:
        self.render.side_effect = report_pdf.PdfRenderError("PDF raporu şu anda oluşturulamadı; kısa süre sonra yeniden deneyin.")
        response = self.request("POST", "/api/customs/report.pdf", self.paid, json={"result": SAMPLE_RESULT})
        self.assertEqual(response.status_code, 503)
        self.assertIn("oluşturulamadı", response.json()["error"])

    def test_cross_site_origin_is_denied(self) -> None:
        response = self.request(
            "POST", "/api/customs/report.pdf", self.paid, json={"result": SAMPLE_RESULT}, headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "origin_denied")


if __name__ == "__main__":
    unittest.main()
