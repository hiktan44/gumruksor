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


_H3 = "<h3 style='margin:14px 0 6px;font-size:15px;'>{title}</h3>"
_UL = "<ul style='margin:4px 0 0;padding-left:18px;font-size:{size}px;'>{items}</ul>"
_TD = "<td style='padding:4px 10px;border:1px solid #d6dee8;'>"
_TABLE = "<table style='border-collapse:collapse;font-size:13px;'>{rows}</table>"
_FINDING_STATUS = {
    "required": "Zorunlu", "likely": "Muhtemel", "conditional": "Koşullu", "not_found": "Bulunamadı",
    "unknown": "Belirsiz", "applicable": "Uygulanır", "possible": "Olası",
}
_RISK_LABELS = {"moderate": "Orta", "high": "Yüksek", "critical": "Kritik"}
_REVIEW_LABELS = {"BTB": "Bağlayıcı Tarife Bilgisi", "gümrük_müşaviri": "Gümrük müşaviri", "yetkili_kurum": "Yetkili kurum"}
_COST_LABELS = (
    ("customs_value_estimate", "Gümrük kıymeti (tahmini)"),
    ("customs_duty", "Gümrük vergisi"),
    ("additional_duty", "İlave gümrük vergisi (İGV)"),
    ("additional_financial_liability", "Ek mali yükümlülük"),
    ("vat_base_estimate", "KDV matrahı (tahmini)"),
    ("vat", "KDV"),
    ("known_landed_total", "Bilinen kalemlerle toplam maliyet"),
    ("unit_landed_cost", "Birim maliyet"),
)


def _items(values: Any, *, limit: int | None = None) -> str:
    values = list(values or [])
    if limit is not None:
        values = values[:limit]
    return "".join(f"<li>{_esc(item)}</li>" for item in values)


def _citations(values: list[str]) -> str:
    return f" <small>[{_esc(', '.join(values))}]</small>" if values else ""


