"""asyncpg pool ceiling (#666, prod-caught).

The full multi-worker Temporal stack (server + main worker + remote worker) each
instantiated ~16 Postgres stores that each opened their OWN pool; unbounded,
their summed pools exhausted Postgres `max_connections` and failed the remote-job
path with `asyncpg TooManyConnectionsError` (live-reproduced 2026-07-11). The real
fix (#614 finished): every app store now routes through
`storage.postgres.pool.shared_pool`, so a process holds ONE app pool whose size is
`OBS_PG_POOL_MAX` (central cap). The `pg_pool_max` helper still bounds the two
documented direct-pool exceptions (the Postgres secret backend + pgvector).
"""
from __future__ import annotations

import os

from cairn.core.settings import pg_pool_max


def test_unset_returns_caller_default(monkeypatch):
    monkeypatch.delenv("OBS_PG_POOL_MAX", raising=False)
    assert pg_pool_max(8) == 8
    assert pg_pool_max(2) == 2  # no behavior change when unset


def test_env_caps_every_pool(monkeypatch):
    monkeypatch.setenv("OBS_PG_POOL_MAX", "2")
    assert pg_pool_max(8) == 2
    assert pg_pool_max(4) == 2
    assert pg_pool_max(2) == 2  # uniform cap regardless of the site's default


def test_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("OBS_PG_POOL_MAX", "not-an-int")
    assert pg_pool_max(4) == 4
    monkeypatch.setenv("OBS_PG_POOL_MAX", "0")
    assert pg_pool_max(4) == 1  # floored at 1 (a pool needs >= 1)


def test_cap_applied_centrally_via_shared_pool(monkeypatch):
    """#666: app stores no longer size their own pool — they route through
    ``shared_pool()``, which reads ``OBS_PG_POOL_MAX`` once (``_default_max_size``).
    So one env knob caps the whole process's app pool. (The source-level guard
    that no store re-opens its own pool lives in test_pg_shared_pool.py.)"""
    from cairn.core.storage.postgres.pool import _default_max_size

    monkeypatch.setenv("OBS_PG_POOL_MAX", "3")
    assert _default_max_size() == 3
    monkeypatch.delenv("OBS_PG_POOL_MAX", raising=False)
    assert _default_max_size() == 10  # helper's own process-wide default


def test_direct_pool_exceptions_still_cap(monkeypatch):
    """The two documented stores that keep their own pool (they can't share it)
    still bound it via pg_pool_max — the Postgres secret backend imports it."""
    import importlib

    assert hasattr(importlib.import_module("cairn.core.secrets_store"), "pg_pool_max")
