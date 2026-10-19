"""The effective parameters of one operator step — computed ONCE (ADR-0103 D3).

ADR-0103 D3 says the runtime records every step's effective inputs and config
before ``run``, and ADR-0102 D2 says the launch dialog can **preview** that same
block before anything is spent. Those are two readers of one computation, and
the whole value of the preview is the claim "this is what will run" — a claim a
second, drifting copy of the layering would quietly falsify.

So the layering lives here, once:

  1. ``strip_governance_inputs`` — a run may not carry an env-owned knob (#905);
  2. ``resolve_operator_config`` — run › workflow › environment › default;
  3. ``apply_operator_config`` — config merges UNDER the bound input;
  4. ``input_schema.model_validate`` — the operator's own defaults land here,
     which is why a preview that skipped validation would differ from the record;
  5. redaction of credential-shaped keys, and the per-key scope map.

:func:`~cairn.core.operator.runtime.invoke_operator_core` calls this and hands
the result to the ``parameters`` recorder; the pipeline preview
(:mod:`cairn.core.dispatch.pipeline_preview`) calls it with the same three config
layers and no operator ever runs. Equality of the two is asserted in
``tests/core/test_trainspec_tiers.py`` — the acceptance criterion of #2023.
"""
from __future__ import annotations

import logging

import re
from dataclasses import dataclass, field
from typing import Any

#: A key whose NAME looks like a credential never reaches a run record, whatever
#: it holds. Moved here from ``runtime`` with the rest of the computation.
_REDACT_RE = re.compile(r"(secret|token|password|api[_-]?key|credential)", re.IGNORECASE)


class ContractConformanceError(ValueError):
    """#2241 B — an opted-in operator's input/configuration failed the shared contract
    (no provider invocation happened) or its output did (the effect happened; the run
    fails regardless — never a silent success)."""


@dataclass(frozen=True)
class ContractEnvelope:
    """#2241 B — the shared contract a leg was admitted with, as DATA. Resolved by the
    caller that may read the pack owner (dispatch, from the record's ``InvocationEntry``);
    the ``operator`` boundary never reads contract bodies itself. ``schemas`` holds the
    closure schemas the capability's input/configuration/output reach through ``$ref``,
    by ``contract://`` URI, so references resolve locally; nothing is fetched."""
    capability_ref: str
    operator_ref: str
    operator_version: str
    refs: dict[str, str]            # "input" / "configuration" / "output" → contract:// URI
    schemas: dict[str, Any]         # contract:// URI → JSON Schema (dict or bool)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContractEnvelope":
        return cls(data["capability_ref"], data["operator_ref"], str(data["operator_version"]), dict(data["refs"]), dict(data["schemas"]))

    def problems(self, part: str, value: Any) -> list[str]:
        """Validation messages for ``value`` against the ``part`` schema; empty = conforms.
        ADR-0118 D5: the ONE validator, over the part bundled into a self-contained schema."""
        from cairn.core.dataflow.conformance_check import bundle, problems  # noqa: PLC0415 — keep the dataflow package off the operator import path
        return problems(bundle(self.schemas, self.refs[part]), value, part)

    def check_identity(self, operator_cls: Any) -> None:
        """D5 — the operator running now must still be the one recorded at admission:
        same name, same version, and it still implements the recorded revision."""
        implements = getattr(operator_cls, "implements", ())
        name, version = getattr(operator_cls, "name", ""), str(getattr(operator_cls, "version", ""))
        if self.capability_ref not in implements or name != self.operator_ref or version != self.operator_version:
            raise ContractConformanceError(
                f"operator {name!r}@{version} (implements {list(implements)}) no longer matches the recorded "
                f"resolution {self.operator_ref!r}@{self.operator_version} for {self.capability_ref}; "
                f"refusing to run against a drifted implementation")


def contract_input_problems(contract: ContractEnvelope, operator_cls: Any, merged: Any,
                            effective_cfg: dict[str, Any]) -> list[str]:
    """#2241 B — the shared input + configuration checks on the dicts the OPERATOR will see:
    the input after governance strip and config layering, and the configuration with the
    operator's own defaults filled in — a shared `required` satisfied by a default or a layer
    is not a refusal. ONE implementation for every executor seam (#2345: remote_job/docker)."""
    config_cls = getattr(operator_cls, "config_schema", None)
    defaults = _defaults(config_cls) if config_cls is not None else {}
    fields = set(getattr(config_cls, "model_fields", {}) or {})
    supplied = {k: merged[k] for k in fields if isinstance(merged, dict) and k in merged}
    return (contract.problems("input", merged if isinstance(merged, dict) else {})
            + contract.problems("configuration", {**defaults, **effective_cfg, **supplied}))


