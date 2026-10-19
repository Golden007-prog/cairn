"""The SDK's typed run surface (#1255, ADR-0087 P3) — file 5 of the vocabulary.

A thin bridge, deliberately: every HTTP call delegates to ``cairn_client``
(the ONE typed API client, #1194) — the SDK adds only the authoring-side
ergonomics: launch a COMPILED class by ref **pinned to its build hash** (D6 —
the server 409s if the installed record drifted from the class you hold),
then watch, approve, and wait with pipeline-shaped helpers.

Nothing here executes a pipeline locally. ``run()`` submits the installed
record; the local class is a pointer with a checksum.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from cairn_client import CairnAPIError, CairnClient

from .compiler import CompiledPack

#: Pipeline statuses that end a wait().
_TERMINAL = ("done", "failed", "cancelled")


class PipelineDriftError(RuntimeError):
    """The installed record differs from the compiled class (server 409, D6)."""


class GateHandle:
    """One awaiting approval gate — approve/deny from code or CI."""

    def __init__(self, run: "PipelineRun", step_id: str) -> None:
        self._run = run
        self.step_id = step_id

    def approve(self, *, approver: str = "sdk") -> dict:
        return self._run._client.approve_step(
            self._run.pipeline_id, self.step_id, approver=approver)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<gate {self.step_id} of {self._run.pipeline_id}>"


class PipelineRun:
    """A launched pipeline: status, gates, and a terminal wait."""

    def __init__(self, client: CairnClient, pipeline_id: str) -> None:
        self._client = client
        self.pipeline_id = pipeline_id

    def detail(self) -> dict:
        return self._client.pipeline(self.pipeline_id)

    @property
    def status(self) -> str:
        return str(self.detail().get("status", ""))

    def steps(self) -> dict[str, str]:
        d = self.detail()
        return {sid: str(st.get("status", "")) for sid, st in (d.get("step_state") or {}).items()}

    def pending_gate(self) -> Optional[GateHandle]:
        """The first step awaiting a human, in plan order — None when nothing waits."""
        d = self.detail()
        state = d.get("step_state") or {}
        order = d.get("step_order") or list(state)
        for sid in order:
            if str((state.get(sid) or {}).get("status", "")) == "awaiting_approval":
                return GateHandle(self, sid)
        return None

    def cancel(self) -> dict:
        return self._client.cancel_pipeline(self.pipeline_id)

    def wait(self, *, timeout_s: float = 3600.0, poll_s: float = 2.0,
             on_gate: Optional[Any] = None) -> dict:
        """Block until terminal. ``on_gate(GateHandle)`` fires once per newly
        awaiting gate (approve there for unattended CI); without it the wait
        simply keeps polling while a human answers in the console."""
        deadline = time.monotonic() + timeout_s
        seen_gates: set[str] = set()
        while True:
            d = self.detail()
            if str(d.get("status", "")) in _TERMINAL:
                return d
            if on_gate is not None:
                for sid, st in (d.get("step_state") or {}).items():
                    if str(st.get("status", "")) == "awaiting_approval" and sid not in seen_gates:
                        seen_gates.add(sid)
                        on_gate(GateHandle(self, sid))
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"pipeline {self.pipeline_id} not terminal after {timeout_s}s "
                    f"(status={d.get('status')})")
            time.sleep(poll_s)


def run_pipeline(
    client: CairnClient,
    ref: str,
    *,
    inputs: Optional[dict] = None,
    environment: Optional[str] = None,
    compiled: Optional[CompiledPack] = None,
    expected_hash: Optional[str] = None,
) -> PipelineRun:
    """Launch ``<pack>/<name>`` (or ``pipeline:<pack>/<name>``) as a governed run.

    Pass ``compiled`` (the class's build product) to PIN the launch to that
    exact record (D6): the pin is the compiled text's hash, the server compares
    it to the installed record's ``source_hash``/``content_hash`` and refuses
    with :class:`PipelineDriftError` on mismatch — CI can never silently run a
    different pipeline than the class it imported. ``expected_hash`` pins
    explicitly when you hold a hash rather than a build.
    """
    pin = expected_hash or (compiled.content_hash if compiled is not None else None)
    try:
        out = client.launch_pipeline(
            ref, dict(inputs or {}), environment=environment, expected_hash=pin)
    except CairnAPIError as e:
        if getattr(e, "status_code", None) == 409 and pin:
            raise PipelineDriftError(str(e)) from e
        raise
    return PipelineRun(client, str(out["pipeline_id"]))


__all__ = ["GateHandle", "PipelineDriftError", "PipelineRun", "run_pipeline"]
