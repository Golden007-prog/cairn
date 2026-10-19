"""`cairn.seeds` — what `cairn demo` seeds comes from entry points (ADR-0120 D4, #2418).

Hermetic: in-memory stores, a fixed clock, discovery replaced where the test is
about discovery. Pins:

  1. The loader fails open: a provider that does not import, or is not callable,
     is skipped and recorded (doctor renders it); the rest load.
  2. With no provider installed the seeder writes the workspace and nothing else,
     and warns.
  3. The root distribution registers exactly the core's provider, and it
     resolves (the platform's `platform-demo` is declared by packages/cairn-platform,
     #2421 wave 2f).
  4. The core's provider seeds the example pack (`examples/hello-cairn`, #2423):
     the pipeline installed, two finished pipelines and one paused at `review`,
     every run a leg of the real plan, idempotently; it skips when the pack is not
     loaded.
"""
from __future__ import annotations

import logging
import os
import tomllib
from datetime import datetime, timezone
from importlib.metadata import EntryPoint
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core import demo_seed
from cairn.core import pack as pack_api
from cairn.core import workspace as ws
from cairn.core.pack import loader as loader_mod
from cairn.core.pack import seeds
from cairn.core.storage.memory.runs import InMemoryRunStore
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from cairn.core.storage.reviews import InMemoryReviewStore
from cairn.core.workflow_registry import InMemoryWorkflowRegistry

REPO = Path(__file__).resolve().parents[2]
PACK_DIR = REPO / "examples" / "hello-cairn"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
WS = demo_seed.DEMO_WORKSPACE
HELLO = {"hello-cairn": demo_seed.seed_hello_cairn}


@pytest.fixture
def stores(monkeypatch):
    monkeypatch.setattr(ws, "_store", ws.InMemoryWorkspaceStore())
    return {"store": InMemoryRunStore(), "registry": InMemoryWorkflowRegistry(),
            "pipelines": InMemoryPipelineStore(), "reviews": InMemoryReviewStore()}


@pytest.fixture
def hello_pack(monkeypatch, tmp_path):
    """The example as a dev pack (as `cairn dev` loads it); the real registry is
    restored afterwards so the rest of the session is untouched."""
    monkeypatch.setenv(loader_mod.DEV_PACK_PATH_ENV, str(PACK_DIR))
    monkeypatch.setenv("OBS_PROFILE", str(REPO / "src" / "cairn" / "profiles" / "sandbox.toml"))
    pack_api.clear()
    loader_mod.load_all_packs()
    yield
    pack_api.clear()
    monkeypatch.delenv(loader_mod.DEV_PACK_PATH_ENV)
    loader_mod.load_all_packs()


def _eps(monkeypatch, *pairs: tuple[str, str]) -> None:
    eps = [EntryPoint(name=n, value=v, group=seeds.ENTRY_POINT_GROUP) for n, v in pairs]
    monkeypatch.setattr(seeds, "entry_points", lambda group: eps if group == seeds.ENTRY_POINT_GROUP else [])


# ── the loader ──────────────────────────────────────────────────────────────

def test_the_loader_fails_open_and_records_what_did_not_load(monkeypatch, caplog):
    _eps(monkeypatch,
         ("broken", "cairn.core.no_such_module:seed"),
         ("not-callable", "cairn.core.pack.seeds:ENTRY_POINT_GROUP"),
         ("hello-cairn", "cairn.core.demo_seed:seed_hello_cairn"),
         ("hello-cairn", "cairn.core.demo_seed:seed_hello_cairn"))   # one dist seen twice
    with caplog.at_level(logging.WARNING, logger="cairn.core.pack.seeds"):
        found = seeds.load_seed_providers()
    assert found == {"hello-cairn": demo_seed.seed_hello_cairn}
    assert sorted(f["name"] for f in seeds.seed_load_failures()) == ["broken", "not-callable"]
    assert "broken" in caplog.text and "not-callable" in caplog.text


async def test_no_provider_seeds_nothing_and_warns(monkeypatch, stores, caplog):
    _eps(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="cairn.core.demo_seed"):
        result = await demo_seed.seed_demo_workspace(now=NOW, **stores)
    assert (result.providers, result.enabled, result.completed_runs, result.pipelines) == ([], [], [], [])
    assert "no `cairn.seeds` provider" in caplog.text
    assert (await ws.get_workspace_store().get(WS)).status == "active"   # the workspace still exists
    assert await stores["store"].list(workspace_id=WS, limit=10) == []
    assert await stores["pipelines"].list(workspace_id=WS) == []


def test_doctor_lists_the_providers_and_warns_when_there_are_none(monkeypatch):
    from cairn.cli.commands.doctor import check_seed_providers

    _eps(monkeypatch, ("hello-cairn", "cairn.core.demo_seed:seed_hello_cairn"),
         ("broken", "cairn.core.no_such_module:seed"))
    rows = {r.name: r for r in check_seed_providers()}
    assert (rows["seed_providers"].status, rows["seed_providers"].detail) == ("ok", "hello-cairn")
    assert rows["seed_provider[broken]"].status == "warn"
    _eps(monkeypatch)
    assert [r.status for r in check_seed_providers()] == ["warn"]


