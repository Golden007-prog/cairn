"""cairn UI renderer (#109 Phase 1) — the surface-agnostic core every surface
draws from: turn a :class:`UIContract` into ordered **form fields** (from the
input JSON Schema) and run shared **input validation/coercion**.

No Rich/Textual/web imports here on purpose — CLI, TUI, web, and Slack all reuse
the same field list and the same validation, so a pack's form + rules are
defined once (the schema) and rendered everywhere.
"""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

from cairn.core.ui_contract import UIContract


class FormField(BaseModel):
    """One input widget, derived from a JSON Schema property."""

    name: str
    title: str
    type: str = "string"          # JSON Schema type → widget kind
    required: bool = False
    default: Any = None
    enum: list[Any] = Field(default_factory=list)   # → dropdown
    description: str = ""
    examples: list[Any] = Field(default_factory=list)


def contract_to_form_fields(contract: UIContract) -> list[FormField]:
    """Ordered form fields from the contract's input JSON Schema."""
    schema = contract.inputs or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    fields: list[FormField] = []
    for name, p in props.items():
        p = p or {}
        fields.append(FormField(
            name=name,
            title=str(p.get("title") or name),
            type=str(p.get("type") or "string"),
            required=name in required,
            default=p.get("default"),
            enum=list(p.get("enum") or []),
            description=str(p.get("description") or ""),
            examples=list(p.get("examples") or []),
        ))
    return fields


def _coerce_one(json_type: str, raw: Any) -> Any:
    """Coerce a single (usually string, from a text field) value to its JSON
    Schema type. Raises ValueError/TypeError if it can't."""
    if not isinstance(raw, str):
        return raw  # already typed (e.g. from an API caller)
    if json_type == "number":
        return float(raw)
    if json_type == "integer":
        return int(raw)
    if json_type == "boolean":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "y"):
            return True
        if low in ("false", "0", "no", "n"):
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    return raw  # string / object / array stay as-is


def coerce_and_validate(
    contract: UIContract, values: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Coerce raw form values to their schema types, then validate against the
    contract's input JSON Schema. Returns ``(typed_values, errors)`` — empty
    errors == valid. Empty strings are treated as "not provided" (dropped), so
    required fields fail loudly and optional ones fall back to defaults.

    This is the ONE check every surface runs at its edge (CLI/TUI/web/API)."""
    schema = contract.inputs or {}
    props = schema.get("properties", {}) or {}
    typed: dict[str, Any] = {}
    errors: list[str] = []

    for name, raw in values.items():
        if isinstance(raw, str) and raw.strip() == "":
            continue  # empty = not provided
        spec = props.get(name, {}) or {}
        json_type = str(spec.get("type") or "string")
        try:
            typed[name] = _coerce_one(json_type, raw)
        except (ValueError, TypeError):
            errors.append(f"{spec.get('title', name)}: expected {json_type}")

    if schema:
        errors += sorted(e.message for e in Draft202012Validator(schema).iter_errors(typed))
    return typed, errors


def validate_inputs(contract: UIContract, values: dict[str, Any]) -> list[str]:
    """Just the error messages (empty == valid) — for surfaces that validate
    without needing the coerced values."""
    return coerce_and_validate(contract, values)[1]


# ── Slack: the same contract → a Block Kit modal (no slack_sdk needed) ──


def _plain(text: str, n: int = 75) -> dict[str, Any]:
    return {"type": "plain_text", "text": (text or " ")[:n]}


def _slack_block(f: FormField) -> dict[str, Any]:
    """One JSON Schema field → a Block Kit `input` block."""
    if f.enum:
        opts = [{"text": _plain(str(o)), "value": str(o)} for o in f.enum]
        element: dict[str, Any] = {"type": "static_select", "action_id": f.name, "options": opts}
        if f.default is not None:
            element["initial_option"] = {"text": _plain(str(f.default)), "value": str(f.default)}
    elif f.type == "boolean":
        opt = {"text": _plain(f.title), "value": "true"}
        element = {"type": "checkboxes", "action_id": f.name, "options": [opt]}
        if f.default:
            element["initial_options"] = [opt]
    elif f.type in ("number", "integer"):
        element = {"type": "number_input", "action_id": f.name, "is_decimal": f.type == "number"}
        if f.default is not None:
            element["initial_value"] = str(f.default)
    else:
        element = {"type": "plain_text_input", "action_id": f.name}
        if f.default is not None:
            element["initial_value"] = str(f.default)
    block: dict[str, Any] = {
        "type": "input", "block_id": f.name, "optional": not f.required,
        "label": _plain(f.title), "element": element,
    }
    if f.description:
        block["hint"] = _plain(f.description, 150)
    return block


def contract_to_slack_modal(contract: UIContract) -> dict[str, Any]:
    """The contract's input schema → a Slack Block Kit ``modal`` view. The
    template ref rides in ``private_metadata`` so a ``view_submission`` handler
    knows what to run. Pure JSON — no slack_sdk; a Slack app ``views.open``s it."""
    blocks = [_slack_block(f) for f in contract_to_form_fields(contract)]
    if not blocks:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "_no inputs_"}}]
    return {
        "type": "modal",
        "callback_id": "cairn_run",
        "private_metadata": contract.template_ref,
        "title": _plain(contract.title or contract.template, 24),
        "submit": _plain("Run"),
        "close": _plain("Cancel"),
        "blocks": blocks,
    }
