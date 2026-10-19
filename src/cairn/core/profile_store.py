"""Admin-managed profile records (CRUD-able deployment profiles).

A :class:`ProfileRecord` is a profile definition the admin can create/edit:
capability bindings, disambiguation, and backends. Mirrors
:class:`~cairn.core.connections.ConnectionStore` (JSON blob, sqlite/in-memory).

This is distinct from the shipped TOML profiles loaded by
``cairn.core.pack.profile.load_profile`` (deployment config in git). These are
records you can manage in the admin; wiring the runtime to *load* a store-backed
profile (vs the TOML file) is a follow-up (#11) — for now the store is the
management surface + source for the admin Profiles view.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from cairn.core._time import utc_now_iso
from cairn.core.connections import slugify
from cairn.core.pg_schema_init import PgSchemaInit


class ProfileRecord(BaseModel):
    """A deployment profile: capability→operator ``bindings``, ``capabilities``
    disambiguation, and ``backends`` (llm_gateway / embedder / durable_runtime).
    ``extends`` names a parent profile (layering, like the TOML ``extends``)."""

    id: str = ""
    name: str
    extends: str = ""
    bindings: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(default_factory=dict)
    backends: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("profile name must be non-empty")
        return v.strip()

    def model_post_init(self, _ctx: Any) -> None:
        if not self.id:
            object.__setattr__(self, "id", slugify(self.name))


class InMemoryProfileStore:
    def __init__(self) -> None:
        self._by_id: dict[str, ProfileRecord] = {}

    async def create(self, p: ProfileRecord) -> ProfileRecord:
        self._by_id[p.id] = p
        return p

    async def get(self, pid: str) -> Optional[ProfileRecord]:
        return self._by_id.get(pid)

    async def list(self) -> list[ProfileRecord]:
        return sorted(self._by_id.values(), key=lambda p: p.name)

    async def update(self, pid: str, **fields: Any) -> ProfileRecord:
        cur = self._by_id.get(pid)
        if cur is None:
            raise KeyError(pid)
        updated = cur.model_copy(update={**fields, "updated_at": utc_now_iso()})
        self._by_id[pid] = updated
        return updated

    async def delete(self, pid: str) -> bool:
        return self._by_id.pop(pid, None) is not None

    def close(self) -> None:
        pass


class SqliteProfileStore:
    """JSON-blob sqlite store, to_thread'd (mirrors SqliteConnectionStore)."""

    def __init__(self, path: str | None = None) -> None:
        import sqlite3

        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_PROFILES_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS profiles "
            "(id TEXT PRIMARY KEY, name TEXT UNIQUE, data TEXT NOT NULL)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _create_sync(self, p: ProfileRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO profiles (id, name, data) VALUES (?,?,?)",
            (p.id, p.name, p.model_dump_json()),
        )
        self._conn.commit()

    async def create(self, p: ProfileRecord) -> ProfileRecord:
        async with self._lock:
            await asyncio.to_thread(self._create_sync, p)
        return p

    def _get_sync(self, pid: str) -> Optional[ProfileRecord]:
        row = self._conn.execute(
            "SELECT data FROM profiles WHERE id=?", (pid,)
        ).fetchone()
        return ProfileRecord.model_validate_json(row[0]) if row else None

    async def get(self, pid: str) -> Optional[ProfileRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, pid)

    def _list_sync(self) -> list[ProfileRecord]:
        rows = self._conn.execute(
            "SELECT data FROM profiles ORDER BY name ASC"
        ).fetchall()
        return [ProfileRecord.model_validate_json(r[0]) for r in rows]

    async def list(self) -> list[ProfileRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._list_sync)

    def _update_sync(self, pid: str, fields: dict) -> ProfileRecord:
        row = self._conn.execute(
            "SELECT data FROM profiles WHERE id=?", (pid,)
        ).fetchone()
        if row is None:
            raise KeyError(pid)
        updated = ProfileRecord.model_validate_json(row[0]).model_copy(
            update={**fields, "updated_at": utc_now_iso()}
        )
        self._conn.execute(
            "UPDATE profiles SET name=?, data=? WHERE id=?",
            (updated.name, updated.model_dump_json(), pid),
        )
        self._conn.commit()
        return updated

    async def update(self, pid: str, **fields: Any) -> ProfileRecord:
        async with self._lock:
            return await asyncio.to_thread(self._update_sync, pid, fields)

    def _delete_sync(self, pid: str) -> bool:
        cur = self._conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
        self._conn.commit()
        return cur.rowcount > 0

    async def delete(self, pid: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, pid)

    def close(self) -> None:
        self._conn.close()


