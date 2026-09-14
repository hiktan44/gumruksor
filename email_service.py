"""Transactional e-mail delivery for the customs dossier (HTTP e-posta API).

The delivery provider stays server-side; end-user surfaces only ever say
"e-posta". Emails are rendered here from the validated dossier structure so no
client-supplied HTML ever reaches the message body.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any

import httpx
from customs_advisor import CustomsPrecheckResult

logger = logging.getLogger(__name__)
_SEND_ENDPOINT = "https://api.resend.com/emails"


class MailError(Exception):
    """Raised when the message cannot be delivered; the message is user-safe."""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def render_precheck_email(result: CustomsPrecheckResult, base_url: str) -> str:
    """Render the dossier as a compact, inline-styled HTML e-mail."""
    rows: list[str] = []
    if result.tariff_lookup is not None:
        safe = result.tariff_lookup.unambiguous_rates or {}
        if safe:
            rate_rows = "".join(
                f"<tr><td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(label)}</td>"
                f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>%{_esc(value)}</td></tr>"
                for label, value in (
                    ("Gümrük vergisi", safe.get("customs_duty")),
                    ("İlave gümrük vergisi (İGV)", safe.get("additional_duty")),
                    ("Ek mali yükümlülük", safe.get("additional_financial_liability")),
                )
                if value is not None
            )
            if rate_rows:
                rows.append(
                    "<h3 style='margin:14px 0 6px;font-size:15px;'>Resmî tarife oranları</h3>"
                    f"<table style='border-collapse:collapse;font-size:13px;'>{rate_rows}</table>"
                    f"<p style='margin:4px 0 0;font-size:12px;color:#43536c;'>Kod: {_esc(result.tariff_lookup.gtip)}"
                    f"{f' · menşe: {_esc(result.tariff_lookup.origin_country)}' if result.tariff_lookup.origin_country else ''}</p>"
                )
    if result.origin_documents is not None:
        docs = "".join(f"<li>{_esc(item.name)}</li>" for item in result.origin_documents.documents)
        rows.append(
            f"<h3 style='margin:14px 0 6px;font-size:15px;'>Menşe belgeleri · {_esc(result.origin_documents.regime_name)}</h3>"
            f"<ul style='margin:4px 0 0;padding-left:18px;font-size:13px;'>{docs}</ul>"
        )
    if result.missing_information:
        missing = "".join(f"<li>{_esc(item)}</li>" for item in result.missing_information[:8])
        rows.append(
            f"<h3 style='margin:14px 0 6px;font-size:15px;'>Eksik veya teyit gereken bilgiler</h3>"
            f"<ul style='margin:4px 0 0;padding-left:18px;font-size:13px;'>{missing}</ul>"
        )
    sources = "".join(
        f"<li style='margin:3px 0;'><a href='{_esc(source.url)}' style='color:#006678;'>{_esc(source.title)}</a>"
        f"{f' · {_esc(source.authority)}' if source.authority else ''}</li>"
        for source in result.sources[:12]
        if source.url
    )
    if sources:
        rows.append(
            "<h3 style='margin:14px 0 6px;font-size:15px;'>Resmî kaynaklar</h3>"
            f"<ul style='margin:4px 0 0;padding-left:18px;font-size:12px;'>{sources}</ul>"
        )
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;color:#0b1e3f;max-width:640px;">
  <p style="margin:0 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#006678;">Ticaret Bilgi Masası · İthalat ön değerlendirme dosyası</p>
  <h2 style="margin:0 0 8px;font-size:19px;">{_esc(result.summary)}</h2>
  <p style="margin:0 0 10px;font-size:12px;color:#43536c;">Durum: {_esc(result.status)} · {_esc(result.as_of)}</p>
  {''.join(rows)}
  <div style="margin:16px 0;padding:10px 12px;border-left:3px solid #b54708;background:#fff7ed;font-size:12px;">{_esc(result.legal_notice)}</div>
  <p style="margin:8px 0 0;font-size:11px;color:#738097;">Bu e-posta {_esc(base_url)} üzerinden oluşturulan ön değerlendirme dosyasıyla gönderilmiştir; bağlayıcı tarife bilgisi değildir.</p>
</div>"""


def render_watch_email(items: list[dict[str, Any]], base_url: str) -> str:
    """One e-mail per user listing the official tariff line changes on watched codes."""
    blocks: list[str] = []
    for item in items:
        rows = "".join(
            f"<tr><td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(change.get('gtip'))}</td>"
            f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(change.get('measure_label') or change.get('measure_type'))}</td>"
            f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(change.get('country_group'))}</td>"
            f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(change.get('before') if change.get('before') is not None else '—')}</td>"
            f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(change.get('after') if change.get('after') is not None else '— (satır kaldırıldı)')}</td></tr>"
            for change in item.get("changes", [])[:20]
        )
        label = f" · {_esc(item.get('label'))}" if item.get("label") else ""
        blocks.append(
            f"<h3 style='margin:14px 0 6px;font-size:15px;'>{_esc(item.get('gtip'))}{label}</h3>"
            f"<p style='margin:0 0 6px;font-size:12px;color:#43536c;'>{_esc(item.get('source_title'))} · yeni sürüm {_esc(item.get('new_snapshot'))}</p>"
            "<table style='border-collapse:collapse;font-size:12px;'><tr>"
            "<th style='padding:4px 10px;border:1px solid #d6dee8;'>GTİP</th><th style='padding:4px 10px;border:1px solid #d6dee8;'>Önlem</th>"
            "<th style='padding:4px 10px;border:1px solid #d6dee8;'>Sütun</th><th style='padding:4px 10px;border:1px solid #d6dee8;'>Önce</th>"
            f"<th style='padding:4px 10px;border:1px solid #d6dee8;'>Sonra</th></tr>{rows}</table>"
        )
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;color:#0b1e3f;max-width:640px;">
  <p style="margin:0 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#006678;">Ticaret Bilgi Masası · İzleme listesi</p>
  <h2 style="margin:0 0 8px;font-size:19px;">İzlediğiniz GTİP satırlarında resmî tarife değişikliği</h2>
  {''.join(blocks)}
  <p style="margin:14px 0 0;font-size:12px;">Ayrıntı ve kaynak satırları: <a href="{_esc(base_url)}/app?scope=customs#changes" style="color:#006678;">{_esc(base_url)}/app</a></p>
  <p style="margin:8px 0 0;font-size:11px;color:#738097;">Bu bildirim resmî arşivin yeni sürümüyle önceki sürümün satır karşılaştırmasından üretilmiştir; bağlayıcı tarife bilgisi değildir. Beyan öncesi resmî kaynağı doğrulayın.</p>
