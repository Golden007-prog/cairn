"""Review records (#1468) — the decision object attached to HITL gates.

Store tests run memory + sqlite through the same assertions; the bridge test
drives the real PipelineBridge with the fakes from test_pipeline_bridge and a
sqlite-backed review store (via OBS_STORAGE), proving the lifecycle: gate fire
opens the review, the SAME approve call that releases the gate decides it,
and a cancelled pipeline leaves no dangling open decisions. The API test uses
the datasets-API harness (keyless auth, httpx over ASGITransport).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import types
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cairn.core.storage.reviews import (
    InMemoryReviewStore,
    ReviewRecord,
    SqliteReviewStore,
    build_review_store,
    pipeline_review_id,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryReviewStore()
    return SqliteReviewStore(str(tmp_path / "reviews.db"))


def _rev(**over) -> ReviewRecord:
    base = dict(
        review_id=pipeline_review_id("pl-1", "promote"),
        subject_ref="pl-1/promote", title="Approve promote",
        pipeline_id="pl-1", step_id="promote",
        gate_snapshot={"gate": "promote-model", "show": {"eval_acc": 0.41}},
        lineage={"measure": "run_9"},
    )
    return ReviewRecord(**{**base, **over})


async def test_open_is_idempotent_while_open_and_decide_is_final(store):
    opened = await store.open_review(_rev())
    assert opened.status == "open"
    # a re-fire (restart/replay) keeps the ORIGINAL open record
    again = await store.open_review(_rev(gate_snapshot={"gate": "changed"}))
    assert again.gate_snapshot["gate"] == "promote-model"

    decided = await store.decide_review(
        opened.review_id, "approved", reviewer="alice", rationale="gates green")
    assert decided is not None and decided.status == "approved"
    assert decided.reviewer == "alice" and decided.decided_at

    # a decided review is final — no second verdict
    assert await store.decide_review(opened.review_id, "rejected") is None
    got = await store.get_review(opened.review_id)
    assert got is not None and got.status == "approved" and got.rationale == "gates green"


async def test_list_filters_by_status(store):
    await store.open_review(_rev())
    other = _rev(review_id=pipeline_review_id("pl-2", "promote"), pipeline_id="pl-2")
    await store.open_review(other)
    await store.decide_review(other.review_id, "rejected", rationale="halluc gate missed")
    assert len(await store.list_reviews()) == 2
    assert [r.pipeline_id for r in await store.list_reviews(status="open")] == ["pl-1"]
    assert [r.status for r in await store.list_reviews(status="rejected")] == ["rejected"]


async def test_null_store_is_silent_noop(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    null = build_review_store()
    r = await null.open_review(_rev())      # never blocks a gate
    assert r.status == "open"
    assert await null.list_reviews() == []


# ─── bridge lifecycle (gate fire → approve / cancel) ─────────────────────────


async def test_bridge_opens_and_decides_review(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path / 'rev.db'}")
    from tests.core.test_pipeline_bridge import _bridge, _complete, _prep_then

    bridge, disp = _bridge()
    rec = await bridge.start_pipeline(_prep_then("train", approval=True), inputs={})
    await _complete(bridge, disp, "run_1", output={"staged_dataset_uri": "s3://s"})

    store = build_review_store()
    rid = pipeline_review_id(rec.pipeline_id, "train")
    opened = await store.get_review(rid)
    assert opened is not None and opened.status == "open"
    assert opened.lineage.get("prep") == "run_1"          # upstream run refs captured

    await bridge.advance_step(rec.pipeline_id, "train", approver="alice",
                              rationale="metrics look right")
    decided = await store.get_review(rid)
    assert decided is not None and decided.status == "approved"
    assert decided.reviewer == "alice" and decided.rationale == "metrics look right"


async def test_bridge_gate_snapshot_carries_spend_and_budget(tmp_path, monkeypatch):
    """#2358 — a budgeted pipeline paused at a gate: the review's snapshot says what
    the legs have spent so far and the declared cap, so the panel shows cost vs cap."""
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path / 'rev.db'}")
    from tests.core.test_pipeline_bridge import _bridge, _complete, _prep_then

    bridge, disp = _bridge()
    plan = _prep_then("train", approval=True)
    plan.governance = {"budget": {"usd": 5}}
    rec = await bridge.start_pipeline(plan, inputs={})
    disp.store.runs["run_1"].cost = {"total_usd": 1.25}
    await _complete(bridge, disp, "run_1", output={"staged_dataset_uri": "s3://s"})

    got = await build_review_store().get_review(pipeline_review_id(rec.pipeline_id, "train"))
    assert got is not None
    assert got.gate_snapshot["evidence"] == {"cost_usd": 1.25, "cost_cap_usd": 5.0}

    # no declared budget → the spend alone, never an invented cap
    bridge, disp = _bridge()
    rec = await bridge.start_pipeline(_prep_then("train", approval=True), inputs={})
    await _complete(bridge, disp, "run_1", output={"staged_dataset_uri": "s3://s"})
    got = await build_review_store().get_review(pipeline_review_id(rec.pipeline_id, "train"))
    assert got is not None and got.gate_snapshot["evidence"] == {"cost_usd": 0.0}


async def test_bridge_cancel_closes_open_reviews(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path / 'rev.db'}")
    from tests.core.test_pipeline_bridge import _bridge, _complete, _prep_then

    bridge, disp = _bridge()
    rec = await bridge.start_pipeline(_prep_then("train", approval=True), inputs={})
    await _complete(bridge, disp, "run_1", output={"staged_dataset_uri": "s3://s"})
    await bridge.cancel_pipeline(rec.pipeline_id)

    got = await build_review_store().get_review(pipeline_review_id(rec.pipeline_id, "train"))
    assert got is not None and got.status == "rejected"
    assert got.rationale == "pipeline cancelled"


# ─── API (routers/reviews.py) ────────────────────────────────────────────────


class _FakeRunner:
    def __init__(self) -> None:
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(list_skills=lambda: [])

    async def run(self, *a, **k):
        return []


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)   # keyless — no auth
    monkeypatch.setenv("OBS_EVAL_LEDGER_DB", ":memory:")
    db = str(tmp_path / "reg.db")
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{db}")
    seed = SqliteReviewStore(db)
    await seed.open_review(_rev())
    await seed.close()
    from cairn.core.triggers.server import build_app
    app = build_app(_FakeRunner())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_api_list_and_get(client):
    r = await client.get("/reviews")
    assert r.status_code == 200
    rows = r.json()["reviews"]
    assert len(rows) == 1 and rows[0]["status"] == "open"
    rid = rows[0]["review_id"]
    r = await client.get(f"/reviews/{rid}")
    assert r.status_code == 200
    assert r.json()["gate_snapshot"]["gate"] == "promote-model"
    assert (await client.get("/reviews/rev:nope:x")).status_code == 404
    assert (await client.get("/reviews?status=approved")).json()["reviews"] == []
