"""Incremental corpus indexer — orchestration over the existing pipeline (ADR-0023).

The pipeline (ingest_document / vector store / embedder) is faked; these tests
exercise the lifecycle logic: first-index, skip-unchanged, re-index-changed,
tombstone-removed, and per-document failure isolation.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

import cairn.services.rag.embeddings as embeddings
import cairn.services.rag.indexer as I
import cairn.services.rag.stores as stores
from cairn.core.storage.rag_catalog import InMemoryRagCatalog
from cairn.services.rag.idempotency import document_id


class _FakeStore:
    def __init__(self):
        self.deleted: list[str] = []

    async def initialize(self, collection, dim):
        self.collection = collection

    async def delete_by_source(self, source):
        self.deleted.append(source)
        return 1


class _FakeEmbedder:
    dim = 8


@pytest.fixture
def wired(monkeypatch):
    store = _FakeStore()
    ingested: list[tuple[str, str]] = []

    async def fake_ingest(path, coll):
        ingested.append((path, coll))
        return f"Stored 4 chunks from {path}"

    monkeypatch.setattr(stores, "get_vector_store", lambda: store)
    monkeypatch.setattr(embeddings, "get_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(I, "ingest_document", fake_ingest)
    return store, ingested


@pytest.mark.asyncio
async def test_first_index_then_skip_unchanged(tmp_path, wired):
    store, ingested = wired
    (tmp_path / "a.md").write_text("alpha")
    (tmp_path / "b.md").write_text("beta")
    cat = InMemoryRagCatalog()

    r1 = await I.index_corpus("c", str(tmp_path), catalog=cat)
    assert r1.status == "completed"
    assert r1.documents_indexed == 2 and r1.chunks_indexed == 8

    ingested.clear()
    r2 = await I.index_corpus("c", str(tmp_path), catalog=cat)
    assert r2.documents_skipped == 2 and r2.documents_indexed == 0
    assert ingested == []  # nothing re-embedded


@pytest.mark.asyncio
async def test_changed_file_reindexed_with_forced_delete(tmp_path, wired):
    store, ingested = wired
    f = tmp_path / "a.md"
    f.write_text("alpha")
    cat = InMemoryRagCatalog()
    await I.index_corpus("c", str(tmp_path), catalog=cat)

    f.write_text("alpha v2")
    ingested.clear()
    store.deleted.clear()
    r = await I.index_corpus("c", str(tmp_path), catalog=cat)
    assert r.documents_indexed == 1 and len(ingested) == 1
    assert str(f) in store.deleted  # delete_by_source before re-embed


@pytest.mark.asyncio
async def test_removed_file_tombstoned(tmp_path, wired):
    store, ingested = wired
    f = tmp_path / "a.md"
    f.write_text("alpha")
    cat = InMemoryRagCatalog()
    await I.index_corpus("c", str(tmp_path), catalog=cat)

    f.unlink()
    r = await I.index_corpus("c", str(tmp_path), catalog=cat)
    assert r.documents_deleted == 1
    assert str(f) in store.deleted
    entry = await cat.get(document_id("c", str(f)))
    assert entry.status == "tombstoned"


@pytest.mark.asyncio
async def test_per_document_failure_isolated(tmp_path, monkeypatch, wired):
    store, _ = wired
    (tmp_path / "a.md").write_text("alpha")
    (tmp_path / "b.md").write_text("beta")

    async def boom(path, coll):
        if path.endswith("a.md"):
            raise RuntimeError("embed failed")
        return "Stored 2 chunks from x"

    monkeypatch.setattr(I, "ingest_document", boom)
    cat = InMemoryRagCatalog()
    r = await I.index_corpus("c", str(tmp_path), catalog=cat)

    assert r.status == "partially_completed"
    assert r.documents_indexed == 1 and len(r.failed_documents) == 1
    # the failed doc is recorded as failed (so it retries next run, not skipped)
    failed_entry = await cat.get(document_id("c", str(tmp_path / "a.md")))
    assert failed_entry.status == "failed"
