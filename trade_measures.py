"""Ticaret politikası savunma araçları, gözetim ve İthalat Tebliğleri: resmî listelerden GTİP eşlemesi
ve günlük otomatik eşitleme.

Kaynaklar:
- Dampinge/sübvansiyona karşı kesin önlemler: Ticaret Bakanlığı İthalat Genel Müdürlüğü
  "Yürürlükteki Önlemler" çalışma kitabı (damping sayfasındaki .xlsx bağlantısı her güncellemede
  yeni tarihle yayımlanır; bağlantı sayfadan keşfedilir).
- Korunma önlemleri: Korunma Önlemleri Dairesi "Yürürlükte Bulunan Korunma Önlemleri" çalışma kitabı.
- Gözetim: mevzuat.gov.tr'de yürürlükteki "İthalatta Gözetim Uygulanmasına İlişkin Tebliğ" metinleri
  (resmî arama + fihrist metni; GTİP / birim gümrük kıymeti tabloları).
- İthalat Tebliğleri dizini: Ticaret Bakanlığı "İthalat Tebliğleri (yıl)" sayfası.

Eşleme kuralı: resmî satırdaki kod, sorgulanan GTİP'in ön eki ise (4, 6, 8, 10 veya 12 hane) satır
kapsam adayıdır. Damping önlemlerinde menşe ülke de eşleşmelidir; korunma önlemleri menşe ayrımı
olmadan (ülkeye özgü olanlar hariç) uygulanır; gözetim menşeden bağımsızdır. Önlem oranı resmî
tabloda yazıldığı gibi metin olarak döner; hesaba otomatik girilmez, kullanıcı doğrulaması istenir.

Depo: `data/official/*.json` tohum dosyaları ilk açılışta yüklenir; günlük eşitleme sonuçları
`trade_measures.sqlite3` içinde saklanır ve her fark değişiklik defterine yazılır.
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
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from change_ledger import batch_id_for
from security_firewall import validate_outbound_url

from countries import find_country
from trusted_certificates import GEOTRUST_TLS_RSA_CA_G1_PEM

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
SEED_DIR = ROOT / "data" / "official"
DEFAULT_SYNC_INTERVAL = int(os.environ.get("TRADE_MEASURES_SYNC_INTERVAL_SECONDS", "86400"))
USER_AGENT = "Mozilla/5.0 (compatible; MevzuatMCP/1.5; +https://gumruksor.com/)"

# İndirilen adreslerin bir kısmı bakanlık sayfasından KAZINAN HTML'den gelir
# (`discover_workbook_link`, `discover_quota_documents`), yani üçüncü tarafın
# değiştirebileceği bir girdidir. Bu yüzden her istek — ve her yönlendirme adımı —
# resmî host izin listesine karşı yeniden doğrulanır.
_OFFICIAL_HOSTS = ("ticaret.gov.tr", "mevzuat.gov.tr")
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_REDIRECTS = 6

ANTIDUMPING_PAGE = "https://ticaret.gov.tr/ithalat/ticaret-politikasi-savunma-araclari/damping-ve-subvansiyon"
SAFEGUARD_PAGE = "https://ticaret.gov.tr/ithalat/ticaret-politikasi-savunma-araclari/korunma-onlemleri/yururlukteki-onlemler"
COMMUNIQUE_PAGE = (
    "https://ticaret.gov.tr/ithalat/ithalat-mevzuati/ithalat-rejimi-karari-igv-karari-ve-ithalat-tebligleri/"
    "3-ithalat-tebligleri-{year}-yili"
)
MEVZUAT_HOME = "https://www.mevzuat.gov.tr/"
MEVZUAT_DATATABLE = "https://www.mevzuat.gov.tr/Anasayfa/MevzuatDatatable"
MEVZUAT_IFRAME = "https://www.mevzuat.gov.tr/anasayfa/MevzuatFihristDetayIframe?MevzuatTur=9&MevzuatNo={no}&MevzuatTertip=5"
MEVZUAT_PUBLIC = "https://www.mevzuat.gov.tr/mevzuat?MevzuatNo={no}&MevzuatTur=9&MevzuatTertip=5"
SURVEILLANCE_TITLE = "İthalatta Gözetim Uygulanmasına İlişkin Tebliğ"

_CODE_RE = re.compile(r"\d{4}(?:\.\d{2}){0,4}|\d{2}\.\d{2}(?:\.\d{2}){0,4}|\d{4,12}")
_ROW_CODE_RE = re.compile(r"^\s*(\d{4}(?:\.\d{2}){1,4}|\d{2}\.\d{2}(?:\.\d{2}){0,4}|\d{4,12})\s*$")
PARSER_VERSION = 4
_QUANTITY_RE = re.compile(r"^\d[\d.,]*\s*(?:ton|kg|adet|baş|bas|litre|lt|m3|m2|hl)\b", re.I)
# Kod, resmî tabloda dipnot işaretiyle bitebilir ("4820.30.00.00.00+").
_TEXT_ITEM_RE = re.compile(r"(\d{4}(?:\.\d{2}){1,4}|\d{2}\.\d{2}(?:\.\d{2}){0,4})[+*]?\s+(.+?)\s+(\d+(?:[.,]\d+)?)(?=\s+(?:\d{4}(?:\.\d{2}){1,4}|\d{2}\.\d{2}(?:\.\d{2}){0,4})[+*]?\s|\s*(?:\*|Gözetim|MADDE|$))", re.DOTALL)
_ALL_COUNTRIES = {"tüm ülkeler", "tum ulkeler", "all countries"}
KINDS = ("anti_dumping", "safeguard", "surveillance", "tariff_quota", "communiques")
AGRI_QUOTA_PAGE = "https://ticaret.gov.tr/ithalat/askiya-alma-ve-tarife-kontenjani/tarim-urunlerinde-acilan-tarife-kontenjanlari"
KIND_LABELS = {
    "anti_dumping": "Damping / sübvansiyon önlemleri",
    "safeguard": "Korunma önlemleri",
    "surveillance": "Gözetim tebliğleri",
    "tariff_quota": "Tarım ürünleri tarife kontenjanları",
    "communiques": "İthalat Tebliğleri dizini",
}


# --------------------------------------------------------------------------- helpers

def normalise_code(value: str) -> str:
    """'7306.40.20.90.00' -> '730640209000'; '55.13' -> '5513'."""
    return re.sub(r"\D", "", value or "")


def extract_codes(raw: str) -> list[str]:
    """Resmî hücrelerdeki serbest metinden GTİP/GTP kodlarını (rakam dizisi) çıkarır."""
    codes: list[str] = []
    for token in _CODE_RE.findall(raw or ""):
        digits = normalise_code(token)
        if len(digits) < 4 or len(digits) > 12 or len(digits) % 2:
            continue
        if digits not in codes:
            codes.append(digits)
    return codes


def code_matches(candidate: str, gtip: str) -> bool:
    """Resmî satır kodu sorgulanan kodun ön eki ise (ya da tersi, kısa sorgularda) kapsamdadır."""
    if not candidate or not gtip:
        return False
    return gtip.startswith(candidate) or candidate.startswith(gtip)


def _ascii_key(text: str) -> str:
    """Başlık eşleştirme için: küçük harf, Türkçe harfler sadeleştirilmiş, yalnız harfler."""
    lowered = (text or "").replace("İ", "i").replace("I", "ı").lower().replace("\u0307", "")
    table = str.maketrans("çğıöşü", "cgiosu")
    return re.sub(r"[^a-z]", "", lowered.translate(table))


def _is_code_header(text: str) -> bool:
    key = _ascii_key(text)
    return key.startswith("gtip") or key.startswith("gtp") or key in {"gtipno", "gtipkodu", "gtpkodu"}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return re.sub(r"[ \t\xa0]+", " ", str(value)).strip()


_GROUP_KEYS = {"avrupa birli": "__eu__", "efta": "__efta__", "birleşik krall": "birlesik_krallik", "birlesik krall": "birlesik_krallik"}


def _country_key(value: str) -> str | None:
    if not value:
        return None
    name = value.split("/")[0].strip()
    lowered = name.lower()
    for needle, key in _GROUP_KEYS.items():
        if needle in lowered:
            if key.startswith("__"):
                return key
            country = find_country("Birleşik Krallık")
            return country.key if country else key
    country = find_country(name)
    if country:
        return country.key
    words = name.split()
    for length in range(len(words) - 1, 0, -1):
        country = find_country(" ".join(words[:length]))
        if country:
            return country.key
    return None


def _country_matches(origin: str | None, measure_country: str) -> bool | None:
    """True/False eşleşme; menşe verilmediyse None (bilinmiyor)."""
    if not origin:
        return None
    lowered = measure_country.strip().lower()
    if lowered in _ALL_COUNTRIES or lowered.startswith("tüm ülkeler") or lowered.startswith("tum ulkeler"):
        return True
    origin_key = _country_key(origin)
    measure_key = _country_key(measure_country)
    if measure_key in {"__eu__", "__efta__"}:
        origin_entry = find_country(origin)
        if origin_entry is None:
            return None
        return origin_entry.regime == ("eu" if measure_key == "__eu__" else "efta")
    if origin_key and measure_key:
        return origin_key == measure_key
    return origin.strip().lower() == measure_country.split("/")[0].strip().lower()


def _parse_unit_value(text: str) -> float | None:
    cleaned = (text or "").strip()
    if not cleaned or cleaned in {"-", "—"}:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", cleaned)
    if not match:
        return None
    number = match.group().replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def _status(expires: str | None, today: date) -> str:
    if not expires:
        return "unknown"
    try:
        return "in_force" if date.fromisoformat(expires[:10]) >= today else "expired"
    except ValueError:
        return "unknown"


def official_ssl_context() -> Any:
    """Resmî sunucular ara sertifikayı göndermediği için yalnız o ara CA eklenir."""
    context = httpx.create_ssl_context()
    context.load_verify_locations(cadata=GEOTRUST_TLS_RSA_CA_G1_PEM)
    return context


# --------------------------------------------------------------------------- parsers (pure)

def parse_antidumping_workbook(payload: bytes) -> dict[str, list[dict[str, str]]]:
    """Bakanlığın 'Yürürlükteki Önlemler' kitabından kesin ve geçici önlem satırları."""
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    out: dict[str, list[dict[str, str]]] = {"definitive": [], "provisional": []}
    for sheet in workbook.worksheets:
        title = sheet.title.lower()
        if "kesin" in title or "definitive" in title:
            key = "definitive"
        elif "geçici" in title or "gecici" in title or "prov" in title:
            key = "provisional"
        else:
            continue
        header_seen = False
        for row in sheet.iter_rows(values_only=True):
            values = [_clean(cell) for cell in row]
            if not header_seen:
                header_seen = bool(values) and values[0] == "DOSYA NO"
                continue
            if not values or not values[0] or values[0] == "CASE NUMBER":
                continue
            values += [""] * (40 - len(values))
            if key == "definitive":
                out[key].append(
                    {
                        "file_no": values[0], "sector": values[1], "product": values[2], "product_en": values[3],
                        "gtip": values[4], "country": values[5], "country_en": values[6], "communique": values[7],
                        "rg_date": values[8], "rg_no": values[9], "rate": values[11], "kind": values[12],
                        "expires": values[13], "orig_initiation": values[14], "orig_provisional": values[15],
                        "orig_definitive": values[16], "expiry_review_open": values[20], "expiry_review_close": values[21],
                        "expiry_review_result": values[22], "circumvention_open": values[26],
                        "circumvention_definitive": values[28], "notes": " | ".join(v for v in values[33:37] if v),
                    }
                )
            else:
                out[key].append(
                    {
                        "file_no": values[0], "sector": values[1], "product": values[2], "product_en": values[3],
                        "gtip": values[4], "country": values[5], "country_en": values[6], "communique": values[7],
                        "rg_date": values[8], "rg_no": values[9], "rate": values[10], "kind": values[11],
                        "initiation": values[12],
                    }
                )
    if not out["definitive"]:
        raise ValueError("Damping çalışma kitabında kesin önlem satırı bulunamadı; biçim değişmiş olabilir.")
    return out


def parse_safeguard_workbook(payload: bytes) -> list[dict[str, Any]]:
    """Korunma Önlemleri Dairesi listesi: her önlem, alt satırlarında işlem adımları ve tutarları."""
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    measures: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in sheet.iter_rows(values_only=True):
        values = [_clean(cell) for cell in row]
        values += [""] * (18 - len(values))
        if values[1].isdigit() and values[2]:
            current = {
                "seq": int(values[1]), "file_no": values[2], "product": values[3], "gtip": values[4],
                "country": values[5], "stage": values[12], "original_start": values[13], "expires": values[14],
                "amounts": [], "acts": [],
            }
            measures.append(current)
        if current is None:
            continue
        if values[6]:
            current["acts"].append(
                {"kind": values[6], "number": values[7], "rg_no": values[8], "rg_date": values[9], "published": values[10], "wto": values[11]}
            )
        if values[15] and values[15] != "-":
            current["amounts"].append(values[15])
    if not measures:
        raise ValueError("Korunma önlemi listesinde satır bulunamadı; biçim değişmiş olabilir.")
    return measures


def _header_unit(text: str) -> str | None:
    match = re.search(r"\(([^)]*)\)", text)
    if match:
        return match.group(1).replace("*", "").strip()
    match = re.search(r"(ABD Doları\s*/\s*\w+|USD\s*/\s*\w+|\$\s*/\s*\w+)", text, flags=re.I)
    return match.group(1).replace("*", "").strip() if match else None


# Düz metin tablosunun başlığı: "G.T.İ.P." yazımı boşluklu olabilir ("G.T.İ .P."),
# kıymet sütunu "Birim Gümrük Kıymeti" ya da "CIF Kıymet" olarak adlandırılır.
_TEXT_TABLE_HEADER_RE = (
    r"G\s*\.?\s*T\s*\.?\s*İ\s*\.?\s*P\s*\.?|GTİP|GTP"
    r")\s+Eşya\S*\s+Tanımı\s+((?:Birim|CIF|Gümrük)[^\d]{0,80}?)(?=\s+\d{2,4}\."
)
_TEXT_TABLE_HEADER_RE = r"(?:" + _TEXT_TABLE_HEADER_RE + r")"

# Eski tebliğlerin bir kısmında tablo yoktur; kapsam doğrudan MADDE 2 cümlesinde yazılıdır:
# "Gözetim uygulaması 6802.23, 6802.93 ve 6802.99 gümrük tarife pozisyonlarında yer alan
#  eşyanın CIF kıymeti 500 ABD Doları/ton (brüt ağırlık)'un altında olanlarının ithalatında..."
_PROSE_SCOPE_RE = re.compile(
    r"Gözetim\s+uygulaması\s+(?P<codes>[\d.,\s]*\d(?:\s*(?:,|ve)\s*[\d.]+)*)\s*"
    r"gümrük\s+tarife\s+(?:istatistik\s+)?pozisyon",
    re.IGNORECASE,
)
_PROSE_VALUE_RE = re.compile(
    r"CIF\s+kıymeti\s+(?P<value>[\d.,]+)\s*(?P<unit>ABD\s*Doları\s*/\s*[A-Za-zÇĞİÖŞÜçğıöşü]+)",
    re.IGNORECASE,
)
_PROSE_NAME_RE = re.compile(r"[“\"]([^”\"]{3,160})[”\"]")


def _prose_surveillance_items(text: str, default_unit: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Tablosu olmayan tebliğlerde kapsamı MADDE 2 cümlesinden çıkarır."""
    match = _PROSE_SCOPE_RE.search(text)
    if not match:
        return [], default_unit
    codes = extract_codes(match.group("codes"))
    if not codes:
        return [], default_unit
    sentence = text[match.start(): match.start() + 600]
    value_match = _PROSE_VALUE_RE.search(sentence)
    value = value_match.group("value") if value_match else ""
    unit = default_unit
    if value_match:
        unit = re.sub(r"\s+", "", value_match.group("unit")).replace("ABDDoları", "ABD Doları")
    name_match = _PROSE_NAME_RE.search(sentence)
    description = name_match.group(1).strip() if name_match else ""
    raw_codes = re.findall(r"\d{4}(?:\.\d{2}){1,4}|\d{2}\.\d{2}(?:\.\d{2}){0,4}", match.group("codes"))
    items = [
        {"gtip": raw, "description": description, "value": value, "unit": unit}
        for raw in raw_codes
    ]
    return items, unit


