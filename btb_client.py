"""Türkiye Bağlayıcı Tarife Bilgisi (BTB) kararları: resmî "Geçerli Kayıtlar" listesi.

Kaynak: Ticaret Bakanlığı BTB Başvuru Sistemi (``uygulama.gtb.gov.tr/BTBBasvuru``), halka açık
sorgulama ekranları. Sayfalar ASP.NET WebForms olduğu için sayfalama ve ayrıntı ``__doPostBack``
ile yürür; bu modül yalnız bu iki açık ekranı, düşük hızla (varsayılan 1 istek/sn) okur.

Ölçülen sınırlar (15 Ekim 2026):

* Liste ekranı yalnız **geçerli** kayıtları verir (BTB no, GTİP, kısaltılmış tanım, geçerlilik
  başlangıcı). Tanım listede ~25 karakterde "..." ile kesilir.
* Tam tanım ve **sınıflandırma gerekçesi** ayrıntı ekranındadır; her kayıt için ayrı istek
  gerektiği için ayrıntı yalnız talep üzerinde okunur ve depoda saklanır.
* Fotoğraf adresi ``grdpht.ashx?ImID=<BTB no>`` kalıbındadır; bu modül yalnız adresi üretir,
  görsel dosyası indirmez veya saklamaz.

Bir kayıt art arda iki tam eşitlemede listede görünmezse pasif sayılır (geçerliliği bitmiş veya
iptal edilmiş olabilir); kayıt silinmez.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from turkish_text import fold

logger = logging.getLogger(__name__)

BASE = "https://uygulama.gtb.gov.tr/BTBBasvuru/"
LIST_URL = BASE + "Btbler"
SEARCH_URL = BASE + "BtbWebArama"
PHOTO_URL = BASE + "grdpht.ashx?ImID={btb_no}"
USER_AGENT = "Mozilla/5.0 (compatible; GumrukSor-research; +https://gumruksor.com)"
GRID = "ctl00$ContentPlaceHolder1$GridView1"
MAX_PAGES = 1500
SYNC_INTERVAL = 7 * 24 * 3600

_ROW = re.compile(
    r"<td[^>]*>\s*<a[^>]*>\s*(TR\d{10,16})\s*</a>\s*</td>\s*<td[^>]*>\s*(\d{4,12})\s*</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*<td[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</td>",
    re.S,
)
_HIDDEN = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')
_BTB_NO = re.compile(r"^TR\d{10,16}$")


class BtbError(RuntimeError):
    """Kaynak okunamadı veya sayfa yapısı beklenenden farklı."""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _iso(value: str) -> str:
    return datetime.strptime(value, "%d/%m/%Y").date().isoformat()


def hidden_fields(page: str) -> dict[str, str]:
    return {name: html.unescape(value) for name, value in _HIDDEN.findall(page)}


def parse_list(page: str) -> list[dict[str, str]]:
    """Liste ekranındaki satırlar. Beklenen yapı yoksa boş döner (çağıran bunu hata sayar)."""
    rows = []
    for btb_no, gtip, excerpt, valid_from in _ROW.findall(page):
        rows.append({
            "btb_no": btb_no, "gtip": gtip, "description_excerpt": _clean(excerpt), "valid_from": _iso(valid_from),
        })
    return rows


def has_next_page(page: str, current: int) -> bool:
    """Pager'da ``Page$<current+1>`` bağlantısı var mı (sayı veya "..." bağlantısı olarak)."""
    return f"Page${current + 1}" in html.unescape(page)


def parse_detail(page: str) -> dict[str, str] | None:
    """Ayrıntı panelinden tam tanım ve sınıflandırma gerekçesi."""
    text = _clean(page)
    marker = text.find("BAĞLAYICI TARİFE BİLGİSİ DETAY BİLGİSİ")
    if marker < 0:
        return None
    block = text[marker:]
    reasoning = re.search(r"Sınıflandırmanın Gerekçesi:\s*(.*?)\s*Eşyanın Tanımı:", block)
    description = re.search(r"Eşyanın Tanımı:\s*(.*?)(?:\s+Sayfa En iyi|\s+//|$)", block)
    if not (reasoning and description):
        return None
    return {"reasoning": reasoning.group(1).strip(), "description_full": description.group(1).strip()}


def photo_url(btb_no: str) -> str:
    if not _BTB_NO.match(btb_no):
        raise ValueError("Geçersiz BTB numarası.")
    return PHOTO_URL.format(btb_no=btb_no)


class BtbStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / "btb_rulings.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS btb_rulings (
                    btb_no TEXT PRIMARY KEY, gtip TEXT NOT NULL, description_excerpt TEXT NOT NULL DEFAULT '',
                    description_full TEXT NOT NULL DEFAULT '', reasoning TEXT NOT NULL DEFAULT '',
                    valid_from TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                    missed_syncs INTEGER NOT NULL DEFAULT 0, detail_fetched_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_btb_gtip ON btb_rulings(gtip);
                CREATE TABLE IF NOT EXISTS btb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO btb_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM btb_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def apply_full_sync(self, rows: list[dict[str, str]], *, now: float | None = None) -> dict[str, int]:
        """Tam liste okunduktan sonra çağrılır: görülenler tazelenir, görülmeyenin kaçırma sayacı artar."""
        now = now or time.time()
        added = 0
        seen = {row["btb_no"] for row in rows}
        with self._connect() as db:
            for row in rows:
                existing = db.execute("SELECT 1 FROM btb_rulings WHERE btb_no=?", (row["btb_no"],)).fetchone()
                if existing:
                    db.execute(
                        "UPDATE btb_rulings SET gtip=?, description_excerpt=?, valid_from=?, last_seen=?, missed_syncs=0 WHERE btb_no=?",
                        (row["gtip"], row["description_excerpt"], row["valid_from"], now, row["btb_no"]),
                    )
                else:
                    added += 1
                    db.execute(
                        "INSERT INTO btb_rulings (btb_no,gtip,description_excerpt,valid_from,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                        (row["btb_no"], row["gtip"], row["description_excerpt"], row["valid_from"], now, now),
                    )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                db.execute(f"UPDATE btb_rulings SET missed_syncs=missed_syncs+1 WHERE btb_no NOT IN ({placeholders})", tuple(seen))
        return {"seen": len(seen), "added": added}

    def save_detail(self, btb_no: str, detail: dict[str, str]) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE btb_rulings SET description_full=?, reasoning=?, detail_fetched_at=? WHERE btb_no=?",
                (detail["description_full"], detail["reasoning"], time.time(), btb_no),
            )

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        full = row["description_full"]
        return {
            "btb_no": row["btb_no"], "gtip": row["gtip"], "valid_from": row["valid_from"],
            "description": full or row["description_excerpt"],
            "description_is_excerpt": not full, "reasoning": row["reasoning"] or None,
            "active": row["missed_syncs"] < 2, "photo_url": photo_url(row["btb_no"]),
            "source_url": SEARCH_URL, "source_label": "T.C. Ticaret Bakanlığı BTB Başvuru Sistemi",
        }

    def get(self, btb_no: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM btb_rulings WHERE btb_no=?", (btb_no,)).fetchone()
        return self._public(row) if row else None

    def search(self, query: str = "", gtip_prefix: str = "", *, limit: int = 20) -> list[dict[str, Any]]:
        """GTİP öneki SQL'de, sözcük eşleşmesi Türkçe duyarlı ``fold`` ile bellekte yapılır.

        SQLite ``LOWER()`` yalnız ASCII'yi küçültür; "Şeker"/"İPEK" gibi büyük harfli resmî tanımlar
        bu yüzden kaçardı. Geçerli kayıt sayısı birkaç bin olduğundan bellek içi süzme yeterlidir.
        """
        limit = max(1, min(int(limit), 50))
        digits = re.sub(r"\D", "", gtip_prefix or "")
        terms = [t for t in re.findall(r"\w{3,}", fold(query or "")) if not t.isdigit()][:8]
        if not digits and not terms:
            return []
        sql, params = "SELECT * FROM btb_rulings WHERE missed_syncs < 2", []  # type: str, list[Any]
        if digits:
            sql += " AND gtip LIKE ?"
            params.append(digits + "%")
        sql += " ORDER BY valid_from DESC"
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        matches = []
        for row in rows:
            haystack = fold(f"{row['description_excerpt']} {row['description_full']} {row['reasoning']}")
            if all(term in haystack for term in terms):
                matches.append(self._public(row))
                if len(matches) >= limit:
                    break
        return matches

    def by_gtip(self, gtip: str, *, limit: int = 6) -> list[dict[str, Any]]:
        """Aynı GTİP'in kayıtları önce, sonra 10, 8 ve 6 haneli ön ek eşleşmeleri; her kayıt bir kez, en yeni önce."""
        digits = re.sub(r"\D", "", gtip or "")
        if len(digits) < 6:
            return []
        limit = max(1, min(int(limit), 12))
        chosen: dict[str, tuple[int, sqlite3.Row]] = {}
        with self._connect() as db:
            for length in (len(digits[:12]), 10, 8, 6):
                prefix = digits[:length]
                if len(prefix) < length:
                    continue
                for row in db.execute(
                    "SELECT * FROM btb_rulings WHERE missed_syncs < 2 AND gtip LIKE ? ORDER BY valid_from DESC LIMIT ?",
                    (prefix + "%", limit),
                ).fetchall():
                    chosen.setdefault(row["btb_no"], (length, row))
                if len(chosen) >= limit:
                    break
        ordered = sorted(chosen.values(), key=lambda item: (-item[0], -int(item[1]["valid_from"].replace("-", ""))))
        return [{**self._public(row), "match_digits": length} for length, row in ordered[:limit]]

    def status(self) -> dict[str, Any]:
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM btb_rulings").fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM btb_rulings WHERE missed_syncs<2").fetchone()[0]
            detailed = db.execute("SELECT COUNT(*) FROM btb_rulings WHERE detail_fetched_at IS NOT NULL").fetchone()[0]
        return {
            "total": total, "active": active, "with_detail": detailed,
            "last_sync": self.meta("last_sync"), "last_error": self.meta("last_error"), "ready": active > 0,
            "source_url": LIST_URL,
        }


