"""Abstract vector store interface.

To add a new store (e.g. Pinecone, PGVector, Weaviate):
  1. Create ``src/rag/stores/mystore.py``
  2. Subclass ``VectorStore`` and implement all abstract methods
  3. Add an ``elif`` branch in ``stores/__init__.py``
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class VectorStore(ABC):

    @abstractmethod
    async def initialize(self, collection: str, dim: int) -> None:
        """Create collection / index if it doesn't exist."""
        ...

    @abstractmethod
    async def upsert(
        self,
        ids: list[str],
        dense: list[list[float]],
        sparse: list[dict] | None,
        payloads: list[dict],
    ) -> int:
        """Insert or update vectors. Returns number of points stored."""
        ...

    @abstractmethod
    async def search(
        self,
        dense: list[float],
        sparse: dict | None = None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[dict]:
        """Hybrid search. Returns one FLAT row per hit: ``{"score": float, **payload}`` —
        the stored payload's fields spread beside ``score`` (sqlite-vec, pgvector and Qdrant
        all do this, and the RAG query engine reads ``content``/``source`` off the row).
        A caller that needs the payload alone reads it with :func:`hit_payload` (#2563)."""
        ...

    @abstractmethod
    async def delete_by_source(self, source: str) -> int:
        """Delete all points from a given source document. Returns count deleted."""
        ...

    @abstractmethod
    async def list_sources(self, limit: int = 50) -> list[dict]:
        """List unique document sources. Returns [{source, source_type, chunk_count}]."""
        ...

    @abstractmethod
    async def has_source(self, source: str) -> bool:
        """Return True if any vectors exist for this source."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...


def hit_payload(row: dict) -> dict:
    """The stored payload of one :meth:`VectorStore.search` row: every field but ``score``.

    The ONE reading of the flat row (#2563). ``rag/search``, ``rag/fetch`` and ``memory/search``
    each used to read ``row["payload"]`` — a key the backends never write — so every real hit
    came back with an empty payload. A stored field that happens to be named ``payload`` is
    data like any other and is returned as such, not unwrapped."""
    return {k: v for k, v in row.items() if k != "score"}
