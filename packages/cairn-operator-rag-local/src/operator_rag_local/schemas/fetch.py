"""Pydantic schemas for rag/fetch."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FetchInput(BaseModel):
    """Inputs for fetching the verbatim content of a known source.

    Unlike ``rag.search`` which similarity-ranks, ``rag.fetch`` pulls
    back every chunk associated with the named ``source`` (typically a
    document URI like ``runbooks/db-failover.md`` recorded as the chunk
    payload's ``source`` field at ingest time).

    Useful as a follow-up after ``rag.search`` returned a hit referencing
    a specific document and the caller wants the full text rather than
    the top-scoring chunk.
    """

    corpus: str = Field(
        ...,
        description="Named RAG corpus from [requires.rag.<corpus>].",
    )
    source: str = Field(
        ...,
        description="Source identifier (e.g. relative path under the corpus root).",
    )


class FetchedChunk(BaseModel):
    """One chunk of a fetched source."""

    payload: dict[str, Any] = Field(default_factory=dict)


class FetchOutput(BaseModel):
    """Result of a RAG fetch."""

    ok: bool
    found: bool = False
    chunks: list[FetchedChunk] = Field(default_factory=list)
    error: str = ""
