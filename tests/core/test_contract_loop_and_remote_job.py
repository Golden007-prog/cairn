"""#2344 — repeats of one declared step share its recorded contract entry (loop iteration
`<id>#<n>`, ADR-0118 D3): a task leg never silently runs without it, a template leg is not
refused for it. #2345 — the remote_job/docker seam carries the shared contract like the
in_process seam: identity and input/configuration before anything is submitted, output on
the terminal result (inline and durable)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from cairn.core.engine.bundle_pipeline import ContractResolution, InvocationEntry
from cairn.core.operator import Operation, OperationContext
from cairn.core.operator.effective import ContractConformanceError, ContractEnvelope
from cairn.core.operator.jobs import JobHandle, JobStatus

CAP = "contract://x.remote/1.0.0"
_IN, _CFG, _OUT = "contract://x.in/1.0.0", "contract://x.cfg/1.0.0", "contract://x.out/1.0.0"
_DATA = {"capability_ref": CAP, "refs": {"input": _IN, "configuration": _CFG, "output": _OUT},
         "schemas": {_IN: {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
                     _CFG: {"type": "object"},
                     _OUT: {"type": "object", "properties": {"verdict": {"enum": ["pass", "fail"]}}, "required": ["verdict"]}}}


def _loops(*body: str) -> dict:
    return {"loop": {"steps": [{"id": b} for b in body]}}


def _resolution(*paths: str) -> ContractResolution:
    return ContractResolution(format="cairn.resolution/1", distributions=(), definitions=(), invocations=tuple(
        InvocationEntry(path=p, capability_ref=CAP, definitions=(), operator_ref="x/remote", operator_version="0.1.0")
        for p in paths))


@pytest.fixture
def contracts(monkeypatch):
    import cairn.core.pack.registry as reg

    monkeypatch.setattr(reg, "invocation_contract", lambda defs, closure: dict(_DATA))


# ── #2344: loop iterations ─────────────────────────────────────────────────────

@pytest.mark.parametrize("step_id", ["delta", "delta#2", "delta#3"])
def test_a_task_leg_finds_its_entry_on_every_loop_iteration(contracts, step_id):
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    rec = SimpleNamespace(plan=SimpleNamespace(contract_resolution=_resolution("delta"), loops=_loops("delta")))
    env = PipelineBridge._recorded_contract(rec, step_id)
    assert env is not None and env.capability_ref == CAP, "iteration ≥2 must not run without its contract"


def test_a_different_step_never_inherits_an_entry(contracts):
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    rec = SimpleNamespace(plan=SimpleNamespace(contract_resolution=_resolution("delta"), loops=_loops("delta")))
    assert PipelineBridge._recorded_contract(rec, "deltax#2") is None
    assert PipelineBridge._recorded_contract(rec, "other") is None


@pytest.mark.parametrize("step_id", ["reassess", "reassess#2"])
async def test_a_template_leg_is_seeded_its_entries_on_every_loop_iteration(contracts, monkeypatch, step_id):
    import cairn.core.storage.pipeline_store as ps
    from cairn.core.dispatch.dispatcher import RunDispatcher

    rec = SimpleNamespace(plan=SimpleNamespace(contract_resolution=_resolution("reassess/run-gate", "assess/run-gate"),
                                               loops=_loops("reassess")))

    class _Store:
        async def get(self, pid):
            return rec

    monkeypatch.setattr(ps, "default_pipeline_store", lambda: _Store())
    seeded = await RunDispatcher._contract_invocations(None, SimpleNamespace(bundle_pipeline_id="pl", step_id=step_id))
    assert seeded["__leg__"] == step_id and set(seeded) == {"__leg__", "run-gate"}


def test_an_authored_id_ending_in_hash_digits_is_its_own_step(contracts):
    """Review finding: `eval#2` written by an author (no loop) is NOT `eval`'s iteration 2 —
    it keeps its own entry, and never borrows another step's."""
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    own = SimpleNamespace(plan=SimpleNamespace(contract_resolution=_resolution("eval#2"), loops={}))
    assert PipelineBridge._recorded_contract(own, "eval#2") is not None
    other = SimpleNamespace(plan=SimpleNamespace(contract_resolution=_resolution("eval"), loops={}))
    assert PipelineBridge._recorded_contract(other, "eval#2") is None


# ── #2345: the remote_job seam ─────────────────────────────────────────────────

class _In(BaseModel):
    n: int = 0


class _Out(BaseModel):
    verdict: str = "pass"


SUBMITTED: list = []


