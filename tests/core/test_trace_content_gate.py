"""Trace content-capture gate (#247).

Prompt/completion content only leaves to the trace backend (Langfuse/Sentry)
through this gate: OBS_TRACE_CONTENT off | redacted (default) | raw. `redacted`
is fail-closed — no content is emitted unless an active redactor scrubs it.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import pytest

from cairn.core.obs import redact as R
from cairn.core.obs import telemetry as T

pytestmark = pytest.mark.asyncio

SECURE = "src/cairn/profiles/secure.toml"


async def test_mode_default_and_parsing(monkeypatch):
    monkeypatch.delenv("OBS_TRACE_CONTENT", raising=False)
    assert T.trace_content_mode() == "redacted"  # safe default
    monkeypatch.setenv("OBS_TRACE_CONTENT", "off")
    assert T.trace_content_mode() == "off"
    monkeypatch.setenv("OBS_TRACE_CONTENT", "RAW")
    assert T.trace_content_mode() == "raw"
    monkeypatch.setenv("OBS_TRACE_CONTENT", "bogus")
    assert T.trace_content_mode() == "redacted"  # unknown → safe default


async def test_off_omits_content(monkeypatch):
    monkeypatch.setenv("OBS_TRACE_CONTENT", "off")
    assert await T.trace_content("bob@example.com") is None


async def test_raw_passes_through(monkeypatch):
    monkeypatch.setenv("OBS_TRACE_CONTENT", "raw")
    assert await T.trace_content("hello bob@example.com") == "hello bob@example.com"


async def test_empty_is_none(monkeypatch):
    monkeypatch.setenv("OBS_TRACE_CONTENT", "raw")
    assert await T.trace_content("") is None


async def test_redacted_fails_closed_without_redactor(monkeypatch):
    """Default posture: no redactor bound → content OMITTED, never leaked raw."""
    monkeypatch.setenv("OBS_TRACE_CONTENT", "redacted")
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    R.reset_redactor()
    try:
        assert not R.redactor_active()
        assert await T.trace_content("bob@example.com") is None
    finally:
        R.reset_redactor()


async def test_redacted_scrubs_pii_with_active_redactor(monkeypatch):
    from cairn.core.operator import load_all_operators

    load_all_operators(strict=False)
    monkeypatch.setenv("OBS_TRACE_CONTENT", "redacted")
    monkeypatch.setenv("OBS_PROFILE", SECURE)
    R.reset_redactor()
    try:
        assert R.redactor_active()
        out = await T.trace_content("email me at bob@example.com please")
        assert out is not None
        assert "bob@example.com" not in out
        assert "[REDACTED:email]" in out
    finally:
        R.reset_redactor()
