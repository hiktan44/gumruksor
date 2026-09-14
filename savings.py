"""Savings ranking across origin scenarios (PRD Faz 2.2).

Pure functions: take the scenario rows produced by ``scenarios.build_origin_scenarios``
plus the user's cost inputs, run the deterministic landed-cost ledger for every
comparable row and rank them by total landed cost.

Comparability is conservative.  A row enters the ranking only when the official
rate of every calculable measure type is known without ambiguity; nothing is
silently assumed to be zero.  The result is decision support, never advice or a
binding tariff/origin decision – every ranked row carries the conditions (document
presentation, origin proof) under which its cost applies.
"""
from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from tariff_engine import LandedCostInput, LandedCostResult, calculate_landed_cost

MEASURE_LABELS = {
    "customs_duty": "Gümrük vergisi",
    "additional_duty": "İGV",
    "additional_financial_liability": "Ek mali yükümlülük",
}
RATE_FIELDS = {
    "customs_duty": "customs_duty_rate",
    "additional_duty": "additional_duty_rate",
    "additional_financial_liability": "additional_financial_liability_rate",
}
# Motorun "bu kalem listede yok, uygulanmaz" dediği uyarı metinleri (tariff_engine.lookup).
_NOT_APPLICABLE_MARKERS = {
    "additional_duty": ("İGV uygulanmaz (%0)",),
    "additional_financial_liability": ("ek mali yükümlülük (EMY) tespit edilmemiştir",),
}
_STATUS_LABELS = {"partial": "kısmi eşleşme", "not_found": "satır bulunamadı", "unavailable": "tarife tabloları hazır değil"}
_DOCUMENT_CONDITIONS = {
    "ATR": "A.TR dolaşım belgesi ibrazı (eşya AB'de serbest dolaşımda olmalı)",
    "EUR1": "EUR.1 / EUR-MED dolaşım sertifikası veya onaylanmış ihracatçı fatura beyanı",
    "EUR_MED": "EUR.1 / EUR-MED dolaşım sertifikası veya onaylanmış ihracatçı fatura beyanı",
    "ORIGIN_DECLARATION": "İhracatçının fatura/ticari belge üzerindeki menşe beyanı",
    "AGREEMENT_CERT": "Anlaşmaya özgü menşe ispat belgesi",
    "GSP_ORIGIN": "GTS menşe belgesi (Form A / menşe beyanı)",
    "SUPPLIER_DECLARATION": "Tedarikçi beyanı veya menşe belgesi ile menşe tevsiki (yoksa 'Diğer Ülkeler' oranı)",
    "CERT_ORIGIN": "Menşe şahadetnamesi (İGV/EMY menşe tevsiki istenirse)",
}

LEGAL_NOTICE = (
    "Tasarruf sıralaması, resmî tarife arşivinin güncel snapshot'ından ve girdiğiniz maliyet kalemlerinden "
    "üretilen bir karar desteğidir; bağlayıcı tarife bilgisi, menşe kararı veya tavsiye değildir. Sıralama menşe "
    "tevsikinin sağlandığı varsayımıyla yapılır; tevsik yoksa uygulanacak 'Diğer Ülkeler' toplamı ayrıca gösterilir. "
    "Her satırdaki koşullar (belge ibrazı, menşe tevsiki, ürün bazlı menşe kuralları) sağlanmadan tasarruf "
    "gerçekleşmez; beyan öncesinde GTİP, menşe, dipnot ve yürürlük yeniden doğrulanmalıdır."
)


