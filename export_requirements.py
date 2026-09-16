"""İhracat yönü için hedef ülke şartları ve Türkiye tarafı prosedür — saf kural tablosu.

Bu modül ``origin_documents.py`` ile aynı sözleşmeye sahiptir: **ağ yok, model yok,
rastgelelik yok.** Aynı girdi her zaman aynı çıktıyı verir.  Hedef ülkenin vergi
oranı buradan *çekilmez*; çağıran (``customs_advisor``) ilgili motoru sorgular ve
sonucu ``destination_duty`` olarak enjekte eder.

Üç tasarım kuralı — her biri kod okunarak doğrulanmış bir olguya dayanır:

1. **``Türkiye`` ``countries.py`` kayıt defterinde yoktur** (94 kayıt, hepsi yabancı;
   defterin docstring'i girdileri açıkça "Türkiye'ye ithal edilirken" diye tanımlar).
   Bu yüzden ihracatta Türkiye için ``find_country`` çağrılmaz ve yabancı motorlara
   daima ``EXPORTER_ISO2`` sabiti geçilir.  Türkiye'yi deftere eklemek
   ``by_regime`` / ``column_1_keys`` / ``explicit_labels`` / ``numeric_columns``
   üzerinden ithalat sütun çözümlemesini bozardı.

2. **Veri düzeyi (``DataTier``) uydurmayı yapısal olarak engeller.** ``destination_duty``
   yalnız ``rates`` düzeyinde dolu olabilir; diğer her düzeyde ``None`` kalır ve arayüz
   sayı yerine ``badge_text`` cümlesini basar.  Elimizde AB-27 (TARIC arşivi), Birleşik
   Krallık (resmî API) dışında oran verisi yoktur; İsviçre oran yayımlamaz; kalan
   ülkeler için yalnız anlaşma ve belge kuralı vardır.

3. **Yabancı oran Türkiye maliyet hesabına giremez.** İhracatta ``deterministic_cost``
   zaten ``None``'dır — hesap yolu hiç yoktur; burada da hiçbir tutar üretilmez.

Menşe ispat belgeleri ``countries.py``'deki anlaşma kayıtlarından türetilir ama ifade
**tersine çevrilir**: ithalatta belge *ibraz edilir*, ihracatta Türkiye tarafı
*düzenler*.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from countries import Country, REGISTRY_CHECKED_AT, country_key, find_country, PENDING_AGREEMENTS
from export_costing import ExportCostEstimate, build_export_cost
from origin_documents import CustomsUnionRoute, customs_union_route

# Türkiye kayıt defterinde yok; yabancı motorlara geçilecek tek gerçek kimlik bu sabittir.
EXPORTER_ISO2 = "TR"

DataTier = Literal["rates", "nomenclature", "agreement_only", "none"]
ExportEngine = Literal["eu_taric", "foreign_tariff_uk", "foreign_tariff_ch", "none"]

# Hedef ülkede açılacak beyanname yanlış doldurulursa ciddi zarar doğar. Bu yüzden her alan
# üç durumdan birini taşır ve hiçbir tahmin "doğrulandı" sayılmaz:
#   verified       – elimizdeki resmî anlık görüntüden okundu; kaynak ve tarih alanda taşınır.
#   check_required – kuraldan türetildi ya da veri eksik/bayat; ``note`` NEDENİNİ ve neyin
#                    kontrol edileceğini yazar.
#   unavailable    – bu veriye sahip değiliz; değer gösterilmez, yalnız nereye bakılacağı yazılır.
Certainty = Literal["verified", "check_required", "unavailable"]

TICARET_EXPORT_URL = "https://ticaret.gov.tr/ihracat"
TICARET_EXPORT_LEGISLATION_URL = "https://ticaret.gov.tr/ihracat/mevzuat"
TICARET_PRODUCT_SAFETY_URL = "https://ticaret.gov.tr/urun-guvenligi"
TICARET_FTA_URL = "https://ticaret.gov.tr/dis-iliskiler/serbest-ticaret-anlasmalari"
TIM_URL = "https://tim.org.tr"
GIB_VAT_EXPORT_URL = "https://www.gib.gov.tr/kdv-istisnalari"
EU_ACCESS2MARKETS_URL = "https://trade.ec.europa.eu/access-to-markets/tr/home"


class DestinationProfile(BaseModel):
    """Hedef ülke için hangi veriye sahip olduğumuzun tek doğruluk kaynağı."""

    country_input: str
    country_name: str | None = None
    iso2: str | None = None
    regime: Literal["eu", "efta", "fta", "pta", "kktc", "mfn"] | None = None
    regime_name: str | None = None
    agreement: str | None = None
    tier: DataTier
    engine: ExportEngine
    badge_text: str
    recognised: bool = False
    pending_note: str | None = None
    downgraded_from: DataTier | None = None


class ExportDocument(BaseModel):
    """İhracatta düzenlenecek/hazırlanacak tek bir belge veya işlem adımı."""

    code: str
    name: str
    applicability: str = Field(..., description="Belgenin ne zaman gerektiğine dair giriş düzeyi açıklama.")
    note: str = ""
    source_url: str | None = None


class MarketHint(BaseModel):
    """Onaylanmış evsaftan türeyen hedef pazar uygunluk ipucu — bulgu değil, ipucudur."""

    id: str
    title: str
    detail: str
    trigger_field: str
    trigger_value: str = ""
    source_url: str | None = None


class DeclarationField(BaseModel):
    """Hedef ülke beyannamesine girecek tek bir veri kalemi ve ne kadar emin olduğumuz."""

    key: str
    label: str
    value: str | None = None
    certainty: Certainty
    mandatory: bool = True
    # Yalnız beyan sahibinin bilebileceği kalemler (eşya tanımı, menşe beyanı, fatura,
    # alıcı kimliği) makine tarafından doğrulanamaz. Hazırlık kapısı bunları "dolu mu"
    # diye sorar, "resmî kaynaktan okundu mu" diye değil — bu ayrım olmadan hiçbir dosya
    # asla 'hazır' olamazdı ve kapı anlamsız bir süsten ibaret kalırdı.
    user_supplied: bool = False
    note: str = ""
    source_url: str | None = None
    source_date: str | None = None
    source_sha256: str | None = None


class DeclarationReadiness(BaseModel):
    """Dosyanın beyanname hazırlığına dair kapı kararı — 'hazır' yalnız her zorunlu alan doğrulandıysa."""

    status: Literal["ready", "needs_check", "blocked"]
    verified: int = 0
    check_required: int = 0
    unavailable: int = 0
    blocking: list[str] = Field(default_factory=list, max_length=20)
    summary: str = ""


class ExportRequirements(BaseModel):
    """İhracat ön değerlendirmesinin hedef ülke bloğu."""

    destination: DestinationProfile
    destination_duty: dict[str, Any] | None = None
    duty_source: dict[str, str] | None = None
    on_demand_lookup: dict[str, str] | None = None
    declaration_fields: list[DeclarationField] = Field(default_factory=list, max_length=24)
    readiness: DeclarationReadiness | None = None
    proof_documents: list[ExportDocument] = Field(default_factory=list, max_length=6)
    commercial_documents: list[str] = Field(default_factory=list, max_length=10)
    turkish_procedure: list[ExportDocument] = Field(default_factory=list, max_length=12)
    market_hints: list[MarketHint] = Field(default_factory=list, max_length=10)
    # Hedef ülke gümrük yükü; `rates` dışındaki kademede sebebini taşıyan bir
    # "hesaplanmadı" sonucudur, asla uydurma bir sayı değildir.
    cost: ExportCostEstimate | None = None
    caveats: list[str] = Field(default_factory=list, max_length=8)
    sources: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    checked_at: str = REGISTRY_CHECKED_AT


_REGIME_NAMES: dict[str, str] = {
    "eu": "AB Gümrük Birliği",
    "efta": "EFTA",
    "fta": "Serbest Ticaret Anlaşması",
    "pta": "Tercihli Ticaret Anlaşması",
    "kktc": "KKTC",
    "mfn": "Tercihsiz (anlaşma yok)",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def destination_profile(value: Any) -> DestinationProfile:
    """Hedef ülke adını veri düzeyine çevirir. Tek girdi: kullanıcının yazdığı ad."""
    raw = _text(value)
    country = find_country(raw) if raw else None
    if country is None:
        return DestinationProfile(
            country_input=raw,
            tier="none",
            engine="none",
            recognised=False,
            badge_text=(
                f'"{raw}" kayıtlı ülke listemizde yok. Ülke adını kontrol edin; bu ülke için '
                "ne vergi ne de anlaşma verimiz var."
                if raw
                else "Hedef ülke girilmedi; ülke seçilmeden hedef pazar şartları belirlenemez."
            ),
        )

    regime_name = _REGIME_NAMES.get(country.regime, country.regime)
    pending = PENDING_AGREEMENTS.get(country.key)
    base = {
        "country_input": raw,
        "country_name": country.name,
        "iso2": country.iso2,
        "regime": country.regime,
        "regime_name": regime_name,
        "agreement": country.agreement or None,
        "recognised": True,
        "pending_note": pending,
    }

    if country.regime == "eu":
        return DestinationProfile(
            **base,
            tier="rates",
            engine="eu_taric",
            badge_text="Hedef ülke vergi verisi: AB TARIC arşivinden okunur (Türk menşeli eşya için).",
        )
    if country.iso2 == "GB":
        return DestinationProfile(
            **base,
            tier="rates",
            engine="foreign_tariff_uk",
            badge_text="Hedef ülke vergi verisi: Birleşik Krallık resmî tarife API'sinden okunur.",
        )
    if country.iso2 == "CH":
        return DestinationProfile(
            **base,
            tier="nomenclature",
            engine="foreign_tariff_ch",
            badge_text=(
                "İsviçre vergi oranı yayımlamaz. Tarife numarası ve eşya tanımı gösterilir; "
                "oran için Tares ekranını kullanın."
            ),
        )
    agreement = country.agreement or regime_name
    return DestinationProfile(
        **base,
        tier="agreement_only",
        engine="none",
        badge_text=(
            f"Bu ülke için vergi oranı verimiz yok. Yalnız {agreement} kapsamındaki menşe ve belge "
            "kuralları gösterilir; oranı hedef ülkenin resmî tarife ekranından doğrulayın."
        ),
    )


def downgrade_profile(profile: DestinationProfile, *, reason: str, note: str) -> DestinationProfile:
    """Motor veri döndüremediğinde kademeyi düşürür — sayı yerine dürüst cümle kalır."""
    if profile.tier != "rates":
        return profile
    return profile.model_copy(
        update={
            "tier": "agreement_only",
            "downgraded_from": "rates",
            "badge_text": note,
            "engine": profile.engine if reason == "archive_miss" else "none",
        }
    )


def archive_miss_note(archived: int | None = None, total: int | None = None) -> str:
    """AB arşiv ıskası için rozet metni. İlk cümle 1/95 OKK'nın hukuki sonucudur, tahmin değil."""
    scope = ""
    if archived is not None and total:
        scope = f" (arşiv {archived}/{total} kod)"
    return (
        f"AB TARIC arşivimizde bu kod için satır yok{scope}. Gümrük Birliği kapsamındaki sanayi "
        "ürününde AB gümrük vergisi alınmaz; ek vergi, kota ve belge şartı için canlı sorgu gerekir."
    )


