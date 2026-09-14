"""Deterministic interactive decision questions (PRD Faz 2.3).

Turns one tariff lookup (plus the VAT list suggestion, the official trade-measure
report and the inquiry the user already filled in) into a short list of concrete
questions whose answers the user — never the model, never this module — supplies.

Rules that hold everywhere in this file:

* No model call, no network, no guessing: every question is derived from fields
  that already exist on the lookup / VAT report / inquiry.
* A question is produced only when the corresponding field is still unanswered.
  Answered fields (``vat_rate``, ``payment_method``, ``atr_certificate`` …) are
  silent.
* ``apply_decision_answers`` writes a rate into the cost input **only** because
  the user picked that option; it never fills a field on its own, never
  overwrites a value the user already typed, and ignores unknown ids/values.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

QUESTION_SET_VERSION = "tr-import-decisions-v1"

# Answer values shared by the yes/no questions.
YES = "true"
NO = "false"

_VAT_LEGAL_BASIS = "3065 s. KDV Kanunu md. 28; 2007/13033 s. BKK ekli listeler"
_KKDF_LEGAL_BASIS = "88/12944 s. Kararname; Kaynak Kullanımını Destekleme Fonu kesintisi"
_SURVEILLANCE_LEGAL_BASIS = "İthalatta Gözetim Uygulanmasına İlişkin Tebliğler (2004/7 s. Karar)"
_ATR_LEGAL_BASIS = "1/95 s. Ortaklık Konseyi Kararı; A.TR Dolaşım Belgesi"
_ORIGIN_PROOF_LEGAL_BASIS = "Gümrük Yönetmeliği md. 205 vd.; tercihli menşe tevsiki (EUR.1 / menşe beyanı / tedarikçi beyanı)"
_QUOTA_LEGAL_BASIS = "İthalatta Kota ve Tarife Kontenjanı İdaresi Hakkında Karar; tarife kontenjanı tebliğleri"
_USED_GOODS_LEGAL_BASIS = "İthalat Rejimi Kararı; kullanılmış eşya ithalinde izin (Ticaret Bakanlığı)"


class DecisionOption(BaseModel):
    """One selectable answer; ``value`` is what the client sends back."""

    value: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=300)


class DecisionQuestion(BaseModel):
    id: str = Field(..., min_length=1, max_length=60)
    text: str = Field(..., min_length=1, max_length=600)
    options: list[DecisionOption] = Field(default_factory=list, max_length=8)
    affects: list[str] = Field(default_factory=list, max_length=6)
    legal_basis: str | None = Field(None, max_length=400)
    default: str | None = Field(None, max_length=80)
    reason: str = Field("", max_length=600)


# --------------------------------------------------------------------------- helpers


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:  # pragma: no cover - non-pydantic dump()
            return dump()
    return {}


def _field(source: Any, name: str) -> Any:
    """Read one field from a pydantic model, a dict or ``None``."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _rate_label(rate: Any) -> str:
    try:
        number = float(rate)
    except (TypeError, ValueError):
        return ""
    return f"%{number:g}"


