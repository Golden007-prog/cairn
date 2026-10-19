"""Path conventions for CLI run storage.

Single source of truth for ``where does run X's trace live``. Both writer
(``cairn run``) and reader (``cairn replay``) consult these helpers
so the layout is one change away across both commands.

Layout:
  <runs_root>/<run_id>/meta.json     # one-shot
  <runs_root>/<run_id>/events.jsonl  # append-only

Defaults to ``./.cairn/runs/`` in the current working directory. An
operator can override with ``CAIRN_RUNS_DIR`` when they want a project-wide or
HOME-relative location.
"""

from __future__ import annotations
from cairn.core.settings import getenv

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# CLI-only knob. Intentionally NOT added to the core settings inventory:
# this is a CLI-layer concern, not an engine knob, so it stays out of the
# `cairn doctor` settings table.
_RUNS_DIR_ENV = "CAIRN_RUNS_DIR"

#: Default relative location for run storage. Resolved against CWD at call
#: time (not at import time) so a test changing cwd sees the right dir.
DEFAULT_RUNS_ROOT = ".cairn/runs"


def runs_root() -> Path:
    """Resolve the runs storage root.

    Order of precedence:
      1. ``$CAIRN_RUNS_DIR`` if set (supports ``~`` expansion).
      2. ``./.cairn/runs/`` relative to current working directory.

    A non-default workspace (#229 B5, ADR-0048) nests under ``workspace/<id>/``
    so per-workspace traces don't collide on disk — the ``workspace/<id>/`` blob
    namespace the ADR specifies. The ``default`` workspace keeps the flat path
    (backward-compatible). Writer (`cairn run`) and reader (`cairn replay`) both
    resolve through here, so they stay symmetric under the active workspace.
    """
    override = getenv(_RUNS_DIR_ENV)
    base = Path(override).expanduser().resolve() if override else (Path.cwd() / DEFAULT_RUNS_ROOT).resolve()
    # local: cli ↔ core seam; the workspace lib is stdlib + pydantic only (no cycle).
    from cairn.core.workspace import DEFAULT_WORKSPACE, current_workspace
    ws = current_workspace()
    return base if ws == DEFAULT_WORKSPACE else base / "workspace" / ws


def new_run_id() -> str:
    """A sortable, distinctive run id.

    Shape: ``run-YYYYMMDD-HHMMSS-<8hex>``. UTC-anchored so cross-machine
    runs sort consistently. The hex suffix prevents collisions when two
    runs start within the same second.
    """
    now = datetime.now(timezone.utc)
    return f"run-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def resolve_run_dir(run_id: str, *, create: bool = False) -> Path:
    """Resolve the storage directory for a run.

    ``create=True`` makes the directory (and the runs root) if missing,
    which is what ``cairn run`` wants; ``replay`` leaves ``create=False``
    so a typo'd id surfaces as a clear "not found".
    """
    p = runs_root() / run_id
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def meta_path(run_id: str) -> Path:
    """Path to ``meta.json`` for a run (existence not guaranteed)."""
    return resolve_run_dir(run_id) / "meta.json"


def events_path(run_id: str) -> Path:
    """Path to ``events.jsonl`` for a run (existence not guaranteed)."""
    return resolve_run_dir(run_id) / "events.jsonl"
