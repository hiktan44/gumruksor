"""Single source of truth for origin countries used by the tariff and origin-document modules.

Each entry records how the country is treated when goods are imported into Türkiye:

* ``regime``       – ``eu`` (Customs Union), ``efta``, ``fta`` (free-trade agreement in force),
                     ``pta`` (preferential trade agreement), ``kktc``, ``mfn`` (no preference).
* ``column_1``     – the country is listed in column 1 ("AB, EFTA ve STA ülkeleri") of the
                     numeric industrial tables of the Import Regime Decree and the İGV decree.
* ``label``        – the country's own column token where an official list gives it one
                     (``G.KORE``, ``MLZ``, ``BK``, ``B-HER``...).
* ``proof``        – the proof-of-origin document the agreement uses; intake guidance only.

The registry mirrors the ministry's in-force FTA list as scraped on 2026-09-04 and must be
re-checked against https://ticaret.gov.tr/dis-iliskiler/serbest-ticaret-anlasmalari when the
list changes.  Countries that are not in the registry resolve to ``None`` so that callers can
warn instead of silently using the residual column.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Regime = Literal["eu", "efta", "fta", "pta", "kktc", "mfn"]
ProofKind = Literal["atr_or_eur1", "eur1", "eur1_or_invoice", "origin_declaration", "agreement_certificate", "none", "special"]

REGISTRY_CHECKED_AT = "2026-09-04"


def country_key(value: Any) -> str:
    """Normalise a country name for matching: casefold, strip diacritics and punctuation noise."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ı", "i"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.replace("-", " ").replace("_", " ").replace(".", " ").split())


@dataclass(frozen=True)
class Country:
    key: str
    name: str
    iso2: str
    regime: Regime
    aliases: tuple[str, ...] = ()
    column_1: bool = False
    label: str | None = None
    numeric_column: str | None = None
    proof: ProofKind = "none"
    proof_note: str = ""
    agreement: str = ""

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(country_key(item) for item in (self.key, self.name, *self.aliases)))


_EUR1_INVOICE = "EUR.1 Hareket Belgesi; eşik altı gönderi veya onaylanmış ihracatçı için fatura beyanı"


def _eu(key: str, name: str, iso2: str, *aliases: str) -> Country:
    return Country(key, name, iso2, "eu", aliases, column_1=True, label="AB", proof="atr_or_eur1",
                   agreement="Türkiye – AB Gümrük Birliği (1/95 OKK); tarım için 1/98 OKK")


def _efta(key: str, name: str, iso2: str, *aliases: str) -> Country:
    return Country(key, name, iso2, "efta", aliases, column_1=True, label="EFTA", proof="eur1_or_invoice",
                   proof_note=_EUR1_INVOICE + " (EUR-MED mümkündür)", agreement="Türkiye – EFTA STA")


def _fta(key: str, name: str, iso2: str, *aliases: str, label: str | None = None, numeric: str | None = None,
         column_1: bool = True, proof: ProofKind = "eur1_or_invoice", proof_note: str = _EUR1_INVOICE,
         agreement: str = "Türkiye – Serbest Ticaret Anlaşması") -> Country:
    return Country(key, name, iso2, "fta", aliases, column_1=column_1, label=label, numeric_column=numeric,
                   proof=proof, proof_note=proof_note, agreement=agreement)


def _mfn(key: str, name: str, iso2: str, *aliases: str) -> Country:
    return Country(key, name, iso2, "mfn", aliases)


