"""#1904 — one place that sequences a Postgres store's first use.

Every Postgres-backed store here is lazy: the first call that needs the database
creates the pool and runs its ``CREATE TABLE IF NOT EXISTS`` statements. Nearly
all of them used to do that in this order, publishing the pool *first*::

    self._pool = await shared_pool(self._dsn)   # ← observable HERE
    await self._ensure_schema()                 # ← the tables exist only HERE

On a warm database that is invisible. On a FRESH one — a light install (#1828), a
new deployment, CI — the console's first paint fires several requests at once:
the first caller parks inside the DDL, the second sees ``self._pool is not None``,
returns immediately, and queries a table that is not there yet. #1833 caught it
live on the cp box as two 500s (``relation "input_presets" does not exist``) on
the very first page, and never again.

:class:`PgSchemaInit` is that fix once, for the whole family: the pool becomes
observable only after the schema is there, and concurrent first callers coalesce
on one per-store lock instead of racing. Deliberately not a retry: the
ordering is what is wrong, so the ordering is what is fixed. Across processes
(#2544) the DDL itself runs under one fleet-wide Postgres advisory lock, because
two replicas on a fresh database otherwise race ``CREATE TABLE IF NOT EXISTS``
into a ``pg_type`` unique violation. That lock is held only for each process's
first use of a store, so stores still initialise concurrently within a process
until they reach the DDL.

It lives at ``cairn.core`` rather than next to ``shared_pool`` in
``cairn.core.storage.postgres`` on purpose: importing anything under
``cairn.core.storage`` executes that package's ``__init__``, which reaches
``storage.backend`` → ``storage.protocols`` → ``engine.schema`` →
``engine.triggers_grammar`` → ``workspace`` → ``account``. Several of the stores that
mix this in sit *below* that chain, so a module-top import from there is a
circular import — ``test_every_mixin_importer_imports_first_without_a_cycle`` in
``tests/core/test_pg_store_init_race.py`` is the guard. ``cairn.core``'s own
``__init__`` is a docstring and a version string, so this module is importable
from anywhere. It has no third-party imports at all — asyncpg included.

**Across processes (#2544).** The per-store ``asyncio.Lock`` only coalesces callers
inside ONE process. Two replicas starting on a fresh database each run their
``CREATE TABLE IF NOT EXISTS`` at the same moment, and Postgres does not make
that statement concurrency-safe: the loser fails with ``UniqueViolationError`` on
``pg_type_typname_nsp_index`` (the table's row type) instead of skipping. So the
DDL runs under a session advisory lock taken on one pool connection, and
``_ensure_schema`` is handed a view of the pool that yields *that* connection —
the DDL never borrows a second one. That matters: every store waiting on the
lock holds a connection while it waits, so a holder that needed another one from
the shared pool (``CAIRN_PG_POOL_MAX``, default 10, for ~25 stores) could wait
forever on its own waiters.

The lock is the two-``int4`` form, which Postgres keeps in a key space separate
from the one-``bigint`` form that ``leadership.py``, ``dispatch/coordination.py``
and ``budget_reservations.py`` use — so a ``hashtext`` collision with a
long-held leader or pipeline section can never stall schema setup.

Stores that keep their own public ``ensure_schema`` instead of this mixin (#2551:
artifacts, reviews, judges, suggestions, fingerprints, secrets, pgvector, capture)
take the same lock through :func:`schema_init_lock` on the connection that runs
their DDL, and likewise only on first use: once per store (per collection for
pgvector), and for the secret backend only when its table is missing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger("cairn.pg_schema_init")

#: One fleet-wide key: schema setup is a handful of fast idempotent statements,
#: so serialising every store's first use costs milliseconds, and it also covers
#: two stores that touch the same table.
_SCHEMA_LOCK_SQL = "SELECT pg_advisory_lock(hashtext('cairn:schema-init'), 0)"
_SCHEMA_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('cairn:schema-init'), 0)"


@contextlib.asynccontextmanager
async def schema_init_lock(conn: Any) -> AsyncIterator[Any]:
    """Hold the fleet-wide schema lock on ``conn`` for the body, then release it.

    The one cross-process DDL lock (#2544), for every store that runs schema DDL —
    :class:`PgSchemaInit` below and the stores that keep their own ``ensure_schema``
    (#2551). Run the DDL on ``conn`` itself (it is yielded back): a waiter holds its
    own connection while it waits, so a holder that borrowed a second one from a
    shared pool could wait forever on its own waiters.

    A failed unlock never masks the body's error: the connection is terminated
    instead, and a closed session frees the lock.
    """
    await conn.execute(_SCHEMA_LOCK_SQL)
    try:
        yield conn
    finally:
        try:
            await conn.execute(_SCHEMA_UNLOCK_SQL)
        except Exception:  # noqa: BLE001 — never mask the DDL error; a closed session frees the lock
            logger.warning("schema-init unlock failed — terminating the "
                           "connection so the session releases the lock",
                           exc_info=True)
            conn.terminate()


class _LockedPool:
    """The pool as ``_ensure_schema`` sees it while the schema lock is held:
    ``acquire()`` yields the ONE connection that holds the lock."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        yield self._conn


class PgSchemaInit:
    """Mixin giving a Postgres store a race-free lazy ``_get_pool()``.

    The store keeps its own ``__init__`` (which must still set ``self._pool =
    None`` and ``self._schema_ready = False``) and implements the two halves:

    ``_open_pool()``
        its optional-dep and DSN checks, then ``shared_pool(...)``. It must
        **return** the pool and must never assign ``self._pool`` — that
        assignment is what this mixin owns.

    ``_ensure_schema(pool)``
        its DDL, run against the ``pool`` argument. ``self._pool`` is still
        ``None`` while this runs, which is the entire point; the mixin owns the
        ``_schema_ready`` guard, so the body is just the statements. The
        argument is a one-connection view that holds the cross-process schema
        lock (#2544): use ``pool.acquire()`` on it, one at a time, and reach no
        other pool from here.
    """

    _pool: Any
    _schema_ready: bool
    _pg_init_lock: asyncio.Lock | None = None

    async def _open_pool(self) -> Any:  # pragma: no cover — always overridden
        raise NotImplementedError

    async def _ensure_schema(self, pool: Any) -> None:  # pragma: no cover — always overridden
        raise NotImplementedError

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        # Created lazily so a store can be constructed outside a running loop —
        # most of these are process-wide singletons built at import time. The
        # lazy creation needs no lock of its own: there is no ``await`` between
        # the read and the write, and one event loop never interleaves that pair.
        if self._pg_init_lock is None:
            self._pg_init_lock = asyncio.Lock()
        async with self._pg_init_lock:
            if self._pool is not None:
                return self._pool
            pool = await self._open_pool()
            if not self._schema_ready:
                async with pool.acquire() as conn, schema_init_lock(conn):
                    await self._ensure_schema(_LockedPool(conn))
                self._schema_ready = True
            self._pool = pool  # published LAST — every reader sees the schema
            return pool
