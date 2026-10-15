"""Multi-line landed-cost calculation from a spreadsheet (CSV/XLSX) or JSON rows.

Each row is one declaration line: tariff code, origin, optional dispatch country, value
inputs and the user-verified tax facts.  Rows are calculated one by one through
``TariffEngine.calculate`` so every line carries the same official-rate provenance,
warnings and missing-input list as a single calculation.  Nothing is inferred: a
missing verified rate leaves the line ``partial`` instead of becoming zero.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any

import openpyxl
from pydantic import ValidationError

from tariff_engine import LandedCostInput, TariffEngine

MAX_ROWS = 200
MAX_FILE_BYTES = 2 * 1024 * 1024

# Normalised header → LandedCostInput / request field.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "gtip": ("gtip", "gtp", "hs", "hs kodu", "tarife kodu", "tarife", "cn", "code", "kod"),
    "origin_country": ("mense", "mense ulke", "mense ulkesi", "origin", "origin country", "ulke"),
    "dispatch_country": ("sevk", "sevk ulkesi", "cikis ulkesi", "dispatch", "dispatch country", "cikis"),
    "description": ("aciklama", "urun", "esya", "tanim", "description", "product", "kalem"),
    "invoice_value": ("fatura", "fatura bedeli", "bedel", "invoice", "invoice value", "kiymet", "fob", "tutar"),
    "freight": ("navlun", "freight"),
    "insurance": ("sigorta", "insurance"),
    "other_costs": ("diger", "diger gider", "diger giderler", "other", "other costs", "tescil oncesi gider"),
    "quantity": ("miktar", "adet", "quantity", "qty", "kg"),
    "currency": ("para birimi", "doviz", "currency", "pb"),
    "vat_rate": ("kdv", "kdv orani", "vat", "vat rate"),
    "payment_method": ("odeme", "odeme sekli", "payment", "payment method"),
    "kkdf_rate": ("kkdf", "kkdf orani"),
    "anti_dumping_amount": ("damping", "damping tutari", "anti dumping", "dumping", "subvansiyon"),
    "sct_amount": ("otv", "otv tutari", "sct"),
    "surveillance_unit_value": ("gozetim", "gozetim kiymeti", "gozetim birim kiymeti", "surveillance"),
    "has_surveillance_certificate": ("gozetim belgesi", "surveillance certificate"),
    "atr_certificate": ("atr", "a tr", "atr belgesi", "a tr belgesi", "atr certificate"),
    "additional_financial_liability_rate": ("emy", "ek mali yukumluluk", "ek mali yukumluluk orani"),
    "customs_duty_rate": ("gv", "gumruk vergisi", "gumruk vergisi orani", "customs duty"),
    "additional_duty_rate": ("igv", "ilave gumruk vergisi", "additional duty"),
    "trt_bandrol_rate": ("trt", "trt bandrol", "bandrol", "trt bandrol orani"),
    "exchange_rate": ("kur", "doviz kuru", "tcmb kuru", "exchange rate", "rate"),
    "exchange_rate_date": ("kur tarihi", "tescil tarihi", "exchange rate date"),
    "stamp_duty_try": ("damga", "damga vergisi", "stamp duty"),
    "port_storage_try": ("liman", "ardiye", "liman ardiye", "tahmil tahliye", "port storage"),
    "gekap_try": ("gekap", "geri kazanim katilim payi"),
}
_NUMERIC_FIELDS = {
    "invoice_value", "freight", "insurance", "other_costs", "quantity", "vat_rate", "kkdf_rate",
    "anti_dumping_amount", "sct_amount", "surveillance_unit_value", "additional_financial_liability_rate",
    "customs_duty_rate", "additional_duty_rate", "trt_bandrol_rate", "exchange_rate", "stamp_duty_try",
    "port_storage_try", "gekap_try",
}
_DEFAULT_ZERO = {"freight", "insurance", "other_costs"}

TEMPLATE_HEADERS = [
    "GTİP", "Menşe", "Sevk ülkesi", "Açıklama", "Fatura bedeli", "Navlun", "Sigorta", "Diğer gider", "Miktar",
    "Para birimi", "KDV", "Ödeme şekli", "KKDF", "Damping", "ÖTV", "Gözetim", "EMY", "GV", "İGV",
    "Kur", "Kur tarihi", "Damga vergisi", "TRT bandrol", "Liman/ardiye", "GEKAP",
]
TEMPLATE_ROWS = [
    ["6911.10.00.00.11", "Çin", "", "Porselen yemek takımı", "10000", "1200", "50", "300", "2000", "USD", "20", "mal mukabili", "", "0", "0", "0", "0", "", "", "41,25", "2026-09-05", "1.250", "0", "18.000", "0"],
    ["6103.42.00.00.00", "Almanya", "Almanya", "Pamuklu erkek pantolon", "25.000,00", "800", "0", "0", "1500", "EUR", "10", "peşin", "0", "0", "0", "0", "0", "", "", "", "", "", "", "", ""],
]


def normalise_header(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ı", "i"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[%()/._-]+", " ", text)
    return " ".join(text.split())


# Oran ve kur alanlarında "41.250" binlik değil ondalık okunur (KDV %41.250 veya kur 41250 TL
# anlamsızdır); tutar alanlarında Türkçe binlik ayırıcı kuralı korunur ve uyarı üretilir.
_DECIMAL_DOT_FIELDS = frozenset({
    "exchange_rate", "vat_rate", "kkdf_rate", "customs_duty_rate", "additional_duty_rate",
    "additional_financial_liability_rate", "trt_bandrol_rate",
})
_DOT_THOUSANDS_RE = re.compile(r"-?\d{1,3}(\.\d{3})+")


def _clean_number_text(value: Any) -> str:
    return re.sub(r"[\s₺$€£%]|TL|USD|EUR|GBP", "", str(value), flags=re.IGNORECASE)


def is_ambiguous_number(value: Any) -> bool:
    """"12.500" gibi yalnız noktalı ve noktadan sonra tam üç hane: binlik mi ondalık mı belirsiz."""
    if value is None or isinstance(value, (bool, int, float)):
        return False
    return bool(_DOT_THOUSANDS_RE.fullmatch(_clean_number_text(value)))


def parse_number(value: Any, *, dot_is_decimal: bool = False) -> float | None:
    """Accept 1.234,56 / 1234.56 / 12.500 / 12,5; empty → None.

    ``dot_is_decimal`` oran/kur alanları içindir: "41.250" → 41.25.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_number_text(value)
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif not dot_is_decimal and _DOT_THOUSANDS_RE.fullmatch(text):
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = normalise_header(value)
    if text in {"evet", "var", "yes", "true", "1", "e"}:
        return True
    if text in {"hayir", "yok", "no", "false", "0", "h"}:
        return False
    return None


