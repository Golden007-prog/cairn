"""Tier-2 Bus — Postgres ``LISTEN``/``NOTIFY`` (#477, ADR-0010 §24.10.6).

Durable, cross-process fan-out behind the same :class:`~cairn.core.bus.Bus`
Protocol as ``InProcessBus``. Every ``publish`` INSERTs the message into
``bus_messages`` (so it survives restarts and is queryable via ``replay``) and
fires ``NOTIFY cairn_bus, <message_id>``. A subscriber process holds one
dedicated ``LISTEN`` connection; on each notification it loads the row and
fans it out to local :class:`Subscription` queues — reusing the exact topic-
match + at-most-once handler machinery Tier 1 already has.

Delivery is at-most-once per live subscriber (a handler that raises drops the
message), but ``replay(topic, since)`` reads the durable log, so a subscriber
that was down can catch up — the Tier-1 → Tier-2 upgrade ADR-0010 promised.

Requires the ``storage-postgres`` optional dep group (asyncpg). Selected via
``CAIRN_BUS=postgres://…`` (see :func:`cairn.core.bus.get_bus`).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from cairn.core.bus import Handler, Message, Subscription, _topic_matches
from cairn.core.pg_schema_init import PgSchemaInit

_log = logging.getLogger("cairn.bus.postgres")

#: The single NOTIFY channel; the payload is the message id (well under
#: Postgres' 8000-byte NOTIFY limit — the full row is fetched by id).
_CHANNEL = "cairn_bus"


def _require_asyncpg() -> None:
    """Lazy import — keep the dep optional. Clean error if not installed."""
    try:
        import asyncpg  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "PostgresBus requires the 'storage-postgres' optional dep group "
            "(asyncpg). Install with: pip install 'cairndev[storage-postgres]'"
        ) from e


_CREATE_BUS_MESSAGES_SQL = """
    CREATE TABLE IF NOT EXISTS bus_messages (
        message_id     TEXT PRIMARY KEY,
        topic          TEXT NOT NULL,
        correlation_id TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        data           JSONB NOT NULL
    )
"""
_CREATE_BUS_CREATED_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_bus_messages_created ON bus_messages(created_at)"
)
_CREATE_BUS_TOPIC_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_bus_messages_topic ON bus_messages(topic)"
)
_INSERT_BUS_MESSAGE_SQL = (
    "INSERT INTO bus_messages (message_id, topic, correlation_id, data) "
    "VALUES ($1, $2, $3, $4)"
)
_SELECT_BUS_MESSAGE_SQL = "SELECT data::text AS data FROM bus_messages WHERE message_id = $1"
_SELECT_BUS_REPLAY_SQL = (
    "SELECT data::text AS data FROM bus_messages WHERE created_at >= $1 ORDER BY created_at"
)


class PostgresBus(PgSchemaInit):
    """Durable Tier-2 bus over ``LISTEN``/``NOTIFY``. Same Protocol as
    ``InProcessBus``; ``replay`` is supported (the durable log)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._listen_conn: Any = None
        self._subs: list[Subscription] = []
        self._schema_ready = False
        self._deliver_tasks: set[asyncio.Task] = set()

    async def _open_pool(self):
        _require_asyncpg()
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        dsn = self._dsn
        if not dsn:
            raise RuntimeError(
                "PostgresBus needs a DSN. Pass dsn= explicitly or set "
                "CAIRN_BUS=postgres://… before constructing the bus."
            )
        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_BUS_MESSAGES_SQL)
            await c.execute(_CREATE_BUS_CREATED_INDEX_SQL)
            await c.execute(_CREATE_BUS_TOPIC_INDEX_SQL)

    async def publish(
        self, topic: str, payload: dict[str, Any], *,
        publisher: Optional[str] = None, correlation_id: Optional[str] = None,
    ) -> None:
        msg = Message(
            topic=topic, payload=dict(payload),
            publisher=publisher, correlation_id=correlation_id,
        )
        message_id = uuid.uuid4().hex
        pool = await self._get_pool()
        await pool.execute(
            _INSERT_BUS_MESSAGE_SQL, message_id, topic, correlation_id, msg.model_dump_json()
        )
        # NOTIFY carries only the id; live subscribers fetch the row by id.
        await pool.execute("SELECT pg_notify($1, $2)", _CHANNEL, message_id)

    async def subscribe(self, topic: "str | list[str]", handler: Handler) -> Subscription:
        await self._ensure_listener()
        patterns = [topic] if isinstance(topic, str) else list(topic)
        sub = Subscription(self, patterns, handler)
        self._subs.append(sub)
        sub._start()
        return sub

    async def replay(self, topic: str, since: datetime) -> AsyncIterator[Message]:
        pool = await self._get_pool()
        rows = await pool.fetch(_SELECT_BUS_REPLAY_SQL, since)
        for row in rows:
            msg = Message.model_validate_json(row["data"])
            if _topic_matches(topic, msg.topic):
                yield msg

    async def _ensure_listener(self) -> None:
        """Acquire (and hold) one dedicated connection LISTENing on the channel.
        A pure-publisher process never calls this, so it holds no extra conn."""
        if self._listen_conn is not None:
            return
        pool = await self._get_pool()
        conn = await pool.acquire()
        await conn.add_listener(_CHANNEL, self._on_notify)
        self._listen_conn = conn

    def _on_notify(self, conn: Any, pid: int, channel: str, message_id: str) -> None:
        """asyncpg listener callback (sync, called on the event loop). Schedule
        the async load+fan-out; keep a task ref so it isn't GC'd mid-flight."""
        task = asyncio.create_task(self._deliver(message_id))
        self._deliver_tasks.add(task)
        task.add_done_callback(self._deliver_tasks.discard)

    async def _deliver(self, message_id: str) -> None:
        try:
            pool = await self._get_pool()
            row = await pool.fetchrow(_SELECT_BUS_MESSAGE_SQL, message_id)
            if row is None:
                return
            msg = Message.model_validate_json(row["data"])
            for sub in list(self._subs):
                if sub.matches(msg.topic):
                    sub.queue.put_nowait(msg)
        except Exception as e:  # noqa: BLE001 — delivery is best-effort
            _log.warning("PostgresBus delivery failed for %s: %s", message_id, e)

    def _remove(self, sub: Subscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    async def close(self) -> None:
        """Release the LISTEN connection + pool (shutdown/tests)."""
        if self._listen_conn is not None:
            try:
                await self._listen_conn.remove_listener(_CHANNEL, self._on_notify)
                await self._pool.release(self._listen_conn)
            except Exception:  # noqa: BLE001
                pass
            self._listen_conn = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False


__all__ = ["PostgresBus"]
