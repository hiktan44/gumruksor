"""Risk kriterleri için yönetici toplu CSV içe aktarma.

Akış (uzman odaklı): yönetici şablonu indirir, kaynaklı satırları doldurur, yükler. Her satır
``RiskStore._validate`` kurallarıyla (kaynak adı + https kaynak bağlantısı, yıl, kategori)
doğrulanır ve ayrıca **son doğrulama tarihi** zorunludur. Dosyada tek hata varsa hiçbir satır
yazılmaz (kısmi yükleme yok); rapor her satırın durumunu söyler. Hatasız dosyadaki satırlar
**taslak** olarak yazılır ve bir yönetici ya da aktif danışman onaylayana kadar risk kontrolüne
girmez. Kullanıcı notları bu akıştan bağımsızdır.
"""
from __future__ import annotations

import csv
import io
import uuid
from typing import Any

from risk_engine import CATEGORIES, RiskError, RiskStore, parse_verified_at

COLUMNS = (
    "gtip_prefix", "direction", "year_from", "year_to", "category", "title", "description",
    "source_label", "source_url", "verified_at",
)
REQUIRED = ("gtip_prefix", "category", "title", "source_label", "source_url", "verified_at")
MAX_ROWS = 1000
MAX_BYTES = 1_000_000
#: Excel'in Türkçe sürümünde ``;`` ayırıcıdır; ikisi de kabul edilir.
_DIRECTION_ALIASES = {
    "": "both", "both": "both", "her ikisi": "both", "ithalat": "import", "import": "import",
    "ihracat": "export", "export": "export",
}
_CATEGORY_ALIASES = {label.casefold(): key for key, label in CATEGORIES.items()}


def template_csv() -> str:
    """Yalnız başlık satırı: örnek veri konmaz, çünkü örnek satır resmî kayıt gibi içe aktarılabilirdi."""
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=";", lineterminator="\r\n").writerow(COLUMNS)
    return "﻿" + buffer.getvalue()


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    clean = {key: str(row.get(key) or "").strip() for key in COLUMNS}
    direction = clean["direction"].casefold()
    if direction not in _DIRECTION_ALIASES:
        raise RiskError("Yön ithalat, ihracat veya boş olmalıdır.")
    clean["direction"] = _DIRECTION_ALIASES[direction]
    category = clean["category"]
    if category.casefold() in _CATEGORY_ALIASES:
        clean["category"] = _CATEGORY_ALIASES[category.casefold()]
    return clean


def parse_csv(text: str) -> list[tuple[int, dict[str, Any]]]:
    """CSV'yi satırlara ayırır. Başlık hataları ``RiskError``; boş satırlar atlanır."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise RiskError("Dosya en çok 1 MB olabilir.")
    text = text.lstrip("﻿")
    first_line = text.splitlines()[0] if text.strip() else ""
    if not first_line:
        raise RiskError("Dosya boş.")
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = [str(name or "").strip() for name in (reader.fieldnames or [])]
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        raise RiskError("Başlıkta eksik sütun: " + ", ".join(missing) + ". Şablonu indirip kullanın.")
    reader.fieldnames = header
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_no, row in enumerate(reader, start=2):
        if not any(str(value or "").strip() for value in row.values() if not isinstance(value, list)):
            continue
        rows.append((line_no, row))
        if len(rows) > MAX_ROWS:
            raise RiskError(f"Dosya en çok {MAX_ROWS} satır içerebilir.")
    if not rows:
        raise RiskError("Dosyada veri satırı yok.")
    return rows


def row_identifier(clean: dict[str, Any]) -> str:
    """Aynı kayıt tekrar yüklenirse çoğalmasın diye içerikten türetilen kimlik."""
    seed = "|".join((clean["gtip_prefix"], clean["direction"], clean["category"], clean["title"], clean["source_url"]))
    return uuid.uuid5(uuid.NAMESPACE_URL, "risk-import:" + seed).hex


def import_csv(store: RiskStore, text: str, *, created_by: str, dry_run: bool = False) -> dict[str, Any]:
    """Doğrular ve (hata yoksa, ``dry_run`` değilse) taslak olarak yazar."""
    rows = parse_csv(text)
    report: list[dict[str, Any]] = []
    prepared: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for line_no, raw in rows:
        entry: dict[str, Any] = {"line": line_no, "ok": False, "title": str(raw.get("title") or "").strip()[:80], "error": "", "duplicate": False}
        try:
            clean = _normalise_row(raw)
            if not clean["verified_at"]:
                raise RiskError("Son doğrulama tarihi zorunludur.")
            parse_verified_at(clean["verified_at"])
            store._validate(clean, official=True)  # noqa: SLF001 - tek doğrulama kaynağı
            identifier = row_identifier({**clean, "gtip_prefix": "".join(ch for ch in clean["gtip_prefix"] if ch.isdigit())})
            if identifier in seen:
                raise RiskError("Aynı kayıt dosyada birden fazla kez geçiyor.")
            seen.add(identifier)
            entry["ok"] = True
            entry["duplicate"] = store.exists(identifier)
            prepared.append((identifier, clean))
        except RiskError as exc:
            entry["error"] = str(exc)
        report.append(entry)
    errors = sum(1 for item in report if not item["ok"])
    result: dict[str, Any] = {
        "rows": report, "total": len(report), "errors": errors, "valid": len(report) - errors,
        "dry_run": dry_run, "imported": 0, "skipped_duplicates": 0, "committed": False,
    }
    if errors:
        result["message"] = "Dosyada hatalı satır var; hiçbir kayıt yazılmadı. Satırları düzeltip yeniden yükleyin."
        return result
    if dry_run:
        result["message"] = "Doğrulama başarılı; kayıt yazılmadı (deneme)."
        return result
    for identifier, clean in prepared:
        if store.exists(identifier):
            result["skipped_duplicates"] += 1
            continue
        store.add(clean, official=True, owner_sub=None, status="draft", created_by=created_by, identifier=identifier)
        result["imported"] += 1
    result["committed"] = True
    result["message"] = (
        f"{result['imported']} kayıt taslak olarak yazıldı; onaylanana kadar risk kontrolünde kullanılmaz."
        + (f" {result['skipped_duplicates']} kayıt zaten vardı." if result["skipped_duplicates"] else "")
    )
    return result
