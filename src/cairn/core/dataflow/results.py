"""The job-workdir result contract — core since #2583 (ADR-0129 D1).

Every transport of the kernel runner ends the same way: ``cairn-dataflow-run``
(or a bridge calling :func:`cairn.core.dataflow.runner.run_dataflow` in-process)
leaves ``metrics.json`` (progressive, what a poller reads) and ``result.json``
(terminal: a ``DataflowResult`` payload, or ``{"error": …}``) in the job
workdir. These readers are the ONE spelling of that file contract, so an engine
adapter outside the builtin dataflow pack reads the same files the same way
without importing that pack. They were ``BuiltinLocalEngine``'s static methods
until #2583; the pack's engine still delegates here.

:func:`validate` is the capability-discovery half every registry-backed engine
shares: an unknown stage is caught control-side, before a job is submitted or a
machine provisioned (ADR-0081 D3).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from cairn.core.dataflow.contract import DataflowSpec
from cairn.core.dataflow.stage_loader import load_stages


def validate(spec: DataflowSpec) -> list[str]:
    """What the shared kernel registry cannot run: the problems an engine that
    resolves kernels through :func:`load_stages` reports from ``validate()``."""
    registry = load_stages()
    problems = [
        f"unknown stage {ref.stage!r} (registry has {sorted(registry)})"
        for ref in spec.stages
        if ref.stage not in registry
    ]
    if spec.dataflow_ref and not spec.stages:
        problems.append(
            f"dataflow_ref {spec.dataflow_ref!r} not resolvable yet — pack-first-class "
            "dataflows land in P2a (#1051); pass inline stages"
        )
    return problems


def read_metrics(workdir: Path) -> dict:
    """The runner's progressive ``metrics.json``; ``{}`` while it is absent or
    torn (a torn read by a poller just means "not yet")."""
    try:
        return json.loads((workdir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_result(workdir: Path) -> Optional[dict]:
    """The terminal ``result.json`` payload, or ``None`` when there is none yet."""
    try:
        return json.loads((workdir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_error(workdir: Path) -> str:
    """The failure ``result.json`` names, or ``""`` (no result, or a success)."""
    payload = read_result(workdir)
    return str(payload.get("error", "")) if payload else ""
