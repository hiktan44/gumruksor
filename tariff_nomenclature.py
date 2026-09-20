"""Türk Gümrük Tarife Cetveli: kod başına resmî **eşya tanımı**.

Bu modülün var olma sebebi ölçülmüş bir boşluk. Ürün, GTİP başına oranı, kontrol
şartını, önlemi ve yargı içtihadını biliyordu; bilmediği tek şey **eşyanın kendisinin
resmî tanımıydı**. Sonucu somut:

* ``/api/tariff/tree`` çocukları ``code`` ve oran alanları döndürüyor, ``description``
  **döndürmüyordu** — kullanıcı "8471.60.60 mı 8471.60.70 mi" sorusunu tarife
  ağacına bakarak cevaplayamıyordu;
* ``customs_advisor`` içinde ``nomenclature_matches`` alanı ve ona ait ``+10`` puan
  **yıllardır duruyordu**, ama besleyecek veri hiç yoktu (``grep -rn nomenclature
  hybrid_corpora.py tariff_engine.py`` → boş).

Nereden gelmediği de ölçüldü: İthalat Rejimi Kararı ekleri **tanım taşımıyor**.
``I sayılı Liste.xlsx`` sütunları tam olarak ``GTİP | DİPNOT | GÜMRÜK VERGİSİ ORANI``;
``tariff_engine`` bu yüzden 1. sütunu dipnot olarak okur ve doğru yapar.

Tanımlar ayrı bir resmî yayında ve makine okunur: **İstatistik Pozisyonlarına
Bölünmüş Türk Gümrük Tarife Cetveli** (CB Kararı 10781, RG 30.12.2025 / 33123
1. Mükerrer), Gümrükler Genel Müdürlüğünün duyurusundaki Excel arşivi. Fasıl başına
bir çalışma kitabı, sütunlar ``POZİSYON NO | EŞYANIN TANIMI | ÖLÇÜ BİRİMİ | 474
VERGİ HADDİ``; arşivde ayrıca 98 **fasıl notu** ve **Genel Yorum Kuralları** var.

Ayrıştırmada iki gerçek zorluk var, ikisi de ölçülerek çözüldü:

1. **Tanım satıra sığmıyor, alt satıra taşıyor.** 2026 cetvelinde 4.764 devam satırı
   ölçtüm. Devam satırı tire ile başlamaz; alt kırılım başlığı ``" - - "`` ile başlar.
   Ayrım bu; yoksa tanımların yarısı ortadan kesilirdi.
2. **Tanım tek başına anlamsız.** ``8471.30`` satırında yazan yalnızca "Portatif
   otomatik bilgi işlem makinaları…"dır; hangi aileye ait olduğunu üst satırlar söyler.
   Bu yüzden her kod için ata satırları birleştirilmiş **tam yol** tutulur. Ölçülen
   örnek::

       847130000000 → "… otomatik bilgi işlem makinaları ve bunlara ait birimler …
                        > Portatif otomatik bilgi işlem makinaları ( en az bir
                        merkezi işlem …"

**Pazarlık dışı ray — ``474 VERGİ HADDİ`` bir oran değildir.** O sütun kanuni azami
haddi (Bakanlar Kuruluna verilen yetki sınırını) taşır, uygulanan gümrük vergisini
taşımaz. Ölçtüm: 8401.10 için cetvel 15 der, İthalat Rejimi I sayılı liste AB menşe
için 0 der. Bu yüzden değer ``statutory_rate_text`` adıyla, **metin olarak** saklanır;
hiçbir hesaba girmez, hiçbir yerde "gümrük vergisi oranı" diye gösterilmez. Oran
yalnızca ``tariff_engine``'in resmî anlık görüntüsünden gelir.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx
import xlrd

from security_firewall import validate_outbound_url
from turkish_text import fold

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
SEED_FILE = ROOT / "tariff_nomenclature_sources.json"

#: Yalnız bu alan adları. Duyuru ve arşiv Gümrükler Genel Müdürlüğünde durur; bakanlık
#: kök alan adı yönlendirme hedefi olabildiği için o da listede.
_OFFICIAL_HOSTS = frozenset(
    {"ggm.ticaret.gov.tr", "ticaret.gov.tr", "www.ticaret.gov.tr"}
)

USER_AGENT = os.environ.get("MEVZUAT_USER_AGENT") or "gumruksor-nomenclature/1.0"

SYNC_INTERVAL_SECONDS = max(3600, int(os.environ.get("NOMENCLATURE_SYNC_SECONDS") or 86400))
SYNC_ENABLED = (os.environ.get("NOMENCLATURE_SYNC_ENABLED") or "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

#: Arşiv 2,7 MB ölçüldü; tavan kazara dev bir dosya indirmeyi engeller.
_MAX_ARCHIVE_BYTES = max(4_000_000, int(os.environ.get("NOMENCLATURE_MAX_BYTES") or 40_000_000))

#: Ayrıştırma sonucu bu eşiğin altına düşerse yeni anlık görüntü **yazılmaz**: kaynağın
#: düzeni değiştiyse yarım bir cetvel, eskisinin yerine geçmemeli.
_MIN_CODES = max(1000, int(os.environ.get("NOMENCLATURE_MIN_CODES") or 12000))

_DASH_PREFIX = re.compile(r"^[\s ]*(-[\s ]*)+")
_SECTION_HEADING = re.compile(r"^\s*(?:[IVXLC]+)\s*[\.\-]")
_HEADER_CODE_CELL = re.compile(r"pozisyon\s*no", re.IGNORECASE)
_CHAPTER_FILE = re.compile(r"(\d{1,2})\s*fas", re.IGNORECASE)
_NOTES_FILE = re.compile(r"fas[iı]l\s*(\d{1,2})\.xls", re.IGNORECASE)

#: Fasıl 77 nomenklatürde ayrılmıştır (ileride kullanılmak üzere saklı) ve 98/99
#: ulusal kullanımdır; kod doğrulaması bunları dışlamaz, yalnız 0 faslını reddeder.
_MAX_CHAPTER = 99


def normalise_code(value: Any) -> str:
    """``"8401.10.00.00.00"`` → ``"840110000000"``; geçersizse boş dize.

    Cetvel pozisyonları noktalı yazılır (``84.01``, ``8401.20``); depo ve tüm sorgular
    rakam dizisiyle çalışır. Tek haneli kalan ya da faslı geçersiz olan değer düşer.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 2 or len(digits) % 2:
        return ""
    chapter = int(digits[:2])
    if chapter < 1 or chapter > _MAX_CHAPTER:
        return ""
    return digits[:12]


