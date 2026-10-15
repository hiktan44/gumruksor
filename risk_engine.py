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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

CATEGORIES = {
    "kiymet": "Kıymet",
    "mense": "Menşe",
    "tareks": "TAREKS / ürün güvenliği",
    "damping_kacagi": "Damping kaçağı",
    "beyan_uyumsuzlugu": "Beyan uyumsuzluğu",
}
#: ``emsal`` yalnız yargı kararlarından türetilen "dayanaklı emsal notu" için ayrılmıştır; resmî kriter değildir.
PRECEDENT_CATEGORY = "emsal"
DIRECTIONS = {"import", "export", "both"}
STATUSES = {"draft": "Taslak", "approved": "Onaylı", "rejected": "Reddedildi"}
#: Son doğrulama tarihi bu süreden eskiyse kayıt "eskimiş olabilir" uyarısı taşır.
STALE_AFTER_DAYS = 365
LEVELS = {"high": "Yüksek", "medium": "Orta", "info": "Bilgi", "none": "Kayıt yok", "unknown": "Belirsiz (kaynak eksik)"}
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
    updated_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'approved',
    kind TEXT NOT NULL DEFAULT 'criterion',
    verified_at TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    reviewed_by TEXT,
    reviewed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_risk_criteria_prefix ON risk_criteria(gtip_prefix);
