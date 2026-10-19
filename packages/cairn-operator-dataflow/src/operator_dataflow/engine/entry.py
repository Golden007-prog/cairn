"""cairn-dataflow-run — the on-machine entrypoint (ADR-0081 D3/D5, #1048).

The data-plane analog of ``cairn-train-local``/``cairn-dataset-fix``: reads a
``DataflowSpec`` (``CAIRN_DATAFLOW_SPEC`` env JSON, or a ``spec.json`` path
arg — the remote/exec dispatch shape proven on fix-remote), executes it in the
current working directory, and writes ``result.json`` + ``metrics.json`` for
the poller. ``CAIRN_PARENT_RUN``/``CAIRN_RUN_ID`` stamp lineage (#989).

Exit codes: 0 ok · 3 budget exceeded · 1 anything else. The launcher (local
subprocess or remote/exec over SSH) owns kill/cancel — the runner itself never
traps signals, so a cancel is an honest external kill (#603 semantics).

#2614 — a job never outlives its OWNER. The local builtin engine runs the
runner as its own child and records its pid in ``CAIRN_DATAFLOW_OWNER_PID``;
when that process dies (a server SIGKILL), the runner's parent changes and a
watchdog thread kills the runner's whole process group. Without it the job
kept running unwatched while the restarted server failed the leg
"process table lost" — side effects landing after the leg was settled. The
variable is set only by the local engine, so an SSH box or Metaflow run of
this same entrypoint is untouched.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from cairn.core.dataflow import DataflowSpec

from cairn.core.dataflow.runner import BudgetExceeded, run_dataflow


def load_spec(argv: list[str] | None = None) -> DataflowSpec:
    raw = os.environ.get("CAIRN_DATAFLOW_SPEC", "").strip()
    if raw:
        return DataflowSpec.model_validate(json.loads(raw))
    args = argv if argv is not None else sys.argv[1:]
    if args and Path(args[0]).exists():
        return DataflowSpec.model_validate(json.loads(Path(args[0]).read_text(encoding="utf-8")))
    raise SystemExit(
        "cairn-dataflow-run: no spec. Set CAIRN_DATAFLOW_SPEC=<json> or pass a spec.json path."
    )


#: How often the owner watchdog looks (#2614). A dead owner is noticed within it.
OWNER_POLL_S = 0.5


def watch_owner(owner_pid: int, *, poll_s: float = OWNER_POLL_S) -> threading.Thread:
    """Kill this runner's process group once its owner is gone (#2614).

    The owner is this process's parent: the local engine spawns the runner
    directly, in its own session. When the owner dies the runner is re-parented,
    so ``os.getppid()`` stops being ``owner_pid`` — the portable test (Linux has
    ``PR_SET_PDEATHSIG``, macOS has nothing like it). SIGKILL to the group takes
    any kernel subprocess with it, the same way cancel does (#603)."""
    def _loop() -> None:
        while True:
            if os.getppid() != owner_pid:
                try:
                    os.killpg(os.getpgid(0), signal.SIGKILL)
                finally:
                    os._exit(137)  # if the group kill could not reach us, leave anyway
            time.sleep(poll_s)

    t = threading.Thread(target=_loop, name="cairn-dataflow-owner-watchdog", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    owner = os.environ.get("CAIRN_DATAFLOW_OWNER_PID", "").strip()
    if owner.isdigit():
        watch_owner(int(owner))
    spec = load_spec(argv)
    workdir = Path(os.getcwd())
    parent_run = os.environ.get("CAIRN_PARENT_RUN", "").strip() or spec.parent_run
    try:
        result = run_dataflow(spec, workdir)
    except BudgetExceeded as e:
        (workdir / "result.json").write_text(
            json.dumps({"error": str(e), "kind": "budget"}), encoding="utf-8")
        print(f"cairn-dataflow-run: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — the terminal artifact must name the failure
        (workdir / "result.json").write_text(
            json.dumps({"error": f"{type(e).__name__}: {e}"}), encoding="utf-8")
        print(f"cairn-dataflow-run: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    payload = result.model_dump()
    if parent_run:
        payload["lineage_parent_run"] = parent_run  # stamped, not a contract field
    (workdir / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "stats": result.stats}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