# --- Menşe / dolaşım belgeleri (ithalat kuralının tersine çevrilmiş hâli) --------------------

_ATR_EXPORT = ExportDocument(
    code="ATR",
    name="A.TR Dolaşım Belgesi",
    applicability="AB'ye gönderilen serbest dolaşımdaki sanayi ürününde ihracatçı düzenler; oda vizesi ve gümrük idaresi onayı alınır.",
    note="A.TR serbest dolaşımı kanıtlar, menşei kanıtlamaz. Menşe ayrıca sorulursa menşe şahadetnamesi veya tedarikçi beyanı gerekir.",
    source_url=TICARET_FTA_URL,
)
_EUR1_EXPORT = ExportDocument(
    code="EUR1",
    name="EUR.1 Hareket Belgesi",
    applicability="Tercihli menşe talep edilecekse ihracatçı düzenler; oda vizesi ve gümrük onayı gerekir.",
    note="Menşe kuralı ürün bazlıdır; anlaşmanın menşe eki doğrulanmadan tercih varsayılmamalıdır.",
    source_url=TICARET_FTA_URL,
)
_SUPPLIER_DECLARATION_EXPORT = ExportDocument(
    code="SUPPLIER_DECLARATION",
    name="Tedarikçi beyanı (üretici/satıcıdan)",
    applicability="Eşyayı kendiniz üretmediyseniz menşe veya serbest dolaşım durumunu tevsik için tedarikçinizden alınır.",
    note="A.TR veya EUR.1 düzenlenebilmesi için dayanak belgedir; ihracatçı dosyasında saklanır.",
    source_url=TICARET_FTA_URL,
)
_MFN_CERT_EXPORT = ExportDocument(
    code="CERT_ORIGIN",
    name="Menşe Şahadetnamesi (tercihsiz)",
    applicability="Alıcı veya hedef ülke mevzuatı isterse ticaret/sanayi odasınca düzenlenir.",
    note="Tercihsiz menşe belgesidir; hedef ülkede vergi indirimi sağlamaz.",
    source_url=TICARET_FTA_URL,
)
_GSP_NOTE = ExportDocument(
    code="GSP_ORIGIN",
    name="Form A / REX menşe belgesi (koşullu)",
    applicability="Yalnız hedef ülke Türkiye'yi kendi genel tercihler sisteminin (GSP) yararlanıcısı "
    "sayıyorsa düzenlenir; aksi hâlde bu belge ihracatçıdan istenmez.",
    note="Bir ülkenin hangi ülkelere GSP tanıdığı o ülkenin KENDİ mevzuatıdır ve ürün/dönem bazında "
    "değişir; Türkiye'de bunu gösteren resmî bir kayıt defteri yayımlanmadığı için burada tablo "
    "tutulmuyor. Alıcınızdan veya hedef ülkenin gümrük idaresinden teyit alın.",
    source_url=TICARET_FTA_URL,
)


