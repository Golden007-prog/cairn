"""#1904 — a Postgres store must not publish its pool before its schema exists.

The whole family is lazy: the first call that needs the database creates the pool
and runs its ``CREATE TABLE IF NOT EXISTS`` statements. Nearly all of them used to
publish ``self._pool`` BEFORE the DDL, so the second of two concurrent first
requests got a pool and queried a table that was not there yet. #1833 caught it
live on a fresh cp box: two 500s on the console's first paint
(``relation "input_presets" does not exist``) and never again — the kind of bug a
warm dev database can never show.

This drives the real thing against a REAL, FRESH Postgres database, one test per
store. ``shared_pool`` is wrapped so that the FIRST connection acquire — always
the schema DDL — parks on a gate; a second caller then gets every chance to sneak
past. The assertion is the invariant, not a timing hope: while the schema step is
parked, the second caller must still be waiting, and when it is released its very
first real query against one of the store's own tables must succeed.

The second test here has nothing to do with Postgres and needs no server: the
shared mixin is a module-top import in 23 stores, so where it LIVES is part of the
fix — see :mod:`cairn.core.pg_schema_init`.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.postgres import pool as pgpool


def _admin_dsn() -> str:
    return f"postgres://{getpass.getuser()}@localhost:5432/postgres"


def _pg_reachable() -> bool:
    try:
        import psycopg
        with psycopg.connect(_admin_dsn(), connect_timeout=2):
            return True
    except Exception:  # noqa: BLE001 — no server, no integration test
        return False


needs_pg = pytest.mark.skipif(not _pg_reachable(), reason="no local Postgres on :5432")


@pytest.fixture
async def fresh_dsn():
    """A database created for this test and dropped after it — the point of the
    issue is that only a FRESH database can show the bug."""
    import psycopg
    db = f"cairn_1904_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(_admin_dsn(), autocommit=True) as c:
        c.execute(f"CREATE DATABASE {db}")
    try:
        yield f"postgres://{getpass.getuser()}@localhost:5432/{db}"
    finally:
        await pgpool.reset_pools()
        with psycopg.connect(_admin_dsn(), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


class _GatedPool:
    """The real asyncpg pool, except that its FIRST ``acquire()`` — which is
    always the schema DDL on a store's first use — parks until released."""

    def __init__(self, pool: Any, arrived: asyncio.Event, release: asyncio.Event) -> None:
        self._pool, self._arrived, self._release = pool, arrived, release
        self._first = True

    def acquire(self, *a: Any, **k: Any) -> Any:
        if self._first:
            self._first = False
            return self._gated_acquire(*a, **k)
        return self._pool.acquire(*a, **k)

    @asynccontextmanager
    async def _gated_acquire(self, *a: Any, **k: Any):
        self._arrived.set()
        await self._release.wait()
        async with self._pool.acquire(*a, **k) as conn:
            yield conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


def _make_installs(dsn):
    from cairn.core.registry_install import PostgresInstallStore
    return PostgresInstallStore(dsn)


def _make_profiles(dsn):
    from cairn.core.profile_store import PostgresProfileStore
    return PostgresProfileStore(dsn)


def _make_workflow_registry(dsn):
    from cairn.core.workflow_registry import PostgresWorkflowRegistry
    return PostgresWorkflowRegistry(dsn)


def _make_connections(dsn):
    from cairn.core.connections import PostgresConnectionStore
    return PostgresConnectionStore(dsn)


def _make_features(dsn):
    from cairn.core.features import PostgresFeatureStore
    return PostgresFeatureStore(dsn)


def _make_budget(dsn):
    from cairn.core.budget_reservations import _PostgresBackend
    return _PostgresBackend(dsn)


def _make_workspaces(dsn):
    from cairn.core.workspace import PostgresWorkspaceStore
    return PostgresWorkspaceStore(dsn)


def _make_accounts(dsn):
    from cairn.core.account import PostgresAccountStore
    return PostgresAccountStore(dsn)


def _make_oauth(dsn):
    from cairn.core.oauth.store import PostgresOAuthTokenStore
    return PostgresOAuthTokenStore(dsn)


def _make_triggers(dsn):
    from cairn.core.storage.trigger_store import PostgresTriggerStore
    return PostgresTriggerStore(dsn)


def _make_schedules(dsn):
    from cairn.core.storage.schedule_store import PostgresScheduleStore
    return PostgresScheduleStore(dsn)


def _make_rag_catalog(dsn):
    from cairn.core.storage.rag_catalog import PostgresRagCatalog
    return PostgresRagCatalog(dsn)


def _make_eval_ledger(dsn):
    from cairn.core.storage.eval_ledger import PostgresEvalLedger
    return PostgresEvalLedger(dsn)


def _make_runs(dsn):
    from cairn.core.storage.postgres.runs import PostgresRunStore
    return PostgresRunStore(dsn)