def _op(verdict: str = "pass", implements: tuple = (CAP,)):
    class _RemoteOp(Operation):
        name, version = "x/remote", "0.1.0"
        input_schema, output_schema = _In, _Out
        executors = ["remote_job"]

        async def run(self, inp, ctx: OperationContext):
            return _Out(verdict=verdict)

        async def submit(self, inp, ctx):
            SUBMITTED.append(inp)
            return JobHandle(job_id="j1", backend="fake")

        async def poll(self, handle, inp, ctx):
            return JobStatus(state="succeeded", output=_Out(verdict=verdict))

        async def cancel(self, handle):
            return None
    _RemoteOp.implements = implements
    return _RemoteOp


def _binding(n):
    from cairn.core.operator.binding import OperationBinding

    return OperationBinding(operator="x/remote", input={"n": n})


async def _job(op_cls, n=1, contract=True):
    from cairn.core.operator.runtime import invoke_operator_job

    SUBMITTED.clear()
    return await invoke_operator_job(_binding(n), op_cls, driver_kind="remote_job", state={}, pipeline_id="p",
                                     node_name="d", poll_interval=0, max_polls=3,
                                     contract=ContractEnvelope.from_dict({**_DATA, "operator_ref": "x/remote",
                                                                          "operator_version": "0.1.0"}) if contract else None)


async def test_a_conforming_remote_job_runs_and_returns():
    from cairn.core.operator.binding import OperationBinding

    SUBMITTED.clear()
    from cairn.core.operator.runtime import invoke_operator_job

    out = await invoke_operator_job(
        OperationBinding(operator="x/remote", input={"n": 1}, output={"verdict": "$state.v"}), _op(),
        driver_kind="remote_job", state={}, pipeline_id="p", node_name="d", poll_interval=0, max_polls=3,
        contract=ContractEnvelope.from_dict({**_DATA, "operator_ref": "x/remote", "operator_version": "0.1.0"}))
    assert SUBMITTED and out == {"v": "pass"}


async def test_a_shared_input_violation_refuses_before_anything_is_submitted():
    with pytest.raises(ContractConformanceError, match="input"):
        await _job(_op(), n="not-an-int")
    assert SUBMITTED == []


async def test_identity_drift_refuses_before_anything_is_submitted():
    with pytest.raises(ContractConformanceError, match="no longer matches"):
        await _job(_op(implements=()))
    assert SUBMITTED == []


async def test_a_non_conforming_remote_result_fails_the_node():
    with pytest.raises(ContractConformanceError, match="output"):
        await _job(_op(verdict="maybe"))


async def test_a_legacy_remote_job_is_unchanged():
    await _job(_op(verdict="maybe"), contract=False)   # no contract → no check, the job still ran
    assert len(SUBMITTED) == 1


async def test_invoke_operator_hands_the_contract_to_the_job_seam(monkeypatch):
    import cairn.core.operator.runtime as rt

    seen = {}

    async def _spy(binding, op_cls, **kw):
        seen.update(kw)
        return {}
    monkeypatch.setattr(rt, "invoke_operator_job", _spy)
    monkeypatch.setattr("cairn.core.operator.executor.pick_executor", lambda *a, **k: "remote_job")
    env = ContractEnvelope.from_dict({**_DATA, "operator_ref": "x/remote", "operator_version": "0.1.0"})
    await rt.invoke_operator(_binding(1), _op(), state={}, pipeline_id="p", node_name="d", contract=env)
    assert seen.get("contract") is env


async def test_the_durable_path_judges_the_terminal_result(monkeypatch):
    import cairn.core.operator.runtime as rt
    from cairn.core.operator.job_store import DONE

    class _Store:
        async def get(self, rid):
            return SimpleNamespace(status=DONE, result={"verdict": "maybe"})

    monkeypatch.setattr("cairn.core.operator.job_store.get_remote_job_store", lambda: _Store())
    env = ContractEnvelope.from_dict({**_DATA, "operator_ref": "x/remote", "operator_version": "0.1.0"})
    with pytest.raises(ContractConformanceError, match="output"):
        await rt._invoke_remote_job_durable(None, _In(n=1), None, _binding(1), "x/remote", run_id="run-1",
                                            node_name="d", resolved_input={"n": 1}, contract=env)


async def test_the_durable_path_refuses_before_registering_or_submitting(monkeypatch):
    """The check sits ABOVE the durable branch: with OBS_REMOTE_JOB_DURABLE on, a
    non-conforming input registers no job record and submits nothing."""
    registered = []

    class _Store:
        async def get(self, rid):
            return None

        async def register(self, rec):
            registered.append(rec)

    monkeypatch.setenv("OBS_REMOTE_JOB_DURABLE", "1")
    monkeypatch.setattr("cairn.core.operator.job_store.get_remote_job_store", lambda: _Store())
    with pytest.raises(ContractConformanceError, match="input"):
        await _job(_op(), n="not-an-int")
    assert SUBMITTED == [] and registered == []