class BtbEngine:
    def __init__(self, data_dir: str | Path | None = None, *, http: httpx.AsyncClient | None = None, delay: float = 1.0) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or Path(__file__).resolve().parent / ".dev-data")
        self.store = BtbStore(root)
        self.delay = delay
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0), headers={"User-Agent": USER_AGENT})
        self._sync_lock = asyncio.Lock()
        # Bakanlık sitesine giden TÜM istekler (eşitleme + ayrıntı) tek kilitte ve en az ``delay`` aralıkla gider.
        self._pace_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._pace_lock:
            wait = self._last_request + self.delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await getattr(self._http, method)(url, **kwargs)
            finally:
                self._last_request = time.monotonic()

    async def sync(self) -> dict[str, Any]:
        """Tüm geçerli kayıt listesini okur. Eksik/bozuk okumada hiçbir şey yazılmaz."""
        if self._sync_lock.locked():
            return {"status": "busy"}
        async with self._sync_lock:
            try:
                rows = await self._read_all_pages()
            except (BtbError, httpx.HTTPError) as exc:
                self.store.set_meta("last_error", f"{date.today().isoformat()}: {exc}")
                logger.warning("BTB eşitlemesi başarısız: %s", exc)
                return {"status": "error", "error": str(exc)}
            known = self.store.status()["active"]
            if known and len(rows) < 0.8 * known:
                message = f"Liste beklenenden kısa ({len(rows)} / {known}); eşitleme uygulanmadı."
                self.store.set_meta("last_error", f"{date.today().isoformat()}: {message}")
                return {"status": "error", "error": message}
            result = self.store.apply_full_sync(rows)
            self.store.set_meta("last_sync", date.today().isoformat())
            self.store.set_meta("last_error", "")
            return {"status": "ok", **result}

    async def _read_all_pages(self) -> list[dict[str, str]]:
        """Sayfaları zincirleme gezer: olay doğrulaması yalnız o anki sayfanın pager bağlantılarını kabul eder.

        Bitiş koşulu, mevcut sayfanın pager'ında bir sonraki sayfa bağlantısının bulunmamasıdır;
        hata kodlarına dayanılmaz, böylece geçici bir 5xx listeyi sessizce kısaltmaz.
        """
        response = await self._request("get", LIST_URL)
        response.raise_for_status()
        page, collected = response.text, []
        for number in range(1, MAX_PAGES + 1):
            rows = parse_list(page)
            if not rows:
                raise BtbError(f"{number}. sayfada beklenen tablo bulunamadı; sayfa yapısı değişmiş olabilir.")
            collected.extend(rows)
            if not has_next_page(page, number):
                break
            form = hidden_fields(page)
            form.update({"__EVENTTARGET": GRID, "__EVENTARGUMENT": f"Page${number + 1}"})
            response = await self._request("post", LIST_URL, data=form)
            response.raise_for_status()
            page = response.text
        else:
            raise BtbError(f"Sayfa sınırına ({MAX_PAGES}) ulaşıldı; liste tamamlanamadı.")
        return list({row["btb_no"]: row for row in collected}.values())

    async def fetch_detail(self, btb_no: str) -> dict[str, Any] | None:
        """Tek kaydın tam tanımı ve gerekçesi; bir kez okunur, sonra depodan sunulur."""
        if not _BTB_NO.match(btb_no):
            raise ValueError("Geçersiz BTB numarası.")
        record = self.store.get(btb_no)
        if record is None:
            return None
        if not record["description_is_excerpt"] and record["reasoning"]:
            return record
        page = (await self._request("get", SEARCH_URL)).text
        form = hidden_fields(page)
        fields = {k: "" for k in ("txtGbastar", "txtGtip", "txtSinger", "txtEsya")}
        form.update({f"ctl00$ContentPlaceHolder1${k}": v for k, v in fields.items()})
        form.update({"ctl00$ContentPlaceHolder1$txtBtbno": btb_no, "ctl00$ContentPlaceHolder1$btnBul": "BUL"})
        listed = await self._request("post", SEARCH_URL, data=form)
        listed.raise_for_status()
        if btb_no not in listed.text:
            return record
        form = hidden_fields(listed.text)
        form.update({f"ctl00$ContentPlaceHolder1${k}": v for k, v in fields.items()})
        form.update({"__EVENTTARGET": f"{GRID}$ctl02$btn", "__EVENTARGUMENT": "", "ctl00$ContentPlaceHolder1$txtBtbno": btb_no})
        detail_page = await self._request("post", SEARCH_URL, data=form)
        detail_page.raise_for_status()
        detail = parse_detail(detail_page.text)
        if detail is None:
            return record
        self.store.save_detail(btb_no, detail)
        return self.store.get(btb_no)

    async def periodic_sync_loop(self, *, initial_delay: float = 240.0) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            outcome = await self.sync()
            await asyncio.sleep(3600.0 if outcome.get("status") == "error" else float(SYNC_INTERVAL))

    async def aclose(self) -> None:
        await self._http.aclose()
