"""ÖTV (III) sayılı listeyi resmî PDF'in tablo kılavuz çizgilerinden okur.

Neden ayrı bir okuyucu: ``data/official/excise_tax_lists.json`` tohumundaki (III)
sayılı liste **metin akışından** çıkarılmıştı ve satırlar kaydı; eşya adı bir sonraki
satıra, oran sütunları birbirine karışıyordu. Bu yüzden ``tax_lists.py`` o iki cetveli
``rates_verified: false`` sayıyor ve sorguda **hiçbir oran göstermiyor** — alkollü
içecek ve tütün mamulü ithalatında ÖTV kalemi boş kalıyordu.

Bu modül aynı PDF'i iki ölçülmüş olguya dayanarak okur (19.09.2026'da canlı doğrulandı,
varsayım değil):

1. **Tablo kılavuz çizgileri gerçekten var.** ``pymupdf.Page.find_tables()`` (III) sayılı
   listenin sayfalarında (A) cetvelini 6, (B) cetvelini 7 sütun olarak birebir döndürür;
   hücre sınırları çizgilerden gelir, metin sırasından tahmin edilmez.
2. **Dipnot işaretleri font boyutundan ayırt edilir.** Tablo metni 11-12 punto, Resmî
   Gazete dipnot üstsimgeleri (``59``, ``61`` …) 6,5-8 puntodur. Bu yüzden ``4559``
   aslında ``45`` oranı ve ``59`` dipnotudur; ``626,001859`` ise ``626,0018`` tutarı ve
   aynı dipnottur. Sayıyı desenle kesmek kırılgan olurdu (``65,25`` gerçek bir orandır),
   bu yüzden eşik **punto** üzerinedir.

Modül bilerek iki katmana ayrılmıştır:

* :func:`extract_pages` PDF'e dokunan ince katmandır (pymupdf, font süzgeci, hücre
  metni) ve çıktısı düz veridir.
* :func:`build_sections` **saftır**: o düz veriden satırları kurar. Testler resmî
  PDF'ten alınmış gerçek hücre dökümünü (``tests/fixtures/excise_iii_cells.json``) bu
  saf katmandan geçirir, böylece tohumdaki her satır depodaki kodun çıktısıdır.

Değişmezler:

* Ağ yok, dosya yazma yok. İndirme ve tohum yazma işi çağıranındır.
* Kılavuz çizgisi bulunamayan ya da başlığı beklenen sütunlara oturmayan sayfa **sessizce
  atlanmaz**: cetvelin ``warnings`` listesine yazılır ve uyarı varsa ``rates_verified``
  kendiliğinden ``False`` olur. Yarısı okunmuş bir cetveli "doğrulandı" saymak, beyanname
  verecek kullanıcıya eksik oran göstermek olurdu.
* Bu modül yalnız (III) sayılı listeyi okur. (I), (II) ve (IV) sayılı listelerin tohum
  satırları **değiştirilmez**; onların sayfalarında kılavuz çizgisi tespiti her sayfada
  çalışmıyor (ölçüldü: (II) sayılı listede iki sayfada tablo bulunamıyor) ve yarım bir
  yeniden okuma mevcut doğrulanmış veriyi bozardı.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

SOURCE_URL = "https://www.mevzuat.gov.tr/MevzuatMetin/1.5.4760.pdf"
SOURCE_LABEL = "4760 sayılı Özel Tüketim Vergisi Kanunu ekli (I)-(IV) sayılı listeler"
ALLOWED_HOSTS = ("www.mevzuat.gov.tr", "mevzuat.gov.tr")

#: Tablo metni 11-12 punto; Resmî Gazete dipnot üstsimgeleri 6,5-8 punto (ölçüldü).
MIN_FONT_SIZE = 10.0

#: (III) sayılı listenin başlangıç ve bitiş işaretleri.
LIST_START = "(III) SAYILI LİSTE"
LIST_END = "(IV) SAYILI LİSTE"
CETVEL_B_MARKER = "(B) CETVELİ"

_HEADER_FIRST_CELL = "G.T.İ.P. NO"

#: Resmî başlık metni → tohum sütun anahtarı. Başlıklar hücre içinde satırlara bölündüğü
#: için boşluklar sıkıştırılarak ve ön ekten karşılaştırılır.
_COLUMN_KEYS: tuple[tuple[str, str], ...] = (
    ("uygulanacak asgari maktu vergi tutarı", "applied_minimum_specific_tax"),
    ("uygulanacak maktu vergi tutarı", "applied_specific_tax"),
    ("uygulanacak vergi oranı", "applied_tax_rate"),
    ("asgari maktu vergi tutarı", "minimum_specific_tax"),
    ("maktu vergi tutarı", "specific_tax"),
    ("vergi oranı", "tax_rate"),
)

#: Kanun metnindeki iki kod biçimi: pozisyon (``22.04``) ve noktalı ulusal kod
#: (``2208.90.48.00.11``). "hariç" listeleri eşya adı hücresinde kaldığı için kod
#: sütununda yalnız bu iki biçim satır açar.
_CODE_RE = re.compile(r"^(?:\d{2}\.\d{2}|\d{4}(?:\.\d{2}){1,4})$")


class ExciseParseError(RuntimeError):
    """PDF (III) sayılı listeyi beklenen biçimde vermedi."""


@dataclass
class ExciseSection:
    """Tek cetvel: sütun düzeni, satırlar ve okuma uyarıları."""

    list_name: str
    cetvel: str
    value_columns: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_seed(self) -> dict[str, Any]:
        return {
            "list": self.list_name,
            "cetvel": self.cetvel,
            "value_columns": list(self.value_columns),
            "rates_verified": bool(self.rows) and not self.warnings,
            "row_count": len(self.rows),
            "rows": list(self.rows),
        }


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _has_values(row: dict[str, str]) -> bool:
    return any(key not in ("code", "description") for key in row)


def _column_keys(header: list[str]) -> list[str] | None:
    """Başlık hücrelerini tohum anahtarlarına çevirir; tanınmayan sütunda ``None``."""
    keys: list[str] = []
    for cell in header[2:]:
        normalised = _squash(cell).lower()
        match = next((key for label, key in _COLUMN_KEYS if normalised.startswith(label)), None)
        if match is None:
            return None
        keys.append(match)
    return keys


def _cell_text(page: Any, bbox: Any) -> str:
    """Hücre metnini yalnız gövde puntosundaki span'lardan toplar.

    Dipnot üstsimgesi eşiğin altında kaldığı için ``4559`` → ``45`` olur. Kırpma
    kutusuna **kısmen** giren span'lar da geldiğinden her span'ın merkezi kutunun içinde
    mi diye ayrıca bakılır; aksi hâlde geniş bir eşya adı komşu sütuna taşardı.
    """
    x0, y0, x1, y1 = bbox
    lines: list[str] = []
    for block in page.get_text("dict", clip=bbox).get("blocks", []):
        for line in block.get("lines", []):
            parts: list[str] = []
            for span in line.get("spans", []):
                if span.get("size", 0) < MIN_FONT_SIZE:
                    continue
                sx0, sy0, sx1, sy1 = span.get("bbox", (0, 0, 0, 0))
                cx, cy = (sx0 + sx1) / 2, (sy0 + sy1) / 2
                if not (x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1):
                    continue
                parts.append(span.get("text", ""))
            text = _squash("".join(parts))
            if text:
                lines.append(text)
    return _squash(" ".join(lines))


def extract_pages(payload: bytes) -> list[dict[str, Any]]:
    """(III) sayılı listenin sayfalarını düz hücre dökümüne çevirir.

    Çıktı her sayfa için ``{"page": 1'den başlayan numara, "cetvel_b": bool,
    "tables": [[hücre listesi, …], …]}``. Bu şekil hem :func:`build_sections`'ın girdisi
    hem de testlerdeki fikstürün biçimidir.
    """
    import pymupdf

    document = pymupdf.open(stream=io.BytesIO(payload), filetype="pdf")
    try:
        start = end = -1
        for index in range(document.page_count):
            text = document[index].get_text()
            if start < 0 and LIST_START in text:
                start = index
            elif start >= 0 and LIST_END in text:
                end = index
                break
        if start < 0:
            raise ExciseParseError(f"PDF'te {LIST_START} başlığı bulunamadı.")
        if end <= start:
            end = document.page_count
        pages: list[dict[str, Any]] = []
        for index in range(start, end):
            page = document[index]
            tables = [
                [[_cell_text(page, cell) if cell else "" for cell in row.cells] for row in table.rows]
                for table in page.find_tables().tables
            ]
            pages.append({
                "page": index + 1,
                "cetvel_b": CETVEL_B_MARKER in page.get_text(),
                "tables": tables,
            })
        return pages
    finally:
        document.close()


def build_sections(pages: list[dict[str, Any]]) -> list[ExciseSection]:
    """Hücre dökümünden (A) ve (B) cetvellerini kurar. Saf fonksiyon.

    Kodu olmayan satırın iki ayrı anlamı var ve ikisi karıştırılırsa veri kaybolur:

    * **Devam satırı** — kodu ve oran hücreleri boş, yalnız eşya adı var. Resmî tabloda
      uzun tanımlar sayfa sonunda bölünüyor; metin bir önceki satırın adına eklenir.
    * **Alt varyant** — kodu boş ama *kendi oran hücreleri dolu*. (B) cetvelinde
      ``2402.90.00.00.00`` böyledir: ana satırın oranı yoktur, altındaki iki tire'li
      varyant ("…purolar" ve "…sigaralar") farklı oran taşır. Bunları devam satırı
      saymak, tütün mamullerinde iki ayrı oranı tek satıra ezip yok etmek olurdu.
      Varyant ana kodu ve ana tanımı miras alır; ana satırın kendi oranı yoksa yalnız
      varyantlar kalır.

    Kodu olmayan ve bağlanacak önceki satırı da bulunmayan parça atılır, ama sayısı
    uyarıya yazılır — sessizce düşen satır olmaz.
    """
    sections: dict[str, ExciseSection] = {
        "A": ExciseSection("III", "A"),
        "B": ExciseSection("III", "B"),
    }
    current = "A"
    orphans = 0
    last_coded: dict[str, str] | None = None
    variants = 0
    for page in pages:
        number = page.get("page")
        if page.get("cetvel_b"):
            current = "B"
            last_coded = None
            variants = 0
        section = sections[current]
        tables = page.get("tables") or []
        if not tables:
            section.warnings.append(f"{number}. sayfada tablo kılavuz çizgisi bulunamadı.")
            continue
        for table in tables:
            for cells in table:
                if not cells:
                    continue
                if _squash(cells[0]).upper().startswith(_HEADER_FIRST_CELL):
                    keys = _column_keys(cells)
                    if keys is None:
                        section.warnings.append(
                            f"{number}. sayfadaki başlık satırı beklenen sütunlara oturmadı: "
                            + " | ".join(_squash(cell) for cell in cells[2:])
                        )
                        continue
                    if section.value_columns and section.value_columns != keys:
                        section.warnings.append(
                            f"{number}. sayfada sütun düzeni değişti: {keys} ≠ {section.value_columns}"
                        )
                    section.value_columns = keys
                    continue
                code = _squash(cells[0])
                description = _squash(cells[1]) if len(cells) > 1 else ""
                if not section.value_columns:
                    if code or description:
                        section.warnings.append(
                            f"{number}. sayfada başlık satırı okunmadan veri satırı geldi."
                        )
                    continue
                values: dict[str, str] = {}
                for offset, key in enumerate(section.value_columns):
                    position = offset + 2
                    value = _squash(cells[position]) if position < len(cells) else ""
                    if value:
                        values[key] = value
                if _CODE_RE.match(code.replace(" ", "")):
                    row: dict[str, str] = {"code": code, "description": description, **values}
                    section.rows.append(row)
                    last_coded = row
                    variants = 0
                    continue
                if not description:
                    continue
                if last_coded is None or not section.rows:
                    orphans += 1
                    continue
                if not values:
                    previous = section.rows[-1]
                    previous["description"] = _squash(previous["description"] + " " + description)
                    continue
                variants += 1
                if variants == 1 and not _has_values(last_coded) and last_coded in section.rows:
                    # Ana satırın kendi oranı yok; yalnız varyantları anlam taşıyor.
                    section.rows.remove(last_coded)
                section.rows.append({
                    "code": last_coded["code"],
                    "description": _squash(last_coded["description"] + " " + description),
                    **values,
                })
    if orphans:
        sections["A"].warnings.append(f"{orphans} satır parçası hiçbir koda bağlanamadı.")
    for section in sections.values():
        if not section.rows:
            section.warnings.append("Cetvelde hiç satır okunamadı.")
    return [sections["A"], sections["B"]]


def parse_list_iii(payload: bytes) -> list[ExciseSection]:
    """Resmî PDF baytlarından (III) sayılı listenin iki cetvelini döndürür."""
    return build_sections(extract_pages(payload))
