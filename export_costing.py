"""İhracatta hedef ülke maliyeti — alıcının gümrükte ödeyeceği kalemler (FAZ 8.4).

Bu modül saftır: ağ yok, model yok, durum yok. Girdisi ön değerlendirmenin zaten
topladığı alanlar; çıktısı satır satır gerekçeli bir maliyet defteri.

**Neden bu kadar dar:** ihracatçı bir sayı görürse ona göre fiyat verir. Yanlış bir
"toplam", kaybedilen bir ihale ya da zarar edilen bir sevkiyat demektir. Bu yüzden
hesap dört kapıdan geçer ve herhangi biri kapalıysa **sayı üretilmez, sebep yazılır**:

1. **Veri düzeyi.** Yalnız ``tier == "rates"`` (AB-27 arşiv isabeti, Birleşik Krallık)
   hesaba girer. Diğer ülkelerde oran verimiz yok; kısmi veriden toplam üretmek
   "oran yalnız resmî anlık görüntüden" kuralını çiğnerdi.
2. **Oranın biçimi.** Yalnız **saf ad valorem** oran çözülür. ``10.2 % MIN 1.6 EUR/kg``
   bileşiktir ve yüzde gibi işlenirse gerçek yükü **olduğundan düşük** gösterir;
   ``1.6 EUR/kg`` spesifiktir ve kıymetten hesaplanamaz. İkisi de reddedilir.
3. **Tercih ispata bağlıdır.** Tercihli oran yalnız kullanıcı menşe ispatını
   (A.TR / EUR.1 / menşe beyanı) düzenleyeceğini **onayladıysa** uygulanır. Aksi hâlde
   kötümser üçüncü ülke oranı esas alınır — ithalat tarafındaki "tevsik yoksa Diğer
   Ülkeler oranı" kuralının aynası.
4. **KDV toplama girmez.** Hedef ülke KDV'si bizde fasıl kuralından türetilmiş bir
   **tahmindir** (hiçbir koşulda ``verified`` olmaz) ve üstelik KDV mükellefi alıcı
   için çoğu zaman **indirilebilir/iade edilebilir** bir kalemdir. Bu yüzden ayrı
   satırda, açıkça tahmin olarak gösterilir ve başlık toplamına dahil edilmez.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "ExportCostLine",
    "ExportCostEstimate",
    "parse_ad_valorem",
    "build_export_cost",
]

CostStatus = Literal["calculated", "rates_unavailable", "rate_not_calculable", "input_missing"]

# Gümrük kıymeti hedef ülkede CIF esaslıdır (AB ve BK); navlun/sigorta eksikse
# matrah olduğundan düşük çıkar ve bu açıkça uyarıya yazılır.
_CIF_NOTE = (
    "Hedef ülkede gümrük kıymeti CIF esaslıdır: navlun ve sigorta matraha dahildir. "
    "Teslim şekliniz EXW/FOB ise bu kalemleri ekleyin, aksi hâlde matrah eksik kalır."
)

# "Free", "0 %", "3.7 %" gibi SAF ad valorem ifadeler. Sayıdan sonra yüzde dışında
# bir şey gelirse (MIN/MAX, EUR/kg, +) ifade bileşiktir ve buraya düşmez.
_PURE_PERCENT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*%\s*$")
_FREE_WORDS = {"free", "serbest", "0", "0%", "%0", "muaf", "nil", "yok"}


def parse_ad_valorem(text: Any) -> float | None:
    """Saf ad valorem oranı yüzde olarak döndürür; bileşik/spesifik ifadede ``None``.

    ``None`` "oran sıfır" demek **değildir**, "bu ifadeden oran hesaplanamaz" demektir.
    Çağıran bu ikisini karıştırmamalıdır.
    """
    value = str(text or "").strip()
    if not value:
        return None
    if value.casefold() in _FREE_WORDS:
        return 0.0
    match = _PURE_PERCENT_RE.match(value)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:  # pragma: no cover - regex zaten sayıyı garanti eder
        return None


class ExportCostLine(BaseModel):
    """Maliyet defterinin tek satırı."""

    key: str
    label: str
    amount: float | None = None
    rate_percent: float | None = None
    basis: str = ""
    # Toplama giren satır mı? KDV gibi tahmin kalemleri `False` taşır.
    included_in_total: bool = True
    note: str = ""
    source_url: str | None = None


class ExportCostEstimate(BaseModel):
    """Hedef ülkede alıcının ödeyeceği gümrük yükü."""

    status: CostStatus
    currency: str = ""
    customs_value: float | None = None
    total_duties: float | None = None
    landed_before_vat: float | None = None
    duty_basis: Literal["third_country", "preferential"] | None = None
    lines: list[ExportCostLine] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=10)
    # Neden hesaplanmadığını insan diliyle söyler; `status != "calculated"` iken dolu.
    reason: str = ""
    legal_note: str = (
        "Bu tutar hedef ülkede alıcının ödeyeceği gümrük yükünün tahminidir; bağlayıcı "
        "tarife bilgisi veya vergi görüşü değildir. Kesin tutar, tescil günündeki kur, "
        "gümrük kıymeti tespiti ve yürürlükteki ölçülere göre belirlenir."
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _field(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        return data.get(key)
    return getattr(data, key, None)


def _duty_expression(duty: dict[str, Any] | None, *keys: str) -> str | None:
    if not duty:
        return None
    for key in keys:
        value = duty.get(key)
        if isinstance(value, dict):
            value = value.get("rate") or value.get("duty_expression") or value.get("text")
        text = str(value or "").strip()
        if text:
            return text
    return None


def build_export_cost(
    inquiry_like: Any,
    profile: Any,
    *,
    destination_duty: dict[str, Any] | None = None,
    duty_source: dict[str, str] | None = None,
    destination_vat: dict[str, Any] | None = None,
    preference_proof_confirmed: bool = False,
) -> ExportCostEstimate:
    """Hedef ülke gümrük yükünü hesaplar; kapılardan biri kapalıysa sebebini yazar."""
    tier = str(_field(profile, "tier") or "")
    country = str(_field(profile, "country_name") or _field(profile, "country_input") or "hedef ülke")

    if tier != "rates" or not destination_duty:
        badge = str(_field(profile, "badge_text") or "")
        return ExportCostEstimate(
            status="rates_unavailable",
            reason=(
                f"{country} için oran verimiz yok, bu yüzden maliyet hesaplanmadı. "
                + (badge or "Oranı hedef ülkenin resmî tarife ekranından doğrulayın.")
            ),
        )

    data = inquiry_like if isinstance(inquiry_like, dict) else getattr(inquiry_like, "__dict__", {}) or {}
    invoice = _number(data.get("invoice_value"))
    if invoice is None or invoice <= 0:
        return ExportCostEstimate(
            status="input_missing",
            reason="Fatura bedeli girilmeden hedef ülke maliyeti hesaplanamaz.",
        )

    freight = _number(data.get("freight")) or 0.0
    insurance = _number(data.get("insurance")) or 0.0
    currency = str(data.get("currency") or "").strip()
    customs_value = invoice + freight + insurance

    warnings: list[str] = []
    if freight == 0.0 and insurance == 0.0:
        warnings.append(_CIF_NOTE)

    third_text = _duty_expression(destination_duty, "third_country_duty", "mfn_rate")
    pref_text = _duty_expression(destination_duty, "origin_preference", "partner_rate")

    # Tercihli oran yalnız ispat onaylandıysa; aksi hâlde kötümser üçüncü ülke oranı.
    if preference_proof_confirmed and pref_text:
        chosen_text, basis = pref_text, "preferential"
    else:
        chosen_text, basis = third_text, "third_country"
        if pref_text:
            warnings.append(
                f"Tercihli oran ({pref_text}) hesaba katılmadı: menşe ispat belgesini "
                "düzenleyeceğinizi onaylamadınız. Onaylarsanız hesap tercihli oranla yenilenir."
            )

    if not chosen_text:
        return ExportCostEstimate(
            status="rate_not_calculable",
            currency=currency,
            customs_value=customs_value,
            warnings=warnings,
            reason=f"{country} için gümrük vergisi oranı anlık görüntüde bulunamadı.",
        )

    rate = parse_ad_valorem(chosen_text)
    if rate is None:
        return ExportCostEstimate(
            status="rate_not_calculable",
            currency=currency,
            customs_value=customs_value,
            duty_basis=basis,  # type: ignore[arg-type]
            warnings=warnings,
            reason=(
                f"Oran ifadesi ({chosen_text}) bileşik veya miktar esaslıdır; kıymet üzerinden "
                "yüzde olarak hesaplanamaz. Bu ifadeyi yüzde sanıp çarpmak gerçek yükü "
                "olduğundan düşük gösterirdi. Tutarı hedef ülkenin resmî tarife ekranından hesaplayın."
            ),
        )

    source_url = (duty_source or {}).get("url")
    duty_amount = round(customs_value * rate / 100.0, 2)

    lines: list[ExportCostLine] = [
        ExportCostLine(
            key="customs_value",
            label="Gümrük kıymeti (CIF)",
            amount=round(customs_value, 2),
            basis=f"fatura {invoice:g} + navlun {freight:g} + sigorta {insurance:g}",
            note=_CIF_NOTE,
        ),
        ExportCostLine(
            key="customs_duty",
            label=(
                "Gümrük vergisi (tercihli oran)"
                if basis == "preferential"
                else "Gümrük vergisi (üçüncü ülke oranı)"
            ),
            amount=duty_amount,
            rate_percent=rate,
            basis=chosen_text,
            note=(
                "Menşe ispat belgesi ibraz edildiği varsayımıyla."
                if basis == "preferential"
                else "Menşe ispatı olmadan uygulanacak oran."
            ),
            source_url=source_url,
        ),
    ]

    total_duties = duty_amount

    # Ek ölçüler (damping vb.) listelenir ama yalnız saf ad valorem olanı toplanır.
    for measure in (destination_duty.get("additional_duties") or [])[:4]:
        text = _duty_expression({"x": measure}, "x") or ""
        extra = parse_ad_valorem(text)
        label = str((measure or {}).get("label") if isinstance(measure, dict) else "") or "Ek ölçü"
        if extra is None:
            lines.append(
                ExportCostLine(
                    key="additional_measure",
                    label=label,
                    basis=text,
                    included_in_total=False,
                    note="Bu ölçü kıymet üzerinden yüzde olarak hesaplanamıyor; tutarı resmî ekrandan doğrulayın.",
                )
            )
            warnings.append(f"{label} toplama dahil edilmedi: oran ifadesi hesaplanabilir değil ({text}).")
            continue
        amount = round(customs_value * extra / 100.0, 2)
        total_duties += amount
        lines.append(
            ExportCostLine(
                key="additional_measure",
                label=label,
                amount=amount,
                rate_percent=extra,
                basis=text,
                source_url=source_url,
            )
        )

    landed = round(customs_value + total_duties, 2)

    # KDV bilerek toplamın DIŞINDA: fasıl kuralından türetilmiş bir tahmindir ve
    # KDV mükellefi alıcı için çoğu zaman indirilebilir/iade edilebilir bir kalemdir.
    vat_rate = _number((destination_vat or {}).get("applicable"))
    if vat_rate is not None:
        basis_kind = str((destination_vat or {}).get("applicable_basis") or "")
        lines.append(
            ExportCostLine(
                key="destination_vat",
                label="Hedef ülke ithalat KDV'si (tahmin — toplama dahil değil)",
                amount=round(landed * vat_rate / 100.0, 2),
                rate_percent=vat_rate,
                basis=f"%{vat_rate:g}",
                included_in_total=False,
                note=(
                    ("Oran fasıl kuralından türetilmiş bir öneridir, tespit değildir; doğrulayın. "
                     if basis_kind == "chapter_rule" else "")
                    + "KDV mükellefi alıcı için genellikle indirilebilir/iade edilebilir bir kalemdir, "
                    "bu yüzden gümrük yükü toplamına eklenmez."
                ),
            )
        )
        warnings.append(
            "Hedef ülke KDV'si tahminidir ve toplam gümrük yüküne dahil edilmemiştir."
        )

    return ExportCostEstimate(
        status="calculated",
        currency=currency,
        customs_value=round(customs_value, 2),
        total_duties=round(total_duties, 2),
        landed_before_vat=landed,
        duty_basis=basis,  # type: ignore[arg-type]
        lines=lines,
        warnings=warnings,
    )
