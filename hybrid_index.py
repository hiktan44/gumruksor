"""Kalıcı hibrit arama indeksi: SQLite FTS5 (BM25) + embedding vektörleri (PRD Faz 3.1).

* Belgeler ``MEVZUAT_DATA_DIR/hybrid_index.sqlite3`` içinde tutulur (``documents``,
  ``documents_fts``, ``embeddings``, ``index_meta``).
* Vektörler RAM'de numpy float16 matris olarak durur (numpy yoksa saf Python listeleri);
  benzerlik kosinüs, birleştirme Reciprocal Rank Fusion (RRF).
* Sorgu gömme ``embed_timeout`` içinde bitmezse ya da hata verirse sonuç yalnız
  sözlüksel (BM25) döner; ``mode`` alanı ``lexical`` ya da ``hybrid`` olur.
* ``upsert_documents`` idempotenttir: ``source_sha256`` değişmeyen belge yeniden yazılmaz
  ve yeniden gömülmez.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy kurulu ortamlarda çalışmaz
    np = None  # type: ignore[assignment]

from security_firewall import SecurityViolation, guard_text, sanitize_untrusted_context

logger = logging.getLogger(__name__)

DB_FILE = "hybrid_index.sqlite3"
REFRESH_SECONDS = max(300, int(os.environ.get("HYBRID_INDEX_REFRESH_SECONDS", "1800") or 1800))
RRF_K = 60
GTIP_BOOST = 0.05
# Kosinüs benzerliği bu eşiğin altında kalan belgeler vektör adayı sayılmaz
# (ilgisiz belgeler yalnız sıralamaya girdikleri için RRF puanı almasın).
VECTOR_MIN_SIMILARITY = 0.05
MAX_TEXT_CHARS = 12_000
DEFAULT_EMBED_TIMEOUT = 0.45
_TOKEN_RE = re.compile(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü]+", re.UNICODE)


def _default_data_dir() -> Path:
    default_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mevzuat-mcp"
    return Path(os.environ.get("MEVZUAT_DATA_DIR", default_root))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalise_gtip(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def chunk_text(text: str, size: int = 1200, overlap: int = 120) -> list[str]:
    """Uzun metni yaklaşık ``size`` karakterlik, hafif örtüşen parçalara böler."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [chunk for chunk in chunks if chunk]


def _fts_query(text: str) -> str:
    terms = [term for term in _TOKEN_RE.findall(text) if len(term) >= 2][:12]
    if not terms:
        return ""
    return " OR ".join(f'"{term}"*' for term in terms)


def _vector_blob(vector: Iterable[float]) -> bytes:
    return array("f", [float(v) for v in vector]).tobytes()


def _blob_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


