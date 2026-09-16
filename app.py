"""ASGI application for the Mevzuat MCP server and its web interface."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from auth_service import AuthError, GoogleAuthService
from account_service import PLANS, AccountError, AccountService, QuotaExceeded
from billing_service import BillingError, StripeBilling
from customs_advisor import (
    CustomsInquiry,
    CustomsPrecheckResult,
    ProductClassificationRequest,
    decode_image_data_url,
    register_llm_usage_hook,
)
from assistant import AssistantRequest
from compliance import compliance_report, high_alert_digest
from email_service import MailError, ResendEmailSender, render_compliance_email, render_consultation_email, render_precheck_email, render_review_email, render_watch_email
import report_pdf
from report_pdf import PdfRenderError, render_precheck_report_html, report_footer_html
from mevzuat_mcp_server import (
    _BED_VALID_TYPES,
    bedesten_client,
    change_ledger,
    classification_engine,
    control_engine,
    customs_advisor_service,
    customs_assistant,
    excise_tax_index,
    exchange_rate_service,
    eylemio_client,
    ebti_engine,
    eu_taric_engine,
    foreign_tariff_engine,
    hybrid_index,
    review_service,
    storage_service,
    tariff_engine,
    ticaret_client,
    trade_measure_engine,
    eu_vat_index,
    vat_rate_index,
)
from bulk_costing import MAX_FILE_BYTES as BULK_MAX_FILE_BYTES, calculate_rows as bulk_calculate_rows, rows_from_upload as bulk_rows_from_upload, template_csv as bulk_template_csv
from countries import COUNTRIES, PENDING_AGREEMENTS
from declaration_draft import build_declaration_draft, draft_to_csv, draft_to_xml
from export_requirements import destination_profile
from savings import evaluate_scenarios, rank_savings
from storage import resolve_backup_file
from scenarios import build_origin_scenarios
from product_page import BROWSER_HEADERS as PRODUCT_PAGE_BROWSER_HEADERS, brand_model_match, detect_bot_wall, extract_product_page
from shipping_documents import decode_document_data_url, extract_shipping_document, pdf_page_count, rasterize_pdf_pages
from mevzuat_mcp_server import (
    BACKGROUND_LOOPS,
    app as mcp,
)
from security_firewall import AgentTokenVerifier, SecurityViolation, guard_data, redact_data, redact_text, sanitize_untrusted_context
from temporal import normalise_as_of, parse_iso_date, today_iso
from exchange_rates import ExchangeRateError, parse_registration_date
from eylemio_client import EylemioError, summarise_declaration
from trade_measures import KIND_LABELS as TRADE_MEASURE_LABELS, summary_lines as trade_measure_summary
from tax_lists import summary_lines as excise_tax_summary
from vat_lists import summary_lines as vat_rate_summary
from decision_questions import apply_decision_answers, build_decision_questions
from tariff_engine import LandedCostInput
from unified_search import UnifiedSearchEngine

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent / "web"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://gumruksor.com").rstrip("/")
ADDITIONAL_ALLOWED_ORIGINS = tuple(
    origin.strip().rstrip("/")
    for origin in os.environ.get("ADDITIONAL_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)
SALES_CONTACT_EMAIL = os.environ.get("SALES_CONTACT_EMAIL", "hiktan44@gmail.com").strip()
google_auth = GoogleAuthService()
account_service = AccountService(google_auth.data_dir)
stripe_billing = StripeBilling()
email_sender = ResendEmailSender()
agent_identity = AgentTokenVerifier()
unified_search = UnifiedSearchEngine(vat_index=vat_rate_index)


def _track_llm_telemetry(
    operation: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    try:
        account_service.record_llm_usage(
            operation=operation,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
        )
    except Exception:
        logger.exception("LLM kullanım telemetrisi kaydedilemedi")


register_llm_usage_hook(_track_llm_telemetry)


class FixedWindowRateLimiter:
    """Small fail-open fixed-window limiter for the public web endpoints."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        try:
            now = int(time.time())
            window = now // window_seconds
            with self._lock:
                current_window, count = self._entries.get(key, (window, 0))
                if current_window != window:
                    current_window, count = window, 0
                count += 1
                self._entries[key] = (current_window, count)

            retry_after = window_seconds - (now % window_seconds)
            return count <= limit, max(retry_after, 1)
        except Exception:
            logger.exception("Rate limiter failed open")
            return True, 0


rate_limiter = FixedWindowRateLimiter()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_response(
    request: Request,
    scope: str,
    *,
    limit: int = 30,
    window_seconds: int = 60,
) -> JSONResponse | None:
    allowed, retry_after = rate_limiter.check(
        f"{scope}:{_client_ip(request)}", limit=limit, window_seconds=window_seconds
    )
    if allowed:
        return None
    return JSONResponse(
        {
            "error": "Çok hızlı arama yapıyorsunuz. Lütfen kısa bir süre sonra yeniden deneyin.",
            "retry_after": retry_after,
        },
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def _quota_error(exc: QuotaExceeded) -> JSONResponse:
    """429 gövdesi: sebep kodu + hangi işlem + satın alınabilir üst paketler."""
    body: dict[str, Any] = {"error": str(exc), "code": "quota_exceeded"}
    operation = getattr(exc, "operation", None)
    if operation:
        body["operation"] = operation
    upgrade = getattr(exc, "upgrade", None)
    if upgrade:
        body["upgrade"] = upgrade
    return JSONResponse(body, status_code=429)


def _security_response(exc: SecurityViolation) -> JSONResponse:
    return JSONResponse({"error": str(exc), "code": exc.code}, status_code=403)


def _origin_key(value: str) -> tuple[str | None, str | None, int | None]:
    parts = urlsplit(value)
    return (parts.scheme, parts.hostname, parts.port)


def _trusted_request_origin(request: Request) -> None:
    """Reject cross-site browser POSTs while preserving non-browser MCP/API clients."""
    supplied = request.headers.get("origin") or ""
    if not supplied:
        return
    trusted = {_origin_key(PUBLIC_BASE_URL)}
    trusted.update(_origin_key(extra) for extra in ADDITIONAL_ALLOWED_ORIGINS)
    if _origin_key(supplied) not in trusted:
        raise SecurityViolation("Bu istek güvenilir uygulama adresinden gelmiyor.", code="origin_denied")


def _agent_or_browser_identity(request: Request) -> None:
    """Optionally require a signed browser session or short-lived agent token."""
    if os.environ.get("REQUIRE_AGENT_IDENTITY", "0") != "1":
        return
    if isinstance(getattr(request.state, "api_user", None), dict):
        return  # API anahtarı zaten doğrulanmış bir kullanıcı kimliğidir.
    session_token = request.cookies.get(google_auth.session_cookie, "")
    if session_token:
        try:
            google_auth.parse_session(session_token)
            return
        except AuthError:
            pass
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise SecurityViolation("Bu işlem için doğrulanmış kullanıcı veya ajan kimliği gerekir.", code="identity_required")
    agent_identity.verify(authorization.removeprefix("Bearer ").strip())


def _session_user(request: Request, *, required: bool = False) -> dict[str, Any] | None:
    token = request.cookies.get(google_auth.session_cookie, "")
    if token:
        try:
            return google_auth.parse_session(token)
        except AuthError:
            pass
    # API anahtarı kimliği yalnız `_api_key_identity` çağıran rotalarda kurulur; bu
    # yüzden anahtar, beyaz listeye alınmamış hiçbir rotaya erişemez.
    api_user = getattr(request.state, "api_user", None)
    if isinstance(api_user, dict):
        return api_user
    if required:
        raise AuthError("Bu işlem için Google hesabınızla giriş yapın.")
    return None


def _auth_error(exc: Exception, *, status_code: int = 401) -> JSONResponse:
    return JSONResponse({"error": str(exc), "code": "authentication_required"}, status_code=status_code)


def _required_user(request: Request) -> dict[str, Any]:
    user = _session_user(request, required=True)
    if user is None:  # Defensive for type narrowing; required=True already raises.
        raise AuthError("Bu işlem için Google hesabınızla giriş yapın.")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _required_user(request)
    if not account_service.is_admin(user):
        raise AuthError("Bu alan yalnızca yöneticilere açıktır.")
    return user


class FeatureNotAvailable(AccountError):
    """The signed user's plan or role does not include a gated feature."""

    def __init__(self, feature: str, message: str) -> None:
        super().__init__(message)
        self.feature = feature


def _require_role(request: Request, *roles: str) -> dict[str, Any]:
    """Require one of the given roles; the admin allow-list always passes."""
    user = _required_user(request)
    role = account_service.role_of(user)
    if role != "admin" and role not in roles:
        raise AuthError("Bu alan için yetkiniz yok.")
    return user


def require_feature(request: Request, feature: str) -> dict[str, Any] | None:
    """Gate a paid/role feature like _enforce_quota gates usage.

    Without Google OAuth (self-hosted) everything stays open. With OAuth the
    caller must be signed in and the plan or role must include the feature.
    """
    if feature not in account_service.feature_catalog():
        raise FeatureNotAvailable(feature, "Bilinmeyen özellik kilidi.")
    user = _session_user(request)
    if not user:
        if google_auth.configured:
            raise AuthError("Bu özellik için Google hesabınızla giriş yapın.")
        return None
    if feature not in account_service.capabilities_for(user):
        label = account_service.feature_catalog()[feature]
        plans = ", ".join(item["name"] for item in account_service.plans_with_feature(feature)) or "Kurumsal"
        raise FeatureNotAvailable(
            feature, f"{label} paketinizde yok. Hesabım alanından {plans} paketine geçebilirsiniz."
        )
    return user


_API_KEY_HEADER = "x-api-key"


def _api_key_identity(request: Request) -> dict[str, Any] | None:
    """ERP / dış sistem API anahtarını çözüp isteğe kimlik olarak bağlar.

    Yalnızca bu işlevi **açıkça çağıran** rotalar anahtar kabul eder; beyaz listede
    olmayan bir rotaya anahtarla erişilemez (hesap silme, ödeme, yönetim ve anahtar
    üretiminin kendisi buna dahildir — anahtar yeni anahtar üretemez).

    Anahtar yoksa ``None`` döner ve rota normal çerez oturumuyla ilerler.
    """
    raw = request.headers.get(_API_KEY_HEADER, "").strip()
    if not raw:
        authorization = request.headers.get("authorization", "")
        candidate = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
        # Kısa ömürlü ajan JWT'si ile karışmaması için yalnız kendi ön ekimiz anahtar sayılır.
        raw = candidate if candidate.startswith(f"{AccountService.API_KEY_PREFIX}_") else ""
    if not raw:
        return None
    resolved = account_service.authenticate_api_key(raw)
    if resolved is None:
        raise AuthError("API anahtarı geçersiz veya iptal edilmiş.")
    user = resolved["user"]
    if "api_access" not in account_service.capabilities_for(user):
        # Anahtar üretildikten sonra paket düşmüş olabilir; kilit her istekte yeniden okunur.
        label = account_service.feature_catalog()["api_access"]
        plans = ", ".join(item["name"] for item in account_service.plans_with_feature("api_access")) or "Kurumsal"
        raise FeatureNotAvailable("api_access", f"{label} paketinizde yok. {plans} paketine geçmeniz gerekir.")
    # Anahtarla gelen her istek ayrıca sayılır: rotanın kendi kotası (ön değerlendirme,
    # kanıt dosyası) aynen işler, `api_call` yalnız API kullanımını görünür kılar.
    account_service.consume(user, "api_call")
    request.state.api_user = user
    return user


def _feature_error(exc: FeatureNotAvailable) -> JSONResponse:
    return JSONResponse(
        {
            "error": str(exc),
            "code": "feature_required",
            "feature": exc.feature,
            "plans": account_service.plans_with_feature(exc.feature),
        },
        status_code=403,
    )


def _enforce_quota(request: Request, operation: str) -> dict[str, Any] | None:
    """Check a signed user's quota; require login when Google OAuth is enabled."""
    user = _session_user(request)
    if not user:
        if google_auth.configured:
            raise AuthError("Bu analiz için Google hesabınızla giriş yapın.")
        return None
    account = account_service.account(user)
    quota = account["quotas"][operation]
    if quota["remaining"] is not None and quota["remaining"] <= 0:
        plan_code = str((account.get("plan") or {}).get("code") or "starter")
        raise QuotaExceeded(
            f"Aylık {operation} kotanız doldu. Hesabım alanından paketinizi yükseltebilirsiniz.",
            operation=operation,
            upgrade=account_service.upgrade_options(plan_code, operation),
        )
    return user


def _record_usage(user: dict[str, Any] | None, operation: str) -> None:
    if user:
        account_service.consume(user, operation)


def _tri_state(value: Any) -> bool | None:
    """'true'/'false'/boş üçlü seçim: belirtilmediyse None döner (varsayım yapılmaz)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "evet", "var", "yes"}


def _normalise_date(value: Any) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        year, month, day = value.split("-")
        return f"{day}/{month}/{year}"
    return value


def _document_json(document: Any) -> dict[str, Any]:
    type_code = ""
    type_label = "Mevzuat"
    if isinstance(document.mevzuat_tur, dict):
        type_code = str(document.mevzuat_tur.get("name", ""))
        type_label = str(
            document.mevzuat_tur.get("description")
            or document.mevzuat_tur.get("name")
            or type_label
        )
    elif document.mevzuat_tur:
        type_code = type_label = str(document.mevzuat_tur)

    gazette_date = document.resmi_gazete_tarihi
    if gazette_date and "T" in gazette_date:
        gazette_date = gazette_date.split("T", 1)[0]

    return {
        "id": document.mevzuat_id,
        "number": str(document.mevzuat_no or ""),
        "title": document.mevzuat_adi,
        "type": type_code,
        "type_label": type_label,
        "gazette_date": gazette_date,
        "gazette_number": document.resmi_gazete_sayisi,
        "rationale_id": document.gerekce_id,
        "source_url": document.url,
    }


_TICARET_CONTENT_KINDS = {
    "mevzuat",
    "destek",
    "veri",
    "rapor",
    "ulke_bilgisi",
    "iletisim",
    "yayin",
}


def _ticaret_document_json(document: Any) -> dict[str, Any]:
    """Return the stable, public subset used by the research interface."""
    return {
        "id": document.id,
        "title": document.title,
        "source_id": document.source_id,
        "content_kind": document.content_kind,
        "section": document.section,
        "subsection": document.subsection,
        "document_type": document.document_type,
        "number": document.number,
        "publication_date": document.publication_date,
        "official_gazette": document.official_gazette,
        "page_updated_at": document.page_updated_at,
        "document_url": document.document_url,
        "source_page_url": document.source_page_url,
        "file_type": document.file_type,
        "is_page": document.is_page,
        "is_repealed": document.is_repealed,
        "context": document.context,
    }


@mcp.custom_route("/", methods=["GET"])
async def web_index(request: Request):
    landing = (WEB_DIR / "landing.html").read_text(encoding="utf-8")
    verification = html.escape(os.environ.get("GOOGLE_SITE_VERIFICATION", ""), quote=True)
    landing = landing.replace("{{GOOGLE_SITE_VERIFICATION}}", verification)
    return HTMLResponse(
        landing,
        headers={"Cache-Control": "public, max-age=300", "Vary": "Accept-Encoding"},
    )


@mcp.custom_route("/app", methods=["GET"])
@mcp.custom_route("/app/", methods=["GET"])
async def web_application(request: Request):
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@mcp.custom_route("/admin", methods=["GET"])
async def web_admin(request: Request):
    try:
        _require_admin(request)
    except AuthError:
        return RedirectResponse("/app?account=login", status_code=303)
    return FileResponse(WEB_DIR / "admin.html", media_type="text/html")


@mcp.custom_route("/gizlilik", methods=["GET"])
async def web_privacy(request: Request):
    return FileResponse(WEB_DIR / "privacy.html", media_type="text/html")


@mcp.custom_route("/kullanim-kosullari", methods=["GET"])
async def web_terms(request: Request):
    return FileResponse(WEB_DIR / "terms.html", media_type="text/html")


@mcp.custom_route("/assets/app.css", methods=["GET"])
async def web_css(request: Request):
    return FileResponse(WEB_DIR / "app.css", media_type="text/css")


@mcp.custom_route("/assets/app.js", methods=["GET"])
async def web_js(request: Request):
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@mcp.custom_route("/assets/admin.js", methods=["GET"])
async def web_admin_js(request: Request):
    return FileResponse(WEB_DIR / "admin.js", media_type="text/javascript")


@mcp.custom_route("/assets/landing.css", methods=["GET"])
async def web_landing_css(request: Request):
    return FileResponse(WEB_DIR / "landing.css", media_type="text/css")


@mcp.custom_route("/assets/landing.js", methods=["GET"])
async def web_landing_js(request: Request):
    return FileResponse(WEB_DIR / "landing.js", media_type="text/javascript")


@mcp.custom_route("/favicon.svg", methods=["GET"])
async def web_favicon(request: Request):
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


@mcp.custom_route("/assets/og-image.svg", methods=["GET"])
async def web_og_image(request: Request):
    return FileResponse(WEB_DIR / "og-image.svg", media_type="image/svg+xml")


@mcp.custom_route("/assets/og-image.png", methods=["GET"])
async def web_og_image_png(request: Request):
    return FileResponse(
        WEB_DIR / "og-image.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@mcp.custom_route("/site.webmanifest", methods=["GET"])
async def web_manifest(request: Request):
    return JSONResponse(
        {
            "name": "Ticaret Bilgi Masası",
            "short_name": "Ticaret Masası",
            "description": "Türkiye gümrük ve dış ticaret mevzuatı için resmî kaynaklı araştırma ve ön değerlendirme.",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#eef6fa",
            "theme_color": "#08233d",
            "icons": [{"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"}],
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@mcp.custom_route("/robots.txt", methods=["GET"])
async def web_robots(request: Request):
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /auth/\nDisallow: /mcp\n"
        f"Sitemap: {PUBLIC_BASE_URL}/sitemap.xml\n",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@mcp.custom_route("/sitemap.xml", methods=["GET"])
async def web_sitemap(request: Request):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{PUBLIC_BASE_URL}/</loc><lastmod>2026-08-30</lastmod>"
        "<changefreq>daily</changefreq><priority>1.0</priority></url>"
        f"<url><loc>{PUBLIC_BASE_URL}/gizlilik</loc><lastmod>2026-08-30</lastmod>"
        "<changefreq>monthly</changefreq><priority>0.3</priority></url>"
        f"<url><loc>{PUBLIC_BASE_URL}/kullanim-kosullari</loc><lastmod>2026-08-30</lastmod>"
        "<changefreq>monthly</changefreq><priority>0.3</priority></url>"
        "</urlset>"
    )
    return Response(
        xml,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _google_redirect_uri(request: Request | None = None) -> str:
    if request:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if host:
            if (proto == "https" and host.endswith(":443")) or (proto == "http" and host.endswith(":80")):
                host = host.rsplit(":", 1)[0]
            origin = f"{proto}://{host}"
            trusted = {_origin_key(PUBLIC_BASE_URL)}
            trusted.update(_origin_key(extra) for extra in ADDITIONAL_ALLOWED_ORIGINS)
            trusted.add(_origin_key("https://mevzuat-mcp.seymata.com"))
            trusted.add(_origin_key("https://gumruksor.com"))
            trusted.add(_origin_key("https://www.gumruksor.com"))
            trusted.add(_origin_key("http://localhost:8000"))
            trusted.add(_origin_key("http://127.0.0.1:8000"))
            if _origin_key(origin) in trusted:
                return f"{origin}/auth/google/callback"
    return f"{PUBLIC_BASE_URL}/auth/google/callback"


def _secure_cookie() -> bool:
    return PUBLIC_BASE_URL.startswith("https://")


@mcp.custom_route("/api/auth/me", methods=["GET"])
async def web_auth_me(request: Request):
    session = _session_user(request)
    account = account_service.account(session) if session else None
    response = JSONResponse(
        {
            "authenticated": session is not None,
            "google_enabled": google_auth.configured,
            "user": (
                {
                    "email": session.get("email", ""),
                    "name": session.get("name", ""),
                    "picture": session.get("picture", ""),
                    "is_admin": bool(account and account["is_admin"]),
                }
                if session
                else None
            ),
            "account": account,
            "billing_enabled": stripe_billing.configured,
            "billing_provider": "stripe",
            "billing_mode": stripe_billing.mode,
            "email_enabled": email_sender.configured,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@mcp.custom_route("/auth/google", methods=["GET"])
async def web_google_login(request: Request):
    limited = _rate_limit_response(request, "auth-login", limit=10, window_seconds=900)
    if limited:
        return limited
    if not google_auth.configured:
        return RedirectResponse("/?auth=google-setup#login", status_code=303)
    redirect_uri = _google_redirect_uri(request)
    state, nonce = google_auth.create_oauth_state(redirect_uri=redirect_uri)
    response = RedirectResponse(
        google_auth.authorization_url(
            redirect_uri=redirect_uri, state=state, nonce=nonce
        ),
        status_code=303,
    )
    response.set_cookie(
        google_auth.state_cookie,
        state,
        max_age=600,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/auth",
    )
    return response


@mcp.custom_route("/auth/google/callback", methods=["GET"])
async def web_google_callback(request: Request):
    limited = _rate_limit_response(request, "auth-callback", limit=10, window_seconds=900)
    if limited:
        return limited
    state = request.query_params.get("state", "")
    state_cookie = request.cookies.get(google_auth.state_cookie, "")
    code = request.query_params.get("code", "")
    if request.query_params.get("error"):
        return RedirectResponse("/?auth=cancelled#login", status_code=303)
    try:
        if not state or not state_cookie or not hmac.compare_digest(state, state_cookie):
            raise AuthError("Google giriş isteği eşleşmedi.")
        state_payload = google_auth.verify_oauth_state(state_cookie)
        callback_redirect_uri = str(state_payload.get("redirect_uri") or _google_redirect_uri(request))
        profile = await google_auth.exchange_code(
            code=code,
            redirect_uri=callback_redirect_uri,
            expected_nonce=str(state_payload.get("nonce", "")),
        )
        google_auth.upsert_user(profile)
        session_token = google_auth.create_session(profile)
    except (AuthError, httpx.HTTPError):
        logger.warning("Google login callback could not be verified", exc_info=True)
        response = RedirectResponse("/?auth=failed#login", status_code=303)
        response.delete_cookie(google_auth.state_cookie, path="/auth")
        return response

    response = RedirectResponse("/app", status_code=303)
    response.set_cookie(
        google_auth.session_cookie,
        session_token,
        max_age=google_auth.session_ttl_seconds,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/",
    )
    response.delete_cookie(google_auth.state_cookie, path="/auth")
    return response


@mcp.custom_route("/auth/logout", methods=["POST"])
async def web_logout(request: Request):
    limited = _rate_limit_response(request, "auth-logout", limit=10, window_seconds=900)
    if limited:
        return limited
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(google_auth.session_cookie, path="/")
    return response


@mcp.custom_route("/api/plans", methods=["GET"])
async def web_plans(request: Request):
    return JSONResponse(
        {
            "plans": account_service.public_plans(),
            "features": account_service.feature_catalog(),
            "billing_enabled": stripe_billing.configured,
            "billing_provider": "stripe",
            "billing_mode": stripe_billing.mode,
            "sales_email": SALES_CONTACT_EMAIL,
        }
    )


@mcp.custom_route("/api/account", methods=["GET"])
async def web_account(request: Request):
    try:
        user = _required_user(request)
        return JSONResponse(account_service.account(user), headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc)


def _build_compliance_report(google_sub: str) -> dict[str, Any]:
    """Deterministic compliance dashboard for one user (PRD Faz 2.6); no LLM involved."""
    return compliance_report(
        account_service, google_sub,
        ledger=change_ledger, trade_engine=trade_measure_engine, control_engine=control_engine,
    )


@mcp.custom_route("/api/account/compliance", methods=["GET"])
async def web_account_compliance(request: Request):
    limited = _rate_limit_response(request, "account-compliance", limit=30, window_seconds=60)
    if limited:
        return limited
    try:
        user = _required_user(request)
        report = await asyncio.to_thread(_build_compliance_report, str(user["sub"]))
        report["email_alerts"] = "change_alerts" in account_service.capabilities_for(user)
        return JSONResponse(report, headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc)
    except Exception:
        logger.exception("Compliance report failed")
        return JSONResponse({"error": "Uyum raporu şu anda oluşturulamadı."}, status_code=500, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/account", methods=["DELETE"])
async def web_delete_account(request: Request):
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        subscription = account_service.subscription_for_user(user)
        if subscription and subscription.get("provider") and subscription.get("status") in {"active", "pending", "past_due"}:
            reference = str(subscription.get("provider_subscription_ref") or "")
            if not reference:
                raise BillingError("Ücretli abonelik referansı bulunamadığı için hesap güvenle silinemedi.")
            if subscription.get("provider") != "stripe":
                raise BillingError("Eski ödeme sağlayıcısındaki abonelik önce yönetici tarafından kapatılmalıdır.")
            await stripe_billing.cancel_subscription(reference)
        account_service.delete_account(user)
        response = JSONResponse({"deleted": True})
        response.delete_cookie(google_auth.session_cookie, path="/")
        return response
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (BillingError, httpx.HTTPError) as exc:
        logger.warning("Account deletion stopped because subscription cancellation failed", exc_info=True)
        return JSONResponse({"error": f"Abonelik iptal edilemedi; hesap silinmedi. {exc}"}, status_code=502)


def _safe_declaration_draft(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Kanıt dosyasıyla birlikte saklanacak beyanname taslağı.

    Taslak üretimi saf ve ağsızdır, ama bozuk/eksik bir gövde yüzünden dosya kaydının
    tamamı düşmemelidir: taslak üretilemezse dosya taslaksız kaydedilir ve kullanıcı
    onu sonradan ``/api/customs/declaration-draft`` ile yeniden üretebilir.
    """
    try:
        return build_declaration_draft(payload).model_dump(mode="json")
    except Exception:
        logger.warning("Beyanname taslağı kanıt dosyası için üretilemedi", exc_info=True)
        return None


def _dossier_evidence() -> dict[str, Any]:
    tariff_status = tariff_engine.status().model_dump(mode="json")
    control_status = control_engine.status().model_dump(mode="json")
    classification_status = classification_engine.status().model_dump(mode="json")
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tariff": {
            "last_checked_at": tariff_status.get("last_checked_at"),
            "active_snapshots": tariff_status.get("active_snapshots", []),
            "errors": tariff_status.get("errors", []),
        },
        "controls": {
            "last_checked_at": control_status.get("last_checked_at"),
            "active_snapshots": control_status.get("active_snapshots", []),
            "errors": control_status.get("errors", []),
        },
        "classification_evidence": {
            "last_checked_at": classification_status.get("last_checked_at"),
            "active_sha256": classification_status.get("active_sha256"),
            "page_count": classification_status.get("page_count", 0),
            "errors": classification_status.get("errors", []),
        },
        "legal_notice": (
            "Bu dosya oluşturulduğu andaki kanuni metinler ve resmî veri anlık görüntüleriyle hazırlanmıştır. "
            "Sonraki değişiklikler için yürürlük tarihi, GTİP, menşe ve dipnotlar yeniden doğrulanmalıdır."
        ),
    }


