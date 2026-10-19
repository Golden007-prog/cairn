"""DB-backed store for **console/user-authored** policy specs.

Same three-layer overlay as triggers (see :mod:`cairn.core.storage.trigger_store`):

  ① pack-bundled YAML   ``<pack>/policies/*.yaml``   read-only, distributable defaults
  ② user-overlay dir    ``~/.cairn/policies/*.yaml``  hand / git authored
  ③ this store          ``~/.cairn/policies.db``      what the *console* writes  ← writable

The console writes layer ③ (a DB row, not a file) so policies authored against a
possibly-remote engine reach that engine's source of truth — SQLite locally, the
Postgres mirror in prod. A row holds the validated YAML text + an indexed
``name``; the merge step (``PolicyEngine``) parses it back through the same
``PolicySpec.model_validate`` the file loader uses, last-wins by name.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
import re
from typing import Optional

from pydantic import BaseModel, Field
from cairn.core._aio import run_sync
from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import PgSchemaInit


def _slug(name: str) -> str:
    """Stable, URL-safe id from a policy name (pk + lookup key)."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-")
    return s or "policy"


class StoredPolicy(BaseModel):
    """One console-authored policy: its validated YAML text + metadata."""

    id: str = ""
    name: str
    yaml: str
    enabled: bool = True
    description: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    def model_post_init(self, _ctx: object) -> None:  # pydantic v2 hook
        if not self.id:
            object.__setattr__(self, "id", _slug(self.name))


class InMemoryPolicyStore:
    """Volatile store (tests / no-config default)."""

    def __init__(self) -> None:
        self._by_id: dict[str, StoredPolicy] = {}

    async def create(self, p: StoredPolicy) -> StoredPolicy:
        self._by_id[p.id] = p
        return p

    async def get(self, policy_id: str) -> Optional[StoredPolicy]:
        return self._by_id.get(policy_id)

    async def get_by_name(self, name: str) -> Optional[StoredPolicy]:
        return next((p for p in self._by_id.values() if p.name == name), None)

    async def list(self) -> list[StoredPolicy]:
        return self.list_sync()

    def list_sync(self) -> list[StoredPolicy]:
        return sorted(self._by_id.values(), key=lambda p: p.name)

    async def delete(self, policy_id: str) -> bool:
        return self._by_id.pop(policy_id, None) is not None

    def close(self) -> None:
        pass