class EffectiveParametersError(ValueError):
    """The resolved input does not satisfy the operator's ``input_schema``.

    Carries the message the runtime has always raised, so
    :class:`~cairn.core.engine.expr.ExprError` keeps its wording and the
    preview can report the same refusal without running anything.
    """


def redact(obj: Any) -> Any:
    """Never let a credential-shaped key reach a run record or a preview."""
    if isinstance(obj, dict):
        return {k: ("***" if isinstance(k, str) and _REDACT_RE.search(k) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def config_scopes(
    operator_cls: Any, effective_cfg: dict[str, Any], *,
    profile_config: dict[str, Any] | None, workflow_config: dict[str, Any] | None,
    run_overrides: dict[str, Any] | None,
) -> dict[str, str]:
    """Which layer supplied each top-level config key: run · workflow · environment · default."""
    op_name = getattr(operator_cls, "name", "")
    run = (run_overrides or {}).get(op_name) or {}
    wf = (workflow_config or {}).get(op_name) or {}
    prof = (profile_config or {}).get(op_name) or {}
    scopes: dict[str, str] = {}
    for key in effective_cfg:
        scopes[key] = ("run" if key in run else "workflow" if key in wf
                       else "environment" if key in prof else "default")
    schema = getattr(operator_cls, "config_schema", None)
    fields = getattr(schema, "model_fields", None) or {}
    for key in fields:
        scopes.setdefault(key, "default")
    return scopes


@dataclass
class EffectiveParameters:
    """What one step runs with, after every layer and every default."""

    operator: str = ""
    version: str = ""
    risk: str = ""
    #: JSON-safe, redacted dump of the VALIDATED input — the operator's own
    #: schema defaults included, because that is what the operator sees.
    inputs: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    scopes: dict[str, str] = field(default_factory=dict)
    governance_dropped: list[str] = field(default_factory=list)
    #: The validated pydantic model the operator is called with. Never
    #: serialised — :attr:`inputs` is its JSON projection.
    validated: Any = None
    #: The merged dict handed to validation (post-strip, post-config-merge).
    resolved_input: dict[str, Any] = field(default_factory=dict)

    def as_payload(self, *, mocked: bool = False) -> dict[str, Any]:
        """The ``parameters`` run-event body — and, minus ``mocked``, the
        preview's per-step body. One shape, one producer."""
        return {
            "operator": self.operator,
            "version": self.version,
            "risk": self.risk,
            "inputs": self.inputs,
            "config": self.config,
            "scopes": self.scopes,
            "governance_dropped": list(self.governance_dropped),
            "mocked": bool(mocked),
        }


def _defaults(config_cls: Any) -> dict[str, Any]:
    try:
        return config_cls().model_dump(mode="json")
    except Exception:  # noqa: BLE001 — a config schema with required knobs has no defaults to fill
        return {}


def effective_parameters(
    operator_cls: Any,
    resolved_input: Any,
    *,
    profile_config: dict[str, Any] | None = None,
    workflow_config: dict[str, Any] | None = None,
    run_overrides: dict[str, Any] | None = None,
    contract: ContractEnvelope | None = None,
) -> EffectiveParameters:
    """Layer, strip, merge and validate — the ONE computation (see module doc).

    #2241 B: with a ``contract`` (an opted-in operator), the shared input schema is
    checked against the layered, governance-stripped input and the shared configuration
    schema against the effective config with the operator's own defaults filled in —
    the dicts the operator will actually see — BEFORE the operator's own Pydantic
    validation, so a refusal names the contract and no provider is invoked. Legacy
    operators (``contract=None``) take the byte-identical path they always did.

    :raises EffectiveParametersError: the resolved input fails the operator's
        ``input_schema``. The runtime re-raises it as a binding error; the
        preview reports it as the step's refusal, before anything spends.
    """
    from cairn.core.operator.config import (  # noqa: PLC0415 — cycle: protocol → runtime → config
        apply_operator_config,
        resolve_operator_config,
        strip_governance_inputs,
    )

    op_name = getattr(operator_cls, "name", operator_cls.__name__)
    merged: Any = resolved_input
    effective_cfg: dict[str, Any] = {}
    dropped: list[str] = []

    if getattr(operator_cls, "config_schema", None) is not None:
        # #905 — a GOVERNANCE knob (ADR-0070, env-only) must never be settable
        # by a run. Dropped BEFORE the merge, so the profile/default supplies it.
        merged, dropped = strip_governance_inputs(merged, operator_cls.config_schema)
        effective_cfg = resolve_operator_config(
            op_name, profile_config=profile_config,
            workflow_config=workflow_config, run_overrides=run_overrides,
        )
        if effective_cfg:
            merged = apply_operator_config(merged, effective_cfg)

    if contract is not None:
        problems = contract_input_problems(contract, operator_cls, merged, effective_cfg)
        if problems:
            raise ContractConformanceError(
                f"operator {op_name!r} does not conform to {contract.capability_ref}: " + "; ".join(problems))
    try:
        validated = operator_cls.input_schema.model_validate(merged)
    except Exception as e:
        raise EffectiveParametersError(
            f"operator {op_name!r} input failed Pydantic validation: {e}"
        ) from e

    try:
        inputs = (validated.model_dump(mode="json") if hasattr(validated, "model_dump")
                  else dict(validated or {}))
    except Exception:  # noqa: BLE001 — a non-serialisable input is recorded by repr, not skipped
        inputs = {"_repr": repr(validated)[:2000]}

    return EffectiveParameters(
        operator=op_name,
        version=str(getattr(operator_cls, "version", "") or ""),
        risk=str(getattr(operator_cls, "risk", "") or ""),
        inputs=redact(inputs),
        config=redact(dict(effective_cfg or {})),
        scopes=config_scopes(
            operator_cls, effective_cfg or {}, profile_config=profile_config,
            workflow_config=workflow_config, run_overrides=run_overrides,
        ),
        governance_dropped=list(dropped),
        validated=validated,
        resolved_input=merged if isinstance(merged, dict) else {},
    )


__all__ = [
    "ContractConformanceError", "ContractEnvelope",
    "EffectiveParameters", "EffectiveParametersError", "config_scopes",
    "effective_parameters", "redact",
]


# ─── flow legs (ADR-0103 D3 for the OTHER kind of step) ─────────────────────
#
# A pipeline leg that is a FLOW (`flow:<pack>/<name>`, kind ml | data) never
# passes through invoke_operator_core: the flow engine runs its stages in its
# own process. It still must record what it ran with — the preview said so,
# and a training run's Parameters block is exactly this leg. What is knowable
# at the seam is the leg's resolved inputs; the operator-level config of the
# stages inside the flow resolves in the engine and is not restated here.


def flow_parameters(ref: str, kind: str, inputs: dict[str, Any]) -> EffectiveParameters:
    """The ``parameters`` body for a flow leg: its resolved inputs, redacted.

    ``kind`` is accepted for symmetry with the executor but deliberately not
    written: ``risk`` is an operator property, and the preview only knows the
    ref prefix (``flow``) while the executor knows the flow's declared kind
    (``ml`` / ``data``) — writing either would make the two sides differ.
    """
    del kind
    ref = ref[len("flow:"):] if ref.startswith("flow:") else ref
    return EffectiveParameters(
        operator=f"flow:{ref}",
        version="",
        risk="",
        inputs=redact(dict(inputs or {})),
        config={},
        scopes={},
        governance_dropped=[],
        resolved_input=dict(inputs or {}),
    )


async def record_flow_parameters(*, run_id: str, node: str, ref: str, kind: str,
                                 inputs: dict[str, Any], mocked: bool = False) -> dict[str, Any] | None:
    """Append a flow leg's ``parameters`` run event. Never raises — the leg
    must submit whether or not its record could be written (the stage_events
    rule). Returns the payload written, or ``None`` when it could not be."""
    payload = {"node": node, "seq": 1, **flow_parameters(ref, kind, inputs).as_payload(mocked=mocked)}
    try:
        from cairn.core.reporting import PARAMETERS_EVENT_TYPE  # noqa: PLC0415 — keep reporting types off this module's import path
        from cairn.core.storage import get_storage  # noqa: PLC0415 — keep the storage stack off the operator import path (runtime.py convention)
        await get_storage().runs.append_event(run_id, PARAMETERS_EVENT_TYPE, payload)
        return payload
    except Exception:  # noqa: BLE001 — never fail the leg for its own record
        logging.getLogger("cairn.core.operator.effective").warning(
            "parameters record for flow leg %s on run %s failed (leg unaffected)", ref, run_id, exc_info=True)
        return None
