"""The current-UTC-time helper every store and record writer shares.

A stdlib-only leaf: anything in ``cairn`` may import it without pulling in a
store, the engine schema or an import cycle (``storage/protocols`` itself
imports ``workspace`` and ``account``, which stamp rows with it).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Now, timezone-aware UTC, as an ISO-8601 string (``+00:00`` offset)."""
    return datetime.now(timezone.utc).isoformat()
