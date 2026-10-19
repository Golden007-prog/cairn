"""Core audit service — interface, resolver, emission seams (ADR-0028).

Auditing is OPTIONAL: unbound → events dropped (null sink); bound → recorded
and non-bypassable. These tests drive the binding via the resolver seam.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.obs import audit as A
from cairn.core.operator import load_all_operators


@pytest.fixture(autouse=True)
def _reset_cache():
    A.reset_audit_sink()
    yield
    A.reset_audit_sink()


class _Recorder:
    """Minimal in-memory AuditSink for assertions."""
    def __init__(self):
        self.events: list[A.AuditEvent] = []

    async def record(self, event: A.AuditEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_default_is_null_drop(monkeypatch):
    monkeypatch.setattr(A, "_bound_provider_name", lambda: None)
    sink = A.get_audit_sink()
    assert isinstance(sink, A.NullAuditSink)
    # emit is a no-op fast path; must not raise.
    await A.audit_emit("tool.call", target="x/y")


@pytest.mark.asyncio
async def test_bound_sink_resolves_via_registry(monkeypatch):
    load_all_operators(strict=False)
    monkeypatch.setattr(A, "_bound_provider_name", lambda: "audit/store")
    A.reset_audit_sink()
    sink = A.get_audit_sink()
    assert not isinstance(sink, A.NullAuditSink)
    assert hasattr(sink, "record")


@pytest.mark.asyncio
async def test_emit_records_when_bound(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(A, "_bound_provider_name", lambda: "audit/store")
    monkeypatch.setattr(A, "get_audit_sink", lambda: rec)
    await A.audit_emit("tool.call", target="github/list", outcome="ok", run_id="r1", n=3)
    assert len(rec.events) == 1
    ev = rec.events[0]
    assert ev.action == "tool.call" and ev.target == "github/list"
    assert ev.outcome == "ok" and ev.run_id == "r1"
    assert ev.metadata == {"n": 3} and ev.ts  # ts filled at emit


@pytest.mark.asyncio
async def test_unknown_binding_fails_open(monkeypatch):
    monkeypatch.setattr(A, "_bound_provider_name", lambda: "does/not-exist")
    A.reset_audit_sink()
    assert isinstance(A.get_audit_sink(), A.NullAuditSink)  # fail-open, not crash


@pytest.mark.asyncio
async def test_emit_is_failsafe_when_sink_raises(monkeypatch):
    class _Boom:
        async def record(self, event):
            raise RuntimeError("sink down")

    monkeypatch.setattr(A, "get_audit_sink", lambda: _Boom())
    # Must swallow — auditing never breaks the audited operation.
    await A.audit_emit("llm.complete", target="gpt-4o-mini")


@pytest.mark.asyncio
async def test_llm_gateway_emits_audit(monkeypatch):
    """llm_complete must emit an audit event for the vendor egress."""
    rec = _Recorder()
    monkeypatch.setattr(A, "get_audit_sink", lambda: rec)

    async def _fake_acompletion(**params):
        class _Msg:
            content = "ok"
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 7})()
        return _Resp()

    import cairn.core.llm.client as client
    monkeypatch.setattr(client, "acompletion", _fake_acompletion)

    await client.llm_complete(
        messages=[{"role": "user", "content": "hi"}], model="gpt-4o-mini",
    )
    actions = [e.action for e in rec.events]
    assert "llm.complete" in actions
    ev = next(e for e in rec.events if e.action == "llm.complete")
    assert ev.metadata.get("prompt_tokens") == 5


@pytest.mark.asyncio
async def test_node_path_fills_ts_when_blank(tmp_path, monkeypatch):
    """#1233 — the node path (template `operation: audit/store`) must honor the
    schema's contract ("ISO timestamp; filled by the sink if blank"): templates
    never pass `ts`, and unstamped rows rendered the console audit WHEN column
    as an em-dash on every node-authored row."""
    import json

    from operator_audit_store.operators.audit_store import AuditStoreOp
    from operator_audit_store.schemas.audit_store import AuditWriteInput

    from cairn.core.operator.protocol import OperationContext

    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("OBS_AUDIT_LOG", str(log))
    op = AuditStoreOp()
    ctx = OperationContext(pipeline_id="p", node_name="n")

    out = await op.run(AuditWriteInput(action="model.eval_gate", outcome="pass"), ctx)
    assert out.ok and out.hash
    row = json.loads(log.read_text().strip().splitlines()[-1])
    assert row["ts"]  # stamped by the sink, not left blank
    # explicit ts still wins (replays / backfills keep their original stamp)
    await op.run(AuditWriteInput(action="x", ts="2026-01-01T00:00:00+00:00"), ctx)
    row = json.loads(log.read_text().strip().splitlines()[-1])
    assert row["ts"] == "2026-01-01T00:00:00+00:00"