def _preference_document_export(country: Country) -> ExportDocument:
    """Anlaşmanın kullandığı menşe ispat belgesi — ihracatçı gözünden."""
    if country.proof == "origin_declaration":
        return ExportDocument(
            code="ORIGIN_DECLARATION",
            name="Menşe beyanı (fatura veya ticari belge üzerinde, ihracatçı tarafından)",
            applicability=country.proof_note or "Tercih talep edilecekse ihracatçı fatura üzerinde menşe beyanı yazar.",
            note=f"{country.agreement}: EUR.1 düzenlenmez; beyan metni ve onaylanmış ihracatçı numarası anlaşma ekine uymalıdır.",
            source_url=TICARET_FTA_URL,
        )
    if country.proof == "agreement_certificate":
        return ExportDocument(
            code="AGREEMENT_CERT",
            name=f"Menşe ispat belgesi ({country.agreement})",
            applicability=country.proof_note or "Anlaşmaya özgü menşe ispat belgesi ihracatçı tarafından temin edilir.",
            note="Belge biçimi ve düzenleyen kurum anlaşma ekinden doğrulanmalıdır.",
            source_url=TICARET_FTA_URL,
        )
    return _EUR1_EXPORT.model_copy(
        update={
            "applicability": country.proof_note or _EUR1_EXPORT.applicability,
            "note": f"{country.agreement}: menşe kuralı ürün bazlıdır; anlaşma ek kuralları doğrulanmalıdır.",
        }
    )


def export_proof_documents(
    destination: Optional[Country], gtip: Any = None
) -> tuple[list[ExportDocument], list[str]]:
    """Hedef ülkeye göre Türkiye'nin düzenleyeceği menşe/dolaşım belgeleri ve uyarılar."""
    if destination is None:
        return [], []

    caveats: list[str] = []
    if destination.regime == "eu":
        route: CustomsUnionRoute | None = customs_union_route(gtip)
        if route == "eur1_agricultural":
            caveats.append("Tarım ürününde A.TR geçerli değildir; tercih için EUR.1/EUR-MED düzenlenir.")
            return [_EUR1_EXPORT, _SUPPLIER_DECLARATION_EXPORT], caveats
        if route == "eur1_ecsc":
            caveats.append("AKÇT (kömür-çelik) ürününde A.TR geçerli değildir; tercih için EUR.1 düzenlenir.")
            return [_EUR1_EXPORT, _SUPPLIER_DECLARATION_EXPORT], caveats
        if route is None:
            caveats.append(
                "Tarife kodu verilmediği için sanayi ürünü varsayıldı; tarım (1-24. fasıl) veya AKÇT "
                "ürününde düzenlenecek belge EUR.1'dir."
            )
        return [_ATR_EXPORT, _SUPPLIER_DECLARATION_EXPORT], caveats

    if destination.regime == "kktc":
        caveats.append("KKTC ile ticaret ayrı düzenlemeye tabidir; belge kuralı resmî kaynaktan doğrulanmalıdır.")
        return [_MFN_CERT_EXPORT], caveats

    if destination.regime == "mfn":
        caveats.append(
            f"{destination.name} ile yürürlükte tercihli ticaret anlaşması yoktur; hedef ülke Türk menşeli "
            "eşyaya kendi genel (MFN) oranını uygular."
        )
        return [_MFN_CERT_EXPORT, _GSP_NOTE], caveats

    documents = [_preference_document_export(destination), _SUPPLIER_DECLARATION_EXPORT]
    pending = PENDING_AGREEMENTS.get(destination.key)
    if pending:
        caveats.append(pending)
    return documents, caveats