</div>"""


def render_review_email(items: list[dict[str, Any]], base_url: str) -> str:
    """Notify editors that official data snapshots wait in the review queue."""
    labels = {"tariff": "Tarife cetveli", "controls": "Kontrol tebliği", "classification": "AB tüzükleri"}
    rows = "".join(
        f"<tr><td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(labels.get(str(item.get('kind')), item.get('kind')))}</td>"
        f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc(item.get('title') or item.get('source_id'))}</td>"
        f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc((item.get('diff_summary') or {}).get('changed', 0))} / {_esc(item.get('total_rows'))}</td>"
        f"<td style='padding:4px 10px;border:1px solid #d6dee8;'>{_esc('; '.join((item.get('diff_summary') or {}).get('reasons') or []))}</td></tr>"
        for item in items[:30]
    )
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;color:#0b1e3f;max-width:640px;">
  <p style="margin:0 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#006678;">Gümrükçe'ye Sor · İnceleme kuyruğu</p>
  <h2 style="margin:0 0 8px;font-size:19px;">{len(items)} resmî veri sürümü editör onayı bekliyor</h2>
  <p style="margin:0 0 10px;font-size:13px;">Yeni indirilen sürümler onaylanana kadar sorgularda önceki onaylı sürüm kullanılmaya devam eder.</p>
  <table style="border-collapse:collapse;font-size:12px;"><tr>
  <th style="padding:4px 10px;border:1px solid #d6dee8;">Tür</th><th style="padding:4px 10px;border:1px solid #d6dee8;">Kaynak</th>
  <th style="padding:4px 10px;border:1px solid #d6dee8;">Değişen / toplam</th><th style="padding:4px 10px;border:1px solid #d6dee8;">Gerekçe</th></tr>{rows}</table>
  <p style="margin:14px 0 0;font-size:12px;">Karar için: <a href="{_esc(base_url)}/admin#reviews" style="color:#006678;">{_esc(base_url)}/admin</a></p>
</div>"""


def render_consultation_email(kind: str, subject: str, snippet: str, base_url: str) -> str:
    titles = {
        "new_request": "Yeni danışmanlık talebi",
        "message": "Danışmanlık görüşmesinde yeni mesaj",
        "status": "Danışmanlık talebinin durumu değişti",
    }
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;color:#0b1e3f;max-width:640px;">
  <p style="margin:0 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#006678;">Ticaret Bilgi Masası · Gümrük danışmanları</p>
  <h2 style="margin:0 0 8px;font-size:19px;">{_esc(titles.get(kind, 'Danışmanlık bildirimi'))}</h2>
  <p style="margin:0 0 10px;font-size:13px;"><b>Konu:</b> {_esc(subject)}</p>
  <blockquote style="margin:0 0 12px;padding:8px 12px;border-left:3px solid #006678;background:#f3f7fa;font-size:13px;">{_esc(snippet)}</blockquote>
  <p style="margin:0;font-size:12px;">Görüşmeyi uygulamada açın: <a href="{_esc(base_url)}/app?scope=customs" style="color:#006678;">{_esc(base_url)}/app</a></p>
  <p style="margin:8px 0 0;font-size:11px;color:#738097;">Mesaj içeriği yalnızca kısa bir alıntı olarak gönderildi; tam metin ve dosya uygulama içinde görüntülenir.</p>
</div>"""


class ResendEmailSender:
    """Small async client for the transactional e-mail HTTP API."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("RESEND_API_KEY", "").strip()
        self._from = os.environ.get("MAIL_FROM", "").strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20)) if self.configured else None

    @property
    def configured(self) -> bool:
        return bool(self._api_key) and bool(self._from)

    async def send(self, *, to: str, subject: str, html_body: str) -> str:
        if not self.configured or self._client is None:
            raise MailError("E-posta gönderimi henüz yapılandırılmadı.")
        try:
            response = await self._client.post(
                _SEND_ENDPOINT,
                json={"from": self._from, "to": [to], "subject": subject, "html": html_body},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("E-mail delivery request failed: %s", exc)
            raise MailError("E-posta servisine şu anda ulaşılamadı.") from exc
        if response.status_code >= 400:
            logger.warning("E-mail delivery rejected: %s %s", response.status_code, response.text[:200])
            raise MailError("E-posta gönderilemedi; ayarları kontrol edin.")
        message_id = ""
        try:
            message_id = str(response.json().get("id", ""))
        except Exception:
            pass
        return message_id
