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

#: Her listenin PDF'teki başlangıç başlığı ve onu bitiren bir sonraki başlık.
#: (IV) sayılı listeyi ekli değişiklik cetvelleri bitirir, başka bir liste değil.
LIST_BOUNDS: dict[str, tuple[str, str]] = {
    "I": ("(I) SAYILI LİSTE", "(II) SAYILI LİSTE"),
    "II": ("(II) SAYILI LİSTE", "(III) SAYILI LİSTE"),
    "III": ("(III) SAYILI LİSTE", "(IV) SAYILI LİSTE"),
    "IV": ("(IV) SAYILI LİSTE", "4760 SAYILI KANUNUN EKİ CETVELLERDE"),
}

#: Geriye dönük kolaylık: modül (III) sayılı liste için yazılmıştı.
LIST_START = LIST_BOUNDS["III"][0]
LIST_END = LIST_BOUNDS["III"][1]
CETVEL_B_MARKER = "(B) CETVELİ"

_HEADER_FIRST_CELL = "G.T.İ.P. NO"

#: Resmî başlık metni → tohum sütun anahtarı. Başlıklar hücre içinde satırlara bölündüğü
#: için boşluklar sıkıştırılarak ve ön ekten karşılaştırılır; uzun etiket kısa olandan
#: önce denenir ("uygulanacak vergi oranı" ile "vergi oranı" birbirini yutmasın).
_COLUMN_KEYS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("uygulanacak asgari maktu vergi tutarı", "applied_minimum_specific_tax"),
            ("uygulanacak maktu vergi tutarı", "applied_specific_tax"),
            ("uygulanacak vergi oranı", "applied_tax_rate"),
            ("uygulanacak vergi tutarı", "applied_tax_amount"),
            ("asgari maktu vergi tutarı", "minimum_specific_tax"),
            ("maktu vergi tutarı", "specific_tax"),
            ("vergi oranı", "tax_rate"),
            ("vergi tutarı", "tax_amount"),
            ("birimi", "unit"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

#: Oran/tutar hücresinde beklenen tek değer: sayı, tire ya da ölçü birimi. Birden çok
#: sayı taşıyan hücre, resmî tabloda alt kırılımların tek hücrede birleşmesi demektir
#: (ör. (II) sayılı listede "150 150 220 10 40 50 60"); o satırın oranı hangi alt
#: kırılıma ait olduğu makineyle çözülemez ve satır doğrulanmamış sayılır.
_NUMERIC_RE = re.compile(r"\d")

#: Kanun metnindeki iki kod biçimi: pozisyon (``22.04``) ve noktalı ulusal kod
#: (``2208.90.48.00.11``). "hariç" listeleri eşya adı hücresinde kaldığı için kod
#: sütununda yalnız bu iki biçim satır açar.
_CODE_RE = re.compile(r"^(?:\d{2}\.\d{2}|\d{4}(?:\.\d{2}){1,4})$")


class ExciseParseError(RuntimeError):
    """PDF istenen listeyi beklenen biçimde vermedi."""


@dataclass
class ExciseSection:
    """Tek cetvel: sütun düzeni, satırlar ve okuma uyarıları.

    ``cetvel`` yalnız (A)/(B) diye ayrılan listelerde doludur; (II) ve (IV) sayılı
    listeler tek parçadır ve ``None`` taşır (mevcut tohum biçimiyle aynı).
    """

    list_name: str
    cetvel: str | None
    value_columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Başlık satırı görüldü mü (çözülebilmiş olsun olmasın).
    header_seen: bool = False

    @property
    def rates_verified(self) -> bool:
        return bool(self.rows) and not self.warnings

    def as_seed(self) -> dict[str, Any]:
        return {
            "list": self.list_name,
            "cetvel": self.cetvel,
            "value_columns": list(self.value_columns),
            "rates_verified": self.rates_verified,
            "row_count": len(self.rows),
            "rows": list(self.rows),
        }

    def warn(self, message: str) -> None:
        """Aynı uyarıyı tekrarlamaz: bir başlık hatası 24 satırda 24 kez yazılmasın."""
        if message not in self.warnings:
            self.warnings.append(message)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _has_values(row: dict[str, Any]) -> bool:
    return any(key not in ("code", "description", "rates_verified") for key in row)


def _is_collapsed(value: str) -> bool:
    """Hücrede birden çok değer var mı: alt kırılımlar tek hücrede birleşmiş demektir."""
    parts = value.split()
    return len(parts) > 1 and sum(1 for part in parts if _NUMERIC_RE.search(part)) > 1


def _column_keys(header: list[str]) -> list[str] | None:
    """Başlık hücrelerini tohum anahtarlarına çevirir.

    Tanınmayan sütunda **ve tekrar eden sütunda** ``None`` döner. Tekrar önemli:
    (II) sayılı listenin başlığı iki sütunu da "Uygulanacak Vergi Oranı" olarak
    veriyor, yani kanuni oran ile uygulanacak oran ayırt edilemiyor.
    """
    keys: list[str] = []
    for cell in header[2:]:
        normalised = _squash(cell).lower()
        match = next((key for label, key in _COLUMN_KEYS if normalised.startswith(label)), None)
        if match is None or match in keys:
            return None
        keys.append(match)
    return keys or None


def _normalise_rows(rows: list[list[str]]) -> list[list[str]]:
    """Dikey birleşmiş kod hücresini kendi satırına geri taşır.

    Resmî tabloda kimi kod hücresi iki görsel satırı kaplıyor ve metin **alt** banda
    düşüyor: üstteki satır kodsuz ama adlı ve oranlı, alttaki satır kodlu ama oransız
    geliyor. İki örnek ölçüldü:

    * ``2710.12.45.00.13`` (kurşunsuz benzin 95 oktan E10) — alttaki satır bomboş.
    * ``8517.69.90.90.24`` (halk bandı telsiz) — alttaki satırın adı var, oranı yok.

    Düzeltilmezse ad ve oran bir önceki kodun varyantı sayılır, gerçek kod ise oransız
    kalır. İki ölçüt birlikte kullanılır:

    1. **Oran nerede** — kodlu satırın oran hücreleri boşsa, üstteki kodsuz satırın
       oranı ona aittir. (III) sayılı listedeki gerçek varyantlarda kodsuz satırı
       izleyen kodlu satırın kendi oranı vardır, birleşme tetiklenmez.
    2. **Tire işareti** — resmî tabloda alt kırılımlar "-", "--", "---" ile başlar
       ((II) sayılı listede ``- Otobüs``, ``- Midibüs``, ``- Minibüs``). Tire ile
       başlayan satır bir önceki kodun alt kırılımıdır, sonraki kodun adının başı
       değildir; birleştirilirse minibüs oranı binek otomobile yazılırdı.
    """
    out: list[list[str]] = []
    index = 0
    while index < len(rows):
        current = rows[index]
        following = rows[index + 1] if index + 1 < len(rows) else None
        current_code = _squash(current[0]) if current else ""
        current_description = _squash(current[1]) if current and len(current) > 1 else ""
        if (
            following is not None
            and not current_code
            and not current_description.startswith(("-", "–", "—"))
            and any(_squash(cell) for cell in current[2:])
            and _CODE_RE.match(_squash(following[0]).replace(" ", ""))
            and not any(_squash(cell) for cell in following[2:])
        ):
            description = _squash(
                (_squash(current[1]) if len(current) > 1 else "")
                + " "
                + (_squash(following[1]) if len(following) > 1 else "")
            )
            out.append([_squash(following[0]), description, *current[2:]])
            index += 2
            continue
        out.append(current)
        index += 1
    return out


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


def extract_pages(payload: bytes, list_name: str = "III") -> list[dict[str, Any]]:
    """İstenen listenin sayfalarını düz hücre dökümüne çevirir.

    Çıktı her sayfa için ``{"page": 1'den başlayan numara, "cetvel_b": bool,
    "tables": [[hücre listesi, …], …]}``. Bu şekil hem :func:`build_sections`'ın girdisi
    hem de testlerdeki fikstürün biçimidir.
    """
    import pymupdf

    try:
        start_marker, end_marker = LIST_BOUNDS[list_name]
    except KeyError:
        raise ExciseParseError(f"Bilinmeyen liste: {list_name!r}") from None

    document = pymupdf.open(stream=io.BytesIO(payload), filetype="pdf")
    try:
        start = end = -1
        for index in range(document.page_count):
            text = document[index].get_text()
            if start < 0 and start_marker in text:
                start = index
            elif start >= 0 and end_marker in text:
                end = index
                break
        if start < 0:
            raise ExciseParseError(f"PDF'te {start_marker} başlığı bulunamadı.")
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


def build_sections(pages: list[dict[str, Any]], list_name: str = "III") -> list[ExciseSection]:
    """Hücre dökümünden bir listenin cetvellerini kurar. Saf fonksiyon.

    (I) ve (III) sayılı listeler (A)/(B) cetvellerine ayrılır; (II) ve (IV) tek parçadır
    ve tek bir ``cetvel=None`` bölümü üretir.

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
    split = any(page.get("cetvel_b") for page in pages)
    if split:
        sections: dict[str | None, ExciseSection] = {
            "A": ExciseSection(list_name, "A"),
            "B": ExciseSection(list_name, "B"),
        }
        current: str | None = "A"
    else:
        sections = {None: ExciseSection(list_name, None)}
        current = None
    orphans = 0
    last_coded: dict[str, Any] | None = None
    variants = 0
    for page in pages:
        number = page.get("page")
        if split and page.get("cetvel_b"):
            current = "B"
            last_coded = None
            variants = 0
        section = sections[current]
        tables = page.get("tables") or []
        if not tables:
            section.warn(f"{number}. sayfada tablo kılavuz çizgisi bulunamadı.")
            continue
        for table in tables:
            for cells in _normalise_rows(table):
                if not cells:
                    continue
                if _squash(cells[0]).upper().startswith(_HEADER_FIRST_CELL):
                    section.header_seen = True
                    keys = _column_keys(cells)
                    if keys is None:
                        # Sütunlar ayırt edilemiyor. Satırlar yine kurulur — kapsam
                        # bilgisi (hangi kod ÖTV'ye tabi) değerlidir — ama hiçbir oran
                        # taşınmaz; cetvel uyarı yüzünden zaten doğrulanmamış sayılır.
                        section.warn(
                            "Başlık satırı beklenen sütunlara oturmadı: "
                            + " | ".join(_squash(cell) for cell in cells[2:])
                        )
                        continue
                    if section.value_columns and section.value_columns != keys:
                        section.warn(
                            f"{number}. sayfada sütun düzeni değişti: {keys} ≠ {section.value_columns}"
                        )
                    section.value_columns = keys
                    continue
                code = _squash(cells[0])
                description = _squash(cells[1]) if len(cells) > 1 else ""
                if not section.header_seen:
                    if code or description:
                        section.warn("Başlık satırı okunmadan veri satırı geldi.")
                    continue
                values: dict[str, Any] = {}
                collapsed = False
                for offset, key in enumerate(section.value_columns):
                    position = offset + 2
                    value = _squash(cells[position]) if position < len(cells) else ""
                    if not value:
                        continue
                    if _is_collapsed(value):
                        collapsed = True
                    values[key] = value
                if collapsed:
                    # Alt kırılımlar tek hücrede birleşmiş; hangi oranın hangi kırılıma
                    # ait olduğu çözülemez. Satır kapsam bildirir, oran göstermez.
                    values["rates_verified"] = False
                if _CODE_RE.match(code.replace(" ", "")):
                    row: dict[str, Any] = {"code": code, "description": description, **values}
                    section.rows.append(row)
                    last_coded = row
                    variants = 0
                    continue
                if not description:
                    continue
                if last_coded is None or not section.rows:
                    # Listenin başındaki "(Hafif yağlar ve müstahzarları)" gibi kategori
                    # başlıkları hiçbir koda ait değildir ve oran taşımaz; atılması veri
                    # kaybı değildir. Oran taşıyan bir parça ise kaybolan bir satırdır.
                    if values:
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
    first = sections["A"] if split else sections[None]
    if orphans:
        first.warn(f"{orphans} satır parçası hiçbir koda bağlanamadı.")
    for section in sections.values():
        if not section.rows:
            section.warn("Cetvelde hiç satır okunamadı.")
    return [sections["A"], sections["B"]] if split else [sections[None]]


def parse_list(payload: bytes, list_name: str = "III") -> list[ExciseSection]:
    """Resmî PDF baytlarından istenen listenin cetvellerini döndürür."""
    return build_sections(extract_pages(payload, list_name), list_name)


def parse_list_iii(payload: bytes) -> list[ExciseSection]:
    """Geriye dönük ad: (III) sayılı listenin iki cetveli."""
    return parse_list(payload, "III")
