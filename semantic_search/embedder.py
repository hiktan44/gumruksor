# semantic_search/embedder.py
"""Embedding sağlayıcıları.

* ``EmbeddingProvider`` – ortak async yüzey (``embed(texts, task=...)``, ``dim``, ``name``).
* ``GeminiNativeEmbedder`` – Google AI Studio ``batchEmbedContents`` (``GEMINI_API_KEY``).
* ``OpenRouterEmbedder`` – OpenRouter/OpenAI uyumlu uç (``OPENROUTER_API_KEY``); eski senkron
  ``encode_query``/``encode_documents`` yüzeyi geri uyumluluk için korunur.
* ``build_embedder()`` – ``EMBEDDING_PROVIDER=gemini|openrouter|none`` seçimi; boşsa anahtara göre.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any, List, Optional, Protocol, runtime_checkable

import httpx

try:  # numpy isteğe bağlıdır; yoksa saf Python normalizasyonu kullanılır.
    import numpy as np
except Exception:  # pragma: no cover - numpy kurulu ortamlarda çalışmaz
    np = None  # type: ignore[assignment]

from security_firewall import guard_text, redact_text, sanitize_untrusted_context, validate_outbound_url

logger = logging.getLogger(__name__)

# OpenRouter üzerinden desteklenen modeller ve boyutları
EMBEDDING_MODELS = {
    "google/gemini-embedding-001": 3072,
    "intfloat/multilingual-e5-large": 1024,
}
DEFAULT_MODEL = "google/gemini-embedding-001"

# Google yerel uç
GEMINI_DEFAULT_MODEL = "gemini-embedding-001"
GEMINI_DEFAULT_DIM = 768
GEMINI_API_HOST = "generativelanguage.googleapis.com"
GEMINI_BATCH_SIZE = 100
RETRY_DELAYS: tuple[float, ...] = (1.5, 3.0, 6.0)
TASK_TYPES = {"query": "RETRIEVAL_QUERY", "document": "RETRIEVAL_DOCUMENT"}


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Sorgu ve belge metinlerini normalize edilmiş vektörlere dönüştürür."""

    name: str
    dim: int

    async def embed(self, texts: list[str], *, task: str = "document") -> list[list[float]]: ...


def is_openrouter_available() -> bool:
    """Check if OpenRouter API key is available."""
    return bool(os.getenv("OPENROUTER_API_KEY"))


def is_gemini_available() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def get_embedding_model() -> str:
    """OpenRouter için modeli env'den okur; bilinmeyen modelde varsayılana düşer."""
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
    if model not in EMBEDDING_MODELS:
        logger.warning(f"Unknown EMBEDDING_MODEL '{model}', falling back to {DEFAULT_MODEL}")
        return DEFAULT_MODEL
    return model


def _l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return [float(value) for value in vector]
    return [float(value) / norm for value in vector]


def prepare_document_text(text: str, title: str = "") -> str:
    """Belge metnini modele göndermeden önce temizler ve kişisel veriyi maskeler."""
    safe_doc, _ = sanitize_untrusted_context(str(text or ""))
    safe_title = redact_text(str(title or ""), contact_data=True)
    body = redact_text(safe_doc, contact_data=True)
    if safe_title and safe_title != "none":
        return f"{safe_title}\n{body}"
    return body


