"""The ``train.lora`` stage through the BUILTIN engine: a real ``cairn-dataflow-run`` subprocess,
selected by name through core's engine registry (this pack never imports the dataflow pack).
The mock run needs no extra; the real one is ``-m train``. Both need the pack INSTALLED: the
engine's subprocess finds the stage by its ``cairn.dataflow_stages`` entry point."""
from __future__ import annotations

import asyncio
import math
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from cairn.core.dataflow import DataflowSpec, StageRef
from cairn.core.dataflow.conformance import poll_until_terminal
from cairn.core.dataflow.engine_registry import engine_for_name

ROWS = str(Path(__file__).parent / "fixtures" / "tiny.jsonl")

pytestmark = pytest.mark.skipif(
    "train.lora" not in {ep.name for ep in entry_points(group="cairn.dataflow_stages")}
    or engine_for_name("builtin", kind="ml") is None,
    reason="needs cairn-operator-train-local and cairn-operator-dataflow installed (entry points)")


def _run(spec: DataflowSpec, budget_s: float = 60.0):
    async def go():
        engine = engine_for_name("builtin", kind="ml")
        handle = await engine.submit(spec, None)
        status = await poll_until_terminal(engine, handle, budget_s=budget_s)
        assert status.state == "succeeded", status
        return await engine.result(handle)
    return asyncio.run(go())


def test_the_mock_stage_runs_on_the_builtin_engine():
    result = _run(DataflowSpec(stages=[StageRef(stage="train.lora", config={"dataset": ROWS, "mock": True})]))
    (art,) = result.output_artifacts
    assert art.kind == "dataset" and art.uri.endswith("/stage-00"), art  # the runner's placeholder, never a model
    assert result.stats["train.lora.mock"] is True and result.stats["train.lora.cost_usd"] == 0.0
    assert result.stats["train.lora.adapter_path"].startswith("mock://train-local/")
    assert result.stats["train.lora.steps_run"] == 8 and result.stats["train.lora.tokens_seen"] > 0


@pytest.mark.train
def test_the_real_stage_trains_and_the_descriptor_carries_the_adapter(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("peft")
    from _train_local_tiny import build_tiny_model

    model = build_tiny_model(tmp_path / "tiny")
    result = _run(DataflowSpec(stages=[StageRef(stage="train.lora", config={
        "dataset": ROWS, "base_model": model, "steps": 2, "lora_rank": 2, "max_seq_len": 32})]), budget_s=300.0)
    (art,) = result.output_artifacts
    adapter = Path(art.uri.removeprefix("file://"))
    assert art.kind == "model" and (adapter / "adapter_model.safetensors").is_file()
    assert art.content_hash == result.stats["train.lora.content_hash"] and art.size_bytes > 0
    assert math.isfinite(result.stats["train.lora.train_loss"]) and result.stats["train.lora.tokens_seen"] > 0
