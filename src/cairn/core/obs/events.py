"""Structured event sink — one JSON line per request.

Default sink: ``<project_root>/events.jsonl``. The path is project-relative
(not CWD-relative) so it lands in the same place whether the app is run
via ``make dev``, pytest, or a docker entrypoint.

Override with ``CAIRN_EVENTS_PATH`` — the docker-compose stack points the
app and the dashboard service at the same bind-mounted file so both see
the same event log.

Production swap path: change ``write`` to push to ClickHouse / Kafka / etc.
Call sites stay unchanged because they only see ``events.write(ctx)``.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import json
import os
from dataclasses import asdict
from pathlib import Path

from cairn.core.obs.base import RequestCtx

# events.py lives at <root>/cairn/core/obs/events.py — parents[3] is <root>.
# (The old comment claimed parents[2] from the pre-rename src/obs/ layout; that
# silently landed the default inside the cairn/ package dir.)
_PATH = Path(getenv(
    "CAIRN_EVENTS_PATH",
    str(Path(__file__).resolve().parents[3] / "events.jsonl"),
))


def write(ctx: RequestCtx) -> None:
    """Append one event to the sink. Telemetry must never break a request,
    so all errors are swallowed."""
    try:
        with _PATH.open("a") as f:
            f.write(json.dumps(asdict(ctx), default=str) + "\n")
    except Exception:
        pass
