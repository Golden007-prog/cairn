"""Engine conformance suite — the executable definition of the protocol
(ADR-0081 D3, #1046; the Beam ``ValidatesRunner`` steal).

An engine IS whatever passes this suite. It ships in CORE (importable by any
pack's tests) so the builtin engine (#1048) and every adapter (Metaflow /
SkyPilot, #1054) run the IDENTICAL cases — deviation between engines becomes a
failing test, not a review opinion.

Usage (in an engine's own test file)::

    from cairn.core.dataflow.conformance import DataflowEngineConformance

    class TestMyEngineConformance(DataflowEngineConformance):
        def make_engine(self):
            return MyEngine(...)

Hooks with sensible defaults: ``sample_spec`` (a 2-stage no-op dataflow every
engine must run), ``unknown_stage_spec`` (validate() must flag it), and
capability flags for optional cases (``supports_cancel_midrun``,
``supports_budget_abort``). pytest-asyncio ``asyncio_mode=auto`` (already the
repo default) makes the async tests collect directly.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from cairn.core.dataflow.conformance_check import MAX_SPEC_BYTES, StageContractError, bundle, submission_problems
from cairn.core.dataflow.contract import (
    ArtifactDescriptor,
    Budget,
    DataflowResult,
    DataflowSpec,
    SpecContracts,
    StageContract,
    StageRef,
)
from cairn.core.dataflow.engine import (
    DataflowEngine,
    EngineJobHandle,
    EngineJobStatus,
    carries_stage_contracts,
    is_terminal,
)

# every engine must run these two no-op stages; adapters that execute stages
# via the shared kernel registry get them for free (stage 'noop' is part of
# the P1b registry precisely so conformance never needs real data work).
CONFORMANCE_STAGES = [StageRef(stage="noop"), StageRef(stage="noop")]

_POLL_INTERVAL_S = 0.05
_POLL_BUDGET_S = 30.0


async def poll_until_terminal(
    engine: DataflowEngine,
    handle: EngineJobHandle,
    budget_s: float = _POLL_BUDGET_S,
) -> EngineJobStatus:
    """Drive poll() to a terminal state within the suite's time budget.

    ``budget_s`` lets a caller running REAL kernels (cold spaCy/presidio
    imports in the engine subprocess ≈ 25s alone) extend the default, which
    is sized for the noop conformance stages."""
    deadline = asyncio.get_event_loop().time() + budget_s
    while True:
        status = await engine.poll(handle)
        if is_terminal(status):
            return status
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"engine {engine.name!r} did not reach a terminal state in {budget_s}s")
        await asyncio.sleep(_POLL_INTERVAL_S)


# ── ADR-0118 D7: the stage-contract probe the interpreter-backed cases run ─────
PROBE_STAGE = "conformance.contract_probe"
PROBE_CONTRACT = "contract://conformance.probe/1.0.0"
_PROBE_CFG = "contract://conformance.probe.configuration/1.0.0"
_PROBE_OUT = "contract://conformance.probe.output/1.0.0"


def contract_probe(workdir, config: dict, inputs: list) -> dict:
    """An opted-in test kernel: reports ``rows`` straight from its config."""
    return {"rows": config.get("rows", 0)}


contract_probe.implements = (PROBE_CONTRACT,)
contract_probe.risk_class = "read_only"


def contract_probe_spec(rows: object = 1, *, envelope: bool = True, padding: int = 0) -> DataflowSpec:
    """A stamped spec running :func:`contract_probe` under a self-contained stage contract:
    config ``rows`` must be an integer, the raw output must carry ``rows >= 0``."""
    schemas = {_PROBE_CFG: bundle({_PROBE_CFG: {"type": "object", "properties": {"rows": {"type": "integer"}}}}, _PROBE_CFG),
               _PROBE_OUT: bundle({_PROBE_OUT: {"type": "object", "required": ["rows"],
                                                "properties": {"rows": {"type": "integer", "minimum": 0}}}}, _PROBE_OUT)}
    contract = StageContract(capability_ref=PROBE_CONTRACT, configuration=_PROBE_CFG, output=_PROBE_OUT) if envelope else None
    config = {"rows": rows, **({"padding": "x" * padding} if padding else {})}
    return DataflowSpec(stages=[StageRef(stage=PROBE_STAGE, config=config, contract=contract)],
                        contracts=SpecContracts(leg="conformance-leg", schemas=schemas), parent_run="conformance-run")


class DataflowEngineConformance:
    """Subclass per engine; override ``make_engine`` (required) + hooks."""

    # ── capability flags (optional cases skip, never silently pass) ─────────
    supports_cancel_midrun: bool = True
    supports_budget_abort: bool = False
    # ADR-0082 D8a (#1076): running polls carry {stage, stages_done,
    # stages_total} so the control plane can bridge stage transitions into
    # run events with zero engine-specific wiring.
    supports_stage_events: bool = True
    # #1243: a conforming engine reports per-stage lineage — silence (an empty
    # `output_artifacts` on a multi-stage spec) must be an explicit, reviewed
    # opt-out, never the default an adapter falls into by omission.
    emits_artifact_descriptors: bool = True
    # Some adapters (real subprocess-per-step engines) need more wall-clock
    # than the suite's default poll budget before a job reaches a terminal
    # state — override in the subclass, never by padding the default for
    # everyone (fast engines should keep failing fast).
    poll_budget_s: float = _POLL_BUDGET_S

    def make_engine(self) -> DataflowEngine:  # pragma: no cover - abstract
        raise NotImplementedError("conformance subclass must build its engine")

    def sample_spec(self) -> DataflowSpec:
        return DataflowSpec(stages=list(CONFORMANCE_STAGES), parent_run="conformance-run")

    def unknown_stage_spec(self) -> Optional[DataflowSpec]:
        """A spec whose stage cannot exist; return None to skip (engines that
        defer stage resolution to submit-time should still fail loudly there)."""
        return DataflowSpec(stages=[StageRef(stage="conformance.does_not_exist")])

    def slow_spec(self) -> Optional[DataflowSpec]:
        """A spec that stays running long enough to cancel; None ⇒ skip the
        mid-run cancel case (the terminal-cancel case still runs)."""
        return None

    def stage_progress_spec(self) -> Optional[DataflowSpec]:
        """A multi-stage spec whose FIRST stage finishes quickly while a later
        stage holds the run — so a running poll can show ``stages_done >= 1``
        (D8a). Defaults to ``noop`` prepended to the slow spec; None ⇒ skip."""
        slow = self.slow_spec()
        if slow is None:
            return None
        return slow.model_copy(
            update={"stages": [StageRef(stage="noop"), *slow.stages]}
        )

    def make_ctx(self):
        """Operator-context stand-in handed to submit(); engines that need a
        resolved connection override this in their subclass."""
        return None

    def run_interpreter(self, spec: DataflowSpec, workdir) -> Optional[DataflowResult]:
        """ADR-0118 D7 — run ``spec`` through the interpreter this engine runs kernels with,
        in-process, with :func:`contract_probe` registered as :data:`PROBE_STAGE`. ``None``
        ⇒ the interpreter-backed stage-contract cases skip (never silently pass)."""
        return None

    # ── the cases ───────────────────────────────────────────────────────────

    def test_protocol_shape(self):
        engine = self.make_engine()
        assert isinstance(engine, DataflowEngine)  # runtime_checkable structural check
        assert isinstance(engine.name, str) and engine.name

    def test_validate_accepts_sample(self):
        engine = self.make_engine()
        problems = engine.validate(self.sample_spec())
        assert problems == [], f"engine {engine.name!r} rejects the conformance spec: {problems}"

    def test_validate_reports_unknown_stage(self):
        spec = self.unknown_stage_spec()
        if spec is None:
            pytest.skip("engine defers stage resolution to submit-time")
        engine = self.make_engine()
        problems = engine.validate(spec)
        assert problems, "unknown stage must be reported by validate() (capability discovery)"
        assert any("conformance.does_not_exist" in p for p in problems)

    async def test_submit_poll_result_roundtrip(self):
        engine = self.make_engine()
        spec = self.sample_spec()
        handle = await engine.submit(spec, self.make_ctx())
        assert isinstance(handle, EngineJobHandle) and handle.engine == engine.name
        status = await poll_until_terminal(engine, handle, self.poll_budget_s)
        assert status.state == "succeeded", f"conformance dataflow failed: {status.error}"
        result = await engine.result(handle)
        assert isinstance(result, DataflowResult)
        for descriptor in result.output_artifacts:
            assert isinstance(descriptor, ArtifactDescriptor)  # registry-registrable
        # #1243: an empty list here was vacuously true — a multi-stage spec
        # must come back with real per-stage lineage, or the engine must say
        # so explicitly rather than pass by omission.
        if self.emits_artifact_descriptors:
            assert result.output_artifacts, (
                f"engine {engine.name!r}: a multi-stage spec produced no "
                "output_artifacts — silent lineage drop (declare "
                "emits_artifact_descriptors=False only if this engine "
                "genuinely cannot report per-stage descriptors)"
            )
            assert len(result.output_artifacts) == len(spec.stages), (
                f"engine {engine.name!r}: expected one descriptor per stage "
                f"({len(spec.stages)}), got {len(result.output_artifacts)}"
            )
        # stats must be gate-consumable scalars — enforced by the model, proven here
        DataflowResult.model_validate(result.model_dump())

    async def test_poll_is_idempotent_after_terminal(self):
        engine = self.make_engine()
        handle = await engine.submit(self.sample_spec(), self.make_ctx())
        first = await poll_until_terminal(engine, handle, self.poll_budget_s)
        again = await engine.poll(handle)
        assert again.state == first.state, "a terminal state must be stable across polls"

    async def test_cancel_terminal_is_safe(self):
        engine = self.make_engine()
        handle = await engine.submit(self.sample_spec(), self.make_ctx())
        await poll_until_terminal(engine, handle, self.poll_budget_s)
        await engine.cancel(handle)  # must not raise on an already-terminal job

    async def test_cancel_midrun_reaches_cancelled(self):
        if not self.supports_cancel_midrun:
            pytest.skip("engine does not support mid-run cancel")
        spec = self.slow_spec()
        if spec is None:
            pytest.skip("engine provides no slow_spec for the mid-run cancel case")
        engine = self.make_engine()
        handle = await engine.submit(spec, self.make_ctx())
        await engine.cancel(handle)
        status = await poll_until_terminal(engine, handle, self.poll_budget_s)
        assert status.state == "cancelled", (
            f"cancel must KILL the compute (#603 — orphaned job = cost leak); got {status.state}"
        )

    async def test_metrics_are_scalar_stream(self):
        engine = self.make_engine()
        handle = await engine.submit(self.sample_spec(), self.make_ctx())
        status = await engine.poll(handle)
        # model-level validation IS the check: EngineJobStatus rejects row-shaped
        # metrics, so a conforming engine cannot smuggle data into the stream.
        EngineJobStatus.model_validate(status.model_dump())
        await poll_until_terminal(engine, handle, self.poll_budget_s)

    async def test_running_polls_carry_stage_progress(self):
        """D8a (#1076): stage progress must appear in RUNNING polls' metrics —
        the control plane (job_control / the inline lifecycle) bridges it into
        `stage` run events, so an engine that hides progress mid-run breaks
        the one-pipeline timeline for every flow it executes."""
        if not self.supports_stage_events:
            pytest.skip("engine does not surface stage progress mid-run")
        spec = self.stage_progress_spec()
        if spec is None:
            pytest.skip("engine provides no slow spec for the stage-progress case")
        engine = self.make_engine()
        handle = await engine.submit(spec, self.make_ctx())
        saw_progress = False
        deadline = asyncio.get_event_loop().time() + self.poll_budget_s
        while asyncio.get_event_loop().time() < deadline:
            status = await engine.poll(handle)
            if is_terminal(status):
                break
            if (status.metrics or {}).get("stages_done"):
                saw_progress = True
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
        try:
            await engine.cancel(handle)  # don't wait out the held stage's clock
        except Exception:  # noqa: BLE001 — cleanup only
            pass
        assert saw_progress, (
            f"engine {engine.name!r}: no running poll carried stages_done — "
            "stage events are impossible for this engine (declare "
            "supports_stage_events=False only if that is genuinely true)"
        )

    async def test_budget_abort(self):
        if not self.supports_budget_abort:
            pytest.skip("engine does not self-enforce budgets (control-loop abort still applies)")
        spec = self.slow_spec()
        if spec is None:
            pytest.skip("engine provides no slow_spec for the budget case")
        spec = spec.model_copy(update={"budget": Budget(max_seconds=1)})
        engine = self.make_engine()
        handle = await engine.submit(spec, self.make_ctx())
        status = await poll_until_terminal(engine, handle, self.poll_budget_s)
        assert status.state in ("failed", "cancelled"), "budget breach must abort the dataflow"


    def test_stage_contract_declaration(self):
        """ADR-0118 D7 — an opted-in spec is submitted only to an engine that DECLARES it
        carries stage contracts; any other engine is refused at the submission site."""
        engine = self.make_engine()
        refused = submission_problems(contract_probe_spec(), engine)
        if carries_stage_contracts(engine):
            assert refused == [], refused
        else:
            assert any("carries_stage_contracts" in p for p in refused), refused

    @pytest.mark.parametrize("case", ["conforming", "configuration", "output", "no_envelope"])
    def test_stage_contract_in_the_interpreter(self, case, tmp_path):
        """ADR-0118 D5/D6 — the interpreter accepts a conforming stage and refuses a
        configuration violation, a raw-output violation and, inside an admitted spec, an
        opted-in stage that lost its envelope."""
        engine = self.make_engine()
        if not carries_stage_contracts(engine):
            pytest.skip("engine does not carry stage contracts; refused at submission instead")
        spec = {"conforming": contract_probe_spec(1), "configuration": contract_probe_spec("x"),
                "output": contract_probe_spec(-1), "no_envelope": contract_probe_spec(1, envelope=False)}[case]
        if case == "conforming":
            result = self.run_interpreter(spec, tmp_path)
            if result is None:
                pytest.skip("engine provides no in-process interpreter hook")
            assert result.stats[f"{PROBE_STAGE}.rows"] == 1
            return
        match = {"configuration": "configuration", "output": "output", "no_envelope": "no contract"}[case]
        with pytest.raises(StageContractError, match=match):
            if self.run_interpreter(spec, tmp_path) is None:
                pytest.skip("engine provides no in-process interpreter hook")

    def test_stage_contract_unknown_format_and_oversized_spec(self):
        """ADR-0118 D4 — an unknown contracts format never parses, and a spec over the
        environment-string bound is refused at the submission site."""
        data = contract_probe_spec().model_dump()
        data["contracts"]["format"] = 2
        with pytest.raises(ValueError):
            DataflowSpec.model_validate(data)
        oversized = submission_problems(contract_probe_spec(padding=MAX_SPEC_BYTES), self.make_engine())
        assert any("bound" in p for p in oversized), oversized


class StubEngine:
    """In-memory reference implementation used to validate the SUITE itself
    (tests/dataflow_conformance/). Executes nothing: submit records the spec,
    the first poll completes it, result echoes one descriptor per stage. Kept
    in core so any adapter test can also sanity-check its wiring against a
    known-conforming engine."""

    name = "stub"

    # a job whose spec contains this stage stays RUNNING until cancelled — the
    # spec-driven seam for the mid-run cancel case (engine-level flags would
    # leak across the suite's fresh-engine-per-test calls).
    HOLD_STAGE = "noop.slow"

    def __init__(self, known_stages: Optional[set[str]] = None) -> None:
        self._known = known_stages if known_stages is not None else {"noop", self.HOLD_STAGE}
        self._jobs: dict[str, dict] = {}

    def validate(self, spec: DataflowSpec) -> list[str]:
        return [
            f"unknown stage {ref.stage!r} (stub knows {sorted(self._known)})"
            for ref in spec.stages
            if ref.stage not in self._known
        ]

    async def submit(self, spec: DataflowSpec, ctx) -> EngineJobHandle:
        job_id = f"stub-{len(self._jobs)}"
        held = any(ref.stage == self.HOLD_STAGE for ref in spec.stages)
        self._jobs[job_id] = {"spec": spec, "state": "running", "held": held}
        return EngineJobHandle(job_id=job_id, engine=self.name, meta={"held": held})

    async def poll(self, handle: EngineJobHandle) -> EngineJobStatus:
        job = self._jobs[handle.job_id]
        if job["state"] == "running" and not job["held"]:
            job["state"] = "succeeded"  # instant completion on first observation
        spec: DataflowSpec = job["spec"]
        # D8a stage progress: a held job reports everything BEFORE the hold
        # stage as done; terminal reports the full count.
        if job["state"] == "running":
            done = next(
                (i for i, ref in enumerate(spec.stages) if ref.stage == self.HOLD_STAGE),
                len(spec.stages),
            )
            current = self.HOLD_STAGE if job["held"] else ""
        else:
            done, current = len(spec.stages), ""
        return EngineJobStatus(
            state=job["state"],
            metrics={"stages_total": len(spec.stages), "stages_done": done,
                     "stage": current, "rows_done": 0},
        )

    async def cancel(self, handle: EngineJobHandle) -> None:
        job = self._jobs[handle.job_id]
        if job["state"] == "running":
            job["state"] = "cancelled"

    async def result(self, handle: EngineJobHandle) -> DataflowResult:
        job = self._jobs[handle.job_id]
        spec: DataflowSpec = job["spec"]
        return DataflowResult(
            output_artifacts=[
                ArtifactDescriptor(
                    uri=f"memory://stub/{handle.job_id}/{i}",
                    content_hash=f"stub{i:04d}",
                    stats={"rows": 0},
                )
                for i, _ in enumerate(spec.stages)
            ],
            stats={"stages_run": len(spec.stages)},
            summary="stub dataflow (no data processed)",
        )
