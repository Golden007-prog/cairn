"""Writer side of the CLI trace store: a JSONL :class:`EventSink`.

``JsonlEventSink`` is the bridge between the engine's streaming-events API
(:data:`cairn.core.engine.runner.EventSink`) and the on-disk JSONL log
that ``cairn replay`` reads. Each call appends one line:

    {"ts": "<iso8601>", "type": "<event_type>", "data": {…}}

Writes are non-blocking from the runner's point of view (the sink is async)
and best-effort: a disk error here logs but never breaks the run, matching
the contract :meth:`Runner._emit` already enforces upstream.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from cairn.cli.runs.paths import events_path, meta_path, resolve_run_dir
from cairn.core._time import utc_now_iso

_log = logging.getLogger("cairn.cli.runs.writer")


class JsonlEventSink:
    """``EventSink``-shaped callable that appends to ``events.jsonl``.

    The sink stays open across the whole run; the file handle is opened
    lazily on the first event so an empty / aborted run still leaves a
    correct meta.json without an empty event file.

    Use as::

        sink = JsonlEventSink(run_id)
        await runner.run(event, event_sink=sink)
        sink.close()
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        # Make sure the run dir exists so write_meta has somewhere to land.
        self._dir = resolve_run_dir(run_id, create=True)
        self._events_path = events_path(run_id)
        self._fh = None  # opened lazily on first event

    async def __call__(self, type: str, data: dict[str, Any]) -> None:
        """The EventSink shape: ``await sink(type, data)``."""
        if self._fh is None:
            # text mode + line-buffered = each event flushed; cheap because
            # event volume is small (5–20 per skill, typically).
            self._fh = self._events_path.open("a", buffering=1)
        line = json.dumps(
            {"ts": utc_now_iso(), "type": type, "data": data},
            default=_json_default,
        )
        try:
            self._fh.write(line + "\n")
        except Exception as e:  # noqa: BLE001 - streaming is non-critical
            _log.debug("event write failed for %s/%s: %s", self.run_id, type, e)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


def write_meta(run_id: str, meta: dict[str, Any]) -> Path:
    """Write the one-shot ``meta.json`` for a run.

    Called once at run start (status="running") and once at end
    (status="done"/"failed", ended_at, summary). Overwrite is intentional:
    meta is *not* append-only, events are.
    """
    path = meta_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(meta, indent=2, default=_json_default)
    path.write_text(payload)
    return path


def _json_default(obj: Any) -> Any:
    """JSON fallback: Pydantic models, datetimes, sets, paths."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    # LangGraph HITL control objects (Interrupt/Command) carry the pause
    # payload; capture it rather than crash the meta writer. Duck-typed to
    # avoid depending on langgraph's internal type layout.
    if type(obj).__name__ == "Interrupt":
        return {"__interrupt__": getattr(obj, "value", None)}
    # Last resort: trace serialization must never break a run.
    _log.debug("trace: stringifying non-JSON type %s", type(obj).__name__)
    return str(obj)
