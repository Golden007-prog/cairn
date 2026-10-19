"""The gang-scheduled distributed-train bridge (#1366, Argo-readiness epic #1367).

The SECOND static Metaflow bridge class — parameterized, never code-generated,
so D1 holds exactly as it does for ``CairnFlow``. Where ``CairnFlow`` runs the
whole stage list inside one ``execute`` step, this flow exists for the one
shape that step cannot express: a stage that needs N *coordinated* workers
(``torch.distributed`` data-parallel training). ``num_parallel`` is a runtime
``Parameter``, so one class covers every worker count.

Scope contract (enforced by ``MetaflowEngine.validate``): the spec contains
ONLY the distributed stage. Post-train stages (eval, package) are their own
flow leg — pipelines already chain legs across gates, and a multi-step remote
flow would reopen the #1358 cross-pod filesystem problem for the working set.

Each rank runs the SAME kernel through the SAME ``run_dataflow`` interpreter
(#1048 anti-deviation rule) with the torch-conventional rendezvous env
(``RANK``/``WORLD_SIZE``/``MASTER_ADDR``/``MASTER_PORT``) plus the
``CAIRN_DIST_*`` mirror exported first — a kernel that never looks at them
behaves exactly as it does today, which is what makes the noop-kernel
integration test meaningful. Rank 0 owns the REAL workdir (the poller's
progress.json contract) and its result is authoritative; other ranks write
into ``rank-NN/`` subdirs so a shared local filesystem cannot interleave
``result.json`` writes.

Run standalone (which is what makes it testable):

    python -m operator_dataflow_metaflow.metaflow_train_bridge run \
        --spec_json '{"stages":[…]}' --workers 2

Found live while proving this locally: Metaflow's local ``@parallel`` mock
spawns secondary ranks with the bare command ``python``, so the submitting
engine must put the interpreter's bin dir on PATH (uv venvs expose only
``python3`` system-wide).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from metaflow import FlowSpec, Parameter, current, parallel, step  # noqa: PLC0415 - optional [metaflow] extra, module only imported by the adapter

from cairn.core.dataflow import DataflowSpec

from cairn.core.dataflow.runner import BudgetExceeded, run_dataflow
from operator_dataflow_metaflow.metaflow_bridge import _write


class CairnTrainFlow(FlowSpec):
    """start → N-rank gang (`num_parallel`) → join → end.

    Deliberately NOT a general DAG: one distributed stage, one gang. The
    static shape is the point — every worker count is the same class.
    """

    spec_json = Parameter("spec_json", default="{}", help="Serialized DataflowSpec.")
    workdir = Parameter("workdir", default="", help="Where progress/result land (rank 0).")
    workers = Parameter("workers", default=2, help="Gang size (num_parallel).")

    @step
    def start(self):
        self.spec = json.loads(self.spec_json)
        wd = Path(self.workdir or ".")
        wd.mkdir(parents=True, exist_ok=True)
        _write(wd / "progress.json", {
            "state": "running", "stage": None,
            "stages_done": 0, "stages_total": len(self.spec.get("stages") or []),
        })
        self.next(self.train, num_parallel=self.workers)

    @parallel
    @step
    def train(self):
        self.rank = current.parallel.node_index
        self.world = current.parallel.num_nodes
        # torch.distributed env:// rendezvous, exported BEFORE the kernel runs;
        # the CAIRN_DIST_* mirror is the engine-neutral spelling a kernel can
        # depend on without committing to torch's names.
        os.environ.update({
            "RANK": str(self.rank),
            "WORLD_SIZE": str(self.world),
            "MASTER_ADDR": current.parallel.main_ip or "127.0.0.1",
            "MASTER_PORT": os.environ.get("MASTER_PORT", "29500"),
            "CAIRN_DIST_RANK": str(self.rank),
            "CAIRN_DIST_WORLD": str(self.world),
        })
        base = Path(self.workdir or ".")
        wd = base if self.rank == 0 else base / f"rank-{self.rank:02d}"
        wd.mkdir(parents=True, exist_ok=True)
        parsed = DataflowSpec.model_validate(self.spec)

        def on_progress(progress: dict) -> None:
            # SAME progress.json shape the poller reads (D8a) — rank 0 only,
            # so N ranks never interleave writes into the polled file.
            _write(base / "progress.json", {
                "state": "running",
                "stage": progress.get("stage"),
                "stages_done": progress.get("stages_done", 0),
                "stages_total": progress.get("stages_total", len(parsed.stages)),
            })

        try:
            result = run_dataflow(
                parsed, wd,
                on_progress=on_progress if self.rank == 0 else None,
            )
        except BudgetExceeded as e:
            if self.rank == 0:
                _write(base / "result.json", {"error": str(e), "kind": "budget"})
            raise
        except Exception as e:  # noqa: BLE001 — a failed rank is a REAL failure (#568)
            if self.rank == 0:
                _write(base / "result.json", {"error": f"{type(e).__name__}: {e}"})
            raise
        # rank 0's result is authoritative; other ranks' payloads are dropped
        # at the join (their kernel work reaches rank 0 via torch collectives,
        # not via cairn's result contract).
        self.result_payload = result.model_dump() if self.rank == 0 else None
        self.next(self.join)

    @step
    def join(self, inputs):
        control = next(i for i in inputs if i.rank == 0)
        payload = dict(control.result_payload or {})
        stats = dict(payload.get("stats") or {})
        # The gang's shape, recorded where gates/reports already read numbers —
        # and what the integration test asserts a REAL 2-rank run produced.
        stats["dist.world"] = control.world
        stats["dist.ranks_completed"] = len(list(inputs))
        payload["stats"] = stats
        self.result_payload = payload
        self.merge_artifacts(inputs, exclude=["result_payload", "rank", "world"])
        self.next(self.end)

    @step
    def end(self):
        wd = Path(self.workdir or ".")
        parsed = DataflowSpec.model_validate(self.spec)
        payload = dict(self.result_payload)
        if parsed.parent_run:
            payload["lineage_parent_run"] = parsed.parent_run
        _write(wd / "result.json", payload)
        stages_total = len(parsed.stages)
        _write(wd / "progress.json", {
            "state": "succeeded", "stage": None,
            "stages_done": stages_total, "stages_total": stages_total,
        })


if __name__ == "__main__":
    CairnTrainFlow()
