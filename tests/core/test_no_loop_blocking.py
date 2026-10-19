"""blockbuster guard — the in-memory orchestration hot path stays non-blocking.

blockbuster (the async-correctness tool from Langflow's stack) flagged that
several cairn modules do sync I/O on the event loop — SqliteRunStore, the audit
sink, sqlite_vec, dataset-checks file I/O — tracked for to_thread/aiofiles
hardening in an internal issue. This pins the part that is ALREADY clean:
the in-memory control plane (dispatch → run → store reads/writes). If someone
later sneaks a sync file/sqlite call into that hot path, this fails loud.

Uses the opt-in `no_loop_blocking` fixture (conftest); skips without the
`blockbuster` dev-extra.
"""
from __future__ import annotations

import types

import pytest

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.engine.schema import TriggerEvent
from cairn.core.storage import InMemoryRunStore


class _FakeRunner:
    """Returns a result without touching disk — isolates the dispatcher's own
    store/event hot path (the runner's tracing is exercised elsewhere)."""

    async def run(self, event, *, environment=None, event_sink=None,
                  run_id=None, _cascade_depth=0):
        return [types.SimpleNamespace(
            skill_name="s", template_name="t", duration_seconds=0.0, actions=[],
        )]


@pytest.mark.asyncio
async def test_inmemory_dispatch_does_not_block_event_loop(no_loop_blocking):
    store = InMemoryRunStore()
    d = RunDispatcher(_FakeRunner(), store=store)

    inv = await d.submit(
        TriggerEvent(source="pattern", matched_pattern="boom", service="svc"),
        wait=True,
    )
    assert inv.status == "done"

    # The completion read also rides the loop with no sync I/O (in-memory tier).
    got = await store.get(inv.id)
    assert got is not None and got.status == "done"
