"""cairn.core.dataflow.resources — the helpers remote dataflow engines share (ADR-0134 D3b, #2678)."""
from __future__ import annotations

import pytest

from cairn.core.dataflow import DataflowSpec, StageRef, StageResources
from cairn.core.dataflow.resources import aggregate_resources, essential_env, gang_workers


def _spec(*resources):
    if not resources:
        return DataflowSpec(dataflow_ref="pack/empty")
    return DataflowSpec(stages=[StageRef(stage="noop", resources=r) for r in resources])


def test_the_gang_is_the_first_stages_workers_or_one():
    assert gang_workers(_spec()) == 1
    assert gang_workers(_spec(None)) == 1
    assert gang_workers(_spec(StageResources(workers=4), StageResources(workers=9))) == 4


def test_resources_collapse_to_the_heaviest_stage_and_gpu_zero_is_left_out():
    spec = _spec(StageResources(cpu=2, memory_mb=1024, gpu=0), None, StageResources(cpu=8, memory_mb=512))
    assert aggregate_resources(spec) == {"cpu": 8, "memory": 1024}
    assert aggregate_resources(_spec(StageResources(gpu=1)))["gpu"] == 1


def test_the_allowlist_is_engine_neutral_unless_an_engine_adds_its_prefixes():
    neutral = essential_env()
    for k in ("PATH", "CAIRN_DATAFLOW_SPEC", "CAIRN_JOB_CHANNEL", "CAIRN_CONN_DB_URL"):
        assert k in neutral, k
    # no blanket CAIRN_ prefix, no engine's own configuration, no ambient credential
    for k in ("CAIRN_STORAGE", "METAFLOW_DATASTORE_SYSROOT_S3", "AWS_SECRET_ACCESS_KEY"):
        assert k not in neutral, k
    metaflow = essential_env(extra_prefixes=("METAFLOW_",))
    assert "METAFLOW_DATASTORE_SYSROOT_S3" in metaflow and "AWS_SECRET_ACCESS_KEY" not in metaflow


def test_a_bare_string_is_refused_rather_than_split_into_one_letter_prefixes():
    with pytest.raises(TypeError):
        essential_env("METAFLOW_")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        essential_env(("",))       # an empty prefix would admit everything
