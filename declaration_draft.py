"""Gümrük beyannamesi taslağı — Tek İdari Belge (BİLGE) kutularına saf alan eşlemesi.

Bu modül ``export_requirements.py`` ile aynı sözleşmeyi taşır: **ağ yok, model yok,
rastgelelik yok.** Aynı ön değerlendirme sonucu her zaman aynı taslağı üretir.

## Neden taslak, neden tescil değil

Beyanname tescili hukuken bağlayıcı bir işlemdir; yanlış tescil edilen beyanname
ceza ve sorumluluk doğurur (GK md. 234 vd.). Bu yüzden bu modülün hedefi
"tek tuşla tescil" değil, **beyan sahibinin inceleyip kendi tescil edeceği eksiksiz
bir taslaktır**. Dışa aktarılan JSON/CSV/XML, beyanname yazılımına elle ya da
ileride bir yazma ucu üzerinden aktarılacak gövdedir.

## Kesinlik rayı (``Certainty``) burada da aynen geçerlidir

* ``verified``       – elimizdeki resmî anlık görüntüden **okundu**; kutu kaynak
  URL'sini, tarihini ve sha256'sını taşır.
* ``check_required`` – kuraldan türetildi, kullanıcı yazdı ya da hesaplandı; ``note``
  neyin kontrol edileceğini yazar.
* ``unavailable``    – bu veriye sahip değiliz; kutu **değer taşımaz**, yalnız nereden
  alınacağı yazılır. Hiçbir kutu uydurulmaz.

Vergi tutarları (kutu 47) **hiçbir koşulda** ``verified`` olmaz: tescil günündeki kur
ve oran, ön değerlendirme anındakinden farklı olabilir ve hesap kullanıcının girdiği
kalemleri de içerir.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from export_requirements import Certainty, DeclarationReadiness

# Tek İdari Belge kutuları arasında bu taslağın doldurduğu rejim kodları.
# Kaynak: Gümrük Yönetmeliği Ek-14 (rejim kodları listesi).
IMPORT_REGIME_CODE = "4000"
IMPORT_REGIME_LABEL = "Serbest dolaşıma giriş (nihai kullanım dışı)"
EXPORT_REGIME_CODE = "1000"
EXPORT_REGIME_LABEL = "Kesin ihracat"

GUMRUK_YONETMELIGI_URL = "https://www.mevzuat.gov.tr/mevzuat?MevzuatNo=13634&MevzuatTur=7&MevzuatTertip=5"
BILGE_URL = "https://ticaret.gov.tr/gumruk-islemleri"
TICARET_EXPORT_URL = "https://ticaret.gov.tr/ihracat"

DRAFT_VERSION = "tr-declaration-draft-v1"

# Taslak, beyannamenin tamamı değildir; kalem bazlı kutular tek kalem varsayımıyla üretilir.
MAX_FIELDS = 64


class DraftField(BaseModel):
    """Beyannamenin tek bir kutusu ve o kutu için ne kadar emin olduğumuz."""

    key: str
    box: str | None = Field(None, description="Tek İdari Belge kutu numarası (örn. '33').")
    label: str
    value: str | None = None
    certainty: Certainty
    mandatory: bool = True
    # Kullanıcının beyan ettiği kutular (eşya tanımı, fatura, taraflar) makine tarafından
    # doğrulanamaz; hazırlık kapısı bunları "dolu mu" diye sorar, "doğrulandı mı" diye değil.
    user_supplied: bool = False
    # Resmî anlık görüntüden gelmesi gereken kutu (GTİP gibi): kuraldan türetilmiş ya da
    # eşleşmemiş bir değer bu kutuyu karşılamaz, ``verified`` olması gerekir.
    official: bool = False
    note: str = ""
    source_url: str | None = None
    source_date: str | None = None
    source_sha256: str | None = None


class DraftSection(BaseModel):
    """Beyanname taslağının bir bölümü (beyan, sevkiyat, eşya, kıymet, vergi, belge)."""

    id: str
    title: str
    fields: list[DraftField] = Field(default_factory=list, max_length=MAX_FIELDS)


class DeclarationDraft(BaseModel):
    """Beyanname yazılımına aktarılacak taslak gövde."""

    version: str = DRAFT_VERSION
    direction: Literal["import", "export"]
    regime_code: str
    regime_label: str
    generated_at: str
    sections: list[DraftSection] = Field(default_factory=list, max_length=8)
    readiness: DeclarationReadiness
    caveats: list[str] = Field(default_factory=list, max_length=8)
    legal_notice: str = ""


LEGAL_NOTICE = (
    "Bu taslak karar destek çıktısıdır; beyanname yerine geçmez ve bağlayıcı tarife, menşe veya "
    "kıymet tespiti değildir. Beyannamenin tescili beyan sahibinin sorumluluğundadır; tescil öncesi "
    "her kutu yürürlükteki mevzuat ve tescil günü kuru ile doğrulanmalıdır."
)


# --- Yardımcılar -----------------------------------------------------------------------------


def _get(source: Any, key: str, default: Any = None) -> Any:
    """Hem ``dict`` hem pydantic modeli/nesne için tek okuma yolu."""
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _text(value: Any, limit: int = 300) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        # Beyannamede ondalık ayırıcı nokta; gereksiz sondaki sıfırlar atılır.
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return " ".join(text.split())[:limit]


def _number(value: Any) -> str:
    text = _text(value)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot_reference(tariff_lookup: Any) -> dict[str, str | None]:
    """Tarife sonucunun dayandığı aktif anlık görüntünün künyesi."""
    snapshots = _get(tariff_lookup, "snapshots") or []
    for snapshot in snapshots:
        if _get(snapshot, "active", True):
            return {
                "url": _text(_get(snapshot, "landing_url"), 400) or None,
                "date": _text(_get(snapshot, "retrieved_at"), 40) or None,
                "sha256": _text(_get(snapshot, "archive_sha256"), 80) or None,
            }
    return {"url": None, "date": None, "sha256": None}


def _goods_description(inquiry: Any, tariff_lookup: Any) -> str:
    """Kutu 31 için ticari tanım: kullanıcının beyanı esastır, cetvel tanımı yedektir."""
    for key in ("declared_product_type", "product_description"):
        text = _text(_get(inquiry, key), 350)
        if text:
            return text
    for measure in _get(tariff_lookup, "measures") or []:
        description = _text(_get(measure, "description"), 350)
        if description:
            return description
    return ""


# --- Bölüm kurucuları ------------------------------------------------------------------------


class _Builder:
    """Kutuları sırayla toplayan küçük yardımcı; ithalat ve ihracat aynı kuralları paylaşır."""

    def __init__(self, direction: str, snapshot: dict[str, str | None]) -> None:
        self.direction = direction
        self.snapshot = snapshot
        self.sections: list[DraftSection] = []
        self._current: DraftSection | None = None
        self._count = 0

    def section(self, section_id: str, title: str) -> None:
        self._current = DraftSection(id=section_id, title=title)
        self.sections.append(self._current)

    def add(
        self,
        key: str,
        box: str | None,
        label: str,
        value: Any,
        certainty: Certainty,
        note: str = "",
        *,
        mandatory: bool = True,
        user_supplied: bool = False,
        official: bool = False,
        url: str | None = None,
        from_snapshot: bool = False,
    ) -> None:
        if self._current is None or self._count >= MAX_FIELDS:
            return
        text = _text(value, 400)
        if certainty == "unavailable":
            text = ""
        elif not text:
            # Değeri olmayan kutu "doğrulandı" ya da "kontrol edilecek" olamaz.
            certainty = "unavailable"
        self._current.fields.append(
            DraftField(
                key=key,
                box=box,
                label=label,
                value=text or None,
                certainty=certainty,
                mandatory=mandatory,
                user_supplied=user_supplied,
                official=official,
                note=note,
                source_url=url or (self.snapshot["url"] if from_snapshot and certainty == "verified" else None),
                source_date=self.snapshot["date"] if from_snapshot and certainty == "verified" else None,
                source_sha256=self.snapshot["sha256"] if from_snapshot and certainty == "verified" else None,
            )
        )
        self._count += 1

    def user(
        self,
        key: str,
        box: str | None,
        label: str,
        value: Any,
        note: str,
        *,
        mandatory: bool = True,
    ) -> None:
        """Yalnız beyan sahibinin bilebileceği kutu: doluysa ``check_required``, boşsa ``unavailable``."""
        text = _text(value, 400)
        self.add(
            key,
            box,
            label,
            text,
            "check_required" if text else "unavailable",
            note,
            mandatory=mandatory,
            user_supplied=True,
        )

    def fields(self) -> list[DraftField]:
        return [field for section in self.sections for field in section.fields]


def _declaration_section(builder: _Builder, inquiry: Any) -> None:
    export = builder.direction == "export"
    builder.section("declaration", "Beyan ve taraflar")
    builder.add(
        "regime_code",
        "1 / 37",
        "Rejim kodu",
        EXPORT_REGIME_CODE if export else IMPORT_REGIME_CODE,
        "check_required",
        "Varsayılan rejim kodudur. Dahilde/hariçte işleme, geçici ithalat, antrepo veya nihai kullanım "
        "rejimlerinde kod değişir; rejiminizi beyan öncesi doğrulayın.",
        url=GUMRUK_YONETMELIGI_URL,
    )
    builder.user(
        "declarant_tax_id",
        "14",
        "Beyan sahibi / temsilci vergi kimlik numarası",
        _get(inquiry, "declarant_tax_id"),
        "Beyan sahibinin vergi kimlik numarası; dolaylı temsilde müşavirlik firmasının numarası yazılır.",
    )
    builder.user(
        "customs_office_code",
        "A",
        "Beyannamenin tescil edileceği gümrük idaresi kodu",
        _get(inquiry, "customs_office_code"),
        "Dört haneli idare kodu (örn. 060500). Eşyanın bulunduğu yere ve rejime göre yetkili idare değişir.",
    )
    builder.user(
        "consignor_name",
        "2",
        "Gönderici / ihracatçı unvanı",
        _get(inquiry, "consignor_name"),
        "İhracatta Türkiye'deki ihracatçı, ithalatta yurt dışındaki satıcı yazılır; fatura ile birebir aynı olmalıdır.",
    )
    builder.user(
        "consignor_address",
        "2",
        "Gönderici / ihracatçı adresi",
        _get(inquiry, "consignor_address"),
        "Fatura ve taşıma belgesindeki adresle uyumlu olmalıdır.",
        mandatory=False,
    )
    builder.user(
        "consignee_name",
        "8",
        "Alıcı / ithalatçı unvanı",
        _get(inquiry, "consignee_name"),
        "İhracatta yurt dışındaki alıcı, ithalatta Türkiye'deki ithalatçı yazılır.",
    )
    builder.user(
        "consignee_address",
        "8",
        "Alıcı / ithalatçı adresi",
        _get(inquiry, "consignee_address"),
        "Fatura ve taşıma belgesindeki adresle uyumlu olmalıdır.",
        mandatory=False,
    )
    builder.user(
        "consignee_tax_id",
        "8",
        "Alıcı / ithalatçı kimlik numarası (VKN, EORI vb.)",
        _get(inquiry, "consignee_tax_id"),
        "İhracatta alıcının hedef ülkedeki kayıt numarası (AB'de EORI) beyannameyi açacak taraftan alınır; "
        "ithalatta ithalatçının vergi kimlik numarasıdır. Bu numara sistem tarafından üretilemez.",
    )


def _shipment_section(builder: _Builder, inquiry: Any, profile: Any) -> None:
    export = builder.direction == "export"
    builder.section("shipment", "Sevkiyat ve taşıma")
    destination = _text(_get(inquiry, "destination_country"), 100)
    dispatch = _text(_get(inquiry, "dispatch_country"), 100)
    builder.add(
        "dispatch_country",
        "15",
        "Sevk / ihracat ülkesi",
        "Türkiye" if export else dispatch,
        "check_required" if (export or dispatch) else "unavailable",
        "İhracat Türkiye'den yapılmaktadır."
        if export
        else "Eşyanın Türkiye'ye gönderildiği ülke; menşe ülkeden farklı olabilir ve A.TR ile serbest "
        "dolaşım sütununu bu belirler.",
        url=TICARET_EXPORT_URL if export else None,
    )
    builder.add(
        "destination_country",
        "17",
        "Varış ülkesi",
        destination if export else "Türkiye",
        "check_required" if (destination or not export) else "unavailable",
        "Eşyanın fiilen gireceği ülke; transit geçilen ülkeler yazılmaz."
        + (f" Hedef ülke veri düzeyi: {_text(_get(profile, 'badge_text'), 300)}" if export and profile is not None else ""),
    )
    builder.add(
        "trade_country",
        "11",
        "Ticaret yapılan ülke",
        destination if export else (dispatch or None),
        "check_required" if (destination if export else dispatch) else "unavailable",
        "Satış sözleşmesinin karşı tarafının yerleşik olduğu ülke; sevk ülkesinden farklı olabilir.",
        mandatory=False,
    )
    builder.user(
        "incoterm",
        "20",
        "Teslim şekli (Incoterm)",
        _get(inquiry, "incoterm"),
        "Teslim şekli istatistiki kıymeti ve navlun/sigorta sorumluluğunu belirler; sözleşmeyle uyumlu olmalıdır.",
    )
    builder.user(
        "delivery_place",
        "20",
        "Teslim yeri",
        _get(inquiry, "delivery_place"),
        "Incoterm'in geçerli olduğu yer (örn. 'FOB İzmir'); teslim şekliyle birlikte yazılır.",
        mandatory=False,
    )
    builder.user(
        "transport_mode",
        "25",
        "Sınırdaki taşıma şekli",
        _get(inquiry, "transport_mode"),
        "Denizyolu, karayolu, havayolu, demiryolu veya posta; Gümrük Yönetmeliği Ek-14 kod listesinden seçilir.",
    )
    builder.user(
        "transport_identity",
        "18 / 21",
        "Taşıtın kimliği ve kayıtlı olduğu ülke",
        _get(inquiry, "transport_identity"),
        "Gemi adı ve IMO, plaka, uçuş numarası veya vagon numarası; taşıma belgesinden alınır.",
        mandatory=False,
    )
    builder.user(
        "container_numbers",
        "19 / 31",
        "Konteyner numaraları",
        _get(inquiry, "container_numbers"),
        "Konteynerli taşımada her konteyner numarası yazılır; konteynersiz sevkiyatta kutu 19 '0' olur.",
        mandatory=False,
    )
    builder.user(
        "border_customs_office",
        "29",
        "Çıkış / giriş gümrük idaresi",
        _get(inquiry, "border_customs_office"),
        "İhracatta eşyanın Türkiye'yi terk edeceği, ithalatta Türkiye'ye girdiği sınır kapısı idaresi.",
        mandatory=False,
    )


def _goods_section(builder: _Builder, inquiry: Any, tariff_lookup: Any) -> None:
    builder.section("goods", "Eşya bilgileri")
    gtip = _text(_get(inquiry, "candidate_gtip"), 30)
    confirmed = bool(_get(inquiry, "exact_gtip_confirmed"))
    lookup_status = _text(_get(tariff_lookup, "status"), 40)
    match_mode = _text(_get(tariff_lookup, "match_mode"), 20)
    lookup_gtip = _text(_get(tariff_lookup, "gtip"), 30)
    # Kod, yalnız kullanıcı 12 haneyi onayladıysa VE aynı kod resmî cetvel anlık görüntüsünde
    # birebir eşleştiyse "resmî cetvelden okundu" sayılır. Sınıflandırma sorumluluğu yine beyan sahibindedir.
    from_snapshot = bool(
        confirmed
        and gtip
        and len(gtip) == 12
        and lookup_status == "matched"
        and match_mode == "exact"
        and lookup_gtip == gtip
        and builder.snapshot["sha256"]
    )
    builder.add(
        "item_number",
        "32",
        "Kalem numarası",
        "1",
        "check_required",
        "Bu taslak tek kalem için üretilir; birden fazla kalemde her kalem ayrı satır olarak açılmalıdır.",
        mandatory=False,
    )
    builder.add(
        "commodity_code",
        "33",
        "Eşyanın kod numarası (GTİP)",
        gtip,
        "verified" if from_snapshot else ("check_required" if gtip else "unavailable"),
        "Kod, yürürlükteki Türk Gümrük Tarife Cetveli anlık görüntüsünde birebir eşleşti. Sınıflandırma "
        "sorumluluğu beyan sahibindedir; bağlayıcı tespit için BTB gerekir."
        if from_snapshot
        else (
            "12 haneli kod henüz onaylanmadı ya da cetvelde birebir eşleşmedi; tescil öncesi ağaçtan "
            "kesinleştirin."
            if gtip
            else "GTİP seçilmeden beyanname açılamaz."
        ),
        official=True,
        from_snapshot=True,
    )
    builder.user(
        "goods_description",
        "31",
        "Kaplar ve eşyanın tanımı",
        _goods_description(inquiry, tariff_lookup),
        "Tanım eşyanın ticari adını ve ayırt edici evsafını taşımalı, fatura ile birebir uyumlu olmalıdır.",
    )
    builder.user(
        "origin_country",
        "34",
        "Menşe ülke",
        _get(inquiry, "origin_country"),
        "Menşe, eşyanın üretildiği veya son esaslı değişikliğe uğradığı ülkedir; sevk ülkesinden farklı "
        "olabilir ve tercihli oranı belirler.",
    )
    builder.user(
        "package_count",
        "6 / 31",
        "Kap adedi",
        _get(inquiry, "package_count"),
        "Toplam kap adedi taşıma belgesi ve çeki listesiyle uyumlu olmalıdır.",
    )
    builder.user(
        "package_kind",
        "31",
        "Kapların cinsi",
        _get(inquiry, "package_kind"),
        "Palet, koli, çuval, varil gibi kap cinsi; Gümrük Yönetmeliği Ek-14 kod listesinden seçilir.",
        mandatory=False,
    )
    builder.user(
        "package_marks",
        "31",
        "Ambalaj marka ve numaraları",
        _get(inquiry, "package_marks"),
        "Kaplar üzerindeki marka ve numaralar; taşıma belgesiyle uyumlu olmalıdır.",
        mandatory=False,
    )
    builder.user(
        "gross_weight_kg",
        "35",
        "Brüt ağırlık (kg)",
        _number(_get(inquiry, "gross_weight_kg")),
        "Ambalaj dahil toplam ağırlık; çeki listesi ve taşıma belgesiyle uyumlu olmalıdır.",
    )
    builder.user(
        "net_weight_kg",
        "38",
        "Net ağırlık (kg)",
        _number(_get(inquiry, "net_weight_kg")),
        "Ambalaj hariç eşya ağırlığı; brüt ağırlıktan büyük olamaz.",
    )
    builder.user(
        "supplementary_unit",
        "41",
        "Ek birim / miktar",
        _number(_get(inquiry, "quantity")),
        "Tarife cetvelinin o pozisyon için aradığı ölçü birimi (adet, metre, litre vb.) ile yazılır.",
        mandatory=False,
    )


def _value_section(builder: _Builder, inquiry: Any) -> None:
    builder.section("value", "Kıymet ve fatura")
    currency = _text(_get(inquiry, "currency"), 8)
    invoice_value = _get(inquiry, "invoice_value")
    builder.user(
        "invoice_number",
        "44",
        "Fatura numarası",
        _get(inquiry, "invoice_number"),
        "Beyannameye eklenecek ticari faturanın numarası.",
    )
    builder.user(
        "invoice_date",
        "44",
        "Fatura tarihi",
        _get(inquiry, "invoice_date"),
        "GG.AA.YYYY biçiminde; fatura ile birebir aynı olmalıdır.",
        mandatory=False,
    )
    builder.user(
        "invoice_total",
        "22",
        "Fatura bedeli ve döviz cinsi",
        f"{_number(invoice_value)} {currency}".strip() if invoice_value not in (None, "") else "",
        "Kıymet beyanı faturaya dayanır; iskonto, komisyon ve royalti gibi kıymet unsurları ayrıca beyan edilir.",
    )
    builder.user(
        "payment_method",
        "28",
        "Ödeme şekli",
        _get(inquiry, "payment_method_code") or _get(inquiry, "payment_method"),
        "Peşin, mal mukabili, vesaik mukabili veya akreditif; ithalatta KKDF yükümlülüğünü bu belirler.",
        mandatory=False,
    )
    builder.user(
        "freight_amount",
        "44",
        "Navlun",
        _number(_get(inquiry, "freight")),
        "Teslim şekli navlunu kapsamıyorsa kıymete ilave edilir; kapsıyorsa ayrıca eklenmez.",
        mandatory=False,
    )
    builder.user(
        "insurance_amount",
        "44",
        "Sigorta",
        _number(_get(inquiry, "insurance")),
        "Teslim şekli sigortayı kapsamıyorsa kıymete ilave edilir.",
        mandatory=False,
    )


def _tax_section(builder: _Builder, cost: Any) -> None:
    """Kutu 47 — yalnız ithalatta ve **asla** ``verified`` değil."""
    builder.section("taxes", "Vergilerin hesaplanması (kutu 47)")
    if builder.direction == "export":
        builder.add(
            "export_no_import_tax",
            "47",
            "İthalat vergileri",
            None,
            "unavailable",
            "İhracat beyannamesinde Türk ithalat vergileri (GV, İGV, EMY, KKDF, KDV, ÖTV) hesaplanmaz. "
            "Hedef ülkede doğacak vergiler o ülkenin beyannamesinde, ithalatçı tarafından beyan edilir.",
            mandatory=False,
        )
        builder.add(
            "export_vat_exemption",
            "44",
            "KDV istisnası",
            "KDVK md. 11/1-a ve 12 kapsamında ihracat istisnası",
            "check_required",
            "İhracat teslimlerinde KDV istisnası şarta bağlıdır; gümrük çıkış beyannamesinin kapanması ve "
            "bedelin yurda getirilmesi koşulları ayrıca aranır.",
            mandatory=False,
            url="https://www.gib.gov.tr/kdv-istisnalari",
        )
        return

    status = _text(_get(cost, "status"), 20)
    lines = _get(cost, "lines") or []
    currency = _text(_get(cost, "currency"), 8)
    if not lines:
        builder.add(
            "tax_lines",
            "47",
            "Vergi kalemleri",
            None,
            "unavailable",
            "Maliyet hesabı yapılamadı; oranlar doğrulanmadan vergi kutuları doldurulmamalıdır.",
        )
        return
    note = (
        "Tutar ön değerlendirme anındaki oran ve kurla hesaplandı. Beyannamede tescil tarihindeki "
        "GK md. 30 kuru ve o gün yürürlükteki oran esas alınır; tescil öncesi yeniden hesaplayın."
    )
    if status != "complete":
        note += " Hesap eksik kalemler içeriyor; doğrulanmamış oranlar tutar üretmez."
    for line in lines:
        code = _text(_get(line, "code"), 40)
        if not code:
            continue
        amount = _get(line, "amount")
        rate = _get(line, "rate")
        parts = []
        if amount is not None:
            parts.append(f"{_number(amount)} {currency}".strip())
        if rate is not None:
            parts.append(f"%{_number(rate)}")
        builder.add(
            f"tax_{code}",
            "47",
            _text(_get(line, "label"), 120) or code,
            " · ".join(parts),
            "check_required" if parts else "unavailable",
            note if parts else f"{_text(_get(line, 'formula'), 200) or 'Bu kalem doğrulanmadı'}; beyan öncesi doğrulayın.",
            mandatory=False,
        )
    total = _get(cost, "total_taxes")
    builder.add(
        "tax_total",
        "47",
        "Toplam vergi",
        f"{_number(total)} {currency}".strip() if total is not None else "",
        "check_required" if total is not None else "unavailable",
        note,
        mandatory=False,
    )


def _document_section(builder: _Builder, origin_documents: Any, control_lookup: Any, export_requirements: Any) -> None:
    builder.section("documents", "Sunulan belgeler ve izinler (kutu 44)")
    names: list[str] = []
    if builder.direction == "export":
        for document in _get(export_requirements, "proof_documents") or []:
            name = _text(_get(document, "name"), 160)
            if name:
                names.append(name)
        builder.add(
            "origin_proof",
            "44",
            "Düzenlenecek menşe / dolaşım belgesi",
            ", ".join(dict.fromkeys(names)),
            "check_required" if names else "unavailable",
            "Belge türü anlaşma ve fasıl kuralından türetildi; oda/gümrük vizesi şartı ve menşe kuralı "
            "ürün bazlıdır. Tercih talep edilmeyecekse bu belge zorunlu değildir."
            if names
            else "Hedef ülke ile tercihli anlaşma verimiz yok; menşe şahadetnamesi alıcının talebine bağlıdır.",
            mandatory=False,
        )
        builder.add(
            "export_control_documents",
            "44",
            "İhracat kontrol ve izin belgeleri",
            None,
            "unavailable",
            "İhracı yasak/ön izne bağlı mallar, TAREKS ihracat denetimi ve ikili kullanım listeleri "
            "sistemimizde indekslenmemiştir; Ticaret Bakanlığı'nın güncel listelerinden doğrulanmalıdır.",
            mandatory=False,
            url=TICARET_EXPORT_URL,
        )
        return

    for document in _get(origin_documents, "documents") or []:
        name = _text(_get(document, "name"), 160)
        if name:
            names.append(name)
    builder.add(
        "origin_proof",
        "44",
        "Menşe / dolaşım belgesi",
        ", ".join(dict.fromkeys(names)),
        "check_required" if names else "unavailable",
        "Tercihli oran ancak geçerli belge ibraz edilirse uygulanır; belge yoksa tercihsiz sütun geçerlidir."
        if names
        else "Sevk ve menşe ülkesi için tercihli belge kuralı çözülemedi.",
        mandatory=False,
    )
    control_names: list[str] = []
    for item in _get(control_lookup, "matches") or []:
        title = _text(_get(_get(item, "rule"), "title"), 160)
        if title:
            control_names.append(title)
    builder.add(
        "control_documents",
        "44",
        "Ürün güvenliği ve denetim belgeleri (TAREKS / ÜGD)",
        ", ".join(dict.fromkeys(control_names))[:400],
        "check_required" if control_names else "unavailable",
        "Kapsam listesi GTİP ön ekine göre eşleşti; nihai kapsam eşyanın evsafına bağlıdır ve TAREKS "
        "başvurusu tescil öncesi tamamlanmalıdır."
        if control_names
        else "Bu GTİP için denetim tebliği kapsam satırı eşleşmedi; kapsam dışı olduğu anlamına gelmez.",
        mandatory=False,
    )
    builder.add(
        "invoice_document",
        "44",
        "Ticari fatura ve taşıma belgesi",
        "Ticari fatura, taşıma belgesi (konşimento/CMR/AWB), çeki listesi",
        "check_required",
        "Beyannameye eklenecek asgari ticari belgeler; idare ek belge isteyebilir.",
        mandatory=False,
    )


# --- Hazırlık kapısı -------------------------------------------------------------------------


def assess_draft_readiness(fields: list[DraftField]) -> DeclarationReadiness:
    """Taslak yalnız her ZORUNLU kutu karşılanmışsa 'hazır' sayılır.

    Kutu türüne göre iki ayrı ölçüt:

    * **Resmî veriden gelmesi gereken kutu** (``official``, bugün yalnız GTİP):
      ``verified`` olmalıdır — onaylanmamış ya da cetvelde eşleşmemiş bir kod yeterli değildir.
    * **Diğer her kutu** (beyan sahibinin yazdığı ya da kuraldan türetilmiş varsayılan):
      ölçüt "dolu mu"dur; değeri olmayan zorunlu kutu kapıyı kilitler.

    Bu ayrım olmadan hiçbir dosya asla 'hazır' olamazdı: eşya tanımı, taraflar ve fatura
    makine tarafından doğrulanamaz, hedef ülke KDV'si ise tasarım gereği hiçbir zaman
    ``verified`` olmaz. Kapının anlamlı olması için ölçütün kutuya uyması gerekir.
    """
    verified = sum(1 for item in fields if item.certainty == "verified")
    checks = sum(1 for item in fields if item.certainty == "check_required")
    missing = sum(1 for item in fields if item.certainty == "unavailable")

    def blocks(item: DraftField) -> bool:
        if not item.mandatory:
            return False
        if item.certainty == "unavailable" or not item.value:
            return True
        return item.official and item.certainty != "verified"

    blocked_items = [item for item in fields if blocks(item)]
    blocking = [f"{item.label} (kutu {item.box})" if item.box else item.label for item in blocked_items]
    official_blocked = any(item.official for item in blocked_items)

    if not blocking:
        status: Literal["ready", "needs_check", "blocked"] = "ready"
        summary = (
            "Zorunlu beyanname kutularının tamamı dolduruldu ve resmî veriden gelmesi gereken kutular "
            "doğrulandı. Tescil öncesi son kontrol ve sorumluluk beyan sahibindedir."
        )
    elif official_blocked:
        status = "blocked"
        summary = (
            f"{len(blocking)} zorunlu kutu eksik ve bunlardan en az biri resmî cetvelden doğrulanamadı. "
            "GTİP ağaçtan kesinleştirilmeden bu taslak beyanname yazılımına aktarılmamalıdır."
        )
    else:
        status = "needs_check"
        summary = (
            f"{len(blocking)} zorunlu kutu sizin tarafınızdan doldurulmayı bekliyor. Eksikleri "
            "tamamladıktan sonra taslak aktarılabilir; taslak tek başına beyanname yerine geçmez."
        )
    return DeclarationReadiness(
        status=status,
        verified=verified,
        check_required=checks,
        unavailable=missing,
        blocking=blocking[:20],
        summary=summary,
    )


def _caveats(direction: str) -> list[str]:
    items = [
        "Bu taslak tek kalem varsayımıyla üretilir; çok kalemli beyannamede her kalem için kutu 31-46 "
        "ayrı ayrı doldurulmalıdır.",
        "Vergi tutarları tescil tarihindeki kur ve orana göre yeniden hesaplanmalıdır; taslaktaki tutarlar "
        "hiçbir koşulda doğrulanmış sayılmaz.",
        "Rejim kodu, gümrük idaresi kodu, kap cinsi ve taşıma şekli kodları Gümrük Yönetmeliği Ek-14 "
        "listelerinden seçilir; taslak bu kodları üretmez.",
    ]
    if direction == "export":
        items.append(
            "İhracatçı birliği kaydı, ihracı yasak/ön izne bağlı mallar ve ihracat kontrol listeleri "
            "sistemimizde izlenmez; bu kutular her dosyada resmî kaynaktan doğrulanmalıdır."
        )
    else:
        items.append(
            "TAREKS/ÜGD kapsamı GTİP ön ekine göre eşleşir; nihai kapsam eşyanın evsafına bağlıdır."
        )
    return items[:8]


def build_declaration_draft(result_like: Any) -> DeclarationDraft:
    """Ön değerlendirme sonucundan beyanname taslağı üretir. Hiçbir kutu uydurulmaz."""
    direction = "export" if _text(_get(result_like, "direction"), 10) == "export" else "import"
    inquiry = _get(result_like, "inquiry") or {}
    tariff_lookup = _get(result_like, "tariff_lookup")
    export_requirements = _get(result_like, "export_requirements")
    profile = _get(export_requirements, "destination")

    builder = _Builder(direction, _snapshot_reference(tariff_lookup))
    _declaration_section(builder, inquiry)
    _shipment_section(builder, inquiry, profile)
    _goods_section(builder, inquiry, tariff_lookup)
    _value_section(builder, inquiry)
    _tax_section(builder, _get(result_like, "deterministic_cost"))
    _document_section(
        builder,
        _get(result_like, "origin_documents"),
        _get(result_like, "control_lookup"),
        export_requirements,
    )

    return DeclarationDraft(
        direction=direction,
        regime_code=EXPORT_REGIME_CODE if direction == "export" else IMPORT_REGIME_CODE,
        regime_label=EXPORT_REGIME_LABEL if direction == "export" else IMPORT_REGIME_LABEL,
        generated_at=_now(),
        sections=builder.sections,
        readiness=assess_draft_readiness(builder.fields()),
        caveats=_caveats(direction),
        legal_notice=LEGAL_NOTICE,
    )


# --- Dışa aktarım ----------------------------------------------------------------------------


_CSV_HEADER = ("bolum", "kutu", "alan_kodu", "alan_adi", "deger", "kesinlik", "zorunlu", "not", "kaynak")


def draft_to_csv(draft: DeclarationDraft) -> str:
    """Beyanname yazılımına elle aktarım için düz CSV (Excel uyumlu, noktalı virgül)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(_CSV_HEADER)
    for section in draft.sections:
        for field in section.fields:
            writer.writerow(
                [
                    section.title,
                    field.box or "",
                    field.key,
                    field.label,
                    field.value or "",
                    field.certainty,
                    "evet" if field.mandatory else "hayır",
                    field.note,
                    field.source_url or "",
                ]
            )
    return buffer.getvalue()


