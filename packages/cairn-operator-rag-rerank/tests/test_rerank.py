"""rag/rerank operator (#513) — identity default, top_k cap, graceful fallback.

Hermetic: no cross-encoder / cohere extras needed; the identity backend is a
pure score sort. `auto` resolves to identity when no extra is installed.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.operator import OperationContext

from operator_rag_rerank.operators.rerank import RerankOp, _resolve
from operator_rag_rerank.schemas.rerank import RerankHit, RerankInput


def _ctx() -> OperationContext:
    return OperationContext(pipeline_id="t", node_name="rag/rerank")


def _hits() -> list[RerankHit]:
    return [
        RerankHit(score=0.3, text="low", payload={"src": "a"}),
        RerankHit(score=0.9, text="high", payload={"src": "b"}),
        RerankHit(score=0.6, text="mid", payload={"src": "c"}),
    ]


@pytest.mark.asyncio
async def test_identity_sorts_by_score_and_caps_top_k():
    out = await RerankOp().run(
        RerankInput(query="q", hits=_hits(), top_k=2, reranker="identity"), _ctx()
    )
    assert out.ok and out.reranker_used == "identity"
    assert [h.score for h in out.hits] == [0.9, 0.6]      # sorted desc
    assert len(out.hits) == 2                              # capped
    assert out.hits[0].payload == {"src": "b"}            # payload carried through


@pytest.mark.asyncio
async def test_empty_hits_returns_empty():
    out = await RerankOp().run(RerankInput(query="q", hits=[], top_k=4), _ctx())
    assert out.ok and out.hits == []


@pytest.mark.asyncio
async def test_missing_extra_falls_back_to_identity():
    # explicitly ask for cross_encoder; the extra isn't installed in the test env
    out = await RerankOp().run(
        RerankInput(query="q", hits=_hits(), top_k=3, reranker="cross_encoder"), _ctx()
    )
    assert out.ok and out.reranker_used == "identity"
    assert [h.score for h in out.hits] == [0.9, 0.6, 0.3]


@pytest.mark.asyncio
async def test_mock_path_is_identity():
    out = RerankOp().mock(RerankInput(query="q", hits=_hits(), top_k=1), _ctx())
    assert out.reranker_used == "mock" and out.hits[0].score == 0.9


def test_resolve_auto_without_extras_is_identity(monkeypatch):
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    # sentence_transformers/cohere aren't installed in the hermetic test env
    assert _resolve("auto") in {"identity", "cross_encoder", "cohere"}
    assert _resolve("cohere") == "identity"  # no key/extra → identity