def depth_of(text: str) -> int:
    """Tanımdaki baştaki tire sayısı: nomenklatür kırılım derinliği."""
    match = _DASH_PREFIX.match(text or "")
    return match.group(0).count("-") if match else 0


def strip_dashes(text: str) -> str:
    return _DASH_PREFIX.sub("", text or "").strip()


#: Satır sonunda kelimeyi bölen tire: ``"yakıt ele-"`` + ``"manları"`` → ``"elemanları"``.
#: Harfe yapışık olması şart; ``" - "`` kırılım işaretidir ve buraya girmez.
_WORD_BREAK_HYPHEN = re.compile(r"(?<=\w)-$")


def join_continuation(head: str, tail: str) -> str:
    """Taşan satırı birleştirir; kelimeyi bölen tireyi **kaldırır**.

    Resmî cetvel satır sonunda kelimeyi tireyle böler. Boşlukla birleştirmek
    ``"ele- manları"`` gibi iki bozuk parça üretir; ikisi de aranabilir kelime değildir
    ve "eleman" arayan kullanıcı satırı bulamaz. Tire harfe yapışıksa atılır ve iki
    parça boşluksuz birleşir.
    """
    head = (head or "").rstrip()
    tail = (tail or "").strip()
    if not tail:
        return head
    if not head:
        return tail
    if _WORD_BREAK_HYPHEN.search(head):
        return f"{_WORD_BREAK_HYPHEN.sub('', head)}{tail}"
    return f"{head} {tail}"


def is_continuation(text: str) -> bool:
    """Bu satır önceki tanımın kuyruğu mu?

    Devam satırının iki işareti var: tire ile **başlamaz** ve bölüm başlığı değildir
    (``I.``, ``II.`` gibi). Ölçülen örnek: ``8401`` başlığı iki satıra taşıyor ve
    ikinci satır ``"manları (kartuşlar); izotopik ayırım için makina ve …"`` diye
    ortadan başlıyor.
    """
    body = (text or "").strip()
    if not body:
        return False
    if depth_of(text):
        return False
    return not _SECTION_HEADING.match(body)


@dataclass(slots=True)
class NomenclatureRow:
    """Cetvelin tek satırı; ``full_path`` ata tanımlarıyla birleştirilmiş hâli."""

    code: str
    level: int
    description: str
    full_path: str
    unit: str = ""
    statutory_rate_text: str = ""
    chapter: str = ""
    parent_code: str = ""
    source_file: str = ""
    source_row: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "description": self.description,
            "full_path": self.full_path,
            "unit": self.unit,
            # Kasıtlı olarak "rate" değil: bu kanuni azami haddir, uygulanan oran değil.
            "statutory_rate_text": self.statutory_rate_text,
            "chapter": self.chapter,
            "parent_code": self.parent_code,
        }


def sheet_rows(data: bytes, extension: str) -> list[tuple[str, list[list[Any]]]]:
    """Çalışma kitabının ilk sayfasını satır listesine çevirir (``tariff_engine`` deseni)."""
    if extension == ".xls":
        book = xlrd.open_workbook(file_contents=data)
        return [
            (sheet.name, [list(sheet.row_values(index)) for index in range(sheet.nrows)])
            for sheet in book.sheets()
        ]
    if extension == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return [
            (sheet.title, [list(row) for row in sheet.iter_rows(values_only=True)])
            for sheet in workbook.worksheets
        ]
    return []


def _cell(row: Sequence[Any], index: int) -> str:
    if index >= len(row):
        return ""
    value = row[index]
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\n", " ").strip()


