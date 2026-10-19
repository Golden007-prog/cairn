"""cairn.core.dataflow — the data-plane contract (ADR-0081 D2/D3, #1046).

The typed boundary between the control plane and dataflow engines. This package
is deliberately tiny and dependency-free (pydantic only): it ships in the core
wheel and is imported by BOTH sides — the `dataflow/run` operator on the
control plane and every engine (builtin / Metaflow / SkyPilot adapters).

Hard rules the contract enforces (ADR-0081 D3):
- manifests + scalars cross the boundary, never rows;
- `DataflowSpec` is engine-neutral — engine-specific config lives only in the
  environment backend (`[profile.backends.dataflow.<engine>]`), never here;
- engines return `ArtifactDescriptor`s; the CONTROL PLANE registers them
  (engines get no registry/database credentials).

The conformance suite (:mod:`cairn.core.dataflow.conformance`) is the
protocol's executable definition — an engine IS whatever passes it.
"""
from cairn.core.dataflow.contract import (
    ArtifactDescriptor,
    Budget,
    DataflowResult,
    DataflowSpec,
    ResumeFrom,
    StageMap,
    StageRef,
    StageResources,
)
from cairn.core.dataflow.engine import (
    DataflowEngine,
    EngineError,
    EngineJobHandle,
    EngineJobStatus,
)
from cairn.core.dataflow.defs import (
    FLOW_KIND_DATA,
    FLOW_KIND_ML,
    DataflowDef,
    DataflowDefError,
    FlowDef,
    aggregate_risk,
    flow_def_for,
    flow_leg_risk,
    load_dataflow_defs,
    load_flow_defs,
    resolve_dataflow_ref,
    resolve_flow_key,
    resolve_flow_ref,
)
from cairn.core.dataflow.manifest import Manifest, ShardRef

__all__ = [
    "FLOW_KIND_DATA",
    "FLOW_KIND_ML",
    "ArtifactDescriptor",
    "Budget",
    "DataflowDef",
    "DataflowDefError",
    "DataflowEngine",
    "DataflowResult",
    "DataflowSpec",
    "ResumeFrom",
    "EngineError",
    "EngineJobHandle",
    "EngineJobStatus",
    "FlowDef",
    "Manifest",
    "ShardRef",
    "StageMap",
    "StageRef",
    "StageResources",
    "aggregate_risk",
    "flow_def_for",
    "flow_leg_risk",
    "load_dataflow_defs",
    "load_flow_defs",
    "resolve_dataflow_ref",
    "resolve_flow_key",
    "resolve_flow_ref",
]