# --- Türkiye tarafı ihracat prosedürü -------------------------------------------------------

_COMMERCIAL_DOCUMENTS = [
    "Ticari fatura (ihracat faturası)",
    "Taşıma belgesi (konşimento, CMR, havayolu irsaliyesi)",
    "Ambalaj listesi / çeki listesi",
    "Sigorta poliçesi (teslim şekli sorumluluğu size aitse)",
    "Alıcı/ithalatçı bilgileri ve varsa hedef ülkedeki yetkili temsilci",
]


def turkish_export_procedure(inquiry_like: Any = None) -> list[ExportDocument]:
    """Her ihracat dosyasında geçerli olan Türkiye tarafı işlem listesi.

    Bunların hiçbirinin durumu elimizdeki veriyle gözlemlenemez; bu yüzden liste
    sabittir ve her madde resmî kaynağa yönlendirir.
    """
    return [
        ExportDocument(
            code="EXPORTER_UNION",
            name="İhracatçı birliği üyeliği ve İBGS kaydı",
            applicability="Gümrük çıkış beyannamesi tescilinden önce ilgili ihracatçı birliğine üyelik ve kayıt gerekir.",
            note="Birlik verisi sistemimizde yok; üyelik durumu TİM/ilgili birlikten doğrulanmalıdır.",
            source_url=TIM_URL,
        ),
        ExportDocument(
            code="EXPORT_DECLARATION",
            name="Gümrük çıkış beyannamesi (GÇB) tescili",
            applicability="12 haneli GTİP, eşya tanımı, kıymet, teslim şekli ve alıcı bilgileriyle tescil edilir.",
            note="Beyanname 12 hane ister; 6 veya 8 haneli kod yeterli değildir.",
            source_url=TICARET_EXPORT_URL,
        ),
        ExportDocument(
            code="EXPORT_PRODUCT_CONTROL",
            name="İhracatta ürün güvenliği ve TAREKS denetimi",
            applicability="Bazı ürün gruplarında ihracat öncesi denetim veya TAREKS başvurusu zorunludur.",
            note="İhracat tarafı ürün denetim indeksimiz yok; ürün grubunuz için resmî tebliğ listesi kontrol edilmelidir.",
            source_url=TICARET_PRODUCT_SAFETY_URL,
        ),
        ExportDocument(
            code="EXPORT_PROHIBITION",
            name="İhracı yasak veya ön izne bağlı mallar kontrolü",
            applicability="Eşyanın ihracı yasak ya da bir kurumun ön iznine bağlı olabilir.",
            note="Bu listeler indekslenmemiştir; 'kapsam dışıdır' denemez, resmî listeden doğrulanmalıdır.",
            source_url=TICARET_EXPORT_LEGISLATION_URL,
        ),
        ExportDocument(
            code="DUAL_USE",
            name="İkili kullanım (dual-use) ve ihracat kontrol listeleri",
            applicability="Teknik ürünlerde ikili kullanım listesi ve yaptırım/ambargo kontrolü yapılmalıdır.",
            note="Kontrol listeleri sistemimizde yok; teknik özellikler resmî liste ile karşılaştırılmalıdır.",
            source_url=TICARET_EXPORT_LEGISLATION_URL,
        ),
        ExportDocument(
            code="ORIGIN_ISSUANCE",
            name="Menşe / dolaşım belgesi düzenleme ve vize",
            applicability="A.TR, EUR.1 veya menşe şahadetnamesi oda vizesi ve gümrük onayıyla düzenlenir.",
            note="Belgenin türü hedef ülkenin anlaşma durumuna göre yukarıdaki listede belirtilmiştir.",
            source_url=TICARET_FTA_URL,
        ),
        ExportDocument(
            code="VAT_EXEMPTION",
            name="İhracat KDV istisnası ve iade",
            applicability="İhracat teslimleri KDV'den istisnadır; iade için beyannamenin kapanması ve belge şartları aranır.",
            note="3065 sayılı KDV Kanunu m.11/1-a ve m.12. İade süreci sistemimizde izlenmez.",
            source_url=GIB_VAT_EXPORT_URL,
        ),
        ExportDocument(
            code="INCOTERM_PAYMENT",
            name="Teslim şekli, ödeme şekli, navlun ve sigorta sorumluluğu",
            applicability="Incoterm hangi tarafın navlun, sigorta ve gümrük masrafını üstlendiğini belirler.",
            note="Sözleşme ile beyannamedeki teslim şekli birbirini tutmalıdır.",
            source_url=TICARET_EXPORT_URL,
        ),
    ]


# --- Hedef pazar uygunluk ipuçları (onaylanmış evsaftan) -------------------------------------

_HINT_FIELDS = (
    "target_user",
    "intended_use",
    "product_category",
    "composition",
    "construction_form",
    "function_mechanism",
    "components_accessories",
    "label_text",
    "packaging",
)

_CE_MARKET = {"eu": ("CE", EU_ACCESS2MARKETS_URL), "gb": ("UKCA", "https://www.gov.uk/guidance/using-the-ukca-marking")}


def _market_scope(profile: DestinationProfile) -> tuple[str, str] | None:
    """İpuçlarının hangi pazar mevzuatına atıf yapacağını belirler."""
    if profile.regime == "eu":
        return _CE_MARKET["eu"]
    if profile.iso2 == "GB":
        return _CE_MARKET["gb"]
    return None


