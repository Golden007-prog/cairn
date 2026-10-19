"""#882 — the prep_scrub_gate_passed guard condition (dataset/prep_gate)."""
from __future__ import annotations

from cairn.core.engine.condition_registry import resolve_condition


def _c(findings: dict) -> str:
    return resolve_condition("prep_scrub_gate_passed")({"findings": findings})


def test_block_action_routes_false():
    # only an explicit block halts the consumer's train subtree.
    assert _c({"prep_gate_action": "block"}) == "false"


def test_pass_and_review_and_absent_route_true():
    assert _c({"prep_gate_action": "pass"}) == "true"      # clean / off
    assert _c({"prep_gate_action": "review"}) == "true"    # hitl handled by its own guard
    assert _c({}) == "true"                                  # gate didn't run → proceed


# ── #883 — the HITL review guard ──────────────────────────────────────────────


def _r(findings: dict) -> str:
    return resolve_condition("prep_scrub_needs_review")({"findings": findings})


def test_review_action_routes_to_hitl():
    assert _r({"prep_gate_action": "review"}) == "true"     # mode=hitl + dirty


def test_non_review_actions_skip_hitl():
    assert _r({"prep_gate_action": "pass"}) == "false"      # clean / off
    assert _r({"prep_gate_action": "block"}) == "false"     # block is fail-closed, not review
    assert _r({}) == "false"                                 # gate didn't run


def test_prep_gate_op_error_blocks():
    # #914 fail-CLOSED: prep_gate op errored (no action written) → the re-scan
    # verdict is unknown → block, don't proceed on an unverified scrub.
    from cairn.core.engine.condition_registry import resolve_condition
    fn = resolve_condition("prep_scrub_gate_passed")
    assert fn({"findings": {"run-prep-gate_error": "boom"}}) == "false"
    # explicit action still wins over a stray error finding
    assert fn({"findings": {"prep_gate_action": "pass", "run-prep-gate_error": "x"}}) == "true"
