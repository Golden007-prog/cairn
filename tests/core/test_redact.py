"""Core redaction service — interface, resolver, chokepoint (ADR-0028).

Redaction is OPTIONAL: unbound → no-op pass-through; bound → applied and
non-bypassable. These tests drive the binding via the resolver seam.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.obs import redact as R
from cairn.core.operator import load_all_operators


@pytest.fixture(autouse=True)
def _reset_cache():
    R.reset_redactor()
    yield
    R.reset_redactor()


@pytest.mark.asyncio
async def test_default_is_noop_passthrough(monkeypatch):
    monkeypatch.setattr(R, "_bound_provider_name", lambda: None)
    r = R.get_redactor()
    assert isinstance(r, R.NoOpRedactor)
    res = await r.redact_text("contact bob@example.com")
    assert res.text == "contact bob@example.com" and not res.redacted


@pytest.mark.asyncio
async def test_redact_messages_is_identity_when_unbound(monkeypatch):
    monkeypatch.setattr(R, "_bound_provider_name", lambda: None)
    msgs = [{"role": "user", "content": "email a@b.com"}]
    out = await R.redact_messages(msgs)
    assert out is msgs  # same object — zero cost when off


@pytest.mark.asyncio
async def test_bound_redactor_resolves_via_registry(monkeypatch):
    load_all_operators(strict=False)
    monkeypatch.setattr(R, "_bound_provider_name", lambda: "redact/text")
    R.reset_redactor()
    r = R.get_redactor()
    assert not isinstance(r, R.NoOpRedactor)
    res = await r.redact_text("email bob@example.com")
    assert res.redacted and "[REDACTED:email]" in res.text


@pytest.mark.asyncio
async def test_redact_messages_redacts_when_bound(monkeypatch):
    load_all_operators(strict=False)
    monkeypatch.setattr(R, "_bound_provider_name", lambda: "redact/text")
    R.reset_redactor()
    out = await R.redact_messages(
        [{"role": "user", "content": "key sk-abcdefghij0123456789klmno"}]
    )
    assert "[REDACTED:openai_key]" in out[0]["content"]
    assert "sk-abcdefghij0123456789klmno" not in out[0]["content"]


@pytest.mark.asyncio
async def test_unknown_binding_fails_open(monkeypatch):
    monkeypatch.setattr(R, "_bound_provider_name", lambda: "does/not-exist")
    R.reset_redactor()
    assert isinstance(R.get_redactor(), R.NoOpRedactor)  # fail-open, not crash


@pytest.mark.asyncio
async def test_llm_gateway_chokepoint_redacts_before_send(monkeypatch):
    """llm_complete must pass redacted messages to the underlying provider."""
    load_all_operators(strict=False)
    monkeypatch.setattr(R, "_bound_provider_name", lambda: "redact/text")
    R.reset_redactor()

    captured: dict = {}

    async def _fake_acompletion(**params):
        captured.update(params)
        class _Msg:
            content = "ok"
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        return _Resp()

    import cairn.core.llm.client as client
    monkeypatch.setattr(client, "acompletion", _fake_acompletion)

    await client.llm_complete(
        messages=[{"role": "user", "content": "user is bob@example.com"}],
        model="gpt-4o-mini",
    )
    sent = captured["messages"][0]["content"]
    assert "[REDACTED:email]" in sent and "bob@example.com" not in sent
