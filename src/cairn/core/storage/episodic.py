"""Concrete :class:`MemoryStore` impls + factory.

Today: a thin :class:`EpisodicMemoryStore` adapter that wraps the existing
module-level functions in :mod:`cairn.services.memory.store` (Qdrant-backed
with cosine-similarity dedup). The wrapper satisfies the Protocol so new
consumers can program against the contract instead of importing the legacy
module functions directly.

P4.8 will add :class:`SqliteVecMemoryStore` (Tier 1: sqlite-vec on the same
SQLite database as :class:`SqliteRunStore`). P5 adds the Postgres + pgvector
variant. Both implement the same Protocol; the factory below picks one by
env.

Why this file isn't in ``memory/``: that directory is the **in-process
storage tier** (volatile, used by :class:`InMemoryRunStore`). The episodic
memory store concept is orthogonal to the runs-storage tier — it can be
backed by any of SQLite/Postgres/Qdrant regardless of which run store is
active. Putting it at the storage root avoids a naming clash.
"""

from __future__ import annotations

from typing import Any, Optional

from cairn.core.storage.protocols import MemoryStore


class EpisodicMemoryStore:
    """Adapter — wraps :mod:`cairn.services.memory.store` as a Protocol-shaped class.

    No behavior change vs the module-level functions; just a class surface
    so consumers can inject + mock + swap backends. Async methods delegate
    to the existing functions one-to-one.
    """

    async def save(
        self,
        payload: dict[str, Any],
        *,
        embed_text: str,
        dedup_id: Optional[str] = None,
    ) -> dict[str, Any]:
        # Lazy import — the embedder + Qdrant client load is heavy and
        # offline-tolerant code shouldn't pay it just for type imports.
        from cairn.services.memory.store import save_episodic

        return await save_episodic(payload, embed_text=embed_text, dedup_id=dedup_id)

    async def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        from cairn.services.memory.store import recall_episodic

        return await recall_episodic(query, top_k=top_k, filters=filters)

    async def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        from cairn.services.memory.store import list_episodic

        return await list_episodic(limit=limit)


def default_memory_store() -> MemoryStore:
    """Pick a memory store from environment.

    Today: returns :class:`EpisodicMemoryStore` regardless of config — the
    legacy Qdrant path is the only impl. Future:

      - ``CAIRN_MEMORY`` unset, ``CAIRN_STORAGE=sqlite://…`` → ``SqliteVecMemoryStore``
        (Tier 1, P4.8)
      - ``CAIRN_MEMORY`` unset, ``CAIRN_STORAGE=postgres://…`` → ``PgvectorMemoryStore``
        (Tier 2, P5)
      - ``CAIRN_MEMORY=qdrant://…`` → :class:`EpisodicMemoryStore` (Tier 3 swap-in)

    The shape is settled; only the implementations are still to land.
    """
    return EpisodicMemoryStore()