COUNTRIES: tuple[Country, ...] = (
    # --- Avrupa Birliği (27) -------------------------------------------------------------
    _eu("almanya", "Almanya", "DE", "germany", "deutschland", "federal almanya"),
    _eu("avusturya", "Avusturya", "AT", "austria"),
    _eu("belcika", "Belçika", "BE", "belgium"),
    _eu("bulgaristan", "Bulgaristan", "BG", "bulgaria"),
    _eu("cekya", "Çekya", "CZ", "cek cumhuriyeti", "czechia", "czech republic"),
    _eu("danimarka", "Danimarka", "DK", "denmark"),
    _eu("estonya", "Estonya", "EE", "estonia"),
    _eu("finlandiya", "Finlandiya", "FI", "finland"),
    _eu("fransa", "Fransa", "FR", "france"),
    _eu("hirvatistan", "Hırvatistan", "HR", "croatia"),
    _eu("hollanda", "Hollanda", "NL", "netherlands", "the netherlands"),
    _eu("irlanda", "İrlanda", "IE", "ireland"),
    _eu("ispanya", "İspanya", "ES", "spain"),
    _eu("isvec", "İsveç", "SE", "sweden"),
    _eu("italya", "İtalya", "IT", "italy"),
    _eu("kibris", "Kıbrıs (GKRY)", "CY", "guney kibris", "guney kibris rum yonetimi", "gkry", "cyprus"),
    _eu("letonya", "Letonya", "LV", "latvia"),
    _eu("litvanya", "Litvanya", "LT", "lithuania"),
    _eu("luksemburg", "Lüksemburg", "LU", "luxembourg"),
    _eu("macaristan", "Macaristan", "HU", "hungary"),
    _eu("malta", "Malta", "MT"),
    _eu("polonya", "Polonya", "PL", "poland"),
    _eu("portekiz", "Portekiz", "PT", "portugal"),
    _eu("romanya", "Romanya", "RO", "romania"),
    _eu("slovakya", "Slovakya", "SK", "slovakia"),
    _eu("slovenya", "Slovenya", "SI", "slovenia"),
    _eu("yunanistan", "Yunanistan", "GR", "greece"),
    # --- EFTA -----------------------------------------------------------------------------
    _efta("isvicre", "İsviçre", "CH", "switzerland"),
    _efta("norvec", "Norveç", "NO", "norway"),
    _efta("izlanda", "İzlanda", "IS", "iceland"),
    _efta("lihtenstayn", "Lihtenştayn", "LI", "liechtenstein"),
    # --- Yürürlükteki STA'lar (Bakanlık listesi, 04.09.2026) --------------------------------
    _fta("arnavutluk", "Arnavutluk", "AL", "albania"),
    _fta("birlesik arap emirlikleri", "Birleşik Arap Emirlikleri", "AE", "bae", "united arab emirates", "uae",
         label="BAE", numeric="3", column_1=False, proof="agreement_certificate",
         proof_note="CEPA menşe belgesi veya onaylanmış ihracatçı menşe beyanı (anlaşma ekinden doğrulayın)",
         agreement="Türkiye – BAE Kapsamlı Ekonomik Ortaklık Anlaşması"),
    _fta("birlesik krallik", "Birleşik Krallık", "GB", "ingiltere", "united kingdom", "uk", "great britain", "england",
         label="BK", proof="origin_declaration",
         proof_note="İhracatçının fatura/ticari belge üzerindeki menşe beyanı; EUR.1 düzenlenmez",
         agreement="Türkiye – Birleşik Krallık STA"),
    _fta("bosna hersek", "Bosna-Hersek", "BA", "bosna-hersek", "bosnia and herzegovina", "bosnia", label="B-HER"),
    _fta("faroe adalari", "Faroe Adaları", "FO", "faroe islands", "faroe", label="F.ADA"),
    _fta("fas", "Fas", "MA", "morocco"),
    _fta("filistin", "Filistin", "PS", "palestine"),
    _fta("guney kore", "Güney Kore", "KR", "kore cumhuriyeti", "kore", "south korea", "republic of korea", "korea",
         label="G.KORE", proof="origin_declaration",
         proof_note="İhracatçının menşe beyanı (fatura üzeri); EUR.1 kullanılmaz",
         agreement="Türkiye – Kore Cumhuriyeti STA"),
    _fta("gurcistan", "Gürcistan", "GE", "georgia", label="GÜR"),
    _fta("israil", "İsrail", "IL", "israel"),
    _fta("karadag", "Karadağ", "ME", "montenegro"),
    _fta("katar", "Katar", "QA", "qatar", numeric="2", column_1=False, proof="agreement_certificate",
         proof_note="Anlaşmaya özgü menşe ispat belgesi (EUR.1 veya menşe beyanı; anlaşma ekinden doğrulayın)",
         agreement="Türkiye – Katar STA"),
    _fta("kosova", "Kosova", "XK", "kosovo", label="KOS"),
    _fta("kuzey makedonya", "Kuzey Makedonya", "MK", "makedonya", "north macedonia", "macedonia"),
    _fta("malezya", "Malezya", "MY", "malaysia", label="MLZ", proof="agreement_certificate",
         proof_note="Türkiye–Malezya STA menşe belgesi (yetkili kurumca düzenlenir)",
         agreement="Türkiye – Malezya STA"),
    _fta("misir", "Mısır", "EG", "egypt"),
    _fta("moldova", "Moldova", "MD"),
    _fta("morityus", "Morityus", "MU", "mauritius"),
    _fta("sirbistan", "Sırbistan", "RS", "serbia"),
    _fta("singapur", "Singapur", "SG", "singapore", label="SNG", proof="origin_declaration",
         proof_note="İhracatçının menşe beyanı; EUR.1 kullanılmaz", agreement="Türkiye – Singapur STA"),
    _fta("sili", "Şili", "CL", "chile"),
    _fta("tunus", "Tunus", "TN", "tunisia"),
    _fta("venezuela", "Venezuela", "VE", "bolivarci venezuela cumhuriyeti", label="VNZ", proof="agreement_certificate",
         proof_note="Türkiye–Venezuela Ticaretin Geliştirilmesi Anlaşması menşe belgesi",
         agreement="Türkiye – Venezuela Ticaretin Geliştirilmesi Anlaşması"),
    # --- Tercihli ticaret anlaşması ---------------------------------------------------------
    Country("iran", "İran", "IR", "pta", ("iran islam cumhuriyeti", "islamic republic of iran"), label="İRAN",
            proof="agreement_certificate", proof_note="Türkiye–İran Tercihli Ticaret Anlaşması menşe ispat belgesi",
            agreement="Türkiye – İran Tercihli Ticaret Anlaşması"),
    # --- KKTC --------------------------------------------------------------------------------
    Country("kktc", "KKTC", "CY", "kktc", ("kuzey kibris turk cumhuriyeti", "kuzey kibris", "northern cyprus"),
            proof="special", proof_note="KKTC ile ticaret ayrı düzenlemeye tabidir"),
    # --- Tercihsiz (sık kullanılan) ----------------------------------------------------------
    _mfn("cin", "Çin", "CN", "cin halk cumhuriyeti", "china", "people's republic of china", "prc"),
    _mfn("abd", "ABD", "US", "amerika birlesik devletleri", "amerika", "united states", "usa", "united states of america"),
    _mfn("hindistan", "Hindistan", "IN", "india"),
    _mfn("japonya", "Japonya", "JP", "japan"),
    _mfn("rusya", "Rusya", "RU", "rusya federasyonu", "russia", "russian federation"),
    _mfn("ukrayna", "Ukrayna", "UA", "ukraine"),
    _mfn("tayvan", "Tayvan", "TW", "taiwan", "çin tayvanı", "cin tayvani", "chinese taipei", "tayvan çin"),
    _mfn("hong kong", "Hong Kong", "HK"),
    _mfn("vietnam", "Vietnam", "VN", "viet nam"),
    _mfn("banglades", "Bangladeş", "BD", "bangladesh"),
    _mfn("pakistan", "Pakistan", "PK"),
    _mfn("endonezya", "Endonezya", "ID", "indonesia"),
    _mfn("tayland", "Tayland", "TH", "thailand"),
    _mfn("brezilya", "Brezilya", "BR", "brazil"),
    _mfn("meksika", "Meksika", "MX", "mexico"),
    _mfn("kanada", "Kanada", "CA", "canada"),
    _mfn("avustralya", "Avustralya", "AU", "australia"),
    _mfn("suudi arabistan", "Suudi Arabistan", "SA", "saudi arabia"),
    _mfn("irak", "Irak", "IQ", "iraq"),
    _mfn("azerbaycan", "Azerbaycan", "AZ", "azerbaijan"),
    _mfn("kazakistan", "Kazakistan", "KZ", "kazakhstan"),
    _mfn("ozbekistan", "Özbekistan", "UZ", "uzbekistan"),
    _mfn("guney afrika", "Güney Afrika", "ZA", "south africa"),
    _mfn("nijerya", "Nijerya", "NG", "nigeria"),
    _mfn("cezayir", "Cezayir", "DZ", "algeria"),
    _mfn("urdun", "Ürdün", "JO", "jordan"),
    _mfn("lubnan", "Lübnan", "LB", "lebanon"),
    _mfn("peru", "Peru", "PE"),
    _mfn("kolombiya", "Kolombiya", "CO", "colombia"),
    _mfn("arjantin", "Arjantin", "AR", "argentina"),
    _mfn("turkmenistan", "Türkmenistan", "TM"),
    _mfn("kirgizistan", "Kırgızistan", "KG", "kyrgyzstan"),
    _mfn("belarus", "Belarus", "BY", "beyaz rusya"),
    _mfn("sri lanka", "Sri Lanka", "LK"),
    _mfn("yeni zelanda", "Yeni Zelanda", "NZ", "new zealand"),
    _mfn("guney sudan", "Güney Sudan", "SS"),
    _mfn("etiyopya", "Etiyopya", "ET", "ethiopia"),
    _mfn("kenya", "Kenya", "KE"),

    # --- GTS (Genelleştirilmiş Tercihler Sistemi) yararlanıcıları ---------------------
    # Bunlar Türkiye ile tercihli ticaret ANLAŞMASI olan ülkeler değildir; tavizi
    # Türkiye tek taraflı olarak İthalat Rejimi Kararı ekiyle verir. Kayıt defterinde
    # olmamaları sessiz bir FAZLA VERGİ üretiyordu: resmî ekteki yazım dışında bir ad
    # girildiğinde sorgu 'Diğer Ülkeler' sütununa düşüyordu (ör. 'Myanmar' → %12,
    # 'Burma/Myanmar' → %0). Anahtar resmî ekteki yazımdır; takma adlar kullanıcının
    # yazabileceği varyantlardır. Rejim 'mfn' kalır — GTS sütununu `gts_countries`
    # tablosu seçer, bu kayıt yalnız adı çözer.
    _mfn("afganistan", "Afganistan", "AF", "afghanistan"),
    _mfn("angola", "Angola", "AO"),
    _mfn("benin", "Benin", "BJ"),
    _mfn("bolivya", "Bolivya", "BO", "bolivia"),
    _mfn("burkina faso", "Burkina Faso", "BF"),
    _mfn("burma/myanmar", "Burma/Myanmar", "MM", "myanmar", "burma", "myanmar birligi"),
    _mfn("burundi", "Burundi", "BI"),
    _mfn("butan", "Butan", "BT", "bhutan"),
    _mfn("cape verde", "Cape Verde", "CV", "cabo verde", "yesil burun adalari", "yeşil burun adaları"),
    _mfn("cibuti", "Cibuti", "DJ", "djibouti"),
    _mfn("cook adalari", "Cook Adaları", "CK", "cook islands"),
    _mfn("ekvatoryal gine", "Ekvatoryal Gine", "GQ", "equatorial guinea"),
    _mfn("eritre", "Eritre", "ER", "eritrea"),
    _mfn("filipinler", "Filipinler", "PH", "philippines"),
    _mfn("gambiya", "Gambiya", "GM", "gambia"),
    _mfn("gine", "Gine", "GN", "guinea"),
    _mfn("gine-bissau", "Gine-Bissau", "GW", "gine bissau", "guinea-bissau", "guinea bissau"),
    _mfn("haiti", "Haiti", "HT"),
    _mfn("kambocya", "Kamboçya", "KH", "cambodia"),
    _mfn("kiribati", "Kiribati", "KI"),
    _mfn("komor", "Komor", "KM", "komorlar", "comoros"),
    _mfn("kongo", "Kongo", "CG", "kongo cumhuriyeti", "republic of the congo", "congo"),
    _mfn("kongo demokratik cum.", "Kongo Demokratik Cum.", "CD", "kongo demokratik cumhuriyeti", "demokratik kongo cumhuriyeti", "dr kongo", "drc", "democratic republic of the congo", "zaire"),
    _mfn("laos", "Laos", "LA"),
    _mfn("lesotho", "Lesotho", "LS"),
    _mfn("liberya", "Liberya", "LR", "liberia"),
    _mfn("madagaskar", "Madagaskar", "MG", "madagascar"),
    _mfn("malavi", "Malavi", "MW", "malawi"),
    _mfn("mali", "Mali", "ML"),
    _mfn("mikronezya", "Mikronezya", "FM", "micronesia"),
    _mfn("moritanya", "Moritanya", "MR", "mauritania"),
    _mfn("mozambik", "Mozambik", "MZ", "mozambique"),
    _mfn("mogolistan", "Moğolistan", "MN", "mongolia"),
    _mfn("nepal", "Nepal", "NP"),
    _mfn("nijer", "Nijer", "NE", "niger"),
    _mfn("niue", "Niue", "NU"),
    _mfn("orta afrika cumhuriyeti", "Orta Afrika Cumhuriyeti", "CF", "central african republic"),
    _mfn("ruanda", "Ruanda", "RW", "rwanda"),
    _mfn("sao tome ve principe", "Sao Tome ve Principe", "ST", "sao tome and principe", "sao tome"),
    _mfn("senegal", "Senegal", "SN"),
    _mfn("sierra leone", "Sierra Leone", "SL"),
    _mfn("solomon adalari", "Solomon Adaları", "SB", "solomon islands", "salomon adalari"),
    _mfn("somali", "Somali", "SO"),
    _mfn("sudan", "Sudan", "SD"),
    _mfn("tacikistan", "Tacikistan", "TJ", "tajikistan"),
    _mfn("tanzanya", "Tanzanya", "TZ", "tanzania"),
    _mfn("timor-leste", "Timor-Leste", "TL", "timor leste", "dogu timor", "doğu timor", "east timor"),
    _mfn("togo", "Togo", "TG"),
    _mfn("tuvalu", "Tuvalu", "TV"),
    _mfn("uganda", "Uganda", "UG"),
    _mfn("yemen", "Yemen", "YE"),
    _mfn("zambiya", "Zambiya", "ZM", "zambia"),
    _mfn("cad", "Çad", "TD", "chad"),
)

