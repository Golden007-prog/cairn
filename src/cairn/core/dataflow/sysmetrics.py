"""System-metrics sampling per stage (#1465) — the MLflow ``log_system_metrics``
analogue, engine-enforced.

A background thread samples cpu / rss / gpu-util / vram every few seconds
while a stage runs. The full series is written to the workdir as
``sysmetrics-<NN>-<stage>.jsonl`` — which #1465's capture then mirrors to the
artifact store like any other stage product — and min/max/mean summaries merge
into the stage's stats as flat scalars (D3).

Twelve GPU trainings ran in the C2 campaign (2026-08-19/20) with zero
utilization capture — nobody can say whether the T4 was 30% or 95% busy.

Gating (owner ruling: platform-wide, ML-default): samples when the stage
declares ``resources.gpu >= 1`` (the ML legs) or ``CAIRN_SYSMETRICS=on``
forces it for every stage; ``CAIRN_SYSMETRICS=off`` disables. psutil and
pynvml are OPTIONAL — lazy imports (the runner stays stdlib-only, D5); with
neither present the sampler no-ops. Best-effort throughout: sampling must
never fail the stage it observes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

_log = logging.getLogger("cairn.dataflow.sysmetrics")

_DEFAULT_INTERVAL_S = 5.0


def should_sample(resources: dict | None) -> bool:
    mode = os.environ.get("CAIRN_SYSMETRICS", "").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return False
    if mode in ("on", "1", "true", "yes"):
        return True
    try:
        return float((resources or {}).get("gpu") or 0) >= 1
    except (TypeError, ValueError):
        return False


def _probes():
    """(cpu_probe, gpu_probe) — each ``None`` when its optional dep is absent.
    Resolved once per sampler so a missing dep costs one failed import."""
    cpu = gpu = None
    try:
        import psutil  # noqa: PLC0415 - optional dependency: not in the base install

        proc = psutil.Process()

        def cpu_probe() -> dict:
            return {
                "cpu_pct": psutil.cpu_percent(interval=None),
                "rss_mb": round(proc.memory_info().rss / 2**20, 1),
                "ram_avail_mb": round(psutil.virtual_memory().available / 2**20, 1),
            }
        cpu = cpu_probe
    except Exception:  # noqa: BLE001 — optional, never fatal
        pass
    try:
        import pynvml  # noqa: PLC0415 - optional dependency: NVIDIA boxes only

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)

        def gpu_probe() -> dict:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return {"gpu_pct": float(util.gpu),
                    "vram_mb": round(mem.used / 2**20, 1)}
        gpu = gpu_probe
    except Exception:  # noqa: BLE001 — optional, never fatal
        pass
    return cpu, gpu


class Sampler:
    """Background sampler for one stage. ``stop()`` writes the series file and
    returns flat summary scalars (or ``{}`` when nothing was sampled)."""

    def __init__(self, workdir: Path, index: int, stage: str, interval_s: float) -> None:
        self._path = workdir / f"sysmetrics-{index:02d}-{stage}.jsonl"
        self._interval = interval_s
        self._stop = threading.Event()
        self._rows: list[dict] = []
        self._cpu, self._gpu = _probes()
        self._thread: threading.Thread | None = None
        if self._cpu or self._gpu:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name=f"sysmetrics-{stage}")
            self._thread.start()

    def _sample(self) -> None:
        row: dict = {"t": round(time.time(), 1)}
        if self._cpu:
            row.update(self._cpu())
        if self._gpu:
            row.update(self._gpu())
        self._rows.append(row)

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._interval):
                self._sample()
        except Exception:  # noqa: BLE001 — observation must never hurt the observed
            _log.warning("sysmetrics: sampler died", exc_info=True)

    def stop(self) -> dict:
        if self._thread is None:
            return {}
        self._stop.set()
        self._thread.join(timeout=self._interval + 1)
        try:
            self._sample()  # final reading so even sub-interval stages get one row
        except Exception:  # noqa: BLE001
            pass
        if not self._rows:
            return {}
        try:
            with self._path.open("w", encoding="utf-8") as fh:
                for r in self._rows:
                    fh.write(json.dumps(r) + "\n")
        except OSError:
            _log.warning("sysmetrics: series write failed", exc_info=True)
        out: dict = {"sysmetrics_samples": len(self._rows)}
        for key in ("cpu_pct", "rss_mb", "gpu_pct", "vram_mb"):
            vals = [r[key] for r in self._rows if key in r]
            if vals:
                out[f"{key}_mean"] = round(sum(vals) / len(vals), 1)
                out[f"{key}_max"] = round(max(vals), 1)
        return out


def start(workdir: Path, index: int, stage: str, resources: dict | None) -> Sampler | None:
    """A running :class:`Sampler` for the stage, or ``None`` when gating or
    missing deps say no. Never raises."""
    try:
        if not should_sample(resources):
            return None
        interval = float(os.environ.get("CAIRN_SYSMETRICS_INTERVAL_S", _DEFAULT_INTERVAL_S))
        s = Sampler(workdir, index, stage, interval)
        return s if s._thread is not None else None
    except Exception:  # noqa: BLE001 — observation must never hurt the observed
        _log.warning("sysmetrics: start failed for %s", stage, exc_info=True)
        return None
