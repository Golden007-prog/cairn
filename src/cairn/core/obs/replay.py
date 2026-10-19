"""Snapshot writer — one pretty-printed JSON file per request.

Same data as ``events.jsonl`` (today), separate file so you can
``cat replays/<request_id>.json`` after a bug report and diff against a
re-run. As deeper instrumentation lands (raw prompts, raw MCP responses)
they'll accumulate on ``RequestCtx`` and flow through here automatically.

Default sink: ``<project_root>/replays/{request_id}.json``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from cairn.core.obs.base import RequestCtx

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parents[2] / "replays"


def save(ctx: RequestCtx) -> None:
    """Persist a full snapshot of the request context.

    Best-effort: errors are logged and swallowed — telemetry must not
    break the request path.
    """
    payload = json.dumps(asdict(ctx), default=str, indent=2)
    try:
        _save_fs(ctx.request_id, payload)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("replay.save failed for %s: %s", ctx.request_id, exc)


def _save_fs(request_id: str, payload: str) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    (_DIR / f"{request_id}.json").write_text(payload)
