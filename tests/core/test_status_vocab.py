"""Run-status vocabulary normalization (#651).

The `cairn ask` agent guesses common status synonyms ("completed", "success",
"in_progress"); cairn's canonical vocab is done/failed/cancelled/running/queued/
awaiting_approval. Unmapped, `status=completed` matched 0 rows even with `done`
runs → the agent wrongly reported "no runs". Assert the normalizer maps synonyms
and that the /runs endpoint applies it before hitting the store.
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.triggers.server import _normalize_run_status, build_app


def test_synonyms_map_to_canonical():
    assert _normalize_run_status("completed") == "done"
    assert _normalize_run_status("SUCCESS") == "done"
    assert _normalize_run_status("succeeded") == "done"
    assert _normalize_run_status("error") == "failed"
    assert _normalize_run_status("canceled") == "cancelled"
    assert _normalize_run_status("in_progress") == "running"
    assert _normalize_run_status("pending") == "queued"


def test_canonical_and_unknown_pass_through():
    for s in ("done", "failed", "cancelled", "running", "queued", "awaiting_approval"):
        assert _normalize_run_status(s) == s
    assert _normalize_run_status("weird-custom") == "weird-custom"  # unknown untouched
    assert _normalize_run_status(None) is None
    assert _normalize_run_status("") == ""


class _Runner:
    def __init__(self) -> None:
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(list_skills=lambda: [])

    async def run(self, *a, **k):
        return []


@pytest.mark.asyncio
async def test_runs_endpoint_normalizes_status_before_store(monkeypatch):
    """GET /runs?status=completed reaches the run store as `done` (#651)."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    from httpx import ASGITransport, AsyncClient

    from cairn.core.dispatch.dispatcher import RunDispatcher

    seen: dict = {}

    async def _spy(self, *, lane=None, status=None, workspace_id=None, limit=100):
        seen["status"] = status
        return []

    monkeypatch.setattr(RunDispatcher, "list", _spy)

    app = build_app(_Runner(), enable_scheduler=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs", params={"status": "completed"})
    assert r.status_code == 200
    assert seen.get("status") == "done", seen