def parse_chapter_rows(rows: Sequence[Sequence[Any]], chapter: str = "") -> list[NomenclatureRow]:
    """Bir fasıl sayfasını kod satırlarına çevirir; tam yolu burada kurar.

    Başlık satırları (``POZİSYON NO`` ve onu izleyen ``1 | 2 | 3 | 4`` numaralandırması)
    atlanır. Kodsuz ama tireli satırlar **ağaç düğümü** olarak yaşar: 12 haneli kodun
    tam yolu onlardan geçer, ama kendileri kod olarak kaydedilmez.
    """
    out: list[NomenclatureRow] = []
    stack: dict[int, NomenclatureRow] = {}
    previous: NomenclatureRow | None = None
    started = False
    for number, row in enumerate(rows, start=1):
        code_cell = _cell(row, 0)
        text = _cell(row, 1)
        if not started:
            if _HEADER_CODE_CELL.search(code_cell) or _HEADER_CODE_CELL.search(text):
                started = True
            continue
        if not text and not code_cell:
            previous = None
            continue
        # Sütun numaralandırma satırı ("1 | 2 | 3 | 4"): veri değil.
        if code_cell == "1" and text == "2":
            previous = None
            continue
        code = normalise_code(code_cell)
        if not code and code_cell and not text:
            previous = None
            continue
        if not code and is_continuation(text) and previous is not None:
            previous.description = join_continuation(previous.description, text)
            previous.full_path = _rebuild_path(stack, previous)
            continue
        if not text:
            previous = None
            continue
        depth = depth_of(text)
        node = NomenclatureRow(
            code=code,
            level=len(code),
            description=strip_dashes(text),
            full_path="",
            unit=_cell(row, 2),
            statutory_rate_text=_cell(row, 3),
            chapter=chapter or code[:2],
            source_row=number,
        )
        for key in [key for key in stack if key >= depth]:
            del stack[key]
        stack[depth] = node
        node.full_path = _rebuild_path(stack, node)
        node.parent_code = _nearest_coded_ancestor(stack, depth)
        previous = node
        if code:
            out.append(node)
    return out


def _rebuild_path(stack: dict[int, NomenclatureRow], node: NomenclatureRow) -> str:
    parts = [stack[key].description for key in sorted(stack) if stack[key] is not node]
    parts.append(node.description)
    return " > ".join(part for part in parts if part)


def _nearest_coded_ancestor(stack: dict[int, NomenclatureRow], depth: int) -> str:
    for key in sorted((key for key in stack if key < depth), reverse=True):
        if stack[key].code:
            return stack[key].code
    return ""