# ── Postgres backend (mirrors storage/postgres/runs.py) — #427 ─────────────
_CREATE_PROFILES_PG_SQL = """
    CREATE TABLE IF NOT EXISTS profiles (
        id   TEXT PRIMARY KEY,
        name TEXT UNIQUE,
        data JSONB NOT NULL
    )
"""
_INSERT_PROFILE_PG_SQL = """
    INSERT INTO profiles (id, name, data)
    VALUES ($1, $2, $3::jsonb)
    ON CONFLICT (id) DO UPDATE
    SET name = EXCLUDED.name, data = EXCLUDED.data
"""
_SELECT_PROFILE_PG_BY_ID = "SELECT data::text FROM profiles WHERE id = $1"
_LIST_PROFILES_PG_SQL = "SELECT data::text FROM profiles ORDER BY name ASC"
_UPDATE_PROFILE_PG_SQL = "UPDATE profiles SET name = $2, data = $3::jsonb WHERE id = $1"
_DELETE_PROFILE_PG_SQL = "DELETE FROM profiles WHERE id = $1"


class PostgresProfileStore(PgSchemaInit):
    """Postgres-backed profile store — mirrors :class:`SqliteProfileStore` (full
    :class:`ProfileRecord` in a JSONB ``data`` column) and
    :class:`~cairn.core.storage.postgres.runs.PostgresRunStore` (lazy asyncpg
    pool). Selected by :func:`default_profile_store` when
    ``CAIRN_STORAGE=postgres://…`` so admin-managed profiles live in the one prod
    Postgres instead of a per-container SQLite file (#427)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)
        except ImportError as e:
            raise RuntimeError(
                "Postgres profile store requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresProfileStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_PROFILES_PG_SQL)

    async def create(self, p: ProfileRecord) -> ProfileRecord:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(_INSERT_PROFILE_PG_SQL, p.id, p.name, p.model_dump_json())
        return p

    async def get(self, pid: str) -> Optional[ProfileRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_PROFILE_PG_BY_ID, pid)
        return ProfileRecord.model_validate_json(row["data"]) if row else None

    async def list(self) -> list[ProfileRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_LIST_PROFILES_PG_SQL)
        return [ProfileRecord.model_validate_json(r["data"]) for r in rows]

    async def update(self, pid: str, **fields: Any) -> ProfileRecord:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(_SELECT_PROFILE_PG_BY_ID, pid)
                if row is None:
                    raise KeyError(pid)
                updated = ProfileRecord.model_validate_json(row["data"]).model_copy(
                    update={**fields, "updated_at": utc_now_iso()}
                )
                await c.execute(_UPDATE_PROFILE_PG_SQL, pid, updated.name, updated.model_dump_json())
        return updated

    async def delete(self, pid: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(_DELETE_PROFILE_PG_SQL, pid)
        return status.split()[-1] != "0"  # asyncpg returns e.g. "DELETE 1"

    def close(self) -> None:
        pass  # pool lives for the process (parity with SqliteProfileStore.close)


def default_profile_store():
    """Pick a profile store from the env. **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…`` → :class:`PostgresProfileStore` (the one prod Postgres).
    Else ``CAIRN_PROFILES_DB`` set → durable sqlite; else in-memory."""
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresProfileStore(dsn=url)
    if getenv("CAIRN_PROFILES_DB", "").strip():
        return SqliteProfileStore()
    return InMemoryProfileStore()
