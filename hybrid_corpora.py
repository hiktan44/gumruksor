"""Hibrit indeks korpus besleyicileri (PRD Faz 3.1).

Her fonksiyon ``HybridIndex.upsert_documents`` için belge sözlükleri döndürür:
``{"id", "corpus", "title", "text", "gtip_codes", "source_url", "source_sha256", "snapshot_id"}``.
Kaynak dosya/tablo yoksa boş liste döner; hata sunucuyu durdurmaz. Belge metinleri
``HybridIndex`` tarafında ``sanitize_untrusted_context`` ile temizlenir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from hybrid_index import chunk_text, normalise_gtip

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
CORPUS_CONTROLS = "controls"
CORPUS_CLASSIFICATION = "eu_classification"
CORPUS_MEASURES = "trade_measures"
CORPUS_OFFICIAL_PAGES = "official_pages"
CORPUS_EXCISE = "excise_tax"
CORPUS_VAT = "vat_lists"
CORPUS_TARIFF = "tariff_descriptions"
PAGE_CHUNK_CHARS = 1200


def _sha(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _open(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def control_documents(control_engine: Any) -> list[dict[str, Any]]:
    """Aktif ve onaylı ÜGD tebliğlerinin Ek kapsam satırları."""
    db_path = Path(getattr(control_engine, "db_path", ""))
    connection = _open(db_path)
    if connection is None:
        return []
    docs: list[dict[str, Any]] = []
    try:
        rows = connection.execute(
            """
            SELECT s.snapshot_id, s.gtip_prefix, s.description, s.source_line, s.list_kind,
                   d.code, d.title, d.authority, d.system, d.source_url, d.document_sha256
            FROM control_scope s JOIN control_snapshots d ON d.id=s.snapshot_id
            WHERE d.active=1 AND s.excluded=0
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Kontrol kapsamı okunamadı: %s", exc)
        return []
    finally:
        connection.close()
    for row in rows:
        description = (row["description"] or "").strip() or (row["source_line"] or "").strip()
        if not description:
            continue
        docs.append(
            {
                "id": f"control:{row['snapshot_id']}:{row['gtip_prefix']}:{row['list_kind']}",
                "corpus": CORPUS_CONTROLS,
                "title": f"{row['code']} – {row['title']}",
                "text": f"{description} · {row['authority']} · {row['system']}",
                "gtip_codes": [row["gtip_prefix"]],
                "source_url": row["source_url"] or "",
                "source_sha256": _sha(row["document_sha256"], row["gtip_prefix"], description),
                "snapshot_id": row["snapshot_id"],
            }
        )
    return docs


