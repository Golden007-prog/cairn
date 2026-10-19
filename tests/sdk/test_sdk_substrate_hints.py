"""#1350 (ADR-0089) — SDK resource hints must actually reach the substrate.

`flow.ml(...)` compiled to a stage list with no way to say "this training stage
needs a GPU and 32GB", so an SDK-authored ml flow could not drive the substrate
#1345/#1346 wired. `StageResources` was already on the contract — the SDK
simply had no surface for it.

The test that matters is the LAST one: author intent → record → engine argv, so
a break anywhere along that chain fails here rather than silently sizing a
training task at the default.

Retry deliberately has NO SDK surface: it is environment-level
(`metaflow_retry_times`, #1345). Per ADR-0081, a field must never enter the
engine contract because one engine wants it — engine knobs live in the
environment backend.
"""
from __future__ import annotations

import yaml

from cairn_sdk.builders import flow
from cairn_sdk.compiler import _emit_flow, _stage_resources


def _doc(fb) -> dict:
    return yaml.safe_load(_emit_flow(fb, []))


# ── the SDK surface ──────────────────────────────────────────────────────────

def test_a_stage_can_declare_resources():
    fb = flow.ml("train").stage("train.qlora", resources={"cpu": 8, "memory_mb": 32000, "gpu": 1})
    assert _doc(fb)["stages"][0]["resources"] == {"cpu": 8, "memory_mb": 32000, "gpu": 1}


def test_resources_are_not_confused_with_stage_config():
    """Config keys are arbitrary — a kernel may legitimately have its own `cpu`
    field, which is why resources is a dedicated kwarg."""
    fb = flow.ml("t").stage("x.y", resources={"cpu": 8}, cpu="a-config-value")
    stage = _doc(fb)["stages"][0]
    assert stage["resources"] == {"cpu": 8}
    assert stage["config"]["cpu"] == "a-config-value"


def test_a_stage_without_resources_is_unchanged():
    """Existing SDK pipelines must compile byte-identically."""
    assert "resources" not in _doc(flow.ml("t").stage("x.y"))["stages"][0]


def test_checkpoint_and_resources_coexist():
    fb = flow.ml("t").stage("train.qlora", checkpoint=True, resources={"gpu": 1})
    stage = _doc(fb)["stages"][0]
    assert stage["checkpoint"] is True and stage["resources"] == {"gpu": 1}


def test_both_flow_kinds_carry_resources():
    for maker in (flow.data, flow.ml):
        assert _doc(maker("f").stage("s", resources={"cpu": 2}))["stages"][0]["resources"]


# ── build-time validation: a typo must not reach submit ──────────────────────

def test_an_unknown_axis_is_dropped_with_a_note():
    """`memory=` (not `memory_mb=`) would be rejected by StageResources'
    extra='forbid' at SUBMIT — hours after launch. Catch it at build."""
    notes: list[str] = []
    assert _stage_resources({"memory": 4096}, "train.qlora", notes) == {}
    assert any("memory" in n and "DROPPED" in n for n in notes)


def test_a_non_integer_value_is_dropped_with_a_note():
    notes: list[str] = []
    assert _stage_resources({"cpu": "lots"}, "s", notes) == {}
    assert any("not an\ninteger" in n or "not an integer" in n for n in notes)


def test_numeric_strings_are_coerced_not_rejected():
    assert _stage_resources({"cpu": "8"}, "s", []) == {"cpu": 8}


def test_valid_axes_survive_alongside_an_invalid_one():
    notes: list[str] = []
    assert _stage_resources({"cpu": 4, "bogus": 1}, "s", notes) == {"cpu": 4}


# ── the whole chain: SDK author intent → engine argv ─────────────────────────

def test_sdk_hints_reach_the_metaflow_command_line():
    """The load-bearing test. Author writes resources in Python; the Metaflow
    engine must end up passing `--with resources:...`. Breaks if ANY link
    (SDK kwarg, emission, contract parse, aggregation, argv) regresses."""
    from cairn.core.dataflow import DataflowSpec
    # the Metaflow engine is its own optional wheel (ADR-0134 D2): cairndev[metaflow]
    import pytest  # noqa: PLC0415

    build_with_args = pytest.importorskip("operator_dataflow_metaflow.engines_metaflow").build_with_args

    fb = (flow.ml("train")
          .stage("prep.tokenize", resources={"cpu": 2, "memory_mb": 4096})
          .stage("train.qlora", resources={"cpu": 8, "memory_mb": 32000, "gpu": 1}))

    # the compiled record parses as the engine contract...
    spec = DataflowSpec.model_validate({"stages": _doc(fb)["stages"]})
    # ...and the envelope is the max across stages (one step fits the heaviest)
    assert build_with_args(spec, {"kind": "local"}, 0) == [
        "--with", "resources:cpu=8,gpu=1,memory=32000",
    ]
