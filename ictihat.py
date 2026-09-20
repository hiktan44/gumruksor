"""Danıştay gümrük içtihadı arşivi: GTİP bazında emsal karar kanıtı.

Neden: uygulamada sınıflandırma kanıtı olarak **AB tüzükleri** ve **AB BTB kararları** var,
ama **Türk yargı içtihadı** hiç yok. Bir GTİP tartışmasında "Danıştay Yedinci Daire bu eşyayı
şu pozisyonda değerlendirdi, karar şu tarihte kesinleşti" demek, AB tüzüğü kanıtı kadar
ağırlıklı bir dayanaktır ve gümrük müşavirinin itiraz dilekçesinde gerçekten kullandığı
şeydir. Bu modül o boşluğu kapatır.

Kaynak ölçümü (20.09.2026, Bedesten'e gerçek isteklerle; varsayım değil):

* ``POST bedesten.adalet.gov.tr/emsal-karar/searchDocuments`` **anahtarsız** çalışıyor ve
  gümrük sorgularında sonuçların neredeyse tamamı **Danıştay 7. Daire** (gümrük vergisi
  dairesi) ile **Vergi Dava Daireleri Kurulu**'ndan geliyor.
* ``/emsal-karar/getDocumentContent`` kararın tam metnini ``text/html`` olarak veriyor;
  ölçülen ortalama 5.885 karakter, Türkçe'ye özgü harf oranı **0,08** (temiz metin).
* **Kararların %65'inde makine tarafından okunabilir GTİP var** (20 kararlık örneklemde
  13'ü): ``8471.60.90.00.19``, ``8701.93.90.00.00``, ``8501.61.20.90.00``,
  ``8541.40.90.00.11``… Yani arşiv GTİP'e göre indekslenebilir.
* ``kararTarihiStart``/``kararTarihiEnd`` süzgeci **çalışıyor** ve sayfalama çalışıyor; bu
  yüzden geriye dolum tarih penceresiyle sınırlanabilir ve kaldığı yerden devam edebilir.

Ölçümün ortaya çıkardığı ve **kodun hesabını verdiği** dört sınır:

1. **API'nin ``total`` değeri güvenilmez.** "ilave gümrük vergisi" 407.851, "gümrük vergisi"
   232.321 döndürüyor — bir birleşik ifade aramasında bu imkânsız; kelimeler VEYA'lanıyor.
   ``tamCumle`` bu uçta (mevzuat ucundan farklı olarak) **hiç etki etmiyor**. Bu yüzden
   ``total`` hiçbir yerde "şu kadar gümrük kararı var" diye gösterilmez ve ilgi süzgeci
   **bizim tarafta**, kararın kendi metni üzerinde çalışır (:func:`assess_relevance`).
2. **Kararların %35'inde GTİP kodu yok.** Onlar GTİP sorgusuyla değil yalnız tam metin
   aramasıyla bulunur. "O GTİP'te karar yok" demek yanlış olurdu; arşiv kapsamını açıkça
   bildirir.
3. **Seri kararlar birebir yineleniyor.** Örneklemde 2025 tarihli dört karar aynı kod
   kümesini taşıyordu (aynı ihtilafın seri kararları). Gövde sha256'sı aynı olan karar
   ``duplicate_of`` ile işaretlenir ve aramada bir kez görünür.
4. **Karar emsaldir, bağlayıcı değildir** ve mevzuat o tarihten sonra değişmiş olabilir.
   1999 tarihli bir 8471.60 kararı bugünkü oranı **etkilemez**. Bu yüzden:

   * modül hiçbir oran, kod veya belge şartı **üretmez**; maliyet hesabına girdi vermez,
   * her sonuç karar tarihini, dairesini ve kesinleşme durumunu taşır,
   * ``AGED_AFTER_YEARS`` yılından eski karar ``dated: True`` ile işaretlenir ve uyarı
     metni mevzuatın değişmiş olabileceğini söyler,
   * GTİP eşleşmesi kararın **metninden** okunur; kullanıcının girdiği koda göre asla
     "bu karar sizin eşyanız için geçerlidir" denmez.

Diğer değişmezler: her istek ``security_firewall.validate_outbound_url`` ile yeniden
doğrulanır ve yalnız ``bedesten.adalet.gov.tr``'e çıkılır; arşiv **ekleyicidir** (yayımlanmış
bir karar değişmez, bu yüzden snapshot farkı ve inceleme kapısı yoktur); aynı karar ikinci
kez indirilmez.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from security_firewall import validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

BASE_URL = (os.environ.get("ICTIHAT_BASE_URL") or "https://bedesten.adalet.gov.tr").rstrip("/")
_OFFICIAL_HOSTS = frozenset({"bedesten.adalet.gov.tr"})
#: Kararın kamuya açık görüntülenme adresi; künyede kullanıcıya bu verilir.
PUBLIC_URL_TEMPLATE = "https://mevzuat.adalet.gov.tr/ictihat/{document_id}"

#: Bedesten istekleri bu başlıkları bekliyor (mevcut ``bedesten_client`` ile aynı).
APP_NAME = "UyapMevzuat"
HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "AdaletApplicationName": APP_NAME,
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}

SYNC_ENABLED = (os.environ.get("ICTIHAT_SYNC_ENABLED") or "1").strip().lower() not in {
    "0", "false", "no", "off",
}
SYNC_INTERVAL_SECONDS = max(60, int(os.environ.get("ICTIHAT_SYNC_SECONDS") or 1800))
#: Bir turda işlenecek en fazla tarih penceresi ve pencere uzunluğu (gün).
WINDOWS_PER_RUN = max(1, min(int(os.environ.get("ICTIHAT_WINDOWS_PER_RUN") or 2), 20))
WINDOW_DAYS = max(7, min(int(os.environ.get("ICTIHAT_WINDOW_DAYS") or 120), 730))
#: Kaynağa saygı: her istek arası bekleme.
REQUEST_DELAY_SECONDS = max(0.0, float(os.environ.get("ICTIHAT_DELAY_SECONDS") or 0.5))
#: Arşivin tabanı. Daha geriye gitmek teknik olarak mümkün (1996 kararı geldi) ama eski
#: karar bugünkü nomenklatürle eşleşmiyor; taban açıkça sınırlanır.
ARCHIVE_FLOOR = os.environ.get("ICTIHAT_FLOOR") or "2010-01-01"
#: Bu yaştan eski karar "mevzuat değişmiş olabilir" uyarısıyla işaretlenir.
AGED_AFTER_YEARS = max(1, int(os.environ.get("ICTIHAT_AGED_AFTER_YEARS") or 5))

_PAGE_SIZE = 20
_MAX_PAGES_PER_QUERY = max(1, min(int(os.environ.get("ICTIHAT_MAX_PAGES") or 5), 50))
_MAX_DECISIONS_PER_WINDOW = 400
_MAX_TEXT_CHARS = 60_000
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Gümrük ihtilafını hedefleyen arama ifadeleri. Kaynağın ``total`` değeri güvenilmez ama
#: **sıralaması** iyi: bu ifadelerle gelen ilk sayfalar ölçümde neredeyse tamamen Danıştay
#: 7. Daire kararıydı. Süzme yine de kararın kendi metninde yapılır.
SEARCH_PHRASES: tuple[str, ...] = (
    "gümrük tarife istatistik pozisyonu",
    "tarife sınıflandırma",
    "ilave gümrük vergisi",
    "gümrük kıymeti",
    "dampinge karşı vergi",
    "gözetim uygulaması gümrük",
    "tarife kontenjanı ithalat",
    "menşe şahadetnamesi A.TR",
)

#: Aranacak karar türleri. Ölçümde gümrük ihtilafı ağırlıklı olarak Danıştay'dan geliyor;
#: istinaf ve yerel mahkeme kararları da kapsama alınır ama dairesi yazılarak gösterilir.
ITEM_TYPES: tuple[str, ...] = ("DANISTAYKARAR",)

#: Gümrük yargısının uzmanlaşmış birimleri. Bu birimlerden gelen karar ilgi eşiğini
#: doğrudan geçer; diğer birimlerde metnin kendisi ölçülür.
CUSTOMS_CHAMBERS: tuple[str, ...] = ("7. daire", "vergi dava daireleri kurulu")

#: İlgi ölçümünde sayılan gümrük terimleri.
_RELEVANCE_TERMS: tuple[str, ...] = (
    "gümrük", "tarife", "ithalat", "ihracat", "beyanname", "gtip", "menşe",
    "damping", "gözetim", "kontenjan", "antrepo", "tasfiye", "kıymet",
)
_MIN_RELEVANCE_HITS = 3

#: Metnin okunabilirlik ölçütü; ``resmi_gazete`` ile aynı gerekçe ve aynı eşik.
_TURKISH_DIACRITICS = set("çğıöşüâîûÇĞİÖŞÜÂÎÛ")
_MIN_DIACRITIC_RATIO = 0.02
_MIN_TEXT_CHARS = 400

#: GTİP deseni. Resmî metinlerde kod satır sonunda bölünebiliyor ve araya boşluk
#: girebiliyor (ölçülen gerçek örnek: ``8471.60.90. 00.19``), bu yüzden boşluk toleranslı
#: okunur ve :func:`normalise_gtip` ile kanonik biçime getirilir.
_GTIP_RE = re.compile(
    r"\b(\d{4})\s*\.\s*(\d{2})(?:\s*\.\s*(\d{2}))?(?:\s*\.\s*(\d{2}))?(?:\s*\.\s*(\d{2}))?\b"
)

_HTML_ENTITIES = (
    ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
    ("&#39;", "'"), ("&apos;", "'"),
)


class IctihatError(RuntimeError):
    """Bedesten içtihat servisi beklenen biçimi vermedi."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