@mcp.custom_route("/api/account/api-keys", methods=["GET"])
async def web_api_keys(request: Request):
    """Hesabın API anahtarlarının künyesi. Gizli değer burada asla dönmez."""
    try:
        user = _required_user(request)
        return JSONResponse(
            {"items": account_service.list_api_keys(user), "limit": AccountService.API_KEY_LIMIT},
            headers={"Cache-Control": "no-store"},
        )
    except AuthError as exc:
        return _auth_error(exc)


@mcp.custom_route("/api/account/api-keys", methods=["POST"])
async def web_create_api_key(request: Request):
    """Yeni API anahtarı üretir. Açık değer yalnızca bu yanıtta bir kez görünür.

    Bilerek yalnız çerez oturumuyla çalışır: bir anahtarın kendi yerine yenisini
    üretebilmesi, çalınan anahtarın kalıcı hâle gelmesi demek olurdu.
    """
    limited = _rate_limit_response(request, "api-keys", limit=10, window_seconds=3600)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        require_feature(request, "api_access")
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        label = str((body or {}).get("label", "")) if isinstance(body, dict) else ""
        created = account_service.create_api_key(user, label=label)
        return JSONResponse(created, status_code=201, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/account/api-keys/{key_id}", methods=["DELETE"])
async def web_revoke_api_key(request: Request):
    """Anahtarı iptal eder. İptal geri alınamaz; kayıt denetim için saklanır."""
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        revoked = account_service.revoke_api_key(user, request.path_params["key_id"])
        return JSONResponse(revoked, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@mcp.custom_route("/api/dossiers", methods=["GET"])
async def web_dossiers(request: Request):
    try:
        _api_key_identity(request)  # ERP beyaz listesi
        user = _required_user(request)
        return JSONResponse({"items": account_service.list_dossiers(user)}, headers={"Cache-Control": "no-store"})
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)


@mcp.custom_route("/api/dossiers", methods=["POST"])
async def web_create_dossier(request: Request):
    try:
        _trusted_request_origin(request)
        _api_key_identity(request)  # ERP beyaz listesi
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Kanıt dosyası isteği bir nesne olmalıdır.")
        payload = body.get("result")
        if not isinstance(payload, dict):
            raise AccountError("Kaydedilecek analiz sonucu eksik.")
        dossier = account_service.create_dossier(
            user,
            title=str(body.get("title", "")),
            product_name=str(body.get("product_name", "")),
            gtip=str(body.get("gtip", "")) or None,
            origin_country=str(body.get("origin_country", "")) or None,
            effective_date=str(body.get("effective_date", "")) or None,
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
            payload=payload,
            evidence=_dossier_evidence(),
            draft=_safe_declaration_draft(payload),
        )
        return JSONResponse(dossier, status_code=201, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    except (AccountError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/dossiers/{dossier_id}", methods=["GET"])
async def web_get_dossier(request: Request):
    try:
        _api_key_identity(request)  # ERP beyaz listesi
        user = _required_user(request)
        dossier = account_service.get_dossier(user, request.path_params.get("dossier_id", ""))
        download = request.query_params.get("download") == "1"
        headers = {"Cache-Control": "no-store"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="kanit-dosyasi-{dossier["id"]}.json"'
        return JSONResponse(dossier, headers=headers)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@mcp.custom_route("/api/dossiers/{dossier_id}", methods=["DELETE"])
async def web_delete_dossier(request: Request):
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        deleted = account_service.delete_dossier(user, request.path_params.get("dossier_id", ""))
        return JSONResponse({"deleted": deleted}, status_code=200 if deleted else 404)
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)


@mcp.custom_route("/api/billing/checkout", methods=["POST"])
async def web_billing_checkout(request: Request):
    limited = _rate_limit_response(request, "billing-checkout", limit=5, window_seconds=900)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise BillingError("Ödeme isteği geçersiz.")
        plan_code = str(body.get("plan_code", ""))
        billing_cycle = str(body.get("billing_cycle", ""))
        payment = account_service.create_payment_session(user, plan_code, billing_cycle)
        subscription = account_service.subscription_for_user(user)
        customer_reference = None
        if subscription and subscription.get("provider") == "stripe":
            customer_reference = str(subscription.get("provider_customer_ref") or "") or None
        checkout = await stripe_billing.create_checkout(
            plan_code=plan_code,
            billing_cycle=billing_cycle,
            payment_session_id=payment["id"],
            google_sub=str(user["sub"]),
            email=str(user.get("email", "")),
            customer_reference=customer_reference,
            public_base_url=PUBLIC_BASE_URL,
            expected_amount_try=int(
                PLANS[plan_code].monthly_price_try
                if billing_cycle == "monthly"
                else PLANS[plan_code].yearly_price_try
            ),
        )
        account_service.attach_payment_token(payment["id"], checkout["session_id"])
        return JSONResponse(checkout, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (BillingError, AccountError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503 if not stripe_billing.configured else 422)


@mcp.custom_route("/api/billing/stripe/return", methods=["GET"])
async def web_billing_stripe_return(request: Request):
    try:
        session_id = request.query_params.get("session_id", "")
        checkout = await stripe_billing.retrieve_checkout(session_id)
        account_service.complete_stripe_checkout(checkout)
        return RedirectResponse("/app?payment=success&account=open", status_code=303)
    except (BillingError, AccountError):
        logger.warning("Stripe checkout return could not be confirmed", exc_info=True)
        return RedirectResponse("/app?payment=failed&account=open", status_code=303)


@mcp.custom_route("/api/billing/stripe/webhook", methods=["POST"])
async def web_billing_stripe_webhook(request: Request):
    try:
        raw_body = await request.body()
        signature = request.headers.get("stripe-signature", "")
        event = stripe_billing.verify_webhook(raw_body, signature)
        account_service.process_stripe_webhook(event, stripe_billing.price_lookup())
        return JSONResponse({"received": True})
    except BillingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/billing/portal", methods=["POST"])
async def web_billing_portal(request: Request):
    limited = _rate_limit_response(request, "billing-portal", limit=10, window_seconds=900)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        subscription = account_service.subscription_for_user(user)
        if not subscription or subscription.get("provider") != "stripe":
            raise BillingError("Yönetilebilecek bir Stripe aboneliği bulunamadı.")
        url = await stripe_billing.create_portal(
            str(subscription.get("provider_customer_ref") or ""), PUBLIC_BASE_URL
        )
        return JSONResponse({"portal_url": url}, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except BillingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503 if not stripe_billing.configured else 422)


@mcp.custom_route("/api/consultants", methods=["GET"])
async def web_consultants(request: Request):
    limited = _rate_limit_response(request, "consultants-list", limit=60, window_seconds=60)
    if limited:
        return limited
    # The marketplace stays hidden until real consultant profiles exist; demo or
    # seed rows must never look like a live directory (CONSULTANTS_MARKETPLACE_ENABLED=1 to open).
    if os.environ.get("CONSULTANTS_MARKETPLACE_ENABLED", "0") != "1":
        return JSONResponse({"items": [], "enabled": False}, headers={"Cache-Control": "no-store"})
    return JSONResponse(
        {"items": account_service.list_consultants(), "enabled": True},
        headers={"Cache-Control": "no-store"},
    )


@mcp.custom_route("/api/consultants/me", methods=["GET"])
async def web_consultant_profile(request: Request):
    try:
        user = _required_user(request)
        return JSONResponse({"profile": account_service.consultant_profile(user)}, headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc)


@mcp.custom_route("/api/consultants/me", methods=["POST"])
async def web_apply_consultant(request: Request):
    limited = _rate_limit_response(request, "consultant-application", limit=5, window_seconds=3600)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Danışman başvurusu geçersiz.")
        guard_data(body, path="danışman başvurusu")
        body = redact_data(body, contact_data=True)
        profile = account_service.apply_as_consultant(user, body)
        return JSONResponse({"profile": profile}, status_code=201, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (AccountError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/consultation-requests", methods=["GET"])
async def web_consultation_requests(request: Request):
    try:
        user = _required_user(request)
        return JSONResponse(account_service.list_consultation_requests(user), headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc)


_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _opaque_identifier(value: Any) -> str:
    """Accept only a compact server-issued id (UUID/slug); anything else becomes empty."""
    text = str(value or "").strip()
    return text if _OPAQUE_ID_RE.fullmatch(text) else ""


@mcp.custom_route("/api/consultation-requests", methods=["POST"])
async def web_create_consultation_request(request: Request):
    limited = _rate_limit_response(request, "consultation-request", limit=10, window_seconds=86_400)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Danışmanlık talebi geçersiz.")
        guard_data(body, path="danışmanlık talebi")
        # Identifiers are server-issued UUIDs, not prose: read them before contact
        # redaction, whose phone pattern can otherwise mangle a digit run inside a UUID.
        consultant_id = _opaque_identifier(body.get("consultant_id"))
        dossier_id = _opaque_identifier(body.get("dossier_id"))
        body = redact_data(body, contact_data=True)
        result = account_service.create_consultation_request(
            user,
            consultant_id=consultant_id,
            subject=str(body.get("subject", "")),
            message=str(body.get("message", "")),
            result=body.get("result") if isinstance(body.get("result"), dict) else {},
            share_consent=body.get("share_consent") is True,
            dossier_id=dossier_id or None,
        )
        asyncio.create_task(_notify_consultation(str(result.get("id", "")), "new_request", str(body.get("message", "")), recipient_role="consultant"))
        return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (AccountError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/consultation-requests/{request_id}", methods=["PATCH"])
async def web_update_consultation_request(request: Request):
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Danışmanlık talebi güncellemesi geçersiz.")
        request_id = request.path_params.get("request_id", "")
        account_service.update_consultation_request(user, request_id, str(body.get("status", "")))
        participants = account_service.consultation_participants(request_id)
        if participants:
            role = "requester" if str(user.get("sub")) == str(participants["consultant_sub"]) else "consultant"
            asyncio.create_task(_notify_consultation(request_id, "status", f"Yeni durum: {body.get('status', '')}", recipient_role=role))
        return JSONResponse({"updated": True})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/consultation-requests/{request_id}/messages", methods=["POST"])
async def web_add_consultation_message(request: Request):
    limited = _rate_limit_response(request, "consultation-message", limit=100, window_seconds=86_400)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Danışman mesajı geçersiz.")
        guard_data(body, path="danışman mesajı")
        body = redact_data(body, contact_data=True)
        request_id = request.path_params.get("request_id", "")
        result = account_service.add_consultation_message(user, request_id, str(body.get("body", "")))
        participants = account_service.consultation_participants(request_id)
        if participants:
            role = "requester" if str(user.get("sub")) == str(participants["consultant_sub"]) else "consultant"
            asyncio.create_task(_notify_consultation(request_id, "message", str(body.get("body", "")), recipient_role=role))
        return JSONResponse(result, status_code=201)
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/admin/overview", methods=["GET"])
async def web_admin_overview(request: Request):
    try:
        _require_admin(request)
        return JSONResponse(account_service.admin_overview(), headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc, status_code=403)


@mcp.custom_route("/api/admin/subscriptions/{google_sub}", methods=["PUT"])
async def web_admin_subscription(request: Request):
    try:
        _trusted_request_origin(request)
        actor = _require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Abonelik güncellemesi geçersiz.")
        account_service.admin_set_plan(
            actor, request.path_params.get("google_sub", ""),
            str(body.get("plan_code", "")), str(body.get("status", "")),
        )
        return JSONResponse({"updated": True})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/admin/consultants/{google_sub}", methods=["PUT"])
async def web_admin_consultant(request: Request):
    try:
        _trusted_request_origin(request)
        actor = _require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Danışman profili güncellemesi geçersiz.")
        account_service.admin_set_consultant_status(
            actor, request.path_params.get("google_sub", ""), str(body.get("status", ""))
        )
        return JSONResponse({"updated": True})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/admin/llm-expenses", methods=["GET"])
async def web_admin_llm_expenses(request: Request):
    try:
        _require_admin(request)
        period = request.query_params.get("filter", "monthly")
        return JSONResponse(account_service.admin_llm_expenses(period), headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc, status_code=403)


@mcp.custom_route("/api/admin/llm-diagnostics", methods=["GET"])
async def web_admin_llm_diagnostics(request: Request):
    """Live provider connectivity test (admin only); never returns secrets."""
    limited = _rate_limit_response(request, "admin-llm-diagnostics", limit=6, window_seconds=60)
    if limited:
        return limited
    try:
        _require_admin(request)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    vision = request.query_params.get("vision", "").strip().lower() in {"1", "true", "yes"}
    recent_only = request.query_params.get("recent", "").strip().lower() in {"1", "true", "yes"}
    try:
        from customs_advisor import diagnose_llm_providers, recent_llm_events

        if recent_only:
            # No live probe: only the in-memory record of the latest real calls.
            report = {"mode": "recent", "recent": recent_llm_events()}
        else:
            report = await diagnose_llm_providers(vision=vision)
    except Exception:
        logger.exception("LLM diagnostics failed")
        return JSONResponse({"error": "Bağlantı testi çalıştırılamadı."}, status_code=500)
    return JSONResponse(redact_data(report), headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/admin/payments", methods=["GET"])
async def web_admin_payments(request: Request):
    try:
        _require_admin(request)
        return JSONResponse(account_service.admin_payments_overview(), headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc, status_code=403)


@mcp.custom_route("/api/admin/logs", methods=["GET"])
async def web_admin_logs(request: Request):
    try:
        _require_admin(request)
        limit = int(request.query_params.get("limit", "200"))
        google_sub = request.query_params.get("user")
        return JSONResponse({"logs": account_service.admin_user_logs(limit, google_sub)}, headers={"Cache-Control": "no-store"})
    except (ValueError, TypeError):
        return JSONResponse({"logs": account_service.admin_user_logs(200)}, headers={"Cache-Control": "no-store"})
    except AuthError as exc:
        return _auth_error(exc, status_code=403)


@mcp.custom_route("/api/admin/users/{google_sub}/role", methods=["PUT"])
async def web_admin_user_role(request: Request):
    """Assign user/consultant/editor/admin role (admin only; audited)."""
    try:
        _trusted_request_origin(request)
        actor = _require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Rol güncellemesi geçersiz.")
        account_service.admin_set_role(actor, request.path_params.get("google_sub", ""), str(body.get("role", "")))
        return JSONResponse({"updated": True})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/admin/users/{google_sub}/credits", methods=["POST"])
async def web_admin_grant_credit(request: Request):
    try:
        _trusted_request_origin(request)
        actor = _require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("Kredi ekleme bilgisi geçersiz.")
        res = account_service.admin_grant_credit(
            actor,
            request.path_params.get("google_sub", ""),
            str(body.get("operation", "all")),
            int(body.get("quantity", 0)),
            str(body.get("note", "")),
        )
        return JSONResponse(res)
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    except (AccountError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/search", methods=["POST"])
async def web_search(request: Request):
    limited = _rate_limit_response(request, "search")
    if limited:
        return limited

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçerli bir arama isteği gönderin."}, status_code=400)

    query = str(body.get("query", "")).strip()
    mode = str(body.get("mode", "title"))
    type_code = str(body.get("type", "")).strip().upper()
    if len(query) > 200:
        return JSONResponse({"error": "Arama metni en fazla 200 karakter olabilir."}, status_code=422)
    if mode not in {"title", "content", "number"}:
        return JSONResponse({"error": "Geçersiz arama türü."}, status_code=422)
    if type_code and type_code not in _BED_VALID_TYPES:
        return JSONResponse({"error": "Geçersiz mevzuat türü."}, status_code=422)

    try:
        page = max(1, min(int(body.get("page", 1)), 10000))
        page_size = max(1, min(int(body.get("page_size", 20)), 50))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Geçersiz sayfa bilgisi."}, status_code=422)

    search_args: dict[str, Any] = {
        "phrase": query if mode == "content" else "",
        "mevzuat_adi": query if mode == "title" else "",
        "mevzuat_no": query if mode == "number" and query else None,
        "mevzuat_tur_list": [type_code] if type_code else list(_BED_VALID_TYPES),
        "resmi_gazete_tarihi_start": _normalise_date(body.get("start_date")),
        "resmi_gazete_tarihi_end": _normalise_date(body.get("end_date")),
        "page": page,
        "page_size": page_size,
        "sort_field": "RESMI_GAZETE_TARIHI",
        "sort_direction": "desc",
    }

    result = await bedesten_client.search_documents(**search_args)
    if result.error_message:
        logger.warning("Web search failed: %s", result.error_message)
        return JSONResponse(
            {"error": "Mevzuat kaynağına şu anda ulaşılamıyor. Lütfen yeniden deneyin."},
            status_code=502,
        )

    return JSONResponse(
        {
            "documents": _deprioritise_future_gazette_dates(
                [_document_json(document) for document in result.documents]
            ),
            "total": result.total_results,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < result.total_results,
        }
    )


def _deprioritise_future_gazette_dates(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort-order guard: a future gazette date is a source data error, not news.

    The official API sorts by gazette date descending, so one bad date from the
    source would permanently top the list. Such records keep their data (source
    fidelity) but move to the end of the page and carry an explicit warning.
    """
    today = datetime.now(UTC).date()
    clean: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []
    for document in documents:
        raw_date = str(document.get("gazette_date") or "")
        try:
            is_future = bool(raw_date) and date.fromisoformat(raw_date) > today
        except ValueError:
            is_future = False
        if is_future:
            document["date_warning"] = (
                "Resmî kaynak bu kayıt için gelecek bir tarih gösteriyor; tarih kaynak hatası olabilir."
            )
            flagged.append(document)
        else:
            clean.append(document)
    return clean + flagged


@mcp.custom_route("/api/document/{mevzuat_id}", methods=["GET"])
async def web_document(request: Request):
    limited = _rate_limit_response(request, "document")
    if limited:
        return limited

    mevzuat_id = request.path_params.get("mevzuat_id", "")
    if not mevzuat_id.isdigit() or len(mevzuat_id) > 20:
        return JSONResponse({"error": "Geçersiz mevzuat kimliği."}, status_code=422)

    plain = await bedesten_client.get_document_plain_text(mevzuat_id)
    if not plain:
        return JSONResponse({"error": "Mevzuat metni bulunamadı."}, status_code=404)
    return JSONResponse({"id": mevzuat_id, "content": plain})


@mcp.custom_route("/api/ticaret/status", methods=["GET"])
async def web_ticaret_status(request: Request):
    limited = _rate_limit_response(request, "ticaret-status")
    if limited:
        return limited
    return JSONResponse(ticaret_client.status().model_dump(mode="json"))


@mcp.custom_route("/api/ticaret/sources", methods=["GET"])
async def web_ticaret_sources(request: Request):
    limited = _rate_limit_response(request, "ticaret-sources")
    if limited:
        return limited
    try:
        return JSONResponse(await ticaret_client.list_sources())
    except Exception:
        logger.exception("Ticaret source catalogue failed")
        return JSONResponse(
            {"error": "Ticaret Bakanlığı kaynak kataloğu şu anda hazırlanıyor."},
            status_code=503,
        )


@mcp.custom_route("/api/ticaret/search", methods=["POST"])
async def web_ticaret_search(request: Request):
    limited = _rate_limit_response(request, "ticaret-search")
    if limited:
        return limited

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçerli bir arama isteği gönderin."}, status_code=400)

    query = str(body.get("query", "")).strip()
    if len(query) > 300:
        return JSONResponse({"error": "Arama metni en fazla 300 karakter olabilir."}, status_code=422)

    raw_kinds = body.get("content_kinds") or []
    raw_sources = body.get("source_ids") or []
    raw_types = body.get("document_types") or []
    if not all(isinstance(item, str) for item in [*raw_kinds, *raw_sources, *raw_types]):
        return JSONResponse({"error": "Filtre değerleri metin olmalıdır."}, status_code=422)
    content_kinds = [item.strip() for item in raw_kinds if item.strip()]
    if any(item not in _TICARET_CONTENT_KINDS for item in content_kinds):
        return JSONResponse({"error": "Geçersiz bilgi katmanı."}, status_code=422)

    known_sources = {source.id for source in ticaret_client.sources}
    source_ids = [item.strip() for item in raw_sources if item.strip()]
    if any(item not in known_sources for item in source_ids):
        return JSONResponse({"error": "Geçersiz resmî kaynak."}, status_code=422)
    if len(raw_types) > 12 or any(len(item) > 80 for item in raw_types):
        return JSONResponse({"error": "Belge türü filtresi çok uzun."}, status_code=422)

    try:
        offset = max(0, min(int(body.get("offset", 0)), 100000))
        limit = max(1, min(int(body.get("limit", 20)), 50))
        raw_year = body.get("year")
        year = int(raw_year) if raw_year not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "Geçersiz sayfalama veya yıl bilgisi."}, status_code=422)
    if year is not None and not 1900 <= year <= 2100:
        return JSONResponse({"error": "Yıl 1900 ile 2100 arasında olmalıdır."}, status_code=422)

    try:
        result = await ticaret_client.search(
            query=query,
            content_kinds=content_kinds or None,
            source_ids=source_ids or None,
            document_types=[item.strip() for item in raw_types if item.strip()] or None,
            year=year,
            include_repealed=bool(body.get("include_repealed", False)),
            offset=offset,
            limit=limit,
        )
    except Exception:
        logger.exception("Ticaret catalogue search failed")
        return JSONResponse(
            {"error": "Ticaret Bakanlığı kataloğunda arama şu anda tamamlanamadı."},
            status_code=502,
        )

    return JSONResponse(
        {
            "documents": [_ticaret_document_json(item) for item in result.documents],
            "total": result.total_results,
            "offset": result.offset,
            "limit": result.limit,
            "has_next": result.offset + result.limit < result.total_results,
            "catalog_synced_at": result.catalog_synced_at,
            "excluded_repealed": result.excluded_repealed,
            "note": result.note,
        }
    )


@mcp.custom_route("/api/ticaret/document/{document_id}", methods=["GET"])
async def web_ticaret_document(request: Request):
    limited = _rate_limit_response(request, "ticaret-document")
    if limited:
        return limited

    document_id = request.path_params.get("document_id", "")
    if not document_id.startswith("ticaret_") or len(document_id) != 32:
        return JSONResponse({"error": "Geçersiz belge kimliği."}, status_code=422)
    try:
        offset = max(0, min(int(request.query_params.get("offset", "0")), 10_000_000))
        content = await ticaret_client.get_document_content(
            document_id,
            offset=offset,
            max_characters=60_000,
        )
    except ValueError as exc:
        if str(exc).startswith("Belge bulunamadı:"):
            return JSONResponse({"error": "Belge katalogda bulunamadı."}, status_code=404)
        logger.warning("Ticaret document could not be extracted: %s: %s", document_id, exc)
        return JSONResponse(
            {"error": "Bu bağlantıdan metin çıkarılamadı. Resmî kaynak bağlantısını açabilirsiniz."},
            status_code=502,
        )
    except Exception:
        logger.exception("Ticaret document extraction failed: %s", document_id)
        return JSONResponse(
            {"error": "Belge metni resmî kaynaktan alınamadı. Kaynak bağlantısını açabilirsiniz."},
            status_code=502,
        )

    return JSONResponse(
        {
            "document": _ticaret_document_json(content.document),
            "content": content.content,
            "total_characters": content.total_characters,
            "offset": content.offset,
            "returned_characters": content.returned_characters,
            "truncated": content.truncated,
            "resolved_url": content.resolved_url,
            "fetched_at": content.fetched_at,
            "warnings": content.warnings,
        }
    )


@mcp.custom_route("/api/customs/describe-image", methods=["POST"])
async def web_customs_describe_image(request: Request):
    """Extract editable visual product attributes; do not run GTIP/TAREKS research."""
    limited = _rate_limit_response(request, "customs-vision", limit=20, window_seconds=60)
    if limited:
        return limited
    upload_limited = _rate_limit_response(request, "customs-upload", limit=30, window_seconds=3600)
    if upload_limited:
        return upload_limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
        quota_user = _enforce_quota(request, "vision")
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    MAX_IMAGE_REQUEST_BYTES = 12 * 1024 * 1024
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        content_length = 0
    if content_length > MAX_IMAGE_REQUEST_BYTES:
        return JSONResponse({"error": "İstek boyutu 12 MB sınırını aşıyor."}, status_code=413)
    try:
        chunks = []
        bytes_read = 0
        async for chunk in request.stream():
            bytes_read += len(chunk)
            if bytes_read > MAX_IMAGE_REQUEST_BYTES:
                return JSONResponse({"error": "İstek boyutu 12 MB sınırını aşıyor."}, status_code=413)
            chunks.append(chunk)
        raw_body = b"".join(chunks)
        if not raw_body:
            return JSONResponse({"error": "Analiz edilecek ürün görselini yükleyin."}, status_code=422)
        body = json.loads(raw_body.decode("utf-8"))
        if not isinstance(body, dict) or not body.get("image_data_url"):
            return JSONResponse({"error": "Analiz edilecek ürün görselini yükleyin."}, status_code=422)
        image_bytes, image_media_type = decode_image_data_url(body["image_data_url"])
        result = await customs_advisor_service.describe_image(image_bytes, image_media_type)
        _record_usage(quota_user, "vision")
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except (ValidationError, ValueError) as exc:
        message = (
            exc.errors(include_url=False)[0].get("msg", "Görsel evsafları doğrulanamadı.")
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return JSONResponse({"error": message}, status_code=422)
    except Exception:
        logger.exception("Customs image description failed")
        return JSONResponse(
            {"error": "Görsel analiz modeli şu anda yanıt vermedi. Alanları elle doldurup onaylayabilirsiniz."},
            status_code=502,
        )
    return JSONResponse(redact_data(result.model_dump(mode="json"), contact_data=True))


_USER_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
_USER_DOCUMENT_MAX_CHARS = 6_000


def _validate_user_document_url(url: str) -> str:
    """HTTPS-only, allow-list-free guard for user-supplied product document URLs."""
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise SecurityViolation("Belge adresi yalnızca kimlik bilgisi içermeyen HTTPS adresi olabilir.", code="unsafe_url")
    if host in {"169.254.169.254", "metadata.google.internal", "metadata.azure.internal"} or host == "localhost":
        raise SecurityViolation("Sunucu meta veri veya yerel ağ adresine erişim engellendi.", code="ssrf_blocked")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise SecurityViolation("Özel, yerel veya ayrılmış ağ adresine erişim engellendi.", code="ssrf_blocked")
    return url


def _validate_user_document_host_resolution(url: str) -> None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SecurityViolation("Belge adresi çözümlenemedi.", code="unsafe_url") from exc
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not address.is_global:
            raise SecurityViolation("Özel, yerel veya ayrılmış ağ adresine erişim engellendi.", code="ssrf_blocked")


def _browser_fallback_enabled() -> bool:
    return os.environ.get("PRODUCT_PAGE_BROWSER_FALLBACK", "1").strip().lower() not in {"0", "false", "no", ""}


async def _render_user_document_with_browser(url: str) -> str | None:
    """Render a JS-heavy or bot-walled product page in headless Chromium.

    Only the already-validated HTTPS URL and its own (sub)domain may be loaded;
    every other request (trackers, third-party APIs, images, fonts) is aborted.
    Returns ``None`` when Playwright is unavailable or rendering fails, so the
    caller can fall back to a clear user-facing message.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception:  # pragma: no cover - optional runtime dependency
        return None
    target_host = (urlsplit(url).hostname or "").lower().rstrip(".")
    site_root = ".".join(target_host.split(".")[-2:]) if target_host.count(".") >= 1 else target_host

    async def gate(route, request):
        parsed = urlsplit(request.url)
        host = (parsed.hostname or "").lower().rstrip(".")
        same_site = host == target_host or host.endswith(f".{site_root}")
        if parsed.scheme != "https" or not same_site or request.resource_type in {"image", "media", "font", "stylesheet", "websocket"}:
            await route.abort()
            return
        try:
            _validate_user_document_url(request.url)
        except SecurityViolation:
            await route.abort()
            return
        await route.continue_()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(
                    user_agent=PRODUCT_PAGE_BROWSER_HEADERS["User-Agent"],
                    locale="tr-TR",
                    java_script_enabled=True,
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()
                await page.route("**/*", gate)
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                try:
                    await page.wait_for_selector("h1", timeout=6_000)
                except Exception:
                    pass
                return await page.content()
            finally:
                await browser.close()
    except Exception as exc:  # pragma: no cover - depends on the live site
        logger.warning("Headless product page render failed for %s: %s", urlsplit(url).hostname, type(exc).__name__)
        return None


async def _fetch_user_document_text(url: str) -> dict[str, Any]:
    """Fetch a user-supplied page with per-hop URL revalidation; no cross-host redirect trust.

    Returns ``{"text", "title", "structured", "extraction"}``. E-commerce pages are
    read through :func:`product_page.extract_product_page` (JSON-LD / Trendyol state /
    meta) so that the product, not the site navigation, fills the text budget.
    """
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(20),
        headers=PRODUCT_PAGE_BROWSER_HEADERS,
    ) as client:
        current = url
        for _ in range(4):
            _validate_user_document_url(current)
            _validate_user_document_host_resolution(current)
            try:
                response = await client.get(current)
            except httpx.HTTPError as exc:
                # Baglanti hatasi / zaman asimi (bazi siteler sunucu IP'lerini TLS
                # duzeyinde keser): once bassiz tarayiciyla dene, olmazsa acik mesaj ver.
                logger.warning("Product page fetch failed for %s: %s", urlsplit(current).hostname, type(exc).__name__)
                if _browser_fallback_enabled():
                    rendered = await _render_user_document_with_browser(current)
                    if rendered and not detect_bot_wall(200, rendered):
                        return extract_product_page(rendered, current, max_chars=_USER_DOCUMENT_MAX_CHARS)
                raise ValueError(
                    f"Siteye bağlanılamadı ({type(exc).__name__}). Sayfadaki başlığı ve açıklamayı kopyalayıp "
                    "ürün tanımına yapıştırın ya da sayfayı PDF olarak kaydedip yükleyin."
                ) from exc
            if response.is_redirect:
                current = urljoin(current, str(response.headers.get("location", "")))
                continue
            content_type = response.headers.get("content-type", "")
            if response.headers.get("content-length", "") and int(response.headers.get("content-length", "0")) > _USER_DOCUMENT_MAX_BYTES:
                raise ValueError("Belge 10 MB sınırını aşıyor.")
            if response.is_success and "pdf" in content_type.lower():
                payload = response.content[:_USER_DOCUMENT_MAX_BYTES]
                return {"text": _extract_pdf_text(payload), "title": current, "structured": {}, "extraction": "pdf"}
            if response.is_success and "wordprocessingml" in content_type.lower():
                payload = response.content[:_USER_DOCUMENT_MAX_BYTES]
                return {"text": _extract_office_text(payload, ".docx"), "title": current, "structured": {}, "extraction": "docx"}
            html_text = response.text if response.is_success else ""
            blocked = detect_bot_wall(response.status_code, response.text)
            if not blocked and not response.is_success:
                raise ValueError(f"Ürün sayfası okunamadı (HTTP {response.status_code}). Adresi tarayıcıda açıp doğrulayın.")
            extracted = extract_product_page(html_text, current, max_chars=_USER_DOCUMENT_MAX_CHARS) if html_text else None
            needs_browser = blocked or not extracted or (extracted["extraction"] == "text" and len(extracted["text"]) < 200)
            if needs_browser and _browser_fallback_enabled():
                rendered = await _render_user_document_with_browser(current)
                if rendered and not detect_bot_wall(200, rendered):
                    extracted = extract_product_page(rendered, current, max_chars=_USER_DOCUMENT_MAX_CHARS)
                    blocked = None
            if blocked and (not extracted or extracted["extraction"] != "structured"):
                raise ValueError(blocked)
            if not extracted:
                raise ValueError("Ürün sayfasından metin çıkarılamadı.")
            return extracted
    raise ValueError("Belge çok fazla yönlendirme içeriyor.")


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_USER_DOCUMENT_DATA_URL_RE = re.compile(
    r"data:(application/pdf|" + re.escape(_DOCX_MIME) + r");base64,([A-Za-z0-9+/=\r\n]+)"
)


def _extract_office_text(payload: bytes, extension: str) -> str:
    """Extract bounded text from a PDF or Word (.docx) file; markitdown is imported lazily."""
    from markitdown import MarkItDown

    result = MarkItDown().convert_stream(io.BytesIO(payload), file_extension=extension)
    return " ".join(str(result.text_content or "").split())[:_USER_DOCUMENT_MAX_CHARS]


def _extract_pdf_text(payload: bytes) -> str:
    return _extract_office_text(payload, ".pdf")


# Taranmış / teknik çizim PDF'i: sayfa başına bu kadar karakterden az metin varsa
# ilk sayfalar görsele çevrilip görsel evsaf modeline verilir (PRD Faz 3.4).
_PDF_MIN_TEXT_CHARS_PER_PAGE = 200
_PDF_MAX_VISION_PAGES = 3


def _pdf_needs_page_vision(text: str, page_count: int) -> bool:
    return len(" ".join(text.split())) < _PDF_MIN_TEXT_CHARS_PER_PAGE * max(1, page_count)


def _attributes_to_text(data: dict[str, Any]) -> str:
    """Compose the review textarea text from the vision attributes (no tariff code)."""
    lines: list[str] = []
    labels = (
        ("product_name", "Ürün adı"), ("product_category", "Kategori"), ("product_description", "Tanım"),
        ("composition", "Malzeme / bileşim"), ("intended_use", "Kullanım amacı"), ("dimensions", "Ölçü / teknik değer"),
        ("construction_form", "Yapı"), ("function_mechanism", "İşlev"), ("packaging", "Ambalaj"), ("label_text", "Belgede okunan metin"),
    )
    for key, label in labels:
        value = str(data.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    brand_model = " / ".join(filter(None, [str(data.get("visible_brand") or ""), str(data.get("visible_model") or "")]))
    if brand_model:
        lines.append(f"Marka / model: {brand_model}")
    for key, label in (("visible_features", "Görülen özellikler"), ("components_accessories", "Parçalar / aksesuarlar"), ("inferred_features", "Doğrulanması gereken tahminler")):
        items = [str(item).strip() for item in (data.get(key) or []) if str(item).strip()]
        if items:
            lines.append(f"{label}: " + "; ".join(items))
    return "\n".join(lines)


async def _describe_pdf_pages(payload: bytes) -> tuple[dict[str, Any], int]:
    """Rasterise the first pages of a text-less PDF and run the shared vision path."""
    pages = rasterize_pdf_pages(payload, max_pages=_PDF_MAX_VISION_PAGES)
    result = await customs_advisor_service.describe_images([(page, "image/png") for page in pages])
    data = result.model_dump(mode="json")
    # Uygulama ilkesi: görsel/belge analizi hiçbir zaman GTİP üretmez; olası model
    # fazlalıkları şema doğrulamasında düşer, burada da savunma amaçlı temizlenir.
    for key in ("candidate_gtip", "gtip", "hs_code", "hs_codes", "tariff_code"):
        data.pop(key, None)
    return data, len(pages)


@mcp.custom_route("/api/customs/ingest-source", methods=["POST"])
async def web_customs_ingest_source(request: Request):
    """Extract bounded text from a user-supplied product page or PDF for attribute review.

    PDF'lerde metin katmanı yoksa ya da sayfa başına 200 karakterden azsa (taranmış
    katalog, teknik çizim) ilk üç sayfa görsele çevrilir ve ürün fotoğrafıyla aynı
    görsel evsaf yolu (``describe_images``) kullanılır; bu yol ``vision`` kotasına
    tabidir. Sonuç yalnızca evsaf listesidir; GTİP hiçbir zaman forma yazılmaz.
    """
    limited = _rate_limit_response(request, "customs-ingest", limit=10, window_seconds=3600)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
    except SecurityViolation as exc:
        return _security_response(exc)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("İstek bir nesne olmalıdır.")
        url = str(body.get("url", "")).strip()
        pdf_data_url = body.get("pdf_data_url") or body.get("document_data_url")
        if bool(url) == bool(pdf_data_url):
            raise ValueError("Tek bir kaynak belirtin: belge adresi veya PDF/Word dosyası.")
        structured: dict[str, Any] = {}
        extraction = "text"
        source_kind = "text"
        extra: dict[str, Any] = {}
        if url:
            fetched = await _fetch_user_document_text(url)
            text, title = fetched["text"], fetched["title"]
            structured, extraction = fetched.get("structured") or {}, fetched.get("extraction") or "text"
            source_type, source_label, source_kind = "url", url, "url"
        else:
            match = _USER_DOCUMENT_DATA_URL_RE.fullmatch(str(pdf_data_url or ""))
            if not match:
                raise ValueError("Yalnızca PDF veya Word (.docx) dosyası yüklenebilir; eski .doc biçimini Word'de .docx olarak kaydedin.")
            payload = base64.b64decode(match.group(2), validate=True)
            if not payload or len(payload) > _USER_DOCUMENT_MAX_BYTES:
                raise ValueError("Belge 10 MB sınırını aşıyor.")
            if match.group(1) == _DOCX_MIME:
                text, title = _extract_office_text(payload, ".docx"), "Yüklenen Word belgesi"
                source_type, source_label, source_kind = "docx", "Word belgesi", "docx_text"
            else:
                source_type, source_label, source_kind = "pdf", "PDF belgesi", "pdf_text"
                try:
                    text = _extract_pdf_text(payload)
                except Exception as exc:
                    logger.warning("PDF text extraction failed: %s", type(exc).__name__)
                    text = ""
                title = "Yüklenen PDF"
                page_count = pdf_page_count(payload)
                if _pdf_needs_page_vision(text, page_count):
                    quota_user = _enforce_quota(request, "vision")
                    attributes, pages_used = await _describe_pdf_pages(payload)
                    _record_usage(quota_user, "vision")
                    text = _attributes_to_text(attributes)
                    source_kind, extraction = "pdf_pages", "vision"
                    extra = {
                        "pages_used": pages_used,
                        "page_count": page_count,
                        "attributes": attributes,
                        "badge": "Taranmış/çizim PDF'i sayfa görseli olarak analiz edildi",
                    }
        if not text.strip():
            raise ValueError("Belgede kopyalanabilir metin bulunamadı; taranmış sayfa ise metin çıkarılamaz.")
        truncated = len(text) > _USER_DOCUMENT_MAX_CHARS
        warning = (
            "Belge metni yalnızca ürün evsaflarını hazırlamak için çıkarıldı. İçeriği gözden geçirip "
            "onaylamadan sınıflandırma araştırması başlamaz."
        )
        if source_kind == "pdf_pages":
            warning = (
                f"PDF'de metin katmanı bulunmadığı için ilk {extra['pages_used']} sayfa görsel olarak analiz edildi. "
                "Yalnızca sayfada görülebilen evsaflar çıkarıldı; alanları doğrulayıp onaylamadan sınıflandırma başlamaz. "
                "Bu sonuç GTİP değildir."
            )
        return JSONResponse(
            redact_data(
                {
                    "source_type": source_type,
                    "source_kind": source_kind,
                    "title": title[:200] or source_label,
                    "text": text[:_USER_DOCUMENT_MAX_CHARS],
                    "truncated": truncated,
                    "structured": structured,
                    "extraction": extraction,
                    "warning": warning,
                    **extra,
                },
                contact_data=True,
            )
        )
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except ValidationError:
        return JSONResponse({"error": "Görsel evsafları doğrulanamadı; alanları elle doldurabilirsiniz."}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("User document ingestion failed")
        return JSONResponse({"error": "Belge metni şu anda çıkarılamadı."}, status_code=502)


_BRAND_MODEL_MAX_CHARS = 150


def _sanitised_page_fields(page: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Run extracted page text/fields through the untrusted-context and redaction filters."""

    def clean(value: Any, limit: int = 1_500) -> str:
        safe, _ = sanitize_untrusted_context(str(value or "")[:limit], max_chars=limit)
        return redact_text(safe, contact_data=True)

    structured = page.get("structured") if isinstance(page.get("structured"), dict) else {}
    cleaned = {
        key: clean(structured.get(key)) for key in ("name", "brand", "category", "description", "price", "currency", "source")
    }
    cleaned["attributes"] = [
        {"name": clean(item.get("name"), 200), "value": clean(item.get("value"), 500)}
        for item in (structured.get("attributes") or [])[:40]
        if isinstance(item, dict)
    ]
    return clean(page.get("text"), _USER_DOCUMENT_MAX_CHARS), cleaned


@mcp.custom_route("/api/customs/brand-model", methods=["POST"])
async def web_customs_brand_model(request: Request):
    """Verify a typed brand + model against a user-supplied manufacturer / shop page.

    Otomatik web araması yapılmaz: kullanıcı kaynak adresi vermezse yalnız yönlendirme
    döner. Verilen adres ``ingest-source`` ile aynı SSRF korumalarından geçer; sayfa
    içeriği veridir, ``sanitize_untrusted_context`` + ``redact_text`` sonrası döner.
    """
    limited = _rate_limit_response(request, "customs-brand-model", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
        # Girişli kullanım: OAuth yapılandırıldığında oturum zorunludur (self-hosted kurulumda açık kalır).
        _session_user(request, required=google_auth.configured)
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("İstek bir nesne olmalıdır.")
        brand = " ".join(str(body.get("brand") or "").split())[:_BRAND_MODEL_MAX_CHARS]
        model = " ".join(str(body.get("model") or "").split())[:_BRAND_MODEL_MAX_CHARS]
        url = str(body.get("url") or "").strip()[:2000]
        if not brand and not model:
            raise ValueError("Doğrulanacak marka ve/veya model girin.")
        if not url:
            return JSONResponse(
                {
                    "status": "source_required",
                    "brand": brand,
                    "model": model,
                    "message": (
                        "Marka/model doğrulaması için kaynak URL verin. Otomatik web araması yapılmaz; "
                        "üreticinin resmî ürün sayfası veya satıcı ürün sayfası adresini girin."
                    ),
                    "suggestions": [
                        "Üreticinin resmî sitesindeki ürün/teknik föy sayfası (en güvenilir kaynak).",
                        "Ürünün satıldığı e-ticaret sayfası (Trendyol, Hepsiburada, Amazon vb.).",
                        "Sayfa erişime kapalıysa ürün sayfasını PDF olarak kaydedip belge alanından yükleyin.",
                    ],
                }
            )
        page = await _fetch_user_document_text(url)
        match = brand_model_match(brand, model, page)
        text, structured = _sanitised_page_fields(page)
        return JSONResponse(
            redact_data(
                {
                    "status": "checked",
                    "brand": brand,
                    "model": model,
                    "url": url,
                    "title": str(page.get("title") or "")[:200],
                    "extraction": page.get("extraction") or "text",
                    "match": match,
                    "structured": structured,
                    "text": text,
                    "warning": (
                        "Eşleşme puanı yalnızca girilen marka/model metninin sayfada geçip geçmediğini gösterir; "
                        "ürünün doğruluğunu, menşeini veya GTİP'ini teyit etmez."
                    ),
                },
                contact_data=True,
            )
        )
    except SecurityViolation as exc:
        return _security_response(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Brand/model verification failed")
        return JSONResponse({"error": "Kaynak sayfa şu anda okunamadı."}, status_code=502)


async def _read_json_body_limited(request: Request, max_bytes: int) -> dict[str, Any]:
    """Stream a JSON body with a hard byte cap; raises ValueError on size/shape problems."""
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        content_length = 0
    limit_mb = max_bytes // (1024 * 1024)
    if content_length > max_bytes:
        raise ValueError(f"İstek boyutu {limit_mb} MB sınırını aşıyor.")
    chunks: list[bytes] = []
    bytes_read = 0
    async for chunk in request.stream():
        bytes_read += len(chunk)
        if bytes_read > max_bytes:
            raise ValueError(f"İstek boyutu {limit_mb} MB sınırını aşıyor.")
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    if not raw_body:
        raise ValueError("İstek gövdesi boş.")
    body = json.loads(raw_body.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("İstek bir nesne olmalıdır.")
    return body


@mcp.custom_route("/api/customs/ingest-shipping-document", methods=["POST"])
async def web_customs_ingest_shipping_document(request: Request):
    """Read a bill of lading / invoice / packing list into editable fields; never a customs decision."""
    limited = _rate_limit_response(request, "customs-shipping", limit=10, window_seconds=3600)
    if limited:
        return limited
    upload_limited = _rate_limit_response(request, "customs-upload", limit=30, window_seconds=3600)
    if upload_limited:
        return upload_limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
        quota_user = _enforce_quota(request, "vision")
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    try:
        body = await _read_json_body_limited(request, 14 * 1024 * 1024)
        if not body.get("document_data_url"):
            raise ValueError("Konşimento, fatura veya çeki listesi dosyasını yükleyin.")
        payload, media_type = decode_document_data_url(body["document_data_url"])
        result = await extract_shipping_document(payload, media_type)
        _record_usage(quota_user, "vision")
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        message = (
            exc.errors(include_url=False)[0].get("msg", "Belge alanları doğrulanamadı.")
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        return JSONResponse({"error": message}, status_code=422)
    except Exception:
        logger.exception("Shipping document extraction failed")
        return JSONResponse(
            {"error": "Belge okuma modeli şu anda yanıt vermedi. Alanları elle doldurabilirsiniz."},
            status_code=502,
        )
    data = result.model_dump(mode="json")
    data["document_type_label"] = result.document_type_label
    data["payment_method_label"] = result.payment_method_label
    return JSONResponse(redact_data(data, contact_data=True))


@mcp.custom_route("/api/customs/classify-product", methods=["POST"])
async def web_customs_classify_product(request: Request):
    """Suggest editable HS6/CN8 candidates from approved attributes and verify tariff existence."""
    limited = _rate_limit_response(request, "customs-classification", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
        quota_user = _enforce_quota(request, "classification")
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Sınıflandırma isteği bir nesne olmalıdır.")
        guard_data(body, path="ürün evsafı")
        classification = ProductClassificationRequest.model_validate(body)
        result = await customs_advisor_service.classify_product(classification)
        _record_usage(quota_user, "classification")
        return JSONResponse(redact_data(result.model_dump(mode="json"), contact_data=True))
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Ürün evsaflarını kontrol edin.")
        return JSONResponse({"error": f"Evsaflar doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        logger.exception("Product tariff classification failed")
        return JSONResponse(
            {"error": "Aday tarife kodları şu anda üretilemedi. Alanları kontrol edip yeniden deneyin."},
            status_code=502,
        )


@mcp.custom_route("/api/customs/precheck", methods=["POST"])
async def web_customs_precheck(request: Request):
    """Run an evidence-first, non-binding customs pre-assessment."""
    limited = _rate_limit_response(request, "customs-ai", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _api_key_identity(request)  # ERP beyaz listesi
        _agent_or_browser_identity(request)
        quota_user = _enforce_quota(request, "precheck")
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        content_length = 0
    if content_length > 12 * 1024 * 1024:
        return JSONResponse({"error": "İstek boyutu 12 MB sınırını aşıyor."}, status_code=413)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Geçerli bir analiz isteği gönderin."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Analiz isteği bir nesne olmalıdır."}, status_code=422)

    image_data_url = body.pop("image_data_url", None)
    if image_data_url:
        return JSONResponse(
            {
                "error": (
                    "Fotoğraf doğrudan GTİP/TAREKS araştırmasına gönderilemez. Önce /api/customs/describe-image "
                    "ile evsafları çıkarın, kullanıcıya düzelttirip onaylatın; sonra yalnızca onaylanan metin alanlarını gönderin."
                )
            },
            status_code=422,
        )

    try:
        guard_data(body, path="gümrük sorusu")
        inquiry = CustomsInquiry.model_validate(body)
        result = await customs_advisor_service.analyse(inquiry)
        _record_usage(quota_user, "precheck")
    except SecurityViolation as exc:
        return _security_response(exc)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Alanları kontrol edin.")
        return JSONResponse({"error": f"İstek doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Customs precheck failed")
        return JSONResponse(
            {
                "error": (
                    "Gümrük ön değerlendirmesi şu anda tamamlanamadı. Kesin işlem yapmadan önce "
                    "resmî kaynak ve yetkili gümrük müşaviri teyidi alın."
                )
            },
            status_code=502,
        )
    return JSONResponse(redact_data(result.model_dump(mode="json"), contact_data=True))


@mcp.custom_route("/api/email/precheck", methods=["POST"])
async def web_email_precheck(request: Request):
    """Send the signed-in user their own precheck dossier by e-mail."""
    limited = _rate_limit_response(request, "email-precheck", limit=5, window_seconds=3600)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
        user = _required_user(request)
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        if not email_sender.configured:
            raise MailError("E-posta gönderimi henüz yapılandırılmadı.")
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("E-posta isteği bir nesne olmalıdır.")
        guard_data(body, path="e-posta dosyası")
        result = CustomsPrecheckResult.model_validate(body)
        recipient = str(user.get("email") or "").strip()
        if "@" not in recipient:
            raise ValueError("Hesabınızda geçerli bir e-posta adresi bulunamadı.")
        subject = f"İthalat ön değerlendirme dosyası · {result.as_of[:10]}"
        message_id = await email_sender.send(to=recipient, subject=subject, html_body=render_precheck_email(result, PUBLIC_BASE_URL))
        return JSONResponse({"sent": True, "recipient": recipient, "message_id": message_id})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except MailError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Dosya verisi doğrulanamadı.")
        return JSONResponse({"error": f"Dosya verisi doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Precheck e-mail delivery failed")
        return JSONResponse({"error": "E-posta şu anda gönderilemedi; kısa süre sonra yeniden deneyin."}, status_code=502)


REPORT_PDF_MAX_BODY_BYTES = 2 * 1024 * 1024


@mcp.custom_route("/api/customs/assistant", methods=["POST"])
async def web_customs_assistant(request: Request):
    """Tool-calling assistant: the model may only cite deterministic tool outputs (PRD Faz 3.3)."""
    limited = _rate_limit_response(request, "customs-assistant", limit=10, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _agent_or_browser_identity(request)
        _required_user(request)
        quota_user = _enforce_quota(request, "classification")
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Asistan isteği bir nesne olmalıdır.")
        guard_data(body, path="asistan sorusu")
        payload = AssistantRequest.model_validate(body)
        as_of = _as_of_param(request, {"as_of": payload.as_of})
        result = await customs_assistant.ask(
            payload.question,
            gtip=payload.gtip,
            origin_country=payload.origin_country,
            as_of=as_of,
            history=[item.model_dump() for item in payload.history],
        )
        _record_usage(quota_user, "classification")
        return JSONResponse(redact_data(result.model_dump(mode="json"), contact_data=True))
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Soruyu kontrol edin.")
        return JSONResponse({"error": f"İstek doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        logger.exception("Customs assistant failed")
        return JSONResponse(
            {"error": "Asistan yanıtı şu anda üretilemedi. Lütfen biraz sonra yeniden deneyin."},
            status_code=502,
        )


@mcp.custom_route("/api/customs/report.pdf", methods=["POST"])
async def web_customs_report_pdf(request: Request):
    """Server-rendered PDF of a precheck dossier with the mandatory legal footer (pdf_report feature).

    Body: ``{"dossier_id": "..."}`` (saved dossier of the signed user) or
    ``{"result": {...}}`` (a precheck result as returned by the API). No quota is
    consumed; the report is a presentation of an already paid-for analysis.
    """
    limited = _rate_limit_response(request, "customs-report-pdf", limit=10, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        require_feature(request, "pdf_report")
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        declared = int(request.headers.get("content-length", "0") or 0)
        raw = b"" if declared > REPORT_PDF_MAX_BODY_BYTES else await request.body()
        if declared > REPORT_PDF_MAX_BODY_BYTES or len(raw) > REPORT_PDF_MAX_BODY_BYTES:
            raise ValueError("Rapor verisi 2 MB sınırını aşıyor.")
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Rapor isteği bir nesne olmalıdır.")
        dossier_id = str(body.get("dossier_id") or "").strip()
        if dossier_id:
            dossier = account_service.get_dossier(user, dossier_id)
            payload = dossier["payload"]
            report_id = re.sub(r"[^a-z0-9]", "", str(dossier["id"]).lower())[:8] or "dosya"
        else:
            payload = body.get("result")
            if not isinstance(payload, dict):
                raise ValueError("Rapor için analiz sonucu eksik.")
            guard_data(payload, path="PDF raporu")
            report_id = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
        result = CustomsPrecheckResult.model_validate(payload)
        generated_at = datetime.now(UTC).isoformat(timespec="seconds")
        generated_for = str(user.get("name") or user.get("email") or "Kayıtlı kullanıcı").strip()[:120]
        html_report = render_precheck_report_html(
            result, base_url=PUBLIC_BASE_URL, generated_for=generated_for, generated_at=generated_at
        )
        pdf = await report_pdf.render_pdf(html_report, footer_html=report_footer_html(result))
        logger.info(
            "PDF ön değerlendirme raporu üretildi: rapor=%s kaynak=%s bayt=%d renderer=%s",
            report_id, "dossier" if dossier_id else "result", len(pdf), report_pdf.renderer_mode(),
        )
        return Response(
            pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="gumruksor-on-degerlendirme-{report_id}.pdf"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except SecurityViolation as exc:
        return _security_response(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Dosya verisi doğrulanamadı.")
        return JSONResponse({"error": f"Dosya verisi doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc) or "Rapor isteği çözümlenemedi."}, status_code=422)
    except PdfRenderError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        logger.exception("PDF ön değerlendirme raporu üretilemedi")
        return JSONResponse({"error": "PDF raporu şu anda oluşturulamadı; kısa süre sonra yeniden deneyin."}, status_code=503)


@mcp.custom_route("/api/customs/declaration-draft", methods=["POST"])
async def web_customs_declaration_draft(request: Request):
    """Ön değerlendirme sonucundan gümrük beyannamesi taslağı (Tek İdari Belge kutuları).

    Gövde: ``{"dossier_id": "..."}`` (kullanıcının kayıtlı dosyası) veya
    ``{"result": {...}}`` (API'nin döndürdüğü ön değerlendirme sonucu). ``format``
    alanı ``json`` (varsayılan), ``csv`` veya ``xml`` olabilir.

    Kota tüketilmez: taslak, hâlihazırda ödenmiş bir analizin sunumudur. Üretim saf ve
    ağsızdır; hiçbir kutu uydurulmaz, eksik kutu değer taşımaz.
    """
    limited = _rate_limit_response(request, "customs-declaration-draft", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _api_key_identity(request)  # ERP beyaz listesi
        user = _required_user(request)
        require_feature(request, "declaration_draft")
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        declared = int(request.headers.get("content-length", "0") or 0)
        raw = b"" if declared > REPORT_PDF_MAX_BODY_BYTES else await request.body()
        if declared > REPORT_PDF_MAX_BODY_BYTES or len(raw) > REPORT_PDF_MAX_BODY_BYTES:
            raise ValueError("Analiz verisi 2 MB sınırını aşıyor.")
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Beyanname taslağı isteği bir nesne olmalıdır.")
        fmt = str(body.get("format") or "json").strip().lower()
        if fmt not in {"json", "csv", "xml"}:
            raise ValueError("Biçim json, csv veya xml olmalıdır.")
        dossier_id = str(body.get("dossier_id") or "").strip()
        if dossier_id:
            payload = account_service.get_dossier(user, dossier_id)["payload"]
            draft_id = re.sub(r"[^a-z0-9]", "", dossier_id.lower())[:8] or "dosya"
        else:
            payload = body.get("result")
            if not isinstance(payload, dict):
                raise ValueError("Taslak için analiz sonucu eksik.")
            guard_data(payload, path="Beyanname taslağı")
            draft_id = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:8]
        # Gövdeyi modele doğrulatıyoruz ki taslak yalnız gerçek bir ön değerlendirme
        # sonucundan üretilsin; uydurma alanlar buradan geçemez.
        result = CustomsPrecheckResult.model_validate(payload)
        draft = build_declaration_draft(result)
        if fmt == "json":
            return JSONResponse(draft.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
        if fmt == "csv":
            content, media, suffix = draft_to_csv(draft), "text/csv; charset=utf-8", "csv"
        else:
            content, media, suffix = draft_to_xml(draft), "application/xml; charset=utf-8", "xml"
        return Response(
            content.encode("utf-8"),
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="beyanname-taslagi-{draft_id}.{suffix}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except SecurityViolation as exc:
        return _security_response(exc)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Analiz verisi doğrulanamadı.")
        return JSONResponse({"error": f"Analiz verisi doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc) or "Beyanname taslağı isteği çözümlenemedi."}, status_code=422)
    except Exception:
        logger.exception("Beyanname taslağı üretilemedi")
        return JSONResponse(
            {"error": "Beyanname taslağı şu anda oluşturulamadı; kısa süre sonra yeniden deneyin."},
            status_code=503,
        )


@mcp.custom_route("/api/tariff/countries", methods=["GET"])
async def web_tariff_countries(request: Request):
    """Canonical origin/dispatch country list shared by the tariff and origin-document modules."""
    limited = _rate_limit_response(request, "tariff-countries", limit=60, window_seconds=60)
    if limited:
        return limited
    regime_labels = {
        "eu": "AB (Gümrük Birliği)", "efta": "EFTA", "fta": "STA", "pta": "Tercihli Ticaret Anlaşması",
        "kktc": "KKTC", "mfn": "Tercihsiz",
    }
    # İhracat veri düzeyi burada da verilir ki arayüz, kullanıcı ülkeyi yazar yazmaz
    # "bu ülke için oran verimiz yok" diyebilsin — kota harcayan bir istek gerekmeden.
    items = []
    for country in sorted(COUNTRIES, key=lambda item: item.name.casefold()):
        profile = destination_profile(country.name)
        items.append(
            {
                "key": country.key,
                "name": country.name,
                "iso2": country.iso2,
                "regime": country.regime,
                "regime_label": regime_labels.get(country.regime, country.regime),
                "aliases": list(country.aliases),
                "agreement": country.agreement or None,
                "pending_note": PENDING_AGREEMENTS.get(country.key),
                "export_data_tier": profile.tier,
                "export_data_note": profile.badge_text,
            }
        )
    response = JSONResponse({"items": items, "count": len(items)})
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@mcp.custom_route("/api/tariff/status", methods=["GET"])
async def web_tariff_status(request: Request):
    """Return official tariff snapshot freshness without forcing a network refresh."""
    limited = _rate_limit_response(request, "tariff-status", limit=60, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(tariff_engine.status().model_dump(mode="json"))


@mcp.custom_route("/api/classification/status", methods=["GET"])
async def web_classification_status(request: Request):
    limited = _rate_limit_response(request, "classification-status", limit=60, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(classification_engine.status().model_dump(mode="json"))


@mcp.custom_route("/api/classification/evidence", methods=["POST"])
async def web_classification_evidence(request: Request):
    limited = _rate_limit_response(request, "classification-evidence", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Sınıflandırma kanıt isteği bir nesne olmalıdır.")
        result = await classification_engine.search(
            str(body.get("query", ""))[:500],
            code_prefix=str(body.get("code_prefix", "")).strip() or None,
            limit=max(1, min(int(body.get("limit", 5)), 12)),
        )
        return JSONResponse(result.model_dump(mode="json"))
    except (TypeError, ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Classification evidence lookup failed")
        return JSONResponse({"error": "Resmî sınıflandırma kanıtları şu anda sorgulanamadı."}, status_code=502)



def _as_of_param(request: Request, body: dict[str, Any] | None, *, query: bool = False) -> str | None:
    """Validate the optional as-of date; a past date requires the temporal_query feature.

    Today (or no date) is the normal current lookup and stays open to everyone.
    """
    raw = (request.query_params.get("as_of") if query else None) or (body or {}).get("as_of")
    as_of = normalise_as_of(raw)  # ValueError → 422 by the caller
    if as_of and as_of < today_iso():
        require_feature(request, "temporal_query")
    return as_of


@mcp.custom_route("/api/tariff/lookup", methods=["POST"])
async def web_tariff_lookup(request: Request):
    """Look up official customs/IGV rows for a 6/8/10/12 digit tariff code and origin."""
    limited = _rate_limit_response(request, "tariff-lookup", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Tarife isteği bir nesne olmalıdır.")
        as_of = _as_of_param(request, body)
        result = await tariff_engine.lookup(
            str(body.get("gtip", "")),
            origin_country=str(body.get("origin_country", "")).strip()[:100] or None,
            dispatch_country=str(body.get("dispatch_country", "") or "").strip()[:100] or None,
            atr_certificate=_tri_state(body.get("atr_certificate")),
            as_of=as_of,
        )
        return JSONResponse(result.model_dump(mode="json"))
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Tariff lookup failed")
        return JSONResponse({"error": "Resmî tarife tabloları şu anda sorgulanamadı."}, status_code=502)


@mcp.custom_route("/api/tariff/tree", methods=["POST"])
async def web_tariff_tree(request: Request):
    """Return the next deterministic HS6/CN8/TR10/GTIP12 branches."""
    limited = _rate_limit_response(request, "tariff-tree", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Tarife karar ağacı isteği bir nesne olmalıdır.")
        as_of = _as_of_param(request, body)
        result = await tariff_engine.decision_tree(
            str(body.get("gtip", "")),
            origin_country=str(body.get("origin_country", "")).strip() or None,
            as_of=as_of,
        )
        return JSONResponse(result.model_dump(mode="json"))
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Tariff decision tree failed")
        return JSONResponse({"error": "Resmî GTİP karar ağacı şu anda hazırlanamadı."}, status_code=502)


@mcp.custom_route("/api/tariff/cost", methods=["POST"])
async def web_tariff_cost(request: Request):
    """Calculate landed cost from official safe rates plus explicit user inputs."""
    limited = _rate_limit_response(request, "tariff-cost", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Maliyet isteği bir nesne olmalıdır.")
        gtip = str(body.pop("gtip", ""))
        origin = str(body.pop("origin_country", "")).strip()[:100]
        dispatch = str(body.pop("dispatch_country", "") or "").strip()[:100] or None
        atr_certificate = _tri_state(body.pop("atr_certificate", None))
        as_of = _as_of_param(request, {"as_of": body.pop("as_of", None)})
        if not origin:
            raise ValueError("Menşe ülke gereklidir.")
        # PRD Faz 2.3: karar sorusu cevapları girdilere yalnız burada, kullanıcı cevabı
        # olarak yansır. LandedCostInput extra=forbid olduğu için önce ayrılır.
        raw_answers = body.pop("decision_answers", None)
        answers = (
            {str(key)[:60]: str(value)[:80] for key, value in list(raw_answers.items())[:20]}
            if isinstance(raw_answers, dict)
            else {}
        )
        if answers:
            body = apply_decision_answers(answers, body)
            answered_atr = _tri_state(body.pop("atr_certificate", None))
            if atr_certificate is None:
                atr_certificate = answered_atr
        inputs = LandedCostInput.model_validate(body)
        result = await tariff_engine.calculate(
            gtip, origin, inputs, dispatch_country=dispatch, atr_certificate=atr_certificate, as_of=as_of
        )
        if isinstance(result, dict):
            lookup_payload = result.get("tariff") or {}
            vat_payload = lookup_payload.get("vat_rate")
            if vat_payload is None and getattr(tariff_engine, "vat_rates", None) is not None:
                try:
                    vat_payload = tariff_engine.vat_rates.lookup(gtip)
                except Exception:  # noqa: BLE001 – KDV önerisi maliyet yanıtını düşürmemeli
                    logger.exception("VAT suggestion lookup failed for %s", gtip)
                    vat_payload = None
            questions = build_decision_questions(
                gtip=gtip,
                tariff_lookup=lookup_payload,
                vat_lookup=vat_payload,
                inquiry={**inputs.model_dump(), "atr_certificate": atr_certificate},
            )
            result["decision_questions"] = [item.model_dump(mode="json") for item in questions]
        return JSONResponse(result)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Alanları kontrol edin.")
        return JSONResponse({"error": f"İstek doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Tariff cost calculation failed")
        return JSONResponse({"error": "Kaynaklı maliyet hesabı şu anda tamamlanamadı."}, status_code=502)


@mcp.custom_route("/api/tariff/exchange-rate", methods=["GET"])
async def web_tariff_exchange_rate(request: Request):
    """TCMB döviz satış kuru: tescil tarihinde yürürlükte olan bülten (GK md. 30)."""
    limited = _rate_limit_response(request, "tariff-fx", limit=60, window_seconds=60)
    if limited:
        return limited
    currency = str(request.query_params.get("currency", "USD"))[:5]
    try:
        registration = parse_registration_date(request.query_params.get("date"))
    except ExchangeRateError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    try:
        result = await exchange_rate_service.customs_quote(currency, registration)
    except ExchangeRateError as exc:
        message = str(exc)
        status = 422 if message.startswith("Geçersiz") or "bültende yer almıyor" in message else 502
        return JSONResponse({"error": message}, status_code=status)
    except Exception:
        logger.exception("Exchange rate lookup failed")
        return JSONResponse({"error": "TCMB kuru şu anda alınamadı."}, status_code=502)
    return JSONResponse(result, headers={"Cache-Control": "public, max-age=900"})


@mcp.custom_route("/api/tariff/measures", methods=["POST"])
async def web_trade_measures(request: Request):
    """Damping/sübvansiyon, korunma ve gözetim kapsamı (resmî listeler, günlük eşitlenir)."""
    limited = _rate_limit_response(request, "trade-measures", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("İstek bir nesne olmalıdır.")
        as_of = _as_of_param(request, body)
        report = trade_measure_engine.lookup(
            str(body.get("gtip", "")), (body.get("origin_country") or None), today=parse_iso_date(as_of) if as_of else None
        )
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    payload = report.as_dict()
    payload["summary"] = trade_measure_summary(report)
    return JSONResponse(payload)


@mcp.custom_route("/api/tariff/measures/status", methods=["GET"])
async def web_trade_measures_status(request: Request):
    limited = _rate_limit_response(request, "trade-measures-status", limit=30, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(trade_measure_engine.status())


@mcp.custom_route("/api/foreign/tariff", methods=["GET"])
async def web_foreign_tariff(request: Request):
    """Yurt dışı tarife karşılaştırma: BK/ABD açık API'lerinden oran, AB/İsviçre için resmî sorgu bağlantısı."""
    limited = _rate_limit_response(request, "foreign-tariff", limit=30, window_seconds=60)
    if limited:
        return limited
    try:
        require_feature(request, "foreign_tariff")
        as_of = _as_of_param(request, {}, query=True)
        result = await foreign_tariff_engine.lookup(
            str(request.query_params.get("gtip", "")),
            origin=(request.query_params.get("origin") or None),
            jurisdiction=str(request.query_params.get("jurisdiction", "all")),
            as_of=as_of,
        )
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except SecurityViolation as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Foreign tariff lookup failed")
        return JSONResponse({"error": "Yurt dışı tarife verisi şu anda alınamadı."}, status_code=502)
    return JSONResponse(result.as_dict(), headers={"Cache-Control": "private, max-age=300"})


@mcp.custom_route("/api/foreign/ebti", methods=["GET"])
async def web_ebti_decisions(request: Request):
    """AB Bağlayıcı Tarife Bilgisi kararlarında GTİP veya kelime araması."""
    limited = _rate_limit_response(request, "ebti-search", limit=30, window_seconds=60)
    if limited:
        return limited
    try:
        require_feature(request, "foreign_tariff")
        result = ebti_engine.search(
            str(request.query_params.get("q", "")),
            code_prefix=(request.query_params.get("gtip") or None),
            limit=int(request.query_params.get("limit") or 8),
        )
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("EBTI search failed")
        return JSONResponse({"error": "AB karar verisi şu anda sorgulanamadı."}, status_code=502)
    return JSONResponse(result.as_dict(), headers={"Cache-Control": "private, max-age=300"})


@mcp.custom_route("/api/foreign/eu-taric", methods=["GET"])
async def web_eu_taric(request: Request):
    """AB TARIC önlemleri: üçüncü ülke vergisi, menşeye özgü oran, ek vergiler ve gereken belgeler."""
    limited = _rate_limit_response(request, "eu-taric", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        require_feature(request, "foreign_tariff")
        result = await eu_taric_engine.lookup(
            str(request.query_params.get("gtip", "")),
            origin=str(request.query_params.get("origin") or "TR"),
        )
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("EU TARIC lookup failed")
        return JSONResponse({"error": "AB TARIC verisi şu anda alınamadı."}, status_code=502)
    return JSONResponse(result.as_dict(), headers={"Cache-Control": "private, max-age=600"})


@mcp.custom_route("/api/foreign/eu-taric/status", methods=["GET"])
async def web_eu_taric_status(request: Request):
    limited = _rate_limit_response(request, "eu-taric-status", limit=30, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(eu_taric_engine.status())


@mcp.custom_route("/api/foreign/ebti/status", methods=["GET"])
async def web_ebti_status(request: Request):
    limited = _rate_limit_response(request, "ebti-status", limit=30, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(ebti_engine.status())


@mcp.custom_route("/api/foreign/tariff/status", methods=["GET"])
async def web_foreign_tariff_status(request: Request):
    limited = _rate_limit_response(request, "foreign-tariff-status", limit=30, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(foreign_tariff_engine.status())


@mcp.custom_route("/api/tariff/excise", methods=["POST"])
async def web_excise_tax(request: Request):
    """4760 sayılı ÖTV Kanunu ekli listelerinde GTİP kapsamı."""
    limited = _rate_limit_response(request, "excise-tax", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("İstek bir nesne olmalıdır.")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    report = excise_tax_index.lookup(str(body.get("gtip", "")))
    report["summary"] = excise_tax_summary(report)
    return JSONResponse(report)


@mcp.custom_route("/api/foreign/vat", methods=["GET"])
async def web_foreign_vat(request: Request):
    """Hedef ülkenin KDV oranı (AB-27). Ağ çağrısı yok; tohum/önbellekten okunur."""
    limited = _rate_limit_response(request, "foreign-vat", limit=60, window_seconds=60)
    if limited:
        return limited
    iso2 = str(request.query_params.get("iso2") or "").strip()
    if len(iso2) != 2 or not iso2.isalpha():
        return JSONResponse({"error": "İki harfli ülke kodu gerekir (?iso2=DE)."}, status_code=422)
    gtip = re.sub(r"\D", "", str(request.query_params.get("gtip") or ""))
    if gtip and not 4 <= len(gtip) <= 12:
        return JSONResponse({"error": "GTİP 4-12 haneli olmalıdır."}, status_code=422)
    report = eu_vat_index.lookup(iso2, gtip=gtip or None)
    report["summary"] = eu_vat_index.summary_lines(report)
    report["status"] = eu_vat_index.status()
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/tariff/vat", methods=["GET"])
async def web_vat_rate(request: Request):
    """2007/13033 sayılı Karar eki (I)/(II) sayılı listelerden KDV oranı önerisi (onay gerekir)."""
    limited = _rate_limit_response(request, "vat-rate", limit=60, window_seconds=60)
    if limited:
        return limited
    gtip = re.sub(r"\D", "", str(request.query_params.get("gtip") or ""))
    if not 2 <= len(gtip) <= 12:
        return JSONResponse({"error": "GTİP 2-12 haneli olmalıdır (?gtip=...)."}, status_code=422)
    report = vat_rate_index.lookup(gtip)
    report["summary"] = vat_rate_summary(report)
    report["status"] = vat_rate_index.status()
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/tariff/gts", methods=["GET"])
async def web_gts_coverage(request: Request):
    """Resmî GTS ülke listesi ve her satırın ülke kayıt defterinde çözülüp çözülmediği.

    Çözülemeyen satır sessiz bir fazla-vergi riskidir (gerekçesi motorda yazılı),
    bu yüzden liste dışarıya açıkça verilir.
    """
    limited = _rate_limit_response(request, "gts-coverage", limit=30, window_seconds=60)
    if limited:
        return limited
    try:
        report = tariff_engine.gts_coverage()
    except Exception:
        logger.exception("GTS coverage failed")
        return JSONResponse({"error": "GTS ülke listesi şu anda okunamadı."}, status_code=502)
    if str(request.query_params.get("unresolved") or "").strip() in {"1", "true", "yes"}:
        report["countries"] = [item for item in report["countries"] if not item["resolved"]]
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/tariff/communiques", methods=["GET"])
async def web_import_communiques(request: Request):
    """Ticaret Bakanlığı İthalat Tebliğleri dizini (resmî bağlantılarla)."""
    limited = _rate_limit_response(request, "communiques", limit=60, window_seconds=60)
    if limited:
        return limited
    year_text = request.query_params.get("year", "")
    year = int(year_text) if year_text.isdigit() else None
    entries = trade_measure_engine.communiques(year)
    query = (request.query_params.get("q") or "").strip().casefold()
    if query:
        entries = [entry for entry in entries if query in entry.get("title", "").casefold()]
    return JSONResponse({"count": len(entries), "entries": entries[:300], "dataset": trade_measure_engine.store.metadata("communiques")})


@mcp.custom_route("/api/tariff/autocomplete", methods=["GET"])
async def web_tariff_autocomplete(request: Request):
    """Search-as-you-type GTİP ve ürün fihristi önerileri."""
    limited = _rate_limit_response(request, "tariff-autocomplete", limit=120, window_seconds=60)
    if limited:
        return limited
    query = str(request.query_params.get("q", "")).strip()[:100]
    limit_val = max(1, min(int(request.query_params.get("limit", "12") or 12), 30))
    # Geçmiş tarihli arama `temporal_query` yeteneğine bağlıdır; bugünkü arama herkese açık.
    try:
        as_of = _as_of_param(request, None, query=True)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc) or "Tarih çözümlenemedi."}, status_code=422)
    hybrid = await _hybrid_hits(query, limit=limit_val, as_of=as_of)
    items = unified_search.autocomplete(query, limit=limit_val, hybrid=hybrid, as_of=as_of)
    return JSONResponse(
        {
            "items": items,
            "results": items,
            "count": len(items),
            "total": len(items),
            "mode": str((hybrid or {}).get("mode") or "lexical"),
        }
    )


@mcp.custom_route("/api/controls/communiques", methods=["GET"])
async def web_controls_communiques(request: Request):
    """Güncel Ürün Güvenliği ve Denetimi (ÜGD 2026/1 - 2026/32) tebliğ fihristi."""
    limited = _rate_limit_response(request, "controls-communiques", limit=60, window_seconds=60)
    if limited:
        return limited
    catalog = control_engine.get_communiques_catalog()
    return JSONResponse({"items": catalog, "communiques": catalog, "count": len(catalog), "total": len(catalog)})


@mcp.custom_route("/api/controls/search", methods=["GET", "POST"])
async def web_controls_search(request: Request):
    """Ürün adı, kelime veya GTİP parçasıyla denetim tebliğleri ve Ek-1 kapsamlarında arama."""
    limited = _rate_limit_response(request, "controls-search", limit=60, window_seconds=60)
    if limited:
        return limited
    if request.method == "POST":
        try:
            body = await request.json()
            query = str(body.get("query", "") if isinstance(body, dict) else "").strip()
        except Exception:
            query = ""
    else:
        query = str(request.query_params.get("q", "")).strip()
    items = control_engine.search_controls(query, limit=60)
    return JSONResponse({"query": query, "items": items, "results": items, "count": len(items), "total": len(items)})


async def _hybrid_hits(
    query: str, *, limit: int = 10, gtip: str | None = None, as_of: str | None = None
) -> dict[str, Any] | None:
    """Kalıcı hibrit indeksten (BM25 + embedding) sonuç alır; hata/boş sorguda None."""
    text = str(query or "").strip()
    if not text:
        return None
    try:
        return await hybrid_index.search(text, limit=limit, gtip_prefix=gtip, as_of=as_of)
    except SecurityViolation:
        raise
    except Exception:  # noqa: BLE001 - hibrit indeks mevcut aramayı hiçbir zaman engellemez
        logger.exception("Hibrit indeks araması başarısız")
        return None


@mcp.custom_route("/api/search/hybrid", methods=["GET"])
async def web_hybrid_search(request: Request):
    """Kalıcı hibrit indeks (BM25 + embedding, RRF) üzerinde arama (PRD Faz 3.1)."""
    limited = _rate_limit_response(request, "hybrid-search", limit=60, window_seconds=60)
    if limited:
        return limited
    query = str(request.query_params.get("q", "")).strip()[:200]
    gtip = re.sub(r"\D", "", str(request.query_params.get("gtip", "")))[:12]
    try:
        limit_val = max(1, min(int(request.query_params.get("limit", "10") or 10), 30))
    except ValueError:
        limit_val = 10
    if not query:
        return JSONResponse({"query": "", "mode": "lexical", "items": [], "count": 0})
    try:
        as_of = _as_of_param(request, None, query=True)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc) or "Tarih çözümlenemedi."}, status_code=422)
    try:
        result = await hybrid_index.search(query, limit=limit_val, gtip_prefix=gtip or None, as_of=as_of)
    except SecurityViolation as exc:
        return _security_response(exc)
    except Exception:
        logger.exception("Hibrit arama başarısız")
        return JSONResponse({"error": "Hibrit arama şu anda kullanılamıyor."}, status_code=503)
    return JSONResponse(result)


@mcp.custom_route("/api/admin/index-status", methods=["GET"])
async def web_admin_index_status(request: Request):
    """Hibrit indeks durumu (belge/embedding sayıları, son yenileme); editör veya yönetici."""
    limited = _rate_limit_response(request, "admin-index-status", limit=30, window_seconds=60)
    if limited:
        return limited
    try:
        _require_role(request, "editor")
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    try:
        status = hybrid_index.status()
    except Exception:
        logger.exception("Hibrit indeks durumu alınamadı")
        return JSONResponse({"error": "İndeks durumu okunamadı."}, status_code=500)
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/admin/eu-taric/fill", methods=["GET", "POST"])
async def web_admin_eu_taric_fill(request: Request):
    """AB TARIC toplu dolumu: GET plan ve harcama durumu, POST bir turluk dolum (yönetici)."""
    limited = _rate_limit_response(request, "admin-eu-taric-fill", limit=10, window_seconds=60)
    if limited:
        return limited
    try:
        _require_admin(request)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    if request.method == "GET":
        try:
            plan = await asyncio.to_thread(eu_taric_engine.fill_plan)
        except Exception:
            logger.exception("AB TARIC dolum planı okunamadı")
            return JSONResponse({"error": "Dolum planı okunamadı."}, status_code=500)
        return JSONResponse(plan, headers={"Cache-Control": "no-store"})
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    limit = payload.get("limit") if isinstance(payload, dict) else None
    try:
        limit_value = max(1, min(int(limit), 500)) if limit is not None else None
    except (TypeError, ValueError):
        limit_value = None
    try:
        report = await eu_taric_engine.fill_once(limit=limit_value)
    except Exception:
        logger.exception("AB TARIC toplu dolumu başarısız")
        return JSONResponse({"error": "Toplu dolum çalıştırılamadı."}, status_code=502)
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/admin/storage", methods=["GET", "POST"])
async def web_admin_storage(request: Request):
    """Veri diski ve yedekler: GET rapor, POST hemen yedek al (yönetici)."""
    limited = _rate_limit_response(request, "admin-storage", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        _require_admin(request)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    if request.method == "POST":
        try:
            await storage_service.backup_now()
        except Exception:
            logger.exception("Yedek alınamadı")
            return JSONResponse({"error": "Yedek alınamadı."}, status_code=500)
    try:
        report = await asyncio.to_thread(storage_service.report)
    except Exception:
        logger.exception("Depolama raporu okunamadı")
        return JSONResponse({"error": "Depolama durumu okunamadı."}, status_code=500)
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/admin/storage/backup/{name}", methods=["GET"])
async def web_admin_storage_backup_download(request: Request):
    """Bir yedeği indirir: sunucu dışına kopya almanın ücretsiz yolu (yönetici).

    Yedek dosyası kullanıcı hesaplarını ve kanıt dosyalarını içerir; bu yüzden
    yalnız yönetici erişir ve dosya adı ``resolve_backup_file`` ile doğrulanır
    (yedek dizininin dışına çıkan hiçbir ad kabul edilmez).
    """
    limited = _rate_limit_response(request, "admin-storage-download", limit=10, window_seconds=60)
    if limited:
        return limited
    try:
        _require_admin(request)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    path = resolve_backup_file(None, request.path_params.get("name", ""))
    if path is None:
        return JSONResponse({"error": "Yedek bulunamadı."}, status_code=404)
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@mcp.custom_route("/api/search/unified", methods=["GET", "POST"])
async def web_unified_search(request: Request):
    """Tarife, TAREKS/TSE denetimleri, ÖTV ve resmi mevzuat üzerinde birleşik arama."""
    limited = _rate_limit_response(request, "unified-search", limit=60, window_seconds=60)
    if limited:
        return limited
    payload: dict[str, Any] | None = None
    if request.method == "POST":
        try:
            body = await request.json()
            payload = body if isinstance(body, dict) else None
            query = str((payload or {}).get("query", "")).strip()
            category = str((payload or {}).get("category", "all")).strip()
        except Exception:
            query, category = "", "all"
    else:
        query = str(request.query_params.get("q", "")).strip()
        category = str(request.query_params.get("category", "all")).strip()
    try:
        as_of = _as_of_param(request, payload, query=request.method == "GET")
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except ValueError as exc:
        return JSONResponse({"error": str(exc) or "Tarih çözümlenemedi."}, status_code=422)
    hybrid = await _hybrid_hits(query, limit=10, as_of=as_of)
    result = unified_search.search_all(query, category=category, limit=30, hybrid=hybrid, as_of=as_of)
    return JSONResponse(result)


@mcp.custom_route("/api/customs/declaration", methods=["POST"])
async def web_customs_declaration(request: Request):
    """Eylemio gümrük konektörüyle beyanname durumu (salt okunur; oturum gerekir)."""
    limited = _rate_limit_response(request, "customs-declaration", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        user = _required_user(request)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("İstek bir nesne olmalıdır.")
        result = await eylemio_client.declaration_status(str(body.get("declaration_no", "")))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except EylemioError as exc:
        status = 503 if "yapılandırılmamış" in str(exc) or "ulaşılamadı" in str(exc) else 422
        return JSONResponse({"error": str(exc)}, status_code=status)
    except Exception:
        logger.exception("Eylemio declaration lookup failed")
        return JSONResponse({"error": "Beyanname sorgusu şu anda tamamlanamadı."}, status_code=502)
    # Beyanname sorgusu kullanıcının kendi BİLGE hesabından okunur; kota işlemi değildir,
    # yalnız uç nokta bazlı hız sınırına tabidir.
    result["summary"] = [{"label": label, "value": value} for label, value in summarise_declaration(result)]
    return JSONResponse(result)


@mcp.custom_route("/api/customs/declaration/status", methods=["GET"])
async def web_customs_declaration_status(request: Request):
    limited = _rate_limit_response(request, "customs-declaration-status", limit=30, window_seconds=60)
    if limited:
        return limited
    return JSONResponse({"configured": eylemio_client.configured, "base_url": eylemio_client.base_url})


@mcp.custom_route("/api/tariff/bulk/template", methods=["GET"])
async def web_tariff_bulk_template(request: Request):
    return Response(
        bulk_template_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="toplu-hesap-sablonu.csv"', "Cache-Control": "public, max-age=3600"},
    )


@mcp.custom_route("/api/tariff/bulk", methods=["POST"])
async def web_tariff_bulk(request: Request):
    """Calculate many declaration lines from an uploaded CSV/XLSX or JSON rows."""
    limited = _rate_limit_response(request, "tariff-bulk", limit=10, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        _api_key_identity(request)  # ERP beyaz listesi
        require_feature(request, "bulk_costing")
    except SecurityViolation as exc:
        return _security_response(exc)
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except QuotaExceeded as exc:
        return _quota_error(exc)
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
        if content_length > BULK_MAX_FILE_BYTES * 2:
            raise ValueError("Dosya 2 MB sınırını aşıyor.")
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Toplu hesap isteği bir nesne olmalıdır.")
        if body.get("file_data_url"):
            match = re.fullmatch(r"data:[\w./+-]*;base64,([A-Za-z0-9+/=\r\n]+)", str(body["file_data_url"]))
            if not match:
                raise ValueError("Dosya base64 veri adresi olarak gönderilmelidir.")
            payload = base64.b64decode(match.group(1), validate=True)
            rows = bulk_rows_from_upload(payload, str(body.get("file_name", "")))
        else:
            rows = body.get("rows")
            if not isinstance(rows, list) or not rows or not all(isinstance(item, dict) for item in rows):
                raise ValueError("En az bir satır gönderin (rows) veya bir CSV/XLSX dosyası yükleyin.")
        result = await bulk_calculate_rows(tariff_engine, rows)
        return JSONResponse(result)
    except (ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Bulk tariff calculation failed")
        return JSONResponse({"error": "Toplu hesap şu anda tamamlanamadı."}, status_code=502)


def _parse_scenario_body(body: Any) -> tuple[str, list[str], str | None, bool | None]:
    """Shared validation for the origin-scenario and savings routes."""
    if not isinstance(body, dict):
        raise ValueError("Senaryo isteği bir nesne olmalıdır.")
    gtip = str(body.get("gtip", "")).strip()
    origins_raw = body.get("origins", [])
    if not isinstance(origins_raw, list):
        raise ValueError("Menşe listesi geçersiz.")
    origins = list(dict.fromkeys(str(item).strip()[:100] for item in origins_raw if str(item).strip()))[:6]
    dispatch = str(body.get("dispatch_country", "") or "").strip()[:100] or None
    atr_certificate = _tri_state(body.get("atr_certificate"))
    if not gtip or len(origins) < 2:
        raise ValueError("Karşılaştırma için tarife kodu ve en az iki farklı menşe ülke gereklidir.")
    return gtip, origins, dispatch, atr_certificate


@mcp.custom_route("/api/tariff/scenarios", methods=["POST"])
async def web_tariff_scenarios(request: Request):
    """Compare deterministic tariff burden and origin documents across origin countries."""
    limited = _rate_limit_response(request, "tariff-scenarios", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        require_feature(request, "scenario_compare")
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        body = await request.json()
        gtip, origins, dispatch, atr_certificate = _parse_scenario_body(body)
        as_of = _as_of_param(request, body)
        rows = await build_origin_scenarios(
            tariff_engine, gtip, origins, dispatch_country=dispatch, atr_certificate=atr_certificate, as_of=as_of
        )
        return JSONResponse({"gtip": gtip, "dispatch_country": dispatch, "as_of": as_of, "rows": rows, "generated_at": time.time()})
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Tariff scenario comparison failed")
        return JSONResponse({"error": "Menşe senaryoları şu anda karşılaştırılamadı."}, status_code=502)


@mcp.custom_route("/api/tariff/savings", methods=["POST"])
async def web_tariff_savings(request: Request):
    """Rank origin scenarios by landed cost with the user's cost inputs (decision support, not advice)."""
    limited = _rate_limit_response(request, "tariff-savings", limit=20, window_seconds=60)
    if limited:
        return limited
    try:
        require_feature(request, "scenario_compare")
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    try:
        body = await request.json()
        gtip, origins, dispatch, atr_certificate = _parse_scenario_body(body)
        baseline_origin = str(body.get("baseline_origin", "") or "").strip()[:100] or None
        cost_body = body.get("cost")
        if not isinstance(cost_body, dict):
            raise ValueError("Tasarruf önerisi için maliyet girdileri (cost) gereklidir; en az fatura bedelini girin.")
        cost_input = LandedCostInput.model_validate(cost_body)
        rows = await build_origin_scenarios(
            tariff_engine, gtip, origins, dispatch_country=dispatch, atr_certificate=atr_certificate
        )
        atr_rows = None
        atr_origins = [row["origin_country"] for row in rows if row.get("atr_available") and not row.get("atr_free_circulation")]
        if atr_origins and atr_certificate is not True:
            atr_rows = await build_origin_scenarios(
                tariff_engine, gtip, atr_origins, dispatch_country=dispatch, atr_certificate=True
            )
        outcomes = evaluate_scenarios(rows, cost_input, atr_rows=atr_rows, atr_certificate=atr_certificate)
        ranking = rank_savings(outcomes, baseline_origin)
        baseline_note = None
        if baseline_origin and not any(origin.casefold() == baseline_origin.casefold() for origin in origins):
            baseline_note = "Temel senaryo menşe listesinde bulunmadığı için en yüksek maliyetli senaryo temel alındı."
        return JSONResponse(
            {
                "gtip": gtip,
                "dispatch_country": dispatch,
                "atr_certificate": atr_certificate,
                "baseline_origin": baseline_origin,
                "baseline_note": baseline_note,
                "currency": cost_input.currency,
                "rows_evaluated": len(outcomes),
                **ranking,
                "generated_at": time.time(),
            }
        )
    except ValidationError as exc:
        message = exc.errors(include_url=False)[0].get("msg", "Alanları kontrol edin.")
        return JSONResponse({"error": f"Maliyet girdileri doğrulanamadı: {message}"}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Tariff savings ranking failed")
        return JSONResponse({"error": "Tasarruf önerisi şu anda hesaplanamadı."}, status_code=502)


@mcp.custom_route("/api/controls/status", methods=["GET"])
async def web_control_status(request: Request):
    limited = _rate_limit_response(request, "control-status", limit=60, window_seconds=60)
    if limited:
        return limited
    return JSONResponse(control_engine.status().model_dump(mode="json"))


@mcp.custom_route("/api/controls/lookup", methods=["POST"])
async def web_control_lookup(request: Request):
    """Return official annex matches and explicitly preserve risk uncertainty."""
    limited = _rate_limit_response(request, "control-lookup", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Kontrol isteği bir nesne olmalıdır.")
        as_of = _as_of_param(request, body)
        # Yön: ihracatta yalnız ihracat listeleri taranır. İhracat indeksi kısmidir ve
        # sonuç bunu her zaman uyarı olarak taşır.
        direction = "export" if str(body.get("direction") or "import").strip().lower() == "export" else "import"
        result = await control_engine.lookup(str(body.get("gtip", "")), as_of=as_of, direction=direction)
        return JSONResponse(result.model_dump(mode="json"))
    except FeatureNotAvailable as exc:
        return _feature_error(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (ValueError, ValidationError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception:
        logger.exception("Import control lookup failed")
        return JSONResponse({"error": "Resmî ithalat kontrol tebliğleri şu anda sorgulanamadı."}, status_code=502)


_MEASURE_LABELS_TR = {
    "customs_duty": "Gümrük vergisi", "additional_duty": "İGV", "additional_financial_liability": "Ek mali yükümlülük",
    "customs_duty_suspension": "Askıya alma", "customs_duty_end_use": "Nihai kullanım",
}
_CHANGE_SOURCE_TITLES = {"import_regime": "İthalat Rejimi Kararı", "additional_duty": "İlave Gümrük Vergisi Kararı"}


def _trade_measure_changes_for(digits: str) -> list[dict[str, Any]]:
    """Damping/korunma/gözetim listelerindeki farklar; izlenen kodla ön ek eşleşmesi."""
    found: list[dict[str, Any]] = []
    section_labels = {"added": "yeni satır", "removed": "kaldırıldı", "modified": "değişti"}
    for change in trade_measure_engine.store.changes(limit=60):
        detail = change.get("detail") or {}
        for section, label in section_labels.items():
            for entry in detail.get(section, []):
                codes = entry.get("codes") or []
                if not any(code.startswith(digits) or digits.startswith(code) for code in codes):
                    continue
                found.append(
                    {
                        "gtip": next((code for code in codes if code.startswith(digits) or digits.startswith(code)), digits),
                        "measure_type": change.get("kind"),
                        "measure_label": TRADE_MEASURE_LABELS.get(change.get("kind"), change.get("kind")),
                        "country_group": label,
                        "before": entry.get("before") if section == "modified" else (entry.get("summary") if section == "removed" else None),
                        "after": entry.get("after") if section == "modified" else (entry.get("summary") if section == "added" else None),
                        "source_id": f"trade_measures:{change.get('kind')}",
                        "source_title": detail.get("label") or "Resmî önlem listesi",
                        "new_snapshot": str(change.get("changed_at", ""))[:10],
                        "old_snapshot": None,
                    }
                )
    return found


def _watch_changes_for(gtip: str, ledgers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Official tariff line changes whose 12-digit code starts with the watched code."""
    digits = re.sub(r"\D", "", gtip)
    found: list[dict[str, Any]] = _trade_measure_changes_for(digits) if digits else []
    for source_id, ledger in ledgers.items():
        if ledger.get("status") != "compared":
            continue
        for change in ledger.get("changes", []):
            if str(change.get("gtip", "")).startswith(digits):
                found.append(
                    {
                        **change,
                        "source_id": source_id,
                        "source_title": _CHANGE_SOURCE_TITLES.get(source_id, source_id),
                        "measure_label": _MEASURE_LABELS_TR.get(change.get("measure_type"), change.get("measure_type")),
                        "new_snapshot": ledger.get("new_snapshot"),
                        "old_snapshot": ledger.get("old_snapshot"),
                    }
                )
    return found


def _change_ledgers() -> dict[str, dict[str, Any]]:
    return {
        "import_regime": tariff_engine.changes("import_regime", limit=1000),
        "additional_duty": tariff_engine.changes("additional_duty", limit=1000),
    }


@mcp.custom_route("/api/watchlist", methods=["GET"])
async def web_watchlist(request: Request):
    try:
        user = _required_user(request)
        ledgers = _change_ledgers()
        items = []
        for item in account_service.list_watchlist(user):
            changes = _watch_changes_for(item["gtip"], ledgers)
            items.append({**item, "changes": changes[:50], "change_count": len(changes)})
        return JSONResponse(
            {
                "items": items,
                "email_enabled": email_sender.configured,
                "ledger": {key: {"status": value.get("status"), "new_snapshot": value.get("new_snapshot")} for key, value in ledgers.items()},
            },
            headers={"Cache-Control": "no-store"},
        )
    except AuthError as exc:
        return _auth_error(exc)


@mcp.custom_route("/api/watchlist", methods=["POST"])
async def web_add_watch(request: Request):
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("İzleme isteği bir nesne olmalıdır.")
        items_in = body.get("items")
        if isinstance(items_in, list):
            created = [
                account_service.add_watch(
                    user, gtip=str(item.get("gtip", "")), label=str(item.get("label", "")),
                    origin_country=str(item.get("origin_country", "") or "") or None,
                )
                for item in items_in[:100] if isinstance(item, dict)
            ]
            return JSONResponse({"items": created}, status_code=201, headers={"Cache-Control": "no-store"})
        created = account_service.add_watch(
            user, gtip=str(body.get("gtip", "")), label=str(body.get("label", "")),
            origin_country=str(body.get("origin_country", "") or "") or None,
        )
        return JSONResponse(created, status_code=201, headers={"Cache-Control": "no-store"})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)
    except (AccountError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@mcp.custom_route("/api/watchlist/{watch_id}", methods=["DELETE"])
async def web_remove_watch(request: Request):
    try:
        _trusted_request_origin(request)
        user = _required_user(request)
        removed = account_service.remove_watch(user, request.path_params.get("watch_id", ""))
        return JSONResponse({"removed": removed}, status_code=200 if removed else 404)
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc)


async def notify_watchlist_changes() -> dict[str, int]:
    """E-mail each user once per new official snapshot that changes a watched code."""
    stats = {"users": 0, "sent": 0, "skipped": 0}
    ledgers = _change_ledgers()
    if not any(ledger.get("status") == "compared" for ledger in ledgers.values()):
        return stats
    by_user: dict[str, dict[str, Any]] = {}
    for watch in account_service.all_watches():
        changes = _watch_changes_for(watch["gtip"], ledgers)
        if not changes:
            continue
        pending = []
        for change in changes:
            key = f"{change['source_id']}:{change['new_snapshot']}:{watch['gtip']}"
            if account_service.notification_sent(watch["google_sub"], "watch", key):
                continue
            pending.append((key, change))
        if not pending:
            continue
        entry = by_user.setdefault(watch["google_sub"], {"email": watch["email"], "items": [], "keys": set()})
        entry["items"].append(
            {
                "gtip": watch["gtip"], "label": watch["label"], "changes": [change for _, change in pending],
                "source_title": ", ".join(sorted({change["source_title"] for _, change in pending})),
                "new_snapshot": ", ".join(sorted({str(change["new_snapshot"]) for _, change in pending})),
            }
        )
        entry["keys"].update(key for key, _ in pending)
    stats["users"] = len(by_user)
    for google_sub, entry in by_user.items():
        if not email_sender.configured or not entry["email"]:
            stats["skipped"] += 1
            continue
        try:
            await email_sender.send(
                to=entry["email"],
                subject="İzlediğiniz GTİP satırlarında resmî tarife değişikliği",
                html_body=render_watch_email(entry["items"], PUBLIC_BASE_URL),
            )
        except MailError as exc:
            logger.warning("Watch-list notification failed for %s: %s", google_sub, exc)
            stats["skipped"] += 1
            continue
        for key in entry["keys"]:
            account_service.mark_notified(google_sub, "watch", key)
        stats["sent"] += 1
    return stats


async def notify_compliance_alerts(*, today: date | None = None) -> dict[str, int]:
    """E-mail a digest of high-severity compliance alerts (change_alerts users, at most once a day).

    Two idempotency keys in ``notification_log``: ``day:<date>`` caps delivery at one
    per calendar day, ``digest:<hash>`` stops the same unchanged set of high alerts from
    being re-sent on later days.
    """
    stats = {"users": 0, "sent": 0, "skipped": 0}
    today = today or date.today()
    for recipient in account_service.compliance_recipients():
        google_sub, email = str(recipient["google_sub"]), str(recipient.get("email") or "")
        if "change_alerts" not in account_service.capabilities_for({"sub": google_sub, "email": email}):
            continue
        if account_service.notification_sent(google_sub, "compliance", f"day:{today.isoformat()}"):
            continue
        try:
            report = await asyncio.to_thread(_build_compliance_report, google_sub)
        except Exception:
            logger.exception("Compliance digest failed for %s", google_sub)
            continue
        if not report["alert_counts"].get("high"):
            continue
        stats["users"] += 1
        digest = high_alert_digest(report)
        if account_service.notification_sent(google_sub, "compliance", f"digest:{digest}"):
            stats["skipped"] += 1
            continue
        if not email_sender.configured or not email:
            stats["skipped"] += 1
            continue
        try:
            await email_sender.send(
                to=email,
                subject=f"Uyum özeti: {report['alert_counts']['high']} yüksek öncelikli uyarı",
                html_body=render_compliance_email(report, PUBLIC_BASE_URL),
            )
        except MailError as exc:
            logger.warning("Compliance digest failed for %s: %s", google_sub, exc)
            stats["skipped"] += 1
            continue
        account_service.mark_notified(google_sub, "compliance", f"day:{today.isoformat()}")
        account_service.mark_notified(google_sub, "compliance", f"digest:{digest}")
        stats["sent"] += 1
    return stats


async def watchlist_notification_loop() -> None:
    interval = max(300, int(os.environ.get("WATCHLIST_NOTIFY_INTERVAL_SECONDS", "1800")))
    while True:
        try:
            await asyncio.sleep(interval)
            stats = await notify_watchlist_changes()
            if stats["users"]:
                logger.info("Watch-list notifications: %s", stats)
            compliance_stats = await notify_compliance_alerts()
            if compliance_stats["users"]:
                logger.info("Compliance digests: %s", compliance_stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Watch-list notification loop failed")


BACKGROUND_LOOPS.append(("watchlist-notifications", watchlist_notification_loop))


async def _notify_consultation(request_id: str, kind: str, snippet: str, *, recipient_role: str) -> None:
    """Best-effort e-mail to the other party of a consultation thread."""
    if not email_sender.configured:
        return
    try:
        participants = account_service.consultation_participants(request_id)
        if not participants:
            return
        recipient = participants["consultant_email"] if recipient_role == "consultant" else participants["requester_email"]
        if not recipient:
            return
        subject_line = {
            "new_request": "Yeni danışmanlık talebi",
            "message": "Danışmanlık görüşmesinde yeni mesaj",
            "status": "Danışmanlık talebinizin durumu değişti",
        }.get(kind, "Danışmanlık bildirimi")
        await email_sender.send(
            to=recipient,
            subject=f"{subject_line}: {participants['subject'][:80]}",
            html_body=render_consultation_email(kind, participants["subject"], snippet[:300], PUBLIC_BASE_URL),
        )
    except Exception:
        logger.exception("Consultation notification failed")


@mcp.custom_route("/api/changes", methods=["GET"])
async def web_changes(request: Request):
    """Expose the local official snapshot ledger for the in-app monitor."""
    limited = _rate_limit_response(request, "change-ledger", limit=60, window_seconds=60)
    if limited:
        return limited
    params = request.query_params
    kind = params.get("kind", "").strip().lower() or None
    if kind and kind not in change_ledger_kinds():
        return JSONResponse({"error": "Bilinmeyen değişiklik türü."}, status_code=422)
    gtip = re.sub(r"\D", "", params.get("gtip", ""))[:12] or None
    since = _normalise_date(params.get("since")) if params.get("since") else None
    try:
        limit = max(1, min(int(params.get("limit", "200") or 200), 1000))
    except ValueError:
        limit = 200
    return JSONResponse(
        {
            "tariff": {
                "import_regime": tariff_engine.changes("import_regime", limit=100),
                "additional_duty": tariff_engine.changes("additional_duty", limit=100),
            },
            "controls": control_engine.changes(limit=100),
            "trade_measures": trade_measure_engine.store.changes(limit=50),
            "trade_measure_status": trade_measure_engine.status(),
            # Unified persistent ledger (all kinds, full history, row-level before/after).
            "ledger": change_ledger.changes(kind=kind, gtip_prefix=gtip, since=since, limit=limit),
            "batches": change_ledger.batches(kind=kind, limit=30),
            "ledger_summary": change_ledger.summary(),
            # Editorial review gate state for the in-app "new version under review" strip.
            "review": {"mode": review_service.policy.mode, "pending_count": review_service.pending_count()},
            "generated_at": time.time(),
        }
    )


def change_ledger_kinds() -> tuple[str, ...]:
    from change_ledger import KINDS

    return KINDS


@mcp.custom_route("/api/admin/changes", methods=["GET"])
async def web_admin_changes(request: Request):
    """Change batches with parse warnings and lineage (admin or editor)."""
    limited = _rate_limit_response(request, "admin-changes", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        _require_role(request, "editor")
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    params = request.query_params
    kind = params.get("kind", "").strip().lower() or None
    if kind and kind not in change_ledger_kinds():
        return JSONResponse({"error": "Bilinmeyen değişiklik türü."}, status_code=422)
    batch_id = params.get("batch", "").strip()[:200] or None
    if batch_id:
        batch = change_ledger.batch(batch_id)
        if batch is None:
            return JSONResponse({"error": "Değişiklik kaydı bulunamadı."}, status_code=404)
        return JSONResponse(
            {"batch": batch, "changes": change_ledger.changes(batch_id=batch_id, limit=500)},
            headers={"Cache-Control": "no-store"},
        )
    try:
        limit = max(1, min(int(params.get("limit", "50") or 50), 200))
    except ValueError:
        limit = 50
    return JSONResponse(
        {"batches": change_ledger.batches(kind=kind, limit=limit), "summary": change_ledger.summary()},
        headers={"Cache-Control": "no-store"},
    )

@mcp.custom_route("/api/admin/reviews", methods=["GET"])
async def web_admin_reviews(request: Request):
    """Editorial review queue: snapshots waiting for approval (admin or editor)."""
    limited = _rate_limit_response(request, "admin-reviews", limit=60, window_seconds=60)
    if limited:
        return limited
    try:
        _require_role(request, "editor")
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    return JSONResponse(review_service.overview(), headers={"Cache-Control": "no-store"})


@mcp.custom_route("/api/admin/reviews/{kind}/{snapshot_id}", methods=["POST"])
async def web_admin_review_decision(request: Request):
    """Approve or reject one pending snapshot (admin or editor; audited)."""
    limited = _rate_limit_response(request, "admin-review-decision", limit=30, window_seconds=60)
    if limited:
        return limited
    try:
        _trusted_request_origin(request)
        actor = _require_role(request, "editor")
        body = await request.json()
        if not isinstance(body, dict):
            raise AccountError("İnceleme kararı geçersiz.")
        kind = str(request.path_params.get("kind", "")).strip().lower()
        snapshot_id = str(request.path_params.get("snapshot_id", "")).strip()[:200]
        action = str(body.get("action", "")).strip().lower()
        if kind not in review_service.engines:
            return JSONResponse({"error": "Bilinmeyen veri türü."}, status_code=422)
        if action not in {"approve", "reject"}:
            return JSONResponse({"error": "Karar 'approve' veya 'reject' olmalıdır."}, status_code=422)
        try:
            result = review_service.review(kind, snapshot_id, action, actor=actor, note=str(body.get("note", ""))[:1000])
        except KeyError:
            return JSONResponse({"error": "Snapshot bulunamadı."}, status_code=404)
        return JSONResponse({"reviewed": True, "result": result, "pending_count": review_service.pending_count()})
    except SecurityViolation as exc:
        return _security_response(exc)
    except AuthError as exc:
        return _auth_error(exc, status_code=403)
    except AccountError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


def _review_audit(actor: dict[str, Any], action: str, target_type: str, target_id: str, details: dict[str, Any]) -> None:
    account_service.record_audit(actor, action, target_type, target_id, details)


review_service.audit = _review_audit
_REVIEW_NOTIFIED: set[str] = set()


async def notify_pending_reviews() -> dict[str, int]:
    """E-mail the admin allow-list once per pending snapshot (best effort, no secrets)."""
    stats = {"pending": 0, "sent": 0}
    pending = [item for item in review_service.pending() if item["snapshot_id"] not in _REVIEW_NOTIFIED]
    stats["pending"] = len(pending)
    if not pending or not email_sender.configured:
        return stats
    recipients = sorted(address for address in account_service.admin_emails if "@" in address)
    for address in recipients:
        try:
            await email_sender.send(
                to=address,
                subject=f"{len(pending)} resmî veri sürümü editör onayı bekliyor",
                html_body=render_review_email(pending, PUBLIC_BASE_URL),
            )
            stats["sent"] += 1
        except MailError as exc:
            logger.warning("Review notification to %s failed: %s", address, exc)
    _REVIEW_NOTIFIED.update(item["snapshot_id"] for item in pending)
    return stats


async def review_notification_loop() -> None:
    await asyncio.sleep(90)
    while True:
        try:
            if review_service.policy.enabled:
                await notify_pending_reviews()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Review notification loop failed")
        await asyncio.sleep(1800)


BACKGROUND_LOOPS.append(("review-notifications", review_notification_loop))


# Add health check endpoint to the MCP server
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for Coolify and other monitoring services."""
    tariff_status = tariff_engine.status()
    control_status = control_engine.status()
    classification_status = classification_engine.status()
    foreign_status = foreign_tariff_engine.status()
    ebti_status = ebti_engine.status()
    eu_taric_status = eu_taric_engine.status()
    # Depolama yalnız özet sayılarla: /health herkese açık, dosya adı ve yol verilmez.
    # Hata durumu sağlık kontrolünü bozmamalı; Coolify bu ucu canlılık için okuyor.
    try:
        storage_report = storage_service.report()
        storage_fields = {
            "disk_percent_used": storage_report["disk"]["percent_used"],
            "disk_free_bytes": storage_report["disk"]["free_bytes"],
            "data_bytes": storage_report["data_bytes"],
            "backup_bytes": storage_report["backup_bytes"],
            "backup_enabled": storage_report["backup"]["enabled"],
            "last_backup_at": (storage_report["backup"]["last_run"] or {}).get("at"),
            "storage_warnings": len(storage_report["warnings"]),
        }
    except Exception:  # noqa: BLE001 – depolama raporu sağlık kontrolünü düşürmez
        logger.exception("Depolama raporu okunamadı")
        storage_fields = {"disk_percent_used": None, "storage_warnings": None}
    return JSONResponse({
        "status": "healthy",
        "service": "Mevzuat MCP Server",
        "version": "1.8.0",
        # Coolify her derlemede SOURCE_COMMIT'i gecirir; hangi surumun canlida
        # oldugunu dogrulamak icin.
        "commit": os.environ.get("SOURCE_COMMIT", "")[:12] or None,
        "tariff_ready": tariff_status.ready,
        "tariff_measures": tariff_status.measure_count,
        "controls_ready": control_status.ready,
        "control_scope_rows": control_status.scope_count,
        "classification_evidence_ready": classification_status.ready,
        "classification_evidence_pages": classification_status.page_count,
        "foreign_tariff_ready": foreign_status.get("ready", False),
        "foreign_tariff_chapters": foreign_status.get("chapter_count", 0),
        "uk_codes": foreign_status.get("uk_code_count", 0),
        "uk_archived_commodities": foreign_status.get("archived_commodities", 0),
        "swiss_codes": foreign_status.get("swiss_code_count", 0),
        "ebti_ready": ebti_status.get("ready", False),
        "ebti_decisions": ebti_status.get("decision_count", 0),
        "eu_taric_enabled": eu_taric_status.get("enabled", False),
        "eu_taric_archived": eu_taric_status.get("archived_lookups", 0),
        "eu_taric_fill_enabled": (eu_taric_status.get("fill") or {}).get("enabled", False),
        "eu_taric_fill_pending": (eu_taric_status.get("fill") or {}).get("pending_pairs", 0),
        "eu_taric_fill_total": (eu_taric_status.get("fill") or {}).get("total_pairs", 0),
        **storage_fields,
        "review_mode": review_service.policy.mode,
        "pending_reviews": (
            tariff_status.pending_review_count
            + control_status.pending_review_count
            + classification_status.pending_review_count
            + int(foreign_status.get("pending_review_count", 0))
            + int(ebti_status.get("pending_review_count", 0))
        ),
    })

class McpRateLimitMiddleware:
    """Fail-open 20 request/minute IP limit for the public AI endpoint."""

    def __init__(self, asgi_app: Any) -> None:
        self.asgi_app = asgi_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            for key, value in scope.get("headers", []):
                if key.lower() == b"x-forwarded-proto" and value.lower() == b"https":
                    scope["scheme"] = "https"
                    break
            path = str(scope.get("path", ""))
            if path == "/mcp/":
                scope["path"] = "/mcp"
                if "raw_path" in scope:
                    scope["raw_path"] = b"/mcp"
                path = "/mcp"

            if path.startswith("/mcp"):
                try:
                    headers = {
                        key.decode("latin-1").lower(): value.decode("latin-1")
                        for key, value in scope.get("headers", [])
                    }
                    forwarded = headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
                    client = scope.get("client") or ("unknown", 0)
                    client_ip = forwarded or str(client[0])
                    allowed, retry_after = rate_limiter.check(
                        f"mcp:{client_ip}", limit=20, window_seconds=60
                    )
                    if not allowed:
                        payload = json.dumps(
                            {
                                "error": "Çok hızlı MCP isteği gönderiyorsunuz. Lütfen kısa bir süre sonra yeniden deneyin.",
                                "retry_after": retry_after,
                            },
                            ensure_ascii=False,
                        ).encode("utf-8")
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 429,
                                "headers": [
                                    (b"content-type", b"application/json; charset=utf-8"),
                                    (b"retry-after", str(retry_after).encode("ascii")),
                                    (b"content-length", str(len(payload)).encode("ascii")),
                                ],
                            }
                        )
                        await send({"type": "http.response.body", "body": payload})
                        return
                except Exception:
                    logger.exception("MCP rate limiter failed open")
        await self.asgi_app(scope, receive, send)


# Create ASGI app directly from FastMCP server and protect the public MCP
# endpoint without buffering its streaming responses.
_mcp_http_app = mcp.http_app()


async def _not_found(request: Request, exc: Exception):
    """Branded 404 for browser paths; JSON for API and MCP clients."""
    path = request.url.path
    if path.startswith(("/api/", "/mcp")) or "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"error": "Kaynak bulunamadı.", "path": path}, status_code=404)
    page = (WEB_DIR / "404.html").read_text(encoding="utf-8")
    return HTMLResponse(page, status_code=404, headers={"Cache-Control": "no-store"})


_mcp_http_app.add_exception_handler(404, _not_found)
app = McpRateLimitMiddleware(_mcp_http_app)

# Endpoints:
# - / - Web search interface
# - /api/search and /api/document/{id} - Web interface API
# - /mcp/ - MCP server (Streamable HTTP transport)
# - /health - Health check for monitoring
# Run with: uvicorn app:app --host 0.0.0.0 --port 8000
