"""Stage-progress → run-event bridge (ADR-0082 D8a, #1076).

Flow engines already surface ``{stage, stages_done, stages_total, elapsed_s}``
in every RUNNING poll's metrics (the conformance suite now requires it —
``supports_stage_events``). This module is the control-plane half of the
contract: turn those transitions into ``stage`` :class:`RunEvent`s keyed on
run_id, so the run timeline, SSE console, RCA correlation, and auto-eval see
engine work with ZERO engine-specific wiring — the same one-stream story as
the ``node`` events (#183).

Contract mirrors pins (#1072): NEVER raises, never blocks a poll — a broken
event store degrades to a log line, not a failed job.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from cairn.core.operator.reports import PROGRESS_EVENT_TYPE

_log = logging.getLogger("cairn.core.operator.stage_events")

# ADR-0103 (#2035): flow-engine stage progress is one producer of the generic
# ``progress`` event every operator's ``ctx.report.stage`` also emits; the
# console keys on one type. Kept as a name for existing importers.
STAGE_EVENT_TYPE = PROGRESS_EVENT_TYPE


async def emit_stage_event(
    run_id: str,
    *,
    node_name: str,
    job_id: str,
    metrics: dict[str, Any],
    last_done: Optional[int],
) -> Optional[int]:
    """Append a ``stage`` run event when the metrics show NEW stage progress.

    Dedupe key is ``stages_done``: an event fires only when it changed since
    ``last_done`` (callers thread the returned value back in). Metrics without
    ``stages_done`` are not stage progress — nothing fires. Returns the new
    watermark (or ``last_done`` unchanged). NEVER raises.
    """
    try:
        done = metrics.get("stages_done")
        if done is None or not run_id:
            return last_done
        done = int(done)
        if last_done is not None and done <= last_done:
            return last_done
        from cairn.core.storage import get_storage  # noqa: PLC0415 — keep the storage stack off the operator import path (runtime.py convention)

        total = int(metrics.get("stages_total", 0) or 0)
        payload: dict[str, Any] = {
            "node": node_name,
            "job_id": job_id,
            "stage": str(metrics.get("stage", "") or ""),
            "stages_done": done,
            "stages_total": total,
            "elapsed_s": metrics.get("elapsed_s"),
        }
        if total > 0:
            payload["percent"] = round(100.0 * min(done, total) / total, 1)
        await get_storage().runs.append_event(run_id, STAGE_EVENT_TYPE, payload)
        return done
    except Exception:  # noqa: BLE001 — stage events must never fail or delay a poll
        _log.warning("stage event for run %r failed (poll unaffected)", run_id, exc_info=True)
        return last_done
