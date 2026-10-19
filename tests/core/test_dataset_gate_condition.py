"""#9/#580: the dataset_gate_passed condition ENFORCES the dataset gate."""
from cairn.core.engine.condition_registry import known_conditions, resolve_condition

_fn = resolve_condition("dataset_gate_passed")


def test_registered():
    assert "dataset_gate_passed" in known_conditions()


def test_fail_blocks_training():
    assert _fn({"findings": {"dataset_verdict": "fail"}}) == "false"


def test_warn_and_pass_train():
    assert _fn({"findings": {"dataset_verdict": "warn"}}) == "true"
    assert _fn({"findings": {"dataset_verdict": "pass"}}) == "true"


def test_absent_verdict_trains():
    # the ungated `train` template has no dataset gate → must not block
    assert _fn({"findings": {}}) == "true"
    assert _fn({}) == "true"


def test_case_insensitive():
    assert _fn({"findings": {"dataset_verdict": "FAIL"}}) == "false"


def test_gate_op_error_blocks_training():
    # #914 fail-CLOSED (found live, #888 E2E): the composed gate op ran and
    # ERRORED (run-gate_error failure-finding, no verdict) — nobody scanned the
    # dataset, so the train subtree must NOT run. Distinct from the ungated
    # template above, which has neither a verdict nor a gate error.
    assert _fn({"findings": {"run-gate_error": "boom: dataset unreadable"}}) == "false"


def test_gate_error_with_verdict_still_uses_verdict():
    # a verdict that DID compute wins over a stale/extra error finding
    assert _fn({"findings": {"dataset_verdict": "warn", "run-gate_error": "x"}}) == "true"
