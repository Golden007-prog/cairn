"""Postgres backend for the writable secret store (#427 — storage unification).

The `cairn://secrets#KEY` backend was sqlite-only (LocalFileSecretBackend) and
ignored `OBS_STORAGE`. This adds a Fernet-at-rest Postgres backend and makes the
`_local()` selector honor the single `OBS_STORAGE` selector, so console-entered
credentials land in the one prod Postgres. Mirrors the registry-family stores
(see test_connections_pg_store.py).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.secrets_store import (
    LocalFileSecretBackend,
    PostgresSecretBackend,
    _local,
    put_secret,
    resolve_secret,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_secret_backend_instantiates(monkeypatch):
    """Constructor doesn't import asyncpg — the dep stays lazy/optional.
    (Fernet key resolution happens at construct; force the env-key path to keep
    the test hermetic / keyring-free.)"""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OBS_SECRET_KEY", Fernet.generate_key().decode())
    s = PostgresSecretBackend(dsn="postgres://nowhere/db")
    assert s._dsn == "postgres://nowhere/db"


@pytest.mark.asyncio
async def test_pg_run_raises_clearly_without_dsn(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OBS_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    store = PostgresSecretBackend(dsn="")

    async def _noop(c):  # pragma: no cover — never reached without a DSN
        return None

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._run(_noop)
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._run(_noop)


# ─── OBS_STORAGE dispatch (the #427 core: _local honors the URL) ──────

@pytest.mark.parametrize(
    "url,expect",
    [
        ("postgres://u:p@h/db", PostgresSecretBackend),
        ("postgresql://u:p@h/db", PostgresSecretBackend),
        ("sqlite:///tmp/x.db", LocalFileSecretBackend),
        ("", LocalFileSecretBackend),
    ],
)
def test_local_selector_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OBS_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_SECRETS_DB", str(tmp_path / "secrets.db"))
    b = _local()
    try:
        assert isinstance(b, expect)
    finally:
        b.close()


def test_sqlite_facade_roundtrip_unaffected(monkeypatch, tmp_path):
    """The sqlite path (nothing / non-postgres OBS_STORAGE) still round-trips
    through the put_secret / resolve_secret facade."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OBS_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_SECRETS_DB", str(tmp_path / "secrets.db"))
    ref = put_secret("cairn://secrets#conn-api_key", "s3cr3t")
    assert ref == "cairn://secrets#conn-api_key"
    assert resolve_secret(ref) == "s3cr3t"


# ─── Live roundtrip (gated on PGSTORE_DSN + dep) ───────────────────────

@pytest.fixture
def _pg_dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("storage-postgres optional dep group not installed")
    return dsn


def test_live_secret_backend_roundtrip(monkeypatch, _pg_dsn):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OBS_SECRET_KEY", Fernet.generate_key().decode())
    store = PostgresSecretBackend(dsn=_pg_dsn)
    ref = store.put("pg-conn/API_KEY", "top-secret")
    assert ref == "cairn://secrets#pg-conn/API_KEY"
    assert store.get("pg-conn/API_KEY") == "top-secret"  # decrypts round-trip
    assert store.delete("pg-conn/API_KEY") is True
    with pytest.raises(KeyError):
        store.get("pg-conn/API_KEY")