def parse_notes_rows(rows: Sequence[Sequence[Any]]) -> str:
    """Fasıl notu / yorum kuralı sayfasını düz metne çevirir.

    Notlar sınıflandırmada **hukuken belirleyicidir** ("bu fasıla dahil değildir"),
    bu yüzden tanımların yanında saklanır. Biçim serbest metin; tek yapılan satırları
    boş satırları koruyarak birleştirmek.
    """
    lines: list[str] = []
    for row in rows:
        cells = [_cell(row, index) for index in range(len(row))]
        line = " ".join(cell for cell in cells if cell).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def load_sources(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or SEED_FILE)
    return json.loads(target.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ depo
class NomenclatureStore:
    """Cetvelin kalıcı kopyası. Eski anlık görüntü **silinmez**; ``active`` taşınır."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "tariff_nomenclature.sqlite3"
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL DEFAULT '',
                    archive_url TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    checked_at TEXT NOT NULL DEFAULT '',
                    code_count INTEGER NOT NULL DEFAULT 0,
                    note_count INTEGER NOT NULL DEFAULT 0,
                    chapter_count INTEGER NOT NULL DEFAULT 0,
                    legal_act TEXT NOT NULL DEFAULT '',
                    gazette_date TEXT NOT NULL DEFAULT '',
                    gazette_number TEXT NOT NULL DEFAULT '',
                    valid_from TEXT NOT NULL DEFAULT '',
                    discovery TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 0,
                    parse_warnings_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS codes (
                    snapshot_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    full_path TEXT NOT NULL DEFAULT '',
                    unit TEXT NOT NULL DEFAULT '',
                    statutory_rate_text TEXT NOT NULL DEFAULT '',
                    chapter TEXT NOT NULL DEFAULT '',
                    parent_code TEXT NOT NULL DEFAULT '',
                    source_file TEXT NOT NULL DEFAULT '',
                    source_row INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (snapshot_id, code)
                );
                CREATE INDEX IF NOT EXISTS idx_nom_codes_code ON codes(code);
                CREATE INDEX IF NOT EXISTS idx_nom_codes_parent ON codes(snapshot_id, parent_code);
                CREATE TABLE IF NOT EXISTS notes (
                    snapshot_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    chapter TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    source_file TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (snapshot_id, kind, chapter)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS codes_fts USING fts5(
                    code UNINDEXED, snapshot_id UNINDEXED, description, full_path,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ yazma
    def save_snapshot(
        self,
        *,
        snapshot_id: str,
        sha256: str,
        rows: Sequence[NomenclatureRow],
        notes: Sequence[dict[str, str]],
        source_url: str,
        archive_url: str,
        legal_act: str = "",
        gazette_date: str = "",
        gazette_number: str = "",
        valid_from: str = "",
        discovery: str = "",
        warnings: Sequence[str] = (),
        activate: bool = True,
    ) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        chapters = {row.chapter for row in rows if row.chapter}
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO snapshots(
                    id, source_url, archive_url, sha256, retrieved_at, checked_at, code_count,
                    note_count, chapter_count, legal_act, gazette_date, gazette_number,
                    valid_from, discovery, active, parse_warnings_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at
                """,
                (
                    snapshot_id, source_url, archive_url, sha256, now, now, len(rows),
                    len(notes), len(chapters), legal_act, gazette_date, gazette_number,
                    valid_from, discovery, 1 if activate else 0,
                    json.dumps(list(warnings), ensure_ascii=False),
                ),
            )
            connection.execute("DELETE FROM codes WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM notes WHERE snapshot_id=?", (snapshot_id,))
            connection.execute("DELETE FROM codes_fts WHERE snapshot_id=?", (snapshot_id,))
            connection.executemany(
                """
                INSERT INTO codes(
                    snapshot_id, code, level, description, full_path, unit,
                    statutory_rate_text, chapter, parent_code, source_file, source_row
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_id, code) DO UPDATE SET
                    level=excluded.level, description=excluded.description,
                    full_path=excluded.full_path, unit=excluded.unit,
                    statutory_rate_text=excluded.statutory_rate_text,
                    chapter=excluded.chapter, parent_code=excluded.parent_code,
                    source_file=excluded.source_file, source_row=excluded.source_row
                """,
                [
                    (
                        snapshot_id, row.code, row.level, row.description, row.full_path,
                        row.unit, row.statutory_rate_text, row.chapter, row.parent_code,
                        row.source_file, row.source_row,
                    )
                    for row in rows
                ],
            )
            connection.executemany(
                "INSERT INTO codes_fts(code, snapshot_id, description, full_path) VALUES(?,?,?,?)",
                [(row.code, snapshot_id, row.description, row.full_path) for row in rows],
            )
            connection.executemany(
                """
                INSERT INTO notes(snapshot_id, kind, chapter, title, body, source_file)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(snapshot_id, kind, chapter) DO UPDATE SET
                    title=excluded.title, body=excluded.body, source_file=excluded.source_file
                """,
                [
                    (
                        snapshot_id, note.get("kind", ""), note.get("chapter", ""),
                        note.get("title", ""), note.get("body", ""), note.get("source_file", ""),
                    )
                    for note in notes
                ],
            )
            if activate:
                connection.execute(
                    "UPDATE snapshots SET active=0 WHERE id<>?", (snapshot_id,)
                )
        return len(rows)

    def touch(self, snapshot_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE snapshots SET checked_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(timespec="seconds"), snapshot_id),
            )

    # ------------------------------------------------------------ okuma
    def active_snapshot(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE active=1 ORDER BY retrieved_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def snapshot_by_sha(self, sha256: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE sha256=? LIMIT 1", (sha256,)
            ).fetchone()
        return dict(row) if row else None

    def code_row(self, code: str, snapshot_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM codes WHERE snapshot_id=? AND code=?", (snapshot_id, code)
            ).fetchone()
        return dict(row) if row else None

    def children(self, parent: str, snapshot_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM codes WHERE snapshot_id=? AND parent_code=? ORDER BY code",
                (snapshot_id, parent),
            ).fetchall()
        return [dict(row) for row in rows]

    def descriptions_for(self, codes: Iterable[str], snapshot_id: str) -> dict[str, dict[str, Any]]:
        wanted = [code for code in {str(item or "") for item in codes} if code]
        if not wanted:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with self._connect() as connection:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT * FROM codes WHERE snapshot_id=? AND code IN ({placeholders})",
                    (snapshot_id, *chunk),
                ).fetchall()
                for row in rows:
                    out[row["code"]] = dict(row)
        return out

    def note(self, chapter: str, snapshot_id: str, kind: str = "chapter") -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notes WHERE snapshot_id=? AND kind=? AND chapter=?",
                (snapshot_id, kind, chapter),
            ).fetchone()
        return dict(row) if row else None

    def search(
        self, query: str, snapshot_id: str, *, limit: int = 20, code_prefix: str = ""
    ) -> list[dict[str, Any]]:
        """FTS araması; Türkçe ekler için **ön ek** eşleşmesi kullanılır.

        Türkçe eklemeli bir dil: "kıymet" araması "kıymeti"yi tam sözcük eşleşmesiyle
        bulmaz. ``resmi_gazete``/``ictihat`` ile aynı çözüm — her belirteç ``"x"*``.
        """
        tokens = [token for token in re.split(r"\W+", fold(query)) if len(token) > 1]
        if not tokens:
            return []
        expression = " AND ".join(f'"{token}"*' for token in tokens)
        sql = (
            "SELECT c.*, bm25(codes_fts) AS score FROM codes_fts"
            " JOIN codes c ON c.code = codes_fts.code AND c.snapshot_id = codes_fts.snapshot_id"
            " WHERE codes_fts MATCH ? AND codes_fts.snapshot_id = ?"
        )
        params: list[Any] = [expression, snapshot_id]
        if code_prefix:
            sql += " AND c.code LIKE ?"
            params.append(f"{code_prefix}%")
        sql += " ORDER BY score LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self._connect() as connection:
            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def export_rows(self, snapshot_id: str, *, limit: int = 40_000) -> list[dict[str, Any]]:
        """Toplu dışa aktarım satırları.

        ``full_path`` **taşınır, türetilmez**: yol kodsuz ağaç düğümlerinden de geçer
        (ör. "Uranyum izotoplarının ayırımına mahsus olanlar…" satırının kodu yoktur),
        dolayısıyla tüketici tarafta yalnız kodlu satırlardan yeniden kurulamaz.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT code, level, description, full_path, unit, chapter, parent_code
                FROM codes WHERE snapshot_id=? ORDER BY code LIMIT ?
                """,
                (snapshot_id, int(limit)),
            ).fetchall()
        return [
            {
                "code": row["code"], "level": row["level"], "description": row["description"],
                "full_path": row["full_path"], "unit": row["unit"], "chapter": row["chapter"],
                "parent_code": row["parent_code"],
            }
            for row in rows
        ]

    def counts(self, snapshot_id: str) -> dict[str, int]:
        with self._connect() as connection:
            by_level = connection.execute(
                "SELECT level, COUNT(*) AS n FROM codes WHERE snapshot_id=? GROUP BY level",
                (snapshot_id,),
            ).fetchall()
        return {str(row["level"]): int(row["n"]) for row in by_level}


