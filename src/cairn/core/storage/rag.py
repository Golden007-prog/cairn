"""Concrete :class:`RagStore` impls + factory.

Today: a thin :class:`HybridRagStore` adapter that wraps the existing
module-level functions in :mod:`cairn.services.rag` (hybrid dense+sparse
retrieval through pluggable VectorStore + reranker + synthesizer). The
wrapper satisfies the Protocol so new consumers can program against the
contract instead of importing the legacy module functions directly.

P4.8 will add :class:`SqliteVecRagStore` (Tier 1: sqlite-vec on the same
SQLite database as :class:`SqliteRunStore`). P5 adds the Postgres + pgvector
variant. The existing Qdrant/Pinecone/pgvector backends remain valid
underneath as VectorStore drivers; the Tier-3 swap-in shape is
``CAIRN_RAG=qdrant://…`` selecting which VectorStore driver the wrapper
delegates to.

Why this file isn't ``vector_store.py`` or ``rag_store.py``: the simple name
keeps the storage namespace flat and matches the §24.4 layout sketch.
"""

from __future__ import annotations

from typing import Any, Optional

from cairn.core.storage.protocols import RagStore


class HybridRagStore:
    """Adapter — wraps :mod:`cairn.services.rag` as a Protocol-shaped class.

    No behavior change vs the module-level functions; just a class surface
    so consumers can inject + mock + swap backends. Async methods delegate
    to the existing pipeline (embed → retrieve → rerank → synthesize for
    :meth:`search`; parse → chunk → embed → upsert for
    :meth:`ingest_document`).
    """

    async def search(
        self,
        query: str,
        *,
        collection: Optional[str] = None,
        top_k: int = 3,
        synthesis_prompt: Optional[str] = None,
        job_tag: str = "rag",
    ) -> dict[str, Any]:
        # Lazy import — the RAG pipeline pulls in llama-index, the embedder,
        # the vector-store client. Offline-tolerant code paths shouldn't pay
        # this just to type-import RagStore.
        from cairn.services.rag.query import search as _search

        return await _search(
            query,
            collection=collection,
            top_k=top_k,
            synthesis_prompt=synthesis_prompt,
            job_tag=job_tag,
        )

    async def ingest_document(
        self,
        file_path: str,
        *,
        collection: Optional[str] = None,
    ) -> str:
        from cairn.services.rag.ingest import ingest_document as _ingest

        return await _ingest(file_path, collection=collection)


def default_rag_store() -> RagStore:
    """Pick a RAG store from environment.

    Today: returns :class:`HybridRagStore` regardless of config — the
    legacy multi-backend path (Qdrant/Pinecone/pgvector under VectorStore)
    is the only impl. Future:

      - ``CAIRN_RAG`` unset, ``CAIRN_STORAGE=sqlite://…`` → ``SqliteVecRagStore``
        (Tier 1, P4.8)
      - ``CAIRN_RAG`` unset, ``CAIRN_STORAGE=postgres://…`` → ``PgvectorRagStore``
        (Tier 2, P5)
      - ``CAIRN_RAG=qdrant://…`` → :class:`HybridRagStore` with Qdrant driver
        (Tier 3 swap-in)
      - ``CAIRN_RAG=chroma://…`` → ``ChromaRagStore`` (Tier 3 swap-in, P7)

    The shape is settled; only the implementations are still to land.
    """
    return HybridRagStore()
