"""#2360 — ``GET /registry/installed-workflows`` reads the operator manifests
ONCE per listing and never stalls the rest of the console.

It planned every installed ref to attach Playground eligibility, and each plan
re-walked every operator ``pack.toml`` (74 manifest loads per ref): 2.2–3.0 s on
every warm call, run synchronously ON the event loop, so a ``/runs`` issued
meanwhile waited behind it — the head-of-line blocking #2317 fixed for
``/pipelines/installed`` and ``/flows``. These tests pin the three halves:

* the manifests are discovered once per listing, not once per ref,
* a slow listing does not delay a cheap request issued while it runs, and
* a warm listing stays inside the issue's acceptance bound.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest
from httpx import ASGITransport, AsyncClient

from cairn.core.eligibility import workflow_eligibility
from cairn.core import workflow_registry
from cairn.core.operator import loader
from cairn.core.pack.loader import DEV_PACK_PATH_ENV
from cairn.core.triggers.server import build_app
from tests import _layout

URL = "/registry/installed-workflows"
SLOW_S = 1.5


class _Runner:
    default_environment = "production"
    skill_engine = types.SimpleNamespace(list_skills=lambda: [])

    async def run(self, *a, **k):
        return []


@pytest.fixture()
def app(monkeypatch, tmp_path):
    """Hermetic: its own store, and the example pack as the discoverable workflows — the
    listing's first read syncs from discovery, and on a fresh machine (the public CI runner)
    nothing else is discoverable; on a developer machine ~/.cairn would answer instead."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)  # keyless
    monkeypatch.setenv("CAIRN_STORAGE", f"sqlite:///{tmp_path / 'listing.db'}")
    monkeypatch.setattr(workflow_registry, "_store", None)   # the process-wide registry is rebuilt from THIS env
    monkeypatch.setenv(DEV_PACK_PATH_ENV, str(_layout.REPO / "examples" / "hello-cairn"))
    return build_app(_Runner(), enable_scheduler=False)


@pytest.mark.asyncio
async def test_manifests_are_discovered_once_per_listing_not_per_ref(app, monkeypatch):
    """The cause, pinned directly: on the old code this counted one discovery
    per distinct ref (12 on the shipped packs). The rows must not change."""
    real = loader.discover_operator_pack_manifests
    calls: list[int] = []

    def counted():
        calls.append(1)
        return real()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=300) as ac:
        await ac.get(URL)  # the default workspace's first read may sync the registry
        monkeypatch.setattr(loader, "discover_operator_pack_manifests", counted)
        r = await ac.get(URL)
    rows = r.json()["workflows"]
    assert r.status_code == 200 and len({w["workflow_ref"] for w in rows}) > 1
    assert len(calls) == 1, f"{len(calls)} manifest discoveries for one listing"
    monkeypatch.setattr(loader, "discover_operator_pack_manifests", real)
    for w in rows:  # same answer as planning each ref on its own
        assert w["playground"] == workflow_eligibility(w["workflow_ref"]).model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_slow_listing_does_not_queue_a_cheap_request(app, monkeypatch):
    """Discovery made deliberately slow (a BLOCKING sleep, like a cold walk):
    `/runs`, issued while the listing runs, must come back long before it.
    On the old code both took the listing's time."""
    def slow_manifests():
        time.sleep(SLOW_S)
        return {}

    monkeypatch.setattr(loader, "discover_operator_pack_manifests", slow_manifests)
    done: dict[str, float] = {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=60) as ac:
        t0 = time.perf_counter()

        async def get(path: str, delay: float = 0.0) -> int:
            await asyncio.sleep(delay)
            r = await ac.get(path)
            done[path] = time.perf_counter() - t0
            return r.status_code

        codes = await asyncio.gather(get(URL), get("/runs", 0.2))

    assert codes == [200, 200]
    assert done[URL] >= SLOW_S
    assert done["/runs"] < done[URL] - 0.5, (
        f"/runs waited for the listing: {done['/runs']:.2f}s vs {done[URL]:.2f}s")


@pytest.mark.timing
@pytest.mark.asyncio
async def test_a_warm_listing_is_bounded(app):
    """The issue's acceptance number: warm < 300 ms (measured ~0.21 s on a
    loaded laptop after the fix, 1.7–2.8 s before). Best of three.

    A benchmark, not a CI gate: a shared CI executor measured 390 ms with the
    fix in place. The mechanism the number stands for is pinned by the two
    tests above (one discovery per listing; a slow listing never queues a
    cheap request). Run with ``pytest -m timing``."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=300) as ac:
        first = await ac.get(URL)
        assert first.status_code == 200 and first.json()["workflows"]
        timings = []
        for _ in range(3):
            t = time.perf_counter()
            r = await ac.get(URL)
            timings.append(time.perf_counter() - t)
            assert r.json() == first.json()
    assert min(timings) < 0.3, f"warm {URL} took {min(timings) * 1000:.0f} ms"
