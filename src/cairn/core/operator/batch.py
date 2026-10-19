"""CPU budgeting for batch (dataset-heavy) operator execution (#962).

Dataset operators (`dataset/anonymize`, `dataset/pii_scan`, `dedup`, `decontam`)
are **CPU-bound**, not IO-bound: presidio's spaCy pipeline, MinHash, regexset —
all burn cores. Parallelizing them across a process pool is the speedup, but a
pool sized at *all* cores just moves the monopoly from one thread to every core:
it starves the worker's asyncio event loop (so it stops servicing other runs and
the console) and any co-scheduled interactive run. This module is the single
place that decides **how many cores batch work may take**, so every dispatcher
(the local pool today; a Temporal/Ray one later) shares one safe policy.

Rules (see #962):
  1. Never all cores — reserve headroom for the event loop + interactive runs.
  2. Count the CONTAINER's cores (cpuset/cgroup via ``sched_getaffinity``), not
     the host's — inside Docker ``os.cpu_count()`` reports host cores, so a naive
     ``cpu_count - 2`` on a CPU-limited container over-subscribes and thrashes.
  3. One level of parallelism: the pool size IS the whole CPU budget; per-shard
     work must not spawn its own processes (no ``pool × n_process`` nesting).
  4. Nice the pool workers so the kernel preempts batch work under contention.

C (local pool) is mitigation — it bounds and de-prioritizes batch CPU but still
shares the box. A (a separate ``prod-worker-dataset`` container with a hard cgroup
``cpus:`` quota) is the physical isolation. Both call ``resolve_batch_workers``.
"""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Callable, Optional, Sequence, TypeVar

S = TypeVar("S")  # a shard spec (must be picklable — plain data, not an operator instance)
R = TypeVar("R")  # a shard result (must be picklable)

# Env knobs (operational, env-owned). Defaults are conservative — a batch job
# should feel like a background chore, never like it owns the box.
_ENV_MAX_WORKERS = "CAIRN_BATCH_MAX_WORKERS"      # hard cap on pool size
_ENV_RESERVED = "CAIRN_BATCH_RESERVED_CORES"       # cores kept for the event loop + interactive
_ENV_NICE = "CAIRN_BATCH_NICE"                      # OS niceness applied to pool workers

DEFAULT_RESERVED_CORES = 2
DEFAULT_NICE = 10


def available_cores() -> int:
    """Cores this PROCESS may actually use — container-aware.

    ``os.sched_getaffinity`` reflects the cpuset/cgroup the container is pinned
    to (Linux); ``os.cpu_count`` reports the host and over-counts inside Docker.
    Prefer affinity, fall back to cpu_count, floor at 1.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def resolve_batch_workers(requested: Optional[int] = None) -> int:
    """How many pool workers batch execution may use, honoring the headroom rule.

    ``requested`` is the operator/config ask (e.g. ``shard_count``); ``None`` =
    "as many as the budget allows". The result is
    ``clamp(1, min(requested?, cores - reserved, CAIRN_BATCH_MAX_WORKERS?), cores)``
    — always ≥ 1, always leaves ``reserved`` cores, never exceeds the container's
    cores. Reserved defaults to 2; set ``CAIRN_BATCH_RESERVED_CORES=0`` on a
    dedicated batch box (A) where taking every core is the point.
    """
    cores = available_cores()
    reserved = _int_env(_ENV_RESERVED, DEFAULT_RESERVED_CORES) or 0
    reserved = max(0, min(reserved, cores - 1))  # always leave ≥1 usable core
    budget = cores - reserved

    if requested is not None and requested > 0:
        budget = min(budget, requested)
    hard_cap = _int_env(_ENV_MAX_WORKERS, None)
    if hard_cap is not None and hard_cap > 0:
        budget = min(budget, hard_cap)
    return max(1, budget)


def batch_nice() -> int:
    """OS niceness for batch pool workers (higher = lower priority). Clamped to
    the portable-positive range so batch work can only ever DE-prioritize itself,
    never elevate above the interactive path."""
    n = _int_env(_ENV_NICE, DEFAULT_NICE) or 0
    return max(0, min(n, 19))


def _nice_init(nice_level: int) -> None:
    """ProcessPool initializer: drop the worker's scheduling priority so the
    kernel favors the asyncio event loop + interactive runs under contention."""
    try:
        os.nice(nice_level)
    except (OSError, AttributeError):
        pass  # unsupported platform — the core cap alone still bounds us


def map_shards_local(
    shard_fn: Callable[[S], R],
    shards: Sequence[S],
    *,
    requested_workers: Optional[int] = None,
    on_result: Optional[Callable[[int, R], None]] = None,
    retries: int = 1,
) -> list[R]:
    """Run ``shard_fn`` over ``shards`` across a budgeted, nice'd process pool.

    The core of the local (C) dispatcher: CPU-bound shard work runs in child
    processes (so the parent worker's asyncio event loop stays free to service
    other runs + the console), the pool is sized by :func:`resolve_batch_workers`
    (never all cores), and workers are nice'd so the kernel de-prioritizes them.
    Results come back **in shard order**.

    ``shard_fn`` and each shard MUST be picklable — a ProcessPool ships them to
    children by pickle. That's why the operator integration passes plain data
    (operator NAME + input dict + row range), re-resolving the operator from the
    registry inside the child, rather than pickling a live operator instance.

    DURABILITY (#1010): dispatch is ``submit`` + ``wait`` rather than
    ``pool.map``, so
      - ``on_result(index, result)`` fires as EACH shard completes — the caller
        checkpoints per shard (the manifest write in ``run_sharded``) instead of
        only learning outcomes when the whole map returns;
      - a shard whose child RAISES is resubmitted up to ``retries`` times before
        the run fails — one flaky shard no longer discards its siblings' work.
    A child killed outright (OOM/kill -9) still breaks the whole pool — that is
    ``ProcessPoolExecutor`` semantics — and surfaces as ``BrokenProcessPool``;
    the per-shard manifest makes the *re-invocation* cheap instead.

    Falls back to in-process serial execution when the budget is 1 (single core /
    capped) — no pool overhead when there's nothing to parallelize.
    """
    if not shards:
        return []
    workers = resolve_batch_workers(requested_workers if requested_workers is not None else len(shards))
    if workers <= 1 or len(shards) == 1:
        results_serial: list[R] = []
        for i, s in enumerate(shards):
            for attempt in range(retries + 1):
                try:
                    r = shard_fn(s)
                    break
                except Exception:
                    if attempt >= retries:
                        raise
            if on_result is not None:
                on_result(i, r)
            results_serial.append(r)
        return results_serial
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_nice_init,
        initargs=(batch_nice(),),
    ) as pool:
        results: list = [None] * len(shards)
        attempts = [0] * len(shards)
        pending: dict = {}

        def _submit(i: int) -> None:
            attempts[i] += 1
            pending[pool.submit(shard_fn, shards[i])] = i

        for i in range(len(shards)):
            _submit(i)
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                i = pending.pop(fut)
                try:
                    r = fut.result()
                except Exception:
                    # BrokenProcessPool (child died) can't be retried in this
                    # pool; an ordinary exception gets its retry budget.
                    if isinstance(fut.exception(), BrokenProcessPool) or attempts[i] > retries:
                        raise
                    _submit(i)
                    continue
                results[i] = r
                if on_result is not None:
                    on_result(i, r)
        return results


__all__ = [
    "available_cores",
    "resolve_batch_workers",
    "batch_nice",
    "map_shards_local",
    "DEFAULT_RESERVED_CORES",
    "DEFAULT_NICE",
]