class NomenclatureError(RuntimeError):
    """Cetvel indirilemedi veya ayrıştırılamadı."""


@dataclass(slots=True)
class NomenclatureLookup:
    """Tek kodun resmî tanımı. Oran alanı **yok** — bilinçli."""

    status: str
    code: str = ""
    matched_code: str = ""
    level: int = 0
    description: str = ""
    full_path: str = ""
    unit: str = ""
    statutory_rate_text: str = ""
    statutory_rate_note: str = ""
    chapter: str = ""
    chapter_note: str = ""
    parent_code: str = ""
    children: list[dict[str, Any]] = field(default_factory=list)
    source_url: str = ""
    archive_url: str = ""
    sha256: str = ""
    retrieved_at: str = ""
    legal_act: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status, "code": self.code, "matched_code": self.matched_code,
            "level": self.level, "description": self.description, "full_path": self.full_path,
            "unit": self.unit, "statutory_rate_text": self.statutory_rate_text,
            "statutory_rate_note": self.statutory_rate_note, "chapter": self.chapter,
            "chapter_note": self.chapter_note, "parent_code": self.parent_code,
            "children": list(self.children), "source_url": self.source_url,
            "archive_url": self.archive_url, "sha256": self.sha256,
            "retrieved_at": self.retrieved_at, "legal_act": self.legal_act,
            "warnings": list(self.warnings),
        }
        return data


STATUTORY_RATE_NOTE = (
    "Bu değer cetvelin '474 Vergi Haddi' sütunudur: kanuni azami hadd. Uygulanan gümrük "
    "vergisi değildir ve maliyet hesabına girmez; oran İthalat Rejimi Kararı ekinden okunur."
)


