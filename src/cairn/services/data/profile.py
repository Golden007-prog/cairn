"""Semantic data profiling — tiers 1+2 (+3a) of ADR-0074.

Given a set of records and a workflow's ``DataExpectation`` (the semantic ROLES
its operators read, not a wire format), answer the one question every surface
needs: *will this data run here?* — and degrade gracefully. This is the shared
logic behind the `data/profile` builtin operator, the Playground upload preview,
and the run pre-flight.

Tier 1 (format/parse) already happened in the loader (parse-or-throw). Tier 2
(semantic roles): resolve each role to a real column and report coverage; a
missing *required* role blocks, a missing *feature-gating* role degrades. Tier 3a
(per-record structural, opt-in via ``strictness: strict``): validate every record
against ``record_schema`` (JSON Schema), or — for known fine-tuning row formats —
the built-in conversational/alpaca/completion checks.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from cairn.services.data.loaders import cell, detect_dataset_format

_USER_ROLES = {"user", "human"}
_ASSISTANT_ROLES = {"assistant", "gpt", "bot", "model"}


class Role(BaseModel):
    """A semantic field a workflow's operators read."""
    name: str
    required: bool = False
    aliases: list[str] = Field(default_factory=list)
    enables: str = Field("", description="What capability this optional role unlocks (for the degrade message).")


class DataExpectation(BaseModel):
    """What a workflow expects of its input data — declared next to the input
    schema. Roles, not a wire format (ADR-0074)."""
    kind: Literal["rows", "spans", "metrics", "logs", "documents"] = "rows"
    container: list[str] = Field(default_factory=list, description="Accepted wire formats (informational; the loader picks).")
    format_families: list[str] = Field(default_factory=list, description="Optional fine-tuning formats, auto-detected.")
    roles: list[Role] = Field(default_factory=list)
    strictness: Literal["profile", "strict"] = "profile"
    record_schema: Optional[dict] = Field(default=None, description="JSON Schema validated per record when strict.")
    binds_to: str = Field("", description="Which input arg receives the (materialized) source.")
    unstructured: bool = Field(False, description="True → content is the only role; format-free (RAG/logs).")


class RoleReport(BaseModel):
    name: str
    required: bool
    resolved: bool
    column: Optional[str] = None
    coverage: float = 0.0  # fraction of records where the role has a non-empty value


class DataProfile(BaseModel):
    """The verdict + evidence — the shape the Playground already renders."""
    verdict: Literal["pass", "degrade", "block"]
    record_count: int
    kind: str
    detected_format: str = "unknown"
    roles: list[RoleReport] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict != "block"


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _turns(row: dict, fmt: str) -> list[tuple[str, object]]:
    if fmt == "openai_chat":
        return [(m.get("role", ""), m.get("content", "")) for m in row.get("messages", []) if isinstance(m, dict)]
    if fmt == "sharegpt":
        return [(t.get("from", ""), t.get("value", "")) for t in row.get("conversations", []) if isinstance(t, dict)]
    return []


def _validate_conversational(turns: list[tuple[str, object]]) -> Optional[str]:
    if not turns:
        return "no turns"
    body = [t for t in turns if str(t[0]).lower() != "system"]
    if not body:
        return "only system turns"
    for role, content in body:
        if not str(content).strip():
            return "empty turn content"
        if str(role).lower() not in _USER_ROLES | _ASSISTANT_ROLES:
            return f"unknown role {role!r}"
    return None


def _validate_row(row: dict, fmt: str) -> Optional[str]:
    """Built-in per-record structural check for the known fine-tuning formats
    (tier 3a for rows without an explicit record_schema)."""
    if fmt in ("openai_chat", "sharegpt"):
        return _validate_conversational(_turns(row, fmt))
    if fmt == "alpaca":
        if not str(row.get("instruction", "")).strip():
            return "empty instruction"
        if not str(row.get("output", "")).strip():
            return "empty output"
        return None
    if fmt == "completion":
        if row.get("text") is not None:
            return None if str(row.get("text", "")).strip() else "empty text"
        if not str(row.get("prompt", "")).strip() or not str(row.get("completion", "")).strip():
            return "empty prompt/completion"
        return None
    return "unrecognized row format"


