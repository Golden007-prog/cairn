"""Operation binding YAML schema + loader (§24.20.5).

A binding file at ``<pack>/operations/<name>.yaml`` declaratively wires
an operator to graph state. The §24.20.5 lock specifies exactly 10
top-level fields; this module is the source of truth for that shape.

Author-facing example (the only thing they touch):

.. code-block:: yaml

    operator: clickstack/query_logs
    version: ">= 0.1"

    input:
      service:    $state.alert.service
      start_time: $state.alert.timeframe_start
      end_time:   $state.alert.timeframe_end
      query:      $input.search_query

    output:
      logs:    $state.logs
      summary: $state.log_summary

    resources:
      timeout_minutes: 5

    secrets:
      - CLICKSTACK_AUTH

Loading is pure: this module reads, parses, validates schema, and
returns an :class:`OperationBinding` instance. It does NOT evaluate
$-bindings, look up operators, or invoke anything. Compile-time
resolution against the OperatorRegistry happens in the engine compiler;
runtime $-binding evaluation is OP4 #183; execution routing is OP6 #185.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class BindingError(ValueError):
    """Raised when an operation binding file fails to load or validate."""


# ─── Sub-models ──────────────────────────────────────────────────────────


class ResourcesField(BaseModel):
    """Per-binding resource override (§24.20.5).

    Domain authors set these per-call; operator-level minimums (declared on
    the Operation class) cannot be relaxed.
    """

    cpu: Optional[int] = None
    memory_gb: Optional[int] = None
    gpu: Optional[int] = None
    gpu_type: Optional[str] = None
    disk_gb: Optional[int] = None
    timeout_minutes: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


class ArtifactsField(BaseModel):
    """Names of operator outputs that should be tracked as artifacts."""

    collect: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class BudgetField(BaseModel):
    """Operation-level budget hint (§24.14 C3 / §24.20.5)."""

    max_cost_usd: Optional[float] = None
    estimated_cost_usd: Optional[float] = None

    model_config = ConfigDict(extra="forbid")


class ApprovalField(BaseModel):
    """Operation-level HITL approval declaration (§24.14 C4).

    Approval is EXPLICIT per §24.20.16 — never auto-injected. Setting
    ``required: true`` here is the only way to gate the operation.
    """

    required: bool = False
    reason: Optional[str] = None
    channel: Optional[str] = None
    timeout_hours: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


class PolicyField(BaseModel):
    """Operation-level named policy references (§24.20.5)."""

    apply: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ExecutionField(BaseModel):
    """Profile reference + mode hint (§24.20.5).

    Demo-time: ``mode = "auto"`` and profile defaults to ``"default"``.
    PRF1–3 implement profile binding; until then mode is informational.
    """

    profile: Optional[str] = None
    mode: str = "auto"
    # #121: the compute connection a remote-job operator runs on. A
    # ``connect``-registered connection NAME (kind=compute.*), or a ``$``-binding
    # into state/input (e.g. ``$input.gpu_provider``). Resolved at dispatch and
    # handed to the operator via ``ctx.compute`` (see runtime.invoke_operator_job).
    compute_target: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


# ─── Top-level binding (§24.20.5 — exactly 10 fields) ────────────────────


class OperationBinding(BaseModel):
    """The 10-field locked schema from §24.20.5.

    Mandatory: ``operator``, ``input``.
    Conditionally mandatory: ``output`` (if operator returns data).
    Optional with safe defaults: everything else.
    """

    # Identity: exactly one of `operator` (legacy, concrete) or `capability` (v1,
    # #2241 C / ADR-0111 P4: `contract://<id>/<rev>`; the profile binds the
    # implementation, the engine resolves it before anything below runs).
    operator: str = ""
    capability: Optional[str] = None
    version: Optional[str] = None  # SpecifierSet; defaults to manifest range
    #: v1 only — semantic configuration mapping, validated against the capability's
    #: configuration schema at the seam; merged under `input` at resolution.
    configuration: dict[str, Any] = Field(default_factory=dict)

    # I/O bindings (mandatory operator → optional output if void return)
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)

    # Optional fields with sane defaults
    resources: Optional[ResourcesField] = None
    secrets: list[str] = Field(default_factory=list)
    artifacts: Optional[ArtifactsField] = None
    budget: Optional[BudgetField] = None
    approval: Optional[ApprovalField] = None
    policy: Optional[PolicyField] = None
    execution: Optional[ExecutionField] = None

    # The 11th field would be a soft boundary breach. Forbid extras.
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _one_identity(self) -> "OperationBinding":
        """ADR-0111 P8 crosswalk: a v1 operation names a capability and may not choose a
        provider, version, resources, secrets, artifacts or execution placement."""
        if bool(self.operator) == bool(self.capability):
            raise ValueError("an operation names exactly one of `operator` (legacy) or `capability` (v1)")
        if self.capability:
            from cairn.core.pack.contracts import validate_implements  # noqa: PLC0415 — the grammar is the pack owner's; lazy, as registry.py's pack use
            validate_implements([self.capability])  # exact contract://<id>/<major.minor.patch>
            forbidden = [f for f in ("version", "resources", "secrets", "artifacts", "execution") if getattr(self, f)]
            if forbidden:
                raise ValueError(f"a v1 operation may not set {forbidden} — the profile and the contract own them (ADR-0111 P8)")
        elif self.configuration:
            raise ValueError("`configuration` belongs to the v1 `capability` form")
        return self


# ─── Loader ──────────────────────────────────────────────────────────────


def load_binding(path: Path) -> OperationBinding:
    """Load an operation binding from a YAML file.

    :raises BindingError: file missing / unreadable / unparseable / schema
        violation. Error message includes the file path for traceability.
    """
    if not path.exists():
        raise BindingError(f"operation binding file not found: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise BindingError(f"could not read binding file {path}: {e}") from e

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise BindingError(f"YAML parse error in {path}: {e}") from e

    if data is None:
        raise BindingError(f"binding file is empty: {path}")
    if not isinstance(data, dict):
        raise BindingError(
            f"binding file {path} must contain a YAML mapping at top level "
            f"(got {type(data).__name__})"
        )

    try:
        return OperationBinding.model_validate(data)
    except Exception as e:
        raise BindingError(
            f"binding file {path} failed schema validation: {e}"
        ) from e


def load_bindings_from_dir(operations_dir: Path) -> dict[str, OperationBinding]:
    """Load every ``*.yaml`` under ``operations_dir`` into a name → binding map.

    Name is the filename stem. Doctor and the compiler use this map to
    resolve template ``operation`` nodes' ``ref:`` field.
    """
    if not operations_dir.exists():
        return {}
    if not operations_dir.is_dir():
        raise BindingError(
            f"expected operations dir, got non-directory: {operations_dir}"
        )

    bindings: dict[str, OperationBinding] = {}
    for yaml_path in sorted(operations_dir.glob("*.yaml")):
        name = yaml_path.stem
        bindings[name] = load_binding(yaml_path)
    return bindings


__all__ = [
    "BindingError",
    "OperationBinding",
    "ResourcesField",
    "ArtifactsField",
    "BudgetField",
    "ApprovalField",
    "PolicyField",
    "ExecutionField",
    "load_binding",
    "load_bindings_from_dir",
]