def _matches(haystack: str, needles: tuple[str, ...]) -> bool:
    # country_key Türkçe aksanları da ayıklar; "çocuk" ile "cocuk" aynı anahtara iner.
    normalised = country_key(haystack)
    return any(needle in normalised for needle in needles)


def market_hints(inquiry_like: Any, profile: DestinationProfile) -> list[MarketHint]:
    """Kullanıcının ONAYLADIĞI evsaflardan hedef pazar uygunluk ipuçları üretir.

    İpuçları bulgu değildir; ``required_documents`` listesine asla girmez. Her ipucu
    hangi evsaf alanından çıktığını (``trigger_field``) taşır ki arayüz gerekçeyi
    gösterebilsin.
    """
    data = inquiry_like if isinstance(inquiry_like, dict) else getattr(inquiry_like, "__dict__", {}) or {}
    values = {field: _text(data.get(field)) for field in _HINT_FIELDS}
    if not any(values.values()):
        return []

    scope = _market_scope(profile)
    mark, mark_url = scope if scope else ("", None)
    hints: list[MarketHint] = []

    def add(hint_id: str, field: str, title: str, detail: str, url: str | None = None) -> None:
        if any(item.id == hint_id for item in hints):
            return
        hints.append(
            MarketHint(
                id=hint_id,
                title=title,
                detail=detail,
                trigger_field=field,
                trigger_value=values.get(field, "")[:120],
                source_url=url or mark_url,
            )
        )

    child = ("cocuk", "bebek", "oyuncak", "child", "baby", "toy")
    for field in ("target_user", "intended_use", "product_category"):
        if values[field] and _matches(values[field], child):
            if profile.regime == "eu":
                add(
                    "toy_safety",
                    field,
                    "Oyuncak / çocuk ürünü güvenliği",
                    "AB Oyuncak Güvenliği Yönetmeliği (2009/48/AT) ve EN 71 serisi uygulanır; CE işareti ve "
                    "AB'de yerleşik bir iktisadi işletmeci (yetkili temsilci/ithalatçı) gerekir.",
                )
            elif profile.iso2 == "GB":
                add(
                    "toy_safety",
                    field,
                    "Oyuncak / çocuk ürünü güvenliği",
                    "Birleşik Krallık'ta UKCA işareti ve BK'de yerleşik yetkili temsilci aranır.",
                )
            else:
                add(
                    "toy_safety",
                    field,
                    "Çocuk ürünü güvenliği",
                    "Çocuk ürünlerinde hedef ülkenin kendi güvenlik ve etiketleme mevzuatı uygulanır; "
                    "hedef pazarın resmî kaynağından doğrulayın.",
                    TICARET_PRODUCT_SAFETY_URL,
                )
            break

    textile = ("pamuk", "polyester", "elyaf", "dokuma", "orme", "tekstil", "kumas", "cotton", "textile")
    for field in ("composition", "construction_form", "product_category"):
        if values[field] and _matches(values[field], textile):
            if profile.regime == "eu":
                add(
                    "textile_labelling",
                    field,
                    "Tekstil elyaf adlandırma ve etiketleme",
                    "AB 1007/2011 sayılı Tüzük elyaf bileşiminin etikette belirtilmesini zorunlu kılar.",
                )
            else:
                add(
                    "textile_labelling",
                    field,
                    "Tekstil etiketleme",
                    "Elyaf bileşimi ve bakım etiketi kuralları hedef ülkeye göre değişir; resmî kaynaktan doğrulayın.",
                    TICARET_PRODUCT_SAFETY_URL,
                )
            break

    electric = ("elektrik", "sarj", "batarya", "pil", "motor", "voltaj", "adaptor", "elektronik", "battery", "charger")
    for field in ("function_mechanism", "components_accessories", "product_category"):
        if values[field] and _matches(values[field], electric):
            if profile.regime == "eu":
                add(
                    "electrical_conformity",
                    field,
                    "Elektrikli ürün uygunluğu",
                    "Alçak Gerilim (2014/35/AB) ve EMC (2014/30/AB) yönetmelikleri, RoHS ve pil/atık pil "
                    "mevzuatı uygulanabilir; CE işareti ve AB uygunluk beyanı gerekir.",
                )
            elif profile.iso2 == "GB":
                add(
                    "electrical_conformity",
                    field,
                    "Elektrikli ürün uygunluğu",
                    "Birleşik Krallık'ta UKCA işareti ve BK uygunluk beyanı aranır.",
                )
            else:
                add(
                    "electrical_conformity",
                    field,
                    "Elektrikli ürün uygunluğu",
                    "Elektrikli ürünlerde hedef ülkenin güvenlik, EMC ve enerji etiketi kuralları uygulanır.",
                    TICARET_PRODUCT_SAFETY_URL,
                )
            break

    wood = ("ahsap", "palet", "tahta", "kereste", "wood", "pallet")
    if values["packaging"] and _matches(values["packaging"], wood):
        add(
            "ispm15",
            "packaging",
            "Ahşap ambalaj ısıl işlem damgası",
            "Ahşap ambalaj ve paletlerde ISPM-15 ısıl işlem (HT) damgası aranır; damgasız ahşap ambalaj "
            "sınırda geri çevrilebilir.",
            TICARET_EXPORT_URL,
        )

    if values["label_text"]:
        add(
            "label_language",
            "label_text",
            "Etiket dili ve zorunlu bilgiler",
            "Etiket hedef ülkenin dilinde olmalıdır; üretici/ithalatçı bilgisi, uyarılar ve varsa uygunluk "
            "işareti hedef pazar mevzuatına göre yenilenir.",
            mark_url or TICARET_PRODUCT_SAFETY_URL,
        )

    if mark and hints:
        for hint in hints:
            if mark not in hint.detail and hint.id != "ispm15":
                continue
    return hints[:10]


# --- Beyanname alanları ve hazırlık kapısı ---------------------------------------------------

_STALE_AFTER_DAYS = 90


