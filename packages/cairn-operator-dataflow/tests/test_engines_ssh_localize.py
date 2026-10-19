"""#1180 bug 10 — cross-host leg hand-off: local-file inputs ship to the box.

The first PROD pipeline E2E handed the SSH-dispatched train leg a control-side
path (``dataset_uri=/tmp/cairn-dataflow-…/output.jsonl``); submitted verbatim,
the box FileNotFound'd on a path that only exists on the control plane. The
engine now uploads local-file config values into the job workdir (the exec
machinery's ``upload``) and rewrites them workdir-relative.
"""
from __future__ import annotations

import os

from cairn.core.dataflow import DataflowSpec

from operator_dataflow.engines_ssh import BuiltinSshEngine


def _spec(cfg: dict) -> DataflowSpec:
    return DataflowSpec(stages=[{"stage": "train.qlora", "config": cfg}])


def test_local_file_input_is_uploaded_and_rewritten(tmp_path):
    ds = tmp_path / "output.jsonl"
    ds.write_text('{"instruction": "x", "output": "y"}\n')
    spec = _spec({"dataset_uri": str(ds), "base_model": "Qwen/Qwen2.5-0.5B", "max_steps": 5})

    s2, uploads = BuiltinSshEngine._localize_inputs(spec)

    assert uploads == [str(ds)]
    assert s2.stages[0].config["dataset_uri"] == "output.jsonl"  # workdir-relative
    # non-path values pass through untouched
    assert s2.stages[0].config["base_model"] == "Qwen/Qwen2.5-0.5B"
    assert s2.stages[0].config["max_steps"] == 5
    # the ORIGINAL spec is not mutated (poll/cancel rebuild from handle meta)
    assert spec.stages[0].config["dataset_uri"] == str(ds)


def test_non_files_are_never_uploaded(tmp_path):
    spec = _spec({
        "dataset_uri": "https://example.com/data.jsonl",   # remote URI
        "output_name": str(tmp_path / "does-not-exist.bin"),  # abs path, no file
        "note": "relative/path.txt",                        # not absolute
    })
    s2, uploads = BuiltinSshEngine._localize_inputs(spec)
    assert uploads == []
    assert s2.stages[0].config == spec.stages[0].config


def test_same_file_referenced_twice_uploads_once(tmp_path):
    ds = tmp_path / "d.jsonl"
    ds.write_text("{}\n")
    spec = DataflowSpec(stages=[
        {"stage": "train.qlora", "config": {"dataset_uri": str(ds)}},
        {"stage": "eval.checkpoints", "config": {"dataset_uri": str(ds)}},
    ])
    s2, uploads = BuiltinSshEngine._localize_inputs(spec)
    assert uploads == [str(ds)]
    assert [r.config["dataset_uri"] for r in s2.stages] == ["d.jsonl", "d.jsonl"]
