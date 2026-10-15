#!/usr/bin/env python3
"""Yargı kararlarından risk için "dayanaklı emsal notu" TASLAĞI içe aktarır.

Kullanım (depo kökünden)::

    uv run python scripts/emsal_notu_aktar.py --ictihat-dir data --risk-dir data --tur kiymet,tarife --limit 50
    uv run python scripts/emsal_notu_aktar.py --ictihat-dir data --dry-run

Kaynak, uygulamanın ``ictihat`` arşividir (``ictihat.sqlite3``; Bedesten'den zaten indirilmiş
Danıştay kararları, tam metin mevzuat.adalet.gov.tr/ictihat/<documentId>). Betik ağa çıkmaz.

Bu araç **resmî risk kriteri üretmez**:

* Kayıtlar ``kind='precedent'``, kategori ``emsal``, ``official=0`` ve ``draft`` olarak yazılır.
  Onaylansalar bile "Yargı emsali (resmî kriter değildir; bağlayıcı olmayabilir)" etiketi taşırlar
  ve risk seviyesini yükseltmezler.
* Açıklama yalnız kararın künyesini, konu etiketlerini ve karar metninde geçen GTİP kodunu
  söyler; kararın sonucu yorumlanmaz (yorum uydurmamak için). Sonuç için karar metni okunur.
* Karar metninde GTİP kodu yoksa karar atlanır (GTİP'e bağlanamaz).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from risk_engine import RiskError, RiskStore  # noqa: E402
from turkish_text import fold  # noqa: E402

#: Hedeflenen uyuşmazlık türleri: konu etiketlerinde aranan katlanmış ifade -> etiket.
KINDS = {"kiymet": ("kıymet", "gümrük kıymeti"), "tarife": ("tarife", "tarife istatistik pozisyonu")}
KIND_LABELS = {"kiymet": "gümrük kıymeti", "tarife": "tarife sınıflandırması"}
DISCLAIMER = "Emsal karardır, bağlayıcı olmayabilir; resmî risk kriteri değildir. Sonucu için karar metnini okuyun."


def classify(konular: list[str], kinds: list[str]) -> str:
    folded = [fold(item) for item in konular]
    for kind in kinds:
        if any(fold(term) in label for term in KINDS[kind] for label in folded):
            return kind
    return ""


def select_decisions(archive_dir: Path, kinds: list[str], limit: int) -> list[dict[str, Any]]:
    path = Path(archive_dir) / "ictihat.sqlite3"
    if not path.exists():
        raise FileNotFoundError(f"İçtihat arşivi bulunamadı: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT * FROM decisions WHERE duplicate_of='' AND text_quality<>'unreadable' ORDER BY karar_tarihi DESC"
        ).fetchall()
        picked: list[dict[str, Any]] = []
        for row in rows:
            konular = json.loads(row["konular"] or "[]")
            kind = classify(konular, kinds)
            if not kind:
                continue
            prefixes = sorted({code[:4] for code in json.loads(row["gtip_json"] or "[]") if len(code) >= 4 and code[:4].isdigit()})
            if not prefixes or not row["esas_no"] or not row["karar_no"]:
                continue
            picked.append({**dict(row), "kind": kind, "konular_list": konular, "prefixes": prefixes})
            if len(picked) >= limit:
                break
        return picked
    finally:
        db.close()


def to_payloads(decision: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Karar başına GTİP 4 haneli ön ek başına bir taslak (kimlik kararlıdır, yeniden çalıştırma çoğaltmaz)."""
    label = f"{decision['birim'] or 'Danıştay'}, E.{decision['esas_no']}, K.{decision['karar_no']}"
    if decision["karar_tarihi"]:
        label += f", T.{decision['karar_tarihi']}"
    items = []
    for prefix in decision["prefixes"]:
        codes = [code for code in json.loads(decision["gtip_json"] or "[]") if code.startswith(prefix)]
        description = (
            f"{KIND_LABELS[decision['kind']].capitalize()} uyuşmazlığı ile ilgili karar. Karar metninde geçen GTİP: "
            f"{', '.join(codes[:5])}. Konu etiketleri: {', '.join(decision['konular_list'][:6]) or 'yok'}. {DISCLAIMER}"
        )
        payload = {
            "gtip_prefix": prefix, "direction": "both", "category": "emsal",
            "title": f"Emsal karar: {KIND_LABELS[decision['kind']]} ({label})"[:200],
            "description": description[:2000], "source_label": f"{label} (mevzuat.adalet.gov.tr içtihat)",
            "source_url": decision["url"],
        }
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"emsal:{decision['document_id']}:{prefix}").hex
        items.append((identifier, payload))
    return items


def run(archive_dir: Path, risk_dir: Path, kinds: list[str], limit: int, *, dry_run: bool = False) -> dict[str, Any]:
    store = None if dry_run else RiskStore(risk_dir)
    summary = {"decisions": 0, "created": 0, "existing": 0, "errors": [], "dry_run": dry_run}
    for decision in select_decisions(archive_dir, kinds, limit):
        summary["decisions"] += 1
        for identifier, payload in to_payloads(decision):
            if store is None:
                RiskStore._validate(payload, official=False, precedent=True)  # noqa: SLF001
                summary["created"] += 1
                continue
            if store.exists(identifier):
                summary["existing"] += 1
                continue
            try:
                store.add_precedent(payload, created_by="script:emsal_notu_aktar", identifier=identifier)
                summary["created"] += 1
            except RiskError as exc:
                summary["errors"].append(f"{decision['document_id']}: {exc}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yargı kararlarından emsal notu taslağı içe aktarır (resmî kriter değildir).")
    parser.add_argument("--ictihat-dir", type=Path, required=True, help="ictihat.sqlite3 dizini")
    parser.add_argument("--risk-dir", type=Path, default=Path("data"), help="risk_criteria.sqlite3 dizini")
    parser.add_argument("--tur", default="kiymet,tarife", help="Virgülle: " + ", ".join(KINDS))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    kinds = [item.strip() for item in args.tur.split(",") if item.strip()]
    if not kinds or any(item not in KINDS for item in kinds):
        print("--tur yalnız " + ", ".join(KINDS) + " olabilir.", file=sys.stderr)
        return 2
    summary = run(args.ictihat_dir, args.risk_dir, kinds, max(1, args.limit), dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