def _map_headers(headers: list[Any]) -> dict[int, str]:
    lookup = {alias: field for field, aliases in _COLUMN_ALIASES.items() for alias in aliases}
    mapping: dict[int, str] = {}
    for index, header in enumerate(headers):
        key = normalise_header(header)
        if not key:
            continue
        field = lookup.get(key)
        if field is None:
            # "Fatura bedeli (USD)" → "fatura bedeli"; "KDV %" → "kdv"
            for alias, candidate in lookup.items():
                if key.startswith(alias + " ") or key == alias:
                    field = candidate
                    break
        if field and field not in mapping.values():
            mapping[index] = field
    return mapping


def _table_from_bytes(data: bytes, file_name: str) -> list[list[Any]]:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Dosya 2 MB sınırını aşıyor.")
    name = (file_name or "").casefold()
    if name.endswith((".xlsx", ".xlsm")):
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.worksheets[0]
        rows = [list(row) for row in sheet.iter_rows(values_only=True, max_row=MAX_ROWS + 50)]
        workbook.close()
        return rows
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1254", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def rows_from_table(table: list[list[Any]]) -> list[dict[str, Any]]:
    """Turn a header + data table into request rows; unknown columns are ignored."""
    cleaned = [row for row in table if any(str(cell or "").strip() for cell in row)]
    if not cleaned:
        raise ValueError("Dosyada satır bulunamadı.")
    mapping = _map_headers(cleaned[0])
    if "gtip" not in mapping.values():
        raise ValueError("Başlık satırında GTİP sütunu bulunamadı (kabul edilen adlar: GTİP, HS, tarife kodu).")
    invoice_is_fob = any(
        field == "invoice_value" and normalise_header(cleaned[0][index]).startswith("fob")
        for index, field in mapping.items()
    )
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(cleaned[1:], start=2):
        row: dict[str, Any] = {"line": line_number}
        for index, field in mapping.items():
            value = raw[index] if index < len(raw) else None
            if field in _NUMERIC_FIELDS:
                row[field] = parse_number(value, dot_is_decimal=field in _DECIMAL_DOT_FIELDS)
                if field not in _DECIMAL_DOT_FIELDS and is_ambiguous_number(value):
                    row.setdefault("_notes", []).append(_ambiguity_note(field, value, row[field]))
            elif field in {"has_surveillance_certificate", "atr_certificate"}:
                row[field] = parse_bool(value)
            else:
                text = str(value or "").strip()
                row[field] = text or None
        if invoice_is_fob and not row.get("freight") and not row.get("insurance"):
            row.setdefault("_notes", []).append(
                "Bedel sütunu FOB; navlun ve sigorta boş olduğu için gümrük kıymeti (CIF) eksik hesaplanmış olabilir."
            )
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"En fazla {MAX_ROWS} satır hesaplanabilir.")
    return rows