def test_the_root_distribution_registers_only_the_core_provider():
    """The core's provider, nothing else: `platform-demo` is the platform's (#2421)."""
    declared = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["entry-points"]["cairn.seeds"]
    assert declared == {"hello-cairn": "cairn.core.demo_seed:seed_hello_cairn"}
    for name, value in declared.items():
        assert callable(EntryPoint(name=name, value=value, group=seeds.ENTRY_POINT_GROUP).load())


# ── the core's provider: the example pack ───────────────────────────────────

async def test_the_core_provider_seeds_the_example_pack(hello_pack, stores):
    result = await demo_seed.seed_demo_workspace(now=NOW, providers=HELLO, **stores)
    assert result.providers == ["hello-cairn"] and not result.skipped
    assert result.enabled == [demo_seed.HELLO_PIPELINE]
    installed = await stores["registry"].list_installed(workspace_id=WS)
    assert f"pipeline:{demo_seed.HELLO_PIPELINE}" in {w.workflow_ref for w in installed}

    recs = {p.pipeline_id: p for p in await stores["pipelines"].list(workspace_id=WS)}
    assert sorted(recs) == sorted(result.pipelines)
    assert sorted(p.status for p in recs.values()) == ["done", "failed", "paused"]
    assert {p.bundle_ref for p in recs.values()} == {f"pipeline:{demo_seed.HELLO_PIPELINE}"}

    (paused,) = [p for p in recs.values() if p.status == "paused"]
    assert result.pending_gates == [f"{paused.pipeline_id}/review"]
    assert paused.cursor == ["review"]
    assert paused.step_state["review"].status == "awaiting_approval"
    assert paused.step_state["publish"].status == "pending"
    (failed,) = [p for p in recs.values() if p.status == "failed"]
    assert (failed.step_state["data-ok"].status, failed.step_state["data-ok"].verdict) == ("skipped", "gate_blocked")
    (done,) = [p for p in recs.values() if p.status == "done"]
    assert (done.step_state["review"].verdict, done.step_state["review"].approver) == ("approved", "alice")

    # every run is a leg of the real plan, on the record it belongs to
    runs = await stores["store"].list(workspace_id=WS, limit=100)
    assert sorted(r.id for r in runs) == sorted(result.completed_runs)
    for r in runs:
        step = recs[r.bundle_pipeline_id].step_state[r.step_id]
        assert step.run_id == r.id
        assert r.template_ref == next(s.ref for s in recs[r.bundle_pipeline_id].plan.steps if s.id == r.step_id)

    # the approval card shows what the gate's `show:` names
    reviews = {x.status: x for x in await stores["reviews"].list_reviews(workspace_id=WS, limit=10)}
    assert set(reviews) == {"open", "approved"}
    assert reviews["open"].pipeline_id == paused.pipeline_id

    # idempotent: a second seed rewrites the same records
    again = await demo_seed.seed_demo_workspace(now=NOW, providers=HELLO, **stores)
    assert (sorted(again.completed_runs), sorted(again.pipelines)) == (
        sorted(result.completed_runs), sorted(result.pipelines))
    assert len(await stores["store"].list(workspace_id=WS, limit=100)) == len(runs)
    assert len(await stores["reviews"].list_reviews(workspace_id=WS, limit=10)) == 2


async def test_every_seeded_record_follows_the_loaded_plan(hello_pack, stores):
    """#2608: a seeded record's steps are the loaded def's plan steps, in plan
    order, and read as one run of it — every step `done` up to where the story
    stops, `pending` after — so a step the pipeline gains is never left pending
    inside a finished record."""
    plan = demo_seed.plan_pipeline(demo_seed._hello_def())
    ids = [s.id for s in plan.steps]
    result = await demo_seed.seed_demo_workspace(now=NOW, providers=HELLO, **stores)
    recs = await stores["pipelines"].list(workspace_id=WS)
    assert len(recs) == len(result.pipelines) == 3
    for rec in recs:
        assert [s.id for s in rec.plan.steps] == ids
        assert list(rec.step_state) == ids
        statuses = [rec.step_state[sid].status for sid in ids]
        ran = [s for s in statuses if s != "pending"]
        assert statuses == ran + ["pending"] * (len(ids) - len(ran)), (rec.status, statuses)
        assert all(s == "done" for s in ran[:-1]), (rec.status, statuses)
    by_status = {rec.status: [rec.step_state[sid].status for sid in ids] for rec in recs}
    assert by_status["done"] == ["done"] * len(ids)
    assert by_status["failed"][ids.index("data-ok")] == "skipped"
    assert by_status["paused"][ids.index("review")] == "awaiting_approval"


async def test_the_core_provider_skips_when_the_example_pack_is_not_loaded(monkeypatch, stores):
    monkeypatch.setattr(demo_seed, "_hello_def", lambda: None)
    result = await demo_seed.seed_demo_workspace(now=NOW, providers=HELLO, **stores)
    assert result.providers == ["hello-cairn"]
    assert result.skipped == [f"hello-cairn: {demo_seed.HELLO_PIPELINE}: the example pack is not loaded"]
    assert (result.enabled, result.completed_runs, result.pipelines) == ([], [], [])


async def test_a_failing_provider_does_not_stop_the_others(hello_pack, stores):
    async def boom(workspace_id, **_):
        raise RuntimeError("no")

    result = await demo_seed.seed_demo_workspace(now=NOW, providers={"boom": boom, **HELLO}, **stores)
    assert result.providers == ["hello-cairn"] and len(result.pipelines) == 3
    assert result.skipped == ["boom: failed (RuntimeError: no)"]
