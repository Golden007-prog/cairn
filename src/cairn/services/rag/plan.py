"""Incremental index planning — the pure decision layer (ADR-0023 Fork C).

``plan()`` is a pure function: given the live corpus (source → content_hash),
the catalog's current entries, and the resolved build fingerprint, it decides
which sources to (re)index, skip, or tombstone. No I/O — trivially testable and
the heart of the idempotency-first model (§24.23.2).

Re-index when the file's bytes changed (``content_hash``) OR the build recipe
changed (``build_fingerprint``: parser/chunk/embedding-model/dim/store). Skip
when both match a prior ``indexed`` entry. Tombstone catalog sources that are no
longer present in the corpus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cairn.core.provider_settings import get_settings
from cairn.core.storage.rag_catalog import CatalogEntry
from cairn.services.rag.idempotency import build_fingerprint, document_id

# Chunking is hardcoded in ingest.py today (SentenceSplitter 512/64; Markdown
# section-aware). Captured here so a future config change re-indexes via the
# fingerprint — even though it isn't tunable yet.
_CHUNK_SIZE = 512
_CHUNK_OVERLAP = 64


def resolved_build_config() -> dict[str, Any]:
    """The active build recipe, resolved from provider settings (the env/profile
    glue layer per ADR-0026 — packs stay backend-agnostic, Fork B). A change to
    any of these keys re-indexes affected corpora."""
    s = get_settings()
    return {
        "embed_provider": s.rag_embed_provider,
        "embed_model": s.rag_embed_model,
        "embed_dim": s.rag_embed_dim,
        "vector_store": s.rag_vector_store,
        "chunk_size": _CHUNK_SIZE,
        "chunk_overlap": _CHUNK_OVERLAP,
        "parser": "auto-by-source-type",
    }


def resolved_build_fingerprint() -> str:
    return build_fingerprint(resolved_build_config())


@dataclass
class RagIndexPlan:
    """The diff between the live corpus and the catalog."""

    corpus: str
    build_fingerprint: str
    to_index: list[str] = field(default_factory=list)   # sources to (re)embed
    to_skip: list[str] = field(default_factory=list)     # unchanged, already indexed
    to_delete: list[str] = field(default_factory=list)   # gone from corpus → tombstone

    @property
    def is_noop(self) -> bool:
        return not self.to_index and not self.to_delete


def plan(
    corpus: str,
    live_sources: dict[str, str],
    catalog_entries: list[CatalogEntry],
    build_fp: str,
) -> RagIndexPlan:
    """Decide index actions. ``live_sources`` maps source → content_hash."""
    by_id = {e.document_id: e for e in catalog_entries}
    p = RagIndexPlan(corpus=corpus, build_fingerprint=build_fp)

    live_ids: set[str] = set()
    for source, chash in live_sources.items():
        did = document_id(corpus, source)
        live_ids.add(did)
        entry = by_id.get(did)
        if (
            entry is not None
            and entry.status == "indexed"
            and entry.content_hash == chash
            and entry.build_fingerprint == build_fp
        ):
            p.to_skip.append(source)
        else:
            p.to_index.append(source)

    # Sources in the catalog (still live-tracked, not already tombstoned) that
    # have vanished from the corpus → tombstone.
    for entry in catalog_entries:
        if entry.status == "tombstoned":
            continue
        if entry.document_id not in live_ids:
            p.to_delete.append(entry.source)

    return p
