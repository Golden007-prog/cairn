"""#568 — a JOB/ACTION operator (remote_job / docker) that fails must FAIL the
run, not be swallowed into a `done` run with a failure finding (a false success:
the governed action never happened). Investigation / in_process operators keep
the resilient degrade-to-finding behavior.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel

from cairn.core.engine.compiler import compile_template
from cairn.core.operator import Operation, OperationContext, default_registry, register_operator
from cairn.core.operator.jobs import JobHandle, JobStatus
from cairn.core.pack.manifest import OperatorEntry


class _In(BaseModel):
    pass


class _Out(BaseModel):
    ok: bool = True


@register_operator(name="test/failing_remote", version="0.1.0", capabilities=["test.fr"])
class _FailingRemote(Operation):
    """A remote_job operator whose submit blows up (e.g. SSH failure)."""
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:  # unused on the job path
        return _Out()

    async def submit(self, inp, ctx: OperationContext) -> JobHandle:
        raise RuntimeError("ssh boom — dispatch failed")

    async def poll(self, handle: JobHandle, inp, ctx: OperationContext) -> JobStatus:
        return JobStatus(state="succeeded", output=_Out())

    async def cancel(self, handle: JobHandle) -> None:
        return None


@register_operator(name="test/failing_probe", version="0.1.0", capabilities=["test.fp"])
class _FailingProbe(Operation):
    """An in_process investigation operator whose run() raises."""
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        raise RuntimeError("probe hiccup")


def _ensure():
    for name, cls, execs in (
        ("test/failing_remote", _FailingRemote, ["remote_job"]),
        ("test/failing_probe", _FailingProbe, ["in_process"]),
    ):
        if not default_registry.has(name):
            default_registry.register(cls)
        default_registry.attach_metadata(OperatorEntry(
            name=name, executors=execs, provides_capabilities=[f"test.{name[-2:]}"]))


def _template(tmp_path, ref, op):
    (tmp_path / "templates").mkdir(exist_ok=True)
    (tmp_path / "operations").mkdir(exist_ok=True)
    (tmp_path / "operations" / f"{ref}.yaml").write_text(
        f"operator: {op}\ninput: {{}}\noutput:\n  ok: $state.did\n")
    tmpl = tmp_path / "templates" / f"{ref}.md"
    tmpl.write_text(
        f"---\nname: {ref}\nversion: 0.1.0\nflow:\n  type: state_machine\n"
        f"  source: behavior_tree\n  root: {{ type: operation, ref: {ref} }}\n  nodes: {{}}\n"
        f"tools:\n  required: []\n---\n# t\n")
    return tmpl


@pytest.mark.asyncio
async def test_remote_job_failure_fails_the_run(tmp_path):
    """A remote_job submit failure PROPAGATES (→ dispatcher marks the run failed),
    instead of returning a success/finding that let the run report `done`."""
    _ensure()
    compiled = compile_template(_template(tmp_path, "actremote", "test/failing_remote"))
    with pytest.raises(RuntimeError, match="ssh boom"):
        await compiled.run({"run_id": "act-1"})


@pytest.mark.asyncio
async def test_in_process_failure_still_degrades_to_finding(tmp_path):
    """Unchanged: an in_process investigation operator's failure is captured as a
    finding and the run continues (resilient probing)."""
    _ensure()
    compiled = compile_template(_template(tmp_path, "actprobe", "test/failing_probe"))
    result = await compiled.run({"run_id": "act-2"})
    assert "__interrupt__" not in result
    assert result.get("last_action_status") == "failure"
    assert "probe hiccup" in str(result.get("findings", {}))


@pytest.mark.asyncio
async def test_remote_job_failure_marks_run_failed_not_done(tmp_path, monkeypatch):
    """The reported symptom, end-to-end through the DISPATCHER: a remote_job submit
    failure must leave the run terminal-`failed` with the error persisted — NOT a
    false `done`. The compiler re-raise (test above) only proves the graph raises;
    this proves the raise reaches the dispatcher's failure handler and becomes the
    governance signal (status + error), which is what was wrong live on Temporal.
    """
    import cairn.core.storage as storage_mod
    from cairn.core.dispatch.dispatcher import RunDispatcher
    from cairn.core.storage import build_storage
    from tests.core.test_trigger_server_dispatch import FakeRunner

    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path}/runs.db")
    monkeypatch.setattr(storage_mod, "_STORAGE", None)  # don't inherit a leaked singleton
    _ensure()
    tmpl = _template(tmp_path, "actremote2", "test/failing_remote")
    monkeypatch.setattr("cairn.core.engine.skill_engine.resolve_template_path", lambda ref: str(tmpl))
    monkeypatch.setattr("cairn.core.operator.load_all_operators", lambda **k: [])
    monkeypatch.setattr("cairn.core.pack.loader.load_all_packs", lambda **k: None)

    d = RunDispatcher(FakeRunner(), backend=build_storage())
    run = await d.run_template("actremote2", {})
    await d._await_task(run.id)

    rec = await d.store.get(run.id)
    assert rec.status == "failed", f"remote-job submit failure must fail the run, got {rec.status!r}"
    assert rec.status != "done"                       # the false-success this guards against
    assert "ssh boom" in (rec.error or "")            # the error is persisted, not swallowed