def _amount(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    text = f"{value:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _tariff_rates_section(result: CustomsPrecheckResult) -> str | None:
    if result.tariff_lookup is None:
        return None
    safe = result.tariff_lookup.unambiguous_rates or {}
    if not safe:
        return None
    rate_rows = "".join(
        f"<tr>{_TD}{_esc(label)}</td>{_TD}%{_esc(value)}</td></tr>"
        for label, value in (
            ("Gümrük vergisi", safe.get("customs_duty")),
            ("İlave gümrük vergisi (İGV)", safe.get("additional_duty")),
            ("Ek mali yükümlülük", safe.get("additional_financial_liability")),
        )
        if value is not None
    )
    if not rate_rows:
        return None
    origin = f" · menşe: {_esc(result.tariff_lookup.origin_country)}" if result.tariff_lookup.origin_country else ""
    return (
        _H3.format(title="Resmî tarife oranları")
        + _TABLE.format(rows=rate_rows)
        + f"<p style='margin:4px 0 0;font-size:12px;color:#43536c;'>Kod: {_esc(result.tariff_lookup.gtip)}{origin}</p>"
    )


def _origin_documents_section(result: CustomsPrecheckResult) -> str | None:
    if result.origin_documents is None:
        return None
    docs = "".join(f"<li>{_esc(item.name)}</li>" for item in result.origin_documents.documents)
    return _H3.format(title=f"Menşe belgeleri · {_esc(result.origin_documents.regime_name)}") + _UL.format(size=13, items=docs)


def _missing_information_section(result: CustomsPrecheckResult) -> str | None:
    if not result.missing_information:
        return None
    return _H3.format(title="Eksik veya teyit gereken bilgiler") + _UL.format(
        size=13, items=_items(result.missing_information, limit=8)
    )


def _sources_section(result: CustomsPrecheckResult) -> str | None:
    sources = "".join(
        f"<li style='margin:3px 0;'><a href='{_esc(source.url)}' style='color:#006678;'>{_esc(source.title)}</a>"
        f"{f' · {_esc(source.authority)}' if source.authority else ''}</li>"
        for source in result.sources[:12]
        if source.url
    )
    if not sources:
        return None
    return _H3.format(title="Resmî kaynaklar") + _UL.format(size=12, items=sources)


def _candidates_section(result: CustomsPrecheckResult) -> str | None:
    if not result.candidate_gtips:
        return None
    rows = "".join(
        f"<tr>{_TD}<code>{_esc(item.code)}</code></td>{_TD}{_esc(item.confidence)}</td>{_TD}{_esc(item.explanation)}"
        f"{_citations(item.citations)}</td></tr>"
        for item in result.candidate_gtips
    )
    return _H3.format(title="Aday GTİP / CN kodları (bağlayıcı değildir)") + _TABLE.format(
        rows=f"<tr><th style='padding:4px 10px;border:1px solid #d6dee8;'>Kod</th><th style='padding:4px 10px;border:1px solid #d6dee8;'>Güven</th><th style='padding:4px 10px;border:1px solid #d6dee8;'>Gerekçe</th></tr>{rows}"
    )


def _findings_section(title: str, findings: list[Any]) -> str | None:
    if not findings:
        return None
    rows = "".join(
        f"<tr>{_TD}{_esc(item.name)}</td>{_TD}{_esc(_FINDING_STATUS.get(item.status, item.status))}"
        f"{f' · %{_esc(item.rate)}' if getattr(item, 'rate', None) else ''}</td>{_TD}{_esc(item.explanation)}"
        f"{_citations(item.citations)}</td></tr>"
        for item in findings
    )
    return _H3.format(title=title) + _TABLE.format(
        rows=f"<tr><th style='padding:4px 10px;border:1px solid #d6dee8;'>Kalem</th><th style='padding:4px 10px;border:1px solid #d6dee8;'>Durum</th><th style='padding:4px 10px;border:1px solid #d6dee8;'>Açıklama</th></tr>{rows}"
    )


def _cost_section(result: CustomsPrecheckResult) -> str | None:
    cost = result.deterministic_cost
    if not isinstance(cost, dict):
        return None
    currency = _esc(cost.get("currency") or "")
    rows = "".join(
        f"<tr>{_TD}{_esc(label)}</td>{_TD}{amount} {currency}</td></tr>"
        for key, label in _COST_LABELS
        if (amount := _amount(cost.get(key))) is not None
    )
    if not rows:
        return None
    notes: list[str] = []
    if cost.get("status") == "rates_missing":
        notes.append("Eksik oranlar nedeniyle toplam bilinçli olarak tamamlanmamıştır.")
    missing = cost.get("missing_rates")
    if isinstance(missing, list) and missing:
        notes.append("Eksik kalemler: " + ", ".join(str(item) for item in missing[:8]))
    if cost.get("note"):
        notes.append(str(cost["note"]))
    note_html = "".join(f"<p style='margin:4px 0 0;font-size:12px;color:#43536c;'>{_esc(note)}</p>" for note in notes)
    return _H3.format(title="Kullanıcı oranlarıyla maliyet taslağı") + _TABLE.format(rows=rows) + note_html


def _expert_packet_section(result: CustomsPrecheckResult) -> str | None:
    packet = result.expert_review_packet
    lines = [
        f"Risk düzeyi: {_RISK_LABELS.get(packet.risk_level, packet.risk_level)}",
        "Uzman incelemesi gerekli: " + ("evet" if packet.escalation_required else "hayır"),
    ]
    if packet.review_types:
        lines.append("Önerilen inceleme: " + ", ".join(_REVIEW_LABELS.get(item, item) for item in packet.review_types))
    lines.extend(packet.reasons[:8])
    questions = _items(packet.questions_for_reviewer, limit=8)
    body = _UL.format(size=13, items=_items(lines))
    if questions:
        body += "<p style='margin:6px 0 0;font-size:13px;'>İnceleyiciye sorular:</p>" + _UL.format(size=13, items=questions)
    return _H3.format(title="Uzman inceleme paketi") + body


def _next_steps_section(result: CustomsPrecheckResult) -> str | None:
    if not result.next_steps:
        return None
    return _H3.format(title="Sonraki güvenli adımlar") + (
        f"<ol style='margin:4px 0 0;padding-left:18px;font-size:13px;'>{_items(result.next_steps, limit=12)}</ol>"
    )


def _safety_notes_section(result: CustomsPrecheckResult) -> str | None:
    if not result.safety_notes:
        return None
    return _H3.format(title="Güvenlik notları") + _UL.format(size=12, items=_items(result.safety_notes, limit=8))


def _image_observation_section(result: CustomsPrecheckResult) -> str | None:
    if not result.image_observation:
        return None
    return _H3.format(title="Fotoğrafta görülenler") + f"<p style='margin:4px 0 0;font-size:13px;'>{_esc(result.image_observation)}</p>"


def precheck_sections(result: CustomsPrecheckResult, base_url: str, *, detailed: bool = False) -> list[str]:
    """Inline-styled HTML fragments shared by the e-mail and the PDF report.

    The default set is the compact e-mail digest; ``detailed=True`` adds the
    findings, cost draft, expert packet and next steps for the PDF report (which
    renders its own source ledger, so the short source list is omitted there).
    ``base_url`` is accepted for parity with the renderers; sections never embed
    client-supplied markup, every value goes through ``_esc``.
    """
    del base_url  # reserved: sections are link-free so e-mail and PDF stay identical
    sections: list[str | None] = [_tariff_rates_section(result), _origin_documents_section(result)]
    if detailed:
        sections.extend(
            [
                _candidates_section(result),
                _findings_section("TAREKS · TSE · kimyasal · laboratuvar kontrolleri", result.controls),
                _findings_section("Gerekli belge ve izinler", result.required_documents),
                _findings_section("Vergi ve mali yükümlülük bulguları", result.taxes),
                _cost_section(result),
            ]
        )
    sections.append(_missing_information_section(result))
    if detailed:
        sections.extend(
            [
                _image_observation_section(result),
                _expert_packet_section(result),
                _next_steps_section(result),
                _safety_notes_section(result),
            ]
        )
    else:
        sections.append(_sources_section(result))
    return [section for section in sections if section]


def render_precheck_email(result: CustomsPrecheckResult, base_url: str) -> str:
    """Render the dossier as a compact, inline-styled HTML e-mail."""
    rows = precheck_sections(result, base_url)
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
