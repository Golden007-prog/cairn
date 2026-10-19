"""Pydantic schemas for memory/search."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SearchInput(BaseModel):
    """Inputs for a memory similarity search.

    ``store`` is the named store from a pack's
    ``[requires.memory.<store>]`` declaration — used to scope the lookup
    so two memory stores (e.g. ``similar_incidents`` and
    ``service_runbooks``) within the same deployment don't collide.

    ``query`` is the text query to embed and search against. ``top_k``
    bounds the result set; ``filters`` is the standard payload-match
    filter accepted by the underlying ``VectorStore``.
    """

    store: str = Field(
        ...,
        description="Named store from [requires.memory.<store>].",
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
        description="Payload-match filters (e.g. {'service': 'checkout'}).",
    )


class SearchHit(BaseModel):
    """One returned record + score."""

    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class SearchOutput(BaseModel):
    """Result of a memory search."""

    ok: bool
    hits: list[SearchHit] = Field(default_factory=list)
    error: str = ""
