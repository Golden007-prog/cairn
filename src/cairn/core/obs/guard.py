"""Input-side guard — cheap defenses against pathological queries.

What it does today:
  - Caps query length so a 10MB paste can't push the LLM bill or context window.
  - Strips ASCII control chars (keeping \\n and \\t) so smuggled bytes don't
    end up in templates or logs.

What it does NOT do today:
  - No heuristic prompt-injection scanning (high false-positive rate, easy
    to bypass — better handled by structured router output + tool allowlist).
  - No semantic toxicity / PII detection on the input.

This is the entry point where a real injection scanner plugs in — keep the
signature ``sanitize_query(q: str) -> str`` stable.

The raw query is preserved in ``RequestCtx.query`` (audit trail). Only the
sanitized version is passed downstream to the graph / LLM.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Generous: most legitimate observability questions are <500 chars; the cap
# is for "user pasted 50KB of stack trace" cases, not for typical use.
DEFAULT_MAX_LEN = 2000


def sanitize_query(q: str, *, max_len: int = DEFAULT_MAX_LEN) -> str:
    """Return a length-capped, control-char-stripped version of ``q``."""
    cleaned = "".join(c for c in q if ord(c) >= 32 or c in "\n\t")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    if cleaned != q:
        # Visible in logs / OTEL; the raw query lives on ctx.query for audit.
        logger.info(
            "guard.sanitize_query: modified input (%d → %d chars)",
            len(q), len(cleaned),
        )
    return cleaned
