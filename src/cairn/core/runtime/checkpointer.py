"""Checkpointer factory — durability backend for LangGraph pause/resume.

Used by the durable action saga (ADR-0024 RM3): an ``approval`` node calls
``interrupt()``, LangGraph snapshots graph state to the checkpointer, and a
later ``Command(resume=...)`` rehydrates and continues — surviving a crash
when the backend is persistent.

Tiers, selected by ``CAIRN_CHECKPOINTER`` (default ``memory``):

  memory    — in-process MemorySaver. Resume works within the same process
              (fine for dev, tests, and a long-lived single server); state
              is lost on restart.
  sqlite    — SqliteSaver (zero-config durable single-node). Requires the
              ``langgraph-checkpoint-sqlite`` extra.
  postgres  — PostgresSaver (multi-node durable). Requires the
              ``runtime-postgres`` extra (``langgraph-checkpoint-postgres``).

The non-memory tiers are durable *across restarts and processes* because the
state lives in the shared backend, not the handler instance — that's what
lets the trigger server resume a saga a different process paused.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import logging
import os
import weakref
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from cairn.core import settings

logger = logging.getLogger("cairn.runtime.checkpointer")

_MEMORY_SINGLETON: Any = None
_DURABLE_SINGLETON: Any = None

# ── Async checkpointer state, keyed by EVENT-LOOP id (an internal issue) ──
# aiosqlite/psycopg connections are loop-bound: one created on loop A and awaited
# from loop B never resolves (the durable-runtime hang, #41). So we NEVER share a
# connection across loops — every async resource is cached per running loop.
# Postgres pools per loop (a connection per concurrent run); SQLite is
# single-writer, so one connection per loop is both correct and contention-free.
_ASYNC_MEMORY_SINGLETON: Any = None
_SQLITE_SAVER_BY_LOOP: dict[int, Any] = {}
_SQLITE_CM_BY_LOOP: dict[int, Any] = {}  # hold the CMs (else GC closes the conn)
# loop_id → (pool, weakref-to-loop). The weakref lets us (a) VALIDATE a cache
# hit actually belongs to the current loop — id() is recycled after a loop is
# GC'd, so a bare id key could hand a NEW loop a dead loop's pool (the #41
# cross-loop hang) — and (b) EVICT pools whose loop has finished instead of
# leaking a whole max_size pool per transient loop (#632: this was the dominant
# half of the Postgres connection exhaustion).
_PG_POOL_BY_LOOP: dict[int, "tuple[Any, weakref.ref]"] = {}
_PG_SETUP_LOOPS: set[int] = set()


def _pg_pool_max_size() -> int:
    """Per-loop checkpointer pool ceiling. `CAIRN_CHECKPOINTER_POOL_MAX` (default 3,
    down from 10) — size against Postgres max_connections × process/loop count
    (#632). Companion to #614's `CAIRN_PG_POOL_MAX` for the store pools."""
    try:
        return max(1, int(getenv("CAIRN_CHECKPOINTER_POOL_MAX", "3")))
    except ValueError:
        return 3


def _pg_pool_max_idle() -> float:
    """Seconds an idle above-min connection lingers before the pool closes it.
    `CAIRN_CHECKPOINTER_POOL_MAX_IDLE` (default 60, down from psycopg's 600) so a
    burst drains in ~1 min instead of ~10, keeping steady-state near min_size (#632)."""
    try:
        return max(5.0, float(getenv("CAIRN_CHECKPOINTER_POOL_MAX_IDLE", "60")))
    except ValueError:
        return 60.0


def _prune_dead_pg_pools() -> None:
    """Drop cache entries whose event loop has finished. Their pooled connections
    were closed when the loop tore down; keeping the entry leaks memory and risks
    id()-reuse aliasing a fresh loop onto a dead pool (#632/#41). Best-effort — a
    still-open pool on a dead loop can't be awaited-closed from here, but the loop
    teardown already closed its sockets."""
    for lid in list(_PG_POOL_BY_LOOP):
        _pool, loop_ref = _PG_POOL_BY_LOOP[lid]
        loop = loop_ref()
        if loop is None or loop.is_closed():
            _PG_POOL_BY_LOOP.pop(lid, None)
            _PG_SETUP_LOOPS.discard(lid)


def get_checkpointer() -> Any:
    """Return the process checkpointer for the configured tier.

    ``memory`` is a process singleton so an interrupt and its later resume
    (same process) share state. Durable tiers return a singleton bound to
    the shared backend.
    """
    global _MEMORY_SINGLETON, _DURABLE_SINGLETON
    kind = settings.checkpointer()

    if kind == "memory":
        if _MEMORY_SINGLETON is None:
            from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415
            _MEMORY_SINGLETON = MemorySaver()
            logger.info("checkpointer: memory (in-process; lost on restart)")
        return _MEMORY_SINGLETON

    if _DURABLE_SINGLETON is not None:
        return _DURABLE_SINGLETON

    if kind == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "CAIRN_CHECKPOINTER=sqlite but langgraph-checkpoint-sqlite is not "
                "installed; `pip install 'cairndev[runtime-sqlite]'`"
            ) from e
        path = settings.checkpointer_path()
        cm = SqliteSaver.from_conn_string(path)
        saver = cm.__enter__()  # held for process lifetime (singleton)
        saver.setup()
        _DURABLE_SINGLETON = saver
        logger.info("checkpointer: sqlite at %s", path)
        return saver

    if kind == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "CAIRN_CHECKPOINTER=postgres but langgraph-checkpoint-postgres is "
                "not installed; `pip install 'cairndev[runtime-postgres]'`"
            ) from e
        dsn = settings.checkpointer_dsn()
        if not dsn:
            raise RuntimeError(
                "CAIRN_CHECKPOINTER=postgres requires CAIRN_CHECKPOINTER_DSN"
            )
        cm = PostgresSaver.from_conn_string(dsn)
        saver = cm.__enter__()
        saver.setup()
        _DURABLE_SINGLETON = saver
        logger.info("checkpointer: postgres")
        return saver

    logger.warning("unknown CAIRN_CHECKPOINTER=%r — falling back to memory", kind)
    return _memory_fallback()


