"""Bus → SSE hub bridge — cross-process live run events (#967, ADR-0010 §24.10).

The SSE stream (`GET /runs/{id}/stream`) tails the run store's **in-process**
`_hub`, which only sees events appended by THIS process. Under Temporal the node
events are appended by the WORKER process, so without this bridge the server's
live stream never sees them — the console showed "no active run" / no live
progress during a run.

This bridge subscribes ONCE per server process to the durable `run.*` bus
firehose (Tier-2 `PostgresBus` = `LISTEN`/`NOTIFY`, so no per-client DB listener)
and, on each message, re-reads the SHARED store for that run's new events and
republishes them into the local `_hub`. The existing `store.subscribe()` then
fans them out to every attached SSE client — live, sub-second, cross-process.

Design: the **store is the durable event log**; the bus is only a cross-process
**wake-up**. We deliberately do NOT trust the bus payload as the event of record
— we re-read the store so `seq` ordering + dedup stay authoritative. A future
NATS/JetStream tier swaps in behind the same `Bus` interface with no change here
(see the NATS follow-up issue).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from cairn.core.storage.protocols import TERMINAL_EVENT_TYPES

if TYPE_CHECKING:
    from cairn.core.bus import Message

logger = logging.getLogger("cairn.sse_bridge")


class SseEventBridge:
    """Feed the cross-process `run.*` bus firehose into the local store `_hub`."""

    def __init__(self, store: Any, *, bus: Any = None) -> None:
        self._store = store
        self._bus = bus
        self._sub: Any = None
        # Per-run high-water seq so each notify republishes only NEW events.
        self._last: dict[str, int] = {}

    async def start(self) -> None:
        from cairn.core.bus import get_bus  # noqa: PLC0415

        bus = self._bus or get_bus()
        hub = getattr(self._store, "_hub", None)
        if hub is None:  # a store without a live hub can't stream — inert
            logger.debug("sse bridge inert: store exposes no _hub")
            return
        self._sub = await bus.subscribe("run.*", self._on_message)
        logger.info("sse bridge live: run.* → store._hub (cross-process SSE)")

    async def _on_message(self, msg: "Message") -> None:
        run_id = msg.correlation_id
        if not run_id:
            return
        hub = getattr(self._store, "_hub", None)
        if hub is None:
            return
        last = self._last.get(run_id, 0)
        try:
            events = await self._store.get_events(run_id, after_seq=last)
        except Exception as e:  # noqa: BLE001 — a store hiccup must not kill the bridge
            logger.debug("sse bridge get_events failed for %s: %s", run_id, e)
            return
        terminal = False
        for ev in events:
            self._last[run_id] = ev.seq
            # Republish into the local hub; store.subscribe() dedups by seq, so a
            # same-process event already delivered is harmlessly ignored there.
            await hub.publish(ev)
            if ev.type in TERMINAL_EVENT_TYPES:
                terminal = True
        if terminal:
            self._last.pop(run_id, None)  # done — drop the high-water entry

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None
