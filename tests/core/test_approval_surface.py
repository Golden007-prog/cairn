"""The awaiting_approval surfacing shared by the durable runtimes (#456, #1847).

A durable runtime pauses the graph INSIDE its workflow, so the dispatcher's
in-process pause branch — the thing that sets ``awaiting_approval`` and captures
the ApprovalRequest — is never reached. Without this write the run sits at
``running``, and ``POST /runs/{id}/resume`` refuses it (that endpoint guards on
``status == "awaiting_approval"``), so a gated run is UNAPPROVABLE.

Temporal got that fix in #456. DBOS shipped the same pause shape and never did,
so its gated runs hung until the 24h ``recv`` timeout (#1847) — verified live
before the fix: status stayed ``running``, ``/pending-approval`` reported
``pending: false``, and resume answered "run is 'running', not awaiting
approval — cannot resume".

These pin the shared write both runtimes now call.
"""
from __future__ import annotations

import pytest

from cairn.core.approval.surface import surface_awaiting_approval

pytestmark = pytest.mark.asyncio


class _FakeStore:
    def __init__(self, fail: bool = False):
        self.updates: list[tuple] = []
        self.events: list[tuple] = []
        self._fail = fail

    async def update(self, run_id, **kw):
        if self._fail:
            raise KeyError(run_id)
        self.updates.append((run_id, kw))

    async def append_event(self, run_id, etype, data):
        self.events.append((run_id, etype, data))


def _patch(monkeypatch, store):
    import cairn.core.storage as _storage
    monkeypatch.setattr(_storage, "default_store", lambda: store)


async def test_surfacing_flips_status_and_records_what_is_approved(monkeypatch):
    store = _FakeStore()
    _patch(monkeypatch, store)
    approval = {"operator": "artifact/promote", "risk": "external_write"}

    assert await surface_awaiting_approval("run-1", approval) is True
    # the status the resume endpoint's guard requires
    assert ("run-1", {"status": "awaiting_approval"}) in store.updates
    # and WHAT is being approved, for /pending-approval and the console card
    rid, etype, data = store.events[0]
    assert rid == "run-1" and etype == "awaiting_approval"
    assert data["approval"]["operator"] == "artifact/promote"


async def test_no_approval_payload_still_flips_status(monkeypatch):
    store = _FakeStore()
    _patch(monkeypatch, store)
    assert await surface_awaiting_approval("run-2", None) is True
    assert ("run-2", {"status": "awaiting_approval"}) in store.updates
    assert store.events[0][2] == {}          # no bogus approval key


async def test_a_store_failure_is_reported_not_raised(monkeypatch):
    """Best-effort by design: the decision channel still works, so a store
    hiccup must not crash a workflow. It returns False (and logs) rather than
    propagating — a raise here would kill a run that is otherwise fine."""
    store = _FakeStore(fail=True)
    _patch(monkeypatch, store)
    assert await surface_awaiting_approval("run-3", {"operator": "x"}) is False


async def test_empty_run_id_is_a_no_op(monkeypatch):
    store = _FakeStore()
    _patch(monkeypatch, store)
    assert await surface_awaiting_approval("", {"operator": "x"}) is False
    assert store.updates == [] and store.events == []


async def test_the_dbos_workflow_surfaces_before_it_blocks_on_recv():
    """#1847's root cause: the surfacing must happen BEFORE ``DBOS.recv_async``.
    Surfacing after the recv would be useless — recv blocks until a decision
    that nobody can send, because the run never became approvable."""
    pytest.importorskip("dbos")
    import inspect

    from cairn.core.runtime.dbos import workflow as W

    src = inspect.getsource(W.run_template)
    assert "surface_awaiting_approval" in src, "the DBOS gate no longer surfaces"
    assert src.index("surface_awaiting_approval") < src.index("recv_async"), (
        "surfacing must precede recv_async, or the run blocks before it is approvable")


# ── the reconciler seam for DBOS (#1847) ──────────────────────────────────


