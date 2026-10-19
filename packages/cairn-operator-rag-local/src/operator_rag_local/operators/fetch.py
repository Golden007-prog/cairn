"""rag/fetch — fetch every chunk associated with a known source.

Bypasses the similarity ranker — useful as a follow-up after a
``rag.search`` returned a hit whose ``source`` is known and the caller
wants the full document text rather than just the top-scoring chunk.

Implementation: VectorStore doesn't expose a "fetch by source" method
directly, so we use ``search`` with a degenerate query vector (the
embedding of the source string itself) combined with a payload-match
filter on ``source``. The store's filter clause does the heavy lifting;
the similarity ranking is irrelevant because every result has the
same source. ``top_k`` is set high to capture all chunks.

This is a Phase-2 compromise — a dedicated ``fetch_by_source`` method
on :class:`VectorStore` would be cleaner; a future refactor (when
extending the base interface anyway for filters / projections) lands
that without changing the operator surface.

Failure modes bubble via ``FetchOutput.ok = False``.
"""

from __future__ import annotations

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)
from cairn.services.rag.embeddings import get_embedder

from operator_rag_local.operators.search import _store_for, hit_payload
from operator_rag_local.schemas.fetch import (
    FetchedChunk,
    FetchInput,
    FetchOutput,
)


# Hard ceiling on the number of chunks returned for one source. A
# runbook fragmenting into thousands of chunks is a data-modeling bug;
# we cap to keep memory bounded.
_FETCH_TOP_K = 256


@register_operator(
    name="rag/fetch",
    version="0.1.0",
    capabilities=["rag.fetch"],
    implements=["contract://rag.fetch/1.0.0"],  # #2243 V2 — cairn-contracts-rag
)
class FetchOp(Operation[FetchInput, FetchOutput]):
    """Fetch all chunks whose ``payload.source`` matches the input."""

    input_schema = FetchInput
    output_schema = FetchOutput
    supports_mock = True
    side_effect_free = True  # #836 — read only; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one bounded read, nothing to report

    async def run(self, inp: FetchInput, ctx: OperationContext) -> FetchOutput:
        try:
            embedder = get_embedder()
            # Embed the source string itself — irrelevant for ranking
            # but the VectorStore.search signature requires a vector.
            vec = await embedder.embed_one(inp.source)
            store = await _store_for(inp.corpus, embedder.dim)
            raw = await store.search(
                dense=vec,
                top_k=_FETCH_TOP_K,
                filters={"source": inp.source},
            )
        except ImportError as exc:
            return FetchOutput(
                ok=False,
                error=f"sqlite-vec backend unavailable: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return FetchOutput(
                ok=False, error=f"{type(exc).__name__}: {exc}",
            )

        chunks = [
            FetchedChunk(payload=hit_payload(item))
            for item in raw
        ]
        return FetchOutput(
            ok=True,
            found=bool(chunks),
            chunks=chunks,
        )

    def mock(self, inp: FetchInput, ctx: OperationContext) -> FetchOutput:
        return FetchOutput(
            ok=True,
            found=True,
            chunks=[
                FetchedChunk(
                    payload={
                        "mocked": True,
                        "corpus": inp.corpus,
                        "source": inp.source,
                        "text": f"# {inp.source}\n\nMock fetched chunk.",
                    },
                ),
            ],
        )
