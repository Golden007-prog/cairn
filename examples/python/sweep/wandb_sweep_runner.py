#!/usr/bin/env python3
"""W&B Sweeps agent → one governed cairn finetune trial (#145).

The W&B agent runs this once per trial: W&B injects the sampled hyperparameters via
``wandb.config``; we feed them into a FinetuneRequest, run the GOVERNED
`cairn run training/finetune` (cost/learning/quality gated, audited, HITL-promoted),
then log the trial's eval_loss back so the sweep can minimize it.

cairn governs each run; W&B Sweeps does the search + orchestration + comparison.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import wandb

TEMPLATE = "training/finetune"
DATASET = "./data.jsonl"


def main() -> None:
    run = wandb.init()  # the sweep agent supplies wandb.config
    cfg = dict(run.config)

    request = {
        "dataset_path": DATASET,
        "output_name": f"sweep-{run.id}",
        "learning_rate": cfg.get("learning_rate"),
        "lora_r": cfg.get("lora_r"),
        "lora_alpha": cfg.get("lora_alpha"),
        "epochs": cfg.get("epochs"),
        "max_steps": 200,  # keep search cheap; per-run cost gate (#143) still guards
    }
    request = {k: v for k, v in request.items() if v is not None}

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(request, f)
        in_path = f.name
    out = subprocess.run(
        ["cairn", "run", TEMPLATE, "--input", in_path, "--json"],
        capture_output=True, text=True, check=True,
    )
    Path(in_path).unlink(missing_ok=True)

    report = json.loads(out.stdout)
    metrics = report.get("metrics") or {}
    # Log the objective so W&B Sweeps can compare/minimize. (#136 also has the
    # trainer stream live curves to W&B; this is the final trial objective.)
    if "eval_loss" in metrics:
        wandb.log({"eval_loss": float(metrics["eval_loss"])})


if __name__ == "__main__":
    main()
