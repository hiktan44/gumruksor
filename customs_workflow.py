"""Deterministic import-workflow engine (PRD Faz 2.4).

Turns one ``CustomsPrecheckResult`` (or its JSON dict) into an ordered list of
workflow steps — from product definition to the expert/BTB decision.  Every step
status is derived from fields that already exist on the result; no model call,
no network, no guessing.  Unknown → ``pending`` with a concrete ``next_action``;
a rule that says the step cannot apply → ``not_applicable``; a missing critical
input that stops the step → ``blocked``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

StepStatus = Literal["done", "pending", "blocked", "not_applicable"]

WORKFLOW_VERSION = "tr-import-workflow-v1"
EXPORT_WORKFLOW_VERSION = "tr-export-workflow-v1"

# Categories of control communiqués that run through TAREKS / product-safety
# (ÜGD) rather than a sector ministry permit.  Used only as a fallback when the
# indexed rule carries no ``system`` text.
_TAREKS_CATEGORIES = {
    "sanayi girdileri", "iş makineleri", "telsiz ekipmanları", "ce ürünleri", "oyuncaklar",
    "kişisel koruyucu donanım", "tüketici ürünleri", "yapı malzemeleri", "pil ve akümülatörler",
    "tıbbi cihazlar", "anne ve bebek ürünleri", "tekstil ve deri", "araç parçaları",
    "karayolu taşıtları", "makinalar",
}

_PREFERENTIAL_DOCUMENT_CODES = {
    "EUR1", "EUR_MED", "ORIGIN_DECLARATION", "AGREEMENT_CERT", "GSP_ORIGIN", "SUPPLIER_DECLARATION",
}


class WorkflowStep(BaseModel):
    id: str
    order: int = Field(..., ge=1)
    title: str
    status: StepStatus
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    next_action: str | None = None
    legal_basis: str | None = None


# --------------------------------------------------------------------------- helpers


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    return {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _lower(value: Any) -> str:
    return _text(value).casefold()


def _fmt_rate(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _text(value)
    return f"%{number:g}"


def _live_hits(hits: Any) -> list[dict[str, Any]]:
    """Trade-measure rows that still apply: not expired and not excluded by origin."""
    out: list[dict[str, Any]] = []
    for hit in hits or []:
        item = _as_dict(hit)
        if item.get("status") == "expired" or item.get("origin_match") is False:
            continue
        out.append(item)
    return out


def _cost_line(cost: dict[str, Any], code: str) -> dict[str, Any] | None:
    for line in cost.get("lines") or []:
        item = _as_dict(line)
        if item.get("code") == code:
            return item
    return None


def _rule_is_tareks(rule: dict[str, Any]) -> bool:
    system = _lower(rule.get("system"))
    if system:
        return "tareks" in system or "ürün güvenliği" in system or "urun guvenligi" in system
    return _lower(rule.get("category")) in _TAREKS_CATEGORIES


class _Builder:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.inquiry = _as_dict(result.get("inquiry"))
        self.tariff = _as_dict(result.get("tariff_lookup"))
        self.control = _as_dict(result.get("control_lookup"))
        self.origin_docs = _as_dict(result.get("origin_documents"))
        self.cost = _as_dict(result.get("deterministic_cost"))
        self.packet = _as_dict(result.get("expert_review_packet"))
        self.trade = _as_dict(self.tariff.get("trade_measures")) if self.tariff else {}
        self.excise = _as_dict(self.tariff.get("excise_tax")) if self.tariff else {}
        self.coverage = {key: _as_dict(value) for key, value in (self.tariff.get("measure_coverage") or {}).items()}
        self.steps: list[WorkflowStep] = []

    # -- shared facts ------------------------------------------------------

    @property
    def gtip(self) -> str:
        return _text(self.inquiry.get("candidate_gtip"))

    @property
    def gtip12_confirmed(self) -> bool:
        return bool(
            self.inquiry.get("exact_gtip_confirmed")
            and len(self.gtip) == 12
            and (not self.tariff or (self.tariff.get("status") == "matched" and self.tariff.get("matched_gtip_count", 0) == 1))
        )

    @property
    def origin(self) -> str:
        return _text(self.inquiry.get("origin_country"))

    @property
    def atr_route(self) -> bool:
        """EU-dispatched goods that move with an A.TR (A.TR answered yes, or not answered yet)."""
        if not self.origin_docs:
            return False
        has_atr = any(_as_dict(doc).get("code") == "ATR" for doc in self.origin_docs.get("documents") or [])
        route_atr = self.origin_docs.get("route") == "atr" and self.origin_docs.get("regime") == "customs_union"
        return (has_atr or route_atr) and self.inquiry.get("atr_certificate") is not False

    def add(
        self,
        step_id: str,
        title: str,
        status: StepStatus,
        summary: str,
        *,
        evidence: list[str] | None = None,
        next_action: str | None = None,
        legal_basis: str | None = None,
    ) -> None:
        self.steps.append(
            WorkflowStep(
                id=step_id,
                order=len(self.steps) + 1,
                title=title,
                status=status,
                summary=summary,
                evidence_refs=list(dict.fromkeys(evidence or [])),
                next_action=next_action if status != "done" else None,
                legal_basis=legal_basis,
            )
        )

    # -- rate helper for GV / İGV / EMY -----------------------------------

    def _rate_step(
        self,
        step_id: str,
        title: str,
        *,
        measure_type: str,
        user_field: str,
        label: str,
        legal_basis: str,
        zero_when_absent: bool = False,
    ) -> None:
        inquiry_rate = self.inquiry.get(user_field)
        evidence = [f"inquiry.{user_field}", f"tariff_lookup.unambiguous_rates.{measure_type}"]
        if inquiry_rate is not None:
            self.add(step_id, title, "done", f"{label} kullanıcı tarafından {_fmt_rate(inquiry_rate)} olarak doğrulandı.",
                     evidence=evidence, legal_basis=legal_basis)
            return
        if not self.gtip:
            self.add(step_id, title, "blocked", f"{label} için önce aday GTİP gerekir.", evidence=evidence,
                     next_action="Aday GTİP seçin.", legal_basis=legal_basis)
            return
        if not self.tariff:
            self.add(step_id, title, "pending", "Resmî tarife snapshot sorgusu bu sonuçta yok.", evidence=evidence,
                     next_action=f"{label} oranını resmî cetvelden doğrulayıp girin.", legal_basis=legal_basis)
            return
        status = self.tariff.get("status")
        if status in {"not_found", "unavailable"}:
            self.add(step_id, title, "blocked", "GTİP resmî cetvelde bulunamadı veya cetvel kullanılamadı.", evidence=evidence,
                     next_action="Kodu karar ağacında yeniden seçin.", legal_basis=legal_basis)
            return
        safe = self.tariff.get("unambiguous_rates") or {}
        ambiguous = self.tariff.get("ambiguous_measure_types") or []
        proof_required = self.tariff.get("origin_proof_required") or []
        fallback = self.tariff.get("fallback_rates") or {}
        if not self.origin:
            self.add(step_id, title, "blocked", f"{label} menşe sütununa bağlıdır; menşe ülke girilmedi.", evidence=evidence,
                     next_action="Menşe ülkeyi girin.", legal_basis=legal_basis)
            return
        if measure_type in safe:
            column = _text(self.tariff.get("resolved_country_group")) or "menşe sütunu"
            summary = f"Resmî snapshot: {label} {_fmt_rate(safe[measure_type])} ({column})."
            if measure_type in proof_required:
                fb = fallback.get(measure_type)
                summary += (
                    f" Tercih için tedarikçi beyanı / menşe belgesi tevsiki gerekir; tevsik yoksa {_fmt_rate(fb)} uygulanır."
                    if fb is not None else " Tercih için tedarikçi beyanı / menşe belgesi tevsiki gerekir."
                )
                self.add(step_id, title, "pending", summary, evidence=[*evidence, "tariff_lookup.origin_proof_required"],
                         next_action="Menşe tevsik belgesini temin edin; yoksa 'Diğer Ülkeler' oranıyla hesaplayın.",
                         legal_basis=legal_basis)
                return
            self.add(step_id, title, "done", summary, evidence=evidence, legal_basis=legal_basis)
            return
        if measure_type in ambiguous:
            variants = (self.tariff.get("rate_variants") or {}).get(measure_type) or []
            listed = ", ".join(_fmt_rate(v) for v in variants[:6])
            self.add(step_id, title, "pending",
                     f"Alt GTİP satırlarında {label} değişiyor ({listed or 'birden çok oran'}).",
                     evidence=[*evidence, f"tariff_lookup.rate_variants.{measure_type}"],
                     next_action="12 haneli satırı seçin veya oranı kendiniz doğrulayıp girin.", legal_basis=legal_basis)
            return
        cov = self.coverage.get(measure_type, {})
        if zero_when_absent and cov.get("status") == "verified_snapshot":
            self.add(step_id, title, "done", f"Resmî {label} listesinde bu GTİP için satır yok; {label} %0 kabul edildi.",
                     evidence=[*evidence, f"tariff_lookup.measure_coverage.{measure_type}"], legal_basis=legal_basis)
            return
        note = _text(cov.get("note")) or f"{label} resmî kaynaktan otomatik çözülemedi."
        self.add(step_id, title, "pending", note, evidence=[*evidence, f"tariff_lookup.measure_coverage.{measure_type}"],
                 next_action=f"{label} oranını resmî kaynaktan doğrulayıp girin.", legal_basis=legal_basis)

    # -- steps -------------------------------------------------------------

    def _opening_steps(self) -> None:
        """Yön-nötr ilk dört adım: eşya tanımı, evsaf onayı, aday GTİP, 12 hane.

        Sınıflandırma akışı ithalatta da ihracatta da aynıdır; ihracat kurucusu bu
        adımları aynen devralır ve tek kod yolu korunur.
        """
        inq = self.inquiry

        # 1. Eşya tanımı
        description = _text(inq.get("product_description"))
        composition = _text(inq.get("composition"))
        function = _text(inq.get("function_mechanism")) or _text(inq.get("intended_use"))
        if not description:
            self.add("product_definition", "Eşya tanımı", "blocked", "Ürünün teknik ve ticari tanımı girilmedi.",
                     evidence=["inquiry.product_description"], next_action="Ürünü işlev, malzeme ve kullanım amacıyla tanımlayın.",
                     legal_basis="Gümrük Yönetmeliği md. 111 (eşyanın tanımı)")
        elif not composition and not function:
            self.add("product_definition", "Eşya tanımı", "pending",
                     "Ticari tanım var; malzeme/bileşim ve temel işlev henüz belirtilmedi.",
                     evidence=["inquiry.product_description", "inquiry.composition", "inquiry.function_mechanism"],
                     next_action="Malzeme/bileşimi ve ürünün temel işlevini ekleyin.",
                     legal_basis="Gümrük Yönetmeliği md. 111 (eşyanın tanımı)")
        else:
            self.add("product_definition", "Eşya tanımı", "done",
                     f"Ürün tanımlandı: {description[:80]}{'…' if len(description) > 80 else ''}",
                     evidence=["inquiry.product_description", "inquiry.composition", "inquiry.function_mechanism"],
                     legal_basis="Gümrük Yönetmeliği md. 111 (eşyanın tanımı)")

        # 2. Evsaf onayı
        answers = inq.get("classification_answers") or []
        open_questions = _text(inq.get("classification_questions"))
        attributes = [key for key in ("declared_product_type", "visible_features", "construction_form", "function_mechanism",
                                      "components_accessories", "label_text", "brand_model", "dimensions") if _text(inq.get(key))]
        evidence = ["inquiry.classification_answers", "inquiry.classification_questions", *[f"inquiry.{key}" for key in attributes]]
        if not description:
            self.add("attribute_confirmation", "Evsaf onayı", "blocked", "Eşya tanımı olmadan evsaf onaylanamaz.",
                     evidence=evidence, next_action="Önce eşya tanımını tamamlayın.")
        elif open_questions and not answers:
            self.add("attribute_confirmation", "Evsaf onayı", "pending",
                     "Ayırt edici sınıflandırma soruları var; yanıtlanmadı.", evidence=evidence,
                     next_action="Sınıflandırma sorularını yanıtlayın veya teknik belge ekleyin.")
        elif answers or attributes:
            self.add("attribute_confirmation", "Evsaf onayı", "done",
                     f"{len(answers)} soru yanıtlandı; {len(attributes)} evsaf alanı kayıtlı.", evidence=evidence)
        else:
            self.add("attribute_confirmation", "Evsaf onayı", "pending",
                     "Görünen/teknik evsaf alanları henüz onaylanmadı.", evidence=evidence,
                     next_action="Fotoğraf, ürün sayfası veya elle özellik girişiyle evsaf onayını çalıştırın.")

        # 3. Aday GTİP
        model_candidates = [_text(_as_dict(item).get("code")) for item in self.result.get("candidate_gtips") or []]
        evidence = ["inquiry.candidate_gtip", "inquiry.tariff_selection_confirmed", "candidate_gtips[].code"]
        if self.gtip and inq.get("tariff_selection_confirmed"):
            self.add("candidate_gtip", "Aday GTİP", "done", f"Aday kod resmî karar ağacında seçildi: {self.gtip}.",
                     evidence=evidence, legal_basis="Türk Gümrük Tarife Cetveli (İthalat Rejimi Kararı eki)")
        elif self.gtip:
            self.add("candidate_gtip", "Aday GTİP", "pending", f"Aday kod girildi ({self.gtip}); karar ağacı onayı yok.",
                     evidence=evidence, next_action="Kodu karar ağacından seçip doğrulayın.",
                     legal_basis="Türk Gümrük Tarife Cetveli (İthalat Rejimi Kararı eki)")
        elif model_candidates:
            self.add("candidate_gtip", "Aday GTİP", "pending",
                     f"Kanıta dayalı {len(model_candidates)} aday üretildi ({', '.join(model_candidates[:3])}); seçim yapılmadı.",
                     evidence=evidence, next_action="Adaylardan birini karar ağacında seçin; kod forma otomatik yazılmaz.",
                     legal_basis="Türk Gümrük Tarife Cetveli (İthalat Rejimi Kararı eki)")
        elif not description:
            self.add("candidate_gtip", "Aday GTİP", "blocked", "Eşya tanımı olmadan aday kod üretilemez.",
                     evidence=evidence, next_action="Önce eşya tanımını tamamlayın.")
        else:
            self.add("candidate_gtip", "Aday GTİP", "pending", "Aday HS6/CN8 kodu belirlenmedi.", evidence=evidence,
                     next_action="Ürün sınıflandırmasını çalıştırıp karar ağacında aday seçin.",
                     legal_basis="Türk Gümrük Tarife Cetveli (İthalat Rejimi Kararı eki)")

        # 4. Ağaçta 12 hane
        evidence = ["inquiry.exact_gtip_confirmed", "tariff_lookup.status", "tariff_lookup.matched_gtip_count"]
        tariff_status = self.tariff.get("status") if self.tariff else None
        if not self.gtip:
            self.add("gtip12_selection", "Ağaçta 12 haneli GTİP", "blocked", "Aday kod yok.", evidence=evidence,
                     next_action="Önce aday GTİP seçin.")
        elif tariff_status == "not_found":
            self.add("gtip12_selection", "Ağaçta 12 haneli GTİP", "blocked",
                     "Kod aktif Türk tarife cetvelinde bulunamadı.", evidence=evidence,
                     next_action="Kodu karar ağacında yeniden seçin.", legal_basis="Türk Gümrük Tarife Cetveli")
        elif self.gtip12_confirmed:
            self.add("gtip12_selection", "Ağaçta 12 haneli GTİP", "done",
                     f"12 haneli satır tek eşleşmeyle doğrulandı: {self.gtip}.", evidence=evidence,
                     legal_basis="Türk Gümrük Tarife Cetveli")
        else:
            count = self.tariff.get("matched_gtip_count") if self.tariff else None
            detail = f" Ön ek {count} alt satırla eşleşiyor." if count and count > 1 else ""
            self.add("gtip12_selection", "Ağaçta 12 haneli GTİP", "pending",
                     f"{len(self.gtip)} haneli kod var; 12 haneli alt satır onayı yok.{detail}", evidence=evidence,
                     next_action="Karar ağacında 12 haneli satırı seçip 'kesin alt GTİP' onayı verin.",
                     legal_basis="Türk Gümrük Tarife Cetveli")


    def build(self) -> list[WorkflowStep]:
        inq = self.inquiry
        missing = [_text(item) for item in self.result.get("missing_information") or []]
        self._opening_steps()

        # 5. Menşe / sevk ülkesi
        dispatch = _text(inq.get("dispatch_country"))
        recognised = bool(self.origin) and (
            (self.tariff.get("origin_recognised", True) if self.tariff else True)
            and (self.origin_docs.get("origin_recognised", True) if self.origin_docs else True)
        )
        evidence = ["inquiry.origin_country", "inquiry.dispatch_country", "tariff_lookup.origin_recognised"]
        if not self.origin:
            self.add("origin_dispatch", "Menşe ve sevk ülkesi", "blocked", "Menşe ülke girilmedi.", evidence=evidence,
                     next_action="Menşe ülkeyi (ve farklıysa sevk ülkesini) girin.",
                     legal_basis="Gümrük Kanunu md. 17-22 (menşe)")
        elif not recognised:
            self.add("origin_dispatch", "Menşe ve sevk ülkesi", "pending",
                     f"'{self.origin}' ülke kayıt defterinde tanınmadı.", evidence=evidence,
                     next_action="Ülke adını Türkçe resmî adıyla yazın (ör. Almanya, Çin).",
                     legal_basis="Gümrük Kanunu md. 17-22 (menşe)")
        else:
            self.add("origin_dispatch", "Menşe ve sevk ülkesi", "done",
                     f"Menşe: {self.origin}; sevk: {dispatch or 'menşe ile aynı'}.", evidence=evidence,
                     legal_basis="Gümrük Kanunu md. 17-22 (menşe)")

        # 6. A.TR dolaşım belgesi
        atr_flag = inq.get("atr_certificate")
        evidence = ["inquiry.atr_certificate", "tariff_lookup.atr_available", "origin_documents.route", "origin_documents.documents[]"]
        # tariff_engine flags A.TR only for third-country goods dispatched from the EU; EU-origin
        # industrial goods carry the A.TR through the origin rule table instead.
        atr_possible = bool(self.tariff.get("atr_available")) or any(
            _as_dict(doc).get("code") == "ATR" for doc in self.origin_docs.get("documents") or []
        )
        if not self.origin:
            self.add("atr_certificate", "A.TR dolaşım belgesi", "blocked", "Menşe/sevk ülkesi olmadan değerlendirilemez.",
                     evidence=evidence, next_action="Menşe ve sevk ülkesini girin.")
        elif not self.origin_docs and not self.tariff:
            self.add("atr_certificate", "A.TR dolaşım belgesi", "pending", "Menşe belgesi kuralı bu sonuçta hesaplanmadı.",
                     evidence=evidence, next_action="GTİP ve menşe ile yeniden değerlendirin.")
        elif not atr_possible:
            self.add("atr_certificate", "A.TR dolaşım belgesi", "not_applicable",
                     "Bu rota/tarife satırı için A.TR düzenlenmez (AB dışı sevk veya tarım/AKÇT ürünü).",
                     evidence=evidence, legal_basis="1/95 sayılı Ortaklık Konseyi Kararı; 2006/10895 sayılı Karar")
        elif atr_flag is True:
            self.add("atr_certificate", "A.TR dolaşım belgesi", "done",
                     "A.TR ibraz edilecek; gümrük vergisi AB sütunundan değerlendirildi.", evidence=evidence,
                     legal_basis="1/95 sayılı Ortaklık Konseyi Kararı; 2006/10895 sayılı Karar")
        elif atr_flag is False:
            self.add("atr_certificate", "A.TR dolaşım belgesi", "done",
                     "A.TR ibraz edilmeyecek; gümrük vergisi üçüncü ülke sütunundan değerlendirildi.", evidence=evidence,
                     legal_basis="1/95 sayılı Ortaklık Konseyi Kararı; 2006/10895 sayılı Karar")
        else:
            self.add("atr_certificate", "A.TR dolaşım belgesi", "pending",
                     "A.TR rotası uygun; belgenin ibraz edilip edilmeyeceği işaretlenmedi.", evidence=evidence,
                     next_action="A.TR ibraz edilecekse formda işaretleyin; aksi hâlde üçüncü ülke sütunu uygulanır.",
                     legal_basis="1/95 sayılı Ortaklık Konseyi Kararı; 2006/10895 sayılı Karar")

        # 7. EUR.1 / menşe beyanı
        docs = [_as_dict(doc) for doc in self.origin_docs.get("documents") or []]
        pref_docs = [doc for doc in docs if doc.get("code") in _PREFERENTIAL_DOCUMENT_CODES]
        regime = _text(self.origin_docs.get("regime"))
        evidence = ["origin_documents.regime", "origin_documents.route", "origin_documents.documents[]"]
        if not self.origin:
            self.add("preferential_origin_proof", "EUR.1 / menşe beyanı", "blocked", "Menşe ülke girilmedi.",
                     evidence=evidence, next_action="Menşe ülkeyi girin.")
        elif not self.origin_docs:
            self.add("preferential_origin_proof", "EUR.1 / menşe beyanı", "pending",
                     "Menşe belgesi kuralı bu sonuçta hesaplanmadı.", evidence=evidence,
                     next_action="GTİP ve menşe ile yeniden değerlendirin.")
        elif self.atr_route and regime == "customs_union":
            self.add("preferential_origin_proof", "EUR.1 / menşe beyanı", "not_applicable",
                     "AB menşeli sanayi ürünü A.TR ile geliyor; EUR.1 veya menşe beyanı gerekmez.", evidence=evidence,
                     legal_basis="1/95 sayılı Ortaklık Konseyi Kararı")
        elif pref_docs:
            names = "; ".join(_text(doc.get("name")) for doc in pref_docs[:3])
            self.add("preferential_origin_proof", "EUR.1 / menşe beyanı", "pending",
                     f"Tercihli rejim ({_text(self.origin_docs.get('regime_name'))}) için belge: {names}.", evidence=evidence,
                     next_action="Belgeyi ihracatçıdan temin edin; belge yoksa tercihsiz oran uygulanır.",
                     legal_basis=_text(self.origin_docs.get("regime_name")) or None)
        elif regime in {"mfn", "kktc"} or not docs:
            self.add("preferential_origin_proof", "EUR.1 / menşe beyanı", "not_applicable",
                     "Tercihli menşe belgesi öngörülmüyor (tercihsiz rejim); menşe şahadetnamesi belge listesinde.",
                     evidence=evidence)
        else:
            self.add("preferential_origin_proof", "EUR.1 / menşe beyanı", "pending",
                     "Menşe belgesi türü kural tablosundan kesinleşmedi.", evidence=evidence,
                     next_action="Menşe belgesi rejimini gümrük müşaviriyle teyit edin.")

        # 8. Kıymet ve Incoterm
        invoice = inq.get("invoice_value")
        freight, insurance, incoterm = inq.get("freight"), inq.get("insurance"), _text(inq.get("incoterm"))
        evidence = ["inquiry.invoice_value", "inquiry.freight", "inquiry.insurance", "inquiry.incoterm",
                    "deterministic_cost.customs_value_estimate"]
        if invoice is None:
            self.add("customs_value", "Kıymet ve Incoterm", "blocked", "Fatura bedeli girilmedi; gümrük kıymeti hesaplanamaz.",
                     evidence=evidence, next_action="Fatura bedelini, navlun/sigortayı ve teslim şeklini girin.",
                     legal_basis="Gümrük Kanunu md. 23-31 (kıymet)")
        elif freight is None or insurance is None or not incoterm:
            gaps = [name for name, value in (("navlun", freight), ("sigorta", insurance), ("Incoterm", incoterm or None)) if value is None]
            self.add("customs_value", "Kıymet ve Incoterm", "pending",
                     f"Fatura {invoice:g} {_text(inq.get('currency')) or ''}; eksik: {', '.join(gaps)}.", evidence=evidence,
                     next_action=f"{', '.join(gaps)} bilgisini tamamlayın; CIF kıymet buna göre kesinleşir.",
                     legal_basis="Gümrük Kanunu md. 23-31 (kıymet)")
        else:
            estimate = self.cost.get("customs_value_estimate")
            self.add("customs_value", "Kıymet ve Incoterm", "done",
                     f"Gümrük kıymeti tahmini {estimate:g} {_text(inq.get('currency'))} ({incoterm})." if estimate is not None
                     else f"Fatura, navlun, sigorta ve Incoterm ({incoterm}) girildi.",
                     evidence=evidence, legal_basis="Gümrük Kanunu md. 23-31 (kıymet)")

        # 9. Kur / tescil tarihi
        currency = _text(inq.get("currency")).upper()
        rate, rate_date, as_of_date = inq.get("exchange_rate"), _text(inq.get("exchange_rate_date")), _text(inq.get("as_of_date"))
        evidence = ["inquiry.exchange_rate", "inquiry.exchange_rate_date", "inquiry.as_of_date", "deterministic_cost.try_summary"]
        if currency == "TRY":
            self.add("exchange_rate", "Kur ve tescil tarihi", "not_applicable", "Fatura TL; kur dönüşümü gerekmez.",
                     evidence=evidence)
        elif invoice is None:
            self.add("exchange_rate", "Kur ve tescil tarihi", "blocked", "Kıymet olmadan TL karşılığı hesaplanamaz.",
                     evidence=evidence, next_action="Önce fatura bedelini girin.")
        elif rate is not None and rate_date:
            self.add("exchange_rate", "Kur ve tescil tarihi", "done",
                     f"{currency} kuru {rate:g} ({rate_date}) ile TL karşılığı hesaplandı.", evidence=evidence,
                     legal_basis="Gümrük Kanunu md. 30 (kıymetin TL'ye çevrilmesi)")
        else:
            self.add("exchange_rate", "Kur ve tescil tarihi", "pending",
                     "Tescil günü döviz alış kuru girilmedi." + (f" Hedef tarih: {as_of_date}." if as_of_date else ""),
                     evidence=evidence, next_action="Beyanname tescil günündeki TCMB döviz alış kurunu ve tarihini girin.",
                     legal_basis="Gümrük Kanunu md. 30 (kıymetin TL'ye çevrilmesi)")

        # 10-12. GV / İGV / EMY
        self._rate_step("customs_duty", "Gümrük vergisi (GV)", measure_type="customs_duty", user_field="customs_duty_rate",
                        label="gümrük vergisi", legal_basis="İthalat Rejimi Kararı (ekli listeler)")
        self._rate_step("additional_duty", "İlave gümrük vergisi (İGV)", measure_type="additional_duty",
                        user_field="additional_duty_rate", label="İGV", legal_basis="İlave Gümrük Vergisi Kararları",
                        zero_when_absent=True)
        self._rate_step("financial_liability", "Ek mali yükümlülük (EMY)", measure_type="additional_financial_liability",
                        user_field="additional_financial_liability_rate", label="ek mali yükümlülük",
                        legal_basis="İthalat Rejimi Kararı IV sayılı liste ve EMY Kararları")

        # 13. Damping / sübvansiyon
        self._trade_step(
            "anti_dumping", "Damping / sübvansiyon önlemi", kinds=("anti_dumping",),
            user_field="anti_dumping_amount", label="damping/sübvansiyon",
            legal_basis="İthalatta Haksız Rekabetin Önlenmesi Hakkında Mevzuat",
            confirm_action="Tebliğdeki firma bazlı oran/tutarı doğrulayıp damping tutarını girin.",
        )

        # 14. Korunma / kota
        self._trade_step(
            "safeguard_quota", "Korunma önlemi / tarım ürünleri tarife kontenjanı", kinds=("safeguard", "tariff_quota"),
            user_field=None, label="korunma önlemi/tarım kontenjanı",
            legal_basis="İthalatta Korunma Önlemleri ve Tarife Kontenjanı Kararları",
            confirm_action="Ek mali yükümlülük oranını ve kontenjan tahsis/bakiye durumunu tescil gününde doğrulayın. Sanayi ürünleri tarife kontenjanları indekslenmemiştir (kararlar makine okunamayan Resmî Gazete PDF'i olarak yayımlanıyor); sanayi ürünlerinde kontenjan açılıp açılmadığını resmî sayfadan ayrıca doğrulayın.",
        )

        # 15. Gözetim
        surveillance = _live_hits(self.trade.get("surveillance")) if self.trade else []
        evidence = ["tariff_lookup.trade_measures.surveillance[]", "inquiry.surveillance_unit_value",
                    "inquiry.has_surveillance_certificate", "tariff_lookup.measure_coverage.surveillance"]
        if not self.gtip:
            self.add("surveillance", "Gözetim uygulaması", "blocked", "GTİP olmadan gözetim listesi taranamaz.",
                     evidence=evidence, next_action="Aday GTİP seçin.")
        elif not self.trade:
            self.add("surveillance", "Gözetim uygulaması", "pending", "Gözetim listesi bu sonuçta taranmadı.",
                     evidence=evidence, next_action="Gözetim tebliğlerini GTİP ile kontrol edin.",
                     legal_basis="İthalatta Gözetim Uygulanması Hakkında Karar")
        elif not surveillance:
            self.add("surveillance", "Gözetim uygulaması", "not_applicable",
                     "Resmî gözetim listesinde bu GTİP için yürürlükte satır yok.", evidence=evidence,
                     legal_basis="İthalatta Gözetim Uygulanması Hakkında Karar")
        elif inq.get("has_surveillance_certificate") is not None or inq.get("surveillance_unit_value") is not None:
            first = surveillance[0]
            self.add("surveillance", "Gözetim uygulaması", "done",
                     f"Gözetim satırı {_text(first.get('matched_code'))} ({_text(first.get('rate_text'))} {_text(first.get('unit'))}); "
                     "belge/kıymet durumu girildi.", evidence=evidence,
                     legal_basis="İthalatta Gözetim Uygulanması Hakkında Karar")
        else:
            first = surveillance[0]
            self.add("surveillance", "Gözetim uygulaması", "pending",
                     f"Gözetim satırı eşleşti: {_text(first.get('matched_code'))} birim kıymet {_text(first.get('rate_text'))} "
                     f"{_text(first.get('unit'))} [{_text(first.get('legal_act'))}].", evidence=evidence,
                     next_action="Gözetim belgesi olup olmadığını işaretleyin; belge yoksa kıymet eşiğe yükseltilir.",
                     legal_basis="İthalatta Gözetim Uygulanması Hakkında Karar")

        # 16. ÖTV
        sct_amount = inq.get("sct_amount")
        evidence = ["tariff_lookup.excise_tax.in_scope", "tariff_lookup.excise_tax.matches[]", "inquiry.sct_amount"]
        excise_matches = self.excise.get("matches") or []
        in_scope = bool(self.excise.get("in_scope")) or bool(excise_matches)
        if sct_amount is not None:
            self.add("sct", "Özel tüketim vergisi (ÖTV)", "done",
                     f"ÖTV tutarı kullanıcı tarafından {sct_amount:g} olarak girildi.", evidence=evidence,
                     legal_basis="4760 sayılı ÖTV Kanunu")
        elif not self.gtip:
            self.add("sct", "Özel tüketim vergisi (ÖTV)", "blocked", "GTİP olmadan ÖTV listesi taranamaz.",
                     evidence=evidence, next_action="Aday GTİP seçin.")
        elif not self.excise:
            self.add("sct", "Özel tüketim vergisi (ÖTV)", "pending", "ÖTV listeleri bu sonuçta taranmadı.",
                     evidence=evidence, next_action="ÖTV (I)-(IV) sayılı listeleri GTİP ile kontrol edin.",
                     legal_basis="4760 sayılı ÖTV Kanunu")
        elif in_scope:
            first = _as_dict(excise_matches[0]) if excise_matches else {}
            self.add("sct", "Özel tüketim vergisi (ÖTV)", "pending",
                     f"Eşya ÖTV kapsamında: {_text(first.get('list_label')) or 'liste'} {_text(first.get('matched_code'))}.",
                     evidence=evidence, next_action="Oran/asgari maktu tutarı Kanun ekinden doğrulayıp ÖTV tutarını girin.",
                     legal_basis="4760 sayılı ÖTV Kanunu")
        elif self.excise.get("warnings"):
            self.add("sct", "Özel tüketim vergisi (ÖTV)", "pending",
                     "GTİP listede yok; aynı pozisyonda ÖTV satırları var (kod revizyonu olabilir).", evidence=evidence,
                     next_action="Eşyanın ÖTV listesindeki karşılığını doğrulayın.", legal_basis="4760 sayılı ÖTV Kanunu")
        else:
            self.add("sct", "Özel tüketim vergisi (ÖTV)", "not_applicable", "ÖTV listelerinde eşleşme yok.",
                     evidence=evidence, legal_basis="4760 sayılı ÖTV Kanunu")

        # 17. KDV
        vat_rate = inq.get("vat_rate")
        evidence = ["inquiry.vat_rate", "tariff_lookup.measure_coverage.vat"]
        if vat_rate is not None:
            self.add("vat", "Katma değer vergisi (KDV)", "done", f"KDV oranı {_fmt_rate(vat_rate)} olarak girildi.",
                     evidence=evidence, legal_basis="3065 sayılı KDV Kanunu ve oran kararnamesi")
        else:
            self.add("vat", "Katma değer vergisi (KDV)", "pending", "Ürüne özgü KDV oranı girilmedi.", evidence=evidence,
                     next_action="KDV oranını (I)/(II) sayılı listelerden doğrulayıp girin.",
                     legal_basis="3065 sayılı KDV Kanunu ve oran kararnamesi")

        # 18. KKDF
        payment = _text(inq.get("payment_method"))
        kkdf_rate = inq.get("kkdf_rate")
        kkdf_line = _cost_line(self.cost, "kkdf") if self.cost else None
        evidence = ["inquiry.payment_method", "inquiry.kkdf_rate", "deterministic_cost.lines[kkdf]"]
        if kkdf_rate is not None:
            self.add("kkdf", "KKDF", "done", f"KKDF oranı {_fmt_rate(kkdf_rate)} olarak doğrulandı (ödeme: {payment or 'belirtilmedi'}).",
                     evidence=evidence, legal_basis="88/12944 sayılı KKDF Kararı")
        elif not payment:
            self.add("kkdf", "KKDF", "pending", "Ödeme şekli girilmedi; KKDF belirlenemez.", evidence=evidence,
                     next_action="Ödeme şeklini girin (peşin %0, mal mukabili/vadeli %6).",
                     legal_basis="88/12944 sayılı KKDF Kararı")
        elif kkdf_line and kkdf_line.get("amount") is not None:
            self.add("kkdf", "KKDF", "done",
                     f"Ödeme şekli '{payment}' için KKDF {_fmt_rate(kkdf_line.get('rate'))} önerildi ve hesaba alındı.",
                     evidence=evidence, legal_basis="88/12944 sayılı KKDF Kararı")
        else:
            self.add("kkdf", "KKDF", "pending", f"Ödeme şekli '{payment}' için KKDF oranı doğrulanmadı.", evidence=evidence,
                     next_action="KKDF oranını doğrulayıp girin (peşin %0, kredili/vadeli %6).",
                     legal_basis="88/12944 sayılı KKDF Kararı")

        # 19-21. Kontroller
        matches = [_as_dict(item) for item in self.control.get("matches") or []] if self.control else []
        control_status = self.control.get("status") if self.control else None
        tareks_matches = [m for m in matches if _rule_is_tareks(_as_dict(m.get("rule")))]
        prohibited = [m for m in matches if _as_dict(m.get("matched_scope")).get("list_kind") == "prohibited"]
        other_agency = [m for m in matches if m not in tareks_matches and m not in prohibited]

        def control_block(step_id: str, title: str) -> bool:
            if not self.gtip12_confirmed:
                self.add(step_id, title, "blocked", "Kontrol taraması 12 haneli kesin GTİP onayı gerektirir.",
                         evidence=["control_lookup.status", "inquiry.exact_gtip_confirmed"],
                         next_action="12 haneli GTİP'i onaylayın; tebliğ Ek-1 taraması sonra çalışır.")
                return True
            if not self.control or control_status == "unavailable":
                self.add(step_id, title, "pending", "Kontrol tebliği endeksi bu sonuçta kullanılamadı.",
                         evidence=["control_lookup.status"], next_action="Ürün Güvenliği ve Denetimi tebliğlerini elle kontrol edin.")
                return True
            return False

        def rule_names(items: list[dict[str, Any]]) -> str:
            return "; ".join(f"{_text(_as_dict(m.get('rule')).get('code'))} {_text(_as_dict(m.get('rule')).get('title'))[:50]}" for m in items[:3])

        if not control_block("tareks", "TAREKS / ÜGD kapsamı"):
            evidence = ["control_lookup.matches[].rule.code", "control_lookup.matches[].matched_scope.gtip_prefix", "control_lookup.scope_determination"]
            if tareks_matches:
                self.add("tareks", "TAREKS / ÜGD kapsamı", "pending",
                         f"Ek-1 eşleşmesi: {rule_names(tareks_matches)}. Fiilî denetim risk analizine bağlıdır.",
                         evidence=evidence, next_action="Ürün tanımı, istisnalar ve TAREKS başvuru gereğini değerlendirin.",
                         legal_basis="Ürün Güvenliği ve Denetimi Tebliğleri")
            else:
                self.add("tareks", "TAREKS / ÜGD kapsamı", "not_applicable",
                         "Endekslenmiş ÜGD tebliğlerinin Ek-1 listelerinde eşleşme yok; ürün tanımı yine gözden geçirilmeli.",
                         evidence=evidence, legal_basis="Ürün Güvenliği ve Denetimi Tebliğleri")

        if not control_block("prohibited_lists", "Yasak / izin listeleri"):
            evidence = ["control_lookup.matches[].matched_scope.list_kind"]
            if prohibited:
                self.add("prohibited_lists", "Yasak / izin listeleri", "blocked",
                         f"Yasak liste eşleşmesi: {rule_names(prohibited)}.", evidence=evidence,
                         next_action="İthalat yasağı veya istisna kapsamını yetkili kurumla teyit etmeden ilerlemeyin.",
                         legal_basis="İlgili ÜGD tebliğinin yasak eşya listesi")
            else:
                self.add("prohibited_lists", "Yasak / izin listeleri", "not_applicable",
                         "Endekslenmiş yasak eşya listelerinde eşleşme yok.", evidence=evidence)

        if not control_block("other_agency_permits", "Diğer kurum izinleri"):
            evidence = ["control_lookup.matches[].rule.authority", "control_lookup.matches[].rule.system"]
            if other_agency:
                authorities = ", ".join(dict.fromkeys(_text(_as_dict(m.get("rule")).get("authority")) or "yetkili kurum" for m in other_agency))
                self.add("other_agency_permits", "Diğer kurum izinleri", "pending",
                         f"Kurum izni/uygunluk kapsamı: {rule_names(other_agency)} ({authorities}).", evidence=evidence,
                         next_action="İlgili kurumdan izin/uygunluk yazısı gereğini teyit edin.",
                         legal_basis="İlgili kurum tebliği")
            else:
                self.add("other_agency_permits", "Diğer kurum izinleri", "not_applicable",
                         "TAREKS dışı kurum izni gerektiren endekslenmiş tebliğ eşleşmesi yok.", evidence=evidence)

        # 22. Belge kontrol listesi
        general = [_text(item) for item in self.origin_docs.get("general_documents") or []]
        origin_names = [_text(doc.get("name")) for doc in docs]
        rule_docs: list[str] = []
        for match in matches:
            for doc in _as_dict(match.get("rule")).get("required_documents") or []:
                rule_docs.append(_text(_as_dict(doc).get("text")))
        listed = [item for item in [*general, *origin_names, *rule_docs] if item]
        evidence = ["origin_documents.general_documents", "origin_documents.documents[]", "control_lookup.matches[].rule.required_documents",
                    "required_documents[]"]
        if not listed:
            self.add("document_checklist", "Belge kontrol listesi", "pending", "Belge listesi henüz oluşturulmadı.",
                     evidence=evidence, next_action="Menşe ve GTİP girildiğinde belge listesi oluşur.")
        else:
            self.add("document_checklist", "Belge kontrol listesi", "pending",
                     f"{len(listed)} belge bekleniyor: {'; '.join(listed[:4])}{'; …' if len(listed) > 4 else ''}",
                     evidence=evidence, next_action="Belgeleri temin edip beyanname ekine hazırlayın.",
                     legal_basis="Gümrük Yönetmeliği md. 114 (beyannameye eklenecek belgeler)")

        # 23. Beyanname öncesi ödemeler
        stamp, port, gekap, trt = inq.get("stamp_duty_try"), inq.get("port_storage_try"), inq.get("gekap_try"), inq.get("trt_bandrol_rate")
        evidence = ["inquiry.stamp_duty_try", "inquiry.port_storage_try", "inquiry.gekap_try", "inquiry.trt_bandrol_rate",
                    "deterministic_cost.try_summary"]
        gaps = [name for name, value in (("damga vergisi", stamp), ("ardiye/liman", port), ("GEKAP", gekap), ("TRT bandrolü", trt))
                if value is None]
        if invoice is None:
            self.add("pre_declaration_payments", "Beyanname öncesi ödemeler", "blocked", "Kıymet olmadan TL kalemleri hesaplanamaz.",
                     evidence=evidence, next_action="Önce fatura bedelini girin.")
        elif stamp is not None and port is not None:
            self.add("pre_declaration_payments", "Beyanname öncesi ödemeler", "done",
                     "Damga vergisi ve ardiye girildi." + (f" Girilmeyen isteğe bağlı kalemler: {', '.join(gaps)}." if gaps else ""),
                     evidence=evidence, legal_basis="488 sayılı Damga Vergisi Kanunu; 3093 sayılı TRT Kanunu; GEKAP Yönetmeliği")
        else:
            self.add("pre_declaration_payments", "Beyanname öncesi ödemeler", "pending",
                     f"Eksik TL kalemleri: {', '.join(gaps)}.", evidence=evidence,
                     next_action="Damga vergisi, ardiye/liman, GEKAP ve varsa TRT bandrol kalemlerini girin.",
                     legal_basis="488 sayılı Damga Vergisi Kanunu; 3093 sayılı TRT Kanunu; GEKAP Yönetmeliği")

        self._expert_handoff_step()

        # Missing-information list feeds next_action of pending steps that lack one.
        if missing:
            for step in self.steps:
                if step.status == "pending" and not step.next_action:
                    step.next_action = f"Eksik bilgiyi tamamlayın: {missing[0]}"
        return self.steps

    def _expert_handoff_step(self) -> None:
        """Uzman devri / BTB adımı — iki yönde de aynı paketten türetilir."""
        escalation = bool(self.packet.get("escalation_required"))
        risk = _text(self.packet.get("risk_level"))
        review_types = [_text(item) for item in self.packet.get("review_types") or []]
        evidence = ["expert_review_packet.risk_level", "expert_review_packet.escalation_required", "expert_review_packet.review_types"]
        if not self.packet:
            self.add("expert_handoff", "Uzman devri / BTB kararı", "pending", "Uzman inceleme paketi bu sonuçta yok.",
                     evidence=evidence, next_action="Sonucu yeniden üretin veya gümrük müşavirine danışın.")
        elif risk == "critical":
            self.add("expert_handoff", "Uzman devri / BTB kararı", "blocked",
                     f"Kritik risk: {', '.join(review_types) or 'uzman'} teyidi olmadan beyanname hazırlanmamalı.",
                     evidence=evidence, next_action="Dosyayı gümrük müşavirine/BTB başvurusuna devredin.",
                     legal_basis="Gümrük Kanunu md. 9 (Bağlayıcı Tarife Bilgisi)")
        elif escalation:
            self.add("expert_handoff", "Uzman devri / BTB kararı", "pending",
                     f"Uzman devri önerildi ({', '.join(review_types) or 'gümrük müşaviri'}); {len(self.packet.get('reasons') or [])} gerekçe.",
                     evidence=evidence, next_action="Uzman inceleme paketini danışmana gönderin veya BTB başvurusu yapın.",
                     legal_basis="Gümrük Kanunu md. 9 (Bağlayıcı Tarife Bilgisi)")
        else:
            self.add("expert_handoff", "Uzman devri / BTB kararı", "done",
                     "Zorunlu uzman devri gerekçesi bulunmadı; sonuç yine bağlayıcı karar değildir.", evidence=evidence,
                     legal_basis="Gümrük Kanunu md. 9 (Bağlayıcı Tarife Bilgisi)")


    def _trade_step(
        self,
        step_id: str,
        title: str,
        *,
        kinds: tuple[str, ...],
        user_field: str | None,
        label: str,
        legal_basis: str,
        confirm_action: str,
    ) -> None:
        evidence = [f"tariff_lookup.trade_measures.{kind}[]" for kind in kinds] + [f"tariff_lookup.measure_coverage.{kinds[0]}"]
        if user_field:
            evidence.append(f"inquiry.{user_field}")
        user_value = self.inquiry.get(user_field) if user_field else None
        if not self.gtip:
            self.add(step_id, title, "blocked", f"GTİP olmadan {label} listesi taranamaz.", evidence=evidence,
                     next_action="Aday GTİP seçin.", legal_basis=legal_basis)
            return
        if not self.trade:
            self.add(step_id, title, "pending", f"Resmî {label} listesi bu sonuçta taranmadı.", evidence=evidence,
                     next_action=f"{label.capitalize()} listelerini GTİP ve menşe ile kontrol edin.", legal_basis=legal_basis)
            return
        hits: list[dict[str, Any]] = []
        for kind in kinds:
            hits.extend(_live_hits(self.trade.get(kind)))
        if not hits:
            if user_value is not None and user_value > 0:
                self.add(step_id, title, "done", f"Listede eşleşme yok; kullanıcı {label} tutarını {user_value:g} olarak girdi.",
                         evidence=evidence, legal_basis=legal_basis)
                return
            self.add(step_id, title, "not_applicable", f"Resmî {label} listesinde bu GTİP/menşe için yürürlükte satır yok.",
                     evidence=evidence, legal_basis=legal_basis)
            return
        first = hits[0]
        detail = (
            f"{_text(first.get('country'))} menşeli '{_text(first.get('product'))[:60]}' ({_text(first.get('matched_code'))}): "
            f"{_text(first.get('rate_text'))} [{_text(first.get('legal_act'))}]"
        )
        if user_field and user_value is not None:
            self.add(step_id, title, "done", f"Önlem eşleşti ve tutar doğrulandı ({user_value:g}). {detail}.",
                     evidence=evidence, legal_basis=legal_basis)
            return
        self.add(step_id, title, "pending", f"{len(hits)} önlem satırı eşleşti. {detail}.", evidence=evidence,
                 next_action=confirm_action, legal_basis=legal_basis)


class _ExportBuilder(_Builder):
    """İhracat akışı: Türkiye'den çıkış + hedef ülke ithalat beyannamesi hazırlığı.

    İlk dört adım (eşya tanımı, evsaf onayı, aday GTİP, 12 hane) ``_Builder``'dan
    aynen devralınır. Kalan adımlar Türk ithalat vergilerinin yerine hedef ülke
    şartlarını ve Türkiye tarafı ihracat işlemlerini alır.

    ``exporter_registration``, ``dual_use_control`` ve ``vat_exemption_refund`` her dosyada
    ``pending`` kalır: bu veriler sistemimizde yoktur ve "kapsam dışıdır" demek yanlış olurdu.
    ``export_prohibitions`` ve ``export_product_control`` ise FAZ 8.2'den beri **kısmi** bir
    ihracat kontrol indeksinden beslenir: eşleşme çıkarsa kanıtlı sonuç yazılır, çıkmazsa
    adım yine ``pending`` kalır — indeks kısmi olduğu için eşleşmemek yükümlülük olmadığını
    göstermez. Gözlemleyemediğimiz şeyi tamamlanmış saymak, hedef ülkede yanlış beyannameye
    yol açabilecek en sessiz hatadır.
    """

    @property
    def export_req(self) -> dict[str, Any]:
        return _as_dict(self.result.get("export_requirements"))

    @property
    def destination(self) -> dict[str, Any]:
        return _as_dict(self.export_req.get("destination"))

    def build(self) -> list[WorkflowStep]:
        inq = self.inquiry
        self._opening_steps()

        req = self.export_req
        dest = self.destination
        dest_name = _text(dest.get("country_name")) or _text(inq.get("destination_country"))
        tier = _text(dest.get("tier"))
        badge = _text(dest.get("badge_text"))

        # 5. Hedef ülke
        evidence = ["inquiry.destination_country", "export_requirements.destination.tier"]
        if not dest_name:
            self.add("destination_country", "Hedef ülke", "blocked", "Eşyanın gideceği ülke girilmedi.",
                     evidence=evidence, next_action="Hedef ülkeyi seçin; beyanname şartları ülkeye göre belirlenir.")
        elif not dest.get("recognised"):
            self.add("destination_country", "Hedef ülke", "pending",
                     f'"{dest_name}" kayıtlı ülke listemizde bulunamadı.', evidence=evidence,
                     next_action="Ülke adını kontrol edin; tanınmayan ülkede anlaşma ve belge kuralı üretilemez.")
        else:
            agreement = _text(dest.get("agreement")) or _text(dest.get("regime_name"))
            self.add("destination_country", "Hedef ülke", "done",
                     f"Hedef: {dest_name}{f' · {agreement}' if agreement else ''}.", evidence=evidence)

        # 6. Hedef ülke gümrük vergisi
        evidence = ["export_requirements.destination_duty", "export_requirements.destination.tier"]
        if not self.gtip:
            self.add("destination_tariff", "Hedef ülke gümrük vergisi", "blocked",
                     "Tarife kodu olmadan hedef ülke oranı sorgulanamaz.", evidence=evidence,
                     next_action="Önce aday GTİP'i belirleyin.")
        elif not dest_name:
            self.add("destination_tariff", "Hedef ülke gümrük vergisi", "blocked",
                     "Hedef ülke seçilmeden oran belirlenemez.", evidence=evidence,
                     next_action="Hedef ülkeyi seçin.")
        elif req.get("destination_duty"):
            source = _as_dict(req.get("duty_source"))
            stamp = _text(source.get("retrieved_at")) or _text(source.get("fetched_at"))
            self.add("destination_tariff", "Hedef ülke gümrük vergisi", "done",
                     f"Oran resmî kaynaktan okundu{f' ({stamp})' if stamp else ''}.", evidence=evidence,
                     legal_basis="Hedef ülkenin yürürlükteki tarife cetveli")
        else:
            action = {
                "nomenclature": "Tares ekranından oranı sorgulayın; İsviçre oran yayımlamaz.",
                "none": "Ülke adını düzeltin veya hedef ülkenin resmî tarife ekranını kullanın.",
            }.get(tier, "Hedef ülkenin resmî tarife ekranından oranı doğrulayın.")
            self.add("destination_tariff", "Hedef ülke gümrük vergisi", "pending",
                     badge or "Hedef ülke için oran verimiz yok.", evidence=evidence, next_action=action)

        # 7. Menşe / dolaşım belgesi düzenleme
        documents = [_as_dict(item) for item in req.get("proof_documents") or []]
        evidence = ["export_requirements.proof_documents", "inquiry.origin_country"]
        if not dest_name:
            self.add("preferential_origin_proof_export", "Menşe / dolaşım belgesi düzenleme", "blocked",
                     "Hedef ülke belli olmadan belge türü belirlenemez.", evidence=evidence,
                     next_action="Hedef ülkeyi seçin.")
        elif documents:
            names = ", ".join(_text(doc.get("name")) for doc in documents[:2] if _text(doc.get("name")))
            self.add("preferential_origin_proof_export", "Menşe / dolaşım belgesi düzenleme", "pending",
                     f"Düzenlenecek belge: {names}.", evidence=evidence,
                     next_action="Belgeyi ihracatçı olarak düzenleyip oda vizesi ve gümrük onayını alın.",
                     legal_basis=_text(dest.get("agreement")) or None)
        else:
            self.add("preferential_origin_proof_export", "Menşe / dolaşım belgesi düzenleme", "pending",
                     "Bu hedef için tercihli menşe belgesi kuralı üretilemedi.", evidence=evidence,
                     next_action="Alıcınızın hangi menşe belgesini istediğini teyit edin.")

        # 8-11. Gözlemleyemediğimiz Türkiye tarafı adımlar: asla "not_applicable" olmaz.
        self.add("exporter_registration", "İhracatçı birliği üyeliği ve İBGS kaydı", "pending",
                 "Birlik üyelik verisi sistemimizde yok; gümrük çıkış beyannamesi öncesi gereklidir.",
                 evidence=["export_requirements.turkish_procedure"],
                 next_action="İlgili ihracatçı birliğine üyeliğinizi ve İBGS kaydınızı teyit edin.")

        # FAZ 8.2: ihracat kontrol motoru artık kısmi bir indeks taşıyor. Eşleşme çıkarsa
        # gerçek kanıt yazılır; çıkmazsa adım yine `pending` kalır — indeks kısmi olduğu
        # için "kapsam dışıdır" demek yanlış olurdu. Bu ayrım bilerek korunuyor.
        export_matches = [_as_dict(item) for item in self.control.get("matches") or []] if self.control else []
        export_control_evidence = [
            "control_lookup.direction",
            "control_lookup.matches[].rule.code",
            "control_lookup.matches[].matched_scope.gtip_prefix",
        ]

        def export_rule_names(items: list[dict[str, Any]]) -> str:
            return "; ".join(
                f"{_text(_as_dict(m.get('rule')).get('code'))} {_text(_as_dict(m.get('rule')).get('title'))[:60]}"
                for m in items[:3]
            )

        restricted = [
            m for m in export_matches
            if _as_dict(m.get("matched_scope")).get("list_kind") in {"prohibited", "licence_required"}
        ]
        if restricted:
            self.add("export_prohibitions", "İhracı yasak veya ön izne bağlı mallar", "blocked",
                     f"İhracat kontrol listesi eşleşmesi: {export_rule_names(restricted)}.",
                     evidence=export_control_evidence,
                     next_action="Yükümlülüğün türünü (yasak / ön izin / kota) resmî metinden teyit etmeden ilerlemeyin.",
                     legal_basis="İhracat Yönetmeliği ve ekli listeler")
        else:
            self.add("export_prohibitions", "İhracı yasak veya ön izne bağlı mallar", "pending",
                     "İhracat kontrol indeksimiz kısmidir; eşleşme çıkmaması kapsam dışı olduğunu göstermez.",
                     evidence=["export_requirements.turkish_procedure", *export_control_evidence],
                     next_action="Eşyanızı resmî yasak/ön izin listesiyle karşılaştırın.",
                     legal_basis="İhracat Yönetmeliği ve ekli listeler")

        evidence = ["inquiry.candidate_gtip", "export_requirements.turkish_procedure"]
        if not self.gtip:
            self.add("export_product_control", "İhracatta ürün güvenliği / TAREKS denetimi", "blocked",
                     "Tarife kodu olmadan denetim kapsamı belirlenemez.", evidence=evidence,
                     next_action="Önce aday GTİP'i belirleyin.")
        elif export_matches:
            self.add("export_product_control", "İhracatta ürün güvenliği / TAREKS denetimi", "pending",
                     f"İndekslenen ihracat tebliğinde eşleşme: {export_rule_names(export_matches)}. "
                     "Fiilî denetim yetkili kurumun değerlendirmesine bağlıdır.",
                     evidence=[*evidence, *export_control_evidence],
                     next_action="Ürün tanımını ve istisnaları tebliğ metninden doğrulayın.")
        else:
            self.add("export_product_control", "İhracatta ürün güvenliği / TAREKS denetimi", "pending",
                     "İhracat ürün denetim indeksimiz kısmidir; ürün grubunuz denetime tabi olabilir.",
                     evidence=evidence, next_action="Ürün grubunuz için ihracat denetim tebliğlerini kontrol edin.")

        if not self.gtip:
            self.add("dual_use_control", "İkili kullanım ve ihracat kontrol listeleri", "blocked",
                     "Tarife kodu olmadan kontrol listesi karşılaştırması yapılamaz.", evidence=evidence,
                     next_action="Önce aday GTİP'i belirleyin.")
        else:
            # İkili kullanım ve yaptırım listeleri askerî/teknik kategoriye göre düzenlenir,
            # GTİP'e göre değil; bu yüzden indekslenmemiştir ve bu adım hep `pending` kalır.
            self.add("dual_use_control", "İkili kullanım ve ihracat kontrol listeleri", "pending",
                     "İkili kullanım ve yaptırım listeleri GTİP'e göre değil teknik kategoriye göre "
                     "düzenlendiğinden indekslenmemiştir.",
                     evidence=evidence,
                     next_action="Teknik özellikleri resmî ikili kullanım listesiyle karşılaştırın.")

        # 12. Hedef pazar uygunluk işareti
        hints = [_as_dict(item) for item in req.get("market_hints") or []]
        evidence = ["export_requirements.market_hints"]
        if not dest_name:
            self.add("destination_market_requirements", "Hedef pazar uygunluk işareti", "blocked",
                     "Hedef ülke belli olmadan uygunluk şartı belirlenemez.", evidence=evidence,
                     next_action="Hedef ülkeyi seçin.")
        elif hints:
            titles = ", ".join(_text(hint.get("title")) for hint in hints[:3] if _text(hint.get("title")))
            self.add("destination_market_requirements", "Hedef pazar uygunluk işareti", "pending",
                     f"Onayladığınız evsaftan çıkan başlıklar: {titles}.", evidence=evidence,
                     next_action="Her başlık için hedef pazarın uygunluk ve etiketleme şartını doğrulayın.")
        else:
            self.add("destination_market_requirements", "Hedef pazar uygunluk işareti", "pending",
                     "Onaylanmış evsaftan uygunluk ipucu çıkmadı; bu kapsam dışı olduğu anlamına gelmez.",
                     evidence=evidence, next_action="Ürün grubunuz için hedef pazar uygunluk şartını kontrol edin.")

        # 13. Ticari ve taşıma belgeleri
        self.add("commercial_documents", "Ticari ve taşıma belgeleri", "pending",
                 "Fatura, taşıma belgesi, çeki listesi ve gerekiyorsa sigorta poliçesi hazırlanır.",
                 evidence=["export_requirements.commercial_documents"],
                 next_action="Belgelerdeki eşya tanımı, miktar ve kıymetin beyanname ile birebir uyduğunu doğrulayın.")

        # 14. Teslim ve ödeme şekli
        incoterm = _text(inq.get("incoterm"))
        payment = _text(inq.get("payment_method"))
        evidence = ["inquiry.incoterm", "inquiry.payment_method"]
        if incoterm and payment:
            self.add("incoterm_payment", "Teslim ve ödeme şekli", "done",
                     f"Teslim {incoterm}, ödeme {payment}.", evidence=evidence)
        else:
            eksik = " ve ".join(filter(None, ["teslim şekli" if not incoterm else "", "ödeme şekli" if not payment else ""]))
            self.add("incoterm_payment", "Teslim ve ödeme şekli", "pending", f"Eksik: {eksik}.", evidence=evidence,
                     next_action="Sözleşmedeki teslim ve ödeme şeklini girin; beyanname ile uyumlu olmalıdır.")

        # 15. Navlun ve sigorta sorumluluğu
        freight = inq.get("freight")
        insurance = inq.get("insurance")
        evidence = ["inquiry.freight", "inquiry.insurance", "inquiry.incoterm"]
        if freight is not None and insurance is not None:
            self.add("freight_insurance", "Navlun ve sigorta sorumluluğu", "done",
                     "Navlun ve sigorta tutarları girildi.", evidence=evidence)
        else:
            self.add("freight_insurance", "Navlun ve sigorta sorumluluğu", "pending",
                     "Navlun veya sigorta tutarı girilmedi.", evidence=evidence,
                     next_action=f"Teslim şekli{f' ({incoterm})' if incoterm else ''} hangi tarafa yüklüyorsa ona göre girin.")

        # 16. Gümrük çıkış beyannamesi
        evidence = ["inquiry.exact_gtip_confirmed", "tariff_lookup.status"]
        if not self.gtip12_confirmed:
            self.add("export_declaration", "Gümrük çıkış beyannamesi (GÇB) tescili", "blocked",
                     "GÇB 12 haneli GTİP ister; kesin alt kod onaylanmadı.", evidence=evidence,
                     next_action="Karar ağacında 12 haneli satırı seçip onaylayın.",
                     legal_basis="Gümrük Yönetmeliği (beyannamenin tescili)")
        else:
            self.add("export_declaration", "Gümrük çıkış beyannamesi (GÇB) tescili", "pending",
                     f"12 haneli kod hazır: {self.gtip}.", evidence=evidence,
                     next_action="Beyannameyi müşaviriniz aracılığıyla tescil ettirin.",
                     legal_basis="Gümrük Yönetmeliği (beyannamenin tescili)")

        # 17. KDV istisnası ve iade
        self.add("vat_exemption_refund", "İhracat KDV istisnası ve iade", "pending",
                 "İhracat teslimleri KDV'den istisnadır; iade süreci sistemimizde izlenmez.",
                 evidence=["export_requirements.turkish_procedure"],
                 next_action="Beyannamenin kapanmasını ve iade belge şartlarını mali müşavirinizle takip edin.",
                 legal_basis="3065 sayılı KDV Kanunu md. 11/1-a ve md. 12")

        # 18. Uzman devri
        self._expert_handoff_step()
        return self.steps


def _direction_of(data: dict[str, Any]) -> str:
    """Yön üst düzeyde ya da inquiry içinde olabilir; göç öncesi dosyalarda hiç yoktur."""
    direction = _text(data.get("direction"))
    if not direction:
        direction = _text(_as_dict(data.get("inquiry")).get("direction"))
    return direction or "import"


def workflow_version_for(direction: Any) -> str:
    return EXPORT_WORKFLOW_VERSION if _text(direction) == "export" else WORKFLOW_VERSION


def build_workflow(result: Any) -> list[WorkflowStep]:
    """Derive the ordered workflow from a ``CustomsPrecheckResult`` or its dict form.

    Deterministic: same input → same steps.  Never calls a model or the network.
    Yön ``export`` ise ihracat kurucusu çalışır; varsayılan ve eski kayıtlar ithalattır.
    """
    data = _as_dict(result)
    builder = _ExportBuilder if _direction_of(data) == "export" else _Builder
    return builder(data).build()


def workflow_summary(
    steps: list[WorkflowStep] | list[dict[str, Any]], *, direction: Any = "import"
) -> dict[str, Any]:
    counts = {"done": 0, "pending": 0, "blocked": 0, "not_applicable": 0}
    for step in steps:
        status = _text(_as_dict(step).get("status"))
        if status in counts:
            counts[status] += 1
    applicable = len(steps) - counts["not_applicable"]
    ratio = round(counts["done"] / applicable, 3) if applicable > 0 else 1.0
    return {
        **counts,
        "total": len(steps),
        "completion_ratio": ratio,
        "version": workflow_version_for(direction),
    }


__all__ = [
    "EXPORT_WORKFLOW_VERSION",
    "WORKFLOW_VERSION",
    "WorkflowStep",
    "build_workflow",
    "workflow_summary",
    "workflow_version_for",
]
