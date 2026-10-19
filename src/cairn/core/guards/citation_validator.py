"""
Deterministic citation validation — catch hallucinated evidence IDs.

An RCA template asks the LLM to cite IDs (``trace_id``, log id, ``error_id`` …)
in its ``evidence`` array. Nothing previously verified those IDs were real, so
an investigation could cite ``trace_id=abc123`` that was never in the data and
ship clean. This module closes that gap: it checks each cited ID against the
**ground-truth universe of IDs the investigation actually surfaced via tool
calls**, and reports a ``hallucination_rate``.

This is the project thesis in miniature — deterministic computation *behind* the
LLM, not more LLM. The check is cheap, offline, and non-destructive.

Ground truth by flow type
--------------------------
- ``agent_loop``: the ``ToolMessage`` contents in ``template_output["messages"]``
  (i.e. what the backend actually returned). ``AIMessage`` is **excluded** — the
  model must never be allowed to validate its own claims, or a hallucinated ID
  that appears in the model's prose would self-certify.
- ``state_machine`` / ``prompt``: no tool outputs are captured at the top level
  (state_machine nests tool calls inside sub-loops; prompt makes no tool calls),
  so there is no ground truth to check against. Validation is *skipped* with a
  recorded reason rather than guessed — a wrong "clean" verdict is worse than an
  honest "not checked".

The result is attached to the investigation output as ``citation_report`` so it
surfaces in notifications, memory, and the status API.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger("cairn.citation_validator")

# A citation must be at least this long to be eligible for a *substring* match
# against the raw tool-output corpus. Exact set-membership has no length floor;
# this only guards the fuzzy fallback so a 2-char id can't match by accident.
_MIN_SUBSTRING_LEN = 4

# Roles whose message content counts as ground truth. "ai" is deliberately
# absent — see module docstring.
_GROUND_TRUTH_ROLES = frozenset({"tool", "human", "system", "function"})


@dataclass
class CitationReport:
    """Outcome of validating an investigation's cited evidence."""

    checked: int = 0
    valid: list[dict[str, Any]] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)
    hallucination_rate: float = 0.0
    skipped: bool = False
    reason: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        return not self.invalid

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "valid_count": len(self.valid),
            "invalid_count": len(self.invalid),
            "invalid": self.invalid,
            "hallucination_rate": round(self.hallucination_rate, 4),
            "skipped": self.skipped,
            "reason": self.reason,
        }


# ─── message / content helpers ──────────────────────────────────────


def _msg_role_content(msg: Any) -> tuple[str, str]:
    """Return (role, text) for a LangChain message object or a plain dict."""
    # LangChain message objects expose `.type` ("human"/"ai"/"tool"/"system")
    role = getattr(msg, "type", None)
    content = getattr(msg, "content", None)
    if role is None and isinstance(msg, dict):
        role = msg.get("type") or msg.get("role") or ""
        content = msg.get("content")
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return (str(role or "").lower(), text or "")


def _walk_string_values(obj: Any, out: set[str]) -> None:
    """Collect every scalar string value in a nested JSON-ish structure."""
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            out.add(s)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_string_values(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_string_values(v, out)


def _collect_ground_truth(template_output: dict[str, Any]) -> Optional[tuple[set[str], str]]:
    """Build the (id_values, raw_corpus) universe the investigation surfaced.

    Returns ``None`` when no trustworthy ground truth is available (so the caller
    skips rather than guesses).
    """
    if not isinstance(template_output, dict):
        return None
    if template_output.get("type") != "agent_loop":
        return None
    messages = template_output.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    id_values: set[str] = set()
    corpus_parts: list[str] = []
    for msg in messages:
        role, text = _msg_role_content(msg)
        if role not in _GROUND_TRUTH_ROLES:
            continue  # skip the model's own AI turns
        if not text:
            continue
        corpus_parts.append(text)
        # If the tool returned JSON, harvest its scalar string values as a precise
        # id set. If not JSON, the raw corpus still backstops via substring.
        try:
            parsed = json.loads(text)
            _walk_string_values(parsed, id_values)
        except (ValueError, TypeError):
            pass

    if not corpus_parts:
        return None
    return (id_values, "\n".join(corpus_parts))


# ─── output parsing ─────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _loads_maybe_fenced(text: str) -> Optional[Any]:
    """Parse JSON from raw text, tolerating ```json fences and surrounding prose."""
    if not isinstance(text, str) or not text.strip():
        return None
    # 1. direct
    try:
        return json.loads(text)
    except ValueError:
        pass
    # 2. fenced ```json ... ```
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    # 3. first {...} span
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            pass
    return None


def _extract_cited_evidence(template_output: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the evidence array (each {type, citation, ...}) from the LLM output."""
    output = template_output.get("output") if isinstance(template_output, dict) else None
    parsed = _loads_maybe_fenced(output) if isinstance(output, str) else output
    if not isinstance(parsed, dict):
        return []
    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        return []
    return [e for e in evidence if isinstance(e, dict) and e.get("citation")]


# ─── public API ─────────────────────────────────────────────────────


def validate_citations(template_output: dict[str, Any]) -> CitationReport:
    """Verify every cited evidence ID exists in the investigation's fetched data.

    Non-destructive: returns a report. Safe to call on any template output —
    returns a *skipped* report when there is no ground truth (non-agent_loop, no
    tool messages) and a *clean* report when the output cites nothing.
    """
    ground = _collect_ground_truth(template_output)
    if ground is None:
        return CitationReport(
            skipped=True,
            reason="no tool-result ground truth (non-agent_loop or no tool calls)",
        )
    id_values, corpus = ground

    evidence = _extract_cited_evidence(template_output)
    if not evidence:
        return CitationReport(checked=0)  # nothing cited → trivially clean

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for ev in evidence:
        citation = str(ev.get("citation", "")).strip()
        ok = bool(citation) and (
            citation in id_values
            or (len(citation) >= _MIN_SUBSTRING_LEN and citation in corpus)
        )
        (valid if ok else invalid).append(ev)
        if not ok:
            _log.warning(
                "hallucinated citation: type=%s citation=%r not found in fetched data",
                ev.get("type"), citation,
            )

    checked = len(evidence)
    rate = len(invalid) / checked if checked else 0.0
    return CitationReport(
        checked=checked, valid=valid, invalid=invalid, hallucination_rate=rate
    )
