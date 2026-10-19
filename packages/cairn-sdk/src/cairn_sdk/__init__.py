"""cairn_sdk — the authoring projection (#1253, ADR-0087 P1).

Fifteen words that each map 1:1 to a record; ``compile_pipeline`` turns the
class into ``pipelines/*.yaml`` (+ inline ``flows/*.yaml``). The runtime never
imports author classes — the compiled record is what installs, hashes, and runs.
"""
from .builders import (
    CompileScopeError,
    cron,
    flow,
    gate,
    loop,
    parallel,
    pipeline,
    step,
    watch,
    when,
)
from .compiler import CompiledPack, CompileError, compile_pipeline
from .client import GateHandle, PipelineDriftError, PipelineRun, run_pipeline
from .envs import Budget, CompiledEnv, Connection, Environment, Policy
from .expr import inp

__all__ = [
    "Budget", "CompiledEnv", "CompiledPack", "CompileError",
    "CompileScopeError", "Connection", "Environment", "GateHandle",
    "PipelineDriftError", "PipelineRun", "Policy", "compile_pipeline", "cron",
    "flow", "gate", "inp", "loop", "parallel", "pipeline", "run_pipeline",
    "step", "watch", "when",
]
