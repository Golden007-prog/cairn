"""monitor/dispatch — launching what a human approved (#1162, closes #992's loop).

The operator is the one place a watch turns into spend, so the tests are about
restraint first: a rejected proposal launches nothing, an approved one launches
exactly what was proposed, and the launch itself goes through the dispatch
layer's handle (``ctx.launch``, ADR-0120 D7 / #2455) — never a dispatcher this
pack builds. Without a handle (dev loop, tests) nothing is launched and the
output says why.
"""
from __future__ import annotations

import ast
from pathlib import Path

from operator_monitor_threshold.operators import dispatch as dispatch_module
from operator_monitor_threshold.operators.dispatch import DispatchOp
from operator_monitor_threshold.schemas.dispatch import DispatchInput


class _Ctx:
    """The only context attribute the operator reads is ``launch``."""

    def __init__(self, launch=None):
        self.launch = launch


class _Launch:
    """A fake launch handle: records (ref, inputs, trigger), returns a fixed id."""

    def __init__(self, run_id: str = "run-123"):
        self.calls: list[tuple] = []
        self.run_id = run_id

    async def __call__(self, ref, inputs, trigger):
        self.calls.append((ref, inputs, trigger))
        return self.run_id


async def _run(launch=None, **kw):
    return await DispatchOp().run(DispatchInput(**kw), _Ctx(launch))


# ── the no-spend path ────────────────────────────────────────────────────────


async def test_a_REJECTED_proposal_launches_nothing():
    h = _Launch()
    out = await _run(launch=h, approved=False, proposed_workflow="training/finetune")
    assert not out.dispatched and out.run_id == ""
    assert h.calls == [], "a rejected proposal must not reach the launch handle"


async def test_the_refusal_says_why():
    out = await _run(launch=_Launch(), approved=False, proposed_workflow="training/finetune")
    assert "not approved" in out.reason


async def test_approved_but_no_target_is_also_a_no_op():
    h = _Launch()
    out = await _run(launch=h, approved=True, proposed_workflow="  ")
    assert not out.dispatched and "no proposed_workflow" in out.reason
    assert h.calls == []


async def test_without_a_launch_handle_nothing_is_launched_and_the_output_says_so():
    """Dev loop / tests: no governed run, no handle, no dispatcher of its own."""
    out = await _run(launch=None, approved=True, proposed_workflow="pipeline:training/e2e-finetune")
    assert not out.dispatched and out.launched_ref == "pipeline:training/e2e-finetune"
    assert "launch handle" in out.reason


# ── the launch ───────────────────────────────────────────────────────────────


async def test_an_approved_ref_is_launched_through_the_handle():
    h = _Launch("run-9")
    out = await _run(launch=h, approved=True, proposed_workflow="training/finetune",
                     inputs={"dataset": "s3://d"})
    assert out.dispatched and out.launched_ref == "training/finetune" and out.run_id == "run-9"
    assert [c[0] for c in h.calls] == ["training/finetune"]
    assert h.calls[0][1] == {"dataset": "s3://d"}


async def test_a_pipeline_ref_is_passed_to_the_handle_unchanged():
    """`pipeline:` is the dispatch layer's door selector, not something to strip."""
    h = _Launch("pl_1")
    out = await _run(launch=h, approved=True, proposed_workflow="pipeline:training/e2e-finetune")
    assert out.dispatched and out.run_id == "pl_1"
    assert h.calls[0][0] == "pipeline:training/e2e-finetune"


async def test_the_launch_carries_WATCH_PROVENANCE():
    """A run that exists because a metric moved must be traceable to that metric,
    the threshold, and the estimate the human actually authorised."""
    h = _Launch()
    await _run(launch=h, approved=True, proposed_workflow="pipeline:p/x", metric="psi",
               value=0.31, threshold=0.2, est_cost_usd=12.5)
    trigger = h.calls[0][2]
    assert trigger == {"source": "watch", "metric": "psi", "value": 0.31,
                       "threshold": 0.2, "approved_est_usd": 12.5}


def test_the_operator_imports_no_dispatch_or_engine_internals():
    """ADR-0120 D7: the pack reaches the launch door only through ctx.launch."""
    src = Path(dispatch_module.__file__).read_text()
    mods = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in mods if m.startswith(("cairn.core.dispatch", "cairn.core.engine", "cairn.core.storage"))], mods
