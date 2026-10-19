"""``OperationContext.launch`` (ADR-0120 D7, #2455): the dispatch layer's closure over
the launch door, bound for every operator a governed run invokes — a template node
through ``launch_scope``, a pipeline task leg through ``ctx_overrides``. A
``pipeline:`` ref takes the launch door (where a policy deny raises before any record
exists); any other ref runs as a template on the background lane; outside a governed
run the handle is ``None``."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.operator import Operation, OperationContext, ReportSpec
from cairn.core.operator.runtime import launch_scope
from cairn.core.operator.testing import run_operator_for_test

LAUNCH = "cairn.core.dispatch.pipeline_launch.launch_pipeline"


async def test_a_pipeline_ref_takes_the_launch_door_with_the_bridge_and_provenance(monkeypatch):
    seen = {}

    async def fake_launch(ref, *, inputs, bridge, trigger=None, **kw):
        seen.update(ref=ref, inputs=inputs, bridge=bridge, trigger=trigger)
        return SimpleNamespace(pipeline_id="pl_1")

    monkeypatch.setattr(LAUNCH, fake_launch)
    handle = RunDispatcher.launch_handle(SimpleNamespace(_pipeline_bridge="BRIDGE"))
    assert await handle("pipeline:p/x", {"a": 1}, {"source": "watch"}) == "pl_1"
    assert seen == {"ref": "pipeline:p/x", "inputs": {"a": 1}, "bridge": "BRIDGE", "trigger": {"source": "watch"}}


async def test_a_deny_at_the_launch_door_refuses_the_launch(monkeypatch):
    """What the door raises (a policy `deny` fires at plan time, before any record)
    reaches the operator as the failure of its own leg — nothing half-started."""
    async def deny(ref, **kw):
        raise ValueError("policy no-gpu-in-staging denies p/x")

    monkeypatch.setattr(LAUNCH, deny)
    handle = RunDispatcher.launch_handle(SimpleNamespace(_pipeline_bridge="BRIDGE"))
    with pytest.raises(ValueError, match="denies p/x"):
        await handle("pipeline:p/x", {}, {})


async def test_any_other_ref_runs_as_a_template_on_the_background_lane():
    calls = []

    async def run_template(ref, inputs, **kw):
        calls.append((ref, inputs, kw))
        return SimpleNamespace(id="run-9")

    handle = RunDispatcher.launch_handle(SimpleNamespace(run_template=run_template))
    assert await handle("training/finetune", {"d": 1}, {}) == "run-9"
    assert calls == [("training/finetune", {"d": 1}, {"lane": "background"})]


class _In(BaseModel):
    pass


class _Out(BaseModel):
    launched: str


class _Probe(Operation[_In, _Out]):
    """Reports what its context's launch handle returns ('-' when there is none)."""
    name = "test/launch-probe"
    version = "0.0.1"
    capabilities = ["test.probe"]
    input_schema = _In
    output_schema = _Out
    reports = ReportSpec.NONE

    async def run(self, inp: _In, ctx: OperationContext) -> _Out:
        if ctx.launch is None:
            return _Out(launched="-")
        return _Out(launched=await ctx.launch("pipeline:p/x", {}, {}))


async def _h(ref, inputs, trigger):
    return "scoped"


async def _h2(ref, inputs, trigger):
    return "override"


async def test_the_scoped_handle_reaches_every_operator_invoked_in_the_block():
    with launch_scope(_h):
        assert (await run_operator_for_test(_Probe, {})).launched == "scoped"
    assert (await run_operator_for_test(_Probe, {})).launched == "-"  # outside: None


async def test_an_explicit_override_wins_over_the_scope():
    with launch_scope(_h):
        out = await run_operator_for_test(_Probe, {}, ctx_overrides={"launch": _h2})
    assert out.launched == "override"
