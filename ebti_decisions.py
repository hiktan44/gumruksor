"""AB Bağlayıcı Tarife Bilgisi (EBTI) kararları: resmî günlük yayın akışı.

Avrupa Komisyonu, üye ülke gümrük idarelerinin verdiği **Bağlayıcı Tarife Bilgisi**
kararlarını ``EBTI Daily Updates`` sayfasında her gün bir ZIP/CSV dosyası olarak
yayımlar. Dosya herkese açıktır, giriş gerektirmez ve şu sütunları taşır:

``BTI_REFERENCE, ISSUING_COUNTRY, START_DATE_OF_VALIDITY, END_DATE_OF_VALIDITY,
NOMENCLATURE_CODE, CLASSIFICATION_JUSTIFICATION, STATUS, INVALIDATION_REASON,
INVALIDATION_JUSTIFICATION, LANGUAGE, PLACE_OF_ISSUE, DATE_OF_ISSUE,
NAME_AND_ADDRESS, DESCRIPTION_OF_GOODS, KEYWORDS``

Bu, Türk **BTB** kararlarının AB karşılığıdır: her satır bir eşyanın hangi koda,
hangi hukuki gerekçeyle (GİR kuralları, fasıl notları, AS İzahnamesi, sınıflandırma
tüzükleri) sınıflandırıldığını gösterir. Ürünün sınıflandırma kanıtı defterini
doğrudan besler.

Tasarım kararları:

* ``NAME_AND_ADDRESS`` sütunu **hiç saklanmaz** — sınıflandırma için gereksizdir ve
  kişisel veriye komşudur. Diğer serbest metinler ``sanitize_untrusted_context``ten
  geçirilir; bu dosyalar dış kaynaklıdır ve modele veri olarak girer, talimat olarak değil.
* Kararlar, veren ülkenin dilinde yazılır (fr, nl, de, it…). Dil alanı saklanır;
  diller arası ortak ve güvenilir anahtar **nomenklatür kodudur**.
* Yayın dosyaları artımlıdır (o günkü kararlar). Her dosya bir anlık görüntüdür ve
  inceleme kapısından geçer; yalnız onaylı anlık görüntülerin satırları aramada görünür.
* Bu kararlar AB'de **yalnız sahibini ve veren idareyi** bağlar; Türkiye'de bağlayıcı
  değildir. Sonuçlar her zaman bu notla döner.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin

import httpx

from change_ledger import batch_id_for, diff_rows
from review_policy import (
    DiffSummary,
    ReviewPolicy,
    decide,
    ensure_review_columns,
    review_metadata,
    row_review_fields,
)
from security_firewall import SecurityViolation, sanitize_untrusted_context, validate_outbound_url

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; MevzuatMCP/1.5; +https://gumruksor.com/)"

EBTI_BASE = (os.environ.get("EBTI_BASE_URL") or "https://ec.europa.eu/taxation_customs/dds2/ebti").rstrip("/")
_OFFICIAL_HOSTS = frozenset({"ec.europa.eu"})
DEFAULT_SYNC_INTERVAL = max(3600, int(os.environ.get("EBTI_SYNC_SECONDS") or 86400))
MAX_FILES_PER_RUN = max(1, int(os.environ.get("EBTI_MAX_FILES_PER_RUN") or 8))
SYNC_ENABLED = (os.environ.get("EBTI_SYNC_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}

_MAX_ZIP_BYTES = 40 * 1024 * 1024          # sıkıştırılmış indirme sınırı
_MAX_UNPACKED_BYTES = 200 * 1024 * 1024    # zip bombasına karşı açılmış boyut sınırı
_MAX_MEMBERS = 8
_MAX_ROWS_PER_FILE = 20_000
_MAX_REDIRECTS = 5
_MAX_TEXT = 8_000

# Kaydedilmeyen sütun: kişisel veriye komşu ve sınıflandırma için gereksiz.
DROPPED_COLUMNS = ("NAME_AND_ADDRESS",)

BINDING_NOTE = (
    "AB Bağlayıcı Tarife Bilgisi kararları yalnız kararı veren idareyi ve kararın sahibini AB "
    "gümrük bölgesinde bağlar; Türkiye'de bağlayıcı değildir. Karşılaştırmalı sınıflandırma "
    "kanıtı olarak değerlendirilir."
)

_PUBLICATION_RE = re.compile(
    r"ebti_export_management\.jsp\?publicationDate=([0-9]{4}-[0-9]{2}-[0-9]{2}[ +%20]+[0-9:]{8})&(?:amp;)?message=extract",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalise_code(value: Any) -> str:
    """'3920202190************' -> '3920202190' (dolgu yıldızları ve boşluklar atılır)."""
    return _digits(value)[:10]


def _clean(value: Any, *, limit: int = _MAX_TEXT) -> str:
    text, _ = sanitize_untrusted_context(str(value or ""))
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def _date(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


# --------------------------------------------------------------------------- saf ayrıştırıcılar

def parse_publication_list(html: str) -> list[dict[str, str]]:
    """``daily_publications.jsp`` gövdesinden yayın tarihlerini ve indirme adreslerini çıkarır."""
    seen: set[str] = set()
    publications: list[dict[str, str]] = []
    for match in _PUBLICATION_RE.finditer(html or ""):
        raw = match.group(1).replace("%20", " ").replace("+", " ").strip()
        if raw in seen:
            continue
        seen.add(raw)
        publications.append(
            {
                "publication_date": raw,
                "day": raw[:10],
                "url": f"{EBTI_BASE}/ebti_export_management.jsp?publicationDate={quote(raw)}&message=extract",
            }
        )
    publications.sort(key=lambda item: item["publication_date"], reverse=True)
    return publications


def extract_csv_from_zip(payload: bytes) -> str:
    """ZIP içindeki tek CSV üyesini güvenli biçimde çıkarır (zip-slip ve zip bombası korumalı)."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        if not members or len(members) > _MAX_MEMBERS:
            raise ValueError("EBTI yayın arşivi beklenen biçimde değil.")
        if sum(item.file_size for item in members) > _MAX_UNPACKED_BYTES:
            raise ValueError("EBTI yayın arşivi açılmış boyut sınırını aşıyor.")
        for item in members:
            name = item.filename
            if item.is_dir():
                continue
            # zip-slip: mutlak yol veya üst dizine çıkış kabul edilmez.
            if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
                raise ValueError("EBTI yayın arşivinde güvensiz dosya adı var.")
            if not name.lower().endswith(".csv"):
                continue
            with archive.open(item) as handle:
                return handle.read().decode("utf-8-sig", errors="replace")
    raise ValueError("EBTI yayın arşivinde CSV bulunamadı.")


