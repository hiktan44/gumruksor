"""Beyanname öncesi risk ön kontrolü.

İki kaynak birleştirilir:

* ``risk_criteria``: GTİP önekine bağlı kriter kayıtları. Resmî kayıt yalnız yönetici
  tarafından, kaynak bağlantısıyla eklenebilir; kullanıcı kayıtları her zaman
  "kullanıcı notu" etiketi taşır ve yalnız sahibine görünür.
* Otomatik sinyaller: damping, korunma, gözetim, tarife kontenjanı ve TAREKS/TSE kapsamı
  mevcut motorların çıktısından türetilir; yeni oran veya kural uydurulmaz.

Seviye kuralı sabit ve açıktır (``assess_level``). Sonuç karar desteğidir; gümrük
idaresinin risk analizinin yerine geçmez.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

CATEGORIES = {
    "kiymet": "Kıymet",
    "mense": "Menşe",
    "tareks": "TAREKS / ürün güvenliği",
    "damping_kacagi": "Damping kaçağı",
    "beyan_uyumsuzlugu": "Beyan uyumsuzluğu",
}
DIRECTIONS = {"import", "export", "both"}
LEVELS = {"high": "Yüksek", "medium": "Orta", "info": "Bilgi", "none": "Kayıt yok"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_criteria (
    id TEXT PRIMARY KEY,
    gtip_prefix TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'both',
    year_from INTEGER,
    year_to INTEGER,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_label TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    official INTEGER NOT NULL DEFAULT 0,
    owner_sub TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_criteria_prefix ON risk_criteria(gtip_prefix);
"""


class RiskError(ValueError):
    """Geçersiz kriter veya sorgu girdisi."""


