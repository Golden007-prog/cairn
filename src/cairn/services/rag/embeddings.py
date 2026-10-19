"""Pluggable embedding providers for RAG.

Selected via RAG_EMBED_PROVIDER env var:
  gemini  — Google Gemini embeddings + fastembed BM25 sparse (default)
  openai  — OpenAI text-embedding-3-small + fastembed BM25 sparse
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from abc import ABC, abstractmethod
from functools import lru_cache

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):

    @property
    def dim(self) -> int:
        """Dense embedding dimension — used for vector store collection creation."""
        from cairn.core.provider_settings import get_settings
        return get_settings().rag_embed_dim

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Dense embeddings for a batch of texts."""
        ...

    @abstractmethod
    async def embed_one(self, text: str) -> list[float]:
        """Dense embedding for a single text."""
        ...

    @abstractmethod
    async def embed_sparse(self, texts: list[str]) -> list[dict] | None:
        """Sparse (BM25) embeddings. Returns [{indices, values}] or None if unsupported."""
        ...

    @abstractmethod
    async def embed_sparse_one(self, text: str) -> dict | None:
        """Sparse embedding for a single text."""
        ...


class GeminiEmbedder(EmbeddingProvider):

    def __init__(self) -> None:
        self._client = None
        self._sparse_model = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            from cairn.core.provider_settings import get_settings
            self._client = genai.Client(api_key=get_settings().gemini_api_key)
        return self._client

    def _get_sparse_model(self):
        if self._sparse_model is None:
            from fastembed import SparseTextEmbedding
            self._sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
        return self._sparse_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from cairn.core.provider_settings import get_settings
        client = self._get_client()
        response = await client.aio.models.embed_content(
            model=get_settings().rag_embed_model, contents=texts,
        )
        return [e.values for e in response.embeddings]

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]

    async def embed_sparse(self, texts: list[str]) -> list[dict] | None:
        model = self._get_sparse_model()
        results = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [
            {"indices": r.indices.tolist(), "values": r.values.tolist()}
            for r in results
        ]

    async def embed_sparse_one(self, text: str) -> dict | None:
        result = await self.embed_sparse([text])
        return result[0] if result else None


class OpenAIEmbedder(EmbeddingProvider):
    """OpenAI embeddings via the openai SDK (text-embedding-3-small default)."""

    def __init__(self) -> None:
        self._client = None
        self._sparse_model = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            from cairn.core.provider_settings import get_settings
            self._client = AsyncOpenAI(api_key=get_settings().openai_api_key)
        return self._client

    def _get_sparse_model(self):
        if self._sparse_model is None:
            from fastembed import SparseTextEmbedding
            self._sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
        return self._sparse_model

    _DEFAULT_MODEL = "text-embedding-3-small"
    _DEFAULT_DIM = 1536

    @property
    def dim(self) -> int:
        _, dim = self._resolve_model_and_dim()
        return dim

    def _resolve_model_and_dim(self):
        from cairn.core.provider_settings import get_settings
        s = get_settings()
        model = s.rag_embed_model
        dim = s.rag_embed_dim
        # If settings still have the Gemini defaults, swap in OpenAI defaults
        if "gemini" in model:
            model = self._DEFAULT_MODEL
            dim = self._DEFAULT_DIM
        return model, dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._get_client()
        model, dim = self._resolve_model_and_dim()
        response = await client.embeddings.create(
            model=model,
            input=texts,
            dimensions=dim,
        )
        return [item.embedding for item in response.data]

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]

    async def embed_sparse(self, texts: list[str]) -> list[dict] | None:
        model = self._get_sparse_model()
        results = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [
            {"indices": r.indices.tolist(), "values": r.values.tolist()}
            for r in results
        ]

    async def embed_sparse_one(self, text: str) -> dict | None:
        result = await self.embed_sparse([text])
        return result[0] if result else None


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingProvider:
    from cairn.core.provider_settings import get_settings
    name = (get_settings().rag_embed_provider or "gemini").lower()

    if name == "gemini":
        logger.info("RAG embeddings: gemini")
        return GeminiEmbedder()

    if name == "openai":
        logger.info("RAG embeddings: openai")
        return OpenAIEmbedder()

    logger.warning("Unknown RAG_EMBED_PROVIDER=%r — falling back to gemini", name)
    return GeminiEmbedder()


def reset_embedder() -> None:
    """Drop the cached embedder (tests, or after RAG_EMBED_* env changes)."""
    get_embedder.cache_clear()


def embedder_fingerprint() -> str:
    """A short stable id of the ACTIVE embedder — provider + model. This IS the
    embedding space a corpus's vectors live in (#512): a corpus can only be
    searched with the embedder that built it, so this becomes part of the
    collection name to keep spaces from silently cross-wiring."""
    from cairn.core.provider_settings import get_settings

    s = get_settings()
    provider = (s.rag_embed_provider or "gemini").lower()
    model = (s.rag_embed_model or "").strip()
    return hashlib.sha256(f"{provider}:{model}".encode("utf-8")).hexdigest()[:8]


def rag_collection(corpus: str) -> str:
    """The vector collection for ``corpus`` under the ACTIVE embedder — the single
    source of truth used by BOTH the ingest path (``discover_corpora``) and the
    ``rag/search`` operator, so index and query always agree (#512).

    A profile that changes the embedder therefore resolves to a DIFFERENT
    collection: search finds it empty ("not indexed for this embedder") and
    returns no hits — fail-closed — instead of comparing the query against
    vectors from a different embedding space and returning silent garbage.

    The corpus name is slugified (non-alphanumerics → ``_``) because a store
    collection must be ``[A-Za-z0-9_]`` only. Pack corpus names are already
    clean, but BYO names are user-supplied (e.g. ``standard-readme``); without
    this, index and search would derive DIFFERENT strings — the hyphen bug that
    silently indexed a BYO corpus into ``rag_standard_readme__…`` while search
    looked up the invalid ``rag_standard-readme__…`` and always got zero hits."""
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", corpus).strip("_") or "corpus"
    return f"rag_{safe}__{embedder_fingerprint()}"
