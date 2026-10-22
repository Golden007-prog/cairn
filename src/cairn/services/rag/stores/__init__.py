"""Pluggable vector store — select backend via RAG_VECTOR_STORE env var.

In-built core backends:
  sqlite_vec — DEFAULT. Dense-only embedded backend via the
               ``rag-sqlite-vec`` optional dep group. Delivers the §24.4
               Tier-1 "one SQLite file for everything in dev" promise —
               defaults to the same file as ``SqliteRunStore``. Sparse
               accepted but ignored (upstream-in-flight).
  pgvector   — prod. Dense-only Postgres backend via the ``rag-pgvector``
               optional dep group. Sparse accepted but ignored; hybrid
               requires a tsvector FTS extension (out of scope for v1).

External PROVIDERS — resolved from the ``cairn.vector_stores`` entry-point
group (core imports none); live in ``cairn/providers/vectorstores/`` (#76):
  qdrant     — hybrid dense + sparse search via Qdrant (``rag-qdrant``)
  pinecone   — stub (community contribution welcome)

To add a new backend: drop a driver file alongside qdrant.py /
pgvector.py / sqlite_vec.py, subclass :class:`VectorStore`, and add an
``if name == "..."`` branch below. The higher-level
:class:`HybridRagStore` in :mod:`cairn.core.storage.rag`
automatically picks it up.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import AsyncIterator

from cairn.services.rag.stores.base import VectorStore

logger = logging.getLogger(__name__)

#: Guards the process-wide store of :func:`get_vector_store`. A store holds the
#: collection its next operation acts on as instance state (``initialize`` selects
#: it), so on a store shared by every workspace, "select the collection" and "run
#: the operation" must happen with no other caller in between: see
#: :func:`on_collection`.
SHARED_STORE_LOCK = threading.Lock()

#: How long a waiting caller sleeps between attempts to take a store lock.
_LOCK_POLL_S = 0.002

#: The store locks the current task (and the tasks it starts while holding them,
#: such as a workflow's steps) already holds, by ``id``: a nested entry point
#: (``index_corpus`` → ``ingest_document``) re-enters instead of deadlocking.
_HELD: ContextVar[frozenset[int]] = ContextVar("cairn_vector_store_locks_held", default=frozenset())


@asynccontextmanager
async def on_collection(
    store: VectorStore, collection: str, dim: int, lock: threading.Lock = SHARED_STORE_LOCK,
) -> AsyncIterator[VectorStore]:
    """Select ``collection`` on a shared ``store`` and hold ``lock`` until the
    block ends, so everything in the block acts on that collection and no other
    caller selects another one in between.

    Each entry point that uses a shared store (one lock per shared store
    instance) wraps its WHOLE logical operation in this block: an index run, an
    ingest, a query, a memory save/recall/list. Re-entrant per task: a nested
    entry point inside a held block selects its collection without taking the
    lock again. The lock is a :class:`threading.Lock` taken without blocking the
    event loop (a non-blocking try, then a short sleep), so it holds across event
    loops and threads, and a caller cancelled while waiting never owns it."""
    held = _HELD.get()
    if id(lock) in held:
        await store.initialize(collection, dim)
        yield store
        return
    while not lock.acquire(blocking=False):
        await asyncio.sleep(_LOCK_POLL_S)
    token = _HELD.set(held | {id(lock)})
    try:
        await store.initialize(collection, dim)
        yield store
    finally:
        _HELD.reset(token)
        lock.release()


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    """Process-wide RAG vector store (cached). Backend via ``RAG_VECTOR_STORE``
    (default: ``sqlite_vec``)."""
    from cairn.core.provider_settings import get_settings
    return build_vector_store(get_settings().rag_vector_store or "sqlite_vec")


def build_vector_store(name: str) -> VectorStore:
    """Construct a fresh vector store by backend name.

    In-built (core): ``sqlite_vec`` (DEFAULT — dev/local, `rag-sqlite-vec`) and
    ``pgvector`` (prod, `rag-pgvector`). Any other backend (``qdrant``,
    ``pinecone``, community) is an external PROVIDER resolved from the
    ``cairn.vector_stores`` entry-point group — core imports none of them.
    Unknown / not-installed → fall back to sqlite_vec.
    """
    name = (name or "sqlite_vec").lower()

    if name in ("sqlite_vec", "sqlite-vec", "sqlitevec"):
        from cairn.services.rag.stores.sqlite_vec import SqliteVecStore
        logger.info("RAG vector store: sqlite-vec (dense-only, embedded)")
        return SqliteVecStore()

    if name == "pgvector":
        from cairn.services.rag.stores.pgvector import PGVectorStore
        logger.info("RAG vector store: pgvector (dense-only)")
        return PGVectorStore()

    # ── external providers (qdrant, pinecone, community) ─────────────────
    # Resolved from the `cairn.vector_stores` entry-point group — core imports
    # NONE of them (#76). Providers live in cairn/providers/vectorstores/ with
    # their deps behind the rag-<name> extras.
    factory = _load_provider(name)
    if factory is not None:
        logger.info("RAG vector store: %s (provider)", name)
        return factory()

    logger.warning(
        "RAG_VECTOR_STORE=%r is not in-built and no `cairn.vector_stores` "
        "provider is installed — falling back to sqlite_vec (install the "
        "provider extra, e.g. rag-%s).", name, name,
    )
    from cairn.services.rag.stores.sqlite_vec import SqliteVecStore
    return SqliteVecStore()


def _load_provider(name: str):
    """Resolve an external vector-store provider from the ``cairn.vector_stores``
    entry-point group. Returns the VectorStore subclass (a zero-arg factory) or None.
    Core never imports the providers directly — they self-register (#76)."""
    from importlib.metadata import entry_points

    for ep in entry_points(group="cairn.vector_stores"):
        if ep.name.lower() == name:
            return ep.load()
    return None


def reset_vector_store() -> None:
    """Drop the cached vector store (tests, or after RAG_VECTOR_STORE changes)."""
    get_vector_store.cache_clear()
