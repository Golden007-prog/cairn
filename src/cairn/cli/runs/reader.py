"""Reader side of the CLI trace store.

Parses the JSONL written by :class:`~cairn.cli.runs.writer.JsonlEventSink`
back into typed events plus the one-shot ``meta.json``. ``cairn replay``
consumes these directly.

Tolerant by design: a malformed line is skipped (with a debug log) rather
than crashing the whole replay — the user wants to see *what we have*, not
hit a parse error on event 47 of 200.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cairn.cli.runs.paths import events_path, meta_path, resolve_run_dir

_log = logging.getLogger("cairn.cli.runs.reader")


@dataclass(frozen=True)
class Event:
    """One row in the events JSONL.

    ``ts`` is an ISO-8601 string (UTC). ``type`` matches the literals the
    :class:`~cairn.core.engine.runner.Runner` emits today:
    ``skill_started`` / ``findings`` / ``action_decision`` /
    ``action_outcome`` / ``skill_completed``. Replay treats unknown types
    as generic so future event kinds render without a release here.
    """

    ts: str
    type: str
    data: dict[str, Any]


def read_meta(run_id: str) -> dict[str, Any] | None:
    """Read ``meta.json`` for a run. Returns None if the run dir or file is
    absent (replay surfaces a clear "not found" error from the CLI layer).
    """
    path = meta_path(run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        _log.warning("meta.json for %s is malformed: %s", run_id, e)
        return None


def read_events(run_id: str) -> Iterator[Event]:
    """Yield events in write order.

    Lines that fail to parse are skipped — the caller still gets every
    well-formed event before and after the bad row. (We never silently
    truncate the stream.)
    """
    path = events_path(run_id)
    if not path.exists():
        return
    with path.open() as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                yield Event(ts=obj["ts"], type=obj["type"], data=obj.get("data", {}))
            except (json.JSONDecodeError, KeyError) as e:
                _log.debug("skipping malformed event %s:%d (%s)", path, lineno, e)


def run_dir_exists(run_id: str) -> bool:
    """True iff the run's storage dir is on disk. Cheap existence check
    used by ``replay`` to short-circuit a not-found case before parsing.
    """
    return resolve_run_dir(run_id).exists()