def parse_decisions(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """CSV metnini karar satırlarına çevirir; ayrıştırma uyarılarını ayrıca döndürür."""
    warnings: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [str(name or "").strip() for name in (reader.fieldnames or [])]
    if "BTI_REFERENCE" not in fieldnames or "NOMENCLATURE_CODE" not in fieldnames:
        raise ValueError("EBTI CSV başlıkları beklenen biçimde değil.")
    # Resmî dosyada bu sütun adı boşluklu yazılıyor ("DATE_OF _ISSUE"); ikisini de kabul et.
    issue_key = next((name for name in fieldnames if name.replace(" ", "") == "DATE_OF_ISSUE"), None)
    decisions: list[dict[str, Any]] = []
    for index, raw in enumerate(reader, start=2):
        if index - 1 > _MAX_ROWS_PER_FILE:
            warnings.append(f"satır sınırı aşıldı, ilk {_MAX_ROWS_PER_FILE} satır alındı")
            break
        reference = _clean(raw.get("BTI_REFERENCE"), limit=120)
        code = normalise_code(raw.get("NOMENCLATURE_CODE"))
        if not reference:
            warnings.append(f"{index}. satırda karar referansı yok")
            continue
        if len(code) < 6:
            warnings.append(f"{reference}: nomenklatür kodu okunamadı ({raw.get('NOMENCLATURE_CODE')!r})")
        decisions.append(
            {
                "reference": reference,
                "issuing_country": _clean(raw.get("ISSUING_COUNTRY"), limit=4).upper(),
                "valid_from": _date(raw.get("START_DATE_OF_VALIDITY")),
                "valid_to": _date(raw.get("END_DATE_OF_VALIDITY")),
                "code": code,
                "status": _clean(raw.get("STATUS"), limit=20).upper() or "UNKNOWN",
                "invalidation_reason": _clean(raw.get("INVALIDATION_REASON"), limit=200),
                "language": _clean(raw.get("LANGUAGE"), limit=8).lower(),
                "place_of_issue": _clean(raw.get("PLACE_OF_ISSUE"), limit=200),
                "date_of_issue": _date(raw.get(issue_key) if issue_key else None),
                "description": _clean(raw.get("DESCRIPTION_OF_GOODS")),
                "justification": _clean(raw.get("CLASSIFICATION_JUSTIFICATION")),
                "keywords": _clean(raw.get("KEYWORDS"), limit=2000),
            }
        )
    if not decisions:
        raise ValueError("EBTI CSV dosyasında karar satırı bulunamadı.")
    return decisions, warnings


def decision_url(reference: str) -> str:
    """Kararın resmî EBTI ekranındaki adresi."""
    return f"{EBTI_BASE}/ebti_details.jsp?Lang=en&reference={quote(str(reference or ''))}"


# --------------------------------------------------------------------------- sonuç modelleri

@dataclass
class EbtiHit:
    id: str
    reference: str
    issuing_country: str
    code: str
    status: str
    language: str
    valid_from: str | None
    valid_to: str | None
    description: str
    justification: str
    keywords: str
    url: str
    publication_date: str
    source_url: str
    source_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reference": self.reference,
            "issuing_country": self.issuing_country,
            "code": self.code,
            "status": self.status,
            "language": self.language,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "description": self.description,
            "justification": self.justification,
            "keywords": self.keywords,
            "url": self.url,
            "publication_date": self.publication_date,
            "source_url": self.source_url,
            "source_sha256": self.source_sha256,
        }