class GeminiNativeEmbedder:
    """Google Generative Language API ``batchEmbedContents`` istemcisi (httpx, async)."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable is not set")
        self.model = (model or os.getenv("EMBEDDING_MODEL") or GEMINI_DEFAULT_MODEL).strip()
        if "/" in self.model:  # OpenRouter biçimi (google/gemini-embedding-001) verilmişse sadeleştir
            self.model = self.model.rsplit("/", 1)[-1]
        try:
            self.dim = int(dim or os.getenv("EMBEDDING_DIM") or GEMINI_DEFAULT_DIM)
        except ValueError:
            self.dim = GEMINI_DEFAULT_DIM
        self.dimension = self.dim  # eski yüzeyle uyum
        self.url = validate_outbound_url(
            f"https://{GEMINI_API_HOST}/v1beta/models/{self.model}:batchEmbedContents",
            allowed_hosts={GEMINI_API_HOST},
        )
        self._http = http
        self._owns_http = http is None
        logger.info("Gemini embedder initialised: model=%s dim=%s", self.model, self.dim)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self._http

    async def close(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = await self._client().post(
                    self.url,
                    json=payload,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = RuntimeError(f"Gemini embedding HTTP {response.status_code}")
                elif response.status_code >= 400:
                    raise RuntimeError(f"Gemini embedding HTTP {response.status_code}: {response.text[:200]}")
                else:
                    return response.json()
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
        raise RuntimeError(f"Gemini embedding request failed: {last_error}")

    async def embed(self, texts: list[str], *, task: str = "document") -> list[list[float]]:
        if not texts:
            return []
        task_type = TASK_TYPES.get(task, TASK_TYPES["document"])
        vectors: list[list[float]] = []
        for start in range(0, len(texts), GEMINI_BATCH_SIZE):
            batch = texts[start:start + GEMINI_BATCH_SIZE]
            payload = {
                "requests": [
                    {
                        "model": f"models/{self.model}",
                        "content": {"parts": [{"text": redact_text(str(text or " "), contact_data=True)[:20_000]}]},
                        "taskType": task_type,
                        "outputDimensionality": self.dim,
                    }
                    for text in batch
                ]
            }
            data = await self._post(payload)
            embeddings = data.get("embeddings") or []
            if len(embeddings) != len(batch):
                raise RuntimeError("Gemini embedding response size mismatch")
            for item in embeddings:
                values = [float(v) for v in (item.get("values") or [])]
                if len(values) != self.dim:
                    raise RuntimeError(f"Gemini embedding dimension mismatch: {len(values)} != {self.dim}")
                vectors.append(_l2_normalise(values))
        return vectors


class OpenRouterEmbedder:
    """
    Embedder using OpenRouter API.
    Supports multiple models via EMBEDDING_MODEL env var:
    - google/gemini-embedding-001 (default, 3072 dim)
    - intfloat/multilingual-e5-large (1024 dim)

    Requires OPENROUTER_API_KEY environment variable.
    """

    name = "openrouter"

    def __init__(self):
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set")

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package is required. Install with: pip install openai")

        validate_outbound_url("https://openrouter.ai/api/v1", allowed_hosts={"openrouter.ai"})
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )
        self.model = get_embedding_model()
        self.dimension = EMBEDDING_MODELS[self.model]
        self.dim = self.dimension
        self._is_e5 = "e5" in self.model

        logger.info(f"OpenRouter Embedder initialized: model={self.model}, dim={self.dimension}")

    def _format_query(self, query: str) -> str:
        """Format query text based on model requirements."""
        if self._is_e5:
            return f"query: {query}"
        return f"task: search result | query: {query}"

    def _format_document(self, text: str, title: str) -> str:
        """Format document text based on model requirements."""
        if self._is_e5:
            return f"passage: {title} {text}" if title and title != "none" else f"passage: {text}"
        return f"title: {title} | text: {text}"

    def _embed_raw(self, inputs: List[str]) -> List[List[float]]:
        response = self.client.embeddings.create(
            model=self.model,
            input=inputs,
            encoding_format="float",
            extra_headers={
                "HTTP-Referer": "https://mevzuatmcp.com",
                "X-Title": "Mevzuat MCP Server",
            }
        )
        return [list(d.embedding) for d in sorted(response.data, key=lambda x: x.index)]

    async def embed(self, texts: list[str], *, task: str = "document") -> list[list[float]]:
        """Ortak async yüzey: metinler zaten hazırlanmış kabul edilir (bkz. prepare_document_text)."""
        if not texts:
            return []
        if task == "query":
            formatted = [self._format_query(redact_text(text, contact_data=True)) for text in texts]
        else:
            formatted = [self._format_document(redact_text(text, contact_data=True), "none") for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(formatted), 50):
            batch = formatted[start:start + 50]
            raw = await asyncio.to_thread(self._embed_raw, batch)
            vectors.extend(_l2_normalise(vector) for vector in raw)
        return vectors

    def encode_query(self, query: str):
        """Encode a search query into an embedding vector."""
        guarded = guard_text(query, source="semantik arama", max_chars=4_000)
        text = self._format_query(redact_text(guarded, contact_data=True))

        try:
            embedding = _l2_normalise(self._embed_raw([text])[0])
            logger.debug("Encoded redacted query (%d chars) -> dim %d", len(query), len(embedding))
            return np.array(embedding, dtype=np.float32) if np is not None else embedding

        except Exception as e:
            logger.error(f"Failed to encode query: {e}")
            raise

    def encode_documents(self, documents: List[str], titles: Optional[List[str]] = None,
                         batch_size: int = 50):
        """Encode multiple documents, batching to avoid API limits."""
        if not documents:
            return np.array([]) if np is not None else []

        texts = []
        for i, doc in enumerate(documents):
            title = titles[i] if titles and i < len(titles) else "none"
            safe_doc, _ = sanitize_untrusted_context(doc)
            texts.append(
                self._format_document(
                    redact_text(safe_doc, contact_data=True),
                    redact_text(title, contact_data=True),
                )
            )

        try:
            all_embeddings: list[list[float]] = []

            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                logger.info(f"Encoding batch {start // batch_size + 1}/{(len(texts) - 1) // batch_size + 1} ({len(batch)} docs)")
                all_embeddings.extend(_l2_normalise(vector) for vector in self._embed_raw(batch))

            logger.info(f"Encoded {len(documents)} documents -> {len(all_embeddings)} vectors")
            return np.array(all_embeddings, dtype=np.float32) if np is not None else all_embeddings

        except Exception as e:
            logger.error(f"Failed to encode documents: {e}")
            raise


def build_embedder() -> EmbeddingProvider | None:
    """``EMBEDDING_PROVIDER`` (gemini|openrouter|none) ya da mevcut anahtara göre sağlayıcı kurar."""
    choice = (os.getenv("EMBEDDING_PROVIDER") or "").strip().lower()
    if not choice:
        if is_gemini_available():
            choice = "gemini"
        elif is_openrouter_available():
            choice = "openrouter"
        else:
            choice = "none"
    try:
        if choice == "gemini":
            return GeminiNativeEmbedder()
        if choice == "openrouter":
            return OpenRouterEmbedder()
        if choice != "none":
            logger.warning("Bilinmeyen EMBEDDING_PROVIDER=%r; embedding kapalı", choice)
    except Exception as exc:  # noqa: BLE001 - eksik anahtar/paket embedding'i kapatır, sunucuyu durdurmaz
        logger.warning("Embedding sağlayıcısı kurulamadı (%s): %s", choice, exc)
    return None


def is_embedding_available() -> bool:
    choice = (os.getenv("EMBEDDING_PROVIDER") or "").strip().lower()
    if choice == "none":
        return False
    if choice == "gemini":
        return is_gemini_available()
    if choice == "openrouter":
        return is_openrouter_available()
    return is_gemini_available() or is_openrouter_available()
