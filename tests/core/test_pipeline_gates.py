"""Named `gate:` steps between legs → the ONE approval queue (#1128, ADR-0084).

The bridge already pauses on `approval` and lands in `awaiting_approval`, which
is the queue the console and `/runs/{id}/pending-approval` already read. What it
did NOT carry is the context an approver needs: **which gate**, **what to look
at**, **who may decide**, and **by when**.

`gate: <name>` + `show:` + `approvers:` + `timeout:` fills that in. The
invariant under test is that this stays ONE queue — a pipeline gate must not
become a second approval surface with its own semantics.
"""
from __future__ import annotations

import textwrap

import pytest

from cairn.core.engine.pipeline import parse_pipeline_def, plan_pipeline
from cairn.core.engine.pipeline_gate import GateContext, gate_context_for, resolve_show

_YAML = """
name: e2e
kind: pipeline
inputs: {dataset: {type: Dataset}}
governance: {approvers: ml-leads}
steps:
  - {id: readiness, flow: readiness}
  - {id: train, flow: sft-train, depends_on: [readiness]}
  - gate: promote-model
    show: [readiness.verdict, train.metrics]
    approvers: release-managers
    timeout: 7d
    depends_on: [train]
  - {id: report, template: report, depends_on: [promote-model]}
"""


def _def(body: str = _YAML):
    return parse_pipeline_def(textwrap.dedent(body), pack="training", source="<t>")


# ── the gate reaches the plan ────────────────────────────────────────────────

def test_a_named_gate_becomes_an_approval_step():
    plan = plan_pipeline(_def())
    gate = next(s for s in plan.steps if s.id == "promote-model")
    assert gate.approval is True, "a gate step must pause for a human"


def test_the_gate_keeps_its_name_as_the_step_id():
    """Approvals are audited BY NAME — the id must be the gate's name, not a
    positional index."""
    assert any(s.id == "promote-model" for s in plan_pipeline(_def()).steps)


def test_downstream_legs_depend_on_the_gate():
    plan = plan_pipeline(_def())
    report = next(s for s in plan.steps if s.id == "report")
    assert "promote-model" in report.depends_on


# ── the context an approver actually needs ───────────────────────────────────

def test_show_fields_resolve_from_prior_leg_outputs():
    """`show:` is the difference between 'approve this' and 'approve this,
    here is why'."""
    outputs = {"readiness": {"verdict": "pass"}, "train": {"metrics": {"loss": 0.4}}}
    got = resolve_show(["readiness.verdict", "train.metrics"], outputs)
    assert got == {"readiness.verdict": "pass", "train.metrics": {"loss": 0.4}}


def test_a_missing_show_field_is_reported_not_dropped():
    """Silently omitting a field the author asked for would show the approver an
    incomplete picture with no sign anything was missing."""
    got = resolve_show(["train.ghost"], {"train": {"metrics": 1}})
    assert "train.ghost" in got and got["train.ghost"] is None


def test_gate_context_carries_name_fields_and_approvers():
    ctx = gate_context_for(
        _def().steps[2],
        outputs={"readiness": {"verdict": "pass"}, "train": {"metrics": {}}},
        governance={"approvers": "ml-leads"},
    )
    assert isinstance(ctx, GateContext)
    assert ctx.gate == "promote-model"
    assert ctx.approvers == "release-managers", "the step overrides the pipeline default"
    assert ctx.timeout == "7d"
    assert ctx.fields["readiness.verdict"] == "pass"


def test_the_context_is_built_from_the_step_the_BRIDGE_passes(): # noqa: N802
    """#1627 — the coverage gap that made #1128 green in tests and inert live.

    Every assertion above hands ``gate_context_for`` the AUTHORED
    ``PipelineStep``. Its one production caller (``PipelineBridge._advance``)
    hands it a ``PipelinePlanStep``, which carried no ``show``, no
    ``approvers`` and no ``timeout`` at all — so a real pipeline gate paused
    with an empty field map and an empty audience, and its ``gate``/``step_id``
    read off a field that holds the PREDICATE on that shape. Same gate, same
    call, the other argument.
    """
    plan = plan_pipeline(_def())
    gate = next(s for s in plan.steps if s.id == "promote-model")
    ctx = gate_context_for(
        gate,
        outputs={"readiness": {"verdict": "pass"}, "train": {"metrics": {"loss": 0.4}}},
        governance={"approvers": "ml-leads"},
    )
    assert ctx.gate == "promote-model" and ctx.step_id == "promote-model"
    assert ctx.fields["readiness.verdict"] == "pass"
    assert ctx.fields["train.metrics"] == {"loss": 0.4}
    assert ctx.approvers == "release-managers"   # the step still overrides
    assert ctx.timeout == "7d"
    # a non-gate leg carries none of it — the context surface is gate-only
    train = next(s for s in plan.steps if s.id == "train")
    assert train.show == [] and train.approvers is None and train.timeout is None


def test_the_pipeline_default_approvers_apply_when_the_step_is_silent():
    body = _YAML.replace("    approvers: release-managers\n", "")
    ctx = gate_context_for(_def(body).steps[2], outputs={},
                           governance={"approvers": "ml-leads"})
    assert ctx.approvers == "ml-leads"


