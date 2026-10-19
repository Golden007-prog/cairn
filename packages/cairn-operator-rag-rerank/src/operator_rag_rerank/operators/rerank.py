"""rag/rerank — cross-corpus re-ranking of RAG hits (#513, ADR-0061).

When a knowledge role composes hits from many corpora, their similarity scores
aren't comparable across corpora; this re-scores (query, passage) so relevance,
not corpus size, decides the top-k. Backends:

  identity      — sort by the incoming score. Deterministic, no deps, the default
                  + what tests run against.
  cross_encoder — a local cross-encoder relevance model ([cross_encoder] extra).
  cohere        — Cohere Rerank API ([cohere] extra; COHERE_API_KEY).

A missing extra falls back to identity — never a hard failure. The core composer
(services/rag/inject.py) keeps its own score-fusion fallback, so this operator is
the pluggable upgrade, not a hard dependency.
"""
from __future__ import annotations

import importlib.util
import os

from cairn.core.operator import Operation, OperationContext, ReportSpec, register_operator

from operator_rag_rerank.schemas.rerank import RerankHit, RerankInput, RerankOutput

_LLM_BACKENDS = ("cross_encoder", "cohere")


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _configured_reranker() -> str:
    """The env/profile-configured reranker (#513) — the ``rag_reranker`` backend
    field / ``RAG_RERANKER``. Defaults to ``identity`` (no cost)."""
    try:
        from cairn.core.operator import get_settings

        return (get_settings().rag_reranker or "identity").lower()
    except Exception:  # noqa: BLE001
        return "identity"


def _resolve(reranker: str) -> str:
    """Resolve ``auto`` to the env/profile-configured backend (default identity,
    no cost); a backend whose extra is missing degrades to identity."""
    r = (reranker or "auto").lower()
    if r == "auto":
        r = _configured_reranker()
        if r == "auto":  # config also 'auto' → best installed, else identity
            if _importable("sentence_transformers"):
                return "cross_encoder"
            if _importable("cohere") and os.environ.get("COHERE_API_KEY"):
                return "cohere"
            return "identity"
    if r == "cross_encoder" and not _importable("sentence_transformers"):
        return "identity"
    if r == "cohere" and not (_importable("cohere") and os.environ.get("COHERE_API_KEY")):
        return "identity"
    return r if r in (*_LLM_BACKENDS, "identity") else "identity"


def _identity(hits: list[RerankHit]) -> list[RerankHit]:
    """Deterministic: sort by the incoming score, descending (stable)."""
    return sorted(hits, key=lambda h: h.score, reverse=True)


def _cross_encoder(query: str, hits: list[RerankHit], model: str) -> list[RerankHit]:
    from sentence_transformers import CrossEncoder  # optional dep: [cross_encoder]

    ce = CrossEncoder(model)
    scores = ce.predict([(query, h.text or "") for h in hits])
    rescored = [h.model_copy(update={"score": float(s)}) for h, s in zip(hits, scores)]
    return sorted(rescored, key=lambda h: h.score, reverse=True)


def _cohere(query: str, hits: list[RerankHit], model: str) -> list[RerankHit]:
    import cohere  # optional dep: [cohere]

    client = cohere.Client(os.environ["COHERE_API_KEY"])
    docs = [h.text or "" for h in hits]
    resp = client.rerank(query=query, documents=docs, top_n=len(docs),
                         model="rerank-english-v3.0")
    out: list[RerankHit] = []
    for r in resp.results:
        out.append(hits[r.index].model_copy(update={"score": float(r.relevance_score)}))
    return out


@register_operator(name="rag/rerank", version="0.1.0", capabilities=["rag.rerank"],
                   implements=["contract://rag.rerank/1.0.0"])  # #2243 V2 — cairn-contracts-rag
class RerankOp(Operation[RerankInput, RerankOutput]):
    """Re-rank retrieved hits against the query, capped to ``top_k``."""

    input_schema = RerankInput
    output_schema = RerankOutput
    supports_mock = True
    side_effect_free = True  # #836 — pure compute; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one bounded rerank pass, nothing to report

    async def run(self, inp: RerankInput, ctx: OperationContext) -> RerankOutput:
        hits = list(inp.hits)
        if not hits:
            return RerankOutput(ok=True, hits=[], reranker_used="identity")
        backend = _resolve(inp.reranker)
        try:
            if backend == "cross_encoder":
                ranked = _cross_encoder(inp.query, hits, inp.model)
            elif backend == "cohere":
                ranked = _cohere(inp.query, hits, inp.model)
            else:
                ranked, backend = _identity(hits), "identity"
        except Exception as exc:  # noqa: BLE001 — a reranker outage never breaks the pipeline
            return RerankOutput(
                ok=True, hits=_identity(hits)[: inp.top_k],
                reranker_used="identity", error=f"{type(exc).__name__}: {exc}",
            )
        return RerankOutput(ok=True, hits=ranked[: inp.top_k], reranker_used=backend)

    def mock(self, inp: RerankInput, ctx: OperationContext) -> RerankOutput:
        return RerankOutput(
            ok=True, hits=_identity(list(inp.hits))[: inp.top_k], reranker_used="mock",
        )
