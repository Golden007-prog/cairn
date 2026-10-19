"""Writable OAuth token store (#116) — the piece the read-only SecretStore can't
provide. Persists captured access/refresh tokens (keyed by connection_id) plus
the short-lived pending-authorization state (keyed by the CSRF ``state``).

Sqlite by default (``~/.cairn/oauth_tokens.db``, file perms 0600), ``:memory:``
for tests, selected by ``CAIRN_OAUTH_TOKEN_DB``. The store is intentionally behind
a small surface so a production deployment can swap in an encrypting backend
(e.g. a writable SecretStore provider) without touching the broker — the same
"extensible secret backend" seam the read path already has (ADR-0033).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import json
import os
import sqlite3
from typing import Any, Optional
from cairn.core.pg_schema_init import PgSchemaInit


def _default_path() -> str:
    from cairn.core import settings

    p = getenv("CAIRN_OAUTH_TOKEN_DB", "").strip()
    # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
    return p or settings.default_sqlite_db()


class OAuthTokenStore:
    """Sqlite-backed token + pending-auth store. Async via ``to_thread`` + a lock,
    so it never blocks the event loop."""

    def __init__(self, path: Optional[str] = None) -> None:
        path = path or _default_path()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS oauth_tokens ("
            "connection_id TEXT PRIMARY KEY, provider TEXT, data TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS oauth_pending ("
            "state TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self._conn.commit()
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)  # tokens at rest — restrict to the owner
            except OSError:
                pass
        self._lock = asyncio.Lock()

    # ── pending authorization (state → provider/connection_id/verifier) ──
    def _put_pending_sync(self, state: str, data: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_pending (state, data) VALUES (?,?)",
            (state, json.dumps(data)),
        )
        self._conn.commit()

    async def put_pending(self, state: str, data: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._put_pending_sync, state, data)

    def _pop_pending_sync(self, state: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT data FROM oauth_pending WHERE state=?", (state,)).fetchone()
        if row is None:
            return None
        self._conn.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
        self._conn.commit()
        return json.loads(row[0])

    async def pop_pending(self, state: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._pop_pending_sync, state)

    # ── tokens (connection_id → access/refresh/expiry) ──
    def _put_token_sync(self, connection_id: str, provider: str, data: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_tokens (connection_id, provider, data) VALUES (?,?,?)",
            (connection_id, provider, json.dumps(data)),
        )
        self._conn.commit()

    async def put_token(self, connection_id: str, provider: str, data: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._put_token_sync, connection_id, provider, data)

    def _get_token_sync(self, connection_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT data FROM oauth_tokens WHERE connection_id=?", (connection_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    async def get_token(self, connection_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._get_token_sync, connection_id)

    def _delete_token_sync(self, connection_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM oauth_tokens WHERE connection_id=?", (connection_id,))
        self._conn.commit()
        return cur.rowcount > 0

    async def delete_token(self, connection_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_token_sync, connection_id)

    def close(self) -> None:
        self._conn.close()


# ── Postgres backend (mirrors storage/postgres/runs.py) — #427 ─────────────
_CREATE_OAUTH_TOKENS_PG_SQL = """
    CREATE TABLE IF NOT EXISTS oauth_tokens (
        connection_id TEXT PRIMARY KEY,
        provider      TEXT,
        data          JSONB NOT NULL
    )
"""
_CREATE_OAUTH_PENDING_PG_SQL = """
    CREATE TABLE IF NOT EXISTS oauth_pending (
        state TEXT PRIMARY KEY,
        data  JSONB NOT NULL
    )
"""
_UPSERT_PENDING_PG_SQL = """
    INSERT INTO oauth_pending (state, data) VALUES ($1, $2::jsonb)
    ON CONFLICT (state) DO UPDATE SET data = EXCLUDED.data
"""
_POP_PENDING_PG_SQL = "DELETE FROM oauth_pending WHERE state = $1 RETURNING data::text"
_UPSERT_TOKEN_PG_SQL = """
    INSERT INTO oauth_tokens (connection_id, provider, data) VALUES ($1, $2, $3::jsonb)
    ON CONFLICT (connection_id) DO UPDATE
    SET provider = EXCLUDED.provider, data = EXCLUDED.data
"""
_SELECT_TOKEN_PG_SQL = "SELECT data::text FROM oauth_tokens WHERE connection_id = $1"
_DELETE_TOKEN_PG_SQL = "DELETE FROM oauth_tokens WHERE connection_id = $1"


class PostgresOAuthTokenStore(PgSchemaInit):
    """Postgres-backed OAuth token + pending-auth store — mirrors
    :class:`OAuthTokenStore` (tokens keyed by ``connection_id``, pending-auth by
    ``state``, both as JSONB ``data``) and
    :class:`~cairn.core.storage.postgres.runs.PostgresRunStore` (lazy asyncpg
    pool). Selected by :func:`default_token_store` when ``CAIRN_STORAGE=postgres://…``
    so captured tokens live in the one prod Postgres instead of a per-container
    SQLite file (#427). A production deployment can still swap in an encrypting
    backend behind this same small surface (ADR-0033)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        # Missing DEP before missing CONFIG (#1184): on a box without the
        # storage-postgres extra, the actionable error is "install the extra",
        # not "set a DSN" — and shared_pool would ModuleNotFoundError anyway.
        # Same order as PostgresWorkspaceStore/PostgresConnectionStore.
        try:
            import asyncpg  # noqa: F401, PLC0415 — optional dep (storage-postgres extra)
        except ImportError as e:
            raise RuntimeError(
                "Postgres OAuth store requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresOAuthTokenStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_OAUTH_TOKENS_PG_SQL)
            await c.execute(_CREATE_OAUTH_PENDING_PG_SQL)

    # ── pending authorization ──
    async def put_pending(self, state: str, data: dict[str, Any]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(_UPSERT_PENDING_PG_SQL, state, json.dumps(data))

    async def pop_pending(self, state: str) -> Optional[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_POP_PENDING_PG_SQL, state)  # DELETE … RETURNING (atomic)
        return json.loads(row["data"]) if row else None

    # ── tokens ──
    async def put_token(self, connection_id: str, provider: str, data: dict[str, Any]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(_UPSERT_TOKEN_PG_SQL, connection_id, provider, json.dumps(data))

    async def get_token(self, connection_id: str) -> Optional[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_TOKEN_PG_SQL, connection_id)
        return json.loads(row["data"]) if row else None

    async def delete_token(self, connection_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(_DELETE_TOKEN_PG_SQL, connection_id)
        return status.split()[-1] != "0"  # asyncpg returns e.g. "DELETE 1"

    def close(self) -> None:
        pass  # pool lives for the process (parity with OAuthTokenStore.close)


_STORE: Optional[object] = None


def default_token_store() -> object:
    """Process-wide token store (cached). **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…`` → :class:`PostgresOAuthTokenStore` (the one prod Postgres),
    else sqlite. ``CAIRN_OAUTH_TOKEN_DB`` picks the sqlite path (``:memory:`` for
    tests — re-created each call so tests get a fresh store)."""
    global _STORE
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        if _STORE is None or not isinstance(_STORE, PostgresOAuthTokenStore):
            _STORE = PostgresOAuthTokenStore(dsn=url)
        return _STORE
    if _STORE is None or getenv("CAIRN_OAUTH_TOKEN_DB", "").strip() == ":memory:":
        _STORE = OAuthTokenStore()
    return _STORE


def reset_token_store() -> None:
    global _STORE
    _STORE = None


__all__ = [
    "OAuthTokenStore",
    "PostgresOAuthTokenStore",
    "default_token_store",
    "reset_token_store",
]
