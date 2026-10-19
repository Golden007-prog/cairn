"""monitor/threshold + monitor/propose — the data-shaped-trigger detector (#992)."""
from __future__ import annotations

import asyncio
import json

import pytest

from operator_monitor_threshold.operators.propose import ProposeOp
from operator_monitor_threshold.operators.threshold import ThresholdOp, _dig
from operator_monitor_threshold.schemas.propose import ProposeInput
from operator_monitor_threshold.schemas.threshold import ThresholdInput


class _Ctx:
    pass


def _run(op, inp):
    return asyncio.run(op.run(inp, _Ctx()))


@pytest.mark.parametrize("cmp,value,thr,crossed", [
    ("gt", 0.31, 0.2, True), ("gt", 0.1, 0.2, False),
    ("gte", 0.2, 0.2, True), ("lt", 0.1, 0.2, True), ("lte", 0.3, 0.2, False),
])
def test_threshold_comparators(cmp, value, thr, crossed):
    o = _run(ThresholdOp(), ThresholdInput(metric="m", threshold=thr, comparator=cmp, value=value))
    assert o.crossed is crossed and o.value == value and o.source == "inline"


def test_threshold_reads_from_json_file(tmp_path, monkeypatch):
    # #1657 — a watch reads only under this deployment's allowed roots, so the
    # test declares where it reads from exactly as a real deployment does.
    monkeypatch.setenv("OBS_WATCH_FILE_ROOTS", str(tmp_path))
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"metrics": {"psi": 0.44}}))
    o = _run(ThresholdOp(), ThresholdInput(
        metric="psi", threshold=0.2, comparator="gt", source_path=str(p), source_key="metrics.psi"))
    assert o.crossed is True and o.value == 0.44 and o.source == "file"


def test_threshold_refuses_a_file_outside_the_allowed_roots(tmp_path, monkeypatch):
    """#1657 — the guard is reached through the OPERATOR, not only in isolation:
    the console's New-watch form is what supplies this path."""
    monkeypatch.setenv("OBS_WATCH_FILE_ROOTS", str(tmp_path / "allowed"))
    (tmp_path / "allowed").mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"metrics": {"psi": 0.9}}))
    with pytest.raises(ValueError, match="outside this deployment's allowed roots"):
        _run(ThresholdOp(), ThresholdInput(
            metric="psi", threshold=0.2, comparator="gt",
            source_path=str(outside), source_key="metrics.psi"))


def test_threshold_refuses_a_file_url(monkeypatch):
    """urllib opens `file://`, so without a scheme check a URL source reads
    local files and walks around the path guard entirely."""
    monkeypatch.delenv("OBS_WATCH_ALLOW_PRIVATE_URLS", raising=False)
    with pytest.raises(ValueError, match="must be http"):
        _run(ThresholdOp(), ThresholdInput(
            metric="psi", threshold=0.2, comparator="gt",
            source_url="file:///etc/passwd", source_key="v"))


def test_threshold_requires_a_source():
    with pytest.raises(ValueError):
        _run(ThresholdOp(), ThresholdInput(metric="m", threshold=0.2))


def test_threshold_rejects_bad_comparator():
    with pytest.raises(ValueError):
        _run(ThresholdOp(), ThresholdInput(metric="m", threshold=0.2, comparator="between", value=1))


def test_dig_nested_key_and_missing():
    assert _dig({"a": {"b": 3}}, "a.b") == 3
    with pytest.raises(KeyError):
        _dig({"a": {}}, "a.b")


def test_threshold_mock_is_deterministically_crossed():
    o = ThresholdOp().mock(ThresholdInput(metric="m", threshold=0.2, comparator="gt"), _Ctx())
    assert o.crossed is True and o.source == "mock"


def test_propose_records_approved_proposal():
    # reaching run() means the approval gate passed (requires_approval fired first).
    o = _run(ProposeOp(), ProposeInput(
        metric="psi", value=0.31, threshold=0.2, proposed_workflow="training/finetune", est_cost_usd=80))
    assert o.approved is True and o.proposed_workflow == "training/finetune" and o.est_cost_usd == 80
