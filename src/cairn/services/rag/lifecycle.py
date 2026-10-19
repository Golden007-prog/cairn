"""RAG indexing lifecycle — drive index_corpus from pack declarations (ADR-0023 slice 2).

Slice 1 built the incremental ``index_corpus``; this is the drive surface that
discovers *which* corpora to index from loaded packs' ``[requires.rag.<name>]``
declarations and invokes it — on install/update (a startup pass) and on a
freshness schedule (see :mod:`cairn.services.rag.schedule`).

Indexing is **opt-in** (``CAIRN_RAG_AUTOINDEX``): a deployment turns it on, just
like redaction/audit. Keyless local-dev stays untouched and pays nothing.

Collection alignment (ADR-0023): we index into ``rag_{name}`` — the same
collection the local query provider (``cairn-operator-rag-local``) reads. The catalog
``corpus`` id is pack-qualified (``{pack}/{name}``) so two packs that declare the
same corpus name plan/tombstone independently. The index backend must match the
bound query provider's backend — :func:`check_backend_alignment` warns when they
diverge.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from cairn.core.storage.rag_catalog import RagCatalogStore
from cairn.services.rag.embeddings import rag_collection  # embedder-scoped collection (#512)

if TYPE_CHECKING:  # annotations only — never imported at runtime (no llama_index)
    from cairn.services.rag.indexer import IndexResult

logger = logging.getLogger(__name__)

# NOTE: cairn.services.rag.indexer is imported lazily inside index_all() — it
# pulls in the LlamaIndex ingest chain (the optional `rag` extra). Importing it
# at module top made `autoindex_enabled()` (an env check the server calls at every
# startup) hard-require llama_index, crash-looping the server without the extra.
# Keep this module import-light so the server boots regardless of the rag extra.

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CorpusTarget:
    """One indexable corpus resolved from a pack declaration."""

    pack: str
    name: str            # the [requires.rag.<name>] key
    corpus_id: str       # catalog id, pack-qualified: f"{pack}/{name}"
    root: str            # absolute corpus directory on disk
    collection: str      # vector collection: f"rag_{name}" (matches cairn-operator-rag-local)
    freshness: Optional[str]
    role: Optional[str] = None  # #496 — the knowledge role this corpus provides into


def autoindex_enabled() -> bool:
    """Whether lifecycle indexing is turned on (``CAIRN_RAG_AUTOINDEX``)."""
    return getenv("CAIRN_RAG_AUTOINDEX", "").strip().lower() in _TRUTHY


def _pack_root(pack) -> Optional[Path]:
    """Pack root dir. Asset dirs sit one level under it, so the rag asset dir's
    parent is the root (``corpus`` paths are relative to the pack root)."""
    rag_dir = getattr(pack, "rag_dir", None)
    return Path(rag_dir).parent if rag_dir else None


def discover_corpora(packs=None) -> list[CorpusTarget]:
    """Resolve every ``[requires.rag.<name>]`` across loaded packs to a target."""
    if packs is None:
        from cairn.core.pack.loader import ensure_loaded
        from cairn.core.pack.registry import get_packs
        ensure_loaded(strict=False)
        packs = get_packs()

    targets: list[CorpusTarget] = []
    for pack in packs:
        manifest = getattr(pack, "manifest", None)
        required = getattr(manifest, "required_rag", None) if manifest else None
        if not required:
            continue
        root = _pack_root(pack)
        if root is None:
            logger.warning(
                "[index] pack %s declares rag corpora but ships no rag asset dir — skipping",
                pack.name,
            )
            continue
        for name, req in required.items():
            if not req.corpus:
                continue
            targets.append(CorpusTarget(
                pack=pack.name, name=name, corpus_id=f"{pack.name}/{name}",
                # embedder-scoped collection (#512) + composition role (#496)
                root=str(root / req.corpus), collection=rag_collection(name),
                freshness=req.freshness, role=getattr(req, "role", None),
            ))
    return targets


async def index_all(packs=None, *, catalog: RagCatalogStore | None = None) -> list[IndexResult]:
    """Index every declared corpus once (the install/update pass)."""
    # Lazy import (see module NOTE): indexer pulls the LlamaIndex ingest chain
    # (optional `rag` extra), so it must not load at module import time (#39).
    from cairn.services.rag.indexer import index_corpus  # noqa: PLC0415

    results: list[IndexResult] = []
    for t in discover_corpora(packs):
        logger.info("[index] %s → collection %s (%s)", t.corpus_id, t.collection, t.root)
        results.append(
            await index_corpus(t.corpus_id, t.root, catalog=catalog, collection=t.collection)
        )
    return results


def check_backend_alignment() -> Optional[str]:
    """Warn if the configured index backend likely diverges from the bound query
    provider. Returns a warning string (also logged) or None when aligned.

    The local query provider (``cairn-operator-rag-local``) reads sqlite-vec; if a
    deployment binds it but points ``RAG_VECTOR_STORE`` elsewhere, indexed
    vectors won't be found. This is a heuristic nudge, not enforcement."""
    try:
        from cairn.core.pack.profile import load_profile_from_env
        bound = load_profile_from_env().bindings.get("rag.search")
    except Exception:  # noqa: BLE001
        bound = None
    # Single source of truth: the same setting the stores factory reads —
    # NOT a direct os.environ read with its own default (split-brain risk).
    from cairn.core.provider_settings import get_settings
    backend = (get_settings().rag_vector_store or "qdrant").strip().lower()
    # cairn-operator-rag-local is the sqlite-vec provider.
    if bound in ("rag/search", "rag-local", None) and backend not in ("sqlite_vec", "sqlite-vec"):
        msg = (
            f"RAG index backend RAG_VECTOR_STORE={backend!r} likely diverges from the "
            f"local query provider (sqlite-vec). Indexed vectors may be unqueryable; "
            f"set RAG_VECTOR_STORE=sqlite_vec or bind a matching query provider."
        )
        logger.warning("[index] %s", msg)
        return msg
    return None


async def check_embedder_alignment(packs=None, catalog=None) -> Optional[str]:
    """Warn if any indexed corpus was built under a DIFFERENT embedder than the
    active one (#512). The catalog records each document's ``build_fingerprint``,
    which encodes parser/chunk/embed-provider/model/dim. Because collections are
    now embedder-scoped, a corpus whose stored fingerprint ≠ the active
    ``resolved_build_fingerprint()`` won't be found by search — it needs a
    re-index. Returns a warning string (also logged) or ``None`` when aligned."""
    from cairn.services.rag.plan import resolved_build_fingerprint

    active = resolved_build_fingerprint()
    if catalog is None:
        from cairn.core.storage.rag_catalog import default_rag_catalog
        catalog = default_rag_catalog()

    stale: list[str] = []
    for t in discover_corpora(packs):
        entries = await catalog.list_by_corpus(t.corpus_id)
        indexed = [e for e in entries if e.build_fingerprint]
        # stale iff it HAS indexed docs but NONE match the active build/embedder
        if indexed and all(e.build_fingerprint != active for e in indexed):
            stale.append(t.corpus_id)

    if stale:
        msg = (
            f"RAG corpora indexed under a different embedder/build than the active "
            f"one ({active}): {', '.join(stale)}. Embedder-scoped collections mean "
            f"search won't find them — re-index (did RAG_EMBED_* change?)."
        )
        logger.warning("[index] %s", msg)
        return msg
    return None
