"""memory/write — upsert a record into a named memory store.

Wraps :class:`cairn.services.rag.stores.sqlite_vec.SqliteVecStore`
+ :func:`cairn.services.rag.embeddings.get_embedder`. Per-store
collection isolation matches :mod:`operator_memory_local.operators.search`
— both operators reuse the cached store instance for a given name.

Stable-id derivation: when the caller omits ``id``, we hash
``embed_text`` to keep upserts idempotent. The underlying
:class:`SqliteVecStore` uses (collection, id) as the upsert key.
"""

from __future__ import annotations

import hashlib

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_memory_local.operators.search import _store_for
from operator_memory_local.schemas.write import WriteInput, WriteOutput


def _stable_id(seed: str) -> str:
    """Match cairn/services/memory/store.py's id-derivation shape so
    records stay addressable by the same id regardless of which layer
    wrote them. 16 hex chars is enough collision-resistance for a
    local-dev store."""
    return hashlib.sha256(seed.lower().encode()).hexdigest()[:16]


@register_operator(
    name="memory/write",
    version="0.1.0",
    capabilities=["memory.write"],
    implements=["contract://memory.write/1.0.0"],  # #2243 V2 — cairn-contracts-memory
)
class WriteOp(Operation[WriteInput, WriteOutput]):
    """Upsert ``record`` into the named memory store with embedding of
    ``embed_text``."""

    input_schema = WriteInput
    output_schema = WriteOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    risk = 'production_mutation'
    supports_mock = True
    reports = ReportSpec.NONE  # ADR-0103: one embed + upsert call, nothing to report

    async def run(self, inp: WriteInput, ctx: OperationContext) -> WriteOutput:
        from cairn.services.rag.embeddings import get_embedder

        try:
            embedder = get_embedder()
            vec = await embedder.embed_one(inp.embed_text)
            store = await _store_for(inp.store, embedder.dim)
            record_id = inp.id or _stable_id(inp.embed_text)
            await store.upsert(
                ids=[record_id],
                dense=[vec],
                sparse=None,
                payloads=[inp.record],
            )
        except ImportError as exc:
            return WriteOutput(
                ok=False,
                error=f"sqlite-vec backend unavailable: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return WriteOutput(
                ok=False, error=f"{type(exc).__name__}: {exc}",
            )

        return WriteOutput(ok=True, id=record_id)

    def mock(self, inp: WriteInput, ctx: OperationContext) -> WriteOutput:
        return WriteOutput(
            ok=True,
            id=inp.id or _stable_id(inp.embed_text),
        )