def parse_surveillance_page(html_text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gözetim tebliği metnindeki GTİP / eşya tanımı / birim gümrük kıymeti tablolarını çıkarır."""
    soup = BeautifulSoup(html_text, "html.parser")
    items: list[dict[str, Any]] = []
    default_unit: str | None = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        gtip_col = 0
        desc_col: int | None = None
        value_col: int | None = None
        country_col: int | None = None
        unit: str | None = None
        header_found = False
        for tr in rows:
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            if not cells:
                continue
            joined = " ".join(cells).lower()
            if not header_found and any(_is_code_header(cell) for cell in cells):
                header_found = True
                for index, cell in enumerate(cells):
                    low = cell.lower()
                    if _is_code_header(cell):
                        gtip_col = index
                    elif "tanım" in low or "eşya" in low or "esya" in low or "madde" in low:
                        desc_col = index
                    elif "kıymet" in low or "kiymet" in low or "abd dolar" in low or "$" in low or "usd" in low or "fiyat" in low:
                        value_col = index
                        unit = _header_unit(cell) or unit
                    elif "menşe" in low or "mense" in low or "ülke" in low:
                        country_col = index
                if unit:
                    default_unit = unit
                continue
            code_cell = cells[gtip_col] if gtip_col < len(cells) else ""
            if not _ROW_CODE_RE.match(code_cell):
                # Bazı tablolarda kod ile açıklama aynı hücrede: "8481.10.05.00.00 Filtre..."
                match = re.match(r"^\s*(\d{4}(?:\.\d{2}){1,4}|\d{2}\.\d{2}(?:\.\d{2}){0,4})\s+(.+)$", code_cell)
                if not match:
                    continue
                code_text, inline_desc = match.group(1), match.group(2)
            else:
                code_text, inline_desc = code_cell.strip(), ""
            description = inline_desc
            if desc_col is not None and desc_col < len(cells):
                description = cells[desc_col] or description
            elif len(cells) > gtip_col + 1 and value_col is None:
                description = cells[gtip_col + 1]
            value = ""
            if value_col is not None and value_col < len(cells):
                value = cells[value_col]
            else:
                numeric = [c for c in cells[gtip_col + 1 :] if re.fullmatch(r"[\d.,]+", c.replace(" ", ""))]
                value = numeric[-1] if numeric else ""
            item: dict[str, Any] = {
                "gtip": code_text,
                "description": description,
                "value": value,
                "unit": unit or default_unit,
            }
            if country_col is not None and country_col < len(cells):
                item["country"] = cells[country_col]
            items.append(item)
    text = soup.get_text(" ", strip=True)
    if not items:
        # Bazı eski tebliğlerde tablo <table> yerine düz metin/paragraf olarak yer alır.
        header = re.search(_TEXT_TABLE_HEADER_RE, text)
        if header:
            unit_text = header.group(1)
            default_unit = _header_unit(unit_text) or default_unit
            tail = text[header.end():]
            stop = re.search(r"\s(?:Gözetim uygulaması|MADDE 2|Yürürlük)\b", tail)
            segment = tail[: stop.start()] if stop else tail[:20000]
            for code, description, value in _TEXT_ITEM_RE.findall(segment):
                items.append({
                    "gtip": code,
                    "description": re.sub(r"\s+", " ", description).strip(" -–"),
                    "value": value,
                    "unit": default_unit,
                })
    if not items:
        items, default_unit = _prose_surveillance_items(text, default_unit)
    if default_unit is None:
        found = re.search(r"\(ABD Doları\s*/\s*([A-Za-zÇĞİÖŞÜçğıöşü0-9]+)\*?\)", text)
        if found:
            default_unit = f"ABD Doları/{found.group(1)}"
            for item in items:
                item["unit"] = item["unit"] or default_unit
    title_match = re.search(r"(İTHALATTA GÖZETİM UYGULANMASINA İLİŞKİN TEBLİĞ\s*\(TEBLİĞ NO:\s*[^)]+\))", text, flags=re.I)
    result = dict(meta or {})
    result.update(
        {
            "title": result.get("title") or (title_match.group(1).strip() if title_match else SURVEILLANCE_TITLE),
            "unit": default_unit,
            "items": items,
            "item_count": len(items),
            "parser_version": PARSER_VERSION,
        }
    )
    return result


def discover_workbook_link(html_text: str, base_url: str, *, must_contain: Iterable[str]) -> str | None:
    """Sayfadaki .xls/.xlsx bağlantıları arasından metni/adresi ipuçlarını içeren ilkini döndürür."""
    soup = BeautifulSoup(html_text, "html.parser")
    hints = [hint.lower() for hint in must_contain]
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        text = anchor.get_text(" ", strip=True).lower()
        if not re.search(r"\.xlsx?(\?|$)", href, flags=re.I):
            continue
        haystack = f"{text} {href.lower()}"
        if all(hint in haystack for hint in hints):
            return urljoin(base_url, href.replace(" ", "%20"))
    return None


def parse_communique_index(html_text: str, base_url: str, year: int) -> list[dict[str, str]]:
    """Bakanlığın 'İthalat Tebliğleri (yıl)' sayfasındaki tebliğ bağlantıları."""
    soup = BeautifulSoup(html_text, "html.parser")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip("–—- ")
        href = urljoin(base_url, anchor["href"])
        if not title or href in seen:
            continue
        if "tebliğ" not in title.lower() and "teblig" not in title.lower():
            continue
        if not re.search(r"resmigazete|mevzuat\.gov|ticaret\.gov\.tr/data|\.pdf", href, flags=re.I):
            continue
        number = re.search(r"\(İthalat:\s*(\d{4}/\d+)\)", title, flags=re.I)
        seen.add(href)
        entries.append(
            {
                "title": title,
                "url": href,
                "number": number.group(1) if number else "",
                "year": str(year),
                "gazette": re.search(r"/eskiler/(\d{4})/(\d{2})/(\d{8})", href).group(3) if re.search(r"/eskiler/(\d{4})/(\d{2})/(\d{8})", href) else "",
            }
        )
    return entries


def _docx_table_rows(payload: bytes) -> list[list[str]]:
    """OOXML belgesindeki tablo satırlarını hücre metinleriyle döndürür (ek bağımlılık gerekmez)."""
    import zipfile
    from xml.etree import ElementTree

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    rows: list[list[str]] = []
    for tr in root.iter(f"{{{ns['w']}}}tr"):
        cells = []
        for tc in tr.findall("w:tc", ns):
            text = " ".join(t.text or "" for t in tc.iter(f"{{{ns['w']}}}t"))
            cells.append(re.sub(r"\s+", " ", text).strip())
        rows.append(cells)
    return rows


_DOC_MAGIC = b"\xd0\xcf\x11\xe0"
_FIB_FLAGS_OFFSET = 0x000A
_FIB_CLX_OFFSET = 0x01A2
_PIECE_DESCRIPTOR_SIZE = 8
_PIECE_COMPRESSED_FLAG = 0x40000000


def _decode_doc_pieces(document: bytes, table: bytes) -> str:
    """Word 97-2003 parça tablosunu çözerek belge metnini döndürür.

    Metin ``WordDocument`` akışında parçalar hâlinde durur; her parçanın yeri ve
    kodlaması ``0Table``/``1Table`` akışındaki CLX parça tablosunda yazılıdır.
    Sıkıştırılmış parçalar tek baytlık (Türkçe belgelerde cp1254), diğerleri
    UTF-16LE kodludur. Tablo hücrelerini ayıran ``\x07`` işaretleri korunur.
    """
    if len(document) < _FIB_CLX_OFFSET + 8 or not table:
        return ""
    clx_offset, clx_length = struct.unpack_from("<II", document, _FIB_CLX_OFFSET)
    clx = table[clx_offset : clx_offset + clx_length]
    cursor = 0
    while cursor + 3 <= len(clx) and clx[cursor] == 1:  # araya giren Prc blokları atlanır
        cursor += 3 + struct.unpack_from("<H", clx, cursor + 1)[0]
    if cursor + 5 > len(clx) or clx[cursor] != 2:
        return ""
    piece_table = clx[cursor + 5 : cursor + 5 + struct.unpack_from("<I", clx, cursor + 1)[0]]
    piece_count = (len(piece_table) - 4) // (4 + _PIECE_DESCRIPTOR_SIZE)
    if piece_count <= 0:
        return ""
    positions = [struct.unpack_from("<I", piece_table, 4 * index)[0] for index in range(piece_count + 1)]
    chunks: list[str] = []
    for index in range(piece_count):
        descriptor = 4 * (piece_count + 1) + _PIECE_DESCRIPTOR_SIZE * index
        location = struct.unpack_from("<I", piece_table, descriptor + 2)[0]
        length = positions[index + 1] - positions[index]
        if length <= 0:
            continue
        if location & _PIECE_COMPRESSED_FLAG:
            start = (location & 0x3FFFFFFF) // 2
            chunks.append(document[start : start + length].decode("cp1254", errors="replace"))
        else:
            start = location & 0x3FFFFFFF
            chunks.append(document[start : start + length * 2].decode("utf-16-le", errors="replace"))
    return "".join(chunks)


def _doc_stream_text(payload: bytes) -> str:
    """.doc (OLE bileşik) belgesinin akışlarını açıp metnini döndürür."""
    import olefile

    if not payload.startswith(_DOC_MAGIC):
        return ""
    with io.BytesIO(payload) as buffer:
        if not olefile.isOleFile(buffer):
            return ""
        ole = olefile.OleFileIO(buffer)
        try:
            if not ole.exists("WordDocument"):
                return ""
            document = ole.openstream("WordDocument").read()
            if len(document) < _FIB_FLAGS_OFFSET + 2:
                return ""
            flags = struct.unpack_from("<H", document, _FIB_FLAGS_OFFSET)[0]
            preferred = "1Table" if flags & 0x0200 else "0Table"
            table_name = preferred if ole.exists(preferred) else ("1Table" if ole.exists("1Table") else "0Table")
            if not ole.exists(table_name):
                return ""
            table = ole.openstream(table_name).read()
        finally:
            ole.close()
    return _decode_doc_pieces(document, table)


def _doc_rows_from_text(text: str) -> list[list[str]]:
    """.doc metnini tablo satırlarına böler: hücreler ``\x07``, satır sonu boş hücredir."""
    rows: list[list[str]] = []
    current: list[str] = []
    for part in text.split("\x07"):
        cell = re.sub(r"[\r\x0b\x00-\x06\x08-\x1f]", " ", part)
        cell = re.sub(r"\s+", " ", cell).strip()
        if cell:
            current.append(cell)
        elif current:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return rows


def _doc_table_rows(payload: bytes) -> list[list[str]]:
    return _doc_rows_from_text(_doc_stream_text(payload))


def _legacy_doc_text(payload: bytes) -> str:
    """.doc için son çare: Docker imajındaki antiword; yoksa boş döner."""
    import shutil
    import subprocess
    import tempfile

    executable = shutil.which("antiword")
    if not executable:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as handle:
        handle.write(payload)
        path = handle.name
    try:
        completed = subprocess.run([executable, "-m", "UTF-8.txt", "-w", "0", path], capture_output=True, check=False, timeout=30)  # nosec B603
        return completed.stdout.decode("utf-8", errors="replace") if completed.returncode == 0 else ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


_QUOTA_HEADER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quota_code", ("kodno", "kontenjankodu", "kotakodu")),
    ("period", ("donemi", "donem")),
    ("quantity", ("miktar",)),
    ("duty_rate", ("vergi", "oran")),
    ("description", ("maddeismi", "esyanintanimi", "esyatanimi", "urunadi", "tanimi", "tanim")),
)
_HEADER_TAIL_CHARS = 40


def _quota_columns(cells: list[str]) -> tuple[int, dict[str, int]]:
    """Başlık satırından GTİP sütununu ve alan sütunlarını çıkarır.

    Resmî eklerde başlık hücresine tebliğ başlığı ya da Resmî Gazete künyesi
    karışabildiğinden yalnız hücrenin sonundaki caption kısmı eşleştirilir; aksi
    hâlde künyedeki "Tarihi" gibi kelimeler yanlış sütunu kapar.
    """
    gtip_col = next(index for index, cell in enumerate(cells) if _is_code_header(cell))
    columns: dict[str, int] = {}
    for index, cell in enumerate(cells):
        if index == gtip_col:
            continue
        label = _ascii_key(cell)[-_HEADER_TAIL_CHARS:]
        for field_name, needles in _QUOTA_HEADER_RULES:
            if field_name in columns:
                continue
            if any(needle in label for needle in needles):
                columns[field_name] = index
                break
    return gtip_col, columns


def _quota_rows_from_cells(rows: Iterable[list[str]]) -> list[dict[str, str]]:
    """Karar/tebliğ eklerindeki tablo satırlarını başlıktaki sütun adlarına göre okur."""
    items: list[dict[str, str]] = []
    header: list[str] = []
    gtip_col = 0
    columns: dict[str, int] = {}
    for raw_cells in rows:
        cells = [str(cell).strip() for cell in raw_cells]
        if not cells:
            continue
        if any(_is_code_header(cell) for cell in cells):
            header = cells
            gtip_col, columns = _quota_columns(cells)
            continue
        code_index = gtip_col if header else 0
        offset = 0
        if code_index + 1 < len(cells) and re.fullmatch(r"\d{1,3}", cells[code_index]) and _ROW_CODE_RE.match(cells[code_index + 1]):
            code_index += 1  # başlıkta yer almayan "Sıra No" sütunu satırı sağa kaydırır
            offset = 1
        if code_index >= len(cells) or not _ROW_CODE_RE.match(cells[code_index]):
            continue
        item: dict[str, str] = {"gtip": cells[code_index], "description": ""}
        used = {code_index}
        if header:
            def value(index: int, shift: int = 0) -> str:
                position = index + offset - shift
                return cells[position] if 0 <= position < len(cells) else ""

            description_column = columns.get("description")
            quantity_column = columns.get("quantity")
            # Birleştirilmiş/boş bırakılmış açıklama hücresinde satır sola kayar: miktar açıklama sütununa düşer.
            shifted = int(
                description_column is not None
                and quantity_column is not None
                and bool(_QUANTITY_RE.match(value(description_column)))
                and not _QUANTITY_RE.match(value(quantity_column))
            )
            for field_name, index in columns.items():
                if field_name == "description" and shifted:
                    continue
                shift = shifted if description_column is not None and index > description_column else 0
                position = index + offset - shift
                if 0 <= position < len(cells) and cells[position]:
                    item[field_name] = cells[position]
                    used.add(position)
            if shifted and description_column is not None:
                item["quantity"] = value(description_column)
                used.add(description_column + offset)
            if not item["description"] and description_column is None and code_index + 1 < len(cells) and code_index + 1 not in used:
                item["description"] = cells[code_index + 1]
                used.add(code_index + 1)
        else:
            if code_index + 1 < len(cells):
                item["description"] = cells[code_index + 1]
                used.add(code_index + 1)
        if _QUANTITY_RE.match(item["description"]):
            item.setdefault("quantity", item["description"])
            item["description"] = ""
        if "quantity" not in item:
            candidate = next((cell for index, cell in enumerate(cells) if index not in used and _QUANTITY_RE.match(cell)), "")
            if candidate:
                item["quantity"] = candidate
        items.append(item)
    return items


def parse_quota_document(payload: bytes, filename: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tarım ürünleri tarife kontenjanı kararı (.docx/.doc) ekindeki GTİP / miktar / vergi tablosu."""
    lowered = filename.lower()
    if lowered.endswith(".docx"):
        rows = _docx_table_rows(payload)
    else:
        rows = _doc_table_rows(payload)
        if not rows:  # OLE okunamazsa antiword metnine düş
            text = _legacy_doc_text(payload)
            rows = [[cell.strip() for cell in line.split("|") if cell.strip()] for line in text.splitlines() if "|" in line]
            if not rows:
                rows = [re.split(r"\s{2,}", line.strip()) for line in text.splitlines() if _ROW_CODE_RE.match(line.strip().split(" ")[0] if line.strip() else "")]
    items = _quota_rows_from_cells(rows)
    result = dict(meta or {})
    title = result.get("title", "")
    origin = re.search(r"^(.+?)\s+(?:Menşeli|Çıkışlı)", title)
    result.update(
        {
            "origin": origin.group(1).strip() if origin else "",
            "items": items,
            "item_count": len(items),
            "parser_version": PARSER_VERSION,
        }
    )
    return result


def discover_quota_documents(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Tarım ürünleri sayfasındaki karar/tebliğ (.doc/.docx) bağlantıları, başlıkla birlikte."""
    soup = BeautifulSoup(html_text, "html.parser")
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        if not re.search(r"\.docx?(\?|$)", href, flags=re.I) or "kontenjan" not in title.lower():
            continue
        url = urljoin(base_url, quote(href, safe=":/%()"))
        if url in seen:
            continue
        seen.add(url)
        found.append({"title": title, "url": url, "filename": href.rsplit("/", 1)[-1]})
    return found


# --------------------------------------------------------------------------- store

@dataclass(frozen=True)
class MeasureHit:
    measure_type: str  # anti_dumping | countervailing | safeguard | surveillance
    matched_code: str
    country: str
    origin_match: bool | None
    rate_text: str
    unit_value_usd: float | None
    unit: str | None
    product: str
    legal_act: str
    gazette: str
    expires: str | None
    status: str  # in_force | expired | unknown
    notes: str = ""
    source: str = ""
    # Row-level lineage from the store (source URL, checksum, first/last seen, RG date).
    provenance: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "measure_type": self.measure_type, "matched_code": self.matched_code, "country": self.country,
            "origin_match": self.origin_match, "rate_text": self.rate_text, "unit_value_usd": self.unit_value_usd,
            "unit": self.unit, "product": self.product, "legal_act": self.legal_act, "gazette": self.gazette,
            "expires": self.expires, "status": self.status, "notes": self.notes, "source": self.source,
            "provenance": self.provenance,
        }


@dataclass
class TradeMeasureReport:
    gtip: str
    origin_country: str | None
    as_of: str
    anti_dumping: list[MeasureHit] = field(default_factory=list)
    safeguard: list[MeasureHit] = field(default_factory=list)
    surveillance: list[MeasureHit] = field(default_factory=list)
    tariff_quota: list[MeasureHit] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def surveillance_unit_value(self) -> float | None:
        values = [hit.unit_value_usd for hit in self.surveillance if hit.unit_value_usd and hit.status == "in_force"]
        return max(values) if values else None

    @property
    def applicable_anti_dumping(self) -> list[MeasureHit]:
        return [hit for hit in self.anti_dumping if hit.origin_match is not False and hit.status != "expired"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "gtip": self.gtip, "origin_country": self.origin_country, "as_of": self.as_of,
            "anti_dumping": [hit.as_dict() for hit in self.anti_dumping],
            "safeguard": [hit.as_dict() for hit in self.safeguard],
            "surveillance": [hit.as_dict() for hit in self.surveillance],
            "tariff_quota": [hit.as_dict() for hit in self.tariff_quota],
            "surveillance_unit_value": self.surveillance_unit_value,
            "sources": self.sources, "warnings": self.warnings,
        }


class TradeMeasureStore:
    """Tohum JSON + sqlite anlık görüntüleri; her kayıt farkı değişiklik defterine yazılır."""

    def __init__(self, data_dir: str | Path | None = None, seed_dir: str | Path | None = None) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "trade_measures.sqlite3"
        self.seed_dir = Path(seed_dir or SEED_DIR)
        if not (self.seed_dir / "antidumping_measures.json").exists():
            for candidate in (Path(sys.prefix) / "data" / "official", Path.cwd() / "data" / "official"):
                if (candidate / "antidumping_measures.json").exists():
                    self.seed_dir = candidate
                    break
        self._cache: dict[str, Any] = {}
        # Optional unified change ledger (change_ledger.ChangeLedger); set by the server.
        self.ledger: Any = None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    kind TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, source_url TEXT NOT NULL,
                    source_label TEXT NOT NULL, item_count INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measure_rows (
                    kind TEXT NOT NULL, row_key TEXT NOT NULL, gtip TEXT, gtip_codes_json TEXT NOT NULL DEFAULT '[]',
                    country TEXT, product TEXT, valid_from TEXT, valid_to TEXT, payload_json TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '', source_sha256 TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, removed_at TEXT,
                    PRIMARY KEY (kind, row_key)
                );
                CREATE INDEX IF NOT EXISTS idx_measure_rows_gtip ON measure_rows(kind, gtip);
                CREATE TABLE IF NOT EXISTS surveillance_docs (
                    mevzuat_no TEXT PRIMARY KEY, title TEXT NOT NULL, rg_date TEXT, rg_no TEXT, url TEXT,
                    fetched_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, changed_at TEXT NOT NULL,
                    added INTEGER NOT NULL, removed INTEGER NOT NULL, modified INTEGER NOT NULL, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_changes_kind ON changes(kind, changed_at);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(datasets)").fetchall()}
            if "sha256" not in columns:
                connection.execute("ALTER TABLE datasets ADD COLUMN sha256 TEXT NOT NULL DEFAULT ''")

    # ---- seeds
    def _seed(self, kind: str) -> Any:
        names = {
            "anti_dumping": "antidumping_measures.json",
            "safeguard": "safeguard_measures.json",
            "surveillance": "surveillance_measures.json",
            "tariff_quota": "agricultural_quotas.json",
            "communiques": "import_communiques.json",
        }
        path = self.seed_dir / names[kind]
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def load(self, kind: str) -> Any:
        if kind in self._cache:
            return self._cache[kind]
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM datasets WHERE kind=?", (kind,)).fetchone()
        payload = json.loads(row["payload"]) if row else self._seed(kind)
        self._cache[kind] = payload
        return payload

    def metadata(self, kind: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fetched_at, source_url, source_label, item_count, sha256 FROM datasets WHERE kind=?", (kind,)
            ).fetchone()
        if row:
            return {"origin": "synced", **dict(row)}
        seed = self._seed(kind)
        if seed is None:
            return {"origin": "missing", "fetched_at": None, "source_url": None, "source_label": None, "item_count": 0}
        return {
            "origin": "seed",
            "fetched_at": None,
            "source_url": None,
            "source_label": "depo tohum dosyası (data/official)",
            "item_count": _count_items(kind, seed),
        }

    def save(self, kind: str, payload: Any, *, source_url: str, source_label: str, sha256: str = "") -> dict[str, Any]:
        previous = self.load(kind)
        diff = diff_payloads(kind, previous, payload)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        digest = sha256 or payload_sha256(payload)
        previous_meta = self.metadata(kind)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO datasets(kind,fetched_at,source_url,source_label,item_count,payload,sha256) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(kind) DO UPDATE SET fetched_at=excluded.fetched_at, source_url=excluded.source_url, "
                "source_label=excluded.source_label, item_count=excluded.item_count, payload=excluded.payload, sha256=excluded.sha256",
                (kind, now, source_url, source_label, _count_items(kind, payload), json.dumps(payload, ensure_ascii=False), digest),
            )
            if previous is not None and diff["total"]:
                connection.execute(
                    "INSERT INTO changes(kind,changed_at,added,removed,modified,detail) VALUES(?,?,?,?,?,?)",
                    (kind, now, diff["added_count"], diff["removed_count"], diff["modified_count"], json.dumps(diff, ensure_ascii=False)),
                )
            self._upsert_measure_rows(connection, kind, payload, source_url=source_url, sha256=digest, seen_at=now)
        self._cache[kind] = payload
        self._record_ledger_batch(
            kind, previous, payload, diff, source_url=source_url, sha256=digest, detected_at=now,
            old_snapshot_id=previous_meta.get("sha256") or (previous_meta.get("origin") if previous is not None else None),
        )
        return diff

    # ---- row-level lineage
    def _upsert_measure_rows(
        self, connection: sqlite3.Connection, kind: str, payload: Any, *, source_url: str, sha256: str, seen_at: str
    ) -> None:
        rows = _item_rows(kind, payload)
        for key, entry in rows.items():
            connection.execute(
                "INSERT INTO measure_rows(kind,row_key,gtip,gtip_codes_json,country,product,valid_from,valid_to,payload_json,"
                "source_url,source_sha256,first_seen_at,last_seen_at,removed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(kind,row_key) DO UPDATE SET gtip=excluded.gtip, gtip_codes_json=excluded.gtip_codes_json, "
                "country=excluded.country, product=excluded.product, valid_from=excluded.valid_from, valid_to=excluded.valid_to, "
                "payload_json=excluded.payload_json, source_url=excluded.source_url, source_sha256=excluded.source_sha256, "
                "last_seen_at=excluded.last_seen_at, removed_at=NULL",
                (
                    kind, key, (entry["codes"] or [None])[0], json.dumps(entry["codes"]), entry.get("country"), entry.get("product"),
                    entry.get("valid_from"), entry.get("valid_to"), json.dumps(entry["row"], ensure_ascii=False, default=str),
                    source_url, sha256, seen_at, seen_at,
                ),
            )
        if rows:
            placeholders = ",".join("?" for _ in rows)
            connection.execute(
                f"UPDATE measure_rows SET removed_at=? WHERE kind=? AND removed_at IS NULL AND row_key NOT IN ({placeholders})",
                (seen_at, kind, *rows.keys()),
            )
        else:
            connection.execute("UPDATE measure_rows SET removed_at=? WHERE kind=? AND removed_at IS NULL", (seen_at, kind))

    def ensure_measure_rows(self) -> int:
        """Populate row lineage from the currently loaded datasets when the table is still empty."""
        written = 0
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            for kind in KINDS:
                if connection.execute("SELECT 1 FROM measure_rows WHERE kind=? LIMIT 1", (kind,)).fetchone():
                    continue
                payload = self.load(kind)
                if payload is None:
                    continue
                meta = self.metadata(kind)
                self._upsert_measure_rows(
                    connection, kind, payload,
                    source_url=meta.get("source_url") or "",
                    sha256=meta.get("sha256") or payload_sha256(payload),
                    seen_at=meta.get("fetched_at") or now,
                )
                written += len(_item_rows(kind, payload))
        return written

    def provenance(self, kind: str, row_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_url, source_sha256, first_seen_at, last_seen_at, removed_at, valid_from, valid_to "
                "FROM measure_rows WHERE kind=? AND row_key=?",
                (kind, row_key),
            ).fetchone()
        if row is None:
            return None
        return {"row_key": row_key, **dict(row)}

    def _record_ledger_batch(
        self, kind: str, previous: Any, payload: Any, diff: dict[str, Any], *, source_url: str, sha256: str,
        detected_at: str, old_snapshot_id: str | None, backfilled: bool = False,
    ) -> str | None:
        if self.ledger is None:
            return None
        try:
            before, after = _item_rows(kind, previous), _item_rows(kind, payload)
            changes: list[dict[str, Any]] = []
            for key in sorted(set(after) - set(before)):
                changes.append({"entity_key": key, "gtip": (after[key]["codes"] or [None])[0], "change_type": "added", "before": None, "after": _ledger_row(after[key])})
            for key in sorted(set(before) - set(after)):
                changes.append({"entity_key": key, "gtip": (before[key]["codes"] or [None])[0], "change_type": "removed", "before": _ledger_row(before[key]), "after": None})
            for key in sorted(set(before) & set(after)):
                if before[key]["summary"] != after[key]["summary"]:
                    changes.append({"entity_key": key, "gtip": (after[key]["codes"] or [None])[0], "change_type": "modified", "before": _ledger_row(before[key]), "after": _ledger_row(after[key])})
            if previous is None:
                changes = []
            return self.ledger.record_batch(
                kind="trade_measures", source_id=kind, title=KIND_LABELS.get(kind, kind), new_snapshot_id=sha256[:24],
                old_snapshot_id=old_snapshot_id, source_url=source_url, sha256=sha256, changes=changes,
                total_rows=len(after), detected_at=detected_at, backfilled=backfilled,
            )
        except Exception as exc:  # noqa: BLE001 – the ledger must never break a sync
            logger.warning("Trade measure change ledger write failed: %s", exc)
            return None

    def backfill_ledger(self) -> int:
        """Import the legacy `changes` rows into the unified ledger (summaries only)."""
        if self.ledger is None:
            return 0
        written = 0
        for change in reversed(self.changes(limit=1000)):
            detail = change.get("detail") or {}
            kind = change.get("kind")
            snapshot_id = f"legacy-{change['id']}"
            if self.ledger.has_batch(batch_id_for("trade_measures", kind, snapshot_id)):
                continue
            rows: list[dict[str, Any]] = []
            for section in ("added", "removed", "modified"):
                for entry in detail.get(section, []):
                    codes = entry.get("codes") or []
                    before = {"summary": entry.get("before") or entry.get("summary")} if section != "added" else None
                    after = {"summary": entry.get("after") or entry.get("summary")} if section != "removed" else None
                    rows.append({"entity_key": entry.get("key", ""), "gtip": codes[0] if codes else None, "change_type": section, "before": before, "after": after})
            try:
                self.ledger.record_batch(
                    kind="trade_measures", source_id=kind, title=detail.get("label") or KIND_LABELS.get(kind, kind),
                    new_snapshot_id=snapshot_id, old_snapshot_id=None, source_url=self.metadata(kind).get("source_url") or "",
                    sha256="", changes=rows, total_rows=int(self.metadata(kind).get("item_count") or 0),
                    detected_at=change.get("changed_at"), backfilled=True,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Trade measure ledger backfill failed: %s", exc)
        return written

    # ---- surveillance document cache
    def surveillance_doc(self, mevzuat_no: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM surveillance_docs WHERE mevzuat_no=?", (mevzuat_no,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_surveillance_doc(self, doc: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO surveillance_docs(mevzuat_no,title,rg_date,rg_no,url,fetched_at,payload) VALUES(?,?,?,?,?,?,?)",
                (
                    str(doc["mevzuat_no"]), doc.get("title", ""), doc.get("rg_date", ""), doc.get("rg_no", ""), doc.get("url", ""),
                    datetime.now(UTC).isoformat(timespec="seconds"), json.dumps(doc, ensure_ascii=False),
                ),
            )

    def changes(self, kind: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT id, kind, changed_at, added, removed, modified, detail FROM changes"
        params: tuple[Any, ...] = ()
        if kind:
            query += " WHERE kind=?"
            params = (kind,)
        query += " ORDER BY id DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["detail"] = json.loads(entry.pop("detail"))
            result.append(entry)
        return result


def _count_items(kind: str, payload: Any) -> int:
    if payload is None:
        return 0
    if kind == "anti_dumping":
        return len(payload.get("definitive", [])) + len(payload.get("provisional", []))
    if kind in {"surveillance", "tariff_quota"}:
        return sum(len(doc.get("items", [])) for doc in payload)
    return len(payload)


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _iso_date(value: Any) -> str | None:
    """dd/mm/yyyy or dd.mm.yyyy → ISO; ISO passes through; anything else → None."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def _ledger_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": entry.get("summary"), "codes": entry.get("codes"), "country": entry.get("country"),
        "product": entry.get("product"), "valid_from": entry.get("valid_from"), "valid_to": entry.get("valid_to"),
    }


def row_key_for(kind: str, *, row: dict[str, Any] | None = None, doc: dict[str, Any] | None = None,
                item: dict[str, Any] | None = None, section: str = "definitive") -> str:
    """Stable per-row key shared by the change ledger and the lineage table."""
    row, doc, item = row or {}, doc or {}, item or {}
    if kind == "anti_dumping":
        if section == "provisional":
            return f"geçici|{row.get('file_no')}|{row.get('country')}"
        codes = extract_codes(row.get("gtip", ""))
        return f"{row.get('file_no')}|{row.get('country')}|{' '.join(codes)[:80]}"
    if kind == "safeguard":
        return f"{row.get('file_no')}|{row.get('country')}"
    if kind == "surveillance":
        return f"{doc.get('mevzuat_no')}|{normalise_code(item.get('gtip', ''))}"
    if kind == "tariff_quota":
        return f"{doc.get('url')}|{normalise_code(item.get('gtip', ''))}|{item.get('quantity', '')}"
    return str(row.get("url", row.get("title", "")))


def _item_rows(kind: str, payload: Any) -> dict[str, dict[str, Any]]:
    """Satır anahtarı -> {summary, codes, row, country, product, valid_from, valid_to}."""
    rows: dict[str, dict[str, Any]] = {}
    if not payload:
        return rows
    if kind == "anti_dumping":
        for section in ("definitive", "provisional"):
            for row in payload.get(section, []):
                codes = extract_codes(row.get("gtip", ""))
                summary = (
                    f"{row.get('product')} – {row.get('rate')} ({row.get('communique')}, bitiş {row.get('expires')})"
                    if section == "definitive" else f"{row.get('product')} – {row.get('rate')} ({row.get('communique')})"
                )
                rows[row_key_for(kind, row=row, section=section)] = {
                    "summary": summary, "codes": codes, "row": row, "country": row.get("country"), "product": row.get("product"),
                    "valid_from": _iso_date(row.get("rg_date")), "valid_to": _iso_date(row.get("expires")),
                }
    elif kind == "safeguard":
        for row in payload:
            acts = row.get("acts") or []
            first_act = next((act for act in acts if act.get("rg_date")), {})
            rows[row_key_for(kind, row=row)] = {
                "summary": f"{row.get('product')} – {'; '.join(row.get('amounts', []))} (bitiş {row.get('expires')})",
                "codes": extract_codes(row.get("gtip", "")), "row": row, "country": row.get("country"), "product": row.get("product"),
                "valid_from": _iso_date(row.get("original_start")) or _iso_date(first_act.get("rg_date")),
                "valid_to": _iso_date(row.get("expires")),
            }
    elif kind == "surveillance":
        for doc in payload:
            for item in doc.get("items", []):
                code = normalise_code(item.get("gtip", ""))
                rows[row_key_for(kind, doc=doc, item=item)] = {
                    "summary": f"{doc.get('title')}: {item.get('gtip')} {item.get('value')} {item.get('unit') or ''}".strip(),
                    "codes": [code] if code else [], "row": {**item, "mevzuat_no": doc.get("mevzuat_no"), "title": doc.get("title")},
                    "country": item.get("country") or "Tüm ülkeler", "product": item.get("description"),
                    "valid_from": _iso_date(doc.get("rg_date")), "valid_to": None,
                }
    elif kind == "tariff_quota":
        for doc in payload:
            for item in doc.get("items", []):
                code = normalise_code(item.get("gtip", ""))
                rows[row_key_for(kind, doc=doc, item=item)] = {
                    "summary": f"{doc.get('origin') or doc.get('title')}: {item.get('gtip')} {item.get('quantity', '')} {item.get('duty_rate', '')}".strip(),
                    "codes": [code] if code else [], "row": {**item, "url": doc.get("url"), "title": doc.get("title")},
                    "country": doc.get("origin") or doc.get("title"), "product": item.get("description"),
                    "valid_from": None, "valid_to": None,
                }
    elif kind == "communiques":
        for row in payload:
            rows[row_key_for(kind, row=row)] = {
                "summary": row.get("title", ""), "codes": [], "row": row, "country": None, "product": row.get("title"),
                "valid_from": None, "valid_to": None,
            }
    return rows


def _item_keys(kind: str, payload: Any) -> dict[str, tuple[str, list[str]]]:
    """Değişiklik defteri için satır anahtarı -> (içerik özeti, GTİP kodları)."""
    return {key: (entry["summary"], entry["codes"]) for key, entry in _item_rows(kind, payload).items()}


def diff_payloads(kind: str, previous: Any, current: Any) -> dict[str, Any]:
    before = _item_keys(kind, previous)
    after = _item_keys(kind, current)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(key for key in set(before) & set(after) if before[key][0] != after[key][0])
    return {
        "kind": kind,
        "label": KIND_LABELS.get(kind, kind),
        "added_count": len(added), "removed_count": len(removed), "modified_count": len(modified),
        "total": len(added) + len(removed) + len(modified),
        "added": [{"key": key, "summary": after[key][0], "codes": after[key][1]} for key in added[:50]],
        "removed": [{"key": key, "summary": before[key][0], "codes": before[key][1]} for key in removed[:50]],
        "modified": [{"key": key, "before": before[key][0], "after": after[key][0], "codes": after[key][1]} for key in modified[:50]],
    }


# --------------------------------------------------------------------------- engine

@dataclass
class SyncOutcome:
    kind: str
    ok: bool
    message: str
    item_count: int = 0
    diff: dict[str, Any] | None = None
    source_url: str = ""


class TradeMeasureEngine:
    def __init__(self, data_dir: str | Path | None = None, *, http: httpx.AsyncClient | None = None) -> None:
        self.store = TradeMeasureStore(data_dir)
        self._http = http
        self._lock = asyncio.Lock()
        self.last_sync: dict[str, Any] = {"started_at": None, "finished_at": None, "outcomes": []}
        self.sync_interval = DEFAULT_SYNC_INTERVAL

    # ---- http
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=20.0),
                headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9"},
                verify=official_ssl_context(),
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- lookup
    def lookup(self, gtip: str, origin_country: str | None = None, *, today: date | None = None) -> TradeMeasureReport:
        """GTİP (4-12 hane) ve menşe için damping/sübvansiyon, korunma ve gözetim kapsamı."""
        today = today or date.today()
        digits = normalise_code(gtip)
        if len(digits) not in {4, 6, 8, 10, 12}:
            raise ValueError("GTİP 4, 6, 8, 10 veya 12 haneli olmalıdır.")
        report = TradeMeasureReport(gtip=digits, origin_country=origin_country, as_of=today.isoformat())
        report.sources = {kind: self.store.metadata(kind) for kind in ("anti_dumping", "safeguard", "surveillance", "tariff_quota")}
        if len(digits) < 12:
            report.warnings.append("Kod 12 haneden kısa; ön ek eşleşmeleri kapsam adayıdır, alt satıra göre doğrulayın.")
        report.anti_dumping = self._antidumping_hits(digits, origin_country, today)
        report.safeguard = self._safeguard_hits(digits, origin_country, today)
        report.surveillance = self._surveillance_hits(digits, today)
        report.tariff_quota = self._quota_hits(digits, origin_country)
        if origin_country and any(hit.origin_match is None for hit in report.anti_dumping):
            report.warnings.append("Menşe ülke resmî listedeki ülke adıyla eşleştirilemedi; damping satırlarını elle doğrulayın.")
        for kind in ("anti_dumping", "safeguard", "surveillance", "tariff_quota"):
            if report.sources[kind]["origin"] == "missing":
                report.warnings.append(f"{KIND_LABELS[kind]} verisi yüklü değil.")
        return report

    def _provenance(self, kind: str, row_key: str) -> dict[str, Any] | None:
        try:
            return self.store.provenance(kind, row_key)
        except Exception:  # noqa: BLE001 – lineage is informative, never blocking
            return None

    def _antidumping_hits(self, gtip: str, origin: str | None, today: date) -> list[MeasureHit]:
        data = self.store.load("anti_dumping") or {}
        hits: list[MeasureHit] = []
        for section, label in (("definitive", "kesin"), ("provisional", "geçici")):
            for row in data.get(section, []):
                matched = [code for code in extract_codes(row.get("gtip", "")) if code_matches(code, gtip)]
                if not matched:
                    continue
                kind = (row.get("kind") or "").upper()
                measure_type = "countervailing" if kind.startswith("SK") or "SÜBVANSİYON" in kind else "anti_dumping"
                hits.append(
                    MeasureHit(
                        provenance=self._provenance("anti_dumping", row_key_for("anti_dumping", row=row, section=section)),
                        measure_type=measure_type,
                        matched_code=max(matched, key=len),
                        country=row.get("country", ""),
                        origin_match=_country_matches(origin, row.get("country", "")),
                        rate_text=row.get("rate", ""),
                        unit_value_usd=None,
                        unit=None,
                        product=row.get("product", ""),
                        legal_act=f"İthalatta Haksız Rekabetin Önlenmesine İlişkin Tebliğ {row.get('communique', '').strip()} ({label} önlem)",
                        gazette=f"RG {row.get('rg_date', '')} / {row.get('rg_no', '')}",
                        expires=row.get("expires") or None,
                        status=_status(row.get("expires"), today) if section == "definitive" else "in_force",
                        notes=" | ".join(part for part in (row.get("kind", ""), row.get("notes", "")) if part),
                        source="Ticaret Bakanlığı – Yürürlükteki Damping/Sübvansiyon Önlemleri listesi",
                    )
                )
        return hits

    def _safeguard_hits(self, gtip: str, origin: str | None, today: date) -> list[MeasureHit]:
        data = self.store.load("safeguard") or []
        hits: list[MeasureHit] = []
        for row in data:
            matched = [code for code in extract_codes(row.get("gtip", "")) if code_matches(code, gtip)]
            if not matched:
                continue
            acts = row.get("acts", [])
            decree = next(
                (act for act in reversed(acts) if "Karar" in act.get("kind", "") and "Geçici" not in act.get("kind", "")),
                acts[-1] if acts else {},
            )
            hits.append(
                MeasureHit(
                    provenance=self._provenance("safeguard", row_key_for("safeguard", row=row)),
                    measure_type="safeguard",
                    matched_code=max(matched, key=len),
                    country=row.get("country", ""),
                    origin_match=_country_matches(origin, row.get("country", "")),
                    rate_text="; ".join(row.get("amounts", [])),
                    unit_value_usd=None,
                    unit=None,
                    product=row.get("product", "").replace("\n", " "),
                    legal_act=f"Korunma önlemi kararı {decree.get('number', '')} ({row.get('stage', '')})".strip(),
                    gazette=f"RG {decree.get('rg_date', '')} / {decree.get('rg_no', '')}",
                    expires=row.get("expires") or None,
                    status=_status(row.get("expires"), today),
                    notes=(
                        "Tarife kontenjanı (GYÜ/genel) tahsisi var; kontenjan dâhilinde ithalatta önlem uygulanmaz."
                        if any("Kontenjan" in act.get("kind", "") for act in acts)
                        else ""
                    ),
                    source="Ticaret Bakanlığı – Yürürlükte Bulunan Korunma Önlemleri listesi",
                )
            )
        return hits

    def _surveillance_hits(self, gtip: str, today: date) -> list[MeasureHit]:
        data = self.store.load("surveillance") or []
        hits: list[MeasureHit] = []
        for doc in data:
            for item in doc.get("items", []):
                code = normalise_code(item.get("gtip", ""))
                if not code_matches(code, gtip):
                    continue
                hits.append(
                    MeasureHit(
                        provenance=self._provenance("surveillance", row_key_for("surveillance", doc=doc, item=item)),
                        measure_type="surveillance",
                        matched_code=code,
                        country=item.get("country") or "Tüm ülkeler",
                        origin_match=True,
                        rate_text=item.get("value", ""),
                        unit_value_usd=_parse_unit_value(item.get("value", "")),
                        unit=item.get("unit") or doc.get("unit"),
                        product=item.get("description", ""),
                        legal_act=doc.get("title", SURVEILLANCE_TITLE),
                        gazette=f"RG {doc.get('rg_date', '')} / {doc.get('rg_no', '')}",
                        expires=None,
                        status="in_force",
                        notes=item.get("note", ""),
                        source=doc.get("url") or MEVZUAT_PUBLIC.format(no=doc.get("mevzuat_no", "")),
                    )
                )
        return hits

    def _quota_hits(self, gtip: str, origin: str | None) -> list[MeasureHit]:
        data = self.store.load("tariff_quota") or []
        hits: list[MeasureHit] = []
        for doc in data:
            for item in doc.get("items", []):
                code = normalise_code(item.get("gtip", ""))
                if not code_matches(code, gtip):
                    continue
                details = " · ".join(part for part in (item.get("quantity", ""), item.get("period", ""), f"vergi {item['duty_rate']}" if item.get("duty_rate") else "") if part)
                hits.append(
                    MeasureHit(
                        provenance=self._provenance("tariff_quota", row_key_for("tariff_quota", doc=doc, item=item)),
                        measure_type="tariff_quota",
                        matched_code=code,
                        country=doc.get("origin") or doc.get("title", ""),
                        origin_match=_country_matches(origin, doc.get("origin") or doc.get("title", "")),
                        rate_text=details,
                        unit_value_usd=None,
                        unit=None,
                        product=item.get("description", ""),
                        legal_act=doc.get("title", ""),
                        gazette="",
                        expires=None,
                        status="in_force",
                        notes="Kontenjan dâhilinde ithalatta indirimli/sıfır vergi; tahsis için Bakanlığa (İthalatBİS) başvuru gerekir.",
                        source=doc.get("url", AGRI_QUOTA_PAGE),
                    )
                )
        return hits

    def communiques(self, year: int | None = None) -> list[dict[str, str]]:
        data = self.store.load("communiques") or []
        if year:
            data = [row for row in data if row.get("year") == str(year)]
        return data

    def status(self) -> dict[str, Any]:
        return {
            "datasets": {kind: {"label": KIND_LABELS[kind], **self.store.metadata(kind)} for kind in KINDS},
            "sync_interval_seconds": self.sync_interval,
            "last_sync": self.last_sync,
            "recent_changes": self.store.changes(limit=10),
        }

    # ---- sync
    async def sync(self, *, kinds: Iterable[str] | None = None) -> list[SyncOutcome]:
        wanted = list(kinds or KINDS)
        async with self._lock:
            self.last_sync = {"started_at": datetime.now(UTC).isoformat(timespec="seconds"), "finished_at": None, "outcomes": []}
            outcomes: list[SyncOutcome] = []
            steps: dict[str, Callable[[], Any]] = {
                "anti_dumping": self._sync_antidumping,
                "safeguard": self._sync_safeguard,
                "surveillance": self._sync_surveillance,
                "tariff_quota": self._sync_quotas,
                "communiques": self._sync_communiques,
            }
            for kind in wanted:
                try:
                    outcome = await steps[kind]()
                except Exception as exc:  # noqa: BLE001 – her kaynak bağımsız raporlanır
                    logger.exception("Trade measure sync failed for %s", kind)
                    outcome = SyncOutcome(kind=kind, ok=False, message=f"{type(exc).__name__}: {exc}"[:400])
                outcomes.append(outcome)
                self.last_sync["outcomes"].append(outcome.__dict__)
            self.last_sync["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            return outcomes

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Resmî kaynaktan indirir; her yönlendirme adımı yeniden doğrulanır.

        Yönlendirme istemciye bırakılmaz: izin listesine giren bir adres, izin
        listesi dışına yönlendirebilirdi. Boyut da okunurken sınırlanır, çünkü
        `content-length` başlığı sunucunun iddiasıdır, ölçüm değil.
        """
        client = self._client()
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_OFFICIAL_HOSTS)
            async with client.stream("GET", current, follow_redirects=False, **kwargs) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError("Resmî kaynak hedefsiz yönlendirme döndürdü.")
                    current = urljoin(str(response.url), location)
                    continue
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise ValueError("Resmî kaynak dosyası 50 MB güvenlik sınırını aşıyor.")
                    chunks.append(chunk)
                buffered = httpx.Response(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=b"".join(chunks),
                    request=response.request,
                )
            buffered.raise_for_status()
            return buffered
        raise ValueError("Resmî kaynak çok fazla yönlendirme yaptı.")

    async def _sync_antidumping(self) -> SyncOutcome:
        page = await self._get(ANTIDUMPING_PAGE)
        link = discover_workbook_link(page.text, ANTIDUMPING_PAGE, must_contain=("rürlükteki",))
        if not link:
            raise ValueError("Damping sayfasında 'Yürürlükteki Önlemler' çalışma kitabı bağlantısı bulunamadı.")
        content = (await self._get(link)).content
        payload = parse_antidumping_workbook(content)
        diff = self.store.save("anti_dumping", payload, source_url=link, source_label="Ticaret Bakanlığı – Yürürlükteki Önlemler (damping/sübvansiyon)", sha256=hashlib.sha256(content).hexdigest())
        return SyncOutcome("anti_dumping", True, "güncellendi", _count_items("anti_dumping", payload), diff, link)

    async def _sync_safeguard(self) -> SyncOutcome:
        page = await self._get(SAFEGUARD_PAGE)
        link = discover_workbook_link(page.text, SAFEGUARD_PAGE, must_contain=("rürlükteki",))
        if not link:
            raise ValueError("Korunma önlemleri sayfasında yürürlükteki önlemler çalışma kitabı bulunamadı.")
        content = (await self._get(link)).content
        payload = parse_safeguard_workbook(content)
        diff = self.store.save("safeguard", payload, source_url=link, source_label="Ticaret Bakanlığı – Yürürlükte Bulunan Korunma Önlemleri", sha256=hashlib.sha256(content).hexdigest())
        return SyncOutcome("safeguard", True, "güncellendi", len(payload), diff, link)

    async def _sync_quotas(self) -> SyncOutcome:
        page = await self._get(AGRI_QUOTA_PAGE)
        links = discover_quota_documents(page.text, AGRI_QUOTA_PAGE)
        if not links:
            raise ValueError("Tarım ürünleri tarife kontenjanı sayfasında karar belgesi bulunamadı.")
        docs: list[dict[str, Any]] = []
        for link in links:
            cached = self.store.surveillance_doc(f"quota:{link['url']}")
            if cached and cached.get("parser_version") == PARSER_VERSION:
                docs.append(cached)
                continue
            response = await self._get(link["url"])
            doc = parse_quota_document(response.content, link["filename"], link)
            doc["mevzuat_no"] = f"quota:{link['url']}"
            self.store.save_surveillance_doc(doc)
            docs.append(doc)
            await asyncio.sleep(0.3)
        docs = [doc for doc in docs if doc.get("items")]
        diff = self.store.save("tariff_quota", docs, source_url=AGRI_QUOTA_PAGE, source_label="Ticaret Bakanlığı – tarım ürünlerinde açılan tarife kontenjanları")
        return SyncOutcome("tariff_quota", True, "güncellendi", _count_items("tariff_quota", docs), diff, AGRI_QUOTA_PAGE)

    async def _sync_communiques(self) -> SyncOutcome:
        year = datetime.now().year
        entries: list[dict[str, str]] = []
        source = ""
        for candidate in (year, year - 1):
            url = COMMUNIQUE_PAGE.format(year=candidate)
            try:
                page = await self._get(url)
            except httpx.HTTPStatusError:
                continue
            parsed = parse_communique_index(page.text, url, candidate)
            if parsed:
                entries.extend(parsed)
                source = source or url
        if not entries:
            raise ValueError("İthalat Tebliğleri dizin sayfasında tebliğ bağlantısı bulunamadı.")
        diff = self.store.save("communiques", entries, source_url=source, source_label="Ticaret Bakanlığı – İthalat Tebliğleri")
        return SyncOutcome("communiques", True, "güncellendi", len(entries), diff, source)

    async def _mevzuat_search(self, title: str) -> list[dict[str, Any]]:
        # Bu iki adres sabittir (kazınan girdi değil) ama izin listesi tek bir
        # yerde tutulsun diye burada da doğrulanır.
        validate_outbound_url(MEVZUAT_HOME, allowed_hosts=_OFFICIAL_HOSTS)
        validate_outbound_url(MEVZUAT_DATATABLE, allowed_hosts=_OFFICIAL_HOSTS)
        client = self._client()
        home = await client.get(MEVZUAT_HOME)
        home.raise_for_status()
        token = ""
        for name, value in client.cookies.items():
            if "Antiforgery" in name:
                token = value
        rows: list[dict[str, Any]] = []
        start = 0
        records_total = 0
        raw_item_count = 0
        sample_keys: list[str] | None = None
        while True:
            payload = {
                "draw": 1,
                "columns": [{"data": None, "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}}] * 3,
                "order": [], "start": start, "length": 20, "search": {"value": "", "regex": False},
                "parameters": {
                    "MevzuatTur": "Teblig", "YonetmelikMevzuatTur": "OsmanliKanunu", "AranacakIfade": title,
                    "TamCumle": "false", "AranacakYer": "2", "MevzuatNo": "", "KurumId": "0", "AltKurumId": "0",
                    "BaslangicTarihi": "", "BitisTarihi": "", "antiforgerytoken": token,
                },
            }
            response = await client.post(
                MEVZUAT_DATATABLE,
                json=payload,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": MEVZUAT_HOME},
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data", [])
            records_total = int(body.get("recordsTotal", 0))
            raw_item_count += len(data)
            if data and sample_keys is None:
                sample_keys = sorted(data[0].keys())
            for item in data:
                name = BeautifulSoup(item.get("mevAdi", ""), "html.parser").get_text(" ", strip=True)
                if SURVEILLANCE_TITLE.lower()[:20] not in name.lower() and "gözetim" not in name.lower():
                    continue
                rows.append(
                    {
                        "mevzuat_no": str(item.get("mevzuatNo", "")), "title": name,
                        "rg_date": item.get("resmiGazeteTarihi", ""), "rg_no": item.get("resmiGazeteSayisi", ""),
                        "url": MEVZUAT_PUBLIC.format(no=item.get("mevzuatNo", "")),
                    }
                )
            start += len(data)
            if not data or start >= records_total:
                break
            await asyncio.sleep(0.5)
        if not rows:
            # Aramanın neden bos dondugunu bir sonraki hata mesajinda gorebilmek icin
            # (antiforgery cerezi, sunucunun bildirdigi toplam kayit ve alan adlari
            # sitenin API'sini gozlemsiz degistirmesi durumunda tani koymayi saglar).
            detail = f"antiforgery cerezi {'bulundu' if token else 'BULUNAMADI'}, recordsTotal={records_total}, satir={raw_item_count}"
            if sample_keys is not None:
                detail += f", ornek alanlar={sample_keys}"
            raise ValueError(f"mevzuat.gov.tr aramasında '{title}' için sonuç bulunamadı ({detail}).")
        return rows

    async def _sync_surveillance(self) -> SyncOutcome:
        index = await self._mevzuat_search(SURVEILLANCE_TITLE)
        docs: list[dict[str, Any]] = []
        fetched = 0
        for entry in index:
            cached = self.store.surveillance_doc(entry["mevzuat_no"])
            if cached and cached.get("rg_date") == entry["rg_date"] and cached.get("items") is not None and cached.get("parser_version") == PARSER_VERSION:
                docs.append(cached)
                continue
            response = await self._get(MEVZUAT_IFRAME.format(no=entry["mevzuat_no"]))
            doc = parse_surveillance_page(response.text, entry)
            self.store.save_surveillance_doc(doc)
            docs.append(doc)
            fetched += 1
            await asyncio.sleep(0.4)
        docs.sort(key=lambda doc: (doc.get("rg_date", "")[-4:], doc.get("rg_date", "")[3:5], doc.get("rg_date", "")[:2]), reverse=True)
        diff = self.store.save("surveillance", docs, source_url=MEVZUAT_HOME, source_label="mevzuat.gov.tr – yürürlükteki gözetim tebliğleri")
        return SyncOutcome("surveillance", True, f"güncellendi ({fetched} yeni metin indirildi)", _count_items("surveillance", docs), diff, MEVZUAT_HOME)

    async def periodic_sync_loop(self, *, initial_delay: float = 90.0) -> None:
        """Günde bir (varsayılan) resmî listeleri yeniler; hata olursa bir sonraki turda yeniden dener."""
        await asyncio.sleep(initial_delay)
        while True:
            try:
                outcomes = await self.sync()
                failed = [outcome.kind for outcome in outcomes if not outcome.ok]
                if failed:
                    logger.warning("Trade measure sync finished with failures: %s", ", ".join(failed))
                else:
                    logger.info("Trade measure sync completed")
            except Exception:  # noqa: BLE001
                logger.exception("Trade measure sync loop crashed; retrying next interval")
            await asyncio.sleep(max(3600, self.sync_interval))


def summary_lines(report: TradeMeasureReport) -> list[str]:
    """Kısa, insan okunur özet (danışman ve e-posta çıktıları için)."""
    lines: list[str] = []
    for hit in report.anti_dumping:
        if hit.origin_match is False:
            continue
        label = "Sübvansiyona karşı önlem" if hit.measure_type == "countervailing" else "Dampinge karşı önlem"
        lines.append(f"{label}: {hit.country} menşeli {hit.product} ({hit.matched_code}) – {hit.rate_text} [{hit.legal_act}, {hit.status}]")
    for hit in report.safeguard:
        lines.append(f"Korunma önlemi: {hit.product} ({hit.matched_code}) – {hit.rate_text} [{hit.legal_act}, bitiş {hit.expires}]")
    for hit in report.surveillance:
        lines.append(f"Gözetim: {hit.product} ({hit.matched_code}) – birim kıymet {hit.rate_text} {hit.unit or ''} [{hit.legal_act}]")
    for hit in report.tariff_quota:
        if hit.origin_match is False:
            continue
        lines.append(f"Tarife kontenjanı: {hit.country} – {hit.product} ({hit.matched_code}) – {hit.rate_text} [{hit.legal_act}]")
    return lines