def draft_to_xml(draft: DeclarationDraft) -> str:
    """Yapılandırılmış aktarım gövdesi. Bir BİLGE şeması değildir; alan taşıyıcıdır."""
    from xml.etree import ElementTree as ET

    root = ET.Element(
        "beyanname-taslagi",
        {
            "surum": draft.version,
            "yon": draft.direction,
            "rejim": draft.regime_code,
            "uretim": draft.generated_at,
        },
    )
    ET.SubElement(root, "yasal-uyari").text = draft.legal_notice
    hazirlik = ET.SubElement(root, "hazirlik", {"durum": draft.readiness.status})
    ET.SubElement(hazirlik, "ozet").text = draft.readiness.summary
    for item in draft.readiness.blocking:
        ET.SubElement(hazirlik, "eksik").text = item
    for section in draft.sections:
        node = ET.SubElement(root, "bolum", {"kod": section.id, "baslik": section.title})
        for field in section.fields:
            attrs = {"kod": field.key, "kesinlik": field.certainty, "zorunlu": "1" if field.mandatory else "0"}
            if field.box:
                attrs["kutu"] = field.box
            if field.source_sha256:
                attrs["sha256"] = field.source_sha256
            child = ET.SubElement(node, "alan", attrs)
            ET.SubElement(child, "ad").text = field.label
            if field.value:
                ET.SubElement(child, "deger").text = field.value
            if field.note:
                ET.SubElement(child, "not").text = field.note
            if field.source_url:
                ET.SubElement(child, "kaynak").text = field.source_url
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


__all__ = [
    "DRAFT_VERSION",
    "EXPORT_REGIME_CODE",
    "EXPORT_REGIME_LABEL",
    "IMPORT_REGIME_CODE",
    "IMPORT_REGIME_LABEL",
    "LEGAL_NOTICE",
    "DeclarationDraft",
    "DraftField",
    "DraftSection",
    "assess_draft_readiness",
    "build_declaration_draft",
    "draft_to_csv",
    "draft_to_xml",
]