@dataclass
class EbtiSearchResult:
    status: str  # ok | unavailable
    query: str = ""
    code_prefix: str | None = None
    hits: list[EbtiHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "code_prefix": self.code_prefix,
            "hits": [hit.as_dict() for hit in self.hits],
            "warnings": self.warnings,
            "binding_note": BINDING_NOTE,
        }


# --------------------------------------------------------------------------- motor

class EbtiDecisionEngine:
    """EBTI günlük yayınlarını indirir, ayrıştırır, inceleme kapısından geçirir ve aranabilir kılar."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        review_policy: ReviewPolicy | None = None,
        ledger: Any = None,
        base_url: str = EBTI_BASE,
        sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL,
        max_files_per_run: int = MAX_FILES_PER_RUN,
    ) -> None:
        root = Path(data_dir or os.environ.get("MEVZUAT_DATA_DIR") or ROOT)
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.db_path = root / "ebti_decisions.sqlite3"
        self.base_url = base_url.rstrip("/")
        self.review_policy = review_policy or ReviewPolicy()
        self.ledger = ledger
        self.sync_interval_seconds = sync_interval_seconds
        self.max_files_per_run = max_files_per_run
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )
        self._sync_lock = asyncio.Lock()
        self._syncing = False
        self._errors: list[str] = []
        self._initialise()

    # ---- depo
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
                    publication_date TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_ebti_snapshots_date ON snapshots(publication_date);
                CREATE TABLE IF NOT EXISTS decisions (
                    reference TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    issuing_country TEXT NOT NULL DEFAULT '',
                    code TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    valid_from TEXT,
                    valid_to TEXT,
                    date_of_issue TEXT,
                    place_of_issue TEXT NOT NULL DEFAULT '',
                    invalidation_reason TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    justification TEXT NOT NULL DEFAULT '',
                    keywords TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (reference, snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ebti_decisions_code ON decisions(code);
                CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
                    reference UNINDEXED, snapshot_id UNINDEXED, code, description, keywords, justification,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            ensure_review_columns(connection, "snapshots")
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def _set_metadata(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _get_metadata(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    # ---- HTTP
    async def _fetch(self, url: str, *, referer: str | None = None) -> tuple[bytes, str, str]:
        current = url
        for _ in range(_MAX_REDIRECTS):
            validate_outbound_url(current, allowed_hosts=_OFFICIAL_HOSTS)
            headers = {"Referer": referer} if referer else {}
            response: httpx.Response | None = None
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await self._http.get(current, headers=headers)
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
            if response is None:
                raise last_error or RuntimeError("EBTI kaynağı yanıt vermedi.")
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError("EBTI kaynağı hedefsiz yönlendirme döndürdü.")
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            content = response.content
            if len(content) > _MAX_ZIP_BYTES:
                raise ValueError("EBTI yayın dosyası beklenenden büyük.")
            media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            return content, media_type, current
        raise ValueError("EBTI kaynağı çok fazla yönlendirme yaptı.")

    async def close(self) -> None:
        await self._http.aclose()

    # ---- eşitleme
    async def sync(self, *, force: bool = False) -> dict[str, Any]:
        """Yayın listesini okur ve henüz alınmamış günlük dosyaları içeri aktarır."""
        async with self._sync_lock:
            self._syncing = True
            ingested = 0
            try:
                last_checked = self._get_metadata("last_checked_at")
                if not force and last_checked:
                    try:
                        elapsed = (datetime.now(UTC) - datetime.fromisoformat(last_checked)).total_seconds()
                        if elapsed < self.sync_interval_seconds:
                            return self.status()
                    except ValueError:
                        pass
                listing_url = f"{self.base_url}/daily_publications.jsp?Lang=en"
                content, _, resolved = await self._fetch(listing_url)
                publications = parse_publication_list(content.decode("utf-8", errors="replace"))
                self._set_metadata("last_checked_at", _now())
                if not publications:
                    raise ValueError("EBTI yayın listesi boş döndü.")
                # Liste okunabildi: hata listesi bu koşuya ait olacak şekilde sıfırlanır; her
                # dosyanın kendi hatası aşağıda birikir ve başarılı bir dosya onu silmez.
                self._errors.clear()
                with self._connect() as connection:
                    known = {
                        str(row["publication_date"])
                        for row in connection.execute("SELECT publication_date FROM snapshots").fetchall()
                    }
                pending = [item for item in publications if item["publication_date"] not in known]
                # En eskiden başla: kararlar kronolojik birikir.
                for publication in sorted(pending, key=lambda item: item["publication_date"])[: self.max_files_per_run]:
                    try:
                        await self._ingest(publication, referer=listing_url)
                        ingested += 1
                    except Exception as exc:  # noqa: BLE001 – bir dosya diğerlerini engellemez
                        self._errors.append(f"{publication['day']}: {type(exc).__name__}: {str(exc)[:200]}")
                        logger.warning("EBTI yayını alınamadı (%s): %s", publication["day"], exc)
                self._set_metadata("last_listing_url", resolved)
            except Exception as exc:  # noqa: BLE001
                self._errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
                logger.warning("EBTI eşitlemesi başarısız: %s", exc)
            finally:
                self._syncing = False
        status = self.status()
        status["ingested"] = ingested
        return status

    async def _ingest(self, publication: dict[str, str], *, referer: str) -> None:
        payload, media_type, resolved = await self._fetch(publication["url"], referer=referer)
        if "zip" not in media_type and not payload.startswith(b"PK"):
            raise ValueError("EBTI yayın dosyası ZIP biçiminde değil.")
        digest = hashlib.sha256(payload).hexdigest()
        text = await asyncio.to_thread(extract_csv_from_zip, payload)
        decisions, warnings = parse_decisions(text)
        snapshot_id = f"ebti-{publication['publication_date'][:10].replace('-', '')}-{digest[:12]}"
        retrieved_at = _now()

        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM snapshots WHERE sha256=?", (digest,)).fetchone():
                return
            previous = connection.execute(
                "SELECT id FROM snapshots WHERE status='approved' ORDER BY publication_date DESC LIMIT 1"
            ).fetchone()
            connection.execute(
                "INSERT INTO snapshots(id,publication_date,source_url,sha256,retrieved_at,row_count,status) "
                "VALUES(?,?,?,?,?,?, 'pending_review')",
                (snapshot_id, publication["publication_date"], resolved, digest, retrieved_at, len(decisions)),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO decisions(reference,snapshot_id,issuing_country,code,status,language,"
                "valid_from,valid_to,date_of_issue,place_of_issue,invalidation_reason,description,justification,keywords) "
                "VALUES(:reference,:snapshot_id,:issuing_country,:code,:status,:language,:valid_from,:valid_to,"
                ":date_of_issue,:place_of_issue,:invalidation_reason,:description,:justification,:keywords)",
                [dict(item, snapshot_id=snapshot_id) for item in decisions],
            )
            connection.executemany(
                "INSERT INTO decisions_fts(reference,snapshot_id,code,description,keywords,justification) "
                "VALUES(?,?,?,?,?,?)",
                [
                    (item["reference"], snapshot_id, item["code"], item["description"], item["keywords"], item["justification"])
                    for item in decisions
                ],
            )

        current_rows = {item["reference"]: {"code": item["code"], "status": item["status"], "gtip": item["code"]} for item in decisions}
        summary = DiffSummary(total_rows=len(current_rows), previous_rows=0, added=len(current_rows))
        decision = decide(self.review_policy, summary, parse_warnings=warnings, first_snapshot=True)
        warnings_json, diff_json = review_metadata(summary, decision, warnings)
        with self._connect() as connection:
            connection.execute(
                "UPDATE snapshots SET status=?, parse_warnings_json=?, diff_summary_json=? WHERE id=?",
                (decision.status, warnings_json, diff_json, snapshot_id),
            )
        self._record_ledger_batch(
            snapshot_id,
            previous["id"] if previous else None,
            changes=[
                {"entity_key": reference, "gtip": row["code"], "change_type": "added", "before": None, "after": row}
                for reference, row in sorted(current_rows.items())
            ],
            source_url=resolved,
            sha256=digest,
            total_rows=len(current_rows),
            detected_at=retrieved_at,
            review_status=decision.status,
            publication_date=publication["publication_date"],
        )
        if decision.pending:
            logger.info("EBTI yayını %s editör onayı bekliyor: %s", snapshot_id, "; ".join(decision.reasons))

    async def periodic_sync_loop(self, *, initial_delay: float = 180.0) -> None:
        await asyncio.sleep(initial_delay)
        while True:
            status = await self.sync()
            await asyncio.sleep(self.sync_interval_seconds if status.get("ready") else 1800)

    # ---- arama
    def search(self, query: str = "", *, code_prefix: str | None = None, limit: int = 8) -> EbtiSearchResult:
        limit = max(1, min(int(limit), 25))
        code = _digits(code_prefix)[:10]
        if code and len(code) < 4:
            raise ValueError("EBTI kodu en az 4 haneli olmalıdır.")
        status = self.status()
        if not status["ready"]:
            return EbtiSearchResult(
                status="unavailable",
                query=str(query or "")[:500],
                code_prefix=code or None,
                warnings=["AB Bağlayıcı Tarife Bilgisi indeksi henüz hazır değil.", *status["errors"][-3:]],
            )
        terms = [term for term in re.findall(r"[\wÀ-ž]{3,}", str(query or "").casefold()) if not term.isdigit()][:12]
        rows: list[sqlite3.Row] = []
        with self._connect() as connection:
            if code:
                rows.extend(
                    connection.execute(
                        "SELECT d.*, s.publication_date, s.source_url, s.sha256 FROM decisions d "
                        "JOIN snapshots s ON s.id=d.snapshot_id "
                        "WHERE s.status='approved' AND d.code LIKE ? "
                        "ORDER BY d.status='VALID' DESC, d.valid_from DESC LIMIT ?",
                        (f"{code}%", limit * 2),
                    ).fetchall()
                )
            if terms and len(rows) < limit:
                fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
                try:
                    rows.extend(
                        connection.execute(
                            "SELECT d.*, s.publication_date, s.source_url, s.sha256 FROM decisions_fts f "
                            "JOIN decisions d ON d.reference=f.reference AND d.snapshot_id=f.snapshot_id "
                            "JOIN snapshots s ON s.id=d.snapshot_id "
                            "WHERE s.status='approved' AND decisions_fts MATCH ? "
                            "ORDER BY bm25(decisions_fts) LIMIT ?",
                            (fts_query, limit * 2),
                        ).fetchall()
                    )
                except sqlite3.OperationalError as exc:  # bozuk FTS ifadesi aramayı düşürmez
                    logger.warning("EBTI tam metin sorgusu çalıştırılamadı: %s", exc)
        seen: set[str] = set()
        hits: list[EbtiHit] = []
        for row in rows:
            if row["reference"] in seen:
                continue
            seen.add(row["reference"])
            hits.append(
                EbtiHit(
                    id=f"ebti_{_digits(row['reference'])[:12] or row['reference'][:12]}_{row['code'][:10]}",
                    reference=row["reference"],
                    issuing_country=row["issuing_country"],
                    code=row["code"],
                    status=row["status"],
                    language=row["language"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    description=row["description"][:1200],
                    justification=row["justification"][:1200],
                    keywords=row["keywords"][:400],
                    url=decision_url(row["reference"]),
                    publication_date=str(row["publication_date"]),
                    source_url=str(row["source_url"]),
                    source_sha256=str(row["sha256"]),
                )
            )
            if len(hits) >= limit:
                break
        return EbtiSearchResult(status="ok", query=str(query or "")[:500], code_prefix=code or None, hits=hits)

    # ---- durum ve inceleme
    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            approved = connection.execute("SELECT COUNT(*) FROM snapshots WHERE status='approved'").fetchone()[0]
            pending = connection.execute("SELECT COUNT(*) FROM snapshots WHERE status='pending_review'").fetchone()[0]
            decisions = connection.execute(
                "SELECT COUNT(*) FROM decisions d JOIN snapshots s ON s.id=d.snapshot_id WHERE s.status='approved'"
            ).fetchone()[0]
            latest = connection.execute(
                "SELECT publication_date FROM snapshots WHERE status='approved' ORDER BY publication_date DESC LIMIT 1"
            ).fetchone()
        return {
            "ready": bool(approved),
            "syncing": self._syncing,
            "review_mode": self.review_policy.mode,
            "publication_count": int(approved),
            "pending_review_count": int(pending),
            "decision_count": int(decisions),
            "latest_publication": str(latest["publication_date"]) if latest else None,
            "last_checked_at": self._get_metadata("last_checked_at"),
            "errors": self._errors[-8:],
        }

    def _review_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "kind": "ebti",
            "snapshot_id": row["id"],
            "source_id": "eu_ebti_daily",
            "title": f"AB Bağlayıcı Tarife Bilgisi günlük yayını ({str(row['publication_date'])[:10]})",
            "source_url": row["source_url"],
            "sha256": row["sha256"],
            "retrieved_at": row["retrieved_at"],
            "valid_from": str(row["publication_date"])[:10],
            "total_rows": int(row["row_count"] or 0),
            "active": row_review_fields(row)["status"] == "approved",
            "ledger_batch": batch_id_for("ebti", "eu_ebti_daily", row["id"]) if self.ledger is not None else None,
        }
        item.update(row_review_fields(row))
        return item

    def pending_reviews(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE status='pending_review' ORDER BY publication_date DESC"
            ).fetchall()
        return [self._review_item(row) for row in rows]

    def review_snapshot(self, snapshot_id: str, action: str, *, reviewed_by: str, note: str = "") -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError("Karar 'approve' veya 'reject' olmalıdır.")
        status = "approved" if action == "approve" else "rejected"
        now = _now()
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM snapshots WHERE id=?", (snapshot_id,)).fetchone() is None:
                raise KeyError(snapshot_id)
            connection.execute(
                "UPDATE snapshots SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                (status, reviewed_by, now, note, snapshot_id),
            )
            updated = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return self._review_item(updated)

    # ---- değişiklik defteri
    def _record_ledger_batch(
        self,
        snapshot_id: str,
        previous_id: str | None,
        *,
        changes: list[dict[str, Any]],
        source_url: str,
        sha256: str,
        total_rows: int,
        detected_at: str,
        review_status: str,
        publication_date: str,
        backfilled: bool = False,
    ) -> str | None:
        if self.ledger is None:
            return None
        try:
            return self.ledger.record_batch(
                kind="ebti",
                source_id="eu_ebti_daily",
                title=f"AB Bağlayıcı Tarife Bilgisi günlük yayını ({publication_date[:10]})",
                new_snapshot_id=snapshot_id,
                old_snapshot_id=previous_id,
                source_url=source_url,
                sha256=sha256,
                changes=changes,
                total_rows=total_rows,
                detected_at=detected_at,
                review_status=review_status,
                valid_from=publication_date[:10],
                backfilled=backfilled,
            )
        except Exception as exc:  # noqa: BLE001 – defter bir eşitlemeyi asla bozmaz
            logger.warning("EBTI değişiklik defteri yazımı başarısız: %s", exc)
            return None

    def backfill_ledger(self) -> int:
        if self.ledger is None:
            return 0
        written = 0
        with self._connect() as connection:
            snapshots = connection.execute("SELECT * FROM snapshots ORDER BY publication_date ASC").fetchall()
        previous_id: str | None = None
        for row in snapshots:
            if not self.ledger.has_batch(batch_id_for("ebti", "eu_ebti_daily", row["id"])):
                if self._record_ledger_batch(
                    row["id"], previous_id, changes=[], source_url=row["source_url"], sha256=row["sha256"],
                    total_rows=int(row["row_count"] or 0), detected_at=row["retrieved_at"],
                    review_status=row_review_fields(row)["status"], publication_date=str(row["publication_date"]),
                    backfilled=True,
                ):
                    written += 1
            previous_id = row["id"]
        return written

    # ---- hibrit indeks beslemesi
    def corpus_rows(self, limit: int = 6000) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT d.*, s.source_url, s.sha256, s.id AS snap FROM decisions d JOIN snapshots s ON s.id=d.snapshot_id "
                "WHERE s.status='approved' ORDER BY d.valid_from DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        documents: list[dict[str, Any]] = []
        for row in rows:
            text = " ".join(part for part in (row["description"], row["keywords"], row["justification"]) if part)
            if not text.strip():
                continue
            documents.append(
                {
                    "id": f"ebti-{row['reference']}",
                    "corpus": "ebti",
                    "title": f"AB BTB {row['reference']} ({row['issuing_country']}) — {row['code']}",
                    "text": text[:4000],
                    "gtip_codes": [row["code"]] if row["code"] else [],
                    "source_url": decision_url(row["reference"]),
                    "source_sha256": str(row["sha256"]),
                    "snapshot_id": str(row["snap"]),
                }
            )
        return documents


__all__ = [
    "BINDING_NOTE",
    "DROPPED_COLUMNS",
    "EbtiDecisionEngine",
    "EbtiHit",
    "EbtiSearchResult",
    "SYNC_ENABLED",
    "decision_url",
    "extract_csv_from_zip",
    "normalise_code",
    "parse_decisions",
    "parse_publication_list",
]
