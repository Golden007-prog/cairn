"""CLI-local trace storage for ``cairn run`` and ``cairn replay``.

Each run materializes a directory under ``.cairn/runs/<run_id>/`` in the
caller's CWD containing:

  - ``meta.json``    — run metadata (id, started_at, trigger, status, …)
  - ``events.jsonl`` — append-only event log, one JSON object per line

The format is intentionally simple: ``replay`` reads the JSONL stream and
renders it. No DB schema, no migrations. The production dispatcher path
(``cairn.core.dispatch.dispatcher``) keeps using ``SqliteInvestigationStore``
— this is a *parallel* sink for local CLI work, both satisfying the
``EventSink`` shape from :mod:`cairn.core.engine.runner`.

This module is part of the CLI layer, not core: ``packs/`` and ``core/`` must
not import from here.
"""

from __future__ import annotations

from cairn.cli.runs.paths import (
    DEFAULT_RUNS_ROOT,
    events_path,
    meta_path,
    new_run_id,
    resolve_run_dir,
    runs_root,
)
from cairn.cli.runs.reader import (
    Event,
    read_events,
    read_meta,
)
from cairn.cli.runs.writer import (
    JsonlEventSink,
    write_meta,
)

__all__ = [
    "DEFAULT_RUNS_ROOT",
    "Event",
    "JsonlEventSink",
    "events_path",
    "meta_path",
    "new_run_id",
    "read_events",
    "read_meta",
    "resolve_run_dir",
    "runs_root",
    "write_meta",
]
