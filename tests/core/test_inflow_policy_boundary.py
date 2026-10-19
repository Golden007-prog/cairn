"""ADR-0089 R2 (#1347) — the in-flow policy boundary, and the guard on it.

**The ruling.** cairn governs an engine-run flow at ADMISSION — plan-time
structural gates on the aggregated stage risk, plus the door's own risk — and
the engine then executes the admitted stage list with no policy engine inside
it. That is correct while the risk vocabulary is "does this mutate?": policy
effects are admission decisions (deny > require_hitl > require_dry_run >
allow_auto), no obligation primitive exists (#700 is open), and no shipped
kernel calls an LLM or egresses.

**The guard.** The boundary erodes the moment a kernel needs a class the
vocabulary cannot express. `stage_risk_class` coerces the unknown to
`external_write` — fail-closed, and correct — but coercing SILENTLY is how that
day passes unnoticed. It now warns, naming the ruling.
"""
from __future__ import annotations

import logging

from cairn.core.dataflow.defs import (
    RISK_EXTERNAL_WRITE,
    RISK_READ_ONLY,
    aggregate_risk,
    stage_risk_class,
)
from cairn.core.dataflow import StageRef


class _Kernel:
    def __init__(self, risk):
        if risk is not None:
            self.risk_class = risk


class _EP:
    def __init__(self, fn):
        self._fn = fn

    def load(self):
        return self._fn


def _registry(monkeypatch, mapping):
    import cairn.core.dataflow.defs as defs
    monkeypatch.setattr(defs, "_stage_entry_points",
                        lambda: {k: _EP(v) for k, v in mapping.items()})


# ── the admission vocabulary still behaves ───────────────────────────────────

def test_declared_classes_pass_through(monkeypatch):
    _registry(monkeypatch, {"scan": _Kernel("read_only"),
                            "write": _Kernel("external_write")})
    assert stage_risk_class("scan") == RISK_READ_ONLY
    assert stage_risk_class("write") == RISK_EXTERNAL_WRITE


def test_undeclared_is_fail_closed(monkeypatch):
    """#744 — an undeclared kernel is treated as mutating."""
    _registry(monkeypatch, {"mystery": _Kernel(None)})
    assert stage_risk_class("mystery") == RISK_EXTERNAL_WRITE


def test_unregistered_is_fail_closed(monkeypatch):
    _registry(monkeypatch, {})
    assert stage_risk_class("nope") == RISK_EXTERNAL_WRITE


def test_a_flow_is_as_risky_as_its_riskiest_stage(monkeypatch):
    _registry(monkeypatch, {"scan": _Kernel("read_only"),
                            "write": _Kernel("external_write")})
    assert aggregate_risk([StageRef(stage="scan")]) == RISK_READ_ONLY
    assert aggregate_risk([StageRef(stage="scan"),
                           StageRef(stage="write")]) == RISK_EXTERNAL_WRITE


# ── the guard: an unmodelled class must not coerce silently ──────────────────

def test_an_unknown_risk_class_still_fails_closed(monkeypatch):
    """Admission safety is unchanged — the guard adds visibility, not leniency."""
    _registry(monkeypatch, {"llm_stage": _Kernel("llm_call")})
    assert stage_risk_class("llm_stage") == RISK_EXTERNAL_WRITE


def test_an_unknown_risk_class_warns_and_names_the_ruling(monkeypatch, caplog):
    """The day a kernel calls an LLM or egresses, R2 must be reopened — this
    warning is the only thing that says so at the moment it happens."""
    _registry(monkeypatch, {"llm_stage": _Kernel("llm_call")})
    with caplog.at_level(logging.WARNING, logger="cairn.core.dataflow.defs"):
        stage_risk_class("llm_stage")
    msg = caplog.text
    assert "llm_call" in msg          # the offending class is named
    assert "ADR-0089" in msg          # where the boundary is written down
    assert "admission" in msg.lower()  # what the boundary actually is


def test_the_modelled_classes_do_not_warn(monkeypatch, caplog):
    """A warning on every ordinary kernel would be noise nobody reads."""
    _registry(monkeypatch, {"scan": _Kernel("read_only"),
                            "write": _Kernel("external_write"),
                            "undeclared": _Kernel(None)})
    with caplog.at_level(logging.WARNING, logger="cairn.core.dataflow.defs"):
        stage_risk_class("scan")
        stage_risk_class("write")
        stage_risk_class("undeclared")
    assert caplog.text == ""
