"""ADR-0058 / #427 — the storage resolver: one DI injection point.

`get_storage()` is the process-wide singleton; `StorageBackend` grows lazy
`cached_property` accessors for the config-family stores, each delegating to its
factory (which owns the single `OBS_STORAGE` routing).
"""
from __future__ import annotations

import pytest

import cairn.core.storage as storage
from cairn.core.storage.backend import StorageBackend


@pytest.fixture(autouse=True)
def _isolate_store_singletons():
    """This module accesses the caching store factories (workspace/account/
    installs/oauth/remote_jobs) under a fake ``postgres://`` env, which
    caches a Postgres store in each module-global singleton. Reset them after
    every test so that store never leaks into other tests (the project convention
    is that tests manage their own singleton resets — tests/conftest.py)."""
    yield
    storage.reset_storage()
    import cairn.core.account as _acct
    import cairn.core.registry_install as _ri
    import cairn.core.workspace as _ws
    from cairn.core.oauth.store import reset_token_store
    from cairn.core.operator.job_store import reset_remote_job_store

    _ws._store = None
    _acct._store = None
    _ri._store = None
    reset_token_store()
    reset_remote_job_store()


def _bare() -> StorageBackend:
    # The 4 eager engine-data fields aren't exercised here — the config-family
    # accessors are independent lazy properties.
    return StorageBackend(runs=None, memory=None, rag=None, rag_catalog=None)


def test_get_storage_is_a_singleton(monkeypatch):
    storage.reset_storage()
    sentinel = _bare()
    monkeypatch.setattr(storage, "build_storage", lambda: sentinel)
    assert storage.get_storage() is sentinel
    assert storage.get_storage() is sentinel  # cached, not rebuilt


def test_reset_storage_rebuilds(monkeypatch):
    storage.reset_storage()
    monkeypatch.setattr(storage, "build_storage", _bare)
    first = storage.get_storage()
    storage.reset_storage()
    assert storage.get_storage() is not first


def test_config_family_lazy_and_cached(monkeypatch):
    # Under a postgres selector every config-family accessor routes to its
    # Postgres backend — constructed lazily (no connection), so a type check is
    # safe. And the accessor caches (same object on re-access).
    monkeypatch.setenv("OBS_STORAGE", "postgres://u:p@h:5432/db")
    b = _bare()
    pol = b.policy
    assert type(pol).__name__ == "PostgresPolicyStore"
    assert b.policy is pol  # cached_property caches
    assert type(b.connections).__name__ == "PostgresConnectionStore"
    assert type(b.workspace).__name__ == "PostgresWorkspaceStore"
    assert type(b.account).__name__ == "PostgresAccountStore"
    assert type(b.triggers).__name__ == "PostgresTriggerStore"
    assert type(b.remote_jobs).__name__ == "PostgresRemoteJobStore"


def test_config_family_sqlite_default(monkeypatch):
    # No OBS_STORAGE → durable sqlite backends (not the postgres ones).
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    b = _bare()
    assert "Postgres" not in type(b.policy).__name__
    assert "Postgres" not in type(b.connections).__name__