def normalise_gtip(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 2 or len(digits) > 12:
        raise RiskError("GTİP en az 2, en çok 12 haneli olmalıdır.")
    return digits


class RiskStore:
    def __init__(self, data_dir: str | Path, seed_path: str | Path | None = None) -> None:
        self.path = Path(data_dir) / "risk_criteria.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
        if seed_path:
            self._seed(Path(seed_path))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _seed(self, path: Path) -> None:
        """Resmî tohum kayıtları: kimliği sabit, kaynağı zorunlu; yinelenen yükleme tekrar yazmaz."""
        import json

        if not path.exists():
            return
        for item in json.loads(path.read_text(encoding="utf-8")).get("criteria", []):
            record = self._validate(item, official=True)
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO risk_criteria (id,gtip_prefix,direction,year_from,year_to,category,title,description,"
                    "source_label,source_url,official,owner_sub,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL,?,?)",
                    (str(item.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, record["title"] + record["gtip_prefix"])),
                     record["gtip_prefix"], record["direction"], record["year_from"], record["year_to"], record["category"],
                     record["title"], record["description"], record["source_label"], record["source_url"], time.time(), time.time()),
                )

    @staticmethod
    def _validate(payload: dict[str, Any], *, official: bool) -> dict[str, Any]:
        category = str(payload.get("category", "")).strip()
        if category not in CATEGORIES:
            raise RiskError("Kategori geçersiz: " + ", ".join(CATEGORIES))
        direction = str(payload.get("direction") or "both").strip().lower()
        if direction not in DIRECTIONS:
            raise RiskError("Yön import, export veya both olmalıdır.")
        title = str(payload.get("title", "")).strip()
        if not title or len(title) > 200:
            raise RiskError("Başlık zorunludur (en çok 200 karakter).")
        description = str(payload.get("description", "")).strip()
        if len(description) > 2000:
            raise RiskError("Açıklama en çok 2000 karakter olabilir.")
        years: list[int | None] = []
        for key in ("year_from", "year_to"):
            raw = payload.get(key)
            if raw in (None, ""):
                years.append(None)
                continue
            try:
                year = int(raw)
            except (TypeError, ValueError) as exc:
                raise RiskError("Yıl sayı olmalıdır.") from exc
            if not 1990 <= year <= 2100:
                raise RiskError("Yıl 1990-2100 arasında olmalıdır.")
            years.append(year)
        if years[0] is not None and years[1] is not None and years[0] > years[1]:
            raise RiskError("Başlangıç yılı bitiş yılından büyük olamaz.")
        source_url = str(payload.get("source_url", "")).strip()
        source_label = str(payload.get("source_label", "")).strip()
        if source_url and not re.match(r"^https://", source_url):
            raise RiskError("Kaynak bağlantısı https:// ile başlamalıdır.")
        if official and not (source_url and source_label):
            raise RiskError("Resmî kriter için kaynak adı ve https kaynak bağlantısı zorunludur.")
        return {
            "gtip_prefix": normalise_gtip(payload.get("gtip_prefix")), "direction": direction,
            "year_from": years[0], "year_to": years[1], "category": category, "title": title,
            "description": description, "source_label": source_label, "source_url": source_url,
        }

    def add(self, payload: dict[str, Any], *, official: bool, owner_sub: str | None) -> dict[str, Any]:
        record = self._validate(payload, official=official)
        if not official and not owner_sub:
            raise RiskError("Kullanıcı notu için giriş gerekir.")
        identifier, now = uuid.uuid4().hex, time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO risk_criteria (id,gtip_prefix,direction,year_from,year_to,category,title,description,source_label,"
                "source_url,official,owner_sub,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, record["gtip_prefix"], record["direction"], record["year_from"], record["year_to"], record["category"],
                 record["title"], record["description"], record["source_label"], record["source_url"], 1 if official else 0,
                 None if official else owner_sub, now, now),
            )
        return self.get(identifier)  # type: ignore[return-value]

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM risk_criteria WHERE id=?", (identifier,)).fetchone()
        return self._public(row) if row else None

    def delete_note(self, identifier: str, owner_sub: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM risk_criteria WHERE id=? AND official=0 AND owner_sub=?", (identifier, owner_sub))
        return cursor.rowcount > 0

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        official = bool(row["official"])
        return {
            "id": row["id"], "gtip_prefix": row["gtip_prefix"], "direction": row["direction"],
            "year_from": row["year_from"], "year_to": row["year_to"], "category": row["category"],
            "category_label": CATEGORIES.get(row["category"], row["category"]), "title": row["title"],
            "description": row["description"], "source_label": row["source_label"], "source_url": row["source_url"],
            "official": official,
            # Resmî olmayan kayıt hiçbir zaman resmî kriter gibi sunulmaz.
            "origin_label": "Resmî kaynaklı kriter" if official else "Kullanıcı notu (resmî değildir)",
            "updated_at": row["updated_at"],
        }

    def match(self, gtip: str, direction: str, year: int, owner_sub: str | None) -> list[dict[str, Any]]:
        """Önek eşleşmesi: kaydın ön eki GTİP'in başıyla aynı olmalı; en uzun ön ek önce."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM risk_criteria WHERE ? LIKE gtip_prefix || '%' AND (official=1 OR owner_sub=?) "
                "AND (direction='both' OR direction=?) AND (year_from IS NULL OR year_from<=?) AND (year_to IS NULL OR year_to>=?) "
                "ORDER BY LENGTH(gtip_prefix) DESC, official DESC, title",
                (gtip, owner_sub or "", direction, year, year),
            ).fetchall()
        return [self._public(row) for row in rows]


def automatic_signals(
    *, trade: dict[str, Any] | None, controls: dict[str, Any] | None, origin_given: bool
) -> list[dict[str, Any]]:
    """Mevcut motor çıktılarından sinyal üretir; hiçbir oran veya kural eklemez."""
    signals: list[dict[str, Any]] = []
    if trade:
        for hit in trade.get("anti_dumping", []):
            if hit.get("status") == "expired" or hit.get("origin_match") is False:
                continue
            certain = hit.get("origin_match") is True
            signals.append({
                "code": "anti_dumping", "level": "high" if certain else "medium", "category": "damping_kacagi",
                "title": "Damping / sübvansiyon önlemi kapsamı" + ("" if certain else " (menşe doğrulanmalı)"),
                "detail": f"{hit.get('product', '')} · {hit.get('country', '')} · {hit.get('rate_text', '')}".strip(" ·"),
                "source": hit.get("legal_act") or hit.get("gazette") or "", "needs_input": None if origin_given or certain else "Menşe ülke",
            })
        for hit in trade.get("safeguard", []):
            if hit.get("status") == "expired" or hit.get("origin_match") is False:
                continue
            signals.append({
                "code": "safeguard", "level": "high", "category": "beyan_uyumsuzlugu", "title": "Korunma önlemi kapsamı",
                "detail": f"{hit.get('product', '')} · {hit.get('rate_text', '')}".strip(" ·"),
                "source": hit.get("legal_act") or hit.get("gazette") or "", "needs_input": None,
            })
        for hit in trade.get("surveillance", []):
            if hit.get("status") == "expired":
                continue
            signals.append({
                "code": "surveillance", "level": "medium", "category": "kiymet", "title": "Gözetim (birim kıymet eşiği) kapsamı",
                "detail": f"{hit.get('product', '')} · eşik {hit.get('unit_value_usd')} USD/{hit.get('unit') or 'birim'}",
                "source": hit.get("legal_act") or hit.get("gazette") or "", "needs_input": None,
            })
        for hit in trade.get("tariff_quota", []):
            signals.append({
                "code": "tariff_quota", "level": "medium", "category": "mense", "title": "Tarife kontenjanı kapsamı",
                "detail": f"{hit.get('product', '')} · {hit.get('country', '')} · {hit.get('rate_text', '')}".strip(" ·"),
                "source": hit.get("legal_act") or hit.get("gazette") or "", "needs_input": None,
            })
    if controls and controls.get("status") == "matched":
        for match in controls.get("matches", []):
            rule = match.get("rule", {})
            signals.append({
                "code": "control", "level": "medium", "category": "tareks",
                "title": f"{rule.get('title', 'Ürün güvenliği / TAREKS kontrolü')} kapsamında olabilir",
                "detail": f"{rule.get('authority', '')} · {rule.get('system', '')}".strip(" ·"),
                "source": rule.get("code", ""), "needs_input": None,
            })
    return signals


def assess_level(signals: list[dict[str, Any]], criteria: list[dict[str, Any]]) -> str:
    """Sabit kural: yüksek sinyal varsa Yüksek; orta sinyal veya resmî kriter varsa Orta; yalnız not varsa Bilgi."""
    if any(item["level"] == "high" for item in signals):
        return "high"
    if any(item["level"] == "medium" for item in signals) or any(item["official"] for item in criteria):
        return "medium"
    if criteria or signals:
        return "info"
    return "none"


def check(
    store: RiskStore, *, gtip: str, direction: str, year: int | None, owner_sub: str | None,
    trade: dict[str, Any] | None, controls: dict[str, Any] | None, origin_given: bool,
) -> dict[str, Any]:
    code = normalise_gtip(gtip)
    direction = "export" if str(direction).lower() == "export" else "import"
    year = year or date.today().year
    criteria = store.match(code, direction, year, owner_sub)
    signals = automatic_signals(trade=trade, controls=controls, origin_given=origin_given)
    level = assess_level(signals, criteria)
    warnings = ["Karar desteğidir; gümrük idaresinin risk analizi ve seçiciliği burada belirlenmez."]
    if not any(item["official"] for item in criteria):
        warnings.append("Bu GTİP için kayıtlı resmî kaynaklı risk kriteri bulunmuyor; bu, riskin olmadığı anlamına gelmez.")
    return {
        "gtip": code, "direction": direction, "year": year, "level": level, "level_label": LEVELS[level],
        "signals": signals, "criteria": criteria, "needs_input": [s["needs_input"] for s in signals if s.get("needs_input")],
        "warnings": warnings,
    }
