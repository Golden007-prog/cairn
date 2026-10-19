"""#1671 — wires inside a loop body must track iterations.

`unroll_iteration` rewrites `depends_on` to the suffixed ids but copies input
wire STRINGS verbatim, so iteration 2's `$steps.a.token` resolved
`outputs["a"]` — iteration 1, forever. `until:` was iteration-aware
(`evaluate_until` suffixes then falls back), which made the freeze invisible:
the loop's exit condition read live data while the work beside it re-processed
iteration 1's rows.

The chain op below makes the data flow itself the assertion: every hop appends
to a token, so the token IS the wiring history and a frozen wire produces a
visibly shorter string. This is also the exact shape ADR-0095's
retrain-cycle flagship needs (#1627): `a` reading `b`'s PREVIOUS-iteration
output is `init_adapter_uri` continuing the chain from the last cycle's
adapter.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel, ConfigDict

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.engine.pipeline import parse_pipeline_def, plan_pipeline
from cairn.core.engine.pipeline_loop import iteration_view, parse_iteration_id
from cairn.core.operator import Operation, register_operator
from cairn.core.storage import build_storage
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from tests.core.test_trigger_server_dispatch import FakeRunner


class _P(BaseModel):
    model_config = ConfigDict(extra="allow")


@register_operator(name="_t1671/chain", version="0.1.0")
class _Chain(Operation[_P, _P]):
    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        return _P(token=f"{getattr(inp, 'prev', '')}>{getattr(inp, 'tag', '')}")


@pytest.fixture(autouse=True)
def _ops_registered():
    from cairn.core.operator.registry import default_registry
    try:
        default_registry.register(_Chain, strict=False)
    except Exception:  # noqa: BLE001 — already registered is fine
        pass


# ─── the pure view ───────────────────────────────────────────────────────


def test_parse_iteration_id_inverts_the_suffix():
    assert parse_iteration_id("train") == ("train", 1)
    assert parse_iteration_id("train#3") == ("train", 3)
    # a non-numeric suffix is part of the name, not an iteration
    assert parse_iteration_id("a#b") == ("a#b", 1)


_LOOPS = {"cyc": {"steps": [{"id": "a"}, {"id": "b"}]}}


def test_view_is_identity_outside_any_loop():
    outs = {"seed": {"x": 1}}
    assert iteration_view(outs, "seed", {}) is outs
    assert iteration_view(outs, "seed", _LOOPS) is outs  # no suffixed rows yet


def test_view_aliases_a_body_name_to_its_latest_completed_iteration():
    outs = {"a": {"n": 1}, "b": {"n": 1}, "a#2": {"n": 2}}
    # b#2 consuming `a` must see a#2 (intra-iteration), and `b` still means
    # iteration 1 — its own #2 hasn't run.
    v = iteration_view(outs, "b#2", _LOOPS)
    assert v["a"] == {"n": 2} and v["b"] == {"n": 1}


def test_view_caps_at_the_consumers_own_iteration():
    # a#2 must not read a future iteration's rows even if one exists (defensive
    # — temporally impossible today, but the cap is what makes replay of a
    # resumed record deterministic).
    outs = {"b": {"n": 1}, "b#2": {"n": 2}, "b#3": {"n": 3}}
    assert iteration_view(outs, "a#2", _LOOPS)["b"] == {"n": 2}


def test_a_step_after_the_loop_sees_the_final_iteration():
    outs = {"a": {"n": 1}, "a#2": {"n": 2}, "a#3": {"n": 3}}
    assert iteration_view(outs, "report", _LOOPS)["a"] == {"n": 3}


# ─── the bridge, end to end ──────────────────────────────────────────────


def _def():
    # `a` reads `b`'s PREVIOUS-iteration output ($default covers iteration 1 —
    # the no-predecessor case, like a first cycle training from the base model);
    # `b` reads THIS iteration's `a`. until is unsatisfiable → exactly
    # max_iterations run, ending at the exhaustion gate.
    return parse_pipeline_def(
        """
name: chained
kind: pipeline
inputs: {seed: {type: str}}
steps:
  - loop: cyc
    steps:
      - id: a
        task: _t1671/chain
        with: {prev: "$default($steps.b.token, $input.seed)", tag: A}
      - id: b
        task: _t1671/chain
        depends_on: [a]
        with: {prev: "$steps.a.token", tag: B}
    until: "b.token == 'never'"
    max_iterations: 2
""", pack="t1671", source="<test>")


async def _run_two_iterations():
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    b = PipelineBridge(d, store=InMemoryPipelineStore())
    await b.start()
    try:
        rec = await b.start_pipeline(plan_pipeline(_def()), inputs={"seed": "S"})
        deadline = asyncio.get_event_loop().time() + 8.0
        while True:
            rec = await b.store.get(rec.pipeline_id)
            # the tail of the FINAL iteration ends at the exhaustion gate
            # (`on_exhausted: gate` default): it RAN, then paused
            # awaiting_approval — that still counts as "its run exists".
            settled = {k for k, v in rec.step_state.items()
                       if v.run_id and v.status in ("done", "awaiting_approval")}
            if {"a", "b", "a#2", "b#2"} <= settled or rec.status == "failed":
                break
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError(
                    "stuck; steps=" + str({k: v.status for k, v in rec.step_state.items()}))
            await asyncio.sleep(0.02)
        outs = {}
        for sid in ("a", "b", "a#2", "b#2"):
            run = await d.store.get(rec.step_state[sid].run_id)
            outs[sid] = run
        return rec, outs
    finally:
        b.stop()


def _token(run) -> str:
    from cairn.core.dispatch.pipeline_bridge import reconstruct_step_output
    return str(reconstruct_step_output(run).get("token", ""))


async def test_iteration_two_wires_read_live_data_not_iteration_one():
    rec, outs = await _run_two_iterations()
    assert _token(outs["a"]) == "S>A"
    assert _token(outs["b"]) == "S>A>B"
    # THE fix: a#2 continues from iteration 1's b (the N−1 carry), and b#2
    # consumes a#2 — not the frozen iteration-1 a. Before #1671 b#2 was
    # "S>A>B": byte-identical to b, silently re-doing iteration 1's work.
    assert _token(outs["a#2"]) == "S>A>B>A"
    assert _token(outs["b#2"]) == "S>A>B>A>B"
