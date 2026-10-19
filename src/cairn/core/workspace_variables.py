"""Workspace variables — a small key/value store per workspace (ADR-0101 D2, #1974).

Kestra's namespace KV, one layer down: the things a workspace answers the same
way for every run. The run dialog reads four **well-known** keys as its Advanced
defaults (:data:`RESERVED_KEYS`) — which environment to run in, which config
preset, what a run may spend, which series to bill — so a normal run needs
nothing but its declared inputs. Everything else is free-form: a workspace may
park whatever it wants here, and it is listed and edited in Settings › Variables.

Three things this deliberately is NOT:

**Not a second config layer.** ADR-0068/0070's precedence (run › workflow ›
environment › default) is untouched. A variable is a *default for the form*, not
a value the engine resolves behind a run: a run still carries whatever the
dialog submitted, and provenance still records that. Changing ``environment``
here changes what the next dialog preselects, never what an already-recorded run
did.

**Not secrets.** Values are stored in the clear in the same JSON blob as every
other console store, and the listing route returns them verbatim. Credentials
belong in connections (``kind:`` + secret values), which encrypt and never echo.
A short docstring cannot enforce that, so the console says it on the section too.

**Not a new persistence pattern.** Same shape as the input-preset / config-preset
store (:mod:`cairn.core.workflow_config_store`): three backends behind one
duck-typed protocol, the whole record as a JSON ``data`` blob, and
``CREATE TABLE IF NOT EXISTS`` as the only migration there is.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
import sqlite3
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core import settings
from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import PgSchemaInit

#: The keys the run dialog's Advanced fold defaults from (ADR-0101 D2). They are
#: reserved only in the sense that the console *reads* them: setting one is an
#: ordinary write, and a workspace that sets none behaves exactly as before.
RESERVED_KEYS: tuple[str, ...] = ("environment", "config_preset", "cost_cap_usd", "series")


def _key(workspace_id: str, key: str) -> str:
    """Primary key — (workspace, name). Keys are slug-constrained at the API edge
    (:func:`valid_key`), so ``:`` never appears inside a segment."""
    return f"{workspace_id or 'default'}:{key}"


def valid_key(key: str) -> bool:
    """A variable name is a short slug: letters, digits, ``_``, ``-``, ``.``.

    Enforced at the route, not here, so the store stays a store. The point is
    the primary key above: a ``:`` in a name would let one workspace's variable
    address another's row."""
    k = (key or "").strip()
    return bool(k) and len(k) <= 128 and all(c.isalnum() or c in "_-." for c in k)


class WorkspaceVariable(BaseModel):
    """One key/value pair for one workspace.

    ``value`` is arbitrary JSON — a string for ``environment``, a number for
    ``cost_cap_usd``, and whatever a user parks under a free-form key. The store
    never coerces it; the console reads the well-known keys defensively."""

    workspace_id: str = "default"
    key: str
    value: Any = None
    description: str = ""
    updated_at: str = Field(default_factory=utc_now_iso)
    updated_by: str = ""

    @property
    def id(self) -> str:
        return _key(self.workspace_id, self.key)


class InMemoryWorkspaceVariableStore:
    def __init__(self) -> None:
        self._rows: dict[str, WorkspaceVariable] = {}

    async def list(self, workspace_id: str) -> list[WorkspaceVariable]:
        wid = workspace_id or "default"
        return sorted([v for v in self._rows.values() if v.workspace_id == wid], key=lambda v: v.key)

    async def get(self, workspace_id: str, key: str) -> Optional[WorkspaceVariable]:
        return self._rows.get(_key(workspace_id, key))

    async def put(self, var: WorkspaceVariable) -> WorkspaceVariable:
        var = var.model_copy(update={"updated_at": utc_now_iso()})
        self._rows[var.id] = var
        return var

    async def delete(self, workspace_id: str, key: str) -> bool:
        return self._rows.pop(_key(workspace_id, key), None) is not None

    def close(self) -> None:
        pass