class HybridIndex:
    """BM25 + vektör hibrit arama indeksi (kalıcı, idempotent besleme)."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        embedder: Any = None,
        *,
        data_dir: str | Path | None = None,
    ) -> None:
        root = Path(data_dir) if data_dir else _default_data_dir()
        self.db_path = Path(db_path) if db_path else root / DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._lock = threading.RLock()
        self._ids: list[str] = []
        self._matrix: Any = None  # numpy float16 (n, dim) ya da list[list[float]]
        self._dim: int = 0
        self.last_refresh_at: str | None = None
        self.last_refresh_counts: dict[str, Any] = {}
        self.last_error: str | None = None
        self._init_db()
        self._load_vectors()

    # ---- storage
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    corpus TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    gtip_codes_json TEXT NOT NULL DEFAULT '[]',
                    source_url TEXT NOT NULL DEFAULT '',
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_documents_corpus ON documents(corpus);
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    id UNINDEXED,
                    corpus UNINDEXED,
                    title,
                    text,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS embeddings (
                    doc_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                    dim INTEGER NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    vector BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Geçmiş sürümler: belge artık "bugün yürürlükte olan" değil, "hangi aralıkta
            # yürürlükteydi" bilgisini taşır. Depo kuralı gereği yalnız eklemeli ALTER.
            self._ensure_column(connection, "documents", "as_of_from", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "documents", "as_of_to", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "documents", "snapshot_active", "INTEGER NOT NULL DEFAULT 1")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_validity ON documents(snapshot_active, as_of_from)"
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        """Eklemeli göç: eski veritabanında sütun yoksa ekler. Hiçbir tablo yeniden yazılmaz."""
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT INTO index_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def _get_meta(self, connection: sqlite3.Connection, key: str) -> Any:
        row = connection.execute("SELECT value FROM index_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    @property
    def embedder_dim(self) -> int:
        return int(getattr(self.embedder, "dim", 0) or 0) if self.embedder is not None else 0

    @property
    def embedder_name(self) -> str | None:
        return str(getattr(self.embedder, "name", "")) or None if self.embedder is not None else None

    # ---- vectors in RAM
    def _load_vectors(self) -> None:
        with self._lock:
            with self._connect() as connection:
                rows = connection.execute("SELECT doc_id, dim, vector FROM embeddings ORDER BY doc_id").fetchall()
            wanted = self.embedder_dim
            ids: list[str] = []
            vectors: list[list[float]] = []
            dim = 0
            for row in rows:
                if wanted and int(row["dim"]) != wanted:
                    continue
                if dim and int(row["dim"]) != dim:
                    continue
                dim = int(row["dim"])
                ids.append(row["doc_id"])
                vectors.append(_blob_vector(row["vector"]))
            self._ids = ids
            self._dim = dim
            if not vectors:
                self._matrix = None
            elif np is not None:
                self._matrix = np.asarray(vectors, dtype=np.float16)
            else:
                self._matrix = vectors

    def _vector_scores(self, query_vector: list[float], limit: int) -> list[tuple[str, float]]:
        with self._lock:
            if self._matrix is None or not self._ids or len(query_vector) != self._dim:
                return []
            if np is not None and not isinstance(self._matrix, list):
                query = np.asarray(query_vector, dtype=np.float32)
                scores = self._matrix.astype(np.float32) @ query
                top = min(limit, len(self._ids))
                order = np.argpartition(-scores, top - 1)[:top] if top < len(scores) else np.arange(len(scores))
                order = order[np.argsort(-scores[order])]
                return [
                    (self._ids[int(i)], float(scores[int(i)]))
                    for i in order
                    if float(scores[int(i)]) >= VECTOR_MIN_SIMILARITY
                ]
            scored = [
                (doc_id, sum(a * b for a, b in zip(vector, query_vector)))
                for doc_id, vector in zip(self._ids, self._matrix)
            ]
            scored.sort(key=lambda item: item[1], reverse=True)
            return [item for item in scored[:limit] if item[1] >= VECTOR_MIN_SIMILARITY]

    # ---- feeding
    @staticmethod
    def _prepare(doc: dict[str, Any]) -> dict[str, Any] | None:
        doc_id = str(doc.get("id") or "").strip()
        corpus = str(doc.get("corpus") or "").strip()
        if not doc_id or not corpus:
            return None
        title, _ = sanitize_untrusted_context(str(doc.get("title") or ""), max_chars=400)
        text, _ = sanitize_untrusted_context(str(doc.get("text") or ""), max_chars=MAX_TEXT_CHARS)
        text = text.strip()
        if not text and not title:
            return None
        codes = sorted({normalise_gtip(code) for code in (doc.get("gtip_codes") or []) if normalise_gtip(code)})
        sha = str(doc.get("source_sha256") or "")
        if not sha:
            sha = hashlib.sha256(f"{title}\n{text}".encode("utf-8")).hexdigest()
        return {
            "id": doc_id,
            "corpus": corpus,
            "title": title.strip(),
            "text": text,
            "gtip_codes_json": json.dumps(codes, ensure_ascii=False),
            "source_url": str(doc.get("source_url") or ""),
            "source_sha256": sha,
            "snapshot_id": str(doc.get("snapshot_id") or ""),
            # Besleyici vermezse belge "bugün yürürlükte" sayılır — göç öncesi davranışın aynısı.
            "as_of_from": str(doc.get("as_of_from") or "")[:40],
            "as_of_to": str(doc.get("as_of_to") or "")[:40],
            "snapshot_active": 0 if doc.get("snapshot_active") is False else 1,
        }

    def upsert_documents(self, docs: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Belgeleri yazar; ``source_sha256`` değişmeyenler atlanır (yeniden gömülmez)."""
        counts = {"inserted": 0, "updated": 0, "skipped": 0, "invalid": 0, "retimed": 0}
        with self._lock, self._connect() as connection:
            for raw in docs:
                prepared = self._prepare(raw)
                if prepared is None:
                    counts["invalid"] += 1
                    continue
                existing = connection.execute(
                    "SELECT source_sha256, as_of_from, as_of_to, snapshot_active FROM documents WHERE id=?",
                    (prepared["id"],),
                ).fetchone()
                if existing and existing["source_sha256"] == prepared["source_sha256"]:
                    # Metin aynı ama yürürlük aralığı kapanmış olabilir (yeni sürüm yayına
                    # girdiğinde önceki snapshot'a valid_to yazılır). Bu, metni yeniden
                    # gömmeyi gerektirmez — yalnız zaman sütunları güncellenir.
                    same_validity = (
                        (existing["as_of_from"] or "") == prepared["as_of_from"]
                        and (existing["as_of_to"] or "") == prepared["as_of_to"]
                        and int(existing["snapshot_active"] or 0) == prepared["snapshot_active"]
                    )
                    if same_validity:
                        counts["skipped"] += 1
                        continue
                    connection.execute(
                        "UPDATE documents SET as_of_from=:as_of_from, as_of_to=:as_of_to, "
                        "snapshot_active=:snapshot_active, updated_at=:updated_at WHERE id=:id",
                        {**prepared, "updated_at": _now()},
                    )
                    counts["retimed"] += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO documents(id, corpus, title, text, gtip_codes_json, source_url, source_sha256, snapshot_id,
                                          as_of_from, as_of_to, snapshot_active, updated_at)
                    VALUES (:id, :corpus, :title, :text, :gtip_codes_json, :source_url, :source_sha256, :snapshot_id,
                            :as_of_from, :as_of_to, :snapshot_active, :updated_at)
                    ON CONFLICT(id) DO UPDATE SET
                        corpus=excluded.corpus, title=excluded.title, text=excluded.text,
                        gtip_codes_json=excluded.gtip_codes_json, source_url=excluded.source_url,
                        source_sha256=excluded.source_sha256, snapshot_id=excluded.snapshot_id,
                        as_of_from=excluded.as_of_from, as_of_to=excluded.as_of_to,
                        snapshot_active=excluded.snapshot_active, updated_at=excluded.updated_at
                    """,
                    {**prepared, "updated_at": _now()},
                )
                connection.execute("DELETE FROM documents_fts WHERE id=?", (prepared["id"],))
                connection.execute(
                    "INSERT INTO documents_fts(id, corpus, title, text) VALUES (?, ?, ?, ?)",
                    (prepared["id"], prepared["corpus"], prepared["title"], prepared["text"]),
                )
                connection.execute("DELETE FROM embeddings WHERE doc_id=?", (prepared["id"],))
                counts["updated" if existing else "inserted"] += 1
        if counts["inserted"] or counts["updated"]:
            self._load_vectors()
        return counts

    def delete_documents(self, ids: Iterable[str]) -> int:
        ids = [str(i) for i in ids]
        if not ids:
            return 0
        removed = 0
        with self._lock, self._connect() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                marks = ",".join("?" for _ in batch)
                connection.execute(f"DELETE FROM documents_fts WHERE id IN ({marks})", batch)
                removed += connection.execute(f"DELETE FROM documents WHERE id IN ({marks})", batch).rowcount
        if removed:
            self._load_vectors()
        return removed

    def reindex(self, corpus: str, docs: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Korpusu verilen belge kümesiyle eşitler: değişenler yazılır, listede olmayanlar silinir."""
        docs = list(docs)
        keep = {str(doc.get("id")) for doc in docs if doc.get("id")}
        counts = self.upsert_documents(docs)
        with self._connect() as connection:
            existing = [row["id"] for row in connection.execute("SELECT id FROM documents WHERE corpus=?", (corpus,))]
        stale = [doc_id for doc_id in existing if doc_id not in keep]
        counts["removed"] = self.delete_documents(stale)
        return counts

    def pending_embedding_ids(self, limit: int | None = None) -> list[str]:
        dim = self.embedder_dim
        sql = (
            "SELECT d.id FROM documents d LEFT JOIN embeddings e ON e.doc_id=d.id "
            "WHERE e.doc_id IS NULL OR e.dim != ? ORDER BY d.updated_at, d.id"
        )
        params: list[Any] = [dim]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as connection:
            return [row["id"] for row in connection.execute(sql, params)]

    async def embed_pending(self, *, batch_size: int = 64, limit: int | None = None) -> int:
        """Embedding'i olmayan (ya da boyutu değişen) belgeleri arka planda gömer."""
        if self.embedder is None:
            return 0
        pending = await asyncio.to_thread(self.pending_embedding_ids, limit)
        if not pending:
            return 0
        done = 0
        model = str(getattr(self.embedder, "model", "") or "")
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            with self._connect() as connection:
                marks = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT id, title, text FROM documents WHERE id IN ({marks})", batch
                ).fetchall()
            if not rows:
                continue
            texts = [f"{row['title']}\n{row['text']}".strip() for row in rows]
            try:
                vectors = await self.embedder.embed(texts, task="document")
            except Exception as exc:  # noqa: BLE001 - indeks sözlüksel modda çalışmaya devam eder
                self.last_error = f"embedding: {exc}"[:300]
                logger.warning("Hybrid index embedding batch failed: %s", exc)
                break
            if len(vectors) != len(rows):
                self.last_error = "embedding: response size mismatch"
                break
            with self._lock, self._connect() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO embeddings(doc_id, dim, model, vector) VALUES (?, ?, ?, ?)",
                    [(row["id"], len(vector), model, _vector_blob(vector)) for row, vector in zip(rows, vectors)],
                )
            done += len(rows)
        if done:
            self._load_vectors()
        return done

    async def refresh(self, corpora: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """Tüm korpusları eşitler ve yalnız değişen belgeleri gömer."""
        summary: dict[str, Any] = {}
        for corpus, docs in corpora.items():
            summary[corpus] = await asyncio.to_thread(self.reindex, corpus, docs)
        summary["embedded"] = await self.embed_pending()
        self.last_refresh_at = _now()
        self.last_refresh_counts = summary
        with self._connect() as connection:
            self._set_meta(connection, "last_refresh_at", self.last_refresh_at)
            self._set_meta(connection, "last_refresh_counts", summary)
        return summary

    # ---- search
    @staticmethod
    def _validity_sql(as_of: str | None, alias: str = "d") -> tuple[str, list[Any]]:
        """Zaman filtresi. ``as_of`` yoksa bugünkü davranış birebir korunur (yalnız aktif sürüm).

        ``as_of`` verilirse o güne ait sürüm seçilir. Aralığı bilinmeyen belgeler (``as_of_from``
        boş) geçmiş sorgusunda **elenir**: tarihi doğrulanamayan bir satırı "o gün yürürlükteydi"
        diye göstermek, kanıtı olmayan bir iddia olurdu.
        """
        if not as_of:
            return f" AND {alias}.snapshot_active=1", []
        return (
            f" AND {alias}.as_of_from != '' AND {alias}.as_of_from <= ?"
            f" AND ({alias}.as_of_to = '' OR {alias}.as_of_to > ?)",
            [as_of, as_of],
        )

    def _lexical(
        self, query: str, limit: int, corpora: list[str] | None, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        match = _fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT d.id, d.corpus, d.title, d.gtip_codes_json, d.source_url, d.source_sha256, "
            "d.snapshot_id, d.as_of_from, d.as_of_to, d.snapshot_active, "
            "snippet(documents_fts, 3, '', '', ' … ', 32) AS snippet, bm25(documents_fts) AS rank "
            "FROM documents_fts f JOIN documents d ON d.id=f.id WHERE documents_fts MATCH ?"
        )
        params: list[Any] = [match]
        if corpora:
            sql += f" AND d.corpus IN ({','.join('?' for _ in corpora)})"
            params.extend(corpora)
        clause, clause_params = self._validity_sql(as_of)
        sql += clause
        params.extend(clause_params)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("Hybrid index FTS query failed: %s", exc)
            return []
        return [dict(row) for row in rows]

    def _fetch(self, ids: list[str], as_of: str | None = None) -> dict[str, dict[str, Any]]:
        """Vektör adaylarının künyesi. Zaman filtresi burada da uygulanır: filtreyi geçemeyen
        aday satır dönmez ve ``_fuse`` onu sessizce atlar."""
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        clause, clause_params = self._validity_sql(as_of, alias="documents")
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id, corpus, title, substr(text, 1, 240) AS snippet, gtip_codes_json, source_url, "
                f"source_sha256, snapshot_id, as_of_from, as_of_to, snapshot_active "
                f"FROM documents WHERE id IN ({marks}){clause}",
                [*ids, *clause_params],
            ).fetchall()
        return {row["id"]: dict(row) for row in rows}

    @staticmethod
    def _gtip_matches(prefix: str, codes: list[str]) -> bool:
        return any(code.startswith(prefix) or prefix.startswith(code) for code in codes if code)

    def search_lexical(
        self,
        query: str,
        *,
        limit: int = 10,
        gtip_prefix: str | None = None,
        corpora: list[str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Yalnız BM25 (senkron); event loop dışından güvenle çağrılabilir."""
        return self._fuse(
            query, limit=limit, gtip_prefix=gtip_prefix, corpora=corpora, query_vector=None, as_of=as_of
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        gtip_prefix: str | None = None,
        corpora: list[str] | None = None,
        embed_timeout: float = DEFAULT_EMBED_TIMEOUT,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """BM25 + vektör sonuçlarını RRF ile birleştirir; gömme gecikirse sözlüksel kalır.

        ``as_of`` verilmezse yalnız yürürlükteki sürüm aranır — göç öncesi davranışın aynısı.
        """
        text = guard_text(query, source="hibrit arama", max_chars=500)
        query_vector: list[float] | None = None
        if self.embedder is not None and text and self._matrix is not None:
            try:
                vectors = await asyncio.wait_for(self.embedder.embed([text], task="query"), timeout=embed_timeout)
                query_vector = list(vectors[0]) if vectors else None
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.info("Hybrid query embedding unavailable (%s); lexical only", type(exc).__name__)
                query_vector = None
        return await asyncio.to_thread(
            self._fuse,
            text,
            limit=limit,
            gtip_prefix=gtip_prefix,
            corpora=corpora,
            query_vector=query_vector,
            as_of=as_of,
        )

    def _fuse(
        self,
        query: str,
        *,
        limit: int,
        gtip_prefix: str | None,
        corpora: list[str] | None,
        query_vector: list[float] | None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        text = str(query or "").strip()
        limit = max(1, min(int(limit or 10), 50))
        prefix = normalise_gtip(gtip_prefix)
        corpora = [str(c) for c in (corpora or []) if str(c)] or None
        if not text:
            return {"query": "", "mode": "lexical", "items": [], "count": 0, "as_of": as_of, "history": bool(as_of)}
        candidates = limit * 4
        lexical = self._lexical(text, candidates, corpora, as_of)
        vector_hits = self._vector_scores(query_vector, candidates * 2) if query_vector else []
        mode = "hybrid" if query_vector else "lexical"

        fused: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(lexical, start=1):
            entry = fused.setdefault(row["id"], {"score": 0.0, "lexical_rank": None, "vector_rank": None, "row": row})
            entry["score"] += 1.0 / (RRF_K + rank)
            entry["lexical_rank"] = rank
        if vector_hits:
            allowed_corpora = set(corpora) if corpora else None
            missing = [doc_id for doc_id, _ in vector_hits if doc_id not in fused]
            details = self._fetch(missing, as_of)
            rank = 0
            for doc_id, similarity in vector_hits:
                row = fused[doc_id]["row"] if doc_id in fused else details.get(doc_id)
                if row is None or (allowed_corpora and row["corpus"] not in allowed_corpora):
                    continue
                rank += 1
                if rank > candidates:
                    break
                entry = fused.setdefault(doc_id, {"score": 0.0, "lexical_rank": None, "vector_rank": None, "row": row})
                entry["score"] += 1.0 / (RRF_K + rank)
                entry["vector_rank"] = rank
                entry["similarity"] = round(float(similarity), 4)

        items: list[dict[str, Any]] = []
        for doc_id, entry in fused.items():
            row = entry["row"]
            codes = json.loads(row.get("gtip_codes_json") or "[]")
            gtip_match = bool(prefix) and self._gtip_matches(prefix, codes)
            score = entry["score"] + (GTIP_BOOST if gtip_match else 0.0)
            items.append(
                {
                    "id": doc_id,
                    "corpus": row["corpus"],
                    "title": row.get("title") or "",
                    "snippet": (row.get("snippet") or "")[:320],
                    "gtip_codes": codes[:20],
                    "gtip_match": gtip_match,
                    "source_url": row.get("source_url") or "",
                    # Geçmiş cevabın künyesi: hangi anlık görüntüden, hangi aralık için.
                    # Künyesi olmayan satır geçmiş sorgusunda zaten elenir (bkz. _validity_sql).
                    "source_sha256": row.get("source_sha256") or "",
                    "snapshot_id": row.get("snapshot_id") or "",
                    "as_of_from": row.get("as_of_from") or None,
                    "as_of_to": row.get("as_of_to") or None,
                    "snapshot_active": bool(row.get("snapshot_active", 1)),
                    "score": round(score, 6),
                    "lexical_rank": entry["lexical_rank"],
                    "vector_rank": entry["vector_rank"],
                    "similarity": entry.get("similarity"),
                }
            )
        items.sort(key=lambda item: (-item["score"], item["id"]))
        items = items[:limit]
        payload = {
            "query": text,
            "mode": mode,
            "items": items,
            "count": len(items),
            "as_of": as_of,
            "history": bool(as_of),
        }
        if not items or mode != "hybrid":
            # Boş sonuç iki tamamen farklı şeyin aynı cevabı olabilir: "bu sorgu
            # eşleşmedi" ya da "indeks boş / gömme sağlayıcısı yok". Yanıt bunları
            # ayırt etmediği için canlıda hangisi olduğunu **saatlerce** bilemedim.
            #
            # Teşhis **düşmüş modda da** eklenir, yalnız boş sonuçta değil: canlıda
            # ``mode: lexical`` ile dolu sonuç aldım ve "vektör katmanı neden kapalı"
            # sorusunun cevabı yine hiçbir yerde yazmıyordu — asıl sorulan soru buydu.
            # Hibrit ve dolu sonuçta blok hiç eklenmez, o yolda maliyeti yok.
            payload["diagnostics"] = self._result_diagnostics(mode, empty=not items)
        return payload

    def _result_diagnostics(self, mode: str, *, empty: bool = True) -> dict[str, Any]:
        """Sebebi söyler: indeks mi boş, gömme mi kapalı, sorgu mu tutmadı.

        ``empty`` yanlışsa sonuç dolu ama mod düşmüş demektir; metinler bu iki durumu
        karıştırmamalı, yoksa teşhis kullanıcıyı yanlış yere bakmaya gönderir.
        """
        try:
            with self._connect() as connection:
                documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                last_refresh = self._get_meta(connection, "last_refresh_at")
        except Exception:  # noqa: BLE001 - teşhis bloğu aramayı düşürmemeli
            return {"reason": "unknown", "note": "İndeks durumu okunamadı."}
        embedder_configured = self.embedder is not None
        if documents == 0:
            reason = "index_empty"
            note = (
                "Hibrit indeks boş: hiçbir korpus belgesi yüklenmemiş. "
                "`hybrid-index-refresh` işinin durumunu ve son hatasını kontrol edin."
            )
        elif not embedder_configured:
            reason = "embedding_disabled"
            note = (
                "İndeks dolu ama gömme sağlayıcısı kurulu değil (EMBEDDING_PROVIDER "
                "ya da anahtar eksik); yalnız sözlüksel arama çalışıyor"
                + (" ve bu sorgu eşleşmedi." if empty else ", anlamsal eşleşme yapılamıyor.")
            )
        elif mode == "lexical":
            reason = "embedding_unavailable_this_query"
            note = (
                "Gömme sağlayıcısı kurulu ama bu sorgu için vektör üretilemedi "
                "(zaman aşımı ya da sağlayıcı hatası); yalnız sözlüksel arama çalıştı."
            )
        else:
            reason = "no_match"
            note = "İndeks dolu ve hibrit arama çalıştı; bu sorgu hiçbir belgeyle eşleşmedi."
        return {
            "reason": reason,
            "note": note,
            "index_documents": documents,
            "embedder": self.embedder_name,
            "embedding_count": len(self._ids),
            "last_refresh_at": self.last_refresh_at or last_refresh,
            "last_error": self.last_error,
        }

    # ---- status
    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            by_corpus = {
                row["corpus"]: int(row["n"])
                for row in connection.execute("SELECT corpus, COUNT(*) AS n FROM documents GROUP BY corpus ORDER BY corpus")
            }
            embedded = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            historical = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE snapshot_active=0"
            ).fetchone()[0]
            last_refresh = self._get_meta(connection, "last_refresh_at")
            last_counts = self._get_meta(connection, "last_refresh_counts")
        return {
            "db_path": str(self.db_path),
            "document_count": int(total),
            # Yürürlükten kalkmış ama arşivde duran sürümler; `as_of` sorgusunun malzemesi.
            "historical_count": int(historical),
            "corpora": by_corpus,
            "embedding_count": int(embedded),
            "vectors_in_memory": len(self._ids),
            "pending_embeddings": max(0, int(total) - len(self._ids)) if self.embedder is not None else 0,
            "embedder": self.embedder_name,
            "embedding_model": str(getattr(self.embedder, "model", "") or "") or None,
            "embedding_dim": self.embedder_dim or None,
            "vector_backend": "numpy" if np is not None else "python",
            "refresh_seconds": REFRESH_SECONDS,
            "last_refresh_at": self.last_refresh_at or last_refresh,
            "last_refresh_counts": self.last_refresh_counts or last_counts or {},
            "last_error": self.last_error,
        }


__all__ = [
    "HybridIndex",
    "REFRESH_SECONDS",
    "VECTOR_MIN_SIMILARITY",
    "SecurityViolation",
    "chunk_text",
    "normalise_gtip",
]
