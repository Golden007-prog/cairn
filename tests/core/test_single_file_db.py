"""Single shared sqlite file for the durable config-family stores (#427/ADR-0058
decision #5). Table names across these stores are unique, so one file
(``~/.cairn/cairn.db``) is safe to share instead of one file per store.

Covers: with no per-store env override and no OBS_SQLITE_DB override, each
store's resolved sqlite path lands on the shared ``cairn.db``; the per-store
``OBS_*_DB`` override still wins over the shared default.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.account import SqliteAccountStore
from cairn.core.connections import SqliteConnectionStore
from cairn.core.policy.store import SqlitePolicyStore
from cairn.core.profile_store import SqliteProfileStore
from cairn.core.workflow_registry import SqliteWorkflowRegistry

# Every per-store override knob touched by this test, plus the storage-url
# selector and the new blanket override — all cleared per sub-test so a
# lingering env var (repo .env, a prior test) can't mask the default path.
_ENV_KEYS = [
    "OBS_STORAGE",
    "OBS_SQLITE_DB",
    "OBS_POLICIES_DB",
    "OBS_CONNECTIONS_DB",
    "OBS_PROFILES_DB",
    "OBS_ACCOUNT_DB",
    "OBS_WORKFLOW_REGISTRY_DB",
]


def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_policy_store_defaults_to_shared_cairn_db(monkeypatch):
    _clear_env(monkeypatch)
    store = SqlitePolicyStore()
    try:
        assert store.path.endswith("cairn.db")
    finally:
        store.close()


def test_connection_store_defaults_to_shared_cairn_db(monkeypatch):
    _clear_env(monkeypatch)
    store = SqliteConnectionStore()
    try:
        assert store.path.endswith("cairn.db")
    finally:
        store.close()


def test_profile_store_defaults_to_shared_cairn_db(monkeypatch):
    _clear_env(monkeypatch)
    store = SqliteProfileStore()
    try:
        assert store.path.endswith("cairn.db")
    finally:
        store.close()


def test_account_store_defaults_to_shared_cairn_db(monkeypatch):
    _clear_env(monkeypatch)
    store = SqliteAccountStore()
    try:
        assert store.path.endswith("cairn.db")
    finally:
        store.close()


def test_workflow_registry_defaults_to_shared_cairn_db(monkeypatch):
    _clear_env(monkeypatch)
    store = SqliteWorkflowRegistry()
    try:
        assert store.path.endswith("cairn.db")
    finally:
        store.close()


def test_obs_sqlite_db_override_applies_to_every_store(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    shared = str(tmp_path / "shared-override.db")
    monkeypatch.setenv("OBS_SQLITE_DB", shared)
    store = SqlitePolicyStore()
    try:
        assert store.path == shared
    finally:
        store.close()
    store2 = SqliteConnectionStore()
    try:
        assert store2.path == shared
    finally:
        store2.close()


def test_per_store_override_still_wins_over_shared_default(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    override_path = str(tmp_path / "x.db")
    monkeypatch.setenv("OBS_POLICIES_DB", override_path)
    store = SqlitePolicyStore()
    try:
        assert store.path == override_path
        assert not store.path.endswith("cairn.db")
    finally:
        store.close()


def test_per_store_override_wins_even_when_blanket_override_also_set(monkeypatch, tmp_path):
    # per-store OBS_POLICIES_DB is documented as first precedence — it must
    # win even over a simultaneously-set OBS_SQLITE_DB blanket override.
    _clear_env(monkeypatch)
    override_path = str(tmp_path / "y.db")
    monkeypatch.setenv("OBS_SQLITE_DB", str(tmp_path / "shared.db"))
    monkeypatch.setenv("OBS_POLICIES_DB", override_path)
    store = SqlitePolicyStore()
    try:
        assert store.path == override_path
    finally:
        store.close()
