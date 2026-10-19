"""Pinecone vector store — stub for community implementation.

To implement:
  1. pip install pinecone-client
  2. Fill in all methods below
  3. Set RAG_VECTOR_STORE=pinecone and PINECONE_API_KEY in env
"""
from __future__ import annotations

from cairn.services.rag.stores.base import VectorStore

_NOT_IMPLEMENTED = (
    "Pinecone vector store is not yet implemented. "
    "Set RAG_VECTOR_STORE=qdrant or contribute an implementation."
)


class PineconeStore(VectorStore):
    async def initialize(self, collection: str, dim: int) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def upsert(self, ids, dense, sparse, payloads) -> int:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def search(self, dense, sparse=None, top_k=5, filters=None) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def delete_by_source(self, source: str) -> int:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def list_sources(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def has_source(self, source: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def close(self) -> None:
        pass
