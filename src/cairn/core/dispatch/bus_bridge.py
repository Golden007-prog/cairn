"""Bus → skill bridge — cross-pack event activation (ADR-0010 P3.c, closes §24.3.7).

A sibling to :class:`SkillScheduler`: where the scheduler turns cron expressions
into skill activations, this turns **bus messages** into them. At start it
subscribes to the union of every loaded skill's ``triggers.subscribes_to`` topic
patterns; when a matching message arrives it builds a ``source="event"``
:class:`TriggerEvent` (topic in ``matched_pattern``) and submits it through the
dispatcher's background lane — the engine then routes it to the precise
subscribing skills via ``match_skill_to_event``.

Loop guard: the engine's own ``run.*`` lifecycle firehose (which the dispatcher
publishes for live subscribers) is **reserved** and never re-activates a skill —
``subscribes_to`` is for cross-pack *domain* topics a pack publishes explicitly
(e.g. ``incident.rca_completed``), not engine internals. Without this guard a
skill subscribing to ``run.done`` would re-trigger on its own completion.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from cairn.core.engine.runner import Runner
from cairn.core.engine.schema import TriggerEvent

if TYPE_CHECKING:
    from cairn.core.dispatch.dispatcher import RunDispatcher

logger = logging.getLogger("cairn.bus_bridge")

_RESERVED_PREFIX = "run."  # engine lifecycle firehose — not an activation source


class BusSkillBridge:
    """Activates skills from bus messages on their ``subscribes_to`` topics."""

    def __init__(
        self,
        runner: Runner,
        dispatcher: "RunDispatcher | None" = None,
        *,
        bus=None,
    ) -> None:
        self.runner = runner
        self.dispatcher = dispatcher
        self._bus = bus
        self._sub = None

    def subscribed_topics(self) -> list[str]:
        """Union of every loaded skill's declared bus topics. Empty (inert) when
        the runner exposes no skill engine (e.g. a minimal/test runner)."""
        engine = getattr(self.runner, "skill_engine", None)
        topics: set[str] = set()
        for spec in getattr(engine, "skills", []) or []:
            for t in getattr(spec.triggers, "subscribes_to", None) or []:
                topics.add(t)
        return sorted(topics)

    async def start(self) -> int:
        """Subscribe to all declared topics. Returns the count (0 → inert)."""
        from cairn.core.bus import get_bus

        topics = self.subscribed_topics()
        if not topics:
            return 0
        bus = self._bus or get_bus()
        self._sub = await bus.subscribe(topics, self._on_message)
        logger.info("bus bridge subscribed to %d topic(s): %s", len(topics), topics)
        return len(topics)

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None

    async def _on_message(self, msg) -> None:
        if msg.topic.startswith(_RESERVED_PREFIX):
            return  # engine lifecycle firehose is reserved (loop guard)
        event = TriggerEvent(
            source="event",
            matched_pattern=msg.topic,
            payload=dict(msg.payload),
            timestamp=msg.ts,
        )
        logger.info("bus bridge activating on topic=%s", msg.topic)
        try:
            if self.dispatcher is not None:
                await self.dispatcher.submit(event, lane="background")
            else:
                await self.runner.run(event)
        except Exception:  # noqa: BLE001 — a bad activation must not kill the bus
            logger.exception("bus bridge fire failed for topic=%s", msg.topic)
