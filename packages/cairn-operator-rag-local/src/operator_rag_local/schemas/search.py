"""Pydantic schemas for rag/search."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SearchInput(BaseModel):
    """Inputs for a RAG corpus retrieval.

    ``corpus`` is the named RAG corpus from a pack's
    ``[requires.rag.<corpus>]`` declaration — used to scope the
    underlying SQLite collection so two corpora (e.g. ``runbooks`` and
    ``service_docs``) within the same deployment don't collide.

    ``query`` is the text to embed and search against. ``top_k`` bounds
    the result set. ``filters`` are payload-match constraints applied
    by the underlying ``VectorStore``.
    """

    corpus: str = Field(
        ...,
        description="Named RAG corpus from [requires.rag.<corpus>].",
    )
    query: str = Field(
        ...,
        description="Text query — embedded by the substrate embedder.",
    )
    top_k: int = Field(
        5,
        ge=1,
        le=100,
        description="Maximum results to return.",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Payload-match filters (e.g. {'doc_type': 'runbook'}).",
    )


class SearchHit(BaseModel):
    """One returned chunk + score."""

    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class SearchOutput(BaseModel):
    """Result of a RAG search."""

    ok: bool
    hits: list[SearchHit] = Field(default_factory=list)
    error: str = ""
