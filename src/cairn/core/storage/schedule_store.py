"""DB-backed store for **console-authored cron schedules** (#354).

Pack-bundled cron lives on the skill (``triggers.schedule: "<cron>"``) and is
read-only, distributable defaults. This store is the console's writable layer —
one row = "run <workflow> with <inputs> on <cron>" — mirroring the trigger
store (:mod:`cairn.core.storage.trigger_store`) exactly: SQLite rows so a possibly
remote engine (not the operator's laptop) is the source of truth, workspace_id
as a real indexed column (ADR-0051), JSON ``data`` blob as the record.

The :class:`~cairn.core.dispatch.scheduler.SkillScheduler` registers one
apscheduler job per **enabled** row and re-syncs when the console mutates the
store; fires go through the SAME contract-validate → provenance →
``dispatcher.run_template`` path as ``POST /packs/{ref}/run``.

Moved here from ``cairn.core.triggers.schedule_store`` (#2416, ADR-0120 D6): a
store is persistence, so it lives below the HTTP layer; the old path is a
deprecated alias for one release.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core._aio import run_sync
from cairn.core._time import utc_now_iso
from cairn.core.workspace import DEFAULT_WORKSPACE
from cairn.core.pg_schema_init import PgSchemaInit


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-")
    return s or "schedule"


class StoredSchedule(BaseModel):
    """One console-authored schedule: cron + the workflow run it fires."""

    id: str = ""
    #: Tenancy scope (ADR-0048 rev 2). Stamped from ``current_workspace()`` on
    #: create; the fire path re-enters this workspace so the run is scoped to it.
    workspace_id: str = DEFAULT_WORKSPACE
    name: str
    #: 5-field crontab expression (validated with CronTrigger.from_crontab
    #: before it ever reaches the store).
    cron: str
    #: Namespaced template ref, e.g. ``dataset-readiness/check``.
    workflow: str
    #: Typed input values for the run (already coerced against the workflow's
    #: input schema at create time — same validator as the Run form).
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: Execution profile/environment name ("" = the default).
    environment: str = ""
    #: ADR-0071 (#792) — a config preset pinned to THIS schedule. Empty ⇒ the
    #: workflow's marked-default preset applies (like any trigger). Set ⇒ every
    #: firing of this schedule runs under the named preset. Rides the JSON data
    #: blob free (no column/migration), like the run's own config fields.
    config_preset: str = ""
    enabled: bool = True
    description: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    #: ADR-0086 H4 (#1246) — what to do with cron fires that fell inside server
    #: downtime: ``skip`` (default: audit only), ``fire_once`` (coalesce into one
    #: catch-up run) or ``fire_all`` (replay each, capped). Default ``skip`` keeps
    #: behaviour unchanged unless an operator opts in. Rides the JSON data blob
    #: free (no column/migration), like ``config_preset``.
    misfire: str = "skip"
    #: ISO-8601 UTC of the last **successful** dispatch of this schedule; empty
    #: until it has fired once. The catch-up watermark reads from here.
    last_fired_at: str = ""
    #: ISO-8601 UTC of the newest missed window already accounted for by a
    #: catch-up pass. Kept apart from ``last_fired_at`` so "it ran" and "we
    #: noticed it didn't run" never get confused; the watermark is the max.
    last_missed_at: str = ""
    #: ADR-0101 D1 (#1973) — set to the workflow ref when this row is the
    #: MATERIALISATION of that flow's ``triggers:`` front matter, empty when a
    #: user authored it in Settings › Triggers. The declared/hand-made line: a
    #: re-enable converges rows it owns and never touches rows it doesn't.
    #: Rides the JSON data blob free, like ``config_preset`` and ``misfire``.
    declared_by: str = ""
    #: ADR-0101 D4 (#1976) — a WATCH's memory: what its source looked like the
    #: last time a check admitted it. ``{checked_at, changed_at, marker: {...},
    #: last_version}``; the marker's shape is the source kind's (a dataset
    #: version number, an s3 key→etag map, a table watermark).
    #:
    #: It lives HERE, on the row that fires the check, because that is the only
    #: record whose lifetime is exactly the watch's: deleting the watch deletes
    #: its memory, and a workspace's watches are scoped like its schedules. The
    #: alternative — a marker on the dataset/artifact records — outlives the
    #: watch and cannot express "an s3 prefix nothing has registered from yet".
    #: Like ``last_fired_at`` it is RUNTIME-owned: materialisation never writes
    #: it, so re-syncing an edited flow does not make a watch re-import history.
    #: Rides the JSON data blob free — no column, no migration.
    watch_state: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _ctx: object) -> None:  # pydantic v2 hook
        if not self.id:
            object.__setattr__(self, "id", _slug(self.name))


class InMemoryScheduleStore:
    """Volatile store (tests / :memory: opt-in)."""

    def __init__(self) -> None:
        self._by_id: dict[str, StoredSchedule] = {}

    async def create(self, s: StoredSchedule) -> StoredSchedule:
        self._by_id[s.id] = s
        return s

    async def get(self, schedule_id: str, workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        s = self._by_id.get(schedule_id)
        if s is not None and workspace_id is not None and s.workspace_id != workspace_id:
            return None
        return s

    async def list(self, workspace_id: Optional[str] = None) -> list[StoredSchedule]:
        return self.list_sync(workspace_id)

    def list_sync(self, workspace_id: Optional[str] = None) -> list[StoredSchedule]:
        recs = self._by_id.values()
        if workspace_id is not None:
            recs = [s for s in recs if s.workspace_id == workspace_id]
        return sorted(recs, key=lambda s: s.name)

    async def update_fields(self, schedule_id: str, updates: dict[str, Any], workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        s = await self.get(schedule_id, workspace_id)
        if s is None:
            return None
        s = s.model_copy(update=dict(updates))
        self._by_id[s.id] = s
        return s

    async def set_enabled(self, schedule_id: str, enabled: bool, workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        return await self.update_fields(schedule_id, {"enabled": enabled}, workspace_id)

    async def delete(self, schedule_id: str, workspace_id: Optional[str] = None) -> bool:
        s = self._by_id.get(schedule_id)
        if s is None or (workspace_id is not None and s.workspace_id != workspace_id):
            return False
        del self._by_id[schedule_id]
        return True

    def close(self) -> None:  # parity with the sqlite store
        pass


class SqliteScheduleStore:
    """Embedded-SQLite store — same shape as :class:`SqliteTriggerStore`:
    JSON ``data`` blob + indexed ``workspace_id``, async ops via
    ``asyncio.to_thread`` under a lock, sync reads for the scheduler's
    (synchronous) registration pass."""

    def __init__(self, path: str | None = None) -> None:
        import sqlite3  # stdlib, but the connection is an instance handle

        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_SCHEDULES_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id           TEXT PRIMARY KEY,
                name         TEXT UNIQUE,
                workflow     TEXT,
                workspace_id TEXT NOT NULL DEFAULT 'default',
                data         TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sched_ws_name ON schedules(workspace_id, name)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _put_sync(self, s: StoredSchedule) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO schedules (id, name, workflow, workspace_id, data) VALUES (?,?,?,?,?)",
            (s.id, s.name, s.workflow, s.workspace_id, s.model_dump_json()),
        )
        self._conn.commit()

    async def create(self, s: StoredSchedule) -> StoredSchedule:
        async with self._lock:
            await asyncio.to_thread(self._put_sync, s)
        return s

    def _get_sync(self, schedule_id: str, workspace_id: Optional[str]) -> Optional[StoredSchedule]:
        sql, params = (
            ("SELECT data FROM schedules WHERE id=? AND workspace_id=?", (schedule_id, workspace_id))
            if workspace_id is not None
            else ("SELECT data FROM schedules WHERE id=?", (schedule_id,))
        )
        row = self._conn.execute(sql, params).fetchone()
        return StoredSchedule.model_validate_json(row[0]) if row else None

    async def get(self, schedule_id: str, workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, schedule_id, workspace_id)

    def list_sync(self, workspace_id: Optional[str] = None) -> list[StoredSchedule]:
        if workspace_id is not None:
            rows = self._conn.execute(
                "SELECT data FROM schedules WHERE workspace_id=? ORDER BY name ASC",
                (workspace_id,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT data FROM schedules ORDER BY name ASC").fetchall()
        return [StoredSchedule.model_validate_json(r[0]) for r in rows]

    async def list(self, workspace_id: Optional[str] = None) -> list[StoredSchedule]:
        async with self._lock:
            return await asyncio.to_thread(self.list_sync, workspace_id)

    async def update_fields(self, schedule_id: str, updates: dict[str, Any], workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        """Read-modify-write the row's JSON blob under the store lock — the one
        mutation door (``set_enabled``, the #1246 catch-up stamps) so concurrent
        writers can't lose each other's fields."""
        async with self._lock:
            s = await asyncio.to_thread(self._get_sync, schedule_id, workspace_id)
            if s is None:
                return None
            s = s.model_copy(update=dict(updates))
            await asyncio.to_thread(self._put_sync, s)
            return s

    async def set_enabled(self, schedule_id: str, enabled: bool, workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        return await self.update_fields(schedule_id, {"enabled": enabled}, workspace_id)

    def _delete_sync(self, schedule_id: str, workspace_id: Optional[str]) -> bool:
        if workspace_id is not None:
            cur = self._conn.execute(
                "DELETE FROM schedules WHERE id=? AND workspace_id=?", (schedule_id, workspace_id)
            )
        else:
            cur = self._conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        self._conn.commit()
        return cur.rowcount > 0

    async def delete(self, schedule_id: str, workspace_id: Optional[str] = None) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, schedule_id, workspace_id)

    def close(self) -> None:
        self._conn.close()


# ── Postgres backend (mirrors storage/postgres/runs.py) — #427 ─────────────
# workspace_id is a real INDEXED column (ADR-0051), mirroring the sqlite table.
_CREATE_SCHEDULES_PG_SQL = """
    CREATE TABLE IF NOT EXISTS schedules (
        id           TEXT PRIMARY KEY,
        name         TEXT UNIQUE,
        workflow     TEXT,
        workspace_id TEXT NOT NULL DEFAULT 'default',
        data         JSONB NOT NULL
    )
"""
_CREATE_SCHEDULES_WS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_sched_ws_name ON schedules(workspace_id, name)"
)
_UPSERT_SCHEDULE_PG_SQL = """
    INSERT INTO schedules (id, name, workflow, workspace_id, data)
    VALUES ($1, $2, $3, $4, $5::jsonb)
    ON CONFLICT (id) DO UPDATE
    SET name = EXCLUDED.name, workflow = EXCLUDED.workflow,
        workspace_id = EXCLUDED.workspace_id, data = EXCLUDED.data
"""
_SELECT_SCHEDULE_PG_BY_ID = "SELECT data::text FROM schedules WHERE id = $1"
_SELECT_SCHEDULE_PG_BY_ID_WS = "SELECT data::text FROM schedules WHERE id = $1 AND workspace_id = $2"
_LIST_SCHEDULES_PG_SQL = "SELECT data::text FROM schedules ORDER BY name ASC"
_LIST_SCHEDULES_PG_WS_SQL = "SELECT data::text FROM schedules WHERE workspace_id = $1 ORDER BY name ASC"
_DELETE_SCHEDULE_PG_SQL = "DELETE FROM schedules WHERE id = $1"
_DELETE_SCHEDULE_PG_WS_SQL = "DELETE FROM schedules WHERE id = $1 AND workspace_id = $2"


class PostgresScheduleStore(PgSchemaInit):
    """Postgres-backed schedule store — mirrors :class:`SqliteScheduleStore` (full
    :class:`StoredSchedule` in a JSONB ``data`` column, indexed ``workspace_id``)
    and :class:`~cairn.core.storage.postgres.runs.PostgresRunStore` (lazy asyncpg
    pool). Selected by :func:`default_schedule_store` when
    ``CAIRN_STORAGE=postgres://…`` so console-authored cron schedules live in the
    one prod Postgres and are shared across a multi-instance fleet (#427).

    ``list_sync`` (the scheduler's synchronous registration pass) opens a
    short-lived private event loop — that path has no running loop to reuse."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)
        except ImportError as e:
            raise RuntimeError(
                "Postgres schedule store requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresScheduleStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_SCHEDULES_PG_SQL)
            await c.execute(_CREATE_SCHEDULES_WS_IDX)

    async def create(self, s: StoredSchedule) -> StoredSchedule:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(
                _UPSERT_SCHEDULE_PG_SQL, s.id, s.name, s.workflow, s.workspace_id, s.model_dump_json()
            )
        return s

    async def get(self, schedule_id: str, workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            if workspace_id is not None:
                row = await c.fetchrow(_SELECT_SCHEDULE_PG_BY_ID_WS, schedule_id, workspace_id)
            else:
                row = await c.fetchrow(_SELECT_SCHEDULE_PG_BY_ID, schedule_id)
        return StoredSchedule.model_validate_json(row["data"]) if row else None

    async def list(self, workspace_id: Optional[str] = None) -> list[StoredSchedule]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            if workspace_id is not None:
                rows = await c.fetch(_LIST_SCHEDULES_PG_WS_SQL, workspace_id)
            else:
                rows = await c.fetch(_LIST_SCHEDULES_PG_SQL)
        return [StoredSchedule.model_validate_json(r["data"]) for r in rows]

    async def _list_oneshot(self, workspace_id: Optional[str]) -> list[StoredSchedule]:
        """Same query as :meth:`list`, but on a private one-shot connection instead
        of the shared pool — asyncpg pools are bound to the loop that created them,
        and :meth:`list_sync` may run on a different loop (worker-thread fallback)."""
        import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)

        if not self._dsn:
            raise RuntimeError(
                "PostgresScheduleStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        conn = await asyncpg.connect(self._dsn)
        try:
            await conn.execute(_CREATE_SCHEDULES_PG_SQL)
            if workspace_id is not None:
                rows = await conn.fetch(_LIST_SCHEDULES_PG_WS_SQL, workspace_id)
            else:
                rows = await conn.fetch(_LIST_SCHEDULES_PG_SQL)
        finally:
            await conn.close()
        return [StoredSchedule.model_validate_json(r["data"]) for r in rows]

    def list_sync(self, workspace_id: Optional[str] = None) -> list[StoredSchedule]:
        """Synchronous read for the scheduler's (sync) registration pass. Runs a
        self-contained one-shot query to completion whether or not a loop is
        already running (a running loop can't be re-entered → worker-thread
        fallback)."""
        return run_sync(self._list_oneshot(workspace_id))

    async def update_fields(self, schedule_id: str, updates: dict[str, Any], workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        """Transactional read-modify-write of the JSONB blob — the one mutation
        door (``set_enabled``, the #1246 catch-up stamps)."""
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                if workspace_id is not None:
                    row = await c.fetchrow(_SELECT_SCHEDULE_PG_BY_ID_WS, schedule_id, workspace_id)
                else:
                    row = await c.fetchrow(_SELECT_SCHEDULE_PG_BY_ID, schedule_id)
                if row is None:
                    return None
                s = StoredSchedule.model_validate_json(row["data"]).model_copy(update=dict(updates))
                await c.execute(
                    _UPSERT_SCHEDULE_PG_SQL, s.id, s.name, s.workflow, s.workspace_id, s.model_dump_json()
                )
                return s

    async def set_enabled(self, schedule_id: str, enabled: bool, workspace_id: Optional[str] = None) -> Optional[StoredSchedule]:
        return await self.update_fields(schedule_id, {"enabled": enabled}, workspace_id)

    async def delete(self, schedule_id: str, workspace_id: Optional[str] = None) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            if workspace_id is not None:
                status = await c.execute(_DELETE_SCHEDULE_PG_WS_SQL, schedule_id, workspace_id)
            else:
                status = await c.execute(_DELETE_SCHEDULE_PG_SQL, schedule_id)
        return status.split()[-1] != "0"  # asyncpg returns e.g. "DELETE 1"

    def close(self) -> None:
        pass  # pool lives for the process (parity with SqliteScheduleStore.close)


def default_schedule_store():
    """Pick a schedule store from the env. **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…`` → :class:`PostgresScheduleStore` (the one prod Postgres).
    Otherwise durable by default (sqlite, co-located with the trigger store's DB
    file); ``CAIRN_SCHEDULES_DB`` overrides the sqlite path; ``:memory:`` opts into
    volatile."""
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresScheduleStore(dsn=url)
    if getenv("CAIRN_SCHEDULES_DB", "").strip() == ":memory:":
        return InMemoryScheduleStore()
    return SqliteScheduleStore()
