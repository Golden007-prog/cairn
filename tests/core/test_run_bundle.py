"""The run bundle — one download that makes a run reproducible elsewhere (#2032).

ADR-0102 D5: "the bundle is what makes a run reproducible somewhere else". Five
things travel: the effective config, the metrics, the eval results, the lineage
and the model card. The acceptance criterion that matters is the second one
here — **the config in the bundle equals the run's effective spec** — because a
bundle whose config is a re-derivation rather than the recorded one is a bundle
that reproduces something else.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest
import yaml

from cairn.core.run_bundle import (
    BUNDLE_ITEMS,
    RunBundleSources,
    build_run_bundle,
    metrics_rows,
)


def _events():
    """A training run's events: the parameters record (T4) and a curve (T6)."""
    return [
        {"type": "parameters", "data": {
            "operator": "train/qlora", "node": "train",
            "inputs": {"base_model": "Qwen/Qwen2.5-3B", "learning_rate": 0.0002,
                       "lora_r": 16, "max_steps": 60, "packing": False},
            "config": {"budget_usd": 5.0},
        }},
        {"type": "metrics", "data": {"node": "train", "step": 1,
                                     "series": {"loss": 1.5, "tokens_per_s": 900.0}}},
        {"type": "metrics", "data": {"node": "train", "step": 2,
                                     "series": {"loss": 1.1}}},
        {"type": "scalars", "data": {"node": "train", "values": {"final_loss": 0.9}}},
        {"type": "log", "data": {"line": "not a report event"}},
    ]


def _sources(**kw) -> RunBundleSources:
    base = dict(
        run_id="run-1",
        events=_events(),
        eval_runs=[{"eval_run_id": "eval-1", "verdict": "pass", "aggregate_score": 0.91,
                    "recipe": "sql-suite", "tier": "gold"}],
        lineage={"run": {"id": "run-1"}, "ancestors": [], "children": [],
                 "record": {"dataset": {"hash": "sha256:abc"}}},
        model_card="---\nbase_model: \"Qwen/Qwen2.5-3B\"\n---\n\n# Model card: sqlbot\n",
    )
    base.update(kw)
    return RunBundleSources(**base)


def _unzip(blob: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


# ─── the five items ───────────────────────────────────────────────────────


def test_the_bundle_unzips_to_the_five_items():
    files = _unzip(build_run_bundle(_sources()))
    # the five the ticket names — metrics is one ITEM in two formats
    assert set(BUNDLE_ITEMS) == {"config", "metrics", "eval", "lineage", "model_card"}
    assert set(files) == {
        "config.yaml", "metrics.csv", "metrics.json", "eval.json",
        "lineage.json", "MODEL_CARD.md", "MANIFEST.json",
    }


def test_the_config_equals_the_runs_effective_spec():
    """ACCEPTANCE. Byte-for-byte the `inputs` of the run's `parameters` event."""
    src = _sources()
    files = _unzip(build_run_bundle(src))
    got = yaml.safe_load(files["config.yaml"])
    assert got == src.events[0]["data"]["inputs"]
    # the governance-scoped config the run resolved travels too, clearly separated
    # from the per-run inputs so nobody replays one as the other.
    assert "learning_rate: 0.0002" in files["config.yaml"]


def test_the_manifest_says_what_is_present_and_why_anything_is_missing():
    files = _unzip(build_run_bundle(_sources(model_card="", eval_runs=[])))
    m = json.loads(files["MANIFEST.json"])
    assert m["run_id"] == "run-1"
    assert m["items"]["config"]["present"] is True
    assert m["items"]["model_card"]["present"] is False
    assert m["items"]["model_card"]["why"]
    assert m["items"]["eval"]["present"] is False
    # a missing item is still a file, so the bundle's shape never varies
    assert files["MODEL_CARD.md"].startswith("<!-- ")
    assert json.loads(files["eval.json"]) == []


# ─── metrics ──────────────────────────────────────────────────────────────


def test_metrics_csv_is_one_row_per_step_with_a_column_per_series():
    rows = list(csv.DictReader(io.StringIO(_unzip(build_run_bundle(_sources()))["metrics.csv"])))
    assert [r["step"] for r in rows] == ["1", "2"]
    assert rows[0]["loss"] == "1.5" and rows[0]["tokens_per_s"] == "900.0"
    # a series absent at a step is EMPTY, not zero — a fabricated point in a
    # reproducibility bundle is worse than a hole (ADR-0102 D2).
    assert rows[1]["tokens_per_s"] == ""


def test_metrics_json_keeps_the_series_and_the_end_scalars():
    m = json.loads(_unzip(build_run_bundle(_sources()))["metrics.json"])
    assert m["series"]["loss"] == [[1, 1.5], [2, 1.1]]
    assert m["scalars"] == {"final_loss": 0.9}
    assert m["steps"] == 2


def test_metrics_rows_ignores_events_that_are_not_reports():
    rows, names = metrics_rows(_events())
    assert names == ["loss", "tokens_per_s"]
    assert len(rows) == 2


def test_a_run_with_no_report_events_gets_an_empty_metrics_file_not_a_crash():
    files = _unzip(build_run_bundle(_sources(events=[])))
    assert files["metrics.csv"].strip() == "step,node"
    assert json.loads(files["metrics.json"])["series"] == {}
    m = json.loads(files["MANIFEST.json"])
    assert m["items"]["metrics"]["present"] is False
    assert "no `metrics`" in m["items"]["metrics"]["why"]


def test_the_bundle_is_deterministic():
    """Same sources ⇒ same bytes: a reproducibility artifact that changes on
    every download cannot be checksummed or diffed."""
    assert build_run_bundle(_sources()) == build_run_bundle(_sources())


def test_a_run_id_with_a_path_separator_cannot_escape_the_zip():
    with pytest.raises(ValueError, match="run id"):
        build_run_bundle(_sources(run_id="../../etc/passwd"))