def rows_from_upload(data: bytes, file_name: str) -> list[dict[str, Any]]:
    return rows_from_table(_table_from_bytes(data, file_name))


def template_csv() -> str:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerows(TEMPLATE_ROWS)
    return "﻿" + output.getvalue()


def _ambiguity_note(field: str, raw: Any, parsed: float | None) -> str:
    shown = f"{parsed:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if parsed is not None else "?"
    return (
        f"'{field}' alanındaki '{str(raw).strip()}' Türkçe binlik biçimle {shown} okundu; "
        "ondalık kastedildiyse virgül kullanın (ör. 12,5)."
    )


def _clean_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a request row into (context, LandedCostInput kwargs)."""
    notes: list[str] = [str(note) for note in row.get("_notes") or []]
    context = {
        "line": int(row.get("line") or 0) or None,
        "gtip": str(row.get("gtip") or "").strip(),
        "origin_country": str(row.get("origin_country") or "").strip()[:100],
        "dispatch_country": (str(row.get("dispatch_country") or "").strip()[:100] or None),
        "description": (str(row.get("description") or "").strip()[:200] or None),
        "notes": notes,
    }
    payload: dict[str, Any] = {}
    for field in _NUMERIC_FIELDS:
        raw = row.get(field)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            value = parse_number(raw, dot_is_decimal=field in _DECIMAL_DOT_FIELDS)
            if field not in _DECIMAL_DOT_FIELDS and is_ambiguous_number(raw):
                notes.append(_ambiguity_note(field, raw, value))
        else:
            value = float(raw)
        if value is None and field in _DEFAULT_ZERO:
            value = 0.0
        if value is not None:
            payload[field] = value
    currency = str(row.get("currency") or "USD").strip().upper()[:3]
    payload["currency"] = currency or "USD"
    payment = row.get("payment_method")
    if payment:
        payload["payment_method"] = str(payment)[:100]
    date_text = str(row.get("exchange_rate_date") or "").strip()
    if date_text:
        match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", date_text)
        payload["exchange_rate_date"] = f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}" if match else date_text[:10]
    certificate = row.get("has_surveillance_certificate")
    if certificate is not None:
        payload["has_surveillance_certificate"] = parse_bool(certificate) if not isinstance(certificate, bool) else certificate
    return context, payload


async def calculate_rows(
    engine: TariffEngine,
    rows: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    atr_certificate: bool | None = None,
) -> dict[str, Any]:
    """``as_of`` (tescil tarihi) ve ``atr_certificate`` tek satır hesabındaki gibi motora iletilir.

    Satırda "A.TR" sütunu varsa o satır için istek düzeyindeki değeri ezer.
    """
    if len(rows) > MAX_ROWS:
        raise ValueError(f"En fazla {MAX_ROWS} satır hesaplanabilir.")
    results: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Any]] = {}
    for position, row in enumerate(rows, start=1):
        context, payload = _clean_row(row)
        notes = context.pop("notes", [])
        row_atr = row.get("atr_certificate")
        row_atr = parse_bool(row_atr) if row_atr is not None and not isinstance(row_atr, bool) else row_atr
        item: dict[str, Any] = {"index": position, **context, "status": "error", "currency": payload.get("currency")}
        try:
            if not context["gtip"]:
                raise ValueError("GTİP boş.")
            if not context["origin_country"]:
                raise ValueError("Menşe ülke boş.")
            inputs = LandedCostInput.model_validate(payload)
            result = await engine.calculate(
                context["gtip"],
                context["origin_country"],
                inputs,
                dispatch_country=context["dispatch_country"],
                atr_certificate=row_atr if row_atr is not None else atr_certificate,
                as_of=as_of,
            )
        except ValidationError as exc:
            item["error"] = "Girdi doğrulanamadı: " + exc.errors(include_url=False)[0].get("msg", "alanları kontrol edin")
            results.append(item)
            continue
        except ValueError as exc:
            item["error"] = str(exc)
            results.append(item)
            continue
        tariff, cost = result["tariff"], result["cost"]
        item.update(
            {
                "status": cost["status"],
                "gtip": tariff["gtip"],
                "tariff_status": tariff["status"],
                "resolved_country_group": tariff.get("resolved_country_group"),
                "origin_recognised": tariff.get("origin_recognised", True),
                "atr_free_circulation": tariff.get("atr_free_circulation", False),
                "rates": {
                    line["code"]: line.get("rate") for line in cost["lines"]
                    if line["code"] in {"customs_duty", "additional_duty", "financial_liability", "kkdf", "vat"}
                },
                "customs_value": cost["customs_value"],
                "total_taxes": cost.get("total_taxes"),
                "landed_total": cost.get("landed_total"),
                "unit_landed_cost": cost.get("unit_landed_cost"),
                "landed_total_try": (cost.get("try_summary") or {}).get("landed_total_try"),
                "total_taxes_try": (cost.get("try_summary") or {}).get("total_taxes_try"),
                "missing_rates": cost.get("missing_rates", []),
                "rate_overrides": cost.get("rate_overrides", []),
                "warnings": [
                    *notes,
                    *[w for w in [*tariff.get("warnings", []), *cost.get("warnings", [])] if "kapsam matrisi" not in w][:4],
                ],
            }
        )
        bucket = totals.setdefault(
            cost["currency"],
            {"currency": cost["currency"], "rows": 0, "complete_rows": 0, "customs_value": 0.0, "total_taxes": 0.0, "landed_total": 0.0},
        )
        bucket["rows"] += 1
        bucket["customs_value"] = round(bucket["customs_value"] + cost["customs_value"], 2)
        if cost.get("landed_total") is not None:
            bucket["complete_rows"] += 1
            bucket["total_taxes"] = round(bucket["total_taxes"] + (cost.get("total_taxes") or 0.0), 2)
            bucket["landed_total"] = round(bucket["landed_total"] + cost["landed_total"], 2)
        results.append(item)
    summary = {
        "rows": len(results),
        "complete": sum(1 for item in results if item["status"] == "complete"),
        "partial": sum(1 for item in results if item["status"] == "partial"),
        "errors": sum(1 for item in results if item["status"] == "error"),
    }
    return {
        "rows": results,
        "totals": list(totals.values()),
        "summary": summary,
        "legal_notice": (
            "Toplu hesap her satırı tek satır hesabıyla aynı kaynak ve uyarılarla üretir; eksik doğrulanmış girdisi olan "
            "satırların toplamı 'partial' kalır ve genel toplama alınmaz. Beyan öncesi her satır ayrıca doğrulanmalıdır."
        ),
    }
