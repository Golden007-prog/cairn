#!/usr/bin/env python3
"""Governed hyperparameter sweep with Optuna driving cairn (#145).

cairn does NOT implement a sweep — Optuna (the search) drives `cairn run
training/finetune` per trial, and EACH trial is a governed run: cost-gated (#143),
learning-gated (#144), model-eval-gated (#125), audited + lineage'd (#146), and
HITL-promoted. Optuna does the bayesian/TPE search + pruning it's best at; cairn
governs each run — which Optuna can't.

    pip install optuna           # not a cairn dependency — the driver's
    python optuna_sweep.py       # (point CAIRN at your dataset first)

Each trial samples hyperparameters, writes them into a FinetuneRequest, runs the
governed pipeline, and returns the held-out eval_loss for Optuna to minimize.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import optuna

DATASET = "./data.jsonl"          # your (input,output) pairs
TEMPLATE = "training/finetune"    # the governed pipeline
N_TRIALS = 20


def _run_cairn(request: dict) -> dict:
    """Run one governed cairn finetune and return its structured report."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(request, f)
        in_path = f.name
    # `cairn run <pack>/<template> --input <file>` (direct-template mode).
    out = subprocess.run(
        ["cairn", "run", TEMPLATE, "--input", in_path, "--json"],
        capture_output=True, text=True, check=True,
    )
    Path(in_path).unlink(missing_ok=True)
    # The run's structured FinetuneReport is the last JSON object on stdout.
    return json.loads(out.stdout)


def objective(trial: optuna.Trial) -> float:
    request = {
        "dataset_path": DATASET,
        "output_name": f"sweep-trial-{trial.number}",
        # Optuna samples the search space; these flow through FinetuneRequest →
        # TrainSpec (exposed for exactly this — #145).
        "learning_rate": trial.suggest_float("learning_rate", 1e-5, 5e-4, log=True),
        "lora_r": trial.suggest_categorical("lora_r", [8, 16, 32, 64]),
        "lora_alpha": trial.suggest_categorical("lora_alpha", [16, 32]),
        "epochs": trial.suggest_float("epochs", 1.0, 3.0),
        # Stay cheap while searching; the per-run cost gate (#143) still guards each.
        "max_steps": 200,
    }
    report = _run_cairn(request)
    metrics = report.get("metrics") or {}
    # Minimize held-out eval_loss. A trial blocked by a governance gate (cost /
    # quality) won't have promoted — prune it out of the search.
    eval_loss = metrics.get("eval_loss")
    if eval_loss is None:
        raise optuna.TrialPruned("no eval_loss — trial gated or eval skipped")
    return float(eval_loss)


def main() -> int:
    study = optuna.create_study(direction="minimize", study_name="cairn-finetune")
    study.optimize(objective, n_trials=N_TRIALS)
    print("best params:", study.best_params)
    print("best eval_loss:", study.best_value)
    # The winning config still ships only through cairn's governed promote.
    return 0


if __name__ == "__main__":
    sys.exit(main())
