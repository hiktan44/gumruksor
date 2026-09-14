"""GTİP bazlı KDV oranı önerisi (2007/13033 sayılı BKK eki (I) ve (II) sayılı listeler).

3065 sayılı KDV Kanunu md. 28 uyarınca oranlar Cumhurbaşkanı kararıyla belirlenir;
yürürlükteki temel karar 24/12/2007 tarihli 2007/13033 sayılı Karardır. Ekli
(I) sayılı listedeki teslimler %1, (II) sayılı listedekiler %10, listelerde yer
almayanlar genel orana (%20) tabidir.

Listeler GTİP tablosu değil; fasıl ve pozisyonlara atıf yapan, şart ve istisna
içeren anlatı satırlarıdır. Bu modül:

* ``parse_vat_decision_text`` – konsolide karar metninden liste satırlarını, GTİP /
  pozisyon / fasıl atıflarını, "hariç" parantezlerini ve şart sözcüklerini çıkarır;
* ``VatRateIndex`` – ``data/official/vat_lists.json`` tohumunu (ya da
  ``MEVZUAT_DATA_DIR`` altındaki eşitlenmiş kopyayı) yükler ve ``lookup(gtip)`` ile
  en özel eşleşmeyi döndürür. Aynı özgüllükte farklı oranlar varsa ya da satır
  "kullanılmış / toptan / perakende" gibi GTİP'ten okunamayan bir şarta bağlıysa
  sonuç ``ambiguous=True`` olur ve adaylar birlikte verilir. Eşleşme yoksa
  ``tax_lists.estimate_vat_rate`` sezgiseline düşülür (``basis="heuristic"``).

Oranlar yalnız **öneridir**; maliyet hesabına kullanıcı onayıyla girer.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from security_firewall import validate_outbound_url
from tax_lists import heuristic_vat_lookup
from trade_measures import USER_AGENT, normalise_code, official_ssl_context

logger = logging.getLogger(__name__)

DATA_FILE = "vat_lists.json"
SOURCE_LABEL = "2007/13033 sayılı BKK eki (I) ve (II) sayılı listeler (mevzuat.gov.tr konsolide metin)"
SOURCE_URL = "https://www.mevzuat.gov.tr/mevzuat?MevzuatNo=200713033&MevzuatTur=3&MevzuatTertip=5"
IFRAME_URL = "https://www.mevzuat.gov.tr/anasayfa/MevzuatFihristDetayIframe?MevzuatTur=3&MevzuatNo=200713033&MevzuatTertip=5"
ALLOWED_HOSTS = ("www.mevzuat.gov.tr", "mevzuat.gov.tr")
PARSER_VERSION = 1
MIN_ROWS_FOR_REPLACE = 30
LIST_RATES = {"I": 1.0, "II": 10.0}
GENERAL_RATE = 20.0
GENERAL_LEGAL_BASIS = "3065 sayılı KDV Kanunu md. 28 (genel oran)"
DEFAULT_SYNC_INTERVAL = int(os.environ.get("VAT_LISTS_SYNC_INTERVAL_SECONDS", "86400"))
SUGGESTION_NOTE = "Öneridir; beyanname öncesi yürürlükteki oranı ve satırdaki şartları doğrulayın."

# Satır metninde geçen ve uygulamayı GTİP dışında bir olguya bağlayan sözcükler.
# Anahtar: sadeleştirilmiş (ASCII) kök; değer: kullanıcıya gösterilecek etiket.
CONDITION_KEYWORDS: dict[str, str] = {
    r"\bperakende": "perakende",
    r"\btoptan(?!ci)": "toptan",
    r"\btoptanci\s+hal": "toptancı hal",
    r"\bambalaj": "ambalaj",
    r"\bsanayi": "sanayi",
    r"\btohumluk": "tohumluk",
    r"\bsertifikali": "sertifikalı",
    r"\bdamizlik": "damızlık",
    r"\bharic": "hariç",
    r"\bkullanilmis": "kullanılmış",
    r"\bposetlen": "poşetlenerek",
    r"\byalniz": "yalnız",
    r"\bfason": "fason",
    r"\bruhsat": "ruhsat",
}
# Bu şartlar aynı GTİP için hem indirimli hem genel oranı mümkün kılar (kod ayırt etmez).
AMBIGUITY_CONDITIONS = {"kullanılmış", "poşetlenerek", "toptan", "perakende", "toptancı hal"}

_CHAPTER_SUFFIX = r"(?:\.|inci|nci|üncü|ncü|uncu|ncu|no\.?\s*lu|no'lu|numaralı)?"
_CODE_RE = re.compile(
    r"(?<![\d.,])(?:"
    r"\d{4}(?:\.\d{2}){1,4}"                                    # 8701.90.50.00.00 / 2303.10
    r"|\d{2}\.\d{2}"                                             # 87.03
    r"|\d{4}(?=\s*(?:(?:ila|-|–)\s*\d{4}\s*)?(?:tarife\s+)?pozisyon)"  # 4901 pozisyonu
    r")(?![\d.])"
)
_HEADING_RANGE_RE = re.compile(
    r"(?<![\d.,])(\d{4}|\d{2}\.\d{2})\s*(?:ila|-|–)\s*(\d{4}|\d{2}\.\d{2})\s*(?:tarife\s+)?pozisyon",
    re.IGNORECASE,
)
_CHAPTER_RANGE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:ila|ile|-|–|—)\s*(\d{1,2})\s*" + _CHAPTER_SUFFIX + r"\s*fas(?:ıl|l)",
    re.IGNORECASE,
)
_CHAPTER_LIST_RE = re.compile(
    r"(?<![\d.])((?:\d{1,2}\s*(?:,|ve)\s*)+\d{1,2})\s*" + _CHAPTER_SUFFIX + r"\s*fas(?:ıl|l)",
    re.IGNORECASE,
)
_CHAPTER_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*" + _CHAPTER_SUFFIX + r"\s*fas(?:ıl|l)", re.IGNORECASE)
_ROW_RE = re.compile(r"^\s*(\d{1,3})\s*[-–—)]\s*(?=\S)")
_SECTION_RE = re.compile(r"^\s*([A-ZÇĞİÖŞÜ])\)\s*(\S.*)$")
_LIST_RE = re.compile(r"^\(?\s*(i{1,2})\s*\)\s*sayili\s+liste(?![a-z])")
_FOOTNOTE_RE = re.compile(r"^\s*\(\d{1,2}\)\s")
_ARTICLE_RE = re.compile(r"^\s*(?:gecici\s+|ek\s+)?madde\s")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<\s*(?:br|/p|/div|/tr|/li|/h[1-6]|/table|/section)\b[^>]*>", re.IGNORECASE)
_CELL_RE = re.compile(r"<\s*/t[dh]\b[^>]*>", re.IGNORECASE)


def _fold(text: str) -> str:
    """Küçük harf + Türkçe harfler sadeleştirilmiş (başlık ve şart eşleştirme için)."""
    lowered = (text or "").replace("İ", "i").replace("I", "ı").lower().replace("\u0307", "")
    return lowered.translate(str.maketrans("çğıöşü", "cgiosu"))


def _html_to_text(markup: str) -> str:
    """Resmî sayfadan satır satır düz metin: blok kapanışları satır sonu, hücreler boşluk."""
    body = _SCRIPT_RE.sub(" ", markup or "")
    body = _BREAK_RE.sub("\n", body)
    body = _CELL_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    body = html_lib.unescape(body).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def _is_upper_title(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 3:
        return False
    return sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.7


def _split_exclusions(text: str) -> tuple[str, str]:
    """Dengeli parantez/köşeli parantez gruplarından 'hariç' içerenleri ayırır.

    Döndürür: (istisna grupları çıkarılmış metin, istisna gruplarının birleşimi).
    """
    positive: list[str] = []
    excluded: list[str] = []
    depth = 0
    buffer: list[str] = []
    for ch in text:
        if ch in "([":
            if depth == 0:
                buffer = []
            depth += 1
            buffer.append(ch)
            continue
        if ch in ")]" and depth:
            depth -= 1
            buffer.append(ch)
            if depth == 0:
                group = "".join(buffer)
                if "haric" in _fold(group):
                    excluded.append(group)
                else:
                    positive.append(group)
                buffer = []
            continue
        (buffer if depth else positive).append(ch)
    if depth and buffer:  # kapanmamış parantez – metni kaybetme
        positive.append("".join(buffer))
    return "".join(positive), " ".join(excluded)


def _expand_heading_range(start: str, end: str) -> list[str]:
    a, b = normalise_code(start), normalise_code(end)
    if len(a) != 4 or len(b) != 4 or int(a) > int(b) or int(b) - int(a) > 200:
        return []
    return [f"{n:04d}" for n in range(int(a), int(b) + 1)]


def _extract_codes(text: str) -> list[str]:
    codes: list[str] = []
    for start, end in _HEADING_RANGE_RE.findall(text):
        for code in _expand_heading_range(start, end):
            if code not in codes:
                codes.append(code)
    for match in _CODE_RE.finditer(text):
        raw = match.group(0)
        digits = normalise_code(raw)
        if len(digits) < 4 or len(digits) > 12 or int(digits[:2]) == 0 or int(digits[:2]) > 97:
            continue
        if raw not in codes and digits not in codes:
            codes.append(raw)
    return codes


def _extract_chapter_ranges(text: str) -> list[list[int]]:
    ranges: list[list[int]] = []
    spans: list[tuple[int, int]] = []

    def add(a: int, b: int) -> None:
        if 1 <= a <= b <= 97 and [a, b] not in ranges:
            ranges.append([a, b])

    for match in _CHAPTER_RANGE_RE.finditer(text):
        add(int(match.group(1)), int(match.group(2)))
        spans.append(match.span())
    for match in _CHAPTER_LIST_RE.finditer(text):
        if any(s <= match.start() < e for s, e in spans):
            continue
        for number in re.findall(r"\d{1,2}", match.group(1)):
            add(int(number), int(number))
        spans.append(match.span())
    for match in _CHAPTER_RE.finditer(text):
        if any(s <= match.start() < e for s, e in spans):
            continue
        add(int(match.group(1)), int(match.group(1)))
    return ranges


def _extract_conditions(text: str) -> list[str]:
    folded = _fold(text)
    found = [label for pattern, label in CONDITION_KEYWORDS.items() if re.search(pattern, folded)]
    return found


def _is_conditional(conditions: list[str]) -> bool:
    present = AMBIGUITY_CONDITIONS & set(conditions)
    if {"toptan", "perakende"} <= present:
        present -= {"toptan", "perakende"}
    return bool(present)


def legal_basis_for(list_name: str, section: str | None, row_no: str | None) -> str:
    base = f"2007/13033 s. BKK eki ({list_name}) sayılı liste"
    if section and row_no:
        return f"{base}, {section}/{row_no}"
    if row_no:
        return f"{base}, {row_no}"
    return base


def _finalise_row(row: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", row["text"]).strip()
    positive, excluded = _split_exclusions(text)
    conditions = _extract_conditions(text)
    return {
        "list": row["list"],
        "rate": LIST_RATES[row["list"]],
        "section": row.get("section"),
        "row_no": row.get("row_no"),
        "text": text[:2000],
        "gtip_expressions": _extract_codes(positive),
        "excluded_expressions": _extract_codes(excluded),
        "chapter_ranges": _extract_chapter_ranges(positive),
        "conditions": conditions,
        "conditional": _is_conditional(conditions),
        "legal_basis": legal_basis_for(row["list"], row.get("section"), row.get("row_no")),
        "verified": True,
    }


def parse_vat_decision_text(text: str) -> list[dict[str, Any]]:
    """2007/13033 konsolide metninden (I) ve (II) sayılı liste satırlarını çıkarır.

    Satır işareti taşımayan satırlar bir önceki satırın devamı sayılır (sarılmış
    metin). Liste başlığından önceki karar maddeleri ve satır sonundaki dipnotlar
    atlanır. Her satır: liste, oran, bölüm harfi, sıra no, GTİP/pozisyon ifadeleri,
    "hariç" parantezindeki ifadeler, fasıl aralıkları, şart sözcükleri ve dayanak.
    """
    rows: list[dict[str, Any]] = []
    current_list: str | None = None
    section: str | None = None
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current["text"].strip():
            rows.append(_finalise_row(current))
        current = None

    for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        folded = _fold(line)
        header = _LIST_RE.match(folded)
        if header:
            flush()
            current_list = "I" if len(header.group(1)) == 1 else "II"
            section = None
            continue
        if current_list is None:
            continue
        if _ARTICLE_RE.match(folded) or _FOOTNOTE_RE.match(line):
            flush()
            continue
        section_match = _SECTION_RE.match(line)
        if section_match and _is_upper_title(section_match.group(2)):
            flush()
            section = section_match.group(1)
            continue
        row_match = _ROW_RE.match(line)
        if row_match:
            flush()
            current = {"list": current_list, "section": section, "row_no": row_match.group(1), "text": line[row_match.end():]}
            continue
        if current is not None:
            current["text"] += " " + line
    flush()
    return rows


def _default_data_dir() -> Path:
    override = os.environ.get("OFFICIAL_DATA_DIR")
    if override:
        return Path(override)
    candidates = [
        Path(__file__).resolve().parent / "data" / "official",
        Path(sys.prefix) / "data" / "official",
        Path.cwd() / "data" / "official",
    ]
    for candidate in candidates:
        if (candidate / DATA_FILE).exists():
            return candidate
    return candidates[0]


def _default_cache_dir() -> Path:
    override = os.environ.get("MEVZUAT_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "mevzuat-mcp"


class VatRateIndex:
    """GTİP -> KDV listesi eşlemesi; tohumdan ya da eşitlenmiş önbellekten yüklenir."""

    def __init__(self, data_dir: str | Path | None = None, cache_dir: str | Path | None = None) -> None:
        self._seed_path = Path(data_dir or _default_data_dir()) / DATA_FILE
        self._cache_path = Path(cache_dir or _default_cache_dir()) / DATA_FILE
        self._payload: dict[str, Any] = {}
        self._rows: list[dict[str, Any]] = []
        self._compiled: list[dict[str, Any]] = []
        self._origin = "none"
        self._last_error: str | None = None
        self._lock = asyncio.Lock()
        self.sync_interval = DEFAULT_SYNC_INTERVAL
        self._load()

    # ---- loading
    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.exception("KDV liste dosyası okunamadı: %s", path)
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            return None
        return payload

    def _load(self) -> None:
        cached = self._read(self._cache_path) if self._cache_path != self._seed_path else None
        if cached and cached.get("parser_version") == PARSER_VERSION and len(cached["rows"]) >= MIN_ROWS_FOR_REPLACE:
            self._install(cached, "synced")
            return
        seed = self._read(self._seed_path)
        if seed:
            self._install(seed, "seed")
        else:
            logger.warning("KDV liste tohumu bulunamadı: %s", self._seed_path)

    def _install(self, payload: dict[str, Any], origin: str) -> None:
        self._payload = payload
        self._rows = [row for row in payload.get("rows", []) if isinstance(row, dict) and row.get("list") in LIST_RATES]
        self._origin = origin
        self._build()

    def _build(self) -> None:
        compiled: list[dict[str, Any]] = []
        for row in self._rows:
            exprs = []
            for raw in row.get("gtip_expressions") or []:
                digits = normalise_code(str(raw))
                if 2 <= len(digits) <= 12:
                    exprs.append((digits, str(raw)))
            excluded = []
            for raw in row.get("excluded_expressions") or []:
                digits = normalise_code(str(raw))
                if 2 <= len(digits) <= 12:
                    excluded.append((digits, str(raw)))
            chapters: list[tuple[int, int]] = []
            for item in row.get("chapter_ranges") or ([row["chapter_range"]] if row.get("chapter_range") else []):
                try:
                    a, b = int(item[0]), int(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if 1 <= a <= b <= 99:
                    chapters.append((a, b))
            rate = row.get("rate")
            try:
                rate = float(rate) if rate is not None else LIST_RATES[row["list"]]
            except (TypeError, ValueError):
                rate = LIST_RATES[row["list"]]
            compiled.append({"row": row, "rate": rate, "exprs": exprs, "excluded": excluded, "chapters": chapters})
        self._compiled = compiled

    # ---- status
    @property
    def ready(self) -> bool:
        return bool(self._compiled)

    def status(self) -> dict[str, Any]:
        counts = {"I": 0, "II": 0}
        for row in self._rows:
            counts[row["list"]] = counts.get(row["list"], 0) + 1
        return {
            "ready": self.ready,
            "row_count": len(self._rows),
            "row_counts": counts,
            "source": self._payload.get("source") or SOURCE_LABEL,
            "source_url": self._payload.get("source_url") or SOURCE_URL,
            "retrieved_at": self._payload.get("retrieved_at"),
            "sha256": self._payload.get("sha256"),
            "parser_version": self._payload.get("parser_version"),
            "origin": self._origin,
            "last_error": self._last_error,
            "cache_path": str(self._cache_path),
            "rates": LIST_RATES,
            "general_rate": GENERAL_RATE,
        }

    # ---- lookup
    def _candidate(self, entry: dict[str, Any], expression: str | None, coverage: str) -> dict[str, Any]:
        row = entry["row"]
        return {
            "rate": entry["rate"],
            "list": row["list"],
            "legal_basis": row.get("legal_basis") or legal_basis_for(row["list"], row.get("section"), row.get("row_no")),
            "matched_expression": expression,
            "coverage": coverage,
            "row_text": (row.get("text") or "")[:240],
            "conditions": list(row.get("conditions") or []),
            "verified": bool(row.get("verified", False)),
            "section": row.get("section"),
            "row_no": row.get("row_no"),
        }

    def _base(self, code: str) -> dict[str, Any]:
        return {
            "gtip": code,
            "source": self._payload.get("source") or SOURCE_LABEL,
            "source_url": self._payload.get("source_url") or SOURCE_URL,
            "retrieved_at": self._payload.get("retrieved_at"),
            "note": SUGGESTION_NOTE,
        }

    def lookup(self, gtip: str) -> dict[str, Any]:
        """Sorgulanan GTİP için KDV oranı önerisi.

        Öncelik: en uzun GTİP/pozisyon ön eki > fasıl aralığı. Aynı özgüllükte farklı
        oranlar ya da GTİP'ten okunamayan bir şart varsa ``ambiguous=True`` ve
        ``rate=None`` döner; adaylar ``candidates`` içindedir. Listede eşleşme yoksa
        sezgisel kural (``basis="heuristic"``) kullanılır.
        """
        code = normalise_code(gtip)
        if not code or not self._compiled:
            return heuristic_vat_lookup(gtip)
        chapter = int(code[:2]) if len(code) >= 2 else None
        matches: list[tuple[int, int, dict[str, Any], str | None, str]] = []
        exclusions: list[dict[str, Any]] = []
        for index, entry in enumerate(self._compiled):
            spec, expression, coverage = 0, None, "full"
            for digits, raw in entry["exprs"]:
                if code.startswith(digits):
                    candidate_spec, candidate_cov = len(digits), "full"
                elif digits.startswith(code):
                    candidate_spec, candidate_cov = len(code), "partial"
                else:
                    continue
                if candidate_spec > spec or (candidate_spec == spec and candidate_cov == "full" and coverage == "partial"):
                    spec, expression, coverage = candidate_spec, raw, candidate_cov
            if chapter is not None and spec < 2:
                for a, b in entry["chapters"]:
                    if a <= chapter <= b:
                        spec, expression, coverage = 2, (f"{a}. fasıl" if a == b else f"{a}-{b}. fasıllar"), "full"
                        break
            if not spec:
                continue
            excluded_spec, excluded_expr = 0, None
            for digits, raw in entry["excluded"]:
                if code.startswith(digits) and len(digits) > excluded_spec:
                    excluded_spec, excluded_expr = len(digits), raw
            if excluded_spec and excluded_spec >= spec:
                exclusions.append(self._candidate(entry, excluded_expr, "excluded"))
                continue
            matches.append((spec, index, entry, expression, coverage))

        if not matches:
            if exclusions:
                first = exclusions[0]
                result = {
                    **self._base(code),
                    "rate": GENERAL_RATE,
                    "basis": "official_list",
                    "list": None,
                    "legal_basis": f"{GENERAL_LEGAL_BASIS}; {first['legal_basis']} satırında hariç tutulmuştur",
                    "matched_expression": first["matched_expression"],
                    "row_text": first["row_text"],
                    "conditions": first["conditions"],
                    "ambiguous": False,
                    "candidates": [],
                    "exclusions": exclusions[:5],
                    "verified": first["verified"],
                }
                return result
            fallback = heuristic_vat_lookup(code)
            fallback["note"] = SUGGESTION_NOTE
            return fallback

        top = max(item[0] for item in matches)
        winners = [item for item in matches if item[0] == top]
        candidates = [self._candidate(entry, expression, coverage) for _, _, entry, expression, coverage in winners]
        rates = {candidate["rate"] for candidate in candidates}
        ambiguous = len(rates) > 1
        if not ambiguous and any(entry["row"].get("conditional") for _, _, entry, _, _ in winners):
            candidates.append({
                "rate": GENERAL_RATE,
                "list": None,
                "legal_basis": f"{GENERAL_LEGAL_BASIS} – satırdaki şart sağlanmazsa",
                "matched_expression": None,
                "coverage": "conditional",
                "row_text": None,
                "conditions": [],
                "verified": True,
                "section": None,
                "row_no": None,
            })
            ambiguous = True

        result = {**self._base(code), "basis": "official_list", "ambiguous": ambiguous, "candidates": candidates[:6]}
        if ambiguous:
            bases: list[str] = []
            for candidate in candidates:
                if candidate["legal_basis"] not in bases:
                    bases.append(candidate["legal_basis"])
            result.update({
                "rate": None,
                "list": None,
                "legal_basis": " / ".join(bases),
                "matched_expression": candidates[0]["matched_expression"],
                "row_text": candidates[0]["row_text"],
                "conditions": sorted({cond for candidate in candidates for cond in candidate["conditions"]}),
                "verified": all(candidate["verified"] for candidate in candidates),
            })
        else:
            winner = candidates[0]
            result.update({
                "rate": winner["rate"],
                "list": winner["list"],
                "legal_basis": winner["legal_basis"],
                "matched_expression": winner["matched_expression"],
                "row_text": winner["row_text"],
                "conditions": winner["conditions"],
                "verified": winner["verified"],
            })
            if winner["coverage"] == "partial":
                result["coverage"] = "partial"
        if exclusions:
            result["exclusions"] = exclusions[:5]
        return result

    # ---- sync (best effort)
    def _own_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=20.0),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9"},
            verify=official_ssl_context(),
            follow_redirects=True,
        )

    async def sync(self, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
        """mevzuat.gov.tr konsolide metnini indirip listeleri yeniden ayrıştırır.

        Ağ yoksa ya da metin beklenenden az satır veriyorsa mevcut veri korunur ve
        ``{"ok": False, "error": ...}`` döner; hiçbir zaman hata fırlatmaz.
        """
        client = http or self._own_client()
        attempts: list[str] = []
        try:
            async with self._lock:
                for url in (IFRAME_URL, SOURCE_URL):
                    try:
                        validate_outbound_url(url, allowed_hosts=ALLOWED_HOSTS)
                        response = await client.get(url)
                        response.raise_for_status()
                        text = _html_to_text(response.text)
                        rows = parse_vat_decision_text(text)
                    except Exception as exc:  # noqa: BLE001 – bir sonraki adresi dene
                        attempts.append(f"{url}: {exc.__class__.__name__}: {str(exc)[:160]}")
                        continue
                    if len(rows) < MIN_ROWS_FOR_REPLACE:
                        attempts.append(f"{url}: yalnız {len(rows)} satır ayrıştı (en az {MIN_ROWS_FOR_REPLACE} gerekir)")
                        continue
                    payload = {
                        "source": SOURCE_LABEL,
                        "source_url": url,
                        "parser_version": PARSER_VERSION,
                        "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "note": "mevzuat.gov.tr konsolide metninden otomatik ayrıştırıldı; oranlar öneridir.",
                        "rows": rows,
                    }
                    try:
                        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = self._cache_path.with_suffix(".tmp")
                        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                        tmp.replace(self._cache_path)
                    except OSError as exc:
                        logger.warning("KDV liste önbelleği yazılamadı (%s); bellek içi veri güncellendi", exc)
                    self._install(payload, "synced")
                    self._last_error = None
                    logger.info("KDV listeleri eşitlendi: %s satır (%s)", len(rows), url)
                    return {
                        "ok": True,
                        "row_count": len(rows),
                        "sha256": payload["sha256"],
                        "retrieved_at": payload["retrieved_at"],
                        "path": str(self._cache_path),
                        "source_url": url,
                    }
                error = "; ".join(attempts) or "kaynak okunamadı"
                self._last_error = error
                logger.warning("KDV listeleri eşitlenemedi; %s verisi korunuyor: %s", self._origin, error)
                return {"ok": False, "error": error, "row_count": len(self._rows)}
        finally:
            if http is None:
                await client.aclose()

    async def periodic_sync_loop(self, *, initial_delay: float = 120.0) -> None:
        """Günde bir (varsayılan) resmî metni yeniler; 0 verilirse döngü çalışmaz."""
        if self.sync_interval <= 0:
            return
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await self.sync()
            except Exception:  # noqa: BLE001
                logger.exception("VAT list sync loop crashed; retrying next interval")
            await asyncio.sleep(max(3600, self.sync_interval))


def summary_lines(report: dict[str, Any]) -> list[str]:
    """MCP / e-posta çıktıları için kısa insan okunur özet."""
    if report.get("ambiguous"):
        parts = []
        for candidate in report.get("candidates", []):
            cond = f" ({', '.join(candidate['conditions'])})" if candidate.get("conditions") else ""
            parts.append(f"%{candidate['rate']:g}{cond} – {candidate['legal_basis']}")
        return ["KDV önerisi belirsiz; şartı doğrulayın: " + " | ".join(parts)]
    basis = "resmî liste" if report.get("basis") == "official_list" else "sezgisel fasıl kuralı"
    rate = report.get("rate")
    line = f"KDV önerisi: %{rate:g} [{basis}; {report.get('legal_basis')}]" if rate is not None else "KDV önerisi yok"
    if report.get("matched_expression"):
        line += f" · eşleşen ifade: {report['matched_expression']}"
    if report.get("conditions"):
        line += f" · şartlar: {', '.join(report['conditions'])}"
    if report.get("verified") is False:
        line += " · satır tohum verisinde doğrulanmadı"
    return [line, "Oran otomatik uygulanmaz; kullanıcı onayıyla hesaba girer."]
