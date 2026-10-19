"""rag/search — similarity-search a named RAG corpus.

Wraps :class:`cairn.services.rag.stores.sqlite_vec.SqliteVecStore`
+ :func:`cairn.services.rag.embeddings.get_embedder`. Per-corpus
collection prefix (``rag_<corpus>``) keeps different
``[requires.rag.<corpus>]`` declarations isolated.

Mirrors the ``cairn-operator-memory-local`` shape — the only material
difference is the collection-name prefix (``rag_`` vs ``memory_``).
Phase 3 may extract the shared store-lookup helper into a substrate
library; for Phase 2 the duplicated 20 LOC keeps each operator pack
self-contained.

Failure modes bubble via ``SearchOutput.ok = False``:
  - sqlite-vec extension not installed → ``ImportError``
  - underlying SQLite I/O error → caught generically
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
from cairn.services.rag.embeddings import get_embedder, rag_collection
from cairn.services.rag.stores import build_vector_store
from cairn.services.rag.stores.base import VectorStore, hit_payload

from operator_rag_local.schemas.search import (
    SearchHit,
    SearchInput,
    SearchOutput,
)


# Per-corpus store cache, keyed by (corpus, backend). Each named corpus gets its
# own collection (``rag_<corpus>``) so collections stay disjoint. Backend follows
# ``RAG_VECTOR_STORE`` via the shared ``build_vector_store`` factory (#76) — same
# as memory_local — so RAG respects the configured backend (sqlite_vec / pgvector)
# instead of hardcoding sqlite-vec.
_STORES: dict[tuple[str, str], VectorStore] = {}


async def _store_for(corpus: str, dim: int) -> VectorStore:
    backend = (get_settings().rag_vector_store or "sqlite_vec").lower()
    # Namespace the collection by workspace (#237) so a tenant's corpus is
    # physically separate. Keying the cache by the *scoped* collection means
    # each (workspace, corpus, backend) gets its own store instance — so the
    # operator path is concurrency-safe across workspaces (each instance holds
    # its own active collection). Default workspace → bare ``rag_<corpus>``.
    # #512 — embedder-scoped collection: search resolves to the SAME collection
    # the corpus was indexed into ONLY when the active embedder matches. A changed
    # embedder → a different collection → empty (fail-closed), never cross-space garbage.
    collection = workspace_collection(rag_collection(corpus))
    key = (collection, backend)
    if key not in _STORES:
        store = build_vector_store(backend)
        await store.initialize(collection, dim)
        _STORES[key] = store
    return _STORES[key]


@register_operator(
    name="rag/search",
    version="0.1.0",
    capabilities=["rag.search"],
    implements=["contract://rag.search/1.0.0"],  # #2243 V2 — cairn-contracts-rag
)
class SearchOp(Operation[SearchInput, SearchOutput]):
    """Embed a query + return top_k matches from a named RAG corpus."""

    input_schema = SearchInput
    output_schema = SearchOutput
    supports_mock = True
    side_effect_free = True  # #836 — read only; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one embed + search call, nothing to report

    async def run(self, inp: SearchInput, ctx: OperationContext) -> SearchOutput:
        try:
            embedder = get_embedder()
            vec = await embedder.embed_one(inp.query)
            store = await _store_for(inp.corpus, embedder.dim)
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
            hits=[SearchHit(score=float(item.get("score", 0.0)), payload=hit_payload(item))
                  for item in raw],
        )

    def mock(self, inp: SearchInput, ctx: OperationContext) -> SearchOutput:
        """Return one mock chunk so template authors can develop offline."""
        return SearchOutput(
            ok=True,
            hits=[
                SearchHit(
                    score=0.88,
                    payload={
                        "mocked": True,
                        "corpus": inp.corpus,
                        "query": inp.query,
                        "source": f"{inp.corpus}/mock.md",
                        "text": f"# mock {inp.corpus}\n\nMock RAG chunk for query: {inp.query}",
                    },
                ),
            ],
        )