class SqlitePolicyStore:
    """Embedded-SQLite store — restart-safe, single file. Mirrors
    :class:`~cairn.core.connections.SqliteConnectionStore`; async ops via
    ``asyncio.to_thread`` under a lock, sync :meth:`list_sync` for the (sync)
    engine load path (WAL + ``check_same_thread=False`` make reads safe)."""

    def __init__(self, path: str | None = None) -> None:
        import sqlite3  # stdlib, but the connection is an instance handle

        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_POLICIES_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policies (
                id   TEXT PRIMARY KEY,
                name TEXT UNIQUE,
                data TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _create_sync(self, p: StoredPolicy) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO policies (id, name, data) VALUES (?,?,?)",
            (p.id, p.name, p.model_dump_json()),
        )
        self._conn.commit()

    async def create(self, p: StoredPolicy) -> StoredPolicy:
        async with self._lock:
            await asyncio.to_thread(self._create_sync, p)
        return p

    def _get_sync(self, sql: str, params: tuple) -> Optional[StoredPolicy]:
        row = self._conn.execute(sql, params).fetchone()
        return StoredPolicy.model_validate_json(row[0]) if row else None

    async def get(self, policy_id: str) -> Optional[StoredPolicy]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_sync, "SELECT data FROM policies WHERE id=?", (policy_id,)
            )

    async def get_by_name(self, name: str) -> Optional[StoredPolicy]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_sync, "SELECT data FROM policies WHERE name=?", (name,)
            )

    def list_sync(self) -> list[StoredPolicy]:
        """Synchronous read for the load/merge path (engine build is sync)."""
        rows = self._conn.execute("SELECT data FROM policies ORDER BY name ASC").fetchall()
        return [StoredPolicy.model_validate_json(r[0]) for r in rows]

    async def list(self) -> list[StoredPolicy]:
        async with self._lock:
            return await asyncio.to_thread(self.list_sync)

    def _delete_sync(self, policy_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM policies WHERE id=?", (policy_id,))
        self._conn.commit()
        return cur.rowcount > 0

    async def delete(self, policy_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, policy_id)

    def close(self) -> None:
        self._conn.close()


# ── Postgres backend (mirrors storage/postgres/runs.py) — #427 ─────────────
_CREATE_POLICIES_PG_SQL = """
    CREATE TABLE IF NOT EXISTS policies (
        id   TEXT PRIMARY KEY,
        name TEXT UNIQUE,
        data JSONB NOT NULL
    )
"""
_INSERT_POLICY_PG_SQL = """
    INSERT INTO policies (id, name, data)
    VALUES ($1, $2, $3::jsonb)
    ON CONFLICT (id) DO UPDATE
    SET name = EXCLUDED.name, data = EXCLUDED.data
"""
_SELECT_POLICY_PG_BY_ID = "SELECT data::text FROM policies WHERE id = $1"
_SELECT_POLICY_PG_BY_NAME = "SELECT data::text FROM policies WHERE name = $1"
_LIST_POLICIES_PG_SQL = "SELECT data::text FROM policies ORDER BY name ASC"
_DELETE_POLICY_PG_SQL = "DELETE FROM policies WHERE id = $1"


class PostgresPolicyStore(PgSchemaInit):
    """Postgres-backed policy store — mirrors :class:`SqlitePolicyStore` (full
    :class:`StoredPolicy` in a JSONB ``data`` column) and
    :class:`~cairn.core.storage.postgres.runs.PostgresRunStore` (lazy asyncpg
    pool, created on first call). Selected by :func:`default_policy_store` when
    ``CAIRN_STORAGE=postgres://…`` so console-authored policies live in the one
    prod Postgres instead of a per-container SQLite file (#427).

    ``list_sync`` (the engine's synchronous merge path) opens a short-lived
    private event loop — the sync engine build has no running loop to reuse."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)
        except ImportError as e:
            raise RuntimeError(
                "Postgres policy store requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresPolicyStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_POLICIES_PG_SQL)

    async def create(self, p: StoredPolicy) -> StoredPolicy:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(_INSERT_POLICY_PG_SQL, p.id, p.name, p.model_dump_json())
        return p

    async def get(self, policy_id: str) -> Optional[StoredPolicy]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_POLICY_PG_BY_ID, policy_id)
        return StoredPolicy.model_validate_json(row["data"]) if row else None

    async def get_by_name(self, name: str) -> Optional[StoredPolicy]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_POLICY_PG_BY_NAME, name)
        return StoredPolicy.model_validate_json(row["data"]) if row else None

    async def list(self) -> list[StoredPolicy]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_LIST_POLICIES_PG_SQL)
        return [StoredPolicy.model_validate_json(r["data"]) for r in rows]

    async def _list_oneshot(self) -> list[StoredPolicy]:
        """Same query as :meth:`list`, but on a private one-shot connection instead
        of the shared pool — asyncpg pools are bound to the loop that created them,
        and :meth:`list_sync` may run on a different loop (worker-thread fallback)."""
        import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)

        if not self._dsn:
            raise RuntimeError(
                "PostgresPolicyStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        conn = await asyncpg.connect(self._dsn)
        try:
            await conn.execute(_CREATE_POLICIES_PG_SQL)
            rows = await conn.fetch(_LIST_POLICIES_PG_SQL)
        finally:
            await conn.close()
        return [StoredPolicy.model_validate_json(r["data"]) for r in rows]

    def list_sync(self) -> list[StoredPolicy]:
        """Synchronous read for the (sync) engine merge path. Runs a self-contained
        one-shot query to completion whether or not a loop is already running (a
        running loop can't be re-entered → worker-thread fallback)."""
        return run_sync(self._list_oneshot())

    async def delete(self, policy_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(_DELETE_POLICY_PG_SQL, policy_id)
        return status.split()[-1] != "0"  # asyncpg returns e.g. "DELETE 1"

    def close(self) -> None:
        pass  # pool lives for the process (parity with SqlitePolicyStore.close)


def default_policy_store():
    """Pick a policy store from the env. **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…`` → :class:`PostgresPolicyStore` (the one prod Postgres).
    Otherwise durable **by default** (sqlite at ``~/.cairn/policies.db``).
    ``CAIRN_POLICIES_DB`` overrides the sqlite path; ``CAIRN_POLICIES_DB=:memory:``
    opts into the volatile in-memory store."""
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresPolicyStore(dsn=url)
    if getenv("CAIRN_POLICIES_DB", "").strip() == ":memory:":
        return InMemoryPolicyStore()
    return SqlitePolicyStore()
