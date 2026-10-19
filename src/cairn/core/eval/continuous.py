"""Continuous-eval consumer (#477, ADR-0060/#480) — a Bus observer that announces
completed runs OUT of the dispatcher's inline terminal path, for durable / out-of-
process topologies (a separate eval worker consuming a Tier-2 ``PostgresBus``).

Subscribes to the run-completion firehose (``run.done`` / ``run.failed``, the
same ``run.<type>`` topics the dispatcher publishes) and fires the matching
``cairn.run_hooks`` event (``run.finished`` / ``run.failed``, #2421 decision 7) —
the same event the dispatcher fires inline, so whoever scores a run there (the
platform's ``scoring`` provider: labels, verdict, training-history row, divergence
reaction) scores it here too. Notification-only (ADR-0010): it OBSERVES a
completed run; it never triggers a skill or mutates a workflow.

Idempotent by contract: the event carries ``source="continuous_eval"``, and a
listener that writes skips a run it already handled — so turning this on beside
single-process inline scoring is safe (the listeners find nothing to do). The
point is the *other* topology: inline scoring in a different process, scoring
driven entirely by the durable bus. No listener installed: nothing happens.

Opt-in: ``CAIRN_CONTINUOUS_EVAL=on``. Off by default — inline scoring (#484) is
the single-process default and double-wiring would be redundant.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import logging
import os
from typing import Any, Optional

_log = logging.getLogger("cairn.eval.continuous")

#: The completion firehose topics the consumer scores on. Matches the
#: dispatcher's terminal ``_emit`` types (``done`` / ``failed``) → ``run.<type>``.
_COMPLETION_TOPICS = ["run.done", "run.failed"]

#: The ``source`` the re-fired run events carry (see :mod:`cairn.core.pack.run_hooks`).
SOURCE = "continuous_eval"


def continuous_eval_enabled() -> bool:
    return getenv("CAIRN_CONTINUOUS_EVAL", "").strip().lower() in ("1", "on", "true", "yes")


class ContinuousEvalConsumer:
    """Re-announces runs on the bus completion firehose as run-hook events. ``start``
    subscribes, ``stop`` unsubscribes; the handler is best-effort (``fire`` never
    raises on these events)."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._sub: Any = None

    async def _on_completion(self, msg: Any) -> None:
        run_id = msg.correlation_id or (msg.payload or {}).get("run_id")
        if not run_id:
            return
        from cairn.core.pack import run_hooks  # noqa: PLC0415 — off the import path

        failed = getattr(msg, "topic", "") == "run.failed"
        await run_hooks.fire(run_hooks.RUN_FAILED if failed else run_hooks.RUN_FINISHED,
                             run_id=run_id, status="failed" if failed else "done",
                             store=self._store, source=SOURCE)

    async def start(self, bus: Any) -> "ContinuousEvalConsumer":
        self._sub = await bus.subscribe(_COMPLETION_TOPICS, self._on_completion)
        _log.info("continuous-eval consumer subscribed to %s", _COMPLETION_TOPICS)
        return self

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None


async def maybe_start_continuous_eval(bus: Any, store: Any) -> Optional[ContinuousEvalConsumer]:
    """Start the consumer iff ``CAIRN_CONTINUOUS_EVAL`` is on. Returns the running
    consumer (so the caller can ``stop()`` it on shutdown) or ``None``."""
    if not continuous_eval_enabled():
        return None
    return await ContinuousEvalConsumer(store).start(bus)


__all__ = [
    "ContinuousEvalConsumer",
    "SOURCE",
    "continuous_eval_enabled",
    "maybe_start_continuous_eval",
]
