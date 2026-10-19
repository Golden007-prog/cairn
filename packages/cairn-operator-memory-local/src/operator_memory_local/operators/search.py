"""memory/search — similarity-search a named memory store.

Wraps the configured :class:`cairn.services.rag.stores.base.VectorStore`
backend (default sqlite_vec; ``RAG_VECTOR_STORE`` selects it) +
:func:`cairn.services.rag.embeddings.get_embedder`. Per-pack collection
isolation: the operator uses the ``store`` field to scope the underlying
collection name so two memory stores (e.g. ``similar_incidents`` and
``service_runbooks``) within the same deployment don't collide.

Backend resolution goes through the same ``build_vector_store`` factory
the memory *service* uses (#76), so memory honors the deployment's vector
backend (sqlite_vec for dev, pgvector for prod) instead of always sqlite.

Failure modes bubble via ``SearchOutput.ok = False``:
  - vector backend unavailable (e.g. sqlite-vec extension not installed)
    → ``ImportError`` from the store guard, surfaced as ``error``
  - underlying store I/O error → caught generically with
    ``type(e).__name__``
"""

from __future__ import annotations

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)
from cairn.core.operator import get_settings
from cairn.core.operator import workspace_collection
from cairn.services.rag.embeddings import get_embedder
from cairn.services.rag.stores import build_vector_store
from cairn.services.rag.stores.base import VectorStore, hit_payload

from operator_memory_local.schemas.search import (
    SearchHit,
    SearchInput,
    SearchOutput,
)


# Cached per (store-name, backend): each named store keeps a disjoint
# ``memory_<name>`` collection, and keying on the backend too means a
# RAG_VECTOR_STORE switch (re-config / tests) yields a fresh store rather
# than a stale-backend one.
_STORES: dict[tuple[str, str], VectorStore] = {}


async def _store_for(name: str, dim: int) -> VectorStore:
    """Return the VectorStore for a named memory store, scoping the
    collection to ``memory_<name>``. Backend follows ``RAG_VECTOR_STORE``
    via the shared ``build_vector_store`` factory (#76) — the same one the
    memory service uses, so memory respects the configured backend."""
    backend = (get_settings().rag_vector_store or "sqlite_vec").lower()
    # Namespace by workspace (#237): each (workspace, store-name, backend) gets
    # its own store instance keyed by the scoped collection, so one tenant never
    # recalls another's memories and the operator path stays concurrency-safe
    # across workspaces. Default workspace → bare ``memory_<name>``.
    collection = workspace_collection(f"memory_{name}")
    key = (collection, backend)
    if key not in _STORES:
        store = build_vector_store(backend)
        await store.initialize(collection, dim)
        _STORES[key] = store
    return _STORES[key]


@register_operator(
    name="memory/search",
    version="0.1.0",
    capabilities=["memory.search"],
    implements=["contract://memory.search/1.0.0"],  # #2243 V2 — cairn-contracts-memory
)
class SearchOp(Operation[SearchInput, SearchOutput]):
    """Embed a query + return top_k matches from a named memory store."""

    input_schema = SearchInput
    output_schema = SearchOutput
    supports_mock = True
    side_effect_free = True  # #836 — read only; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one embed + search call, nothing to report

    async def run(self, inp: SearchInput, ctx: OperationContext) -> SearchOutput:
        try:
            embedder = get_embedder()
            vec = await embedder.embed_one(inp.query)
            store = await _store_for(inp.store, embedder.dim)
            raw = await store.search(
                dense=vec,
                top_k=inp.top_k,
                filters=inp.filters or None,
            )
        except ImportError as exc:
            return SearchOutput(
                ok=False,
                error=f"sqlite-vec backend unavailable: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return SearchOutput(
                ok=False, error=f"{type(exc).__name__}: {exc}",
            )

        return SearchOutput(
            ok=True,
            hits=[
                SearchHit(
                    score=float(item.get("score", 0.0)),
                    payload=hit_payload(item),
                )
                for item in raw
            ],
        )

    def mock(self, inp: SearchInput, ctx: OperationContext) -> SearchOutput:
        """Return one fixed mock hit so template authors can develop offline."""
        return SearchOutput(
            ok=True,
            hits=[
                SearchHit(
                    score=0.92,
                    payload={
                        "mocked": True,
                        "store": inp.store,
                        "query": inp.query,
                    },
                ),
            ],
        )