def test_no_approvers_anywhere_is_empty_not_an_error():
    """An unstaffed gate still pauses — it must never fail OPEN."""
    body = _YAML.replace("    approvers: release-managers\n", "").replace(
        "governance: {approvers: ml-leads}", "governance: {}")
    ctx = gate_context_for(_def(body).steps[2], outputs={}, governance={})
    assert ctx.approvers == "" and ctx.gate == "promote-model"


# ── the invariant: ONE queue ─────────────────────────────────────────────────

def test_the_gate_status_is_the_existing_awaiting_approval():
    """THE invariant. A pipeline gate must land in the SAME queue as every other
    approval — not a parallel surface with its own status vocabulary."""
    from cairn.core.engine.pipeline_gate import GATE_STATUS

    assert GATE_STATUS == "awaiting_approval"


def test_gate_context_serializes_for_the_existing_endpoint():
    """`/runs/{id}/pending-approval` and the console read plain dicts."""
    ctx = gate_context_for(_def().steps[2],
                           outputs={"readiness": {"verdict": "pass"}}, governance={})
    d = ctx.as_dict()
    assert d["gate"] == "promote-model" and "fields" in d and "approvers" in d


# ── shape validation ─────────────────────────────────────────────────────────

def test_a_gate_with_a_malformed_show_ref_is_rejected_at_parse():
    with pytest.raises(Exception, match="show"):
        _def(_YAML.replace("show: [readiness.verdict, train.metrics]",
                           "show: [nodotsatall]"))


def test_a_gate_may_omit_show():
    d = _def(_YAML.replace("    show: [readiness.verdict, train.metrics]\n", ""))
    assert d.steps[2].leg_kind == "gate" and d.steps[2].show == []


# ── #1689: the two gate surfaces must speak ONE grammar ─────────────────────
# A gate has two readers of the same refs: the PREDICATE (the engine) and
# `show:` (the human). They resolved differently. `expr._step_lookup` gives the
# predicate a `findings.` fallback — a FLOW leg's stats live under `findings` —
# while `resolve_show` walked the path literally. So `steps.train.eval_loss`
# passed a gate while the identical spelling rendered `None` to the person
# being asked to approve the spend that gate protects.

def test_a_flow_legs_stat_resolves_without_spelling_findings():
    """The issue's verbatim reproduction. This returned None."""
    got = resolve_show(["train.train_qlora_train_loss"],
                       {"train": {"findings": {"train_qlora_train_loss": 0.3}}})
    assert got == {"train.train_qlora_train_loss": 0.3}


def test_the_explicit_findings_spelling_still_works():
    """#1686 worked around this by authoring `findings.` into every ref. Those
    refs are in shipped pipelines; the fix must not invalidate them."""
    got = resolve_show(["train.findings.train_qlora_train_loss"],
                       {"train": {"findings": {"train_qlora_train_loss": 0.3}}})
    assert got == {"train.findings.train_qlora_train_loss": 0.3}


def test_top_level_wins_over_findings():
    """Precedence matches the predicate's: exact path first, `findings` only as
    a fallback. Reversing it would let a stale finding shadow a real output."""
    got = resolve_show(["d.verdict"],
                       {"d": {"verdict": "pass", "findings": {"verdict": "fail"}}})
    assert got == {"d.verdict": "pass"}


def test_a_truly_missing_ref_is_still_None_after_the_fallback():
    """The fallback must not turn 'absent' into 'silently dropped' — a null the
    approver can see is the honest answer."""
    got = resolve_show(["d.nope"], {"d": {"verdict": "pass", "findings": {}}})
    assert got == {"d.nope": None}


def test_predicate_and_show_agree_on_the_same_ref():
    """The invariant behind the bug, stated directly: one spelling, one value,
    whichever surface of the gate reads it."""
    from cairn.core.engine.expr import step_field

    out = {"readiness": {"findings": {"dedup_exact_rows": 412}}}
    assert (resolve_show(["readiness.dedup_exact_rows"], out)["readiness.dedup_exact_rows"]
            == step_field(out["readiness"], "dedup_exact_rows")
            == 412)


def test_retrain_cycles_first_gate_shows_the_approver_a_number():
    """End-to-end through `gate_context_for`, not just the resolver.

    `enough-signal` is the retrain cycle's NO-PAID-COMPUTE-YET gate
    and it reads a FLOW leg's row count. A person deciding whether to start a
    GPU cycle on the strength of `dedup_exact_rows` was being shown `null`.
    """
    gate = _def(
        """
        name: cl
        kind: pipeline
        governance: {approvers: ml-leads}
        steps:
          - {id: readiness, flow: readiness}
          - gate: enough-signal
            depends_on: [readiness]
            show: [readiness.dedup_exact_rows, readiness.verdict]
        """
    ).steps[1]
    ctx = gate_context_for(
        gate,
        outputs={"readiness": {"verdict": "pass",
                               "findings": {"dedup_exact_rows": 412}}},
        governance={"approvers": "ml-leads"},
    ).as_dict()
    assert ctx["fields"]["readiness.dedup_exact_rows"] == 412
    assert ctx["fields"]["readiness.verdict"] == "pass"
    assert not [k for k, v in ctx["fields"].items() if v is None]
    assert ctx["approvers"] == "ml-leads"
