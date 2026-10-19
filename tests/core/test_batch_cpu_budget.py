"""#962 — batch CPU budgeting: never take all cores, container-aware, capped."""

from __future__ import annotations

import pytest

from cairn.core.operator import batch


@pytest.fixture
def cores(monkeypatch):
    """Pin the perceived core count so the budget is deterministic off-CI-box."""
    def _set(n: int):
        monkeypatch.setattr(batch, "available_cores", lambda: n)
    return _set


def _clear(monkeypatch):
    for k in ("CAIRN_BATCH_MAX_WORKERS", "CAIRN_BATCH_RESERVED_CORES", "CAIRN_BATCH_NICE"):
        monkeypatch.delenv(k, raising=False)


def test_default_reserves_two_cores(cores, monkeypatch):
    _clear(monkeypatch)
    cores(8)
    assert batch.resolve_batch_workers() == 6  # 8 - 2 reserved


def test_never_takes_all_cores_but_always_at_least_one(cores, monkeypatch):
    _clear(monkeypatch)
    cores(2)
    assert batch.resolve_batch_workers() == 1  # 2 - 2 -> clamped to leave >=1 usable
    cores(1)
    assert batch.resolve_batch_workers() == 1  # single-core box still works


def test_requested_shard_count_is_an_upper_bound(cores, monkeypatch):
    _clear(monkeypatch)
    cores(16)
    assert batch.resolve_batch_workers(requested=4) == 4      # asked for fewer than budget
    assert batch.resolve_batch_workers(requested=100) == 14   # asked for more -> capped at 16-2


def test_hard_cap_env_wins(cores, monkeypatch):
    _clear(monkeypatch)
    cores(16)
    monkeypatch.setenv("CAIRN_BATCH_MAX_WORKERS", "3")
    assert batch.resolve_batch_workers(requested=10) == 3


def test_reserved_zero_uses_all_cores_for_dedicated_batch_box(cores, monkeypatch):
    _clear(monkeypatch)
    cores(8)
    monkeypatch.setenv("CAIRN_BATCH_RESERVED_CORES", "0")
    assert batch.resolve_batch_workers() == 8  # A: dedicated worker, take everything


def test_reserved_cannot_starve_all_cores(cores, monkeypatch):
    _clear(monkeypatch)
    cores(4)
    monkeypatch.setenv("CAIRN_BATCH_RESERVED_CORES", "99")  # absurd
    assert batch.resolve_batch_workers() == 1  # still leaves 1 usable


def test_batch_nice_is_clamped_to_deprioritize_only(monkeypatch):
    _clear(monkeypatch)
    assert batch.batch_nice() == batch.DEFAULT_NICE
    monkeypatch.setenv("CAIRN_BATCH_NICE", "-5")  # can't elevate above interactive
    assert batch.batch_nice() == 0
    monkeypatch.setenv("CAIRN_BATCH_NICE", "50")
    assert batch.batch_nice() == 19


def test_available_cores_is_container_aware(monkeypatch):
    # sched_getaffinity (cpuset/cgroup) is preferred over cpu_count (host).
    monkeypatch.setattr("os.sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    assert batch.available_cores() == 2


# ── map_shards_local ──────────────────────────────────────────────────────
# module-level so ProcessPoolExecutor can pickle it to child processes.
import os as _os  # noqa: E402


def _square_pid(x: int):
    return (x * x, _os.getpid())


def test_map_shards_preserves_order_and_results(cores, monkeypatch):
    _clear(monkeypatch)
    cores(4)
    out = batch.map_shards_local(_square_pid, [1, 2, 3, 4, 5])
    assert [r[0] for r in out] == [1, 4, 9, 16, 25]  # order preserved


def test_map_shards_actually_uses_child_processes(cores, monkeypatch):
    _clear(monkeypatch)
    cores(4)  # budget 2 -> a real pool
    out = batch.map_shards_local(_square_pid, list(range(8)))
    pids = {r[1] for r in out}
    assert _os.getpid() not in pids  # ran in children, not the parent
    # The CONTRACT is child-process isolation, not scheduling spread: a pool
    # worker that finishes a trivial shard instantly may legally pick up the
    # next one, so `len(pids) >= 2` flaked whenever one child drained the
    # queue (#1199). Distinctness of workers is the pool's business.
    assert len(pids) >= 1


def test_map_shards_serial_when_budget_is_one(cores, monkeypatch):
    _clear(monkeypatch)
    cores(1)  # single core -> no pool, run inline in the parent
    out = batch.map_shards_local(_square_pid, [3, 4])
    assert all(r[1] == _os.getpid() for r in out)


def test_map_shards_empty_is_noop(cores, monkeypatch):
    _clear(monkeypatch)
    cores(8)
    assert batch.map_shards_local(_square_pid, []) == []
