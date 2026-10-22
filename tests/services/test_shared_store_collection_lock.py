"""One collection operation at a time on a shared vector store.

The process-wide RAG store (``get_vector_store``) and the episodic-memory store
are each ONE instance used for every workspace's collection, and a store keeps
the collection its next operation acts on as instance state. These tests force
the worst interleaving deterministically: the first caller's store operation
pauses until a second caller (another workspace) has selected its own collection
on the same store — or until a short timeout, when the lock keeps the second
caller out — and then reports which collection it actually acted on.

Paths covered: ``ingest_document`` against itself, ``query.search`` against
itself, an ``index_corpus`` run (which nests ``ingest_document``) against
``query.search``, and the memory service's ``recall_episodic`` against
``save_episodic``.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import asyncio
import contextvars
import types

import pytest

from cairn.core import workspace as ws

A, B = "team-a", "team-b"
_HOLD_S = 0.2  # how long the first operation waits for the other caller to cut in


class _SharedStore:
    """A fake store shared by both callers. The first collection selected is the
    "first caller's"; its first operation pauses until another collection has been
    selected (or ``_HOLD_S`` passes), then records the collection it acted on."""

    def __init__(self):
        self._collection = None
        self.first = None
        self.other_selected = asyncio.Event()
        self.acted: list[tuple[str, str]] = []  # (operation, collection acted on)
        self._held = False

    async def initialize(self, collection, dim):
        self._collection = collection
        if self.first is None:
            self.first = collection
        elif collection != self.first:
            self.other_selected.set()

    async def _act(self, op):
        if self._collection == self.first and not self._held:
            self._held = True
            try:
                await asyncio.wait_for(self.other_selected.wait(), _HOLD_S)
            except TimeoutError:
                pass
        self.acted.append((op, self._collection))
        return self._collection

    async def has_source(self, source):
        await self._act(f"has_source:{source}")
        return False

    async def upsert(self, ids, dense, sparse, payloads):
        await self._act(f"upsert:{payloads[0].get('source', '')}")
        return len(ids)

    async def search(self, dense, sparse=None, top_k=5, filters=None):
        coll = await self._act("search")
        return [{"score": 1.0, "content": f"chunk of {coll}", "source": coll, "type": "episodic_memory"}]

    async def list_sources(self, limit=50):
        await self._act("list_sources")
        return []

    async def delete_by_source(self, source):
        await self._act(f"delete:{source}")
        return 0


class _Embedder:
    dim = 4

    async def embed(self, texts):
        return [[0.5] * self.dim for _ in texts]

    async def embed_one(self, text):
        return [0.5] * self.dim

    async def embed_sparse(self, texts):
        return None

    async def embed_sparse_one(self, text):
        return None


async def _no_connection(*_a, **_k):
    return None


async def _in_workspace(workspace_id, coro_fn):
    tok = ws.set_current_workspace(workspace_id)
    try:
        return await coro_fn()
    finally:
        ws.reset_current_workspace(tok)


async def _first_then_second(first, second):
    """Start ``first``, let it reach its store operation, then start ``second``."""
    t1 = asyncio.create_task(first())
    for _ in range(50):
        await asyncio.sleep(0)
    t2 = asyncio.create_task(second())
    return await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_concurrent_ingests_of_two_workspaces_write_to_their_own_collections(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core")
    import cairn.services.rag.ingest as ingest

    store, emb = _SharedStore(), _Embedder()
    monkeypatch.setattr(ingest, "get_vector_store", lambda: store)
    monkeypatch.setattr(ingest, "get_embedder", lambda: emb)
    monkeypatch.setattr(ingest, "apply_backend_connection", _no_connection)
    monkeypatch.setattr(ingest, "get_settings", lambda: types.SimpleNamespace(
        llama_cloud_api_key=None, rag_collection_name="kb"))
    doc_a, doc_b = tmp_path / "a.md", tmp_path / "b.md"
    doc_a.write_text("# A\n\nTeam A notes.\n", encoding="utf-8")
    doc_b.write_text("# B\n\nTeam B notes.\n", encoding="utf-8")

    await _first_then_second(
        lambda: _in_workspace(A, lambda: ingest.ingest_document(str(doc_a), "rag_notes")),
        lambda: _in_workspace(B, lambda: ingest.ingest_document(str(doc_b), "rag_notes")),
    )
    expected = {str(doc_a): "ws_team_a__rag_notes", str(doc_b): "ws_team_b__rag_notes"}
    acted = [(op, coll) for op, coll in store.acted if ":" in op and op.split(":", 1)[1] in expected]
    assert acted, store.acted
    for op, coll in acted:
        assert coll == expected[op.split(":", 1)[1]], (op, coll, store.acted)


@pytest.mark.asyncio
async def test_concurrent_queries_of_two_workspaces_read_their_own_collections(monkeypatch):
    pytest.importorskip("llama_index.core")
    import cairn.core.llm.client as llm_client
    import cairn.services.rag.query as query

    store, emb = _SharedStore(), _Embedder()
    monkeypatch.setattr(query, "get_vector_store", lambda: store)
    monkeypatch.setattr(query, "get_embedder", lambda: emb)
    monkeypatch.setattr(query, "apply_backend_connection", _no_connection)

    async def _llm(**_k):
        msg = types.SimpleNamespace(content="answer")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(llm_client, "llm_complete", _llm)

    res_a, res_b = await _first_then_second(
        lambda: _in_workspace(A, lambda: query.search("q", collection="rag_notes")),
        lambda: _in_workspace(B, lambda: query.search("q", collection="rag_notes")),
    )
    assert {s["source"] for s in res_a["sources"]} == {"ws_team_a__rag_notes"}, store.acted
    assert {s["source"] for s in res_b["sources"]} == {"ws_team_b__rag_notes"}, store.acted


@pytest.mark.asyncio
async def test_an_index_run_and_a_query_of_two_workspaces_stay_in_their_collections(monkeypatch, tmp_path):
    """index_corpus in A (which nests ingest_document) against query.search in B."""
    pytest.importorskip("llama_index.core")
    import cairn.core.llm.client as llm_client
    import cairn.services.rag.embeddings as embeddings
    import cairn.services.rag.indexer as indexer
    import cairn.services.rag.ingest as ingest
    import cairn.services.rag.query as query
    import cairn.services.rag.stores as stores
    from cairn.core.storage.rag_catalog import InMemoryRagCatalog

    store, emb = _SharedStore(), _Embedder()
    for module, attr, value in (
        (stores, "get_vector_store", lambda: store), (ingest, "get_vector_store", lambda: store),
        (query, "get_vector_store", lambda: store), (embeddings, "get_embedder", lambda: emb),
        (ingest, "get_embedder", lambda: emb), (query, "get_embedder", lambda: emb),
        (indexer, "apply_backend_connection", _no_connection), (ingest, "apply_backend_connection", _no_connection),
        (query, "apply_backend_connection", _no_connection),
        (ingest, "get_settings", lambda: types.SimpleNamespace(llama_cloud_api_key=None, rag_collection_name="kb")),
    ):
        monkeypatch.setattr(module, attr, value)

    async def _llm(**_k):
        msg = types.SimpleNamespace(content="answer")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(llm_client, "llm_complete", _llm)
    docs = tmp_path / "a-docs"
    docs.mkdir()
    (docs / "notes.md").write_text("# A\n\nTeam A notes.\n", encoding="utf-8")

    result, answer = await _first_then_second(
        lambda: _in_workspace(A, lambda: indexer.index_corpus(
            "notes", str(docs), catalog=InMemoryRagCatalog(), collection="rag_notes")),
        lambda: _in_workspace(B, lambda: query.search("q", collection="rag_notes")),
    )
    assert result.documents_indexed == 1, result
    a_ops = [c for op, c in store.acted if op != "search"]
    assert a_ops and set(a_ops) == {"ws_team_a__rag_notes"}, store.acted
    assert {s["source"] for s in answer["sources"]} == {"ws_team_b__rag_notes"}, store.acted


@pytest.mark.asyncio
async def test_concurrent_memory_recall_and_save_of_two_workspaces_stay_in_their_collections(monkeypatch):
    import cairn.services.memory.store as mem

    store, emb = _SharedStore(), _Embedder()
    monkeypatch.setattr(mem, "_get_store", lambda: store)
    monkeypatch.setattr(mem, "get_embedder", lambda: emb)
    monkeypatch.setattr(mem, "apply_backend_connection", _no_connection)

    recalled, saved = await _first_then_second(
        lambda: _in_workspace(A, lambda: mem.recall_episodic("q")),
        lambda: _in_workspace(B, lambda: mem.save_episodic({"note": "b"}, embed_text="b")),
    )
    assert {h["source"] for h in recalled} == {"ws_team_a__obs_memories"}, store.acted
    assert [c for op, c in store.acted if op.startswith("upsert:")] == ["ws_team_b__obs_memories"], store.acted


@pytest.mark.asyncio
async def test_a_caller_cancelled_while_waiting_never_holds_the_lock():
    import threading

    from cairn.services.rag.stores import on_collection

    lock = threading.Lock()
    store = _SharedStore()
    async with on_collection(store, "c1", 4, lock):
        # another caller: a task that does not inherit this block's context
        waiter = asyncio.create_task(_enter(store, "c2", lock), context=contextvars.Context())
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert not lock.locked()
    async with on_collection(store, "c3", 4, lock):
        assert store._collection == "c3"


async def _enter(store, collection, lock):
    from cairn.services.rag.stores import on_collection

    async with on_collection(store, collection, 4, lock):
        pass