class NomenclatureEngine:
    """Cetveli indirir, ayrıştırır, saklar ve kod/metin sorgularına cevap verir."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        sources: dict[str, Any] | None = None,
        sync_interval_seconds: int = SYNC_INTERVAL_SECONDS,
        min_codes: int | None = None,
    ) -> None:
        self.store = NomenclatureStore(data_dir)
        self.sources = sources or load_sources()
        self.sync_interval_seconds = max(3600, int(sync_interval_seconds))
        # Ayrıştırma bu sayının altına düşerse yeni cetvel **yazılmaz**; kaynağın düzeni
        # değiştiğinde yarım bir cetvel çalışan bir cetvelin yerine geçmemeli.
        self.min_codes = max(1, int(min_codes if min_codes is not None else _MIN_CODES))
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={"User-Agent": USER_AGENT, "Referer": "https://ggm.ticaret.gov.tr/"},
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()
        self._syncing = False
        self._errors: list[str] = []
        # Değişiklik defteri ve inceleme kapısı sunucuda bağlanır (diğer motor deseni).
        self.ledger: Any = None
        self.review_policy: Any = None

    # ------------------------------------------------------------ ağ
    async def _get(self, url: str, *, binary: bool = False) -> tuple[bytes, str]:
        """Her adımda SSRF doğrulaması yapan indirme.

        ``follow_redirects=False``: yönlendirme elle izlenir ve **her hop** yeniden
        doğrulanır. Aksi hâlde resmî bir adres bizi başka bir alana taşıyabilirdi.
        """
        current = url
        for _ in range(5):
            validate_outbound_url(current, allowed_hosts=_OFFICIAL_HOSTS)
            response = await self._http.get(current)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location") or ""
                if not location:
                    raise NomenclatureError(f"Yönlendirme hedefi yok: {current}")
                current = str(httpx.URL(current).join(location))
                continue
            if response.status_code != 200:
                raise NomenclatureError(f"{current}: HTTP {response.status_code}")
            payload = response.content
            if binary and len(payload) > _MAX_ARCHIVE_BYTES:
                raise NomenclatureError(
                    f"Arşiv beklenenden büyük ({len(payload)} bayt); indirme reddedildi."
                )
            if binary and not payload.startswith(b"PK\x03\x04"):
                raise NomenclatureError("İndirilen dosya bir ZIP arşivi değil.")
            return payload, current
        raise NomenclatureError(f"Çok fazla yönlendirme: {url}")

    async def discover_archive(self) -> tuple[str, str, str]:
        """Arşiv bağlantısını bulur; ``(archive_url, landing_url, discovery)`` döner.

        Duyuru adresi yıla göre değişiyor (karar numarası slug'da), bu yüzden önce
        duyuru **dizini** taranır ve başlığı "tarife cetveli" ile eşleşen duyuru
        izlenir. Dizin bir şey vermezse yapılandırmadaki sabit duyuruya düşülür.
        Hangi yolun işlediği ``status()`` içinde ``discovery`` olarak görünür —
        varsayım değil, ölçüm.
        """
        source = self.sources["source"]
        index_url = str(source.get("index_url") or "")
        landing_url = str(source["landing_url"])
        needle = fold(str(source.get("announcement_match") or "tarife cetveli"))
        if index_url:
            try:
                html_bytes, _ = await self._get(index_url)
                html = html_bytes.decode("utf-8", errors="replace")
                for href, text in re.findall(r'href="([^"]+)"[^>]*>([^<]{0,160})', html):
                    if needle in fold(text) and "/duyuru" in href:
                        candidate = str(httpx.URL(index_url).join(href))
                        archive = await self._archive_link(candidate)
                        if archive:
                            return archive, candidate, "index"
            except (NomenclatureError, httpx.HTTPError, UnicodeDecodeError) as exc:
                self._errors.append(f"duyuru dizini okunamadı: {type(exc).__name__}")
        archive = await self._archive_link(landing_url)
        if not archive:
            raise NomenclatureError(
                "Cetvel duyurusunda Excel arşivi bağlantısı bulunamadı; kaynak düzeni değişmiş olabilir."
            )
        return archive, landing_url, "pinned"

    async def _archive_link(self, landing_url: str) -> str:
        payload, resolved = await self._get(landing_url)
        html = payload.decode("utf-8", errors="replace")
        for href in re.findall(r'href="([^"]+\.zip)"', html, flags=re.IGNORECASE):
            absolute = str(httpx.URL(resolved).join(href.replace(" ", "%20")))
            host = (httpx.URL(absolute).host or "").lower()
            if host in _OFFICIAL_HOSTS and "tgtc" in fold(absolute):
                return absolute
        return ""

    # ------------------------------------------------------------ ayrıştırma
    @staticmethod
    def parse_archive(data: bytes) -> tuple[list[NomenclatureRow], list[dict[str, str]], list[str]]:
        """ZIP arşivini kod satırlarına ve notlara çevirir.

        Fasıl dosyaları ``2026 TGTC/`` altında, fasıl notları ``FASIL NOTLARI/``
        altında; yorum kuralları ve açıklamalar arşiv kökünde. Dosya adları yıla göre
        değişiyor (``45 fasıl 2025.xls`` gibi), bu yüzden eşleme **desene** göre yapılır,
        sabit isme göre değil.
        """
        rows: list[NomenclatureRow] = []
        notes: list[dict[str, str]] = []
        warnings: list[str] = []
        seen: set[str] = set()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                name = item.filename
                leaf = name.rsplit("/", 1)[-1]
                extension = Path(leaf).suffix.lower()
                if extension not in {".xls", ".xlsx"}:
                    continue
                folded = fold(name)
                try:
                    sheets = sheet_rows(archive.read(item), extension)
                except Exception as exc:  # bozuk tek dosya tüm cetveli düşürmesin
                    warnings.append(f"{leaf}: okunamadı ({type(exc).__name__})")
                    continue
                if not sheets:
                    continue
                if "fasil notlari" in folded or "fasıl notları" in folded:
                    match = _NOTES_FILE.search(leaf)
                    chapter = f"{int(match.group(1)):02d}" if match else ""
                    body = parse_notes_rows(sheets[0][1])
                    if body:
                        notes.append(
                            {"kind": "chapter", "chapter": chapter,
                             "title": f"{chapter}. Fasıl notları" if chapter else leaf,
                             "body": body, "source_file": name}
                        )
                    continue
                if "yorum kural" in folded:
                    notes.append(
                        {"kind": "gri", "chapter": "", "title": "Genel Yorum Kuralları",
                         "body": parse_notes_rows(sheets[0][1]), "source_file": name}
                    )
                    continue
                if "aciklamalar" in folded or "açıklamalar" in folded:
                    notes.append(
                        {"kind": "explanation", "chapter": "", "title": "Açıklamalar",
                         "body": parse_notes_rows(sheets[0][1]), "source_file": name}
                    )
                    continue
                match = _CHAPTER_FILE.search(leaf)
                if not match:
                    continue
                chapter = f"{int(match.group(1)):02d}"
                parsed = parse_chapter_rows(sheets[0][1], chapter=chapter)
                if not parsed:
                    warnings.append(f"{leaf}: kod satırı bulunamadı")
                    continue
                for row in parsed:
                    # Aynı kod iki fasıl dosyasında görünmez; görünürse ilki kazanır ve
                    # durum uyarıya yazılır — sessizce üzerine yazmak veriyi bozar.
                    if row.code in seen:
                        warnings.append(f"{row.code}: yinelenen kod ({leaf})")
                        continue
                    seen.add(row.code)
                    row.source_file = name
                    rows.append(row)
        return rows, notes, warnings

    # ------------------------------------------------------------ eşitleme
    async def sync(self, *, force: bool = False) -> dict[str, Any]:
        """Cetveli indirir ve değiştiyse yeni anlık görüntü yazar.

        sha256 aynıysa **hiçbir şey yazılmaz**, yalnız ``checked_at`` tazelenir: aynı
        cetvel için ikinci bir kopya tutmak deponun boyutunu ikiye katlardı.
        """
        async with self._lock:
            self._syncing = True
            self._errors = []
            try:
                archive_url, landing_url, discovery = await self.discover_archive()
                payload, _ = await self._get(archive_url, binary=True)
                checksum = hashlib.sha256(payload).hexdigest()
                existing = self.store.snapshot_by_sha(checksum)
                if existing and not force:
                    self.store.touch(str(existing["id"]))
                    return {
                        "status": "unchanged", "sha256": checksum,
                        "snapshot_id": existing["id"], "code_count": existing["code_count"],
                        "discovery": discovery,
                    }
                rows, notes, warnings = self.parse_archive(payload)
                if len(rows) < self.min_codes:
                    raise NomenclatureError(
                        f"Yalnız {len(rows)} kod ayrıştırıldı (en az {self.min_codes} bekleniyor); "
                        "kaynak düzeni değişmiş olabilir, mevcut cetvel korunuyor."
                    )
                source = self.sources["source"]
                snapshot_id = f"nomenclature:{checksum[:16]}"
                self.store.save_snapshot(
                    snapshot_id=snapshot_id, sha256=checksum, rows=rows, notes=notes,
                    source_url=landing_url, archive_url=archive_url,
                    legal_act=str(source.get("legal_act") or ""),
                    gazette_date=str(source.get("gazette_date") or ""),
                    gazette_number=str(source.get("gazette_number") or ""),
                    valid_from=str(source.get("valid_from") or ""),
                    discovery=discovery, warnings=warnings, activate=True,
                )
                self._record_ledger(snapshot_id, checksum, landing_url, len(rows))
                return {
                    "status": "updated", "sha256": checksum, "snapshot_id": snapshot_id,
                    "code_count": len(rows), "note_count": len(notes),
                    "warnings": warnings[:20], "discovery": discovery,
                }
            except (NomenclatureError, httpx.HTTPError, zipfile.BadZipFile, ValueError) as exc:
                message = f"{type(exc).__name__}: {str(exc)[:220]}"
                self._errors.append(message)
                logger.warning("Tarife nomenklatürü eşitlenemedi: %s", message)
                return {"status": "error", "error": message}
            finally:
                self._syncing = False

    def _record_ledger(self, snapshot_id: str, checksum: str, source_url: str, count: int) -> None:
        ledger = self.ledger
        if ledger is None:
            return
        try:
            ledger.record_batch(
                kind="tariff_nomenclature", source_id="tgtc", new_snapshot=snapshot_id,
                source_url=source_url, sha256=checksum, added=count, removed=0, modified=0,
            )
        except Exception:
            logger.exception("Nomenklatür değişikliği deftere yazılamadı")

    async def periodic_sync_loop(self, initial_delay: float = 90.0) -> None:
        """Günlük eşitleme. Cetvel yılda bir kez değişir; sıklık dosya boyutuna göre düşük."""
        await asyncio.sleep(max(0.0, initial_delay))
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Nomenklatür eşitleme döngüsü hata verdi")
            await asyncio.sleep(self.sync_interval_seconds)

    # ------------------------------------------------------------ sorgu
    def lookup(self, code: str, *, with_children: bool = True) -> NomenclatureLookup:
        """Bir GTİP'in resmî tanımını döndürür; tam eşleşme yoksa **en yakın ata**.

        12 haneli bir kod cetvelde yoksa (ör. tarihsel bir karardan gelen kod) 10, 8, 6
        ve 4 haneye düşülür ve ``matched_code`` hangi seviyeden cevaplandığını söyler.
        Sessizce boş dönmek, kullanıcının elindeki kodu "tanımsız" göstermek olurdu.
        """
        snapshot = self.store.active_snapshot()
        if not snapshot:
            return NomenclatureLookup(
                status="unavailable",
                code=normalise_code(code),
                warnings=["Resmî tarife cetveli henüz indirilmedi."],
            )
        wanted = normalise_code(code)
        if not wanted:
            return NomenclatureLookup(status="invalid_code", code=str(code or "")[:20])
        snapshot_id = str(snapshot["id"])
        row = None
        matched = ""
        for width in (12, 10, 8, 6, 4, 2):
            if len(wanted) < width:
                continue
            candidate = wanted[:width]
            row = self.store.code_row(candidate, snapshot_id)
            if row:
                matched = candidate
                break
        if not row:
            return NomenclatureLookup(
                status="not_found", code=wanted,
                source_url=str(snapshot["source_url"]), sha256=str(snapshot["sha256"]),
                retrieved_at=str(snapshot["retrieved_at"]),
            )
        note = self.store.note(str(row["chapter"]), snapshot_id)
        warnings: list[str] = []
        if matched != wanted:
            warnings.append(
                f"{wanted} cetvelde bulunamadı; tanım {matched} üst pozisyonundan verildi."
            )
        return NomenclatureLookup(
            status="matched", code=wanted, matched_code=matched, level=int(row["level"]),
            description=str(row["description"]), full_path=str(row["full_path"]),
            unit=str(row["unit"]), statutory_rate_text=str(row["statutory_rate_text"]),
            statutory_rate_note=STATUTORY_RATE_NOTE if row["statutory_rate_text"] else "",
            chapter=str(row["chapter"]),
            chapter_note=(str(note["body"])[:4000] if note else ""),
            parent_code=str(row["parent_code"]),
            children=(
                [
                    {
                        "code": child["code"], "level": child["level"],
                        "description": child["description"], "unit": child["unit"],
                    }
                    for child in self.store.children(matched, snapshot_id)
                ]
                if with_children
                else []
            ),
            source_url=str(snapshot["source_url"]), archive_url=str(snapshot["archive_url"]),
            sha256=str(snapshot["sha256"]), retrieved_at=str(snapshot["retrieved_at"]),
            legal_act=str(snapshot["legal_act"]), warnings=warnings,
        )

    def search(self, query: str, *, limit: int = 20, code_prefix: str = "") -> dict[str, Any]:
        """Eşya tanımı metninde arama. Kod döndürür, **oran döndürmez**."""
        snapshot = self.store.active_snapshot()
        if not snapshot:
            return {"status": "unavailable", "count": 0, "hits": []}
        hits = self.store.search(
            query, str(snapshot["id"]), limit=limit, code_prefix=re.sub(r"\D", "", code_prefix or "")
        )
        return {
            "status": "ok" if hits else "no_match",
            "count": len(hits),
            "query": query,
            "hits": [
                {
                    "code": hit["code"], "level": hit["level"],
                    "description": hit["description"], "full_path": hit["full_path"],
                    "unit": hit["unit"], "chapter": hit["chapter"],
                }
                for hit in hits
            ],
            "source_url": str(snapshot["source_url"]),
            "sha256": str(snapshot["sha256"]),
            "retrieved_at": str(snapshot["retrieved_at"]),
        }

    def describe_many(self, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Toplu tanım: tarife ağacı çocuklarını tek sorguda zenginleştirmek için."""
        snapshot = self.store.active_snapshot()
        if not snapshot:
            return {}
        normalised = {normalise_code(code): code for code in codes}
        rows = self.store.descriptions_for(
            [code for code in normalised if code], str(snapshot["id"])
        )
        return {
            code: {
                "description": row["description"], "full_path": row["full_path"],
                "unit": row["unit"], "level": row["level"],
            }
            for code, row in rows.items()
        }

    def export(self, *, limit: int = 40_000) -> dict[str, Any]:
        """Cetvelin tamamını künyesiyle döndürür (TradeOne gibi tüketiciler için).

        Neden bir uç var: cetvel ``.xls`` biçiminde yayımlanıyor ve Node tarafında
        eski BIFF biçimini okuyan bakımlı bir paket yok; açık güvenlik danışmanlığı
        olan bir paketi bağımlılığa eklemek yerine ayrıştırma burada, tek yerde
        yapılır ve sonucu paylaşılır. Oran yok: yalnız tanım, ölçü ve hiyerarşi.
        """
        snapshot = self.store.active_snapshot()
        if not snapshot:
            return {"status": "unavailable", "count": 0, "rows": []}
        rows = self.store.export_rows(str(snapshot["id"]), limit=limit)
        return {
            "status": "ok",
            "count": len(rows),
            "snapshot_id": snapshot["id"],
            "sha256": snapshot["sha256"],
            "retrieved_at": snapshot["retrieved_at"],
            "source_url": snapshot["source_url"],
            "archive_url": snapshot["archive_url"],
            "legal_act": snapshot["legal_act"],
            "gazette_date": snapshot["gazette_date"],
            "gazette_number": snapshot["gazette_number"],
            "valid_from": snapshot["valid_from"],
            "statutory_rate_note": STATUTORY_RATE_NOTE,
            "rows": rows,
        }

    def status(self) -> dict[str, Any]:
        snapshot = self.store.active_snapshot()
        if not snapshot:
            return {
                "ready": False, "code_count": 0, "syncing": self._syncing,
                "errors": list(self._errors[-5:]),
                "note": "Resmî tarife cetveli henüz indirilmedi.",
            }
        return {
            "ready": True,
            "snapshot_id": snapshot["id"],
            "source_url": snapshot["source_url"],
            "archive_url": snapshot["archive_url"],
            "sha256": snapshot["sha256"],
            "retrieved_at": snapshot["retrieved_at"],
            "checked_at": snapshot["checked_at"],
            "legal_act": snapshot["legal_act"],
            "gazette_date": snapshot["gazette_date"],
            "gazette_number": snapshot["gazette_number"],
            "valid_from": snapshot["valid_from"],
            "discovery": snapshot["discovery"],
            "code_count": snapshot["code_count"],
            "note_count": snapshot["note_count"],
            "chapter_count": snapshot["chapter_count"],
            "codes_by_level": self.store.counts(str(snapshot["id"])),
            "parse_warnings": json.loads(str(snapshot["parse_warnings_json"] or "[]"))[:20],
            "syncing": self._syncing,
            "errors": list(self._errors[-5:]),
            "statutory_rate_note": STATUTORY_RATE_NOTE,
        }

    def summary_lines(self) -> list[str]:
        status = self.status()
        if not status.get("ready"):
            return ["Resmî eşya tanımı cetveli henüz indirilmedi."]
        return [
            f"Türk Gümrük Tarife Cetveli: {status['code_count']} pozisyon, "
            f"{status['chapter_count']} fasıl ({status['retrieved_at'][:10]}).",
            f"Kaynak: {status['legal_act'] or status['source_url']} · sha256 {str(status['sha256'])[:12]}…",
            STATUTORY_RATE_NOTE,
        ]

    async def close(self) -> None:
        await self._http.aclose()
