"""Pydantic schemas for memory/write."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WriteInput(BaseModel):
    """Inputs for upserting a single memory record.

    ``store`` is the named store from a pack's
    ``[requires.memory.<store>]`` declaration. ``record`` is the typed
    payload conforming to the pack's declared ``schema`` (validation is
    a Phase 3+ concern; this operator round-trips the dict).

    ``embed_text`` is the text representation used to compute the
    embedding vector. Most callers derive this from one or two fields
    of ``record`` (e.g. for a similar-incident store, embed_text =
    record['symptom']).

    ``id`` is an optional stable identifier for upsert semantics. If
    omitted, the operator derives one from ``embed_text``.
    """

    store: str = Field(
        ...,
        description="Named store from [requires.memory.<store>].",
    )
    record: dict[str, Any] = Field(
        ...,
        description="Payload to store. Conforms to the pack's declared schema.",
    )
    embed_text: str = Field(
        ...,
        description="Text used to compute the embedding vector.",
    )
    id: str = Field(
        "",
        description="Optional stable id for upsert; derived from embed_text if omitted.",
    )


class WriteOutput(BaseModel):
    """Result of a memory write."""

    ok: bool
    id: str = ""
    error: str = ""