def _memory_fallback() -> Any:
    global _MEMORY_SINGLETON
    if _MEMORY_SINGLETON is None:
        from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415
        _MEMORY_SINGLETON = MemorySaver()
    return _MEMORY_SINGLETON


@asynccontextmanager
async def checkpoint_session() -> AsyncIterator[Any]:
    """Yield a LangGraph checkpointer **scoped to one execution segment** —
    used by the async runtimes (in_process + DBOS, ADR-0035) that drive a gated
    graph via ``ainvoke``. The yielded saver is the native LangGraph
    ``BaseCheckpointSaver`` (``Async{Sqlite,Postgres}Saver`` / ``MemorySaver``);
    we pass native types across the boundary, never a wrapper (issue #51).

    Lifecycle by tier:

      * ``postgres`` — check ONE connection out of a per-loop
        :class:`AsyncConnectionPool` for this segment, return it on exit. A run
        paused at HITL holds **no** connection between segments; the resume opens
        a fresh session and reads the checkpoint by ``thread_id``. Concurrent
        runs each get their own connection → no shared-saver lock contention.
      * ``sqlite`` — a per-loop connection (SQLite is single-writer; pooling buys
        nothing and concurrent connections fight the file lock). Held for the
        loop's life; the per-loop cache is what makes it loop-safe.
      * ``memory`` — the process singleton (no connection; an interrupt and its
        in-process resume must see the same instance).

    Durability is unaffected: checkpoints live in the backing store keyed by
    ``thread_id`` (Postgres connections are autocommit), so a fresh session
    always sees committed state.
    """
    kind = settings.checkpointer()
    if kind == "postgres":
        pool = await _pg_pool_for_loop()
        async with pool.connection() as conn:
            # optional dep: langgraph-checkpoint-postgres (runtime-postgres extra)
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415
            yield AsyncPostgresSaver(conn)
        return
    if kind == "sqlite":
        yield await _sqlite_saver_for_loop()
        return
    # memory / unknown fallback — shared singleton, async-capable.
    if kind not in ("memory",):
        logger.warning("unknown CAIRN_CHECKPOINTER=%r — falling back to memory (async)", kind)
    yield _async_memory_saver()


