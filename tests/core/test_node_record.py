"""#1865 — the shared per-node recorder both durable runtimes call.

A DBOS run had no per-node trace and no heartbeat: ``run_template`` executes on
DBOS's background loop, where the dispatcher's contextvar sink is invisible.
``record_node_event`` is the one write the Temporal activity and the DBOS
workflow now share: node event + heartbeat stamp + bus publish, all best-effort.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.runtime.node_record import record_node_event
from cairn.core.settings import masked_dsn
from cairn.core.storage import build_storage
from cairn.core.storage.protocols import Run, TriggerEvent


async def _store_with_run(run_id="r-1865"):
    store = build_storage().runs
    await store.create(Run(id=run_id, trigger=TriggerEvent(source="manual", payload={}), status="running"))
    return store


async def test_record_node_event_appends_node_event_and_heartbeat():
    store = await _store_with_run()
    await record_node_event("r-1865", "classify", {"category": "downstream"}, store=store)
    events = await store.get_events("r-1865")
    node = [e for e in events if e.type == "node"]
    assert len(node) == 1 and node[0].data.get("node") == "classify", events
    assert node[0].data.get("output") == {"category": "downstream"}
    run = await store.get("r-1865")
    assert run.last_heartbeat_at, "a completed node must stamp last_heartbeat_at (#1865)"


async def test_record_node_event_never_raises_on_store_failure():
    class Broken:
        async def append_event(self, *a, **k): raise RuntimeError("db down")
        async def update(self, *a, **k): raise RuntimeError("db down")
    await record_node_event("r-x", "n", {"a": 1}, store=Broken())  # must not raise


def test_masked_dsn_hides_the_password_only():
    """#1867 — the DBOS boot log printed the Postgres password."""
    assert masked_dsn("postgres://cairn:s3cr3t@postgres:5432/cairn_dbos") == "postgres://cairn:***@postgres:5432/cairn_dbos"
    assert "s3cr3t" not in masked_dsn("postgresql+asyncpg://cairn:s3cr3t@h/db?sslmode=require")
    assert masked_dsn("sqlite:////tmp/dbos.sqlite") == "sqlite:////tmp/dbos.sqlite"
    assert masked_dsn("postgres://cairn@h/db") == "postgres://cairn@h/db"
