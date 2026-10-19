"""The stored payload survives a vector-store read (#2563, and the earlier rag/search regression).

``VectorStore.search`` returns FLAT rows — ``{"score": …, **payload}`` — on every backend
(sqlite-vec, pgvector, Qdrant). ``rag/search``, ``rag/fetch`` and ``memory/search`` each used to
read ``row["payload"]``, a key no backend writes, so every real hit came back with an empty
payload and grounded answers silently refused. They now share ONE reading,
``cairn.services.rag.stores.base.hit_payload``. The unit tests pin that reading; the round trip
runs ``rag/fetch`` and ``rag/search`` against a real sqlite-vec store so a mock of the wrong
shape cannot hide the bug again.
"""
from __future__ import annotations

import hashlib

import pytest

from cairn.services.rag.stores.base import hit_payload


def test_flat_store_row_becomes_payload():
    """The real store shape: score + payload fields spread flat."""
    row = {
        "score": 0.83,
        "content": "A compliant README must contain Title, Table of Contents…",
        "source": "/tmp/corpus/spec.md",
        "chunk_type": "text",
    }
    assert hit_payload(row) == {
        "content": "A compliant README must contain Title, Table of Contents…",
        "source": "/tmp/corpus/spec.md",
        "chunk_type": "text",
    }


def test_a_stored_field_named_payload_is_data_not_a_wrapper():
    """One representation: the row is flat, so a record that stores a ``payload`` field keeps it."""
    row = {"score": 0.5, "payload": {"content": "hi"}, "source": "a.md"}
    assert hit_payload(row) == {"payload": {"content": "hi"}, "source": "a.md"}


def test_score_only_row_is_empty_payload():
    assert hit_payload({"score": 0.1}) == {}


class _Embedder:
    """Deterministic and offline, in the provider's shape (``dim``, ``embed_one``)."""

    dim = 8

    async def embed_one(self, text: str) -> list[float]:
        return [b / 255 for b in hashlib.sha256(text.lower().encode()).digest()[: self.dim]]


@pytest.mark.asyncio
async def test_fetch_and_search_return_the_stored_payload_from_a_real_sqlite_vec_store(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec", reason="the rag-sqlite-vec extra is not installed")
    import operator_rag_local.operators.fetch as F
    import operator_rag_local.operators.search as S
    from operator_rag_local.schemas.fetch import FetchInput
    from operator_rag_local.schemas.search import SearchInput

    monkeypatch.setenv("CAIRN_SQLITE_VEC_DB", str(tmp_path / "vec.db"))
    monkeypatch.setattr(S, "_STORES", {})
    for module in (S, F):
        monkeypatch.setattr(module, "get_embedder", _Embedder)
    chunks = [("db-1", "runbooks/db.md", "promote the replica"),
              ("db-2", "runbooks/db.md", "repoint the writers"),
              ("cache-1", "runbooks/cache.md", "shed load")]
    store = await S._store_for("runbooks", _Embedder.dim)
    await store.upsert([i for i, _, _ in chunks], [await _Embedder().embed_one(t) for _, _, t in chunks], None,
                       [{"source": src, "content": text} for _, src, text in chunks])

    try:
        fetched = await F.FetchOp().run(FetchInput(corpus="runbooks", source="runbooks/db.md"), None)
        assert fetched.ok and fetched.found, fetched
        assert sorted(c.payload["content"] for c in fetched.chunks) == ["promote the replica", "repoint the writers"]
        assert {c.payload["source"] for c in fetched.chunks} == {"runbooks/db.md"}
        assert all("score" not in c.payload for c in fetched.chunks)

        found = await S.SearchOp().run(SearchInput(corpus="runbooks", query="shed load", top_k=1), None)
        assert found.ok, found
        assert found.hits[0].payload == {"source": "runbooks/cache.md", "content": "shed load"}
    finally:
        await store.close()