class SqliteWorkspaceVariableStore:
    """JSON-blob sqlite store, ``to_thread``'d — mirrors
    :class:`~cairn.core.workflow_config_store.SqliteWorkflowConfigStore`."""

    def __init__(self, path: str | None = None) -> None:
        # NB the sibling stores lazy-import sqlite3/settings; both are stdlib or
        # cycle-free, so CLAUDE.md rule 4 puts them at module top here.
        if path is None:
            path = getenv("CAIRN_WORKSPACE_VARS_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS workspace_variables "
            "(id TEXT PRIMARY KEY, workspace_id TEXT, key TEXT, data TEXT NOT NULL)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def list(self, workspace_id: str) -> list[WorkspaceVariable]:
        def _l() -> list[WorkspaceVariable]:
            rows = self._conn.execute(
                "SELECT data FROM workspace_variables WHERE workspace_id=? ORDER BY key",
                (workspace_id or "default",),
            ).fetchall()
            return [WorkspaceVariable.model_validate_json(r[0]) for r in rows]
        async with self._lock:
            return await asyncio.to_thread(_l)

    async def get(self, workspace_id: str, key: str) -> Optional[WorkspaceVariable]:
        def _g() -> Optional[WorkspaceVariable]:
            row = self._conn.execute(
                "SELECT data FROM workspace_variables WHERE id=?", (_key(workspace_id, key),)
            ).fetchone()
            return WorkspaceVariable.model_validate_json(row[0]) if row else None
        async with self._lock:
            return await asyncio.to_thread(_g)

    async def put(self, var: WorkspaceVariable) -> WorkspaceVariable:
        var = var.model_copy(update={"updated_at": utc_now_iso()})

        def _w() -> None:
            self._conn.execute(
                "INSERT OR REPLACE INTO workspace_variables (id, workspace_id, key, data) VALUES (?,?,?,?)",
                (var.id, var.workspace_id or "default", var.key, var.model_dump_json()),
            )
            self._conn.commit()
        async with self._lock:
            await asyncio.to_thread(_w)
        return var

    async def delete(self, workspace_id: str, key: str) -> bool:
        def _d() -> bool:
            cur = self._conn.execute(
                "DELETE FROM workspace_variables WHERE id=?", (_key(workspace_id, key),))
            self._conn.commit()
            return cur.rowcount > 0
        async with self._lock:
            return await asyncio.to_thread(_d)

    def close(self) -> None:
        self._conn.close()


class PostgresWorkspaceVariableStore(PgSchemaInit):
    """Postgres-backed store — the full record in a JSONB ``data`` column, the
    lazy asyncpg pool published only once the schema exists (#1833's rule: a
    pool visible before ``CREATE TABLE`` ran is a 500 on a fresh database)."""

    _CREATE = (
        "CREATE TABLE IF NOT EXISTS workspace_variables ("
        " id TEXT PRIMARY KEY, workspace_id TEXT, key TEXT, data JSONB NOT NULL)"
    )
    _UPSERT = (
        "INSERT INTO workspace_variables (id, workspace_id, key, data) VALUES ($1,$2,$3,$4::jsonb)"
        " ON CONFLICT (id) DO UPDATE SET workspace_id=EXCLUDED.workspace_id,"
        " key=EXCLUDED.key, data=EXCLUDED.data"
    )
    _LIST = "SELECT data FROM workspace_variables WHERE workspace_id=$1 ORDER BY key"
    _GET = "SELECT data FROM workspace_variables WHERE id=$1"
    _DELETE = "DELETE FROM workspace_variables WHERE id=$1"

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: F401, PLC0415 — optional dep (storage-postgres)
        except ImportError as e:
            raise RuntimeError(
                "Postgres workspace-variable store requires the 'storage-postgres' optional dep."
            ) from e
        if not self._dsn:
            raise RuntimeError("PostgresWorkspaceVariableStore needs a DSN (CAIRN_STORAGE=postgres://…).")
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415 — optional dep

        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(self._CREATE)

    async def list(self, workspace_id: str) -> list[WorkspaceVariable]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(self._LIST, workspace_id or "default")
        return [WorkspaceVariable.model_validate_json(r["data"]) for r in rows]

    async def get(self, workspace_id: str, key: str) -> Optional[WorkspaceVariable]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(self._GET, _key(workspace_id, key))
        return WorkspaceVariable.model_validate_json(row["data"]) if row else None

    async def put(self, var: WorkspaceVariable) -> WorkspaceVariable:
        var = var.model_copy(update={"updated_at": utc_now_iso()})
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(self._UPSERT, var.id, var.workspace_id or "default", var.key, var.model_dump_json())
        return var

    async def delete(self, workspace_id: str, key: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(self._DELETE, _key(workspace_id, key))
        return status.split()[-1] != "0"  # asyncpg returns e.g. "DELETE 1"

    def close(self) -> None:
        pass  # the pool lives for the process (parity with the sibling stores)


_default_store: Any = None


def default_workspace_variable_store() -> Any:
    """Process-wide store singleton. **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…`` → Postgres (the one prod DB); else the shared cairn.db
    sqlite. Swappable for the in-memory store in tests."""
    global _default_store
    if _default_store is None:
        url = getenv("CAIRN_STORAGE", "").strip()
        if url.startswith(("postgres://", "postgresql://")):
            _default_store = PostgresWorkspaceVariableStore(dsn=url)
        else:
            _default_store = SqliteWorkspaceVariableStore()
    return _default_store


async def variables_map(workspace_id: str, *, store: Any = None) -> dict[str, Any]:
    """``{key: value}`` for one workspace — the shape the run dialog defaults
    from. Convenience over :meth:`list`; no filtering, reserved or not."""
    store = store or default_workspace_variable_store()
    return {v.key: v.value for v in await store.list(workspace_id)}


__all__ = [
    "RESERVED_KEYS",
    "InMemoryWorkspaceVariableStore",
    "PostgresWorkspaceVariableStore",
    "SqliteWorkspaceVariableStore",
    "WorkspaceVariable",
    "default_workspace_variable_store",
    "valid_key",
    "variables_map",
]