def _make_corpora(dsn):
    from cairn.core.storage.knowledge_corpora import PostgresWorkspaceCorpusStore
    return PostgresWorkspaceCorpusStore(dsn)


def _make_pipelines(dsn):
    from cairn.core.storage.pipeline_store import PostgresPipelineStore
    return PostgresPipelineStore(dsn)


def _make_policies(dsn):
    from cairn.core.policy.store import PostgresPolicyStore
    return PostgresPolicyStore(dsn)


def _make_jobs(dsn):
    from cairn.core.operator.job_store import PostgresRemoteJobStore
    return PostgresRemoteJobStore(dsn)


def _make_bus(dsn):
    from cairn.core.bus.postgres import PostgresBus
    return PostgresBus(dsn)


def _make_workflow_config(dsn):
    from cairn.core.workflow_config_store import PostgresWorkflowConfigStore
    return PostgresWorkflowConfigStore(dsn)


def _make_workspace_variables(dsn):
    from cairn.core.workspace_variables import PostgresWorkspaceVariableStore
    return PostgresWorkspaceVariableStore(dsn)


# (id, factory, one table the store's own schema step creates)
STORES = [
    ("registry_install", _make_installs, "installs"),
    ("profile_store", _make_profiles, "profiles"),
    ("workflow_registry", _make_workflow_registry, "workflow_templates"),
    ("connections", _make_connections, "connections"),
    ("features", _make_features, "features"),
    ("budget_reservations", _make_budget, "series_reservations"),
    ("workspace", _make_workspaces, "workspaces"),
    ("account", _make_accounts, "accounts"),
    ("oauth", _make_oauth, "oauth_tokens"),
    ("triggers", _make_triggers, "triggers"),
    ("schedules", _make_schedules, "schedules"),
    ("rag_catalog", _make_rag_catalog, "rag_catalog"),
    ("eval_ledger", _make_eval_ledger, "eval_runs"),
    ("runs", _make_runs, "runs"),
    ("knowledge_corpora", _make_corpora, "workspace_corpora"),
    ("pipeline_store", _make_pipelines, "pipelines"),
    ("policy_store", _make_policies, "policies"),
    ("remote_jobs", _make_jobs, "remote_jobs"),
    ("bus", _make_bus, "bus_messages"),
    # the two that already sequenced it correctly by hand — here so the shared
    # mixin is proven to keep the behaviour #1833 bought, not just to add it
    ("workflow_config", _make_workflow_config, "workflow_configs"),
    ("workspace_variables", _make_workspace_variables, "workspace_variables"),
]


@pytest.mark.parametrize("module", sorted({
    "cairn.core.account", "cairn.core.workspace", "cairn.core.connections",
    "cairn.core.budget_reservations", "cairn.core.features",
    "cairn.core.storage.postgres.runs", "cairn.core.bus.postgres",
}))
def test_every_mixin_importer_imports_first_without_a_cycle(module):
    """The mixin is a module-top import in 23 stores, several of which sit BELOW
    ``cairn.core.storage`` in the import graph — so it must not live under that
    package (its ``__init__`` reaches back down to ``cairn.core.account``). A
    fresh interpreter per module is the only honest check: a cycle here is
    invisible once some other test has already imported the chain in a lucky
    order."""
    r = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True)
    assert r.returncode == 0, f"importing {module} first failed:\n{r.stderr[-2000:]}"


@needs_pg
@pytest.mark.parametrize(("name", "factory", "table"), STORES, ids=[s[0] for s in STORES])
async def test_pool_is_not_observable_before_the_schema(monkeypatch, fresh_dsn, name, factory, table):
    arrived, release = asyncio.Event(), asyncio.Event()
    real_shared_pool = pgpool.shared_pool

    async def gated_shared_pool(dsn: str, **kw: Any) -> Any:
        return _GatedPool(await real_shared_pool(dsn, **kw), arrived, release)

    monkeypatch.setattr(pgpool, "shared_pool", gated_shared_pool)

    store = factory(fresh_dsn)
    first = asyncio.create_task(store._get_pool())
    await asyncio.wait_for(arrived.wait(), 15)  # the schema step is parked

    async def second_caller() -> list:
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608 — literal from STORES

    second = asyncio.create_task(second_caller())
    try:
        await asyncio.sleep(0.2)  # every chance to sneak past, on a real server
        if second.done():
            pytest.fail(
                f"{name}: a second caller got the pool while the schema was still "
                f"being created — that is the first-request 500 on a fresh "
                f"database. Its first query raised {second.exception()!r}"
            )
    finally:
        release.set()  # never leave the first caller parked

    await asyncio.wait_for(first, 15)
    # the point is that it does not raise UndefinedTableError; a couple of these
    # schema steps seed a row (workspaces, accounts), so the count is not the assertion
    assert isinstance(await asyncio.wait_for(second, 15), list)
    assert store._pool is not None and store._schema_ready is True
