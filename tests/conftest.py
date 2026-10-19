"""Suite-wide fixtures + env defaults.

OPENAI_API_KEY: provider settings validate a key at import in several paths;
the suite must run offline, so we default a dummy here ONCE instead of the
historical per-file ``os.environ.setdefault`` copies (47 of them — kept in
place since they're harmless, but new test files should rely on this).

Singleton hygiene: modules with cached singletons expose reset helpers — use
them in tests that mutate the env they were built from:

  - ``cairn.core.obs.redact.reset_redactor()``
  - ``cairn.core.obs.audit.reset_audit_sink()``
  - ``cairn.core.bus.reset_bus()``
  - ``cairn.core.runtime.checkpointer.reset_checkpointer()``
  - ``cairn.services.rag.embeddings.reset_embedder()``
  - ``cairn.services.llm.gateway.reset_gateway()``
  - ``cairn.services.rag.stores.reset_vector_store()``
  - ``cairn.core.provider_settings.get_settings.cache_clear()``

No autouse cache-clearing fixture on purpose: the suite's tests manage their
own resets, and indiscriminate clearing between tests would re-run provider
construction 950+ times for no benefit.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
# typer forces a rich terminal (ANSI codes, boxed panels) when GITHUB_ACTIONS, FORCE_COLOR or
# PY_COLORS is set, read once when typer.rich_utils is imported. Tests that read `--help`
# output must see plain text on every runner, so switch the forcing off before typer loads.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")

# #1199 — the repo .env must not steer the suite. Two leak paths existed:
# (a) THIRD-PARTY pytest plugins (deepeval/langsmith) import before this
#     conftest and load .env themselves, so the keys are already set here;
# (b) cairn's own load_dotenv(override=False) re-runs at every entrypoint
#     (the server the API tests build), refilling anything unset.
# Fix: for each storage-family key, if its current value CAME FROM .env
# (matches the parsed file), neutralize it to set-but-EMPTY — every getter
# treats "" as unset (`.strip() or default`, the proven local `OBS_STORAGE=`
# recipe) and override=False never refills a set key. A shell export that
# DIFFERS from .env is a deliberate developer choice and survives. Without
# this, a .env postgres DSN silently redirected 7 API tests to live asyncpg.
from pathlib import Path as _Path

from dotenv import dotenv_values as _dotenv_values

#
# ADR-0122 (#2429): each cairn key is read as CAIRN_<NAME>, else OBS_<NAME>, so BOTH
# spellings are neutralised. The OBS_ spelling keeps its historical set-but-empty pin;
# the CAIRN_ one is pinned only when the .env names it (a non-empty CAIRN_ value would
# shadow every test's OBS_ monkeypatch, and an empty one is not a value: "" falls back).
_DOTENV = _dotenv_values(_Path(__file__).resolve().parents[1] / ".env")
_STORAGE_NAMES = ("STORAGE", "BUS", "RUNTIME", "CHECKPOINTER", "CONNECTIONS_DB")
_STORAGE_KEYS = (*(f"OBS_{n}" for n in _STORAGE_NAMES), "RAG_VECTOR_STORE")
for _k in _STORAGE_KEYS:
    if os.environ.get(_k) == (_DOTENV.get(_k) or object()):
        os.environ[_k] = ""
    else:
        os.environ.setdefault(_k, "")
for _n in _STORAGE_NAMES:
    _k = f"CAIRN_{_n}"
    if _k in _DOTENV and os.environ.get(_k, _DOTENV[_k]) == _DOTENV[_k]:
        os.environ[_k] = ""
        _STORAGE_KEYS += (_k,)
del _DOTENV


def pytest_collection_finish(session):
    """#2361 — the neutraliser above only holds while each key stays SET. A test
    module that unsets one at import (``os.environ.pop`` at module level) undoes
    it for the whole session: the next ``import litellm`` runs ``load_dotenv()``,
    which refills the key from a developer ``.env`` — a Postgres DSN, so every
    later store reached for 127.0.0.1:5432. Fail the session, not 61 tests."""
    gone = [k for k in _STORAGE_KEYS if k not in os.environ]
    if gone:
        raise pytest.UsageError(
            f"a test module unset {gone} at import (module-level os.environ.pop/del); "
            "set it per test with monkeypatch instead (#2361)")


@pytest.fixture(autouse=True)
def _storage_env_isolation(request):
    """#2361 — every test leaves the storage keys exactly as it found them.

    Restores them, then FAILS the test that changed them: a raw ``os.environ``
    write (or an unset key refilled mid-test by a third-party ``load_dotenv()``)
    is the leak that sent later tests to 127.0.0.1:5432. ``monkeypatch`` undoes
    its own writes before this teardown runs, so a test that uses it never trips."""
    keys = (*_STORAGE_KEYS, *(f"CAIRN_{n}" for n in _STORAGE_NAMES))
    before = {k: os.environ.get(k) for k in keys}
    yield
    leaked = [k for k, v in before.items() if os.environ.get(k) != v]
    for k in leaked:
        if before[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = before[k]
    if leaked:
        pytest.fail(
            f"{request.node.nodeid} left storage env {leaked} changed (restored now); "
            "set it with monkeypatch.setenv/delenv, never os.environ directly (#2361)",
            pytrace=False)


@pytest.fixture
def no_loop_blocking():
    """Opt-in guard (an internal issue): fail the test if cairn code makes a
    blocking call (sync file / socket / sqlite I/O) on the asyncio event loop.

    Scoped to the ``cairn`` package so third-party blocking (aiosqlite internals,
    langchain) is ignored — we only care about OUR async paths. Detects exactly
    the bug class behind the #41 loop-affinity saga and sync-sqlite-under-async.

    Requested per-test (not autouse): several modules still block the loop today
    (SqliteRunStore, audit sink, sqlite_vec, dataset-checks file I/O — tracked in
    #84). Apply this fixture to async paths as they're moved off the loop
    (``asyncio.to_thread`` / aiofiles) so regressions fail loud. Skips cleanly
    when the ``blockbuster`` dev-extra isn't installed.
    """
    bb = pytest.importorskip("blockbuster")
    with bb.blockbuster_ctx(["cairn"]):
        yield


@pytest.fixture(autouse=True)
def _obs_profile_isolation():
    """#2241 D — every test leaves CAIRN_PROFILE / OBS_PROFILE exactly as it found them.

    The dev-loop CLI entry points (`cairn run` / `validate` / `pipeline …`, and their
    argv ``main``s) default an unset OBS_PROFILE to local-dev by writing ``os.environ``.
    Without this, a test that calls one leaks that profile into every later test, which
    then passes only because of test ORDER. Restore-only: it never sets a value.
    """
    before = {k: os.environ.get(k) for k in ("CAIRN_PROFILE", "OBS_PROFILE")}  # ADR-0122: both spellings
    yield
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
