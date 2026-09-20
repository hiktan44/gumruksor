"""Resmî Gazete arşivi: gümrüğü ilgilendiren belgelerin kalıcı, tarihli kopyası.

Neden: uygulamanın tarife, kontrol ve önlem verisi düzgün sürümlenmiş durumda, ama
**mevzuat metni** hiç arşivlenmiyor — ``mevzuat_client`` ve ``bedesten_client`` %100 canlı
sorgu ve RAM'de bir saatlik önbellekle çalışıyor, süreç yeniden başlayınca hepsi gidiyor.
Bu yüzden "12.03.2024'te bu tebliğ ne diyordu" sorusu cevaplanamıyor. Bu modül o boşluğu
**seçici** biçimde kapatır: her şeyi değil, gümrük kararına giren belge ailelerini alır.

Kaynağın ölçülen durumu (19.09.2026, gerçek HTTP istekleriyle; varsayım değil):

* ``/eskiler/YYYY/MM/YYYYMMDD.htm`` günlük fihristi **2012'den bugüne tutarlı biçimde**
  ``YYYYMMDD-N.htm`` ve ``YYYYMMDD-N.pdf`` belge bağlantıları veriyor. 2000 yılı bu
  yoldan gelmiyor, bu yüzden arşivin tabanı yapılandırmayla sınırlanır.
* **Gümrüğü ilgilendiren belgelerin neredeyse tamamı mükerrer sayıda çıkıyor.** Ölçüm:
  31.12.2025 normal fihristinde 30 belge var ve hiçbiri ithalat tebliği değil; aynı günün
  ``…M3`` mükerrerinde **20 İthalat tebliği** (İthalat: 2026/1…), ``…M4`` mükerrerinde
  **26 Ürün Güvenliği ve Denetimi tebliği** yayımlanmış. Bu yüzden her gün için normal
  fihrist ve ``M1…Mn`` mükerrer fihristleri birlikte taranır; yalnız normal fihristi
  taramak yıllık rejimin tamamını kaçırmak olurdu.
* **HTML belgeler temiz metin veriyor.** Ölçülen örnek: "19 Eylül 2026 CUMARTESİ Resmî
  Gazete Sayı : 33375 YÖNETMELİK …".
* **PDF belgeler değişken.** Bir mükerrer sayı PDF'i ``1E4JHIC (J>JFGD6DF`` gibi bozuk
  metin verdi: gömülü font, Unicode eşlemesi yok. Aynı dönemden başka bir PDF tertemiz
  çıktı. Yani sorun belge bazında, kaynak bazında değil. Bozuğu ayırt eden ölçülmüş
  gösterge **Türkçe'ye özgü harf oranı**: resmî metinde %12-13, bozuk çıktıda %0.

Bu son olgu modülün en önemli kuralını doğurdu: **çıkarılan metin doğrulanır.**
:func:`assess_text` her belgeye ``clean | suspect | unreadable`` verir; okunamayan belge
künyesiyle arşivlenir ama **metni saklanmaz ve aramaya girmez**. Bozuk bir metni sessizce
kabul etmek, kullanıcının aradığı hükmü yanlış göstermek olurdu — ÖTV listelerinde
yaşanan hatanın aynısı. Okunamayan belgenin resmî bağlantısı yine verilir; kullanıcı
kaynağın kendisine gider.

Diğer değişmezler:

* Her istek ``security_firewall.validate_outbound_url`` ile **her yönlendirme adımında**
  yeniden doğrulanır; yalnız resmigazete.gov.tr'e çıkılır, boyut sınırlıdır.
* TLS: resmî uç ara sertifikayı göndermiyor, bu yüzden ``trade_measures``'ın dar güven
  eklemesi (``official_ssl_context``) kullanılır.
* Arşiv **ekleyicidir**: yayımlanmış bir Resmî Gazete belgesi değişmez, bu yüzden
  snapshot diff'i ve inceleme kapısı yoktur. Aynı belge ikinci kez indirilmez (sha256).
* Bu modül oran/kod **üretmez**. Metin arşivi ve atıftır; tarife ve önlem verisi yine
  kendi resmî anlık görüntülerinden gelir.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from security_firewall import validate_outbound_url
from turkish_text import fold
from trade_measures import official_ssl_context

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; MevzuatMCP/1.8; +https://gumruksor.com/)"

BASE_URL = (os.environ.get("RESMI_GAZETE_BASE_URL") or "https://www.resmigazete.gov.tr").rstrip("/")
_OFFICIAL_HOSTS = frozenset({"www.resmigazete.gov.tr", "resmigazete.gov.tr"})

#: Fihrist biçiminin tutarlı olduğu ölçülen en eski yıl. Daha geriye gitmek yeni bir
#: ayrıştırıcı ister; sessizce boş sonuç döndürmek yerine taban açıkça sınırlanır.
ARCHIVE_FLOOR = os.environ.get("RESMI_GAZETE_FLOOR") or "2012-01-01"

SYNC_ENABLED = (os.environ.get("RESMI_GAZETE_SYNC_ENABLED") or "1").strip().lower() not in {
    "0", "false", "no", "off",
}
#: Arka plan turları arası bekleme ve tur başına gün sayısı: kaynağa saygı sınırı.
SYNC_INTERVAL_SECONDS = max(60, int(os.environ.get("RESMI_GAZETE_SYNC_SECONDS") or 900))
DAYS_PER_RUN = max(1, min(int(os.environ.get("RESMI_GAZETE_DAYS_PER_RUN") or 6), 60))
REQUEST_DELAY_SECONDS = max(0.0, float(os.environ.get("RESMI_GAZETE_DELAY_SECONDS") or 1.0))

_MAX_INDEX_BYTES = 4 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 24 * 1024 * 1024
_MAX_DOCUMENTS_PER_DAY = 60
_MAX_REDIRECTS = 5
#: Bir günde yoklanacak en fazla mükerrer sayı. Ölçülen en yoğun gün (31.12.2025) M4'e
#: kadar gidiyordu; sınır yine de açık tutulur ama sonsuz değildir.
_MAX_EXTRA_ISSUES = max(0, min(int(os.environ.get("RESMI_GAZETE_MAX_EXTRA_ISSUES") or 6), 12))
#: Aramada ve arşivde tutulan metin sınırı; tam belge resmî bağlantıda durur.
_MAX_TEXT_CHARS = 120_000

#: Gümrük kararına giren belge aileleri. Fihristteki başlık bu desenlere bakılarak
#: süzülür: her Resmî Gazete günü onlarca belge yayımlıyor ve çoğu (atama, yönetmelik,
#: üniversite kararı) gümrükle ilgisiz. Seçici olmak bir tercih değil, kaynağa saygı ve
#: arşivi kullanılabilir tutma gereği.
INTEREST_RULES: tuple[tuple[str, str, str], ...] = (
    ("import_regime", "İthalat Rejimi", r"ithalat\s+rejimi"),
    ("export_regime", "İhracat Rejimi", r"ihracat\s+rejimi"),
    ("additional_duty", "İlave gümrük vergisi", r"ilave\s+g[üu]mr[üu]k\s+vergisi"),
    ("import_communique", "İthalat tebliği", r"\(?\s*[İI]thalat\s*:\s*\d{4}/\d+"),
    ("export_communique", "İhracat tebliği", r"\(?\s*[İI]hracat\s*:\s*\d{4}/\d+"),
    ("product_safety", "Ürün güvenliği ve denetimi tebliği", r"[üu]r[üu]n\s+g[üu]venli[ğg]i\s+ve\s+denetimi"),
    ("anti_dumping", "Damping / sübvansiyon önlemi", r"damping|s[üu]bvansiyon"),
    ("safeguard", "Korunma önlemi", r"korunma\s+[öo]nlem"),
    ("surveillance", "Gözetim tebliği", r"g[öo]zetim\s+uygulan"),
    ("tariff_quota", "Tarife kontenjanı", r"tarife\s+kontenjan"),
    ("customs_regulation", "Gümrük mevzuatı", r"g[üu]mr[üu]k\s+(kanunu|y[öo]netmeli[ğg]i|genel\s+tebli[ğg])"),
    ("excise", "Özel tüketim vergisi", r"[öo]zel\s+t[üu]ketim\s+vergisi"),
    ("vat", "Katma değer vergisi", r"katma\s+de[ğg]er\s+vergisi"),
)

_INTEREST_PATTERNS = tuple(
    (key, label, re.compile(pattern, re.IGNORECASE)) for key, label, pattern in INTEREST_RULES
)

#: Belgenin gerçekten Resmî Gazete metni olduğunu gösteren çapa ifadeler. Biri bile
#: yoksa metin şüphelidir: doğru karakterlerle boş bir çerçeve sayfası da olabilir.
#: Karşılaştırma :func:`turkish_text.fold` ile yapılır — belge başlıkları büyük harf
#: geliyor ve ``"TEBLİĞ".lower()`` Python'da ``tebli̇ğ`` (araya birleşen nokta) ürettiği
#: için düz ``lower()`` ile "tebliğ" çapası hiç eşleşmiyordu; temiz belgeler haksız yere
#: ``suspect`` işaretleniyordu.
_TEXT_ANCHORS = ("resmî gazete", "resmi gazete", "madde", "tebliğ", "karar", "yönetmelik", "kanun")

#: Türkçe'ye **özgü** harfler. Karar verici ölçüt bu: bozuk gömülü fontla çıkan metin
#: ASCII harflerden oluşuyor ve bu kümeden hiç karakter içermiyor.
#:
#: Ölçüm (19.09.2026, gerçek belgeler): resmî fihrist metninde oran **%12-13**;
#: ``20251231M3-6.pdf``'ten çıkan bozuk metinde (``1E4JHIC (J>JFGD6DF``) **%0**. Aynı
#: ölçüt İngilizce çıkan metni de yakalar — Türkçe bir Resmî Gazete belgesinden İngilizce
#: metin çıkması da çıkarımın başarısızlığıdır.
#:
#: Not: "Türk alfabesindeki harflerin oranı" ölçütü bu işi **yapmıyor**; ASCII harfler de
#: Türk alfabesinde olduğu için bozuk örnekte o oran %100 çıkıyor. İlk tasarımda bu hata
#: vardı ve testler ortaya çıkardı.
_TURKISH_DIACRITICS = set("çğıöşüâîûÇĞİÖŞÜÂÎÛ")
_MIN_DIACRITIC_RATIO = 0.02
#: Bu uzunluğun altındaki metinde Türkçe'ye özgü harf hiç geçmeyebilir (tek satırlık
#: başlık); ölçüt ancak gövde sayılabilecek uzunlukta uygulanır.
_MIN_DIACRITIC_CHARS = 120
_MIN_CLEAN_CHARS = 200

#: Fihristteki belge bağlantısı. Mükerrer eki (``M3``) **yakalanır**, çünkü aynı günün
#: normal sayısı ile mükerrerinde 1 numaralı belge farklı belgelerdir; ek kimliğin
#: parçası olmazsa biri diğerinin üzerine yazılır.
_DOC_LINK_RE = re.compile(r'href="((\d{8})(M\d+)?-(\d+)\.(htm|pdf))"', re.IGNORECASE)
#: Fihrist başlığından sayı numarası ve mükerrer sırası. Ölçülen gerçek metin:
#: "31 Aralık 2025 Tarihli ve 33124 Sayılı Resmî Gazete - 3. Mükerrer".
_ISSUE_RE = re.compile(r"ve\s+(\d{4,6})\s+Say[ıi]l[ıi]", re.IGNORECASE)
_MUKERRER_RE = re.compile(r"(\d+)\s*\.\s*M[üu]kerrer", re.IGNORECASE)
#: Başlık metninin başındaki madde işareti ("–– ", "— ") atılır; anlam taşımıyor.
_TITLE_BULLET_RE = re.compile(r"^[\s\-–—·•]+")
_HTML_ENTITIES = (
    ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
    ("&#39;", "'"), ("&apos;", "'"),
)


class ResmiGazeteError(RuntimeError):
    """Resmî Gazete kaynağı beklenen biçimi vermedi."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def day_index_url(day: date, *, suffix: str = "", base_url: str = BASE_URL) -> str:
    """Fihrist adresi: normal sayı ``/eskiler/YYYY/MM/YYYYMMDD.htm``, mükerrer sayı
    ``…/YYYYMMDDM1.htm``.

    Mükerreri ayrı bir adres olarak istemek zorunlu: ölçümde 31.12.2025 normal
    fihristindeki 30 belgenin hiçbiri ithalat tebliği değildi, aynı günün ``M3``
    mükerrerinde İthalat Rejimi, ilave gümrük vergisi ve tarife kontenjanı kararları,
    ``M4`` mükerrerinde ürün güvenliği tebliğleri yayımlanmıştı.
    """
    return f"{base_url}/eskiler/{day:%Y}/{day:%m}/{day:%Y%m%d}{suffix}.htm"