def classification_documents(engine: Any, *, chunk_chars: int = PAGE_CHUNK_CHARS) -> list[dict[str, Any]]:
    """AB sınıflandırma tüzüğü sayfaları, ~1.200 karakterlik parçalar hâlinde."""
    db_path = Path(getattr(engine, "database_path", ""))
    connection = _open(db_path)
    if connection is None:
        return []
    docs: list[dict[str, Any]] = []
    try:
        active = connection.execute("SELECT id, source_url, archive_sha256 FROM snapshots WHERE active=1 LIMIT 1").fetchone()
        if not active:
            return []
        pages = connection.execute(
            "SELECT id, page_number, codes_json, content FROM pages WHERE snapshot_id=? ORDER BY page_number",
            (active["id"],),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Sınıflandırma sayfaları okunamadı: %s", exc)
        return []
    finally:
        connection.close()
    for page in pages:
        try:
            codes = [normalise_gtip(code) for code in json.loads(page["codes_json"] or "[]")]
        except ValueError:
            codes = []
        for index, chunk in enumerate(chunk_text(page["content"], size=chunk_chars), start=1):
            docs.append(
                {
                    "id": f"eu-classification:{page['id']}:{index}",
                    "corpus": CORPUS_CLASSIFICATION,
                    "title": f"AB sınıflandırma tüzükleri – sayfa {page['page_number']} ({index})",
                    "text": chunk,
                    "gtip_codes": [code for code in codes if code][:40],
                    "source_url": active["source_url"] or "",
                    "source_sha256": _sha(active["archive_sha256"], page["page_number"], chunk),
                    "snapshot_id": active["id"],
                }
            )
    return docs


def trade_measure_documents(engine: Any) -> list[dict[str, Any]]:
    """Damping/korunma/gözetim/kota satırlarının ürün tanımları."""
    try:
        from trade_measures import KIND_LABELS, _item_rows
    except Exception:  # pragma: no cover
        return []
    store = getattr(engine, "store", None)
    if store is None:
        return []
    docs: list[dict[str, Any]] = []
    for kind in ("anti_dumping", "safeguard", "surveillance", "tariff_quota"):
        try:
            payload = store.load(kind)
            meta = store.metadata(kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Önlem verisi okunamadı (%s): %s", kind, exc)
            continue
        if not payload:
            continue
        source_url = str(meta.get("source_url") or "")
        dataset_sha = str(meta.get("sha256") or "")
        label = KIND_LABELS.get(kind, kind)
        for row_key, entry in _item_rows(kind, payload).items():
            product = str(entry.get("product") or "").strip()
            summary = str(entry.get("summary") or "").strip()
            if not product and not summary:
                continue
            country = str(entry.get("country") or "").strip()
            text = " · ".join(part for part in (product, summary if summary != product else "", country) if part)
            docs.append(
                {
                    "id": f"measure:{kind}:{hashlib.sha1(row_key.encode('utf-8')).hexdigest()[:20]}",
                    "corpus": CORPUS_MEASURES,
                    "title": f"{label}: {product or summary[:80]}",
                    "text": text,
                    "gtip_codes": list(entry.get("codes") or []),
                    "source_url": source_url,
                    "source_sha256": _sha(dataset_sha, row_key, text),
                    "snapshot_id": kind,
                }
            )
    return docs


def official_page_documents(path: str | Path | None = None) -> list[dict[str, Any]]:
    """``customs_sources.json`` içindeki resmî sayfa başlıkları."""
    file_path = Path(path or ROOT / "customs_sources.json")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    docs: list[dict[str, Any]] = []
    for source in payload.get("sources", []) if isinstance(payload, dict) else []:
        source_id = str(source.get("id") or "").strip()
        title = str(source.get("title") or "").strip()
        if not source_id or not title:
            continue
        text = " · ".join(part for part in (title, str(source.get("authority") or ""), str(source.get("summary") or "")) if part)
        docs.append(
            {
                "id": f"official-page:{source_id}",
                "corpus": CORPUS_OFFICIAL_PAGES,
                "title": title,
                "text": text,
                "gtip_codes": [],
                "source_url": str(source.get("url") or ""),
                "source_sha256": _sha(source),
                "snapshot_id": "customs_sources",
            }
        )
    return docs


def excise_documents(index: Any) -> list[dict[str, Any]]:
    """ÖTV (I)-(IV) sayılı liste satırları."""
    entries = list(getattr(index, "_entries", []) or [])
    if not entries:
        return []
    source_url = str((index.status() or {}).get("source_url") or "") if hasattr(index, "status") else ""
    docs: list[dict[str, Any]] = []
    for entry in entries:
        description = str(entry.get("description") or "").strip()
        code = str(entry.get("code") or "")
        if not description or not code:
            continue
        label = f"ÖTV ({entry.get('list')}) sayılı liste" + (f" {entry.get('cetvel')} cetveli" if entry.get("cetvel") else "")
        values = ", ".join(f"{key}: {value}" for key, value in (entry.get("values") or {}).items())
        docs.append(
            {
                "id": f"excise:{entry.get('list')}:{entry.get('cetvel') or '-'}:{code}",
                "corpus": CORPUS_EXCISE,
                "title": f"{label} – {entry.get('raw_code') or code}",
                "text": f"{description}" + (f" · {values}" if values else ""),
                "gtip_codes": [code],
                "source_url": source_url,
                "source_sha256": _sha(entry),
                "snapshot_id": "excise_tax_lists",
            }
        )
    return docs


def vat_documents(index: Any) -> list[dict[str, Any]]:
    """KDV (I)/(II) sayılı liste satırları (2007/13033 eki)."""
    rows = list(getattr(index, "_rows", []) or [])
    if not rows:
        return []
    source_url = str((index.status() or {}).get("source_url") or "") if hasattr(index, "status") else ""
    docs: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        codes = [normalise_gtip(expr) for expr in (row.get("gtip_expressions") or [])]
        row_id = f"{row.get('list')}:{row.get('section') or '-'}:{row.get('row_no') or '-'}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}"
        rate = row.get("rate")
        docs.append(
            {
                "id": f"vat:{row_id}",
                "corpus": CORPUS_VAT,
                "title": f"KDV ({row.get('list')}) sayılı liste" + (f" %{rate}" if rate not in (None, "") else ""),
                "text": text + (f" · {row.get('legal_basis')}" if row.get("legal_basis") else ""),
                "gtip_codes": [code for code in codes if code],
                "source_url": source_url,
                "source_sha256": _sha(row),
                "snapshot_id": "vat_lists",
            }
        )
    return docs


def tariff_description_documents(engine: Any, *, limit: int = 20_000) -> list[dict[str, Any]]:
    """Tarife cetveli ölçü satırlarındaki eşya tanımları (varsa; nomenklatür yerine geçici kaynak)."""
    db_path = Path(getattr(engine, "db_path", ""))
    connection = _open(db_path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT m.gtip, MIN(m.description) AS description, MIN(m.snapshot_id) AS snapshot_id
            FROM tariff_measures m JOIN tariff_snapshots s ON s.id=m.snapshot_id
            WHERE s.active=1 AND m.description IS NOT NULL AND m.description != ''
            GROUP BY m.gtip ORDER BY m.gtip LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Tarife tanımları okunamadı: %s", exc)
        return []
    finally:
        connection.close()
    docs: list[dict[str, Any]] = []
    for row in rows:
        description = str(row["description"] or "").strip()
        if not description or description.isdigit():
            continue
        docs.append(
            {
                "id": f"tariff:{row['gtip']}",
                "corpus": CORPUS_TARIFF,
                "title": f"GTİP {row['gtip']}",
                "text": description,
                "gtip_codes": [row["gtip"]],
                "source_url": "",
                "source_sha256": _sha(row["gtip"], description),
                "snapshot_id": row["snapshot_id"] or "",
            }
        )
    return docs


def collect_all(
    *,
    control_engine: Any = None,
    classification_engine: Any = None,
    trade_measure_engine: Any = None,
    excise_index: Any = None,
    vat_index: Any = None,
    tariff_engine: Any = None,
    sources_path: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Tüm korpusları toplar; tek bir kaynak hatası diğerlerini engellemez."""
    feeders: list[tuple[str, Any]] = [
        (CORPUS_CONTROLS, lambda: control_documents(control_engine) if control_engine is not None else []),
        (CORPUS_CLASSIFICATION, lambda: classification_documents(classification_engine) if classification_engine is not None else []),
        (CORPUS_MEASURES, lambda: trade_measure_documents(trade_measure_engine) if trade_measure_engine is not None else []),
        (CORPUS_OFFICIAL_PAGES, lambda: official_page_documents(sources_path)),
        (CORPUS_EXCISE, lambda: excise_documents(excise_index) if excise_index is not None else []),
        (CORPUS_VAT, lambda: vat_documents(vat_index) if vat_index is not None else []),
        (CORPUS_TARIFF, lambda: tariff_description_documents(tariff_engine) if tariff_engine is not None else []),
    ]
    result: dict[str, list[dict[str, Any]]] = {}
    for corpus, feeder in feeders:
        try:
            result[corpus] = feeder()
        except Exception:  # noqa: BLE001
            logger.exception("Korpus toplanamadı: %s", corpus)
            result[corpus] = []
    return result


__all__ = [
    "collect_all",
    "control_documents",
    "classification_documents",
    "trade_measure_documents",
    "official_page_documents",
    "excise_documents",
    "vat_documents",
    "tariff_description_documents",
]
