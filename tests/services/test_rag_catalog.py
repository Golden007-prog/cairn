"""RAG index catalog store — CRUD + tombstone, both tiers (ADR-0023)."""
from __future__ import annotations

import pytest

from cairn.core.storage.rag_catalog import (
    CatalogEntry,
    InMemoryRagCatalog,
    SqliteRagCatalog,
)


@pytest.fixture(params=["memory", "sqlite"])
def cat(request):
    if request.param == "memory":
        return InMemoryRagCatalog()
    return SqliteRagCatalog(path=":memory:")


@pytest.mark.asyncio
async def test_upsert_get_list(cat):
    await cat.upsert(CatalogEntry(
        document_id="d1", corpus="c", source="s1",
        content_hash="h", status="indexed",
    ))
    got = await cat.get("d1")
    assert got and got.source == "s1" and got.status == "indexed"
    assert len(await cat.list_by_corpus("c")) == 1
    assert await cat.list_by_corpus("other") == []
    assert await cat.get("missing") is None


@pytest.mark.asyncio
async def test_upsert_overwrites_same_id(cat):
    await cat.upsert(CatalogEntry(document_id="d1", corpus="c", source="s", content_hash="h1"))
    await cat.upsert(CatalogEntry(document_id="d1", corpus="c", source="s", content_hash="h2"))
    assert (await cat.get("d1")).content_hash == "h2"
    assert len(await cat.list_by_corpus("c")) == 1  # not duplicated


@pytest.mark.asyncio
async def test_tombstone_then_delete(cat):
    await cat.upsert(CatalogEntry(document_id="d1", corpus="c", source="s"))
    await cat.tombstone("d1")
    assert (await cat.get("d1")).status == "tombstoned"
    await cat.delete("d1")
    assert await cat.get("d1") is None


@pytest.mark.asyncio
async def test_list_returns_copies_not_aliases(cat):
    await cat.upsert(CatalogEntry(document_id="d1", corpus="c", source="s", chunk_count=1))
    rows = await cat.list_by_corpus("c")
    rows[0].chunk_count = 999  # mutating the returned copy must not leak back
    assert (await cat.get("d1")).chunk_count == 1


@pytest.mark.asyncio
async def test_methods_do_not_block_event_loop(tmp_path):
    """#86: catalog reads/writes run their sqlite via asyncio.to_thread — no
    event-loop blocking. Construct outside the guard (__init__ is sync startup)."""
    bb = pytest.importorskip("blockbuster")
    catalog = SqliteRagCatalog(path=str(tmp_path / "cat.db"))  # unguarded construct
    with bb.blockbuster_ctx(["cairn"]):
        await catalog.upsert(
            CatalogEntry(document_id="d1", corpus="c", source="s", content_hash="h1")
        )
        assert (await catalog.get("d1")) is not None
        assert len(await catalog.list_by_corpus("c")) == 1
        await catalog.tombstone("d1")
        await catalog.delete("d1")