def _live_hits(report: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    hits = report.get(kind) or []
    live: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        if hit.get("status") == "expired" or hit.get("origin_match") is False:
            continue
        live.append(hit)
    return live


def _payment_known(value: Any) -> bool:
    """True when the payment method text already decides the KKDF rate."""
    key = _text(value).casefold().replace("ı", "i")
    if not key:
        return False
    if any(token in key for token in ("mal mukabili", "vadeli", "kredi")):
        return True
    return "pesin" in key or "peşin" in key


# --------------------------------------------------------------------------- questions


def _vat_questions(vat: dict[str, Any], inquiry: Any) -> list[DecisionQuestion]:
    if not vat or _field(inquiry, "vat_rate") is not None:
        return []
    legal_basis = _text(vat.get("legal_basis")) or _VAT_LEGAL_BASIS
    if vat.get("ambiguous"):
        options: list[DecisionOption] = []
        conditions: list[str] = []
        for candidate in vat.get("candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("rate") is None:
                continue
            label = _rate_label(candidate.get("rate"))
            if not label:
                continue
            candidate_conditions = [str(item) for item in (candidate.get("conditions") or []) if str(item).strip()]
            expression = _text(candidate.get("matched_expression"))
            detail = " · ".join([item for item in (", ".join(candidate_conditions), expression) if item])
            conditions.extend(candidate_conditions)
            options.append(
                DecisionOption(
                    value=f"{float(candidate['rate']):g}",
                    label=(f"{label} – {detail}" if detail else f"{label} – {_text(candidate.get('legal_basis'))}")[:300],
                )
            )
        if len(options) < 2:
            return []
        unique_conditions: list[str] = []
        for item in conditions:
            if item not in unique_conditions:
                unique_conditions.append(item)
        subject = _text(vat.get("row_text")) or _text(vat.get("matched_expression")) or "listedeki satır"
        condition_text = "; ".join(unique_conditions[:3]) or subject
        return [
            DecisionQuestion(
                id="vat_rate",
                text=f"Eşya “{condition_text}” koşulunu karşılıyor mu? Karşılanan satıra göre KDV oranını seçin.",
                options=options[:6],
                affects=["vat_rate"],
                legal_basis=legal_basis,
                reason="KDV listesinde aynı özgüllükte birden çok oran adayı var; şartı yalnızca siz doğrulayabilirsiniz.",
            )
        ]
    rate = vat.get("rate")
    if rate is None:
        return []
    label = _rate_label(rate)
    expression = _text(vat.get("matched_expression"))
    basis_label = "resmî liste" if vat.get("basis") == "official_list" else "sezgisel fasıl kuralı"
    return [
        DecisionQuestion(
            id="vat_rate_confirm",
            text=f"KDV önerisi {label} ({basis_label}). Bu oranı hesapta kullanalım mı?",
            options=[
                DecisionOption(value=f"{float(rate):g}", label=f"Evet, {label} uygulansın"),
                DecisionOption(value="reddet", label="Hayır, oranı kendim gireceğim"),
            ],
            affects=["vat_rate"],
            legal_basis=legal_basis,
            reason=(
                f"Öneri {basis_label}nden geliyor{f' (eşleşen ifade: {expression})' if expression else ''}; "
                "oran yalnızca onayınızla hesaba girer."
            ),
        )
    ]


def _surveillance_question(trade: dict[str, Any], inquiry: Any) -> list[DecisionQuestion]:
    if _field(inquiry, "has_surveillance_certificate") is not None:
        return []
    hits = _live_hits(trade, "surveillance")
    threshold = trade.get("surveillance_unit_value")
    if not hits and threshold is None:
        return []
    first = hits[0] if hits else {}
    unit = _text(first.get("unit")) or "ABD Doları"
    rate_text = _text(first.get("rate_text")) or (f"{threshold:g}" if isinstance(threshold, (int, float)) else "")
    detail = f"{rate_text} {unit}".strip()
    return [
        DecisionQuestion(
            id="surveillance_certificate",
            text=f"Bu GTİP gözetim tebliği kapsamında (birim kıymet eşiği {detail}). Gözetim belgeniz var mı?",
            options=[
                DecisionOption(value=YES, label="Evet, gözetim belgesi var"),
                DecisionOption(value=NO, label="Hayır, belge yok"),
            ],
            affects=["has_surveillance_certificate"],
            legal_basis=_text(first.get("legal_act")) or _SURVEILLANCE_LEGAL_BASIS,
            reason="Belge yoksa gümrük kıymeti tebliğdeki birim kıymete yükseltilir; bu yalnızca beyanınızla uygulanır.",
        )
    ]


def _payment_question(inquiry: Any) -> list[DecisionQuestion]:
    if _field(inquiry, "kkdf_rate") is not None:
        return []
    if _payment_known(_field(inquiry, "payment_method")):
        return []
    return [
        DecisionQuestion(
            id="payment_method",
            text="İthalat bedeli nasıl ödenecek? KKDF oranı ödeme şekline bağlıdır.",
            options=[
                DecisionOption(value="pesin", label="Peşin ödeme (KKDF %0)"),
                DecisionOption(value="vadeli", label="Vadeli / mal mukabili / kredili (KKDF %6)"),
            ],
            affects=["payment_method", "kkdf_rate"],
            legal_basis=_KKDF_LEGAL_BASIS,
            reason="Ödeme şekli beyan edilmeden KKDF kalemi hesaplanamaz; oran yalnızca seçiminizle girer.",
        )
    ]


def _atr_question(lookup: dict[str, Any], inquiry: Any) -> list[DecisionQuestion]:
    if not lookup.get("atr_available"):
        return []
    if _field(inquiry, "atr_certificate") is not None or lookup.get("atr_certificate") is not None:
        return []
    dispatch = _text(lookup.get("dispatch_country"))
    return [
        DecisionQuestion(
            id="atr_certificate",
            text=(
                f"Sevkiyat {dispatch or 'AB'} üzerinden geliyor; A.TR Dolaşım Belgesi ibraz edilecek mi?"
                if dispatch
                else "A.TR Dolaşım Belgesi ibraz edilecek mi?"
            ),
            options=[
                DecisionOption(value=YES, label="Evet, A.TR ibraz edilecek"),
                DecisionOption(value=NO, label="Hayır, A.TR yok"),
            ],
            affects=["atr_certificate"],
            legal_basis=_ATR_LEGAL_BASIS,
            reason="Serbest dolaşım sütunu yalnızca A.TR beyanınızla uygulanır; teyit edilmeden oran değiştirilmez.",
        )
    ]


def _origin_proof_question(lookup: dict[str, Any]) -> list[DecisionQuestion]:
    required = [str(item) for item in (lookup.get("origin_proof_required") or []) if str(item).strip()]
    if not required:
        return []
    labels = {
        "customs_duty": "gümrük vergisi",
        "additional_duty": "ilave gümrük vergisi",
        "additional_financial_liability": "ek mali yükümlülük",
    }
    measures = ", ".join(labels.get(item, item) for item in required[:4])
    return [
        DecisionQuestion(
            id="origin_proof",
            text=f"Tercihli oran ({measures}) için menşe tevsiki gerekiyor. EUR.1 / menşe beyanı / tedarikçi beyanı var mı?",
            options=[
                DecisionOption(value=YES, label="Evet, menşe tevsik belgesi var"),
                DecisionOption(value=NO, label="Hayır, belge yok (Diğer Ülkeler oranı)"),
            ],
            affects=["origin_proof_required"],
            legal_basis=_ORIGIN_PROOF_LEGAL_BASIS,
            reason="Tevsik yoksa tercihli sütun uygulanamaz; hangi sütunun geçerli olduğunu belge durumunuz belirler.",
        )
    ]


def _quota_question(trade: dict[str, Any]) -> list[DecisionQuestion]:
    hits = _live_hits(trade, "tariff_quota")
    if not hits:
        return []
    first = hits[0]
    product = _text(first.get("product")) or _text(first.get("matched_code"))
    return [
        DecisionQuestion(
            id="tariff_quota_certificate",
            text=f"Bu GTİP tarife kontenjanı kapsamında ({product}). Kontenjan (ithal lisansı) belgeniz var mı?",
            options=[
                DecisionOption(value=YES, label="Evet, kontenjan belgesi var"),
                DecisionOption(value=NO, label="Hayır, kontenjan dışı"),
            ],
            affects=["tariff_quota"],
            legal_basis=_text(first.get("legal_act")) or _QUOTA_LEGAL_BASIS,
            reason="Kontenjan içi ve dışı oranlar farklıdır; hangisinin uygulanacağını belge durumunuz belirler.",
        )
    ]


def _used_goods_question(inquiry: Any) -> list[DecisionQuestion]:
    condition = _text(_field(inquiry, "condition")) or "unknown"
    if condition in {"new", "used"}:
        return []
    return [
        DecisionQuestion(
            id="used_goods",
            text="Eşya kullanılmış (ikinci el, yenilenmiş) mi?",
            options=[
                DecisionOption(value=NO, label="Hayır, sıfır/yeni eşya"),
                DecisionOption(value=YES, label="Evet, kullanılmış eşya"),
            ],
            affects=["condition"],
            legal_basis=_USED_GOODS_LEGAL_BASIS,
            reason="Kullanılmış eşyada izin, kıymet ve kontrol rejimi değişir; bu bilgi yalnızca sizden alınır.",
        )
    ]


def build_decision_questions(
    *,
    gtip: str | None = None,
    tariff_lookup: Any = None,
    vat_lookup: Any = None,
    trade_measures: Any = None,
    inquiry: Any = None,
) -> list[DecisionQuestion]:
    """Deterministic question list for one GTİP; answered fields produce no question."""
    lookup = _as_dict(tariff_lookup)
    vat = _as_dict(vat_lookup) or _as_dict(lookup.get("vat_rate"))
    trade = _as_dict(trade_measures) or _as_dict(lookup.get("trade_measures"))
    code = _text(gtip) or _text(lookup.get("gtip"))
    questions: list[DecisionQuestion] = [
        *_vat_questions(vat, inquiry),
        *_surveillance_question(trade, inquiry),
        *_payment_question(inquiry),
        *_atr_question(lookup, inquiry),
        *_origin_proof_question(lookup),
        *_quota_question(trade),
        *_used_goods_question(inquiry),
    ]
    if code:
        for question in questions:
            if question.reason and len(question.reason) < 560:
                question.reason = f"{question.reason} (GTİP {code})"
    seen: set[str] = set()
    unique: list[DecisionQuestion] = []
    for question in questions:
        if question.id in seen:
            continue
        seen.add(question.id)
        unique.append(question)
    return unique


# --------------------------------------------------------------------------- answers


def _rate_from_answer(value: str) -> float | None:
    try:
        rate = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if rate < 0 or rate > 100:
        return None
    return rate


def _boolean_from_answer(value: str) -> bool | None:
    key = str(value).strip().casefold()
    if key in {"true", "evet", "var", "yes", "1"}:
        return True
    if key in {"false", "hayir", "hayır", "yok", "no", "0"}:
        return False
    return None


def apply_decision_answers(answers: dict[str, str], cost_input_dict: dict) -> dict:
    """Write the user's own answers into a landed-cost input dict.

    Only the fields the user answered are touched, a field that already carries a
    user value is never overwritten, and unknown question ids or option values are
    ignored.  Nothing is inferred: an unanswered question leaves the field empty.
    """
    updated = dict(cost_input_dict or {})
    if not isinstance(answers, dict):
        return updated

    def put(field: str, value: Any) -> None:
        if updated.get(field) is None:
            updated[field] = value

    for raw_id, raw_value in answers.items():
        question_id = str(raw_id).strip()
        value = str(raw_value).strip() if raw_value is not None else ""
        if not question_id or not value:
            continue
        if question_id in {"vat_rate", "vat_rate_confirm"}:
            rate = _rate_from_answer(value)
            if rate is not None:
                put("vat_rate", rate)
        elif question_id == "payment_method":
            key = value.casefold().replace("ı", "i")
            if key in {"pesin", "peşin", "cash"}:
                put("payment_method", "Peşin")
                put("kkdf_rate", 0.0)
            elif key in {"vadeli", "kredili", "mal mukabili", "deferred"}:
                put("payment_method", "Vadeli / kredili")
                put("kkdf_rate", 6.0)
        elif question_id == "surveillance_certificate":
            flag = _boolean_from_answer(value)
            if flag is not None:
                put("has_surveillance_certificate", flag)
        elif question_id == "atr_certificate":
            flag = _boolean_from_answer(value)
            if flag is not None:
                put("atr_certificate", flag)
    return updated