def _run_record_schema(records: list[dict], schema: dict) -> tuple[int, list[str]]:
    """Validate each record against a JSON Schema. Returns (n_failed, sample_msgs)."""
    try:
        from jsonschema import Draft202012Validator  # base dep (used by coerce_and_validate)
    except ModuleNotFoundError:  # pragma: no cover
        return 0, []
    validator = Draft202012Validator(schema)
    n_bad = 0
    msgs: list[str] = []
    for i, rec in enumerate(records):
        errs = sorted(validator.iter_errors(rec), key=lambda e: e.path)
        if errs:
            n_bad += 1
            if len(msgs) < 5:
                msgs.append(f"record {i}: {errs[0].message}")
    return n_bad, msgs


def profile_data(records: list[dict], expectation: DataExpectation) -> DataProfile:
    """Profile ``records`` against ``expectation`` → a PASS / DEGRADE / BLOCK
    verdict with per-role coverage. Pure + deterministic (hermetic)."""
    n = len(records)
    kind = expectation.kind
    fmt = detect_dataset_format(records) if kind == "rows" else "n/a"

    # Tier 1 — nothing parsed.
    if n == 0:
        return DataProfile(verdict="block", record_count=0, kind=kind, detected_format=fmt,
                           errors=["no records — the file is empty or did not parse into rows"],
                           note="Nothing to profile. Check the file format / that it isn't empty.")

    # Unstructured (RAG/logs): the only requirement is non-empty content.
    roles = expectation.roles
    if expectation.unstructured and not roles:
        roles = [Role(name="content", required=True, aliases=["text", "body", "page_content", "value"])]

    # Tier 2 — resolve roles + coverage.
    reports: list[RoleReport] = []
    missing_required: list[str] = []
    degraded: list[str] = []
    for role in roles:
        hits = 0
        column: Optional[str] = None
        for rec in records:
            v = cell(rec, role.name, role.aliases)
            if _nonempty(v):
                hits += 1
                if column is None:
                    # record which real key resolved (exact or alias)
                    for k in (role.name, *role.aliases):
                        if k in rec or k.lower() in {kk.lower() for kk in rec}:
                            column = next((kk for kk in rec if kk.lower() == k.lower()), role.name)
                            break
        coverage = hits / n if n else 0.0
        resolved = hits > 0
        reports.append(RoleReport(name=role.name, required=role.required, resolved=resolved,
                                  column=column, coverage=round(coverage, 4)))
        if not resolved:
            if role.required:
                missing_required.append(role.name)
            else:
                degraded.append(role.enables or role.name)

    errors: list[str] = []
    verdict: Literal["pass", "degrade", "block"] = "pass"
    if missing_required:
        verdict = "block"
        for name in missing_required:
            errors.append(f"required field '{name}' not found in any record — map a column to it or add it")
    elif degraded:
        verdict = "degrade"

    # Rows with a declared format family (fine-tuning data): the FORMAT is the
    # requirement, since chat rows nest content inside `messages`/`conversations`
    # and flat roles can't resolve them. Unrecognized format → won't train → block.
    if kind == "rows" and expectation.format_families:
        if fmt == "unknown" or fmt not in expectation.format_families:
            verdict = "block"
            errors.append(
                f"unrecognized dataset format — expected one of {expectation.format_families}, detected '{fmt}'")

    # Tier 3a — per-record structural, opt-in.
    if expectation.strictness == "strict" and verdict != "block":
        if expectation.record_schema:
            n_bad, msgs = _run_record_schema(records, expectation.record_schema)
            if n_bad:
                verdict = "block"
                errors.append(f"{n_bad} of {n} records fail record_schema")
                errors.extend(msgs)
        elif kind == "rows":
            bad = [(i, _validate_row(r, fmt)) for i, r in enumerate(records)]
            bad = [(i, m) for i, m in bad if m]
            if bad:
                verdict = "block"
                errors.append(f"{len(bad)} of {n} rows fail the {fmt} format check")
                errors.extend(f"row {i}: {m}" for i, m in bad[:5])

    note = {
        "pass": f"{n} records · {'format ' + fmt + ' · ' if fmt not in ('unknown', 'n/a') else ''}all roles resolved",
        "degrade": f"{n} records — runs, but {len(degraded)} optional capabilit{'y' if len(degraded)==1 else 'ies'} skipped",
        "block": f"{n} records — won't run as-is: {'; '.join(errors[:2])}",
    }[verdict]

    return DataProfile(verdict=verdict, record_count=n, kind=kind, detected_format=fmt,
                       roles=reports, missing_required=missing_required, degraded=degraded,
                       errors=errors, note=note)