#: Türkçe'nin noktalı İ'si ``str.lower()`` sonrası ``i`` + U+0307 (birleşen nokta üstü)
#: oluyor, bu yüzden ``"ithalat" in "İTHALAT".lower()`` **False** döner. Kaynağın konu
#: etiketleri tamamı büyük harf ve İ içeriyor ("GÜMRÜK TARİFE İSTATİSTİK POZİSYONU"), yani
#: bu tuzak gerçek veride ilgi süzgecini sessizce bozuyordu. Karşılaştırma bu katlamayla
#: yapılır.
_COMBINING_DOT_ABOVE = "\u0307"


def fold(text: str) -> str:
    """Karşılaştırma için Türkçe duyarlı küçültme."""
    return (text or "").lower().replace(_COMBINING_DOT_ABOVE, "")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


#: Kaynak boş alanları JSON ``null`` olarak değil **düz "null" metni** olarak da
#: döndürüyor (ölçülen gerçek satır: ``kesinlesmeDurumu = 'null'``). Bu değer arayüze
#: olduğu gibi basılırsa kullanıcı "null" yazısını veri sanır.
_EMPTY_TOKENS = {"", "null", "none", "nan", "-"}


def _clean(value: Any) -> str:
    text = _squash(str(value if value is not None else ""))
    return "" if text.lower() in _EMPTY_TOKENS else text


