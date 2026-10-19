"""Metaflow adapter runs the SAME conformance suite as every engine (#1078).

ADR-0081 D3: "the conformance suite IS the protocol" (Beam's ``ValidatesRunner``
pattern). An engine *is* whatever passes it — deviation is mechanically
detectable rather than a review opinion. So this file is mostly six lines; the
value is that it drives REAL Metaflow, not a fake seam.

Skipped when the optional ``[metaflow]`` extra is absent, so the base install
stays green.
"""
from __future__ import annotations

import types
from unittest import mock

import pytest

from cairn.core.dataflow import DataflowResult, DataflowSpec, StageRef
from cairn.core.dataflow.conformance import PROBE_STAGE, DataflowEngineConformance, contract_probe

metaflow = pytest.importorskip("metaflow", reason="optional [metaflow] extra")


class TestMetaflowConformance(DataflowEngineConformance):
    """The full protocol suite against a live local-mode Metaflow."""

    # Metaflow's own subprocess-per-step startup is heavy compared to the
    # builtin engine's single process — the noop stages themselves are instant.
    poll_budget_s = 180.0

    # Cancel kills the whole process group (engines_metaflow.cancel) — the SAME
    # mechanism the builtin engine uses, so mid-run cancel genuinely works here.
    supports_cancel_midrun = True
    # Budget IS runner-enforced now (#1243 — `execute` calls run_dataflow(),
    # like every runner-based bridge), but only at STAGE BOUNDARIES: run_dataflow checks
    # `budget.max_seconds` before each stage, not while one is running. A lone
    # always-running stage (this suite's slow_spec) has no boundary for that
    # check to land on, so it sails past its budget — NOT like the builtin
    # engine, which layers an independent wall-clock kill in `poll()`. The
    # control loop's cancel path is what actually stops a metaflow job that
    # blows its budget in production. Previously this comment claimed the
    # adapter behaves "exactly like the builtin engine" — false: the builtin
    # engine self-enforces (see test_builtin_conformance.py); this adapter
    # does not, for the reason above.
    supports_budget_abort = False

    def make_engine(self):
        from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine

        return MetaflowEngine()

    def slow_spec(self) -> DataflowSpec:
        return DataflowSpec(stages=[StageRef(stage="noop.slow", config={"seconds": 15})])

    def run_interpreter(self, spec, workdir):
        """ADR-0118 D7 (#2242 V1-C, N3) — the interpreter `CairnFlow.execute` runs: the step body
        itself, in-process, on a stand-in for the step's ``self``."""
        import cairn.core.dataflow.runner as runner_mod
        from operator_dataflow_metaflow.metaflow_bridge import CairnFlow
        from cairn.core.dataflow.stage_loader import load_stages

        step = types.SimpleNamespace(workdir=str(workdir), spec=spec.model_dump(mode="json"), end=None,
                                     next=lambda *a, **k: None)
        with mock.patch.object(runner_mod, "load_stages", lambda: {**load_stages(), PROBE_STAGE: contract_probe}), \
                mock.patch.object(runner_mod, "stage_versions", lambda: {}):
            CairnFlow.execute(step)
        return DataflowResult.model_validate(step.result_payload)


@pytest.mark.asyncio
async def test_pathspec_enables_stateless_reattach():
    """The property the whole design rests on: a handle carries a Metaflow
    pathspec, so ANY process can re-attach — no control-plane state needed.

    Proven live before the adapter was written: a fresh interpreter read
    `Flow('ProbeFlow').latest_run`, its `finished`/`successful` flags and its
    artifacts, with nothing shared but the pathspec string.
    """
    from cairn.core.dataflow import DataflowSpec
    from cairn.core.dataflow.conformance import CONFORMANCE_STAGES, poll_until_terminal
    from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine

    engine = MetaflowEngine()
    spec = DataflowSpec(stages=list(CONFORMANCE_STAGES), parent_run="pathspec-test")
    handle = await engine.submit(spec, ctx=None)
    await poll_until_terminal(engine, handle, budget_s=180.0)

    spec_str = engine.pathspec(handle)
    assert spec_str and spec_str.startswith("CairnFlow/"), spec_str

    # Re-attach the way a reader must. Metaflow's local metadata provider is
    # CWD-BASED (`<cwd>/.metaflow`), not env-configured — established by
    # experiment after the first version raised MetaflowNotFound on a run that
    # had just succeeded. Hence the engine pins one stable root and submits from
    # it; a reader runs from the same place.
    import os as _os

    prev = _os.getcwd()
    _os.chdir(engine.metadata_root)
    try:
        # Select the engine's namespace: runs are submitted as the `cairn` user,
        # so a reader in another namespace gets MetaflowNamespaceMismatch on a
        # run that exists and succeeded (found live).
        metaflow.namespace(engine.metaflow_namespace)
        run = metaflow.Run(spec_str)
        assert run.successful, f"{spec_str} did not succeed"
    finally:
        _os.chdir(prev)


@pytest.mark.asyncio
async def test_unknown_stage_is_rejected_before_submitting():
    """validate() reports what THIS engine cannot run (capability discovery,
    D3) — catching it here beats failing after a job is already burning."""
    from cairn.core.dataflow import DataflowSpec
    from cairn.core.dataflow.contract import StageRef
    from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine

    problems = MetaflowEngine().validate(
        DataflowSpec(stages=[StageRef(stage="no.such.kernel")], parent_run="r"))
    assert problems and "no.such.kernel" in problems[0]