async def test_dbos_terminal_status_maps_only_terminal_workflows(monkeypatch):
    """#1847 — under DBOS the workflow runs in the SAME process as the caller.
    A crash kills the `invoke` await that would have written the terminal
    status; `launch()` then recovers the workflow and completes it with nobody
    listening, stranding the record. Verified live: a gated run killed at its
    gate, restarted and approved ended `dbos SUCCESS` / `run awaiting_approval`
    — permanently, and the approval reaper would have expired a run that
    succeeded. The reconciler repairs it, but only if it can ask DBOS.

    PENDING must map to None: a run parked on `recv` waiting for a human is
    PENDING, and terminalizing it would auto-resolve live approval gates.
    """
    pytest.importorskip("dbos")
    from types import SimpleNamespace

    import dbos as _dbos

    from cairn.core.runtime.dbos import runtime as R

    monkeypatch.setattr(R, "ensure_launched", lambda: None)
    cases = {
        "SUCCESS": "done",
        "ERROR": "failed",
        "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "failed",
        "CANCELLED": "cancelled",
        "PENDING": None,        # parked at a gate — the reaper owns it
        "ENQUEUED": None,       # not started
    }
    for raw, expected in cases.items():
        async def _status(_rid, _raw=raw):
            return SimpleNamespace(status=_raw)
        monkeypatch.setattr(_dbos.DBOS, "get_workflow_status_async", _status)
        got = await R.DBOSRuntime().workflow_terminal_status("run-x")
        assert got == expected, f"{raw} → {got}, expected {expected}"


async def test_dbos_terminal_status_is_none_when_dbos_cannot_answer(monkeypatch):
    """An unknown id or a DBOS hiccup must read as "don't touch it", never as a
    terminal verdict — a spurious `done` would close a run that is still live."""
    pytest.importorskip("dbos")
    import dbos as _dbos

    from cairn.core.runtime.dbos import runtime as R

    monkeypatch.setattr(R, "ensure_launched", lambda: None)

    async def _boom(_rid):
        raise RuntimeError("dbos unavailable")

    monkeypatch.setattr(_dbos.DBOS, "get_workflow_status_async", _boom)
    assert await R.DBOSRuntime().workflow_terminal_status("run-x") is None

    async def _none(_rid):
        return None

    monkeypatch.setattr(_dbos.DBOS, "get_workflow_status_async", _none)
    assert await R.DBOSRuntime().workflow_terminal_status("run-x") is None


# ── the workflow id must be a real run id (#1851) ─────────────────────────


async def test_dbos_invoke_refuses_a_missing_or_template_name_run_id(monkeypatch):
    """#1851 — found on prod: the DBOS system DB held `workflow_uuid = promote`,
    the TEMPLATE name, because the CLI only threaded run_id into state under
    --interactive and the compiler fell back to spec.name. A template name is
    never a valid durable workflow id: it is DBOS's idempotency key (two runs
    of one template become the same workflow) and the key every downstream
    write uses (surface_awaiting_approval failed with KeyError('promote'), so
    the gate parked invisibly). The runtime now refuses before touching DBOS,
    so this needs no live DBOS process."""
    pytest.importorskip("dbos")
    from cairn.core.runtime.dbos import runtime as R

    launched = []
    monkeypatch.setattr(R, "ensure_launched", lambda: launched.append(1))
    rt = R.DBOSRuntime()

    with pytest.raises(ValueError, match="#1851"):
        await rt.invoke(object(), {}, run_id="promote", template_name="promote")
    with pytest.raises(ValueError, match="#1851"):
        await rt.invoke(object(), {}, run_id=None, template_name="promote")
    with pytest.raises(ValueError, match="#1851"):
        await rt.invoke(object(), {}, run_id="", template_name="promote")
    # the refusal happens BEFORE DBOS is launched — a bad id must not leave a
    # half-registered workflow behind
    assert launched == []


async def test_surface_writes_to_the_store_it_is_given():
    """#1865 — the DBOS workflow passes the server's store; default_store() with
    OBS_STORAGE unset is a fresh in-memory instance per call, so a write there
    is a write nobody reads."""
    from cairn.core.approval.surface import surface_awaiting_approval
    from cairn.core.storage import build_storage
    from cairn.core.storage.protocols import Run, TriggerEvent

    mine = build_storage().runs
    await mine.create(Run(id="g-1865", trigger=TriggerEvent(source="manual", payload={}), status="running"))
    assert await surface_awaiting_approval("g-1865", {"operator": "mcp/call"}, store=mine) is True
    assert (await mine.get("g-1865")).status == "awaiting_approval"
    assert [e.type for e in await mine.get_events("g-1865")] == ["awaiting_approval"]
