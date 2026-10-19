"""``score_run`` is labels + verdict, nothing else (#2421 decision 7).

The training-history row (#2034) and the divergence reaction (#2030) it used to write
after the verdict are the platform's ``scoring`` run hook's now
(packages/cairn-platform/tests/test_hooks.py). Public set: no platform installed.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types

from cairn.core.eval import scoring as S


class _Model:
    def __init__(self, d):
        self._d = d

    def model_dump(self, mode="json"):
        return dict(self._d)


class _Store:
    def __init__(self):
        self.calls: list[tuple] = []

    async def get(self, run_id):
        self.calls.append(("get", run_id))
        return types.SimpleNamespace(template_ref="training/finetune", workspace_id="acme",
                                     model_dump=lambda mode="json": {"id": run_id, "status": "done"})

    async def get_events(self, run_id):
        self.calls.append(("get_events", run_id))
        return [_Model({"type": "parameters", "data": {}})]

    async def update(self, run_id, **fields):
        self.calls.append(("update", run_id, fields))


def test_score_run_writes_the_verdict_and_nothing_else(monkeypatch):
    seen = []

    async def _labels(run, events, *, ledger):
        seen.append(("labels", run["id"]))

    async def _verdict(run, *, ledger):
        seen.append(("verdict", run["template_ref"]))
        return "pass"

    monkeypatch.setattr(S, "label_run_failures", _labels)
    monkeypatch.setattr(S, "auto_eval_run", _verdict)
    before = set(sys.modules)
    store = _Store()
    assert asyncio.run(S.score_run("run-1", store=store, ledger=object())) == "pass"
    assert seen == [("labels", "run-1"), ("verdict", "training/finetune")]
    assert store.calls == [("get", "run-1"), ("get_events", "run-1"),
                           ("update", "run-1", {"final_verdict": "pass"})]
    pulled = {m for m in set(sys.modules) - before if "training_history" in m or "mid_training" in m}
    assert not pulled, f"score_run reached the product modules: {sorted(pulled)}"


def test_the_scoring_module_names_no_product_writer():
    src = inspect.getsource(S)
    assert "record_training_run" not in src and "react_to_divergence" not in src
