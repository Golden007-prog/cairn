"""redact/text — zero-dep regex PII/secret redactor (ADR-0028 builtin default).

The detection lives HERE, in the operator pack — core holds only the
``Redactor`` interface + the chokepoint (`cairn.core.obs.redact`). This
class is dual-purpose:

  - ``redact_text(text)`` — the service path. ``get_redactor()`` resolves this
    operator by name (``redact/text``), instantiates it once, and the LLM
    gateway calls ``redact_text`` per request.
  - ``run(inp, ctx)``     — the node path, so the redactor is also usable as a
    template ``operation`` (e.g. ModelOps dataset-PII auditing).

Recognizers are ordered most-specific-first; overlapping matches resolve to the
earliest, then longest, so a key embedded in a PEM block doesn't double-redact.
"""
from __future__ import annotations

import re

from cairn.core.operator import RedactionResult, RedactSpan
from cairn.core.operator import Operation, OperationContext, ReportSpec, register_operator

from operator_redact_regex.schemas.redact_text import (
    RedactTextInput,
    RedactTextOutput,
)

# (kind, compiled pattern). Order = priority for overlap resolution.
_RECOGNIZERS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _scan(text: str) -> list[RedactSpan]:
    raw: list[tuple[int, int, str]] = []
    for kind, pat in _RECOGNIZERS:
        for m in pat.finditer(text):
            if kind == "credit_card" and not _luhn_ok(m.group()):
                continue
            raw.append((m.start(), m.end(), kind))
    # earliest start wins; at a tie, longest match wins
    raw.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    chosen: list[RedactSpan] = []
    last_end = -1
    for start, end, kind in raw:
        if start >= last_end:
            chosen.append(RedactSpan(kind=kind, start=start, end=end))
            last_end = end
    return chosen


def redact(text: str) -> RedactionResult:
    spans = _scan(text)
    if not spans:
        return RedactionResult(text=text, spans=[])
    parts: list[str] = []
    prev = 0
    for sp in spans:
        parts.append(text[prev:sp.start])
        parts.append(f"[REDACTED:{sp.kind}]")
        prev = sp.end
    parts.append(text[prev:])
    return RedactionResult(text="".join(parts), spans=spans)


@register_operator(
    name="redact/text",
    version="0.1.0",
    capabilities=["redact.text"],
    implements=["contract://redact.text/1.0.0"],  # #2243 V2 — cairn-contracts-redact
)
class RedactTextOp(Operation[RedactTextInput, RedactTextOutput]):
    """Regex PII/secret redactor. Backs the substrate ``redact.text`` service."""

    input_schema = RedactTextInput
    output_schema = RedactTextOutput
    supports_mock = True
    side_effect_free = True  # #836 — in-memory transform; live in sandbox
    reports = ReportSpec.NONE  # ADR-0103: one regex pass, nothing to report

    # ── service path (called by obs.redact.get_redactor) ──────────────
    async def redact_text(self, text: str) -> RedactionResult:
        return redact(text)

    # ── node path (usable as a template `operation`) ──────────────────
    async def run(self, inp: RedactTextInput, ctx: OperationContext) -> RedactTextOutput:
        res = redact(inp.text)
        return RedactTextOutput(
            ok=True,
            text=res.text,
            redacted=res.redacted,
            span_count=len(res.spans),
            kinds=sorted({s.kind for s in res.spans}),
        )

    def mock(self, inp: RedactTextInput, ctx: OperationContext) -> RedactTextOutput:
        return RedactTextOutput(ok=True, text=inp.text, redacted=False, span_count=0, kinds=[])