def decode_html(payload: bytes) -> str:
    """Resmî Gazete sayfaları yıla göre UTF-8, cp1254 veya ISO-8859-9 geliyor."""
    for encoding in ("utf-8", "cp1254", "iso-8859-9"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def decode_entities(text: str) -> str:
    """Fihrist ve belge HTML'inde geçen adlandırılmış varlıkları çözer."""
    for entity, char in _HTML_ENTITIES:
        text = text.replace(entity, char)
    return text


def html_to_text(payload: bytes) -> str:
    """Belge HTML'inden düz metin: betik/stil atılır, etiketler boşluğa çevrilir."""
    text = decode_html(payload)
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = decode_entities(text)
    lines = [_squash(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def pdf_to_text(payload: bytes) -> str:
    """PDF'in metin katmanı. Gömülü fontu bozuk belgede çıktı anlamsızdır; bunu
    :func:`assess_text` yakalar, burada ayıklama yapılmaz."""
    import pymupdf

    document = pymupdf.open(stream=payload, filetype="pdf")
    try:
        parts = [document[index].get_text() for index in range(min(document.page_count, 80))]
    finally:
        document.close()
    lines = [_squash(line) for line in "\n".join(parts).splitlines()]
    return "\n".join(line for line in lines if line)


def diacritic_ratio(text: str) -> float:
    """Harflerin içinde Türkçe'ye özgü olanların payı (ç, ğ, ı, ö, ş, ü, î …)."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char in _TURKISH_DIACRITICS) / len(letters)


def assess_text(text: str) -> tuple[str, str]:
    """Metnin gerçekten okunabilir bir Resmî Gazete belgesi olup olmadığına karar verir.

    Döndürülen ikili ``(kalite, gerekçe)``; kalite ``clean``, ``suspect`` veya
    ``unreadable``. İki ayrı soru sorulur ve karşılıkları farklıdır:

    * **Metin bozuk mu?** Karar verici ölçüt Türkçe'ye özgü harf oranıdır
      (:func:`diacritic_ratio`): gövde uzunluğundaki bir Türk mevzuat metninde ç, ğ, ı, ş
      bulunmaması olanaksızdır. Ölçülen gerçek değerler: resmî metinde %12-13, gömülü
      fontu bozuk PDF'te %0. Bozuk metin ``unreadable`` sayılır, **saklanmaz** ve aramaya
      girmez; yalnız künyesi ve resmî bağlantısı kalır.
    * **Doğru belge mi?** Metin okunabilir ama Resmî Gazete belgesine ait çapa ifade
      ("madde", "tebliğ", "karar" …) taşımıyor ya da gövde sayılamayacak kadar kısa ise
      ``suspect``: içerik saklanır, ama kullanıcıya kuşku bildirilir. Burada "okunamadı"
      demek yanlış olurdu — metin okunuyor, kimliği belirsiz.
    """
    stripped = _squash(text)
    if not stripped:
        return "unreadable", "Belgeden hiç metin çıkmadı."
    ratio = diacritic_ratio(stripped)
    if len(stripped) >= _MIN_DIACRITIC_CHARS and ratio < _MIN_DIACRITIC_RATIO:
        return "unreadable", (
            f"Türkçe'ye özgü harf oranı %{ratio * 100:.1f}: belgenin gömülü fontu Unicode "
            "eşlemesi taşımıyor, çıkan metin anlamsız."
        )
    lowered = fold(stripped)
    if not any(anchor in lowered for anchor in _TEXT_ANCHORS):
        return "suspect", "Metinde Resmî Gazete belgesine ait çapa ifade bulunamadı."
    if len(stripped) < _MIN_CLEAN_CHARS:
        return "suspect", f"Metin yalnız {len(stripped)} karakter: belge gövdesi eksik olabilir."
    return "clean", ""


def classify_title(title: str) -> tuple[str, str] | None:
    """Başlık ilgi alanlarımızdan birine giriyor mu? Girmiyorsa ``None``."""
    candidate = _squash(title)
    if not candidate:
        return None
    for key, label, pattern in _INTEREST_PATTERNS:
        if pattern.search(candidate):
            return key, label
    return None


def entry_title(html_after_href: str) -> str:
    """Bağlantıdan sonraki HTML parçasından belge başlığını çıkarır.

    Ölçülen gerçek düzen (31.12.2025, 3. mükerrer)::

        <a href="20251231M3-1.pdf" style="text-decoration: none">––&nbsp;&nbsp; İthalat
        Rejimi Kararında Değişiklik Yapılmasına İlişkin Karar (Karar Sayısı: 10790)</a>

    Yani parça **etiketin ortasında** başlıyor. İki kesme şart: önce ilk ``>``'e kadar olan
    öznitelik kalıntısı, sonra ``</a>``'dan sonrası atılır. Bunlar olmadan başlığa
    ``style="text-decoration: none"`` biçim artığı ve bir sonraki satırın etiketleri
    karışıyor — yani arşiv doğru belgeyi yanlış adla saklardı.
    """
    fragment = html_after_href
    opening = fragment.find(">")
    if opening >= 0:
        fragment = fragment[opening + 1 :]
    closing = re.search(r"(?i)</a\s*>", fragment)
    if closing:
        fragment = fragment[: closing.start()]
    text = decode_entities(re.sub(r"(?s)<[^>]+>", " ", fragment))
    return _TITLE_BULLET_RE.sub("", _squash(text))[:400]


@dataclass
class GazetteEntry:
    """Fihristten okunan bir belge satırı."""

    date: str
    sequence: int
    url: str
    fmt: str
    title: str
    issue_suffix: str = ""
    kind: str = ""
    kind_label: str = ""

    @property
    def document_id(self) -> str:
        """Kimlik mükerrer ekini taşır: ``2025-12-31M3:1``.

        Biçim (htm/pdf) kimliğe **girmez** — aynı belge iki biçimde listelenebiliyor ve
        arşivde tek satır olmalı."""
        return f"{self.date}{self.issue_suffix}:{self.sequence}"


@dataclass
class DayIndex:
    """Tek bir fihrist sayfası: normal sayı ya da o günün bir mükerreri."""

    date: str
    suffix: str = ""
    issue: str = ""
    label: str = ""
    source_url: str = ""
    sha256: str = ""
    entries: list[GazetteEntry] = field(default_factory=list)


def parse_day_index(
    payload: bytes, day: date, *, suffix: str = "", base_url: str = BASE_URL
) -> DayIndex:
    """Bir fihrist sayfasından belge satırlarını, sayı numarasını ve mükerrer sırasını okur.

    Başlık bağlantının **içinde**, ``>`` ile ``</a>`` arasında durur (bkz.
    :func:`entry_title`). Aynı belge hem ``.htm`` hem ``.pdf`` olarak listelenebilir;
    ikisi de döndürülür, seçim sırasında HTML tercih edilir (ölçüm: HTML temiz metin
    veriyor, PDF belge bazında bozuk olabiliyor).

    Belge kimliğinin mükerrer eki **bağlantının kendisinden** okunur, sayfanın adresinden
    değil: bir fihrist başka bir sayının belgesine bağ verirse belge yine doğru kimlikle
    kaydedilir ve iki kez indirilmez.
    """
    html = decode_html(payload)
    flat = _squash(decode_entities(re.sub(r"(?s)<[^>]+>", " ", html)))
    index = DayIndex(date=day.isoformat(), suffix=suffix)
    issue_match = _ISSUE_RE.search(flat)
    if issue_match:
        index.issue = issue_match.group(1)
    mukerrer_match = _MUKERRER_RE.search(flat[:200])
    if mukerrer_match:
        index.label = f"{mukerrer_match.group(1)}. Mükerrer"

    matches = list(_DOC_LINK_RE.finditer(html))
    for position, match in enumerate(matches):
        href, stamp = match.group(1), match.group(2)
        link_suffix = (match.group(3) or "").upper()
        sequence, fmt = match.group(4), match.group(5).lower()
        if stamp != f"{day:%Y%m%d}":
            continue
        end = matches[position + 1].start() if position + 1 < len(matches) else len(html)
        index.entries.append(
            GazetteEntry(
                date=day.isoformat(),
                sequence=int(sequence),
                url=urljoin(f"{base_url}/eskiler/{day:%Y}/{day:%m}/", href),
                fmt=fmt,
                title=entry_title(html[match.end() : end]),
                issue_suffix=link_suffix,
            )
        )
    return index


def select_documents(entries: list[GazetteEntry]) -> list[GazetteEntry]:
    """İlgi alanına giren belgeleri seçer; aynı belgenin HTML sürümünü PDF'e tercih eder.

    Aynılık ölçütü ``(mükerrer eki, sıra numarası)``: normal sayının 1 numaralı belgesi ile
    3. mükerrerin 1 numaralı belgesi **ayrı** belgelerdir.
    """
    best: dict[tuple[str, int], GazetteEntry] = {}
    for entry in entries:
        classified = classify_title(entry.title)
        if classified is None:
            continue
        entry.kind, entry.kind_label = classified
        key = (entry.issue_suffix, entry.sequence)
        current = best.get(key)
        if current is None or (current.fmt == "pdf" and entry.fmt == "htm"):
            best[key] = entry
    ordered = sorted(best.values(), key=lambda item: (item.issue_suffix, item.sequence))
    return ordered[:_MAX_DOCUMENTS_PER_DAY]


@dataclass
class GazetteHit:
    """Arama sonucu tek belge."""

    date: str
    issue: str
    issue_suffix: str
    sequence: int
    kind: str
    kind_label: str
    title: str
    url: str
    fmt: str
    sha256: str
    retrieved_at: str
    text_quality: str
    quality_note: str
    snippet: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "issue": self.issue,
            "issue_suffix": self.issue_suffix,
            "issue_label": (
                f"{self.issue_suffix[1:]}. Mükerrer" if self.issue_suffix.startswith("M") else "Normal sayı"
            ),
            "sequence": self.sequence,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "title": self.title,
            "url": self.url,
            "format": self.fmt,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
            "text_quality": self.text_quality,
            "quality_note": self.quality_note,
            "snippet": self.snippet,
        }


@dataclass
class GazetteSearchResult:
    query: str
    hits: list[GazetteHit] = field(default_factory=list)
    total: int = 0
    since: str | None = None
    until: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "since": self.since,
            "until": self.until,
            "total": self.total,
            "hits": [hit.as_dict() for hit in self.hits],
            "warnings": list(self.warnings),
            "source_note": (
                "Kaynak: Resmî Gazete (resmigazete.gov.tr) arşivi. Arşiv seçicidir: yalnız "
                "gümrük kararına giren belge aileleri alınır. Metni okunamayan belge künyesi "
                "ve resmî bağlantısıyla listelenir, metni aramaya girmez."
            ),
        }


class ResmiGazeteArchive:
    """Günlük fihristi tarar, ilgili belgeleri indirir, doğrular ve aranabilir kılar."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        floor: str = ARCHIVE_FLOOR,
        days_per_run: int = DAYS_PER_RUN,
        delay_seconds: float = REQUEST_DELAY_SECONDS,
        sync_interval_seconds: int = SYNC_INTERVAL_SECONDS,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "resmi_gazete.sqlite3"
        self.base_url = base_url.rstrip("/")
        self.floor = _parse_date(floor)
        self.days_per_run = max(1, int(days_per_run))
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.sync_interval_seconds = max(30, int(sync_interval_seconds))
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
            verify=official_ssl_context(),
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
                CREATE TABLE IF NOT EXISTS days (
                    date TEXT PRIMARY KEY,
                    issue TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    listed_count INTEGER NOT NULL DEFAULT 0,
                    selected_count INTEGER NOT NULL DEFAULT 0,
                    stored_count INTEGER NOT NULL DEFAULT 0,
                    issue_count INTEGER NOT NULL DEFAULT 0,
                    complete INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    issue_suffix TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '',
                    kind_label TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    format TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    text_quality TEXT NOT NULL DEFAULT '',
                    quality_note TEXT NOT NULL DEFAULT '',
                    char_count INTEGER NOT NULL DEFAULT 0,
                    body TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_rg_documents_date ON documents(date);
                CREATE INDEX IF NOT EXISTS idx_rg_documents_kind ON documents(kind);
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    id UNINDEXED, date UNINDEXED, title, body, tokenize='unicode61'
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
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _get_metadata(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    # ---------------------------------------------------------------- HTTP
    async def _fetch(self, url: str, *, limit: int) -> tuple[bytes, str]:
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_OFFICIAL_HOSTS)
            response: httpx.Response | None = None
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await self._http.get(current)
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if response is None:
                raise last_error or ResmiGazeteError("Resmî Gazete yanıt vermedi.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ResmiGazeteError("Resmî Gazete hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            content = response.content
            if len(content) > limit:
                raise ResmiGazeteError(f"Resmî Gazete belgesi beklenenden büyük ({len(content)} bayt).")
            media = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            return content, media
        raise ResmiGazeteError("Resmî Gazete çok fazla yönlendirme yaptı.")

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- eşitleme
    def complete_dates(self) -> set[str]:
        """Tamamlanmış günler: fihristi okunmuş **ve** seçilen her belgesi arşivlenmiş.

        Eksik kalan gün (bir belge indirilemedi, bir mükerrer fihristi hata verdi) bilerek
        bekleyenler arasında kalır; sonraki tur onu yeniden dener ve zaten indirilmiş
        belgeleri tekrar indirmez."""
        with self._connect() as connection:
            return {
                str(row["date"])
                for row in connection.execute("SELECT date FROM days WHERE complete=1")
            }

    def stored_document_ids(self, day: date) -> set[str]:
        """O güne ait, gövdesi zaten alınmış belge kimlikleri (yeniden indirilmez)."""
        with self._connect() as connection:
            return {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM documents WHERE date=? AND sha256<>''", (day.isoformat(),)
                )
            }

    def pending_days(self, limit: int, *, today: date | None = None) -> list[date]:
        """En yeniden geriye doğru, henüz taranmamış günler.

        Sıra bilinçli: en çok sorulan geçmiş yakın geçmiştir, bu yüzden arşiv bugünden
        geriye dolar ve ilk turlardan itibaren işe yarar.
        """
        end = today or datetime.now(UTC).date()
        done = self.complete_dates()
        out: list[date] = []
        cursor = end
        while cursor >= self.floor and len(out) < limit:
            if cursor.isoformat() not in done:
                out.append(cursor)
            cursor -= timedelta(days=1)
        return out

    async def _store_document(self, entry: GazetteEntry) -> dict[str, Any]:
        payload, media = await self._fetch(entry.url, limit=_MAX_DOCUMENT_BYTES)
        digest = _sha256(payload)
        if payload[:5] == b"%PDF-" or "pdf" in media:
            try:
                text = pdf_to_text(payload)
            except Exception as exc:  # pymupdf bozuk dosyada çeşitli hatalar atar
                text, quality, note = "", "unreadable", f"PDF açılamadı: {type(exc).__name__}"
            else:
                quality, note = assess_text(text)
        else:
            text = html_to_text(payload)
            quality, note = assess_text(text)
        body = text[:_MAX_TEXT_CHARS] if quality != "unreadable" else ""
        record = {
            "id": entry.document_id,
            "date": entry.date,
            "sequence": entry.sequence,
            "issue_suffix": entry.issue_suffix,
            "kind": entry.kind,
            "kind_label": entry.kind_label,
            "title": entry.title,
            "url": entry.url,
            "format": entry.fmt,
            "sha256": digest,
            "retrieved_at": _now(),
            "text_quality": quality,
            "quality_note": note,
            "char_count": len(body),
            "body": body,
        }
        with self._connect() as connection:
            connection.execute("DELETE FROM documents_fts WHERE id=?", (record["id"],))
            connection.execute(
                """
                INSERT INTO documents(id,date,sequence,issue_suffix,kind,kind_label,title,url,
                                      format,sha256,retrieved_at,text_quality,quality_note,
                                      char_count,body)
                VALUES(:id,:date,:sequence,:issue_suffix,:kind,:kind_label,:title,:url,
                       :format,:sha256,:retrieved_at,:text_quality,:quality_note,
                       :char_count,:body)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, url=excluded.url, format=excluded.format,
                    sha256=excluded.sha256, retrieved_at=excluded.retrieved_at,
                    text_quality=excluded.text_quality, quality_note=excluded.quality_note,
                    char_count=excluded.char_count, body=excluded.body
                """,
                record,
            )
            # Künye (başlık) her zaman indekse girer, gövde yalnız okunabiliyorsa. Böylece
            # metni bozuk çıkan belge aramada **bulunur** ve kullanıcı resmî bağlantısına
            # gider; bulunamamak, belgenin hiç yayımlanmadığı izlenimi verirdi.
            connection.execute(
                "INSERT INTO documents_fts(id,date,title,body) VALUES(?,?,?,?)",
                (record["id"], record["date"], record["title"], body),
            )
        return record

    async def _read_index(self, day: date, suffix: str) -> DayIndex | None:
        """Bir fihrist sayfasını okur. Sayfa yoksa ``None`` döner (hata değil: o gün gazete
        yayımlanmamış ya da o numaralı mükerrer çıkmamış olabilir)."""
        url = day_index_url(day, suffix=suffix, base_url=self.base_url)
        try:
            payload, _ = await self._fetch(url, limit=_MAX_INDEX_BYTES)
        except httpx.HTTPStatusError as exc:
            # Yalnız 404/410 "yok" demektir. 403 geçici bir engelleme olabilir ve onu
            # "yayımlanmamış" saymak günü sessizce eksik bırakırdı; hata olarak yükselir.
            if exc.response.status_code in {404, 410}:
                return None
            raise
        index = parse_day_index(payload, day, suffix=suffix, base_url=self.base_url)
        index.source_url = url
        index.sha256 = _sha256(payload)
        return index

    def _note_error(self, message: str) -> None:
        logger.warning("Resmî Gazete — %s", message)
        self._errors.append(message[:200])
        del self._errors[:-20]

    async def _read_day_indexes(self, day: date) -> tuple[list[DayIndex], list[str]]:
        """Bir günün normal fihristi ve mükerrer fihristleri.

        Mükerrer numaraları gün içinde sırayla verilir ("1. Mükerrer", "2. Mükerrer" …),
        bu yüzden ``M1``'den başlanır ve **ilk bulunamayan numarada durulur**; sonsuz
        yoklama yapılmaz. Ölçülen en yoğun gün (31.12.2025) ``M4``'e kadar gidiyordu.
        Bir yoklama hata verirse (404 değil, gerçek arıza) gün **tamamlanmamış** sayılır ve
        sonraki turda yeniden denenir — eksik taramayı tam sanmak, yıllık rejimin bir
        kısmını sessizce kaçırmak olurdu.
        """
        main = await self._read_index(day, "")
        if main is None:
            return [], []
        indexes = [main]
        problems: list[str] = []
        for number in range(1, _MAX_EXTRA_ISSUES + 1):
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            try:
                extra = await self._read_index(day, f"M{number}")
            except Exception as exc:
                message = f"{day.isoformat()} M{number} fihristi: {type(exc).__name__}: {exc}"
                self._note_error(message)
                problems.append(message[:200])
                break
            if extra is None or not extra.entries:
                break
            indexes.append(extra)
        return indexes, problems

    async def ingest_day(self, day: date) -> dict[str, Any]:
        """Bir günün bütün sayılarını (normal + mükerrer) tarar ve ilgili belgeleri arşivler."""
        indexes, problems = await self._read_day_indexes(day)
        if not indexes:
            url = day_index_url(day, base_url=self.base_url)
            self._record_day(
                day,
                issue="",
                source_url=url,
                digest="",
                counts=(0, 0, 0),
                issue_count=0,
                complete=True,
                note="O tarihte Resmî Gazete yayımlanmamış (fihrist sayfası yok).",
            )
            return {
                "date": day.isoformat(),
                "issue": "",
                "issues": 0,
                "listed": 0,
                "selected": 0,
                "stored": 0,
                "skipped": 0,
                "published": False,
            }

        entries = [entry for index in indexes for entry in index.entries]
        selected = select_documents(entries)
        already = self.stored_document_ids(day)
        stored = 0
        skipped = 0
        failures = 0
        for entry in selected:
            if entry.document_id in already:
                skipped += 1
                continue
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            try:
                await self._store_document(entry)
                stored += 1
            except Exception as exc:
                failures += 1
                self._note_error(f"{entry.date}{entry.issue_suffix} #{entry.sequence}: "
                                 f"{type(exc).__name__}: {exc}")
        note = "; ".join(problems)
        if failures:
            note = "; ".join(filter(None, [note, f"{failures} belge indirilemedi."]))
        self._record_day(
            day,
            issue=indexes[0].issue,
            source_url=indexes[0].source_url,
            digest=indexes[0].sha256,
            counts=(len(entries), len(selected), stored + skipped),
            issue_count=len(indexes),
            complete=not problems and not failures,
            note=note,
        )
        return {
            "date": day.isoformat(),
            "issue": indexes[0].issue,
            "issues": len(indexes),
            "extra_issues": [index.label or index.suffix for index in indexes[1:]],
            "listed": len(entries),
            "selected": len(selected),
            "stored": stored,
            "skipped": skipped,
            "published": True,
        }

    def _record_day(
        self,
        day: date,
        *,
        issue: str,
        source_url: str,
        digest: str,
        counts: tuple[int, int, int],
        issue_count: int,
        complete: bool,
        note: str,
    ) -> None:
        listed, selected, stored = counts
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO days(date,issue,source_url,sha256,retrieved_at,listed_count,
                                 selected_count,stored_count,issue_count,complete,note)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                    issue=excluded.issue, source_url=excluded.source_url, sha256=excluded.sha256,
                    retrieved_at=excluded.retrieved_at, listed_count=excluded.listed_count,
                    selected_count=excluded.selected_count, stored_count=excluded.stored_count,
                    issue_count=excluded.issue_count, complete=excluded.complete,
                    note=excluded.note
                """,
                (
                    day.isoformat(), issue, source_url, digest, _now(), listed, selected, stored,
                    int(issue_count), 1 if complete else 0, note,
                ),
            )

    async def backfill(self, limit: int | None = None, *, today: date | None = None) -> dict[str, Any]:
        """Bekleyen günlerden en fazla ``limit`` tanesini işler."""
        async with self._lock:
            self._syncing = True
            try:
                days = self.pending_days(limit or self.days_per_run, today=today)
                results = []
                for day in days:
                    try:
                        results.append(await self.ingest_day(day))
                    except Exception as exc:
                        self._note_error(f"{day.isoformat()} günü: {type(exc).__name__}: {exc}")
                self._set_metadata("last_run_at", _now())
                return {
                    "processed_days": len(results),
                    "stored_documents": sum(item["stored"] for item in results),
                    "skipped_documents": sum(item.get("skipped", 0) for item in results),
                    "published_days": sum(1 for item in results if item.get("published")),
                    "scanned_issues": sum(item.get("issues", 0) for item in results),
                    "days": results,
                }
            finally:
                self._syncing = False

    async def periodic_sync_loop(self, initial_delay: float = 90.0) -> None:
        """Arka plan döngüsü: kademeli olarak bugünden geriye doğru arşivi doldurur."""
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while True:
            try:
                report = await self.backfill()
                if not report["processed_days"]:
                    # Arşiv tabana kadar dolu; yalnız yeni günler için beklemeye geç.
                    await asyncio.sleep(max(self.sync_interval_seconds, 3600))
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Resmî Gazete arka plan turu başarısız")
            await asyncio.sleep(self.sync_interval_seconds)

    # ---------------------------------------------------------------- sorgu
    def search(
        self,
        query: str = "",
        *,
        since: str | None = None,
        until: str | None = None,
        kind: str | None = None,
        limit: int = 10,
    ) -> GazetteSearchResult:
        """Arşivde tam metin arar. Metni okunamayan belge gövdeden değil künyeden eşleşir."""
        limit = max(1, min(int(limit or 10), 50))
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("d.date >= ?")
            params.append(_parse_date(since).isoformat())
        if until:
            clauses.append("d.date <= ?")
            params.append(_parse_date(until).isoformat())
        if kind:
            clauses.append("d.kind = ?")
            params.append(str(kind))

        text = _squash(query)
        result = GazetteSearchResult(query=text, since=since, until=until)
        with self._connect() as connection:
            if text:
                # Türkçe eklemeli bir dil: "kıymet" araması "kıymeti", "kıymetinin"
                # geçen belgeyi bulmalı. Tam sözcük eşleşmesi arşivi kullanışsız
                # kılıyordu, bu yüzden her sözcük ön ek olarak aranır.
                match = " ".join(
                    f'"{token}"*' for token in re.findall(r"\w+", text, re.UNICODE)
                )
                if not match:
                    result.warnings.append("Arama ifadesinden aranabilir sözcük çıkmadı.")
                    return result
                where = " AND ".join(["documents_fts MATCH ?"] + clauses)
                sql = (
                    "SELECT d.*, g.issue AS issue, snippet(documents_fts, 3, '<<', '>>', '…', 12) AS snippet "
                    "FROM documents_fts JOIN documents d ON d.id = documents_fts.id "
                    "LEFT JOIN days g ON g.date = d.date "
                    f"WHERE {where} ORDER BY d.date DESC LIMIT ?"
                )
                rows = connection.execute(sql, [match, *params, limit]).fetchall()
            else:
                where = " AND ".join(clauses) or "1=1"
                sql = (
                    "SELECT d.*, g.issue AS issue, '' AS snippet FROM documents d "
                    "LEFT JOIN days g ON g.date = d.date "
                    f"WHERE {where} ORDER BY d.date DESC LIMIT ?"
                )
                rows = connection.execute(sql, [*params, limit]).fetchall()
            total_where = " AND ".join(clauses) or "1=1"
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM documents d WHERE {total_where}", params
            ).fetchone()["total"]

        result.total = int(total)
        for row in rows:
            result.hits.append(
                GazetteHit(
                    date=str(row["date"]),
                    issue=str(row["issue"] or ""),
                    issue_suffix=str(row["issue_suffix"] or ""),
                    sequence=int(row["sequence"]),
                    kind=str(row["kind"]),
                    kind_label=str(row["kind_label"]),
                    title=str(row["title"]),
                    url=str(row["url"]),
                    fmt=str(row["format"]),
                    sha256=str(row["sha256"]),
                    retrieved_at=str(row["retrieved_at"]),
                    text_quality=str(row["text_quality"]),
                    quality_note=str(row["quality_note"]),
                    snippet=_squash(str(row["snippet"] or "")),
                )
            )
        unreadable = [hit for hit in result.hits if hit.text_quality == "unreadable"]
        if unreadable:
            result.warnings.append(
                f"{len(unreadable)} belgenin metni okunamadı (gömülü font); künyesi ve resmî "
                "bağlantısı verildi, içeriği aranmadı."
            )
        return result

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            days = connection.execute(
                "SELECT COUNT(*) AS scanned,"
                " SUM(CASE WHEN listed_count>0 THEN 1 ELSE 0 END) AS published,"
                " SUM(CASE WHEN complete=1 THEN 1 ELSE 0 END) AS complete,"
                " SUM(issue_count) AS issues,"
                " SUM(CASE WHEN issue_count>1 THEN 1 ELSE 0 END) AS with_mukerrer,"
                " MIN(date) AS oldest, MAX(date) AS newest FROM days"
            ).fetchone()
            documents = connection.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN text_quality='clean' THEN 1 ELSE 0 END) AS clean,"
                " SUM(CASE WHEN text_quality='suspect' THEN 1 ELSE 0 END) AS suspect,"
                " SUM(CASE WHEN text_quality='unreadable' THEN 1 ELSE 0 END) AS unreadable"
                " FROM documents"
            ).fetchone()
            by_kind = connection.execute(
                "SELECT kind, kind_label, COUNT(*) AS total FROM documents GROUP BY kind, kind_label"
                " ORDER BY total DESC"
            ).fetchall()
        pending = len(self.pending_days(1))
        return {
            "enabled": SYNC_ENABLED,
            "syncing": self._syncing,
            "floor": self.floor.isoformat(),
            "scanned_days": int(days["scanned"] or 0),
            "published_days": int(days["published"] or 0),
            "complete_days": int(days["complete"] or 0),
            "scanned_issues": int(days["issues"] or 0),
            "days_with_mukerrer": int(days["with_mukerrer"] or 0),
            "max_extra_issues": _MAX_EXTRA_ISSUES,
            "oldest_day": days["oldest"],
            "newest_day": days["newest"],
            "documents": int(documents["total"] or 0),
            "clean": int(documents["clean"] or 0),
            "suspect": int(documents["suspect"] or 0),
            "unreadable": int(documents["unreadable"] or 0),
            "kinds": [
                {"kind": row["kind"], "label": row["kind_label"], "documents": int(row["total"])}
                for row in by_kind
            ],
            "interests": [{"kind": key, "label": label} for key, label, _ in INTEREST_RULES],
            "has_pending_days": bool(pending),
            "last_run_at": self._get_metadata("last_run_at"),
            "days_per_run": self.days_per_run,
            "errors": list(self._errors[-5:]),
            "source_url": f"{self.base_url}/",
            "note": (
                "Arşiv seçicidir ve ekleyicidir: yalnız gümrük kararına giren belge aileleri "
                "alınır, yayımlanmış belge değişmediği için sürüm farkı tutulmaz. Her gün için "
                "normal sayı ve mükerrer sayılar birlikte taranır (gümrük tebliğlerinin "
                "neredeyse tamamı mükerrerde yayımlanıyor). Metni okunamayan belge künyesiyle "
                "saklanır, içeriği aranmaz."
            ),
        }
