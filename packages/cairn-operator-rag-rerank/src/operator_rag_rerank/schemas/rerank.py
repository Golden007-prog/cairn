"""rag/rerank schemas (#513)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RerankHit(BaseModel):
    """One candidate to re-rank. ``text`` is scored against the query; ``score``
    is the incoming (retrieval) score; ``payload`` is carried through opaque."""

    score: float = 0.0
    text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class RerankInput(BaseModel):
    query: str
    hits: list[RerankHit] = Field(default_factory=list)
    top_k: int = 4
    #: auto | identity | cross_encoder | cohere. ``auto`` = the best installed
    #: backend, else identity.
    reranker: str = "auto"
    #: cross-encoder model id (only the cross_encoder backend uses it).
    model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class RerankOutput(BaseModel):
    ok: bool = True
    hits: list[RerankHit] = Field(default_factory=list)
    reranker_used: str = "identity"
    error: str = ""
