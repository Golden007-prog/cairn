"""#1322 — a signal death is only `cancelled` when cancel REQUESTED it.

The old blanket rule (`rc < 0 or rc in (137, 143)` → cancelled) reported a
kernel OOM-kill as a clean cancel: a 45-min train died with no error and no
result.json. cancel() marks the workdir before killing, so the marker is the
intent signal — killed WITHOUT it = failed, with a likely-OOM post-mortem.
"""
from __future__ import annotations

import asyncio
import json

import pytest

metaflow = pytest.importorskip("metaflow", reason="optional [metaflow] extra")

from operator_dataflow_metaflow.engines_metaflow import _PROCS, MetaflowEngine  # noqa: E402
from cairn.core.dataflow import EngineJobHandle  # noqa: E402


class _DeadProc:
    def __init__(self, rc: int) -> None:
        self.returncode = rc


def _poll(tmp_path, rc: int, *, cancelled: bool):
    if cancelled:
        (tmp_path / "progress.json").write_text(
            json.dumps({"state": "cancelled"}), encoding="utf-8")
    handle = EngineJobHandle(job_id="j-sig", engine="metaflow",
                             meta={"workdir": str(tmp_path)})
    _PROCS["j-sig"] = {"proc": _DeadProc(rc), "workdir": str(tmp_path)}
    try:
        return asyncio.run(MetaflowEngine().poll(handle))
    finally:
        _PROCS.pop("j-sig", None)


def test_sigkill_without_cancel_request_is_failed_likely_oom(tmp_path):
    status = _poll(tmp_path, -9, cancelled=False)
    assert status.state == "failed"
    assert "signal 9" in status.error and "OOM" in status.error


def test_rc137_without_cancel_request_is_failed(tmp_path):
    status = _poll(tmp_path, 137, cancelled=False)
    assert status.state == "failed"
    assert "signal 9" in status.error


def test_signal_death_after_cancel_request_stays_cancelled(tmp_path):
    """A REQUESTED kill must not regress to failed — cancel still looks clean."""
    status = _poll(tmp_path, -15, cancelled=True)
    assert status.state == "cancelled"
