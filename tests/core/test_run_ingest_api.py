"""#2025 — the `http` job channel's two routes: a worker's events UP
(``POST /runs/{id}/events/ingest``, idempotent) and control DOWN
(``GET /runs/{id}/control`` long-poll, ``POST /runs/{id}/control``)."""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core.operator import report_ingest as ri
from cairn.core.triggers.server import build_app


class _Runner:
    """A run that stays LIVE for the whole test — a worker only long-polls
    control while its run is running; a finished run answers at once."""

    async def run(self, *a, **k):
        import asyncio
        await asyncio.sleep(20)
        return {"summary": "ok"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)  # keyless
    monkeypatch.setenv("OBS_JOB_CHANNEL", "http")
    ri._HWM.clear()
    app = build_app(_Runner(), enable_scheduler=False)
    with TestClient(app) as c:
        yield c


def _seed_run(client) -> str:
    resp = client.post("/triggers/manual", json={"skill_name": "db-pool", "wait": False})
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"]


def _batch(node: str, seqs: range) -> dict:
    return {"events": [{"type": "metrics", "data": {"node": node, "seq": s, "step": s, "series": {"loss": 1.0 / s}}}
                       for s in seqs]}


def test_ingest_is_idempotent_and_acks_the_high_water_mark(client):
    run_id = _seed_run(client)
    r = client.post(f"/runs/{run_id}/events/ingest", json=_batch("train", range(1, 11)))
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 10 and r.json()["last_seq"] == {"train": 10}
    # the worker's replay after a reconnect
    r = client.post(f"/runs/{run_id}/events/ingest", json=_batch("train", range(6, 16)))
    assert r.json()["accepted"] == 5 and r.json()["dropped"] == 5 and r.json()["last_seq"] == {"train": 15}
    evs = [e for e in client.get(f"/runs/{run_id}/events").json()["events"] if e["type"] == "metrics"]
    assert [e["data"]["seq"] for e in evs] == list(range(1, 16))


def test_ingest_refuses_lifecycle_events_by_name(client):
    run_id = _seed_run(client)
    r = client.post(f"/runs/{run_id}/events/ingest", json={"events": [{"type": "done", "data": {"node": "n", "seq": 1}}]})
    assert r.status_code == 200 and r.json()["accepted"] == 0 and "done" in r.json()["refused"][0]
    assert client.post(f"/runs/{run_id}/events/ingest", json={"events": "nope"}).status_code == 422
    assert client.post("/runs/no-such-run/events/ingest", json={"events": []}).status_code == 404


def test_control_round_trip_and_long_poll(client):
    run_id = _seed_run(client)
    assert client.get(f"/runs/{run_id}/control").json()["control"] is None
    assert client.post(f"/runs/{run_id}/control", json={"action": "pause"}).status_code == 422

    def later():
        time.sleep(0.2)
        client.post(f"/runs/{run_id}/control", json={"action": "checkpoint-and-stop", "by": "console"})

    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    r = client.get(f"/runs/{run_id}/control", params={"wait_s": 5})
    assert r.status_code == 200, r.text
    ctl = r.json()["control"]
    assert ctl and ctl["action"] == "checkpoint-and-stop" and ctl["by"] == "console"
    assert time.monotonic() - t0 < 4.5  # returned on arrival, not at the deadline
    # the worker acks by seq: nothing newer
    assert client.get(f"/runs/{run_id}/control", params={"after_seq": ctl["seq"]}).json()["control"] is None
