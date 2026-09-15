"""Server-side PDF report for the customs precheck dossier (PRD Faz 2.1).

The report is rendered from the validated ``CustomsPrecheckResult`` only, so no
client-supplied HTML reaches the document. Every page carries the mandatory
legal footer (legal notice, decision-support sentence, brand and page numbers).
Two renderers are supported and selected with ``PDF_RENDERER``:

* ``playwright`` (default): headless Chromium; every network request is aborted.
* ``pymupdf``: pure-Python fallback that lays the HTML out with the MuPDF Story
  engine and stamps the footer on each page.
* ``auto``: try Playwright first, then PyMuPDF.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Any

from customs_advisor import CustomsPrecheckResult, _legal_notice
from email_service import _esc, precheck_sections

logger = logging.getLogger(__name__)

REPORT_TITLE = "Gümrükçe'ye Sor · Ön değerlendirme raporu"
BRAND = "gumruksor.com"
DECISION_SUPPORT_SENTENCE = (
    "Nihai tarife tespiti bağlayıcı karar yerine geçmez; sonuçlar karar destek niteliğindedir."
)
PAGE_TOKEN = "{{page}}"
PAGES_TOKEN = "{{pages}}"
RENDER_TIMEOUT_SECONDS = 20
_A4_POINTS = (595.0, 842.0)
_FOOTER_HEIGHT_POINTS = 78.0
_PAGE_MARGIN_POINTS = 40.0

_STATUS_LABELS = {
    "preliminary": "Ön değerlendirme",
    "needs_information": "Bilgi gerekli",
    "insufficient_evidence": "Kanıt yetersiz",
    "evidence_only": "Yalnız kanıt paketi",
}

# Deliberately plain CSS: it has to render identically in Chromium print mode
# and in the MuPDF Story engine (no flex/grid, no external assets, no scripts).
_REPORT_CSS = """
body { font-family: Helvetica, Arial, sans-serif; color: #0b1e3f; font-size: 10.5pt; line-height: 1.45; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 4pt; }
h2 { font-size: 13pt; margin: 14pt 0 4pt; }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt; }
p { margin: 3pt 0; }
ul, ol { margin: 3pt 0 3pt 16pt; padding: 0; }
li { margin: 1pt 0; }
table { border-collapse: collapse; font-size: 9.5pt; margin: 4pt 0; width: 100%; }
th, td { border: 1px solid #d6dee8; padding: 3pt 6pt; text-align: left; vertical-align: top; }
th { background: #f3f7fa; }
code { font-family: Courier, monospace; font-size: 9pt; }
.kicker { font-size: 8.5pt; letter-spacing: .08em; text-transform: uppercase; color: #006678; margin: 0 0 2pt; }
.meta { font-size: 9pt; color: #43536c; }
.summary { font-size: 11pt; margin: 6pt 0 4pt; }
.legal { margin: 14pt 0 6pt; padding: 8pt 10pt; border-left: 3px solid #b54708; background: #fff7ed; font-size: 9pt; }
.ledger { font-size: 8.5pt; }
.ledger td { word-break: break-all; }
.footer-note { font-size: 8pt; color: #738097; margin-top: 8pt; }
"""

_FOOTER_CSS = (
    "font-family:Helvetica,Arial,sans-serif;font-size:6.5pt;line-height:1.35;color:#43536c;"
    "width:100%;padding:0 12mm;box-sizing:border-box;text-align:left;"
)


class PdfRenderError(RuntimeError):
    """Raised when no renderer could produce the PDF; the message is user-safe."""


def renderer_mode() -> str:
    mode = os.environ.get("PDF_RENDERER", "playwright").strip().lower()
    return mode if mode in {"playwright", "pymupdf", "auto"} else "playwright"


def _report_identity(result: CustomsPrecheckResult) -> dict[str, str]:
    inquiry = result.inquiry
    packet = result.expert_review_packet
    gtip = (
        inquiry.candidate_gtip
        or (result.tariff_lookup.gtip if result.tariff_lookup is not None else None)
        or packet.selected_tariff_code
        or "—"
    )
    product = (inquiry.product_description or inquiry.question or "").strip() or "—"
    return {
        "product": product[:300],
        "gtip": str(gtip),
        "origin": inquiry.origin_country or "—",
        "dispatch": inquiry.dispatch_country or "",
        "status": _STATUS_LABELS.get(result.status, result.status),
    }


def report_footer_text(result: CustomsPrecheckResult) -> str:
    """Plain text of the mandatory footer (page tokens included)."""
    return (
        f"{_legal_notice(result.as_of)} {DECISION_SUPPORT_SENTENCE} "
        f"{BRAND} · Sayfa {PAGE_TOKEN} / {PAGES_TOKEN}"
    )


def report_footer_html(result: CustomsPrecheckResult) -> str:
    """HTML fragment for the per-page footer; ``{{page}}``/``{{pages}}`` are substituted by the renderer."""
    return (
        f"<div style=\"{_FOOTER_CSS}\">{_esc(_legal_notice(result.as_of))} "
        f"<b>{_esc(DECISION_SUPPORT_SENTENCE)}</b> "
        f"<span style=\"white-space:nowrap;\">{_esc(BRAND)} · Sayfa {PAGE_TOKEN} / {PAGES_TOKEN}</span></div>"
    )


def _source_ledger(result: CustomsPrecheckResult) -> str:
    if not result.sources:
        return "<p class='meta'>Bu dosyada resmî kaynak kaydı bulunmuyor.</p>"
    rows = "".join(
        "<tr>"
        f"<td><code>{_esc(source.id)}</code></td>"
        f"<td>{_esc(source.authority)}</td>"
        f"<td>{_esc(source.title)}</td>"
        f"<td>{_esc(source.url)}</td>"
        f"<td>{_esc(source.retrieved_at)}</td>"
        f"<td><code>{_esc(source.sha256[:12]) if source.sha256 else '—'}</code></td>"
        "</tr>"
        for source in result.sources
    )
    return (
        "<table class='ledger'><thead><tr><th>Kaynak</th><th>Kurum</th><th>Başlık</th>"
        "<th>URL</th><th>Alınma</th><th>SHA-256</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def render_precheck_report_html(
    result: CustomsPrecheckResult,
    *,
    base_url: str,
    generated_for: str,
    generated_at: str,
) -> str:
    """Standalone A4-friendly HTML: inline CSS only, no scripts, no external assets."""
    identity = _report_identity(result)
    sections = "".join(precheck_sections(result, base_url, detailed=True))
    notice = _legal_notice(result.as_of)
    legal_lines = [result.legal_notice]
    if notice not in result.legal_notice:
        legal_lines.append(notice)
    legal = "".join(f"<p>{_esc(line)}</p>" for line in legal_lines if line)
    dispatch = f" · sevk: {_esc(identity['dispatch'])}" if identity["dispatch"] else ""
    is_export = getattr(result, "direction", "import") == "export"
    # Başlık yönü söylemeli: ihracat dosyasında "İthalat raporu" yazması aktif olarak yanıltıcıdır.
    title = REPORT_TITLE.replace(
        "Ön değerlendirme", "İhracat ön değerlendirme" if is_export else "İthalat ön değerlendirme"
    )
    destination = getattr(getattr(result, "inquiry", None), "destination_country", None)
    destination_line = (
        f"<p class=\"meta\">Hedef ülke: {_esc(str(destination))}</p>" if is_export and destination else ""
    )
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>{_esc(title)}</title><style>{_REPORT_CSS}</style></head>
<body>
<p class="kicker">{_esc(BRAND)} · Ticaret Bilgi Masası</p>
<h1>{_esc(title)}</h1>
<p class="meta">Ürün: {_esc(identity['product'])}</p>
{destination_line}
<p class="meta">GTİP / aday kod: <code>{_esc(identity['gtip'])}</code> · menşe: {_esc(identity['origin'])}{dispatch} · durum: {_esc(identity['status'])}</p>
<p class="meta">Kaynak tarihi (as_of): {_esc(result.as_of)} · Hazırlanan: {_esc(generated_for)} · Oluşturma: {_esc(generated_at)}</p>
<p class="summary">{_esc(result.summary)}</p>
{sections}
<h2>Resmî kanıt defteri · {len(result.sources)} kaynak</h2>
{_source_ledger(result)}
<div class="legal"><b>Zorunlu yasal uyarı.</b>{legal}<p><b>{_esc(DECISION_SUPPORT_SENTENCE)}</b></p></div>
<p class="footer-note">Bu rapor {_esc(base_url)} üzerinden oluşturulan ön değerlendirme dosyasından üretilmiştir; bağlayıcı tarife bilgisi değildir. Her sayfanın alt bilgisinde yasal uyarı ve sayfa numarası yer alır.</p>
</body></html>"""


# ----------------------------------------------------------------------------- renderers
_render_slot = asyncio.Semaphore(1)


async def render_pdf(html: str, *, footer_html: str) -> bytes:
    """Render ``html`` to PDF bytes with the selected renderer; raises PdfRenderError on failure."""
    mode = renderer_mode()
    errors: list[str] = []
    if mode in {"playwright", "auto"}:
        try:
            return _check_pdf(await _render_with_playwright(html, footer_html))
        except Exception as exc:  # noqa: BLE001 - renderer errors are reported to the caller
            logger.warning("Playwright PDF renderer failed: %s", type(exc).__name__)
            errors.append(f"playwright: {type(exc).__name__}")
            if mode == "playwright":
                raise PdfRenderError("PDF raporu şu anda oluşturulamadı; kısa süre sonra yeniden deneyin.") from exc
    try:
        return _check_pdf(await asyncio.to_thread(_render_with_pymupdf, html, footer_html))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PyMuPDF PDF renderer failed: %s (%s)", type(exc).__name__, "; ".join(errors))
        raise PdfRenderError("PDF raporu şu anda oluşturulamadı; kısa süre sonra yeniden deneyin.") from exc


def _check_pdf(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(b"%PDF-"):
        raise PdfRenderError("PDF çıktısı doğrulanamadı.")
    return bytes(data)


async def _render_with_playwright(html: str, footer_html: str) -> bytes:
    from playwright.async_api import async_playwright

    footer_template = footer_html.replace(PAGE_TOKEN, '<span class="pageNumber"></span>').replace(
        PAGES_TOKEN, '<span class="totalPages"></span>'
    )
    launch_kwargs: dict[str, Any] = {"headless": True, "args": ["--disable-dev-shm-usage"]}
    executable = os.environ.get("PDF_CHROMIUM_EXECUTABLE", "").strip()
    if executable:
        launch_kwargs["executable_path"] = executable

    async def _abort(route, _request) -> None:
        await route.abort()

    async def _render() -> bytes:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(locale="tr-TR", java_script_enabled=False)
                page = await context.new_page()
                await page.route("**/*", _abort)
                await page.set_content(html, wait_until="load")
                await page.emulate_media(media="print")
                return await page.pdf(
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template=footer_template,
                    margin={"top": "14mm", "bottom": "30mm", "left": "14mm", "right": "14mm"},
                )
            finally:
                await browser.close()

    async with _render_slot:
        return await asyncio.wait_for(_render(), RENDER_TIMEOUT_SECONDS)


def _import_pymupdf():
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:  # pragma: no cover - older wheels expose only ``fitz``
        import fitz  # type: ignore

        return fitz


def _render_with_pymupdf(html: str, footer_html: str) -> bytes:
    """Lay the report out into A4 pages with MuPDF Story, then stamp the footer on every page."""
    fitz = _import_pymupdf()
    width, height = _A4_POINTS
    mediabox = fitz.Rect(0, 0, width, height)
    content_box = fitz.Rect(
        _PAGE_MARGIN_POINTS, _PAGE_MARGIN_POINTS, width - _PAGE_MARGIN_POINTS, height - _FOOTER_HEIGHT_POINTS - 12
    )
    footer_box = fitz.Rect(_PAGE_MARGIN_POINTS, height - _FOOTER_HEIGHT_POINTS, width - _PAGE_MARGIN_POINTS, height - 14)
    buffer = io.BytesIO()
    if hasattr(fitz, "Story") and hasattr(fitz, "DocumentWriter"):
        story = fitz.Story(html=html, user_css=_REPORT_CSS)
        writer = fitz.DocumentWriter(buffer)
        more = 1
        while more:
            device = writer.begin_page(mediabox)
            more, _ = story.place(content_box)
            story.draw(device)
            writer.end_page()
        writer.close()
        document = fitz.open("pdf", buffer.getvalue())
    else:  # pragma: no cover - very old PyMuPDF without Story
        document = fitz.open()
        page = document.new_page(width=width, height=height)
        page.insert_htmlbox(content_box, html, css=_REPORT_CSS)
    total = len(document)
    for index, page in enumerate(document, start=1):
        page.insert_htmlbox(
            footer_box,
            footer_html.replace(PAGE_TOKEN, str(index)).replace(PAGES_TOKEN, str(total)),
        )
    try:
        return document.tobytes(garbage=1, deflate=True)
    finally:
        document.close()