def decision_date(row: dict[str, Any]) -> str:
    """Kararın gerçek tarihi (ISO).

    Ölçülen tuzak: ``kararTarihi`` alanı ``1999-03-10T22:00:00.000+00:00`` derken aynı
    satırdaki ``kararTarihiStr`` ``11.03.1999`` diyor — damga UTC'ye kaydırılmış. İlk
    on karakteri almak **her kararın tarihini bir gün geri** kaydırırdı. Bu yüzden yerel
    ve yetkili biçim olan ``kararTarihiStr`` önce okunur.
    """
    local = _clean(row.get("kararTarihiStr"))
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", local)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    stamp = _clean(row.get("kararTarihi"))
    return stamp[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", stamp) else ""


def decode_entities(text: str) -> str:
    for entity, char in _HTML_ENTITIES:
        text = text.replace(entity, char)
    return text


def html_to_text(payload: str) -> str:
    """Karar HTML'inden düz metin."""
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", payload or "")
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    lines = [_squash(line) for line in decode_entities(text).splitlines()]
    return "\n".join(line for line in lines if line)


def normalise_gtip(raw: str) -> str:
    """Kararda geçen kodu kanonik biçime getirir: ``8471.60.90. 00.19`` → ``8471609000019``?

    Hayır — kanonik biçim **noktasız rakam dizisidir** (``847160900019``), çünkü
    uygulamanın geri kalanı GTİP'i rakam dizisi olarak tutuyor (``tariff_engine``,
    ``excise_lists``). Nokta ve boşluklar atılır; 4, 6, 8, 10 veya 12 haneli sonuç döner.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) not in {4, 6, 8, 10, 12}:
        return ""
    # Fasıl geçerliliği süzgeci: nomenklatürde 01-97 arası fasıl var, 77 ayrılmış. Bu
    # olmadan karar metnindeki "2024.12.03" gibi tarih benzeri diziler GTİP sanılabiliyor.
    # Süzgeç dar tutuldu: 2009 (meyve suları) gibi yıl'a benzeyen gerçek pozisyonları
    # eleyecek bir "yıl gibi görünüyor" kuralı yazmak doğru okunmuş kodları atardı.
    chapter = int(digits[:2])
    if chapter < 1 or chapter > 97 or chapter == 77:
        return ""
    return digits


def extract_gtip_codes(text: str) -> list[str]:
    """Karar metnindeki GTİP kodlarını kanonik, tekilleştirilmiş ve sıralı olarak döndürür.

    Ölçülen tuzak: aynı kod hem ``8471.60.90.00.19`` hem ``8471.60.90.0019`` biçiminde
    yakalanabiliyordu ve arşive iki ayrı kod olarak giriyordu. Kanonikleştirme bunu tek
    koda indirir.
    """
    found: set[str] = set()
    for match in _GTIP_RE.finditer(text or ""):
        code = normalise_gtip(match.group(0))
        if code:
            found.add(code)
    return sorted(found)


def gtip_prefixes(code: str) -> list[str]:
    """Bir kodun sorgulanabilir ön ekleri (4, 6, 8, 10, 12 hane)."""
    digits = re.sub(r"\D", "", code or "")
    return [digits[:width] for width in (4, 6, 8, 10, 12) if len(digits) >= width]


def diacritic_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char in _TURKISH_DIACRITICS) / len(letters)


def assess_relevance(text: str, birim: str = "", konular: list[str] | None = None) -> tuple[bool, str]:
    """Karar gümrük ihtilafı mı? ``(ilgili_mi, gerekçe)``.

    Süzme **bizim tarafta** yapılır, çünkü kaynağın ``total``/sıralama bilgisi kelimeleri
    VEYA'lıyor ve "gümrük" kelimesi geçmeyen kararlar da sonuç kümesine girebiliyor.
    İki yol da kabul edilir:

    * karar gümrük yargısının uzmanlaşmış biriminden geliyorsa (Danıştay 7. Daire, Vergi
      Dava Daireleri Kurulu) — bu birimlerin iş bölümü zaten gümrük/vergi ihtilafıdır,
    * ya da metinde en az :data:`_MIN_RELEVANCE_HITS` farklı gümrük terimi geçiyorsa.
    """
    lowered = fold(text)
    chamber = fold(birim)
    if any(name in chamber for name in CUSTOMS_CHAMBERS):
        return True, ""
    # Kaynağın konu etiketleri elle küratörlenmiş bir konu dizinidir (ölçülen örnek:
    # "GÜMRÜK VERGİSİ", "GÜMRÜK TARİFE İSTATİSTİK POZİSYONU"); ilgi ölçümünde metinle
    # birlikte sayılır.
    lowered = " ".join([lowered, fold(" ".join(konular or []))])
    hits = sorted({term for term in _RELEVANCE_TERMS if term in lowered})
    if len(hits) >= _MIN_RELEVANCE_HITS:
        return True, ""
    return False, (
        f"Karar gümrük birimlerinden değil ve metinde yalnız {len(hits)} gümrük terimi geçiyor."
    )


def assess_text(text: str) -> tuple[str, str]:
    """Karar metni okunabilir mi? ``(kalite, gerekçe)``; ``clean | suspect | unreadable``.

    Ölçüt ``resmi_gazete.assess_text`` ile aynı gerekçeye dayanır: gövde uzunluğundaki bir
    Türkçe yargı metninde ç, ğ, ı, ş bulunmaması olanaksızdır (ölçülen gerçek kararlarda
    oran 0,08). Okunamayan karar künyesiyle saklanır, gövdesi saklanmaz ve aranmaz.
    """
    stripped = _squash(text)
    if not stripped:
        return "unreadable", "Karardan hiç metin çıkmadı."
    ratio = diacritic_ratio(stripped)
    if len(stripped) >= _MIN_TEXT_CHARS and ratio < _MIN_DIACRITIC_RATIO:
        return "unreadable", (
            f"Türkçe'ye özgü harf oranı %{ratio * 100:.1f}: çıkan metin karar metni değil."
        )
    if len(stripped) < _MIN_TEXT_CHARS:
        return "suspect", f"Metin yalnız {len(stripped)} karakter: karar gövdesi eksik olabilir."
    return "clean", ""


@dataclass
class DecisionRef:
    """Arama sonucundan okunan karar künyesi (metin henüz alınmamış)."""

    document_id: str
    item_type: str = ""
    birim: str = ""
    esas_no: str = ""
    karar_no: str = ""
    karar_tarihi: str = ""
    karar_turu: str = ""
    #: Kaynağın ``kesinlesmeDurumu`` alanı. Adı yanıltıcı: ölçümde içinde kesinleşme
    #: bilgisi değil kararın **konu anahtar kelimeleri** vardı
    #: (``BİLİRKİŞİ RAPORU,GÜMRÜK TARİFE İSTATİSTİK POZİSYONU,GÜMRÜK VERGİSİ,``) ya da düz
    #: ``"null"`` metni. Bu yüzden alan konu etiketi olarak okunur; hiçbir yerde
    #: "karar kesinleşti" anlamında gösterilmez — bir gümrük müşavirine kesinleşmemiş
    #: kararı kesinleşmiş gibi sunmak ciddi bir yanlış olurdu.
    konular: list[str] = field(default_factory=list)

    @property
    def public_url(self) -> str:
        return PUBLIC_URL_TEMPLATE.format(document_id=self.document_id)


def parse_search_response(payload: dict[str, Any]) -> list[DecisionRef]:
    """Arama yanıtından karar künyelerini çıkarır.

    Servis hata durumunu HTTP koduyla değil gövdedeki ``metadata.FMTY`` alanıyla
    bildiriyor; bu yüzden 200 yanıt da doğrulanır.
    """
    meta = payload.get("metadata") or {}
    if meta.get("FMTY") not in (None, "SUCCESS"):
        raise IctihatError(str(meta.get("FMTE") or "Bedesten içtihat araması başarısız."))
    data = payload.get("data") or {}
    refs: list[DecisionRef] = []
    for row in data.get("emsalKararList") or []:
        document_id = str(row.get("documentId") or "").strip()
        if not document_id:
            continue
        item_type = (row.get("itemType") or {})
        raw_konular = _clean(row.get("kesinlesmeDurumu"))
        refs.append(
            DecisionRef(
                document_id=document_id,
                item_type=_clean(item_type.get("name")),
                birim=_clean(row.get("birimAdi")),
                esas_no=_clean(row.get("esasNo")),
                karar_no=_clean(row.get("kararNo")),
                karar_tarihi=decision_date(row),
                karar_turu=_clean(row.get("kararTuru")),
                konular=[part for part in (p.strip() for p in raw_konular.split(",")) if part],
            )
        )
    return refs


def parse_content_response(payload: dict[str, Any]) -> str:
    """Karar içeriği yanıtından düz metin. İçerik base64 kodlu HTML olarak geliyor."""
    meta = payload.get("metadata") or {}
    if meta.get("FMTY") not in (None, "SUCCESS"):
        raise IctihatError(str(meta.get("FMTE") or "Karar metni alınamadı."))
    data = payload.get("data") or {}
    raw = data.get("content") or ""
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        decoded = str(raw)
    mime = str(data.get("mimeType") or "").lower()
    if "html" in mime or "<" in decoded[:200]:
        return html_to_text(decoded)
    return "\n".join(line for line in (_squash(l) for l in decoded.splitlines()) if line)


@dataclass
class DecisionHit:
    """Arama/sorgu sonucunda dönen tek karar."""

    document_id: str
    birim: str
    esas_no: str
    karar_no: str
    karar_tarihi: str
    karar_turu: str
    konular: list[str]
    url: str
    sha256: str
    retrieved_at: str
    text_quality: str
    gtip_codes: list[str] = field(default_factory=list)
    matched_gtip: str = ""
    match_width: int = 0
    snippet: str = ""
    dated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "birim": self.birim,
            "esas_no": self.esas_no,
            "karar_no": self.karar_no,
            "karar_tarihi": self.karar_tarihi,
            "karar_turu": self.karar_turu,
            # Kaynak bu alanı "kesinlesmeDurumu" diye adlandırıyor ama içinde konu
            # anahtar kelimeleri var; kesinleşme bilgisi olarak sunulmaz.
            "konular": list(self.konular),
            "url": self.url,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
            "text_quality": self.text_quality,
            "gtip_codes": list(self.gtip_codes),
            "matched_gtip": self.matched_gtip,
            "match_width": self.match_width,
            "snippet": self.snippet,
            "dated": self.dated,
            "binding": False,
            "note": (
                "Mahkeme kararı emsaldir, bağlayıcı değildir; benzer olaylarda yargının "
                "yaklaşımını gösterir. Karar tarihinden sonra mevzuat değişmiş olabilir."
            ),
        }


@dataclass
class IctihatResult:
    query: str = ""
    gtip: str = ""
    hits: list[DecisionHit] = field(default_factory=list)
    total: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "gtip": self.gtip,
            "total": self.total,
            "hits": [hit.as_dict() for hit in self.hits],
            "warnings": list(self.warnings),
            "source_note": (
                "Kaynak: Adalet Bakanlığı Bedesten içtihat servisi (mevzuat.adalet.gov.tr). "
                "Arşiv seçicidir: yalnız gümrük ihtilafı kararları alınır. Kararlar emsal "
                "niteliğindedir, oran veya GTİP tespiti yerine geçmez ve hesaba girmez."
            ),
        }


class IctihatArchive:
    """Gümrük içtihadını tarar, indirir, GTİP'e göre indeksler ve aranabilir kılar."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        floor: str = ARCHIVE_FLOOR,
        windows_per_run: int = WINDOWS_PER_RUN,
        window_days: int = WINDOW_DAYS,
        delay_seconds: float = REQUEST_DELAY_SECONDS,
        sync_interval_seconds: int = SYNC_INTERVAL_SECONDS,
        phrases: tuple[str, ...] = SEARCH_PHRASES,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "ictihat.sqlite3"
        self.base_url = base_url.rstrip("/")
        self.floor = _parse_date(floor)
        self.windows_per_run = max(1, int(windows_per_run))
        self.window_days = max(1, int(window_days))
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.sync_interval_seconds = max(30, int(sync_interval_seconds))
        self.phrases = tuple(phrases)
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers=HEADERS,
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()
        self._syncing = False
        self._errors: list[str] = []
        self._initialise()

    # ---------------------------------------------------------------- depo
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    document_id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL DEFAULT '',
                    birim TEXT NOT NULL DEFAULT '',
                    esas_no TEXT NOT NULL DEFAULT '',
                    karar_no TEXT NOT NULL DEFAULT '',
                    karar_tarihi TEXT NOT NULL DEFAULT '',
                    karar_turu TEXT NOT NULL DEFAULT '',
                    konular TEXT NOT NULL DEFAULT '[]',
                    url TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    retrieved_at TEXT NOT NULL DEFAULT '',
                    text_quality TEXT NOT NULL DEFAULT '',
                    quality_note TEXT NOT NULL DEFAULT '',
                    char_count INTEGER NOT NULL DEFAULT 0,
                    gtip_json TEXT NOT NULL DEFAULT '[]',
                    duplicate_of TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ictihat_tarih ON decisions(karar_tarihi);
                CREATE INDEX IF NOT EXISTS idx_ictihat_sha ON decisions(sha256);
                CREATE TABLE IF NOT EXISTS decision_gtip (
                    document_id TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    PRIMARY KEY (document_id, prefix, code)
                );
                CREATE INDEX IF NOT EXISTS idx_ictihat_prefix ON decision_gtip(prefix);
                CREATE TABLE IF NOT EXISTS windows (
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    phrase TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    listed INTEGER NOT NULL DEFAULT 0,
                    stored INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    complete INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (start, end, phrase)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
                    document_id UNINDEXED, karar_tarihi UNINDEXED, birim, body,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def _set_metadata(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _get_metadata(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    # ---------------------------------------------------------------- HTTP
    async def _post(self, path: str, inner: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        validate_outbound_url(url, allowed_hosts=_OFFICIAL_HOSTS)
        body = {"data": inner, "applicationName": APP_NAME}
        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._http.post(url, json=body)
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(1 + attempt)
                continue
            break
        if response is None:
            raise last_error or IctihatError("Bedesten içtihat servisi yanıt vermedi.")
        # Yönlendirme istemciye bırakılmaz: izin listesindeki adres listenin dışına
        # yönlendirebilirdi. Bu uç yönlendirme döndürmüyor; dönerse hata sayılır.
        if response.is_redirect:
            raise IctihatError("Bedesten içtihat servisi beklenmeyen yönlendirme döndürdü.")
        response.raise_for_status()
        content = response.content
        if len(content) > _MAX_RESPONSE_BYTES:
            raise IctihatError(f"Bedesten yanıtı beklenenden büyük ({len(content)} bayt).")
        try:
            return json.loads(content)
        except ValueError as exc:
            raise IctihatError(f"Bedesten yanıtı JSON değil: {exc}") from exc

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- eşitleme
    def scanned_windows(self) -> set[tuple[str, str, str]]:
        with self._connect() as connection:
            return {
                (str(row["start"]), str(row["end"]), str(row["phrase"]))
                for row in connection.execute(
                    "SELECT start,end,phrase FROM windows WHERE complete=1"
                )
            }

    def pending_windows(self, limit: int, *, today: date | None = None) -> list[tuple[date, date, str]]:
        """Taranacak (başlangıç, bitiş, ifade) üçlüleri: bugünden geriye doğru.

        Sıra bilinçli: en çok sorulan içtihat yakın tarihlidir ve eski karar zaten
        "mevzuat değişmiş olabilir" uyarısı taşır, bu yüzden arşiv yeniden geriye dolar.
        """
        end = today or datetime.now(UTC).date()
        done = self.scanned_windows()
        out: list[tuple[date, date, str]] = []
        cursor_end = end
        while cursor_end >= self.floor and len(out) < limit:
            cursor_start = max(self.floor, cursor_end - timedelta(days=self.window_days - 1))
            for phrase in self.phrases:
                key = (cursor_start.isoformat(), cursor_end.isoformat(), phrase)
                if key not in done:
                    out.append((cursor_start, cursor_end, phrase))
                    if len(out) >= limit:
                        break
            cursor_end = cursor_start - timedelta(days=1)
        return out

    def stored_ids(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row["document_id"])
                for row in connection.execute(
                    "SELECT document_id FROM decisions WHERE sha256 <> ''"
                )
            }

    def _note_error(self, message: str) -> None:
        logger.warning("İçtihat arşivi — %s", message)
        self._errors.append(message[:200])
        del self._errors[:-20]

    async def search_window(
        self, start: date, end: date, phrase: str
    ) -> list[DecisionRef]:
        """Bir tarih penceresinde bir ifadeyi arar ve karar künyelerini toplar."""
        refs: dict[str, DecisionRef] = {}
        for page in range(1, _MAX_PAGES_PER_QUERY + 1):
            inner = {
                "pageSize": _PAGE_SIZE,
                "pageNumber": page,
                "phrase": phrase,
                "itemTypeList": list(ITEM_TYPES),
                "kararTarihiStart": f"{start.isoformat()}T00:00:00.000Z",
                "kararTarihiEnd": f"{end.isoformat()}T23:59:59.000Z",
            }
            payload = await self._post("/emsal-karar/searchDocuments", inner)
            page_refs = parse_search_response(payload)
            for ref in page_refs:
                refs.setdefault(ref.document_id, ref)
            if len(page_refs) < _PAGE_SIZE or len(refs) >= _MAX_DECISIONS_PER_WINDOW:
                break
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
        return list(refs.values())

    async def fetch_decision(self, ref: DecisionRef) -> dict[str, Any] | None:
        """Kararın metnini alır, ilgi ve okunabilirlik süzgecinden geçirir, arşive yazar.

        İlgisiz karar **hiç saklanmaz** — arşiv seçici olmalı, yoksa GTİP sorgusu alakasız
        vergi kararlarıyla dolar. ``None`` dönmesi "atlandı" demektir, hata değil.
        """
        payload = await self._post(
            "/emsal-karar/getDocumentContent", {"documentId": ref.document_id}
        )
        text = parse_content_response(payload)
        relevant, reason = assess_relevance(text, ref.birim, ref.konular)
        if not relevant:
            return None
        quality, quality_note = assess_text(text)
        body = text[:_MAX_TEXT_CHARS] if quality != "unreadable" else ""
        codes = extract_gtip_codes(text) if body else []
        digest = _sha256(body or ref.document_id)
        record = {
            "document_id": ref.document_id,
            "item_type": ref.item_type,
            "birim": ref.birim,
            "esas_no": ref.esas_no,
            "karar_no": ref.karar_no,
            "karar_tarihi": ref.karar_tarihi,
            "karar_turu": ref.karar_turu,
            "konular": json.dumps(ref.konular, ensure_ascii=False),
            "url": ref.public_url,
            "sha256": digest,
            "retrieved_at": _now(),
            "text_quality": quality,
            "quality_note": quality_note,
            "char_count": len(body),
            "gtip_json": json.dumps(codes, ensure_ascii=False),
            "duplicate_of": "",
            "body": body,
        }
        with self._connect() as connection:
            # Seri kararlar birebir aynı gövdeyle geliyor (ölçüm: 2025 tarihli dört karar).
            # İlk gelen asıl kayıt olur, sonrakiler ona bağlanır ve aramada bir kez görünür.
            if body:
                twin = connection.execute(
                    "SELECT document_id FROM decisions WHERE sha256=? AND duplicate_of='' "
                    "AND document_id<>? LIMIT 1",
                    (digest, ref.document_id),
                ).fetchone()
                if twin:
                    record["duplicate_of"] = str(twin["document_id"])
            connection.execute("DELETE FROM decisions_fts WHERE document_id=?", (ref.document_id,))
            connection.execute("DELETE FROM decision_gtip WHERE document_id=?", (ref.document_id,))
            connection.execute(
                """
                INSERT INTO decisions(document_id,item_type,birim,esas_no,karar_no,karar_tarihi,
                                      karar_turu,konular,url,sha256,retrieved_at,text_quality,
                                      quality_note,char_count,gtip_json,duplicate_of,body)
                VALUES(:document_id,:item_type,:birim,:esas_no,:karar_no,:karar_tarihi,
                       :karar_turu,:konular,:url,:sha256,:retrieved_at,:text_quality,
                       :quality_note,:char_count,:gtip_json,:duplicate_of,:body)
                ON CONFLICT(document_id) DO UPDATE SET
                    birim=excluded.birim, karar_tarihi=excluded.karar_tarihi,
                    konular=excluded.konular, sha256=excluded.sha256,
                    retrieved_at=excluded.retrieved_at, text_quality=excluded.text_quality,
                    quality_note=excluded.quality_note, char_count=excluded.char_count,
                    gtip_json=excluded.gtip_json, duplicate_of=excluded.duplicate_of,
                    body=excluded.body
                """,
                record,
            )
            if not record["duplicate_of"]:
                for code in codes:
                    for prefix in gtip_prefixes(code):
                        connection.execute(
                            "INSERT OR IGNORE INTO decision_gtip(document_id,prefix,width,code) "
                            "VALUES(?,?,?,?)",
                            (ref.document_id, prefix, len(prefix), code),
                        )
                connection.execute(
                    "INSERT INTO decisions_fts(document_id,karar_tarihi,birim,body) "
                    "VALUES(?,?,?,?)",
                    (ref.document_id, ref.karar_tarihi, ref.birim, body),
                )
        return record

    async def ingest_window(self, start: date, end: date, phrase: str) -> dict[str, Any]:
        """Bir pencere + ifade çiftini işler."""
        try:
            refs = await self.search_window(start, end, phrase)
        except Exception as exc:
            message = f"{start}..{end} '{phrase}' arama: {type(exc).__name__}: {exc}"
            self._note_error(message)
            self._record_window(start, end, phrase, counts=(0, 0, 0), complete=False, note=message[:200])
            return {"start": start.isoformat(), "end": end.isoformat(), "phrase": phrase,
                    "listed": 0, "stored": 0, "skipped": 0, "complete": False}
        already = self.stored_ids()
        stored = 0
        skipped = 0
        failures = 0
        for ref in refs:
            if ref.document_id in already:
                skipped += 1
                continue
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            try:
                record = await self.fetch_decision(ref)
                if record is None:
                    skipped += 1
                else:
                    stored += 1
            except Exception as exc:
                failures += 1
                self._note_error(f"karar {ref.document_id}: {type(exc).__name__}: {exc}")
        note = f"{failures} karar alınamadı." if failures else ""
        self._record_window(
            start, end, phrase, counts=(len(refs), stored, skipped),
            complete=not failures, note=note,
        )
        return {
            "start": start.isoformat(), "end": end.isoformat(), "phrase": phrase,
            "listed": len(refs), "stored": stored, "skipped": skipped,
            "complete": not failures,
        }

    def _record_window(
        self,
        start: date,
        end: date,
        phrase: str,
        *,
        counts: tuple[int, int, int],
        complete: bool,
        note: str,
    ) -> None:
        listed, stored, skipped = counts
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO windows(start,end,phrase,scanned_at,listed,stored,skipped,complete,note)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(start,end,phrase) DO UPDATE SET
                    scanned_at=excluded.scanned_at, listed=excluded.listed,
                    stored=excluded.stored, skipped=excluded.skipped,
                    complete=excluded.complete, note=excluded.note
                """,
                (start.isoformat(), end.isoformat(), phrase, _now(), listed, stored, skipped,
                 1 if complete else 0, note),
            )

    async def backfill(self, limit: int | None = None, *, today: date | None = None) -> dict[str, Any]:
        async with self._lock:
            self._syncing = True
            try:
                windows = self.pending_windows(limit or self.windows_per_run, today=today)
                results = [await self.ingest_window(start, end, phrase) for start, end, phrase in windows]
                self._set_metadata("last_run_at", _now())
                return {
                    "processed_windows": len(results),
                    "stored_decisions": sum(item["stored"] for item in results),
                    "skipped": sum(item["skipped"] for item in results),
                    "windows": results,
                }
            finally:
                self._syncing = False

    async def periodic_sync_loop(self, initial_delay: float = 120.0) -> None:
        """Arka plan döngüsü: arşivi bugünden geriye kademeli doldurur."""
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while True:
            try:
                report = await self.backfill()
                if not report["processed_windows"]:
                    # Taban tamamen tarandı; yalnız yeni kararlar için seyrek yoklama.
                    await asyncio.sleep(max(self.sync_interval_seconds, 21600))
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("İçtihat arka plan turu başarısız")
            await asyncio.sleep(self.sync_interval_seconds)

    # ---------------------------------------------------------------- sorgu
    def _is_dated(self, karar_tarihi: str, *, today: date | None = None) -> bool:
        try:
            decided = _parse_date(karar_tarihi)
        except ValueError:
            return False
        reference = today or datetime.now(UTC).date()
        return (reference - decided).days > AGED_AFTER_YEARS * 365

    def _hit(self, row: sqlite3.Row, *, matched: str = "", width: int = 0, snippet: str = "") -> DecisionHit:
        try:
            codes = list(json.loads(row["gtip_json"] or "[]"))
        except ValueError:
            codes = []
        try:
            konular = list(json.loads(row["konular"] or "[]"))
        except ValueError:
            konular = []
        return DecisionHit(
            document_id=str(row["document_id"]),
            birim=str(row["birim"]),
            esas_no=str(row["esas_no"]),
            karar_no=str(row["karar_no"]),
            karar_tarihi=str(row["karar_tarihi"]),
            karar_turu=str(row["karar_turu"]),
            konular=konular,
            url=str(row["url"]),
            sha256=str(row["sha256"]),
            retrieved_at=str(row["retrieved_at"]),
            text_quality=str(row["text_quality"]),
            gtip_codes=codes,
            matched_gtip=matched,
            match_width=width,
            snippet=_squash(snippet),
            dated=self._is_dated(str(row["karar_tarihi"])),
        )

    def lookup(self, gtip: str, *, limit: int = 5) -> IctihatResult:
        """Bir GTİP için en özgül eşleşen emsal kararlar (en uzun ön ek önce, sonra yeni tarih).

        Eşleşme kararın **metninden** okunmuş koddan gelir. Sonuç "bu karar sizin eşyanız
        için geçerlidir" demez; yargının benzer kodda nasıl baktığını gösterir.
        """
        digits = re.sub(r"\D", "", gtip or "")
        result = IctihatResult(gtip=digits)
        if len(digits) < 4:
            result.warnings.append("İçtihat sorgusu için en az 4 haneli bir GTİP gerekir.")
            return result
        limit = max(1, min(int(limit or 5), 25))
        prefixes = gtip_prefixes(digits)
        with self._connect() as connection:
            # Bir kararın 12 haneli kodu beş ön ek satırı doğurur (4, 6, 8, 10, 12) ve
            # sorgunun ön ek listesiyle hepsi eşleşir. Gruplamadan aynı karar beş kez
            # dönüyordu; en uzun eşleşen ön ek kararın tek temsilcisidir.
            rows = connection.execute(
                f"""
                SELECT d.*, g.prefix AS matched, MAX(g.width) AS width
                FROM decision_gtip g JOIN decisions d ON d.document_id = g.document_id
                WHERE g.prefix IN ({','.join('?' for _ in prefixes)})
                  AND d.duplicate_of = '' AND d.text_quality <> 'unreadable'
                GROUP BY d.document_id
                ORDER BY width DESC, d.karar_tarihi DESC
                LIMIT ?
                """,
                [*prefixes, limit],
            ).fetchall()
            total = connection.execute(
                f"SELECT COUNT(DISTINCT g.document_id) AS total FROM decision_gtip g "
                f"JOIN decisions d ON d.document_id=g.document_id "
                f"WHERE g.prefix IN ({','.join('?' for _ in prefixes)}) AND d.duplicate_of=''",
                prefixes,
            ).fetchone()["total"]
        result.total = int(total or 0)
        for row in rows:
            result.hits.append(self._hit(row, matched=str(row["matched"]), width=int(row["width"])))
        if not result.hits:
            result.warnings.append(
                "Bu GTİP için arşivde emsal karar bulunamadı. Arşiv seçicidir ve kararların "
                "yaklaşık üçte birinde GTİP kodu metinde geçmez; karar olmadığı anlamına gelmez."
            )
        if any(hit.dated for hit in result.hits):
            result.warnings.append(
                f"Sonuçlarda {AGED_AFTER_YEARS} yıldan eski karar var; o tarihten sonra tarife "
                "ve mevzuat değişmiş olabilir, kararı güncel metinle birlikte değerlendirin."
            )
        return result

    def search(self, query: str = "", *, since: str | None = None, until: str | None = None,
               limit: int = 10) -> IctihatResult:
        """Arşivde tam metin arar."""
        limit = max(1, min(int(limit or 10), 50))
        clauses = ["d.duplicate_of = ''"]
        params: list[Any] = []
        if since:
            clauses.append("d.karar_tarihi >= ?")
            params.append(_parse_date(since).isoformat())
        if until:
            clauses.append("d.karar_tarihi <= ?")
            params.append(_parse_date(until).isoformat())
        text = _squash(query)
        result = IctihatResult(query=text)
        with self._connect() as connection:
            if text:
                # Türkçe eklemeli bir dil: "kıymet" araması "kıymeti", "kıymetinin"
                # geçen kararı bulmalı. Tam sözcük eşleşmesi bu arşivi kullanışsız
                # kılıyordu, bu yüzden her sözcük ön ek olarak aranır.
                match = " ".join(
                    f'"{token}"*' for token in re.findall(r"\w+", text, re.UNICODE)
                )
                if not match:
                    result.warnings.append("Arama ifadesinden aranabilir sözcük çıkmadı.")
                    return result
                where = " AND ".join(["decisions_fts MATCH ?"] + clauses)
                rows = connection.execute(
                    "SELECT d.*, snippet(decisions_fts, 3, '<<', '>>', '…', 14) AS snippet "
                    "FROM decisions_fts JOIN decisions d ON d.document_id = decisions_fts.document_id "
                    f"WHERE {where} ORDER BY d.karar_tarihi DESC LIMIT ?",
                    [match, *params, limit],
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT d.*, '' AS snippet FROM decisions d "
                    f"WHERE {' AND '.join(clauses)} ORDER BY d.karar_tarihi DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM decisions d WHERE {' AND '.join(clauses)}", params
            ).fetchone()["total"]
        result.total = int(total or 0)
        for row in rows:
            result.hits.append(self._hit(row, snippet=str(row["snippet"] or "")))
        return result

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            decisions = connection.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN duplicate_of<>'' THEN 1 ELSE 0 END) AS duplicates,"
                " SUM(CASE WHEN text_quality='clean' THEN 1 ELSE 0 END) AS clean,"
                " SUM(CASE WHEN text_quality='suspect' THEN 1 ELSE 0 END) AS suspect,"
                " SUM(CASE WHEN text_quality='unreadable' THEN 1 ELSE 0 END) AS unreadable,"
                " MIN(karar_tarihi) AS oldest, MAX(karar_tarihi) AS newest FROM decisions"
            ).fetchone()
            with_code = connection.execute(
                "SELECT COUNT(DISTINCT document_id) AS total FROM decision_gtip"
            ).fetchone()["total"]
            windows = connection.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN complete=1 THEN 1 ELSE 0 END) AS complete,"
                " MIN(start) AS oldest FROM windows"
            ).fetchone()
            chambers = connection.execute(
                "SELECT birim, COUNT(*) AS total FROM decisions WHERE duplicate_of='' "
                "GROUP BY birim ORDER BY total DESC LIMIT 8"
            ).fetchall()
        total = int(decisions["total"] or 0)
        unique = total - int(decisions["duplicates"] or 0)
        coded = int(with_code or 0)
        return {
            "enabled": SYNC_ENABLED,
            "syncing": self._syncing,
            "floor": self.floor.isoformat(),
            "decisions": total,
            "unique_decisions": unique,
            "duplicates": int(decisions["duplicates"] or 0),
            "with_gtip": coded,
            "gtip_coverage_pct": round(100 * coded / unique, 1) if unique else 0.0,
            "clean": int(decisions["clean"] or 0),
            "suspect": int(decisions["suspect"] or 0),
            "unreadable": int(decisions["unreadable"] or 0),
            "oldest_decision": decisions["oldest"],
            "newest_decision": decisions["newest"],
            "scanned_windows": int(windows["total"] or 0),
            "complete_windows": int(windows["complete"] or 0),
            "oldest_window": windows["oldest"],
            "has_pending_windows": bool(self.pending_windows(1)),
            "chambers": [
                {"birim": row["birim"], "decisions": int(row["total"])} for row in chambers
            ],
            "phrases": list(self.phrases),
            "last_run_at": self._get_metadata("last_run_at"),
            "errors": list(self._errors[-5:]),
            "source_url": PUBLIC_URL_TEMPLATE.format(document_id=""),
            "note": (
                "Arşiv seçicidir: yalnız gümrük ihtilafı kararları alınır ve yayımlanmış karar "
                "değişmediği için sürüm farkı tutulmaz. Kararlar emsal niteliğindedir; hiçbir "
                "oran, GTİP veya belge şartı bu kaynaktan otomatik olarak belirlenmez. "
                "Kaynağın kendi sonuç sayacı kelimeleri VEYA'ladığı için güvenilmezdir ve hiçbir "
                "yerde kapsam ölçüsü olarak gösterilmez."
            ),
        }


def summary_lines(result: IctihatResult) -> list[str]:
    """Sonucu kullanıcıya gösterilecek kısa satırlara indirger."""
    lines: list[str] = []
    for hit in result.hits:
        parts = [hit.birim or "Danıştay", f"E. {hit.esas_no}" if hit.esas_no else "",
                 f"K. {hit.karar_no}" if hit.karar_no else "", hit.karar_tarihi]
        label = " · ".join(part for part in parts if part)
        if hit.matched_gtip:
            label += f" · eşleşen kod {hit.matched_gtip}"
        if hit.dated:
            label += " · eski karar, mevzuat değişmiş olabilir"
        lines.append(label)
    lines.extend(result.warnings)
    return lines


__all__ = [
    "ARCHIVE_FLOOR",
    "SEARCH_PHRASES",
    "SYNC_ENABLED",
    "DecisionHit",
    "DecisionRef",
    "IctihatArchive",
    "IctihatError",
    "IctihatResult",
    "assess_relevance",
    "assess_text",
    "extract_gtip_codes",
    "gtip_prefixes",
    "html_to_text",
    "normalise_gtip",
    "parse_content_response",
    "parse_search_response",
    "summary_lines",
]
