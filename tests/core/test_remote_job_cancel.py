"""#601 — cancelling a run must cancel its ACTIVE remote jobs (not orphan them).

Before the fix, `POST /runs/{id}/cancel` marked the run cancelled but never called
the remote-job operator's `cancel`, so a cancelled run left the remote (GPU/SSH)
process running — a cost + resource leak. `cancel_remote_jobs_for_run` now finds
the run's active job records, calls `op.cancel(handle)` (e.g. SSH-kill the pid),
and marks each record cancelled.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from cairn.core.operator import Operation, OperationContext, OperatorRegistry, register_operator
from cairn.core.operator import job_store as js
from cairn.core.operator.job_control import cancel_remote_jobs_for_run
from cairn.core.operator.job_store import (
    DONE,
    RUNNING,
    InMemoryRemoteJobStore,
    RemoteJobRecord,
)

_cancelled: list[str] = []

# #2377 — the test operator lives in this module's own registry, never the process-wide
# default one: left there, it made `default_registry` non-empty-but-wrong, and the
# console's operator listing served `test.cx` alone to every later test.
_REGISTRY = OperatorRegistry()


class _In(BaseModel):
    pass


class _Out(BaseModel):
    ok: bool = True


@register_operator(name="test/cancel_op", version="0.1.0", capabilities=["test.cx"], registry=_REGISTRY)
class _CancelOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:  # noqa: ARG002
        return _Out()

    async def cancel(self, handle) -> None:  # noqa: ANN001
        _cancelled.append(handle.job_id)


@pytest.fixture(autouse=True)
def store(monkeypatch):
    # #2361 — per test, never at import: a module-level `os.environ.pop` here
    # unset OBS_STORAGE for the whole session, and the next `import litellm`
    # (its `load_dotenv()`) refilled it from a developer `.env` with Postgres.
    monkeypatch.setenv("OBS_STORAGE", "")  # "" = unset to every getter
    monkeypatch.setenv("OBS_JOB_STORE", ":memory:")
    # One shared in-memory store for both the test's register and the helper's
    # list (the memory backend isn't cached across get_remote_job_store() calls).
    s = InMemoryRemoteJobStore()
    monkeypatch.setattr(js, "get_remote_job_store", lambda: s)
    _cancelled.clear()
    return s


def _rec(run_id, job_id, status, node="n1"):
    return RemoteJobRecord(
        run_id=run_id, node_name=node, provider="test/cancel_op",
        job_id=job_id, backend="ssh", status=status,
    )


async def test_cancel_kills_active_job_and_marks_cancelled(store):
    await store.register(_rec("r-1", "job-1", RUNNING))
    n = await cancel_remote_jobs_for_run("r-1", registry=_REGISTRY)
    assert n == 1
    assert _cancelled == ["job-1"]                       # op.cancel called (SSH-kill)
    assert (await store.get("r-1:n1")).status == "cancelled"


async def test_cancel_skips_terminal_and_other_runs(store):
    await store.register(_rec("r-2", "done-job", DONE))      # already terminal
    await store.register(_rec("r-3", "other-job", RUNNING))  # a different run
    n = await cancel_remote_jobs_for_run("r-2", registry=_REGISTRY)
    assert n == 0
    assert _cancelled == []
    # the unrelated run's job is untouched
    assert (await store.get("r-3:n1")).status == RUNNING


async def test_cancel_no_jobs_is_zero(store):  # noqa: ARG001
    assert await cancel_remote_jobs_for_run("nonexistent", registry=_REGISTRY) == 0
