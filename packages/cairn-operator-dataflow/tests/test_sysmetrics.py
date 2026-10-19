"""System-metrics sampler (#1465) — hermetic tier: probes stubbed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.core.dataflow import sysmetrics


def _fake_probes():
    seq = iter(range(1, 100))

    def cpu():
        n = next(seq)
        return {"cpu_pct": 10.0 * n, "rss_mb": 100.0 + n}
    return cpu, None


def test_gating(monkeypatch):
    monkeypatch.delenv("CAIRN_SYSMETRICS", raising=False)
    assert sysmetrics.should_sample({"gpu": 1}) is True
    assert sysmetrics.should_sample({"gpu": 0}) is False
    assert sysmetrics.should_sample(None) is False
    monkeypatch.setenv("CAIRN_SYSMETRICS", "on")
    assert sysmetrics.should_sample(None) is True
    monkeypatch.setenv("CAIRN_SYSMETRICS", "off")
    assert sysmetrics.should_sample({"gpu": 1}) is False


def test_series_and_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CAIRN_SYSMETRICS", "on")
    monkeypatch.setenv("CAIRN_SYSMETRICS_INTERVAL_S", "0.01")
    monkeypatch.setattr(sysmetrics, "_probes", _fake_probes)
    s = sysmetrics.start(tmp_path, 3, "train.qlora", None)
    assert s is not None
    import time
    time.sleep(0.06)
    out = s.stop()
    assert out["sysmetrics_samples"] >= 2
    assert out["cpu_pct_max"] >= out["cpu_pct_mean"] > 0
    series = tmp_path / "sysmetrics-03-train.qlora.jsonl"
    rows = [json.loads(l) for l in series.read_text().splitlines()]
    assert len(rows) == out["sysmetrics_samples"]
    assert all("cpu_pct" in r and "t" in r for r in rows)


def test_no_probes_no_sampler(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CAIRN_SYSMETRICS", "on")
    monkeypatch.setattr(sysmetrics, "_probes", lambda: (None, None))
    assert sysmetrics.start(tmp_path, 0, "x", None) is None


def test_runner_gpu_stage_gets_sysmetrics(tmp_path: Path, monkeypatch):
    from cairn.core.dataflow import DataflowSpec
    from cairn.core.dataflow import runner

    monkeypatch.setenv("CAIRN_SYSMETRICS_INTERVAL_S", "0.01")
    monkeypatch.delenv("CAIRN_SYSMETRICS", raising=False)
    monkeypatch.setattr(sysmetrics, "_probes", _fake_probes)

    def slow_stage(workdir: Path, config: dict, inputs: list[str]) -> dict:
        import time
        time.sleep(0.05)
        return {"ok": 1}

    monkeypatch.setattr(runner, "load_stages", lambda: {"train.fake": slow_stage})
    spec = DataflowSpec.model_validate(
        {"stages": [{"stage": "train.fake", "config": {}, "resources": {"gpu": 1}}]})
    result = runner.run_dataflow(spec, tmp_path / "wd")
    assert result.stats["train.fake.ok"] == 1
    assert result.stats["train.fake.sysmetrics_samples"] >= 1
    assert "train.fake.cpu_pct_mean" in result.stats
    assert (tmp_path / "wd" / "sysmetrics-00-train.fake.jsonl").exists()