def _async_memory_saver() -> Any:
    global _ASYNC_MEMORY_SINGLETON
    if _ASYNC_MEMORY_SINGLETON is None:
        from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415
        _ASYNC_MEMORY_SINGLETON = MemorySaver()
        logger.info("async checkpointer: memory (in-process; lost on restart)")
    return _ASYNC_MEMORY_SINGLETON


async def _sqlite_saver_for_loop() -> Any:
    """Per-loop ``AsyncSqliteSaver`` (aiosqlite is loop-bound). Created +
    ``setup()`` once per loop, held for the loop's lifetime."""
    loop_id = id(asyncio.get_running_loop())
    saver = _SQLITE_SAVER_BY_LOOP.get(loop_id)
    if saver is not None:
        return saver
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "CAIRN_CHECKPOINTER=sqlite under an async runtime needs the async "
            "SqliteSaver (aiosqlite); `pip install 'cairndev[runtime-sqlite]'`"
        ) from e
    path = settings.checkpointer_path()
    cm = AsyncSqliteSaver.from_conn_string(path)
    saver = await cm.__aenter__()
    await saver.setup()
    _SQLITE_CM_BY_LOOP[loop_id] = cm  # hold the CM so GC can't close the conn
    _SQLITE_SAVER_BY_LOOP[loop_id] = saver
    logger.info("async checkpointer: sqlite at %s (loop %d)", path, loop_id)
    return saver


async def _pg_pool_for_loop() -> Any:
    """Per-loop psycopg ``AsyncConnectionPool`` (psycopg pools are loop-bound).
    Opened lazily; ``AsyncPostgresSaver.setup()`` runs once per loop."""
    _prune_dead_pg_pools()  # reclaim pools of finished loops before we (maybe) add one
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    entry = _PG_POOL_BY_LOOP.get(loop_id)
    # Only a cache hit whose weakref still points at THIS loop is reusable — an
    # id() collision with a GC'd loop must fall through and build a fresh pool.
    pool = entry[0] if (entry is not None and entry[1]() is loop) else None
    if pool is None:
        dsn = settings.checkpointer_dsn()
        if not dsn:
            raise RuntimeError("CAIRN_CHECKPOINTER=postgres requires CAIRN_CHECKPOINTER_DSN")
        try:
            # optional dep: psycopg + psycopg_pool (runtime-postgres extra)
            from psycopg.rows import dict_row  # noqa: PLC0415
            from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "CAIRN_CHECKPOINTER=postgres under an async runtime needs psycopg + "
                "psycopg_pool; `pip install 'cairndev[runtime-postgres]'`"
            ) from e
        # autocommit + dict_row mirror AsyncPostgresSaver.from_conn_string so the
        # saver's writes commit immediately (a later resume session sees them).
        # Bounded size + short max_idle (#632) so per-loop pools can't overrun
        # Postgres max_connections under sustained durable-HITL load.
        pool = AsyncConnectionPool(
            dsn, open=False, min_size=1,
            max_size=_pg_pool_max_size(), max_idle=_pg_pool_max_idle(),
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        _PG_POOL_BY_LOOP[loop_id] = (pool, weakref.ref(loop))
        _PG_SETUP_LOOPS.discard(loop_id)  # fresh pool → re-run setup for this loop
        logger.info("async checkpointer: postgres pool (loop %d, max_size=%d)",
                    loop_id, _pg_pool_max_size())
    if loop_id not in _PG_SETUP_LOOPS:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415
        async with pool.connection() as conn:
            await AsyncPostgresSaver(conn).setup()
        _PG_SETUP_LOOPS.add(loop_id)
    return pool


def reset_checkpointer() -> None:
    """Drop cached checkpointer state (tests use this to isolate). Best-effort:
    clears the per-loop caches; async connections close on process exit / GC
    (the durable-runtime tests are subprocess-isolated)."""
    global _MEMORY_SINGLETON, _DURABLE_SINGLETON, _ASYNC_MEMORY_SINGLETON
    _MEMORY_SINGLETON = None
    _DURABLE_SINGLETON = None
    _ASYNC_MEMORY_SINGLETON = None
    _SQLITE_SAVER_BY_LOOP.clear()
    _SQLITE_CM_BY_LOOP.clear()
    _PG_POOL_BY_LOOP.clear()
    _PG_SETUP_LOOPS.clear()
