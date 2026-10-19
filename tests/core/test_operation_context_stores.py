"""The deployment's store handles reach an operator through ``OperationContext``
(ADR-0120 D7 / #2453 group B): the default is resolved on first use, an operator that
never asks opens nothing, a handle the context opened is closed by the runtime after
``run``, and an injected handle wins and is never closed."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from pydantic import BaseModel

from cairn.core.operator import Operation, OperationContext, ReportSpec
from cairn.core.operator.testing import run_operator_for_test


class _Handle:
    def __init__(self, log, name):
        self.log, self.name, self.closed = log, name, False
        log.append(f"open:{name}")

    def close(self):
        self.closed = True
        self.log.append(f"close:{self.name}")


class _In(BaseModel):
    ask: list[str] = []


class _Out(BaseModel):
    same: bool
    names: list[str]


class _Probe(Operation[_In, _Out]):
    name = "test/store-probe"
    version = "0.0.1"
    capabilities = ["test.probe"]
    input_schema = _In
    output_schema = _Out
    reports = ReportSpec.NONE

    async def run(self, inp: _In, ctx: OperationContext) -> _Out:
        got = [getattr(ctx, f"{k}")() for k in inp.ask]
        again = [getattr(ctx, f"{k}")() for k in inp.ask]
        return _Out(same=all(a is b for a, b in zip(got, again)), names=[getattr(h, "name", "?") for h in got])


def _fakes(monkeypatch, log):
    import cairn.core.storage as storage
    import cairn.core.storage.fingerprints as fp
    import cairn.core.storage.schedule_store as sched

    class _Backend:
        runs = _Handle(log, "runs")

    monkeypatch.setattr(storage, "get_storage", lambda: _Backend)
    monkeypatch.setattr(sched, "default_schedule_store", lambda: _Handle(log, "schedules"))
    monkeypatch.setattr(fp, "build_fingerprint_store", lambda: _Handle(log, "fingerprints"))


async def test_an_operator_that_never_asks_opens_nothing(monkeypatch):
    log: list[str] = []
    _fakes(monkeypatch, log)
    log.clear()  # the fake backend's run store is built when the fixture is defined
    await run_operator_for_test(_Probe, {"ask": []})
    assert log == []


async def test_handles_are_opened_once_and_closed_after_run(monkeypatch):
    log: list[str] = []
    _fakes(monkeypatch, log)
    log.clear()
    out = await run_operator_for_test(_Probe, {"ask": ["schedule_store", "fingerprint_store"]})
    assert out.same and out.names == ["schedules", "fingerprints"]
    assert log == ["open:schedules", "open:fingerprints", "close:fingerprints", "close:schedules"]


async def test_the_run_store_is_process_wide_and_not_closed(monkeypatch):
    log: list[str] = []
    _fakes(monkeypatch, log)
    log.clear()
    out = await run_operator_for_test(_Probe, {"ask": ["run_store"]})
    assert out.names == ["runs"] and "close:runs" not in log


async def test_an_injected_handle_wins_and_is_never_closed(monkeypatch):
    log: list[str] = []
    _fakes(monkeypatch, log)
    log.clear()
    mine = _Handle(log, "mine")
    out = await run_operator_for_test(_Probe, {"ask": ["schedule_store"]}, ctx_overrides={"schedules": mine})
    assert out.names == ["mine"] and not mine.closed and "open:schedules" not in log


async def test_release_is_safe_to_call_twice_and_tolerates_a_failing_close():
    class _Bad:
        def close(self):
            raise RuntimeError("boom")

    ctx = OperationContext(pipeline_id="p", node_name="n")
    ctx._opened.append(_Bad())
    await ctx.release_handles()
    await ctx.release_handles()
    assert ctx._opened == []
