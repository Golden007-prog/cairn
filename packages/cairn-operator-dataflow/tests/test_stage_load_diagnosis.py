"""#2069 — a kernel that will not IMPORT must be diagnosable, not invisible.

`load_stages` swallowed every exception from `ep.load()` with a bare
`continue` and no log, so a stage whose module raised on import simply was not
in the registry. The runner then reported

    unknown stages ['train.qlora', ...] (registry has ['noop', 'noop.slow'])

which reads as "you asked for a stage nobody ships" when the truth was "the
stage is installed and its import raised ModuleNotFoundError: boto3". That cost
two failed runs on a billed GPU box on 2026-09-07 (#2086) before anyone thought
to call `ep.load()` by hand.

The fix is not to stop being lenient — one broken pack must still not hide the
rest — it is to REMEMBER why each one was dropped and say so when the omission
starts to matter.
"""
from __future__ import annotations

import pytest

from cairn.core.dataflow import stage_loader as st


class _BrokenEP:
    """An entry point whose module raises on import, like a stage missing a dep."""

    name = "train.qlora"
    group = "cairn.flow_stages"

    def load(self):
        raise ModuleNotFoundError("No module named 'boto3'")


class _GoodEP:
    name = "sample.fidelity"
    group = "cairn.flow_stages"

    def load(self):
        return lambda workdir, config, inputs: {"rows": 1}


def _patch_eps(monkeypatch, eps):
    monkeypatch.setattr(st, "entry_points",
                        lambda group: [e for e in eps if e.group == group])


def test_a_broken_stage_is_recorded_with_its_reason(monkeypatch):
    _patch_eps(monkeypatch, [_BrokenEP(), _GoodEP()])
    st.load_stages.cache_clear() if hasattr(st.load_stages, "cache_clear") else None

    registry = st.load_stages()
    # leniency is preserved: the good stage still loads
    assert "sample.fidelity" in registry
    assert "train.qlora" not in registry

    failures = st.stage_load_failures()
    assert "train.qlora" in failures, "a stage that failed to import must be recorded"
    assert "boto3" in failures["train.qlora"], failures["train.qlora"]
    assert "ModuleNotFoundError" in failures["train.qlora"]


def test_a_broken_stage_is_logged_not_silent(monkeypatch, caplog):
    _patch_eps(monkeypatch, [_BrokenEP()])
    with caplog.at_level("WARNING"):
        st.load_stages()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "train.qlora" in joined and "boto3" in joined, (
        f"the drop must be logged with the stage and the reason; got: {joined!r}")


def test_the_runner_names_the_import_failure_instead_of_unknown(monkeypatch, tmp_path):
    """The message a human actually reads when the run dies."""
    from cairn.core.dataflow import DataflowSpec
    from cairn.core.dataflow.contract import StageRef
    from cairn.core.dataflow import runner as rn

    monkeypatch.setattr(rn, "load_stages", lambda: {"noop": lambda *a: {}})
    monkeypatch.setattr(rn, "stage_load_failures",
                        lambda: {"train.qlora": "ModuleNotFoundError: No module named 'boto3'"})

    spec = DataflowSpec(stages=[StageRef(stage="train.qlora")])
    with pytest.raises(ValueError) as e:
        rn.run_dataflow(spec, tmp_path / "wd")
    msg = str(e.value)
    assert "train.qlora" in msg
    assert "boto3" in msg, f"the run must name the import failure, not just 'unknown': {msg}"
    assert "is installed but failed to import" in msg