def _duty_text(duty: dict[str, Any] | None, *keys: str) -> str | None:
    if not duty:
        return None
    for key in keys:
        value = duty.get(key)
        if isinstance(value, dict):
            value = value.get("rate") or value.get("duty_expression") or value.get("text")
        text = _text(value)
        if text:
            return text
    return None


def _age_days(value: Any) -> int | None:
    """Kaynak tarihinin kaç gün önce olduğunu döndürür; çözülemezse ``None``."""
    from datetime import datetime, timezone

    raw = _text(value)
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m"):
        try:
            parsed = datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt)
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - parsed).days)
    return None


def declaration_fields(
    inquiry_like: Any,
    profile: DestinationProfile,
    *,
    destination_duty: dict[str, Any] | None = None,
    duty_source: dict[str, str] | None = None,
    proof_documents: list[ExportDocument] | None = None,
    destination_vat: dict[str, Any] | None = None,
) -> list[DeclarationField]:
    """Hedef ülke ithalat beyannamesine girecek kalemler, her biri emin olma düzeyiyle.

    Kural: ``verified`` yalnız elimizdeki resmî anlık görüntüden **okunan** bir değere verilir.
    Kuraldan türetilen, kullanıcının yazdığı veya bayatlamış her kalem ``check_required``;
    hiç veri olmayan kalem ``unavailable`` olur ve **değer taşımaz**.
    """
    data = inquiry_like if isinstance(inquiry_like, dict) else getattr(inquiry_like, "__dict__", {}) or {}
    source_url = (duty_source or {}).get("url")
    source_date = (duty_source or {}).get("retrieved_at") or (duty_source or {}).get("fetched_at")
    source_sha = (duty_source or {}).get("sha256")
    age = _age_days(source_date)
    stale = age is not None and age > _STALE_AFTER_DAYS
    rate_certainty: Certainty = "verified" if (destination_duty and not stale) else (
        "check_required" if destination_duty else "unavailable"
    )
    stale_note = (
        f"Kaynak {age} gün önce alınmış ({_STALE_AFTER_DAYS} günden eski); beyanname öncesi tazeleyin."
        if stale
        else ""
    )

    def field(
        key: str,
        label: str,
        value: Any,
        certainty: Certainty,
        note: str = "",
        *,
        mandatory: bool = True,
        user_supplied: bool = False,
        url: str | None = None,
    ) -> DeclarationField:
        return DeclarationField(
            key=key,
            label=label,
            value=_text(value) or None if certainty != "unavailable" else None,
            certainty=certainty,
            mandatory=mandatory,
            user_supplied=user_supplied,
            note=note,
            source_url=url or (source_url if certainty == "verified" else None),
            source_date=source_date if certainty == "verified" else None,
            source_sha256=source_sha if certainty == "verified" else None,
        )

    matched = _duty_text(destination_duty, "matched_code", "cn_code")
    gtip = _text(data.get("candidate_gtip"))
    fields: list[DeclarationField] = []

    fields.append(
        field(
            "destination_code",
            "Hedef ülke tarife kodu",
            matched or (gtip[:8] if len(gtip) >= 8 else gtip),
            "verified" if matched and not stale else ("check_required" if gtip else "unavailable"),
            stale_note
            or (
                ""
                if matched
                else "Türk GTİP'inin ilk 6 hanesi hedef ülkede de aynıdır; 7. haneden sonrası "
                "ulusaldır ve hedef ülkenin kendi cetvelinden seçilmelidir."
            ),
        )
    )
    fields.append(
        field(
            "goods_description",
            "Eşya tanımı (ticari)",
            _duty_text(destination_duty, "goods_description") or _text(data.get("declared_product_type")) or _text(data.get("product_description"))[:180],
            "check_required",
            "Beyannamedeki tanım eşyanın ticari adını ve ayırt edici evsafını taşımalı; fatura ile birebir uyumlu olmalıdır.",
            user_supplied=True,
        )
    )
    fields.append(
        field(
            "origin_country",
            "Menşe ülke",
            _text(data.get("origin_country")) or "Türkiye",
            "check_required",
            "Menşe, üretimin gerçekleştiği ülkedir ve tercihli oranı belirler; menşe kuralı ürün bazlıdır.",
            user_supplied=True,
        )
    )
    fields.append(
        field(
            "dispatch_country",
            "Sevk / ihracat ülkesi",
            "Türkiye",
            "verified" if profile.recognised else "check_required",
            "İhracat Türkiye'den yapılmaktadır.",
            url=TICARET_EXPORT_URL,
        )
    )

    third = _duty_text(destination_duty, "third_country_duty", "mfn_rate")
    fields.append(
        field(
            "third_country_duty",
            "Üçüncü ülke (MFN) gümrük vergisi",
            third,
            rate_certainty if third else "unavailable",
            stale_note or ("" if third else profile.badge_text),
        )
    )
    preferential = _duty_text(destination_duty, "origin_preference", "partner_rate")
    fields.append(
        field(
            "preferential_duty",
            "Türk menşeli eşyaya tercihli oran",
            preferential,
            rate_certainty if preferential else "unavailable",
            stale_note
            or (
                "Tercihli oran ancak geçerli menşe/dolaşım belgesi ibraz edilirse uygulanır."
                if preferential
                else profile.badge_text
            ),
            # Tercih talep etmek ihracatçının seçimidir; talep edilmezse MFN oranı uygulanır.
            # Bu yüzden zorunlu kutu değildir.
            mandatory=False,
        )
    )
    additional = destination_duty.get("additional_duties") if destination_duty else None
    fields.append(
        field(
            "additional_duties",
            "Ek vergi / damping / korunma önlemi",
            ", ".join(str(item) for item in additional) if isinstance(additional, list) and additional else None,
            "verified" if additional and not stale else ("check_required" if destination_duty else "unavailable"),
            stale_note
            or (
                "Bu kod için ek vergi satırı okunmadı; damping ve korunma önlemleri kod ve menşe bazlıdır, doğrulayın."
                if destination_duty and not additional
                else profile.badge_text if not destination_duty else ""
            ),
            mandatory=False,
        )
    )
    required_docs = destination_duty.get("required_documents") if destination_duty else None
    fields.append(
        field(
            "required_documents",
            "Hedef ülkenin aradığı belge kodları",
            ", ".join(str(item) for item in required_docs) if isinstance(required_docs, list) and required_docs else None,
            "verified" if required_docs and not stale else ("check_required" if destination_duty else "unavailable"),
            stale_note or ("" if required_docs else "Belge şartı kod ve menşe bazlıdır; hedef ülkenin tarife ekranından doğrulayın."),
            # Boş liste de geçerli bir cevaptır (kod için belge şartı olmayabilir); kapıyı kilitlemez.
            mandatory=False,
        )
    )

    proof = (proof_documents or [None])[0]
    fields.append(
        field(
            "preference_proof",
            "Tercih için ibraz edilecek menşe/dolaşım belgesi",
            proof.name if proof else None,
            "check_required" if proof else "unavailable",
            "Belge türü anlaşma ve fasıl kuralından türetildi; oda/gümrük vizesi şartı ve menşe kuralı ürün bazlıdır."
            if proof
            else "Hedef ülke ile tercihli anlaşma verimiz yok.",
            mandatory=False,
            url=TICARET_FTA_URL,
        )
    )

    # Hedef ülke KDV'si: AB-27 için tohum veriden gelir, kalan ülkelerde veri yoktur.
    # ASLA "verified" olamaz — hangi oranın gerçekten uygulanacağını üye devletin kendi
    # mevzuatı belirler ve tohum satırları doğrulanmamıştır. Bu, oranın beyannameye
    # kontrolsüz girmesini engelleyen yapısal güvencedir.
    if destination_vat and destination_vat.get("standard") is not None:
        standard = destination_vat["standard"]
        applicable = destination_vat.get("applicable")
        if destination_vat.get("applicable_basis") == "chapter_rule" and applicable != standard:
            vat_value = f"Standart %{standard} · bu fasıl için indirimli %{applicable} uygulanabilir"
        else:
            vat_value = f"Standart %{standard}"
        fields.append(
            field(
                "destination_vat",
                "Hedef ülke KDV oranı",
                vat_value,
                "check_required",
                str(destination_vat.get("note") or ""),
                mandatory=False,
                url=destination_vat.get("authority_url") or destination_vat.get("source_url"),
            )
        )
    else:
        fields.append(
            field(
                "destination_vat",
                "Hedef ülke KDV / iç vergi oranı",
                None,
                "unavailable",
                str(destination_vat.get("note")) if destination_vat else
                "Hedef ülkenin KDV ve iç vergi oranları veri kaynaklarımızda yok; ithalatçınızdan veya hedef "
                "ülkenin vergi idaresinden doğrulanmalıdır.",
                mandatory=False,
            )
        )
    fields.append(
        field(
            "incoterm",
            "Teslim şekli (Incoterm)",
            _text(data.get("incoterm")),
            "check_required" if _text(data.get("incoterm")) else "unavailable",
            "Incoterm, beyannamedeki istatistiki kıymeti ve navlun/sigorta sorumluluğunu belirler; sözleşmeyle uyumlu olmalıdır.",
            user_supplied=True,
        )
    )
    invoice = data.get("invoice_value")
    fields.append(
        field(
            "invoice_value",
            "Fatura bedeli ve döviz cinsi",
            f"{invoice} {_text(data.get('currency')) or ''}".strip() if invoice not in (None, "") else None,
            "check_required" if invoice not in (None, "") else "unavailable",
            "Kıymet beyanı faturaya dayanır; hedef ülke kıymeti kendi kurallarına göre yeniden hesaplayabilir.",
            user_supplied=True,
        )
    )
    # Alıcının kayıt numarasını sistem üretemez — ama kullanıcı girebilir. Alan bu numara
    # girilene kadar ``unavailable`` kalır; girildiğinde ``check_required`` olur ve hazırlık
    # kapısı artık yapısal olarak kilitli kalmaz.
    consignee_tax_id = _text(data.get("consignee_tax_id"))
    fields.append(
        field(
            "importer_identity",
            "Alıcı / ithalatçı kimlik numarası (EORI vb.)",
            consignee_tax_id,
            "check_required" if consignee_tax_id else "unavailable",
            "Numara alıcıdan alındığı gibi yazıldı; hedef ülkenin kayıt sisteminde geçerli olduğunu "
            "alıcınıza teyit ettirin."
            if consignee_tax_id
            else "İthalatçının hedef ülkedeki kayıt numarası bizde yok; beyannameyi açacak taraftan alınmalıdır.",
            user_supplied=True,
        )
    )
    return fields[:24]