"""
#: Eski veritabanlarına eklenecek sütunlar. Varsayılanlar mevcut satırları "onaylı kriter" bırakır.
MIGRATION_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'approved'",
    "kind": "TEXT NOT NULL DEFAULT 'criterion'",
    "verified_at": "TEXT",
    "review_note": "TEXT NOT NULL DEFAULT ''",
    "created_by": "TEXT",
    "reviewed_by": "TEXT",
    "reviewed_at": "REAL",
}


class RiskError(ValueError):
    """Geçersiz kriter veya sorgu girdisi."""


def normalise_gtip(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 2 or len(digits) > 12:
        raise RiskError("GTİP en az 2, en çok 12 haneli olmalıdır.")
    return digits


def parse_verified_at(value: Any) -> date | None:
    """``GG.AA.YYYY`` veya ``YYYY-AA-GG``; gelecekteki tarih ve geçersiz biçim reddedilir."""
    text = str(value or "").strip()
    if not text:
        return None
    parsed: date | None = None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(text, pattern).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise RiskError("Son doğrulama tarihi GG.AA.YYYY veya YYYY-AA-GG biçiminde olmalıdır.")
    if parsed > date.today():
        raise RiskError("Son doğrulama tarihi gelecekte olamaz.")
    if parsed.year < 2000:
        raise RiskError("Son doğrulama tarihi 2000 yılından önce olamaz.")
    return parsed


def staleness(verified_at: str | None, needs_date: bool, *, today: date | None = None) -> tuple[bool, str]:
    """Resmî/emsal kayıtlar için eskime durumu: tarih yoksa ya da 12 aydan eskiyse uyarı."""
    if not needs_date:
        return False, ""
    if not verified_at:
        return True, "Son doğrulama tarihi kayıtlı değil; güncelliğini doğrulayın."
    try:
        parsed = date.fromisoformat(verified_at)
    except ValueError:
        return True, "Son doğrulama tarihi okunamadı; güncelliğini doğrulayın."
    age = ((today or date.today()) - parsed).days
    if age > STALE_AFTER_DAYS:
        return True, f"Son doğrulama {parsed.strftime('%d.%m.%Y')} tarihinde yapıldı (12 aydan eski); güncelliğini doğrulayın."
    return False, ""


class RiskStore:
    def __init__(self, data_dir: str | Path, seed_path: str | Path | None = None) -> None:
        self.path = Path(data_dir) / "risk_criteria.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            existing = {row["name"] for row in db.execute("PRAGMA table_info(risk_criteria)").fetchall()}
            for column, definition in MIGRATION_COLUMNS.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE risk_criteria ADD COLUMN {column} {definition}")
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
                    "source_label,source_url,official,owner_sub,created_at,updated_at,verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,?)",
                    (str(item.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, record["title"] + record["gtip_prefix"])),
                     record["gtip_prefix"], record["direction"], record["year_from"], record["year_to"], record["category"],
                     record["title"], record["description"], record["source_label"], record["source_url"], time.time(), time.time(),
                     record["verified_at"]),
                )

    @staticmethod
    def _validate(payload: dict[str, Any], *, official: bool, precedent: bool = False) -> dict[str, Any]:
        category = str(payload.get("category", "")).strip()
        if precedent:
            if category not in ("", PRECEDENT_CATEGORY):
                raise RiskError("Emsal notunun kategorisi yalnız 'emsal' olabilir.")
            category = PRECEDENT_CATEGORY
        elif category not in CATEGORIES:
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
        if (official or precedent) and not (source_url and source_label):
            raise RiskError("Resmî kriter ve emsal notu için kaynak adı ve https kaynak bağlantısı zorunludur.")
        verified_at = parse_verified_at(payload.get("verified_at"))
        return {
            "verified_at": verified_at.isoformat() if verified_at else None,
            "gtip_prefix": normalise_gtip(payload.get("gtip_prefix")), "direction": direction,
            "year_from": years[0], "year_to": years[1], "category": category, "title": title,
            "description": description, "source_label": source_label, "source_url": source_url,
        }

    def add(
        self, payload: dict[str, Any], *, official: bool, owner_sub: str | None,
        status: str = "approved", created_by: str | None = None, identifier: str | None = None,
    ) -> dict[str, Any]:
        """Kayıt ekler. Kullanıcı notu ``owner_sub`` ile yalnız sahibine görünür.

        ``status='draft'`` kayıtlar onaylanana kadar risk kontrolüne **girmez**.
        """
        if status not in STATUSES:
            raise RiskError("Durum geçersiz.")
        record = self._validate(payload, official=official)
        if not official and not owner_sub:
            raise RiskError("Kullanıcı notu için giriş gerekir.")
        return self._insert(record, official=official, owner_sub=owner_sub, status=status, kind="criterion",
                            created_by=created_by, identifier=identifier)

    def add_precedent(
        self, payload: dict[str, Any], *, created_by: str | None, identifier: str | None = None, status: str = "draft",
    ) -> dict[str, Any]:
        """Yargı kararından türetilmiş "dayanaklı emsal notu". Resmî kriter DEĞİLDİR (``official=0``)."""
        record = self._validate(payload, official=False, precedent=True)
        return self._insert(record, official=False, owner_sub=None, status=status, kind="precedent",
                            created_by=created_by, identifier=identifier)

    def _insert(
        self, record: dict[str, Any], *, official: bool, owner_sub: str | None, status: str, kind: str,
        created_by: str | None, identifier: str | None,
    ) -> dict[str, Any]:
        identifier, now = identifier or uuid.uuid4().hex, time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO risk_criteria (id,gtip_prefix,direction,year_from,year_to,category,title,description,source_label,"
                "source_url,official,owner_sub,created_at,updated_at,status,kind,verified_at,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, record["gtip_prefix"], record["direction"], record["year_from"], record["year_to"], record["category"],
                 record["title"], record["description"], record["source_label"], record["source_url"], 1 if official else 0,
                 None if official else owner_sub, now, now, status, kind, record["verified_at"], created_by),
            )
        return self.get(identifier)  # type: ignore[return-value]

    def exists(self, identifier: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM risk_criteria WHERE id=?", (identifier,)).fetchone() is not None

    # ---- inceleme / onay
    def list_for_review(self, status: str = "draft", *, kind: str = "", stale_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        """Yöneticinin ve uzmanın görebildiği, kullanıcı notu olmayan kayıtlar."""
        if status not in STATUSES:
            raise RiskError("Durum geçersiz.")
        query = "SELECT * FROM risk_criteria WHERE status=? AND owner_sub IS NULL"
        params: list[Any] = [status]
        if kind in ("criterion", "precedent"):
            query += " AND kind=?"
            params.append(kind)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        items = [self._public(row) for row in rows]
        return [item for item in items if item["stale"]] if stale_only else items

    def review(
        self, identifier: str, *, approve: bool, reviewer_sub: str, note: str = "", verified_at: Any = None,
    ) -> dict[str, Any]:
        """Taslağı onaylar/reddeder ya da onaylı kaydın doğrulama tarihini yeniler.

        Onay için ``verified_at`` (kaydın ya da istekteki) zorunludur: tarihsiz kayıt yayımlanmaz.
        """
        with self._connect() as db:
            row = db.execute("SELECT * FROM risk_criteria WHERE id=? AND owner_sub IS NULL", (identifier,)).fetchone()
            if row is None:
                raise RiskError("Kayıt bulunamadı.")
            note = str(note or "").strip()[:500]
            if not approve:
                if not note:
                    raise RiskError("Reddetmek için gerekçe yazın.")
                db.execute("UPDATE risk_criteria SET status='rejected', review_note=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
                           (note, reviewer_sub, time.time(), time.time(), identifier))
            else:
                parsed = parse_verified_at(verified_at) if verified_at not in (None, "") else None
                chosen = parsed.isoformat() if parsed else row["verified_at"]
                if not chosen:
                    raise RiskError("Onay için 'son doğrulama tarihi' girilmelidir.")
                db.execute(
                    "UPDATE risk_criteria SET status='approved', verified_at=?, review_note=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
                    (chosen, note, reviewer_sub, time.time(), time.time(), identifier),
                )
        return self.get(identifier)  # type: ignore[return-value]

    def summary(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) n FROM risk_criteria WHERE owner_sub IS NULL GROUP BY status").fetchall()
        counts = {key: 0 for key in STATUSES}
        counts.update({row["status"]: row["n"] for row in rows})
        counts["stale"] = len([item for item in self.list_for_review("approved", limit=500) if item["stale"]])
        return counts

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
        kind = row["kind"] or "criterion"
        stale, stale_label = staleness(row["verified_at"], official or kind == "precedent")
        if kind == "precedent":
            origin_label = "Yargı emsali (resmî kriter değildir; bağlayıcı olmayabilir)"
        elif official:
            origin_label = "Resmî kaynaklı kriter"
        else:
            origin_label = "Kullanıcı notu (resmî değildir)"
        return {
            "id": row["id"], "gtip_prefix": row["gtip_prefix"], "direction": row["direction"],
            "year_from": row["year_from"], "year_to": row["year_to"], "category": row["category"],
            "category_label": CATEGORIES.get(row["category"], row["category"]), "title": row["title"],
            "description": row["description"], "source_label": row["source_label"], "source_url": row["source_url"],
            "official": official,
            # Resmî olmayan kayıt hiçbir zaman resmî kriter gibi sunulmaz.
            "origin_label": origin_label, "kind": kind, "precedent": kind == "precedent",
            "status": row["status"], "status_label": STATUSES.get(row["status"], row["status"]),
            "verified_at": row["verified_at"], "stale": stale, "stale_label": stale_label,
            "review_note": row["review_note"], "updated_at": row["updated_at"],
        }

    def match(self, gtip: str, direction: str, year: int, owner_sub: str | None) -> list[dict[str, Any]]:
        """Önek eşleşmesi: kaydın ön eki GTİP'in başıyla aynı olmalı; en uzun ön ek önce."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM risk_criteria WHERE ? LIKE gtip_prefix || '%' "
                "AND ((status='approved' AND (official=1 OR kind='precedent')) OR owner_sub=?) "
                "AND (direction='both' OR direction=?) AND (year_from IS NULL OR year_from<=?) AND (year_to IS NULL OR year_to>=?) "
                "ORDER BY kind='precedent', LENGTH(gtip_prefix) DESC, official DESC, title",
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
            unknown_origin = hit.get("origin_match") is None and not origin_given
            detail = " · ".join(part for part in (hit.get("product", ""), hit.get("country", ""), hit.get("rate_text", "")) if part)
            signals.append({
                "code": "safeguard", "level": "medium" if unknown_origin else "high",
                "category": "beyan_uyumsuzlugu",
                "title": "Korunma önlemi kapsamı" + (" (menşe doğrulanmalı)" if unknown_origin else ""),
                "detail": detail, "source": hit.get("legal_act") or hit.get("gazette") or "",
                "needs_input": "Menşe ülke" if unknown_origin else None,
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
            if hit.get("origin_match") is False:
                continue
            unknown_origin = hit.get("origin_match") is None and not origin_given
            detail = " · ".join(part for part in (hit.get("product", ""), hit.get("country", ""), hit.get("rate_text", "")) if part)
            signals.append({
                "code": "tariff_quota", "level": "info" if unknown_origin else "medium", "category": "mense",
                "title": "Tarife kontenjanı kapsamı" + (" (menşe doğrulanmalı)" if unknown_origin else ""),
                "detail": detail, "source": hit.get("legal_act") or hit.get("gazette") or "",
                "needs_input": "Menşe ülke" if unknown_origin else None,
            })
    if controls and controls.get("status") == "matched":
        for match in controls.get("matches", []):
            rule = match.get("rule", {})
            scope = match.get("matched_scope", {})
            if scope.get("excluded"):
                continue
            kind = scope.get("list_kind", "scope")
            title = rule.get("title", "Ürün güvenliği / TAREKS kontrolü")
            if kind == "prohibited":
                level, headline = "high", f"YASAK kapsamı: {title}"
            elif kind == "licence_required":
                level, headline = "medium", f"Ön izin / ruhsat gerekebilir: {title}"
            else:
                level, headline = "medium", f"{title} kapsamında olabilir"
            detail = " · ".join(part for part in (rule.get("authority", ""), rule.get("system", ""), match.get("assessment", "")) if part)
            signals.append({
                "code": "control_" + kind, "level": level, "category": "tareks", "title": headline,
                "detail": detail, "source": rule.get("code", ""), "needs_input": None,
            })
    return signals


def assess_level(signals: list[dict[str, Any]], criteria: list[dict[str, Any]]) -> str:
    """Sabit kural: yüksek sinyal varsa Yüksek; orta sinyal veya resmî kriter varsa Orta; yalnız not varsa Bilgi."""
    if any(item["level"] == "high" for item in signals):
        return "high"
    if any(item["level"] == "medium" for item in signals) or any(item["official"] and item.get("kind", "criterion") == "criterion" for item in criteria):
        return "medium"
    if criteria or signals:
        return "info"
    return "none"


def check(
    store: RiskStore, *, gtip: str, direction: str, year: int | None, owner_sub: str | None,
    trade: dict[str, Any] | None, controls: dict[str, Any] | None, origin_given: bool,
    source_notes: list[str] | None = None,
) -> dict[str, Any]:
    code = normalise_gtip(gtip)
    direction = "export" if str(direction).lower() == "export" else "import"
    year = year or date.today().year
    criteria = store.match(code, direction, year, owner_sub)
    signals = automatic_signals(trade=trade, controls=controls, origin_given=origin_given)
    level = assess_level(signals, criteria)
    upstream = [str(item) for item in ((trade or {}).get("warnings") or [])]
    upstream += [str(item) for item in ((controls or {}).get("warnings") or [])]
    incomplete = bool(source_notes) or bool(upstream) or (controls is not None and controls.get("status") == "unavailable")
    if incomplete and level == "none":
        level = "unknown"
    warnings = ["Karar desteğidir; gümrük idaresinin risk analizi ve seçiciliği burada belirlenmez."]
    if not any(item["official"] for item in criteria):
        warnings.append("Bu GTİP için kayıtlı resmî kaynaklı risk kriteri bulunmuyor; bu, riskin olmadığı anlamına gelmez.")
    stale_items = [item for item in criteria if item.get("stale")]
    if stale_items:
        warnings.append(
            f"{len(stale_items)} kayıt 12 aydan eski ya da doğrulama tarihsiz; kaynak bağlantısından güncelliğini doğrulayın."
        )
    if any(item.get("precedent") for item in criteria):
        warnings.append("Yargı emsali notları resmî risk kriteri değildir ve bağlayıcı olmayabilir; seviyeyi yükseltmez.")
    warnings.extend(upstream)
    warnings.extend(source_notes or [])
    return {
        "gtip": code, "direction": direction, "year": year, "level": level, "incomplete": incomplete, "level_label": LEVELS[level],
        "signals": signals, "criteria": criteria, "needs_input": [s["needs_input"] for s in signals if s.get("needs_input")],
        "warnings": warnings,
    }
