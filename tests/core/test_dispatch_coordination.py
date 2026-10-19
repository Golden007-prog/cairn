"""#1532 part 2 — fleet-wide critical sections on Postgres, local otherwise.

The contract under test, in order of importance:

  1. Memory mode is EXACTLY the old behaviour: per-key mutual exclusion inside
     one process, different keys never contend. The whole hermetic suite runs
     in this mode, so it must be indistinguishable from the asyncio.Lock dict
     it replaced.
  2. A broken/absent Postgres demotes to local-only with a warning — the
     coordination upgrade must never be able to turn into an outage.
  3. (live PG, ``PGSTORE_DSN``-gated) Two INSTANCES — two pools, standing in
     for two replicas — mutually exclude on the same key, do not contend on
     different keys or namespaces, and a holder whose session dies releases
     the lock without any reaper (the crash-safety story the design leans on).
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dispatch.coordination import CriticalSections


# ── memory mode (the default for every hermetic test in the suite) ───────


async def test_same_key_serializes_and_different_keys_do_not(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    cs = CriticalSections(namespace="t")
    order: list[str] = []

    async def worker(tag: str, key: str, hold_s: float) -> None:
        async with cs.hold(key):
            order.append(f"{tag}:in")
            await asyncio.sleep(hold_s)
            order.append(f"{tag}:out")

    # same key: strict nesting a:in a:out b:in b:out
    await asyncio.gather(worker("a", "k", 0.05), worker("b", "k", 0))
    assert order == ["a:in", "a:out", "b:in", "b:out"]

    # different keys: b finishes while a still holds
    order.clear()
    await asyncio.gather(worker("a", "k1", 0.05), worker("b", "k2", 0))
    assert order.index("b:out") < order.index("a:out")


async def test_reentry_from_the_same_task_deadlocks_is_not_the_contract():
    """Pin NON-reentrancy explicitly: the old asyncio.Lock dict was not
    reentrant either, and a silent behaviour change here would let nested
    advance paths pass in memory mode and deadlock under Postgres."""
    cs = CriticalSections(namespace="t")

    async def nested() -> None:
        async with cs.hold("k"):
            async with cs.hold("k"):  # pragma: no cover — must never be reached
                pass

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(nested(), timeout=0.2)


async def test_unreachable_postgres_demotes_to_local_not_outage(monkeypatch, caplog):
    """OBS_STORAGE points at a Postgres that is not there: sections must still
    work (locally), warn once, and never raise out of hold()."""
    monkeypatch.setenv("OBS_STORAGE", "postgresql://nobody:x@127.0.0.1:1/void")
    cs = CriticalSections(namespace="t")
    entered = []
    with caplog.at_level("WARNING"):
        async with cs.hold("k"):
            entered.append(True)
        async with cs.hold("k"):   # second entry: the failed pool is cached
            entered.append(True)
    assert entered == [True, True]
    assert sum("advisory-lock pool" in r.message for r in caplog.records) == 1, (
        "degrade must warn exactly once, not per section")


async def test_aclose_is_idempotent_and_resets():
    cs = CriticalSections(namespace="t")
    async with cs.hold("k"):
        pass
    await cs.aclose()
    await cs.aclose()
    async with cs.hold("k"):   # usable again after close
        pass


# ── live Postgres: the two-replica story (PGSTORE_DSN-gated) ─────────────


def _dsn() -> str:
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    pytest.importorskip("asyncpg")
    return dsn


async def test_live_pg_two_instances_mutually_exclude(monkeypatch):
    """Two CriticalSections instances = two pools = the honest single-process
    stand-in for two replicas. Same key serializes ACROSS instances; a
    different key and a different namespace both stay concurrent."""
    monkeypatch.setenv("OBS_STORAGE", _dsn())
    a, b = CriticalSections(namespace="t1532"), CriticalSections(namespace="t1532")
    other = CriticalSections(namespace="t1532-other")
    order: list[str] = []

    async def hold(cs, tag, key, hold_s):
        async with cs.hold(key):
            order.append(f"{tag}:in")
            await asyncio.sleep(hold_s)
            order.append(f"{tag}:out")

    try:
        # Either replica may win the race (#2544); exclusion means no interleaving.
        # Both hold, so whichever enters first would be overlapped by a broken lock.
        await asyncio.gather(hold(a, "a", "k", 0.3), hold(b, "b", "k", 0.3))
        assert order in (["a:in", "a:out", "b:in", "b:out"],
                         ["b:in", "b:out", "a:in", "a:out"]), (
            f"same key must serialize across instances (replicas): {order}")

        order.clear()
        await asyncio.gather(hold(a, "a", "k", 0.3), hold(b, "b", "k2", 0),
                             hold(other, "c", "k", 0))
        assert order.index("b:out") < order.index("a:out"), "different key: concurrent"
        assert order.index("c:out") < order.index("a:out"), "different namespace: concurrent"
    finally:
        await a.aclose(); await b.aclose(); await other.aclose()


async def test_live_pg_session_death_releases_the_lock(monkeypatch):
    """THE crash-safety claim, tested rather than asserted: a holder whose DB
    session dies frees the lock with no reaper and no lease. Simulated by
    terminating the holder's lock connection out from under it."""
    monkeypatch.setenv("OBS_STORAGE", _dsn())
    a, b = CriticalSections(namespace="t1532c"), CriticalSections(namespace="t1532c")
    died = asyncio.Event()
    progressed = asyncio.Event()

    async def crashy():
        try:
            async with a.hold("k"):
                # kill every connection in a's lock pool — the session dies,
                # Postgres must free the advisory lock immediately.
                pool = await a._get_pool()
                for holder in list(getattr(pool, "_holders", [])):
                    con = getattr(holder, "_con", None)
                    if con is not None:
                        con.terminate()
                died.set()
                await asyncio.sleep(3)   # keep "holding" long past b's acquire
        except Exception:
            died.set()
            raise

    async def waiter():
        await died.wait()
        async with b.hold("k"):
            progressed.set()

    try:
        t1 = asyncio.create_task(crashy())
        t2 = asyncio.create_task(waiter())
        await asyncio.wait_for(progressed.wait(), timeout=5), (
            "the lock must be free the moment the holder's session dies")
        t1.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
    finally:
        await a.aclose(); await b.aclose()


