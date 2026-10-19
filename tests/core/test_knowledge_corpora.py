"""Workspace (BYO) knowledge corpus store — Slice / #516.

The persistence foundation for drop-in knowledge (source = a governed artifact
ref, account + workspace scoped). Covers CRUD + the isolation-relevant scoping.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.knowledge_corpora import (
    InMemoryWorkspaceCorpusStore,
    SqliteWorkspaceCorpusStore,
    WorkspaceCorpus,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_store", [
    lambda: InMemoryWorkspaceCorpusStore(),
    lambda: SqliteWorkspaceCorpusStore(path=":memory:"),
])
async def test_crud_roundtrip(make_store):
    store = make_store()
    c = WorkspaceCorpus(account_id="acme", workspace_id="prod", name="runbooks",
                        source_uri="s3://acme-kb/runbooks", role="incident",
                        license="internal", attribution="ACME SRE")
    await store.add(c)

    got = await store.get(c.corpus_id)
    assert got.name == "runbooks" and got.source_uri == "s3://acme-kb/runbooks"
    assert got.role == "incident" and got.account_id == "acme"

    assert len(await store.list()) == 1
    assert (await store.list())[0].corpus_id == c.corpus_id

    assert await store.delete(c.corpus_id) is True
    assert await store.get(c.corpus_id) is None
    assert await store.delete("nope") is False


@pytest.mark.asyncio
async def test_list_scoped_by_workspace():
    """Workspace scoping is the isolation boundary — list(ws) never returns
    another workspace's corpora."""
    store = InMemoryWorkspaceCorpusStore()
    await store.add(WorkspaceCorpus(account_id="acme", workspace_id="prod", name="a", role="r"))
    await store.add(WorkspaceCorpus(account_id="acme", workspace_id="staging", name="b", role="r"))
    await store.add(WorkspaceCorpus(account_id="globex", workspace_id="prod", name="c", role="r"))

    prod = await store.list(workspace_id="prod")
    assert {c.name for c in prod} == {"a", "c"}          # only workspace=prod
    assert len(await store.list()) == 3                   # unscoped sees all (admin)
    assert await store.list(workspace_id="staging") == [*(await store.list(workspace_id="staging"))]
    assert {c.name for c in await store.list(workspace_id="staging")} == {"b"}


def test_factory_routes_to_postgres_on_obs_storage(monkeypatch):
    """OBS_STORAGE=postgres://… selects the Postgres tier (lazy pool → no live
    DB needed to construct it)."""
    import cairn.core.storage.knowledge_corpora as kc
    monkeypatch.setattr(kc, "_STORE", None)
    monkeypatch.setenv("OBS_STORAGE", "postgres://u:p@localhost/db")
    store = kc.get_workspace_corpus_store()
    assert isinstance(store, kc.PostgresWorkspaceCorpusStore)
    monkeypatch.setattr(kc, "_STORE", None)


@pytest.mark.asyncio
async def test_postgres_list_sync_reads_from_cache():
    """The PG tier's sync read (role-composition hot path) is served from the
    add/list-warmed cache — hermetic, no live DB."""
    from cairn.core.storage.knowledge_corpora import PostgresWorkspaceCorpusStore
    store = PostgresWorkspaceCorpusStore(dsn="postgres://x")
    store._cache = {  # simulate a warmed cache (what add()/list() populate)
        "a": WorkspaceCorpus(corpus_id="a", workspace_id="prod", name="a", role="r"),
        "b": WorkspaceCorpus(corpus_id="b", workspace_id="staging", name="b", role="r"),
    }
    assert {c.name for c in store.list_sync("prod")} == {"a"}
    assert len(store.list_sync()) == 2
