"""Async messaging — the `Bus` Protocol + `InProcessBus` (ADR-0010 P3.b).

One abstraction for events, notifications, and work signals, with tiered
backends picked by ``CAIRN_BUS`` (same pattern as §24.4 storage). Tier 1
(``InProcessBus``, the default) is asyncio-queue fan-out with topic dispatch +
wildcard prefix matching — zero new infra. Tier 2 is Postgres LISTEN/NOTIFY
(:mod:`cairn.core.bus.postgres`). These two are the only backends: the
``redis://`` placeholder that only logged "not implemented" was removed by
ADR-0127 D5 and is now refused at selection.

Honest limits (ADR-0010 §24.10.6): InProcessBus is **at-most-once** — if a
subscriber's handler raises, the message is logged and dropped, not retried.
Durable/at-least-once delivery needs a Tier 2/3 backend + the ``replay`` API.
This is fire-and-forget notification, not a queue or a workflow engine.

The run-event stream (``RunStore.append_event`` → ``_EventHub`` →
``subscribe`` → SSE) stays as-is; the dispatcher *also* publishes each run
event here on a ``run.<type>`` topic (the additive convergence bridge), so
topic/wildcard subscribers get them without disturbing the run-scoped path.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso

logger = logging.getLogger("cairn.bus")


class Message(BaseModel):
    """One published message. ``topic`` is a dotted hierarchy
    (``incident.rca_completed``); ``correlation_id`` carries a run/session id."""
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=utc_now_iso)
    publisher: Optional[str] = None
    correlation_id: Optional[str] = None


Handler = Callable[[Message], Awaitable[None]]


def _topic_matches(pattern: str, topic: str) -> bool:
    """Exact match, ``*`` (all), or prefix wildcard ``incident.*`` (matches any
    topic under the ``incident.`` namespace)."""
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])  # "incident.*" → prefix "incident."
    return False


class Subscription:
    """Handle returned by ``subscribe``. Owns a queue + a consumer task that
    feeds matched messages to the handler. ``unsubscribe()`` detaches + stops."""

    def __init__(self, bus: Any, patterns: list[str], handler: Handler) -> None:
        self._bus = bus
        self._patterns = patterns
        self._handler = handler
        self.queue: asyncio.Queue[Message] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    def matches(self, topic: str) -> bool:
        return any(_topic_matches(p, topic) for p in self._patterns)

    def _start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            msg = await self.queue.get()
            try:
                await self._handler(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — at-most-once: log + drop (§24.10.6)
                logger.warning("bus handler raised on topic %s — message dropped: %s", msg.topic, e)

    def unsubscribe(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        if self._task is not None:
            self._task.cancel()


@runtime_checkable
class Bus(Protocol):
    """Single abstraction for events, notifications, and work signals."""

    async def publish(
        self, topic: str, payload: dict[str, Any], *,
        publisher: Optional[str] = None, correlation_id: Optional[str] = None,
    ) -> None: ...

    async def subscribe(self, topic: "str | list[str]", handler: Handler) -> Subscription: ...

    async def replay(self, topic: str, since: datetime) -> AsyncIterator[Message]: ...


class InProcessBus:
    """Tier-1 bus — asyncio-queue subscribers + topic dispatch. No persistence,
    single process. ``replay`` is unsupported (Tier 2+ only)."""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    async def publish(
        self, topic: str, payload: dict[str, Any], *,
        publisher: Optional[str] = None, correlation_id: Optional[str] = None,
    ) -> None:
        msg = Message(
            topic=topic, payload=dict(payload),
            publisher=publisher, correlation_id=correlation_id,
        )
        # Snapshot: a handler may (un)subscribe during dispatch. put_nowait is
        # sync, so this loop is atomic within the event loop.
        for sub in list(self._subs):
            if sub.matches(topic):
                sub.queue.put_nowait(msg)

    async def subscribe(self, topic: "str | list[str]", handler: Handler) -> Subscription:
        patterns = [topic] if isinstance(topic, str) else list(topic)
        sub = Subscription(self, patterns, handler)
        self._subs.append(sub)
        sub._start()
        return sub

    async def replay(self, topic: str, since: datetime) -> AsyncIterator[Message]:
        raise NotImplementedError(
            "replay requires the Tier 2 bus (PostgresBus) — InProcessBus is live-only"
        )
        yield  # pragma: no cover — makes this an async generator

    def _remove(self, sub: Subscription) -> None:
        try:
            self._subs.remove(sub)
        except ValueError:
            pass


_BUS: Optional[Bus] = None


def bus_backend(raw: str) -> str:
    """The backend a ``CAIRN_BUS`` value selects — the ONE reading of the value,
    shared by :func:`get_bus` and ``cairn pipeline explain``.

    ``"postgres"`` for a ``postgres://``/``postgresql://`` DSN; ``"refused"`` for a
    ``redis://``/``rediss://`` URL (ADR-0127 D5: there is no Redis bus);
    ``"inprocess"`` when unset or ``inprocess``; ``"unrecognized"`` for anything
    else (which :func:`get_bus` warns about and runs in-process)."""
    value = raw.strip().lower()
    scheme = value.split("://", 1)[0] if "://" in value else ""
    if scheme in ("postgres", "postgresql"):
        return "postgres"
    if scheme in ("redis", "rediss"):
        return "refused"
    if value in ("", "inprocess"):
        return "inprocess"
    return "unrecognized"


def get_bus() -> Bus:
    """The process Bus, picked from ``CAIRN_BUS`` (default ``inprocess``). Cached.

    ``postgres://…`` → the Tier-2 :class:`~cairn.core.bus.postgres.PostgresBus`
    (#477 — durable LISTEN/NOTIFY + replay; the asyncpg dep is checked lazily on
    first use, so construction never fails). ``redis://``/``rediss://`` is refused with a
    ``ValueError`` naming the two supported backends (ADR-0127 D5: the branch
    that only logged "not implemented" and fell back is gone — a deployment that
    asked for a broker must not silently get an in-process bus)."""
    global _BUS
    if _BUS is not None:
        return _BUS
    # Preserve original case — a DSN carries credentials/host that must not be
    # lowercased; only the scheme test is case-insensitive.
    raw = getenv("CAIRN_BUS", "").strip()
    backend = bus_backend(raw)
    if backend == "postgres":
        from cairn.core.bus.postgres import PostgresBus

        _BUS = PostgresBus(dsn=raw)
        return _BUS
    if backend == "refused":
        # ADR-0127 D5 — fail closed: no Redis bus exists, so selecting one refuses.
        raise ValueError(
            "CAIRN_BUS=redis://… is not a supported bus backend. Supported: "
            "'inprocess' (the default, when unset) or a Postgres DSN "
            "'postgres://…' / 'postgresql://…'."
        )
    if backend == "unrecognized":
        logger.warning("CAIRN_BUS=%r unrecognized — using InProcessBus", raw)
    _BUS = InProcessBus()
    return _BUS


def reset_bus() -> None:
    """Drop the cached bus (tests, or after an CAIRN_BUS change)."""
    global _BUS
    _BUS = None


__all__ = [
    "Bus",
    "Handler",
    "InProcessBus",
    "Message",
    "Subscription",
    "bus_backend",
    "get_bus",
    "reset_bus",
]