def _is_blocking(item: DeclarationField) -> bool:
    """Zorunlu bir alanın hazırlık kapısını kilitleyip kilitlemediği.

    İki farklı ölçüt, çünkü iki farklı alan türü var:

    * **Resmî veriden gelmesi gereken alan** (hedef ülke kodu, üçüncü ülke vergisi):
      ``verified`` olmalıdır — kuraldan türetilmiş ya da bayat bir değer yeterli değildir.
    * **Yalnız beyan sahibinin bilebileceği alan** (eşya tanımı, menşe beyanı, fatura,
      alıcının kayıt numarası): makine tarafından doğrulanamaz; ölçüt "dolu mu"dur.
    """
    if item.user_supplied:
        return not item.value
    return item.certainty != "verified"


def assess_readiness(fields: list[DeclarationField], profile: DestinationProfile) -> DeclarationReadiness:
    """Dosya yalnız her ZORUNLU alan karşılanmışsa 'beyannameye hazır' sayılır."""
    verified = sum(1 for item in fields if item.certainty == "verified")
    checks = sum(1 for item in fields if item.certainty == "check_required")
    missing = sum(1 for item in fields if item.certainty == "unavailable")
    blocking = [item.label for item in fields if item.mandatory and _is_blocking(item)]

    if not blocking:
        status: Literal["ready", "needs_check", "blocked"] = "ready"
        summary = (
            "Zorunlu beyanname alanlarının tamamı resmî kaynaktan doğrulandı. Yine de beyannameyi açacak "
            "taraf kendi mevzuatına göre son kontrolü yapmalıdır."
        )
    elif profile.tier == "rates":
        status = "needs_check"
        summary = (
            f"{len(blocking)} zorunlu alan doğrulanmadı. İşaretli her alan beyanname öncesi kontrol "
            "edilmeli; bu dosya tek başına beyanname yerine geçmez."
        )
    else:
        status = "blocked"
        summary = (
            f"Hedef ülke için oran verimiz olmadığından {len(blocking)} zorunlu alan doğrulanamadı. "
            "Bu dosyayla beyanname doldurulmamalı; hedef ülkenin resmî tarife ekranı kullanılmalıdır."
        )
    return DeclarationReadiness(
        status=status,
        verified=verified,
        check_required=checks,
        unavailable=missing,
        blocking=blocking[:20],
        summary=summary,
    )