def _key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold().replace("ı", "i"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{float(value):g}"


@dataclass
class ScenarioOutcome:
    origin_country: str
    dispatch_country: str | None
    atr_certificate: bool | None
    variant: str  # "base" | "atr"
    comparable: bool = True
    reasons_not_comparable: list[str] = field(default_factory=list)
    rates_used: dict[str, float | None] = field(default_factory=dict)
    fallback_rates: dict[str, float] = field(default_factory=dict)
    cost: LandedCostResult | None = None
    cost_pessimistic: LandedCostResult | None = None
    total_taxes: float | None = None
    landed_total: float | None = None
    origin_documents: dict[str, Any] | None = None
    conditions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resolved_country_group: str | None = None
    atr_available: bool = False
    origin_proof_required: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cost"] = self.cost.model_dump(mode="json") if self.cost is not None else None
        data["cost_pessimistic"] = self.cost_pessimistic.model_dump(mode="json") if self.cost_pessimistic is not None else None
        data["landed_total_pessimistic"] = self.cost_pessimistic.landed_total if self.cost_pessimistic is not None else None
        data["total_taxes_pessimistic"] = self.cost_pessimistic.total_taxes if self.cost_pessimistic is not None else None
        return data


def _engine_says_not_applicable(row: dict[str, Any], measure_type: str) -> bool:
    markers = _NOT_APPLICABLE_MARKERS.get(measure_type, ())
    return any(marker in str(warning) for warning in row.get("warnings") or [] for marker in markers)


def _select_rates(
    row: dict[str, Any], cost_input: LandedCostInput, outcome: ScenarioOutcome
) -> dict[str, float | None]:
    """Choose the rate per measure type; never assume a silent zero."""
    official = dict(row.get("unambiguous_rates") or {})
    ambiguous = set(row.get("ambiguous_measure_types") or [])
    rates: dict[str, float | None] = {}
    for measure_type, field_name in RATE_FIELDS.items():
        label = MEASURE_LABELS[measure_type]
        user_rate = getattr(cost_input, field_name)
        if measure_type in official:
            rate = float(official[measure_type])
            if user_rate is not None and abs(float(user_rate) - rate) > 1e-9:
                outcome.conditions.append(
                    f"{label}: girdiğiniz %{float(user_rate):g} yerine resmî sütundaki %{rate:g} kullanıldı; beyan öncesi doğrulayın."
                )
            rates[measure_type] = rate
        elif measure_type in ambiguous:
            outcome.comparable = False
            outcome.reasons_not_comparable.append(
                f"{label} oranı alt GTİP satırlarında değişiyor; kesin 12 haneli GTİP seçilmeden karşılaştırılamaz."
            )
            rates[measure_type] = None
        elif _engine_says_not_applicable(row, measure_type) and not (user_rate is not None and float(user_rate) > 0):
            # Motor listede olmadığını açıkça söylüyor; kullanıcı pozitif oran girdiyse (doğrulanmış
            # önlem olabilir) o oran aşağıdaki dalda korunur — sessiz sıfırlama yok.
            rates[measure_type] = 0.0
            outcome.conditions.append(f"{label}: resmî liste bu GTİP'i kapsamadığı için %0 alındı.")
        else:
            rates[measure_type] = float(user_rate) if user_rate is not None else None
            if user_rate is not None:
                outcome.conditions.append(
                    f"{label}: resmî satırda bulunamadı; girdiğiniz %{float(user_rate):g} kullanıldı, beyan öncesi doğrulayın."
                )
            else:
                outcome.conditions.append(f"{label}: resmî satırda bulunamadı ve oran girilmedi; doğrulayıp girin (uygulanmıyorsa 0).")
    return rates


def _document_conditions(row: dict[str, Any], variant: str) -> list[str]:
    documents = (row.get("origin_documents") or {}).get("documents") or []
    proof_required = bool(row.get("origin_proof_required"))
    atr_route = variant == "atr" or bool(row.get("atr_free_circulation"))
    conditions: list[str] = []
    for document in documents:
        code = str(document.get("code", ""))
        if code == "ATR" and not atr_route:
            continue
        if code == "CERT_ORIGIN" and not proof_required:
            continue
        if code == "SUPPLIER_DECLARATION" and not proof_required:
            continue
        text = _DOCUMENT_CONDITIONS.get(code) or str(document.get("name") or code)
        if text not in conditions:
            conditions.append(text)
    return conditions


def _evaluate_row(row: dict[str, Any], cost_input: LandedCostInput, *, variant: str, atr_certificate: bool | None) -> ScenarioOutcome:
    outcome = ScenarioOutcome(
        origin_country=str(row.get("origin_country", "")),
        dispatch_country=row.get("dispatch_country"),
        atr_certificate=atr_certificate,
        variant=variant,
        fallback_rates=dict(row.get("fallback_rates") or {}),
        origin_documents=row.get("origin_documents"),
        resolved_country_group=row.get("resolved_country_group"),
        atr_available=bool(row.get("atr_available")),
        origin_proof_required=list(row.get("origin_proof_required") or []),
    )
    status = str(row.get("status", ""))
    if status != "matched":
        outcome.comparable = False
        outcome.reasons_not_comparable.append(
            f"Tarife satırı kesin eşleşmedi ({_STATUS_LABELS.get(status, status or 'durum yok')}); oran otomatik alınmadı."
        )
    if row.get("origin_recognised") is False:
        outcome.comparable = False
        outcome.reasons_not_comparable.append(
            "Menşe ülke tanınmadı; 'Diğer Ülkeler' varsayımıyla üretilen oran karşılaştırmaya alınmaz."
        )
    rates = _select_rates(row, cost_input, outcome)
    outcome.rates_used = rates
    outcome.conditions = [*_document_conditions(row, variant), *outcome.conditions]

    update = {RATE_FIELDS[measure_type]: rate for measure_type, rate in rates.items()}
    outcome.cost = calculate_landed_cost(cost_input.model_copy(update=update))
    outcome.total_taxes = outcome.cost.total_taxes
    outcome.landed_total = outcome.cost.landed_total
    if outcome.landed_total is None:
        outcome.comparable = False
        missing = ", ".join(outcome.cost.missing_rates) or "eksik girdi"
        outcome.reasons_not_comparable.append(f"Maliyet tamamlanamadı; doğrulanmamış kalemler: {missing}.")

    proof_required = [item for item in outcome.origin_proof_required if item in RATE_FIELDS]
    if proof_required and outcome.landed_total is not None:
        pessimistic_update = dict(update)
        covered: list[str] = []
        for measure_type in proof_required:
            fallback = outcome.fallback_rates.get(measure_type)
            if fallback is not None:
                pessimistic_update[RATE_FIELDS[measure_type]] = float(fallback)
                covered.append(f"{MEASURE_LABELS[measure_type]} %{float(fallback):g}")
        if covered:
            outcome.cost_pessimistic = calculate_landed_cost(cost_input.model_copy(update=pessimistic_update))
            outcome.conditions.append(
                "İGV/EMY tercihli oranı menşe tevsikine bağlıdır; tevsik yoksa 'Diğer Ülkeler' oranı ("
                + ", ".join(covered)
                + f") uygulanır → toplam {_fmt(outcome.cost_pessimistic.landed_total)} {outcome.cost.currency}."
            )
        else:
            outcome.conditions.append(
                "İGV/EMY tercihli oranı menşe tevsikine bağlıdır; tevsik yoksa 'Diğer Ülkeler' oranı uygulanır (resmî satırda oran okunamadı)."
            )

    row_warnings = [str(item) for item in row.get("warnings") or [] if "kapsam matrisi" not in str(item)]
    outcome.warnings.extend(row_warnings[:4])
    if variant == "base" and outcome.atr_available and not row.get("atr_free_circulation"):
        outcome.warnings.append(
            "A.TR ile gümrük vergisi serbest dolaşım sütunundan hesaplanabilir; 'A.TR ile' varyantına bakın."
        )
    outcome.conditions = list(dict.fromkeys(outcome.conditions))
    outcome.warnings = list(dict.fromkeys(outcome.warnings))
    return outcome


def evaluate_scenarios(
    rows: list[dict[str, Any]],
    cost_input: LandedCostInput,
    *,
    atr_rows: list[dict[str, Any]] | None = None,
    atr_certificate: bool | None = None,
) -> list[ScenarioOutcome]:
    """One outcome per row, plus an "atr" variant where an A.TR pass was supplied.

    ``atr_certificate`` is the request-level declaration the base rows were looked up with.
    """
    outcomes: list[ScenarioOutcome] = []
    atr_by_origin = {_key(row.get("origin_country")): row for row in atr_rows or []}
    for row in rows:
        base_atr = True if row.get("atr_free_circulation") else atr_certificate
        outcomes.append(_evaluate_row(row, cost_input, variant="base", atr_certificate=base_atr))
        atr_row = atr_by_origin.get(_key(row.get("origin_country")))
        if atr_row is not None and row.get("atr_available") and not row.get("atr_free_circulation"):
            outcomes.append(_evaluate_row(atr_row, cost_input, variant="atr", atr_certificate=True))
    return outcomes


def rank_savings(outcomes: list[ScenarioOutcome], baseline_origin: str | None) -> dict[str, Any]:
    """Rank comparable outcomes by landed total and express savings against a baseline."""
    comparable = [item for item in outcomes if item.comparable and item.landed_total is not None]
    comparable.sort(key=lambda item: (float(item.landed_total or 0.0), float(item.total_taxes or 0.0), _key(item.origin_country), item.variant))
    baseline: ScenarioOutcome | None = None
    if baseline_origin:
        baseline = next(
            (item for item in comparable if item.variant == "base" and _key(item.origin_country) == _key(baseline_origin)), None
        )
    if baseline is None and comparable:
        baseline = comparable[-1]

    ranked: list[dict[str, Any]] = []
    for position, item in enumerate(comparable, start=1):
        data = item.to_dict()
        data["rank"] = position
        data["is_baseline"] = item is baseline
        if baseline is not None and baseline.landed_total is not None and item.landed_total is not None:
            saving = round(float(baseline.landed_total) - float(item.landed_total), 2)
            data["savings_vs_baseline"] = saving
            data["savings_pct"] = round(saving / float(baseline.landed_total) * 100, 2) if baseline.landed_total else None
            data["savings_taxes_vs_baseline"] = (
                round(float(baseline.total_taxes) - float(item.total_taxes), 2)
                if baseline.total_taxes is not None and item.total_taxes is not None
                else None
            )
            if item.cost_pessimistic is not None and item.cost_pessimistic.landed_total is not None:
                data["savings_pessimistic"] = round(float(baseline.landed_total) - float(item.cost_pessimistic.landed_total), 2)
            else:
                data["savings_pessimistic"] = None
        else:
            data["savings_vs_baseline"] = None
            data["savings_pct"] = None
            data["savings_taxes_vs_baseline"] = None
            data["savings_pessimistic"] = None
        ranked.append(data)

    not_comparable = [
        {"origin_country": item.origin_country, "variant": item.variant, "reasons": list(item.reasons_not_comparable)}
        for item in outcomes
        if not (item.comparable and item.landed_total is not None)
    ]
    baseline_data = next((item for item in ranked if item["is_baseline"]), None)
    return {
        "baseline": baseline_data,
        "ranked": ranked,
        "not_comparable": not_comparable,
        "best": ranked[0] if ranked else None,
        "legal_notice": LEGAL_NOTICE,
    }
