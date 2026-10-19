"""A REAL LoRA fine-tune (``-m train``; skipped without the ``[train]`` extra): a one-layer,
eight-wide GPT-2 with random weights and a word-level tokenizer, both built here from the
fixture rows (no network), two optimizer steps on eight rows, and the adapter read back by
``train/eval-loss`` — through the shared implementation and through both operations."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("peft")

from cairn.core.operator.testing import run_operator_for_test  # noqa: E402

from operator_train_local import finetune as F  # noqa: E402
from operator_train_local.operators.eval_loss import EvalLossOp  # noqa: E402
from operator_train_local.operators.lora import TrainLoraOp  # noqa: E402

from _train_local_tiny import ROWS, build_tiny_model  # noqa: E402

pytestmark = pytest.mark.train


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory):
    return build_tiny_model(tmp_path_factory.mktemp("tiny-gpt2"))


def _finite(x) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def test_two_lora_steps_write_an_adapter_and_eval_loss_reads_it_back(tiny_model, tmp_path):
    out = F.finetune(dataset=str(ROWS), base_model=tiny_model, lora_rank=2, steps=2, max_samples=8,
                     learning_rate=1e-3, max_seq_len=32, output_dir=str(tmp_path / "adapter"))
    adapter = Path(out["adapter_path"])
    assert (adapter / "adapter_config.json").is_file() and (adapter / "adapter_model.safetensors").is_file()
    assert out["steps_run"] == 2 and out["tokens_seen"] > 0 and out["base_model"] == tiny_model
    assert _finite(out["train_loss"]) and _finite(out["eval_loss"])  # eight rows: one held out
    back = F.evaluate(adapter_path=str(adapter), base_model=tiny_model, dataset=str(ROWS), max_samples=8,
                      max_seq_len=32)
    assert _finite(back["eval_loss"]) and back["tokens_seen"] > 0


def test_the_operations_train_and_score_for_real(tiny_model, tmp_path):
    sink: list = []
    out = asyncio.run(run_operator_for_test(
        TrainLoraOp, {"dataset": str(ROWS), "steps": 2, "lora_rank": 2, "max_seq_len": 32,
                      "output_dir": str(tmp_path / "a")}, config={"base_model": tiny_model}, report_sink=sink))
    assert out.base_model == tiny_model and out.steps_run == 2 and _finite(out.train_loss)
    steps = [body["step"] for _, body in sink if "loss" in body.get("series", {})]
    assert steps == [0, 1], sink  # the per-step loss series reached the reporter
    score = asyncio.run(run_operator_for_test(
        EvalLossOp, {"adapter_path": out.adapter_path, "base_model": tiny_model, "dataset": str(ROWS),
                     "max_samples": 8, "max_seq_len": 32}))
    assert _finite(score.eval_loss) and score.tokens_seen > 0


def test_the_stage_hashes_only_the_adapter_files_it_wrote(tiny_model, tmp_path):
    from operator_train_local.stages import train_lora

    out_dir = tmp_path / "adapter"
    out_dir.mkdir()
    (out_dir / "stray.bin").write_bytes(b"left by someone else")
    stats = train_lora(tmp_path, {"dataset": str(ROWS), "base_model": tiny_model, "steps": 1, "lora_rank": 2,
                                  "max_seq_len": 32, "output_dir": str(out_dir)}, [])
    mine = sorted(p.name for p in out_dir.iterdir() if p.name != "stray.bin")
    assert "adapter_model.safetensors" in mine and stats["kind"] == "model"
    assert (stats["content_hash"], stats["bytes"]) == F.adapter_digest(str(out_dir), mine)
