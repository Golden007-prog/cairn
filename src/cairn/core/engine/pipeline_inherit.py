"""``inputs: {$inherit: <leg id>}`` — a wrapper does not restate its flow.

ADR-0106 D2 (#2134). ADR-0106 D1 says only a **declared** pipeline runs, so
every flow meant to be runnable gets a file. That rule is only affordable if
the file is cheap: ``pipelines/dpo-train.yaml`` was 96 lines, ~63 of which
restated ``dpo-align``'s inputs and wired them straight back one-to-one, and a
rule that mandates that shape produces input-drift bugs at every flow change.

**The grammar**, inside a pipeline's ``inputs:`` block::

    inputs:
      $inherit: train              # adopt the contract of the leg with id `train`
      gpu: {default: A10G}         # …with this key overridden / added
      $hide: [extra, target_modules]   # …and these inherited keys dropped

and on the leg itself::

    steps:
      - id: train
        flow: dpo-align            # no `with:` — inherited keys wire in by name

Four rules, each chosen so that reading the file tells you what happens:

* **Explicit, never implicit.** An omitted ``inputs:`` block still means *no
  inputs*. Silence never acquires a meaning, so ``$inherit`` is a token the
  lint (and a human) can see.
* **Wiring is by NAME, and an explicit ``with:`` always wins.** Each inherited
  key that the leg does not already bind is wired ``<key>: $input.<key>``. A
  key the author bound by hand keeps the hand-written binding — the wrapper's
  literal (``retry:``, a constant, a wire from another leg) is never
  overwritten by an inherited passthrough.
* **A key declared beside ``$inherit`` overrides the inherited one**, so a
  wrapper pins one default without restating the other twenty-eight.
* **A missing leg, or a leg whose ref does not resolve, is a LOAD error**
  naming both the pipeline and the ref. An inheritance that silently resolved
  to nothing would present an empty launch dialog for a pipeline that is about
  to spend money.

**Where the contract comes from — the readers that already exist.** This
module resolves no schemas of its own:

============  ==========================================================
leg kind      contract
============  ==========================================================
``template``  :func:`cairn.core.ui_contract.template_inputs` — the
              ``inputs`` of :func:`~cairn.core.ui_contract.build_ui_contract`
              (the JSON Schema every launch surface renders and
              ``coerce_and_validate`` checks against) without building
              the plan, so an agentic wrapper asks exactly what the
              flow's own form asks.
``flow``      :func:`cairn.core.dataflow.defs.flow_config_keys` — the
              ``$config.<key>`` placeholders that ARE a data/ml flow's
              input surface (the reader ``/builder``'s flow document and
              the flow editor use).
============  ==========================================================

A data/ml flow declares names and nothing else — no types, no defaults — so a
wrapper over one still declares the defaults it wants to pin. That is a
property of the flow format, not of ``$inherit``; giving ``FlowDef`` a typed
``config:`` block would shrink those wrappers further and is not this slice.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cairn.core.engine.pipeline import PipelineDef

#: The two reserved keys in an ``inputs:`` block. Spelled with a leading ``$``
#: like every other cairn directive (``$input.``, ``$steps.``, ``$config.``),
#: so they cannot collide with a real input name.
INHERIT_KEY = "$inherit"
HIDE_KEY = "$hide"
RESERVED_INPUT_KEYS = (INHERIT_KEY, HIDE_KEY)

#: JSON-Schema type name → the python-ish name a pipeline's ``inputs:`` block
#: uses. The inverse of ``pipeline._INPUT_TYPE_NAMES``; kept here because this
#: is the only place a foreign schema is translated INTO pipeline input specs.
_JSON_TO_INPUT_TYPE = {
    "string": "str", "integer": "int", "number": "float",
    "boolean": "bool", "object": "dict", "array": "list",
}


class InheritError(ValueError):
    """``$inherit`` names a leg that does not exist, or one whose ref does not
    resolve to a readable contract. Raised at LOAD, wrapped by the parser into
    a :class:`~cairn.core.engine.pipeline.PipelineDefError`."""


def _template_contract(ref: str) -> Optional[dict[str, Any]]:
    """``{key: spec}`` for an agentic flow, from the UI contract's input schema.

    :func:`~cairn.core.ui_contract.template_inputs`, not ``build_ui_contract``:
    the plan the full contract builds is ~0.5 s per leg and was most of a cold
    pipeline discovery (#2340); only its ``inputs`` and ``template_found`` were read."""
    from cairn.core.ui_contract import template_inputs  # noqa: PLC0415 — keep plan/pack off the engine import path

    try:
        schema = template_inputs(ref)
    except Exception:  # noqa: BLE001 — unreadable is "does not resolve"
        return None
    if schema is None:
        return None
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: dict[str, Any] = {}
    for key, raw in props.items():
        spec: dict[str, Any] = {}
        t = _JSON_TO_INPUT_TYPE.get(str((raw or {}).get("type", "") or "").lower())
        if t:
            spec["type"] = t
        if isinstance(raw, dict) and "default" in raw:
            spec["default"] = raw["default"]
        desc = str((raw or {}).get("description", "") or "").strip()
        if desc:
            spec["description"] = desc
        # `missing_inputs` reads `required: false` (or a default) as "optional";
        # an input schema that does not list the key is exactly that.
        if key not in required and "default" not in spec:
            spec["required"] = False
        out[key] = spec
    return out


def _flow_contract(ref: str) -> Optional[dict[str, Any]]:
    """``{key: {}}`` for a data/ml flow — its ``$config.<key>`` placeholders.

    The specs are EMPTY because the flow format declares no types or defaults;
    a wrapper that wants either declares it beside ``$inherit``.
    """
    from cairn.core.dataflow.defs import flow_config_keys  # noqa: PLC0415 — cycle: engine ↔ dataflow
    from cairn.core.pack import ensure_loaded  # noqa: PLC0415 — as above

    try:
        # `load_flow_defs` scans the REGISTERED packs and never loads them
        # itself, so a cold process (a CLI validate, a test that parses a
        # definition directly) would see zero flows and read the refusal below
        # as "the flow does not exist". Warm calls are a dict lookup.
        ensure_loaded()
        keys = flow_config_keys(ref)
    except Exception:  # noqa: BLE001 — as above
        return None
    return None if keys is None else {k: {} for k in keys}


def leg_contract(step: Any) -> Optional[dict[str, Any]]:
    """The declared input contract of one leg, or ``None`` if it does not
    resolve. ``task``/``gate``/``loop`` legs have no flow contract to adopt."""
    if getattr(step, "template", None):
        return _template_contract(str(step.template))
    if getattr(step, "flow", None):
        return _flow_contract(str(step.flow))
    return None


def resolve_inherited_inputs(d: PipelineDef, *, source: str = "<string>") -> None:
    """Expand ``$inherit``/``$hide`` in ``d.inputs`` and wire the leg, in place.

    A no-op for a definition that declares neither key — which is every
    pipeline written before ADR-0106, so nothing pays for the mechanism.

    Called from :func:`~cairn.core.engine.pipeline.parse_pipeline_def` after
    step validation (which pack-qualifies a bare leg ref) and before the
    content hash, so ``content_hash`` covers the EFFECTIVE definition: a
    wrapper whose flow grew an input is a different pipeline, which is the
    honest answer and the one the drift pin (#1255) needs.
    """
    raw = dict(d.inputs or {})
    inherit = raw.pop(INHERIT_KEY, None)
    hide = raw.pop(HIDE_KEY, None)
    if inherit is None and hide is None:
        return

    if inherit is None:
        raise InheritError(
            f"{source}: pipeline {d.name!r} declares `{HIDE_KEY}:` with no "
            f"`{INHERIT_KEY}:` — there is nothing to hide. Add "
            f"`{INHERIT_KEY}: <leg id>`, or drop the block.")
    leg_id = str(inherit or "").strip()
    if not leg_id:
        raise InheritError(
            f"{source}: pipeline {d.name!r} declares an empty `{INHERIT_KEY}:` "
            f"— it takes the id of the leg whose contract to adopt.")

    steps = d.all_steps()
    by_id = {s.name: s for s in steps}
    step = by_id.get(leg_id)
    if step is None:
        raise InheritError(
            f"{source}: pipeline {d.name!r} declares `{INHERIT_KEY}: {leg_id}` "
            f"but has no step with that id — known steps: {sorted(by_id)!r}.")

    ref = str(getattr(step, "flow", None) or getattr(step, "template", None) or "")
    if not ref:
        raise InheritError(
            f"{source}: pipeline {d.name!r} declares `{INHERIT_KEY}: {leg_id}` "
            f"but step {leg_id!r} is a {step.leg_kind or 'bare'} leg — only a "
            f"`flow:`/`template:` leg declares an input contract to inherit.")

    contract = leg_contract(step)
    if contract is None:
        raise InheritError(
            f"{source}: pipeline {d.name!r} declares `{INHERIT_KEY}: {leg_id}`, "
            f"but leg {leg_id!r} references {ref!r} and no such flow resolves — "
            f"install the pack that ships it, or fix the ref. (An inherited "
            f"contract that resolved to nothing would open an EMPTY launch "
            f"dialog for a pipeline that spends money.)")

    if hide is not None and not isinstance(hide, (list, tuple)):
        raise InheritError(
            f"{source}: pipeline {d.name!r} declares `{HIDE_KEY}: {hide!r}` — "
            f"it takes a LIST of inherited input names.")
    hidden = [str(k) for k in (hide or [])]
    unknown = [k for k in hidden if k not in contract]
    if unknown:
        raise InheritError(
            f"{source}: pipeline {d.name!r} hides input(s) {unknown!r} that "
            f"{ref!r} does not declare — inherited keys are "
            f"{sorted(contract)!r}. A `{HIDE_KEY}` entry that matches nothing "
            f"is a rename nobody noticed.")

    merged: dict[str, Any] = {}
    for key, spec in contract.items():
        if key in hidden:
            continue
        merged[key] = dict(spec) if isinstance(spec, dict) else spec
    # A key declared beside `$inherit` REPLACES the inherited spec rather than
    # merging into it: a half-overridden spec (this file's default, the flow's
    # type, nobody's description) is a value no one file is responsible for.
    merged.update(raw)
    d.inputs = merged
    # #2134 S4 — record WHERE each key came from, here, where it is known. The
    # `$inherit` token is gone from `d.inputs` by the line above, so a later
    # reader (`cairn pipeline explain`'s resolved view) could only re-derive
    # this by re-reading the flow's contract — a second opinion about a
    # question this function already answered. A key declared beside `$inherit`
    # is NOT recorded: overriding it makes the wrapper its owner.
    d.inherited_inputs = {key: leg_id for key in contract
                          if key not in hidden and key not in raw}

    # Wire by name — but never over an explicit `with:`. The author's binding
    # is the specific statement; inheritance is the general one.
    #
    # Only INHERITED keys are wired. A key declared here that the flow does not
    # take (a licence acknowledgement, a governance-only knob) stays a pipeline
    # input and is not pushed at a leg that would drop it in silence.
    bound = dict(step.inputs or {})
    for key in contract:
        if key in hidden or key in bound:
            continue
        bound[key] = f"$input.{key}"
    step.inputs = bound


__all__ = [
    "HIDE_KEY", "INHERIT_KEY", "RESERVED_INPUT_KEYS", "InheritError",
    "leg_contract", "resolve_inherited_inputs",
]