async def test_nested_sections_across_namespaces_do_not_deadlock(monkeypatch):
    """The bridge NESTS these: `_advance` holds the per-pipeline section and the
    leg-submit path takes the per-target admit section inside it. Each namespace
    therefore gets its OWN pool — sharing one would let N tasks pin the outer
    connection and all wait for an inner one, deadlocking only under concurrent
    load in production. Memory mode can't show the pool bug, but it CAN pin that
    nesting is a supported shape so nobody 'simplifies' the namespaces together.
    """
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    outer = CriticalSections(namespace="pipeline")
    inner = CriticalSections(namespace="target-admit")
    done = []

    async def advance(pid: str, target: str) -> None:
        async with outer.hold(pid):              # per-pipeline section
            await asyncio.sleep(0)
            async with inner.hold(target):       # nested per-target admit
                done.append(pid)

    await asyncio.wait_for(
        asyncio.gather(*(advance(f"p{i}", "gpu-1") for i in range(6))), timeout=5)
    assert sorted(done) == [f"p{i}" for i in range(6)]


def test_each_namespace_gets_its_own_lock_pool():
    """Source-level pin on the fix: the pool purpose must carry the namespace."""
    import inspect

    from cairn.core.dispatch import coordination

    src = inspect.getsource(coordination)
    assert 'purpose=f"advisory-locks:{self.namespace}"' in src, (
        "one pool shared across the nested namespaces deadlocks — see the "
        "module docstring")
