"""Mid-train learning verdict (ADR-0039 Gate 2) — "is the model learning?".

A pure, deterministic read of the eval-loss trajectory → learning / stalled /
diverging. The remote-job control worker runs this on each poll's mid-train
metrics and ABORTS a non-learning run (ADR-0038 L2), so a GPU isn't paid for a
run that isn't converging. Mirrors HF ``EarlyStoppingCallback`` (best-so-far +
patience + min_delta, with a warmup) — no I/O, no LLM, fully testable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LearningConfig:
    """Thresholds for the learning verdict (deployment-tunable)."""

    min_delta: float = 0.0  # an eval_loss drop must exceed this to count as improvement
    patience: int = 3       # consecutive evals without improvement before "not learning"
    warmup: int = 2         # ignore the first N evals (give the run time to start)


@dataclass(frozen=True)
class LearningVerdict:
    verdict: str  # "learning" | "stalled" | "diverging"
    reason: str

    @property
    def learning(self) -> bool:
        return self.verdict == "learning"


def learning_verdict(losses: list, cfg: LearningConfig | None = None) -> LearningVerdict:
    """Judge an eval-loss trajectory. ``stalled`` = no improvement beyond
    ``min_delta`` for ``patience`` evals; ``diverging`` = additionally rising.
    Both mean "not learning" → the worker aborts. During ``warmup`` it's always
    ``learning`` (too early to judge)."""
    cfg = cfg or LearningConfig()
    pts = [float(x) for x in losses if x is not None]

    if len(pts) <= cfg.warmup:
        return LearningVerdict("learning", f"warmup ({len(pts)} ≤ {cfg.warmup} evals) — too early to judge")

    # Walk the trajectory counting evals since the last improvement BEYOND
    # min_delta (HF EarlyStopping semantics) — tiny wiggles don't reset patience.
    best = pts[0]
    since_improve = 0
    for x in pts[1:]:
        if x < best - cfg.min_delta:
            best, since_improve = x, 0
        else:
            since_improve += 1

    if since_improve < cfg.patience:
        return LearningVerdict("learning", f"improving — best eval_loss {best:.4f} ({since_improve} eval(s) since)")

    last = pts[-1]
    if last >= pts[-2] and last > best + max(cfg.min_delta, 0.0):
        return LearningVerdict(
            "diverging", f"eval_loss rising ({last:.4f} > best {best:.4f}) for {since_improve} evals"
        )
    return LearningVerdict(
        "stalled", f"no eval_loss improvement (>{cfg.min_delta}) in {since_improve} evals; best {best:.4f}"
    )


__all__ = ["LearningConfig", "LearningVerdict", "learning_verdict"]
