"""#614 — one asyncpg pool per (DSN, event-loop), shared across every Postgres
store, instead of each store opening its own.

The run store, eval ledger, RAG catalog and BYO-corpus store each called
``asyncpg.create_pool`` independently (``max_size`` 4–8 apiece). Across those 4
store classes × the server + worker + worker-remote processes that overran
Postgres ``max_connections`` — seen live as ``TooManyConnectionsError`` (103/100).
Sharing one sized pool per DSN caps a process at a single pool regardless of how
many stores read it.

Keyed by ``(dsn, id(loop))`` so a fresh event loop (e.g. a test) gets its own
pool and never reuses one bound to a closed loop; within a process's single loop
every store on the same DSN shares one pool. Lock-free and race-free: the cache
entry is a Future registered *before* the first ``create_pool`` is awaited, so
concurrent first-callers await the same in-flight creation rather than each
opening a pool (and a single event loop never interleaves the get→register pair,
which has no ``await`` between them).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
from typing import Any

# (dsn, purpose, loop_id) → Future[asyncpg.Pool]. The Future doubles as the
# in-flight marker so concurrent callers coalesce onto one creation.
#
# `purpose` (#1532p2) is normally "" — every store shares that one pool, which
# is the whole point of #614/#666. A NON-empty purpose buys a separate pool for
# a caller that provably must not borrow a store connection: the advisory-lock
# sections hold a connection for the length of a critical section while the
# section body does store I/O, so sharing would let N holders pin N connections
# and then all wait for one more — a self-exhaustion deadlock. Creation still
# happens HERE, so the #666 guard ("no app store calls create_pool itself")
# holds, sizing stays central and env-tunable, and reset_pools() still closes
# everything. Use it only with that justification.
_POOLS: dict[tuple[str, str, int], "asyncio.Future[Any]"] = {}


def _default_max_size() -> int:
    """Process-wide pool ceiling. ``CAIRN_PG_POOL_MAX`` overrides (default 10) so a
    deployment can size it against its Postgres ``max_connections`` and process
    count."""
    try:
        return max(1, int(getenv("CAIRN_PG_POOL_MAX", "10")))
    except ValueError:
        return 10


async def shared_pool(dsn: str, *, purpose: str = "", min_size: int = 1,
                      max_size: int | None = None) -> Any:
    """Return the process-shared asyncpg pool for ``dsn`` on the current event
    loop, creating it exactly once. ``max_size`` defaults to
    :func:`_default_max_size`. Raises the underlying ``create_pool`` error (and
    forgets the entry so a later call can retry) if creation fails.

    ``purpose`` partitions the cache — see the ``_POOLS`` comment. Leave it
    empty unless the caller must not borrow a store connection."""
    import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)

    loop = asyncio.get_running_loop()
    key = (dsn, purpose, id(loop))
    fut = _POOLS.get(key)
    if fut is not None:
        return await fut  # already created, or created concurrently on this loop

    fut = loop.create_future()
    _POOLS[key] = fut  # register BEFORE the await → no duplicate pools
    try:
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size or _default_max_size()
        )
    except BaseException as e:  # noqa: BLE001 — propagate; allow a later retry
        _POOLS.pop(key, None)
        if not fut.done():
            fut.set_exception(e)
        raise
    fut.set_result(pool)
    return pool


async def reset_pools() -> None:
    """Close and drop every cached pool. For test teardown and graceful
    shutdown — never call mid-request."""
    entries = list(_POOLS.items())
    _POOLS.clear()
    for _key, fut in entries:
        if not fut.done():
            continue
        try:
            pool = fut.result()
        except BaseException:  # noqa: BLE001 — a failed creation has nothing to close
            continue
        try:
            await pool.close()
        except Exception:  # noqa: BLE001 — best effort
            pass