# Agreements that were signed but are not on the ministry's in-force list; the user is warned.
PENDING_AGREEMENTS: dict[str, str] = {
    "ukrayna": "Türkiye–Ukrayna STA imzalanmıştır; yürürlük durumu Bakanlık listesinden doğrulanmalıdır.",
    "urdun": "Türkiye–Ürdün STA 2018'de sona ermiştir; tercih uygulanmaz.",
    "lubnan": "Türkiye–Lübnan STA yürürlüğe girmemiştir.",
    "japonya": "Türkiye–Japonya EPA müzakeresi sürmektedir; tercih uygulanmaz.",
    "peru": "Türkiye–Peru STA yürürlükte değildir.",
    "kolombiya": "Türkiye–Kolombiya STA yürürlükte değildir.",
}

_INDEX: dict[str, Country] = {}
for _country in COUNTRIES:
    for _alias in _country.keys:
        _INDEX.setdefault(_alias, _country)


def find_country(value: Any) -> Optional[Country]:
    """Resolve a user-supplied country name (Turkish or English, any case) to its registry entry."""
    key = country_key(value)
    if not key:
        return None
    if key in _INDEX:
        return _INDEX[key]
    # "Çin Halk Cumhuriyeti", "Almanya Federal Cumhuriyeti": match on the leading word group.
    # The longest alias wins so that a compound name ("Çin Tayvanı") is not captured by a
    # shorter country prefix ("Çin").
    best: Country | None = None
    best_length = 0
    for alias, country in _INDEX.items():
        if len(alias) < 4 or len(alias) <= best_length:
            continue
        if key.startswith(alias + " ") or key.endswith(" " + alias):
            best, best_length = country, len(alias)
    return best


def by_regime(*regimes: Regime) -> frozenset[str]:
    """Every alias key of the countries in the given regimes."""
    return frozenset(alias for country in COUNTRIES if country.regime in regimes for alias in country.keys)


def column_1_keys() -> frozenset[str]:
    return frozenset(alias for country in COUNTRIES if country.column_1 for alias in country.keys)


def explicit_labels() -> dict[str, str]:
    return {alias: country.label for country in COUNTRIES if country.label for alias in country.keys}


def numeric_columns() -> dict[str, str]:
    return {alias: country.numeric_column for country in COUNTRIES if country.numeric_column for alias in country.keys}