# --- Birleştirme -----------------------------------------------------------------------------


def _caveats(profile: DestinationProfile) -> list[str]:
    items = [
        "Bu liste belge ve işlem hazırlığı için giriş düzeyi kılavuzdur; bağlayıcı tarife veya menşe "
        "kararı değildir.",
        "Hedef ülkenin ithalat şartları ürün bazlıdır ve değişebilir; beyanname öncesi alıcınızla ve "
        "hedef ülkenin resmî tarife ekranıyla doğrulayın.",
    ]
    if profile.tier != "rates":
        items.append(profile.badge_text)
    if profile.pending_note:
        items.append(profile.pending_note)
    items.append(
        "İhracatçı birliği kaydı, ihracı yasak/ön izne bağlı mallar, ihracat kontrol listeleri ve KDV "
        "iadesi sistemimizde izlenmez; bu adımlar her dosyada doğrulanmalıdır."
    )
    return items[:8]


def _sources(profile: DestinationProfile) -> list[dict[str, str]]:
    items = [
        {"title": "Ticaret Bakanlığı – İhracat", "url": TICARET_EXPORT_URL},
        {"title": "Ticaret Bakanlığı – İhracat mevzuatı", "url": TICARET_EXPORT_LEGISLATION_URL},
        {"title": "Ticaret Bakanlığı – Yürürlükteki STA'lar", "url": TICARET_FTA_URL},
        {"title": "GİB – İhracat istisnası", "url": GIB_VAT_EXPORT_URL},
    ]
    if profile.regime == "eu":
        items.append({"title": "AB Access2Markets – My Trade Assistant", "url": EU_ACCESS2MARKETS_URL})
    return items[:8]


def build_export_requirements(
    inquiry_like: Any,
    *,
    profile: DestinationProfile | None = None,
    destination_duty: dict[str, Any] | None = None,
    duty_source: dict[str, str] | None = None,
    on_demand_lookup: dict[str, str] | None = None,
    destination_vat: dict[str, Any] | None = None,
    preference_proof_confirmed: bool = False,
) -> ExportRequirements:
    """Hedef ülke bloğunu birleştirir. ``destination_duty`` yalnız ``rates`` düzeyinde kabul edilir."""
    data = inquiry_like if isinstance(inquiry_like, dict) else getattr(inquiry_like, "__dict__", {}) or {}
    if profile is None:
        profile = destination_profile(data.get("destination_country"))

    country = find_country(profile.country_input) if profile.recognised else None
    documents, doc_caveats = export_proof_documents(country, data.get("candidate_gtip"))

    if profile.tier != "rates":
        # Yapısal güvence: oran yalnız gerçekten veri olan kademede taşınabilir.
        destination_duty = None
        duty_source = None

    fields = declaration_fields(
        data,
        profile,
        destination_duty=destination_duty,
        duty_source=duty_source,
        proof_documents=documents,
        destination_vat=destination_vat,
    )
    readiness = assess_readiness(fields, profile)
    # `destination_duty` yukarıda kademeye göre zaten temizlendi; maliyet bu yüzden
    # yalnız gerçekten oran verisi olan ülkede sayı üretebilir.
    cost = build_export_cost(
        data,
        profile,
        destination_duty=destination_duty,
        duty_source=duty_source,
        destination_vat=destination_vat,
        preference_proof_confirmed=preference_proof_confirmed,
    )

    caveats = doc_caveats + _caveats(profile)
    return ExportRequirements(
        destination=profile,
        destination_duty=destination_duty,
        duty_source=duty_source,
        on_demand_lookup=on_demand_lookup,
        declaration_fields=fields,
        readiness=readiness,
        proof_documents=documents,
        commercial_documents=list(_COMMERCIAL_DOCUMENTS),
        turkish_procedure=turkish_export_procedure(data),
        market_hints=market_hints(data, profile),
        cost=cost,
        caveats=caveats[:8],
        sources=_sources(profile),
    )


__all__ = [
    "EXPORTER_ISO2",
    "Certainty",
    "DataTier",
    "DeclarationField",
    "DeclarationReadiness",
    "DestinationProfile",
    "ExportCostEstimate",
    "ExportDocument",
    "ExportRequirements",
    "MarketHint",
    "archive_miss_note",
    "assess_readiness",
    "build_export_requirements",
    "declaration_fields",
    "destination_profile",
    "downgrade_profile",
    "export_proof_documents",
    "market_hints",
    "turkish_export_procedure",
]
