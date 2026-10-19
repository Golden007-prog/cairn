"""Ambient knowledge injection into the run loop (ADR-0061 model C, #509).

A prompt/agent node that declares ``knowledge: [roles]`` should have the bound
corpora composed and prepended to the LLM's context before the call, with the
retrieved chunks recorded as citations. These tests stub retrieval + the LLM so
they stay hermetic (no vector store, no network).
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine import compiler
from cairn.core.engine.schema import PromptFlow


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """Records the messages it was invoked with; returns a canned reply."""

    def __init__(self):
        self.seen: list = []

    async def ainvoke(self, messages):
        self.seen = messages
        return _FakeResp("ok")


class _Cit:
    source_pack = "workspace:default"
    corpus = "handbook"
    source = "spec.md"
    preview = "A compliant README must contain Title, Install, Usage."
    score = 0.9


@pytest.fixture
def fake_llm(monkeypatch):
    llm = _FakeLLM()
    monkeypatch.setattr(compiler, "build_chat_model", lambda *a, **k: llm)
    monkeypatch.setattr(compiler, "_budget_cost_cap", lambda spec: None)
    return llm


async def _stub_compose(roles, query, **kwargs):
    assert roles == ["handbook"]
    return "## Knowledge\n- [workspace:default:handbook] required sections: Title, Install, Usage", [_Cit()]


def _spec(knowledge):
    flow = PromptFlow(type="prompt", model="gpt-4o-mini", knowledge=knowledge)
    return types.SimpleNamespace(flow=flow)


@pytest.mark.asyncio
async def test_prompt_flow_injects_declared_knowledge(fake_llm, monkeypatch):
    monkeypatch.setattr(compiler, "compose_ambient_knowledge", _stub_compose)
    run = compiler._build_prompt(_spec(["handbook"]), "You are a README linter.")

    out = await run({"user_message": "what sections are required?"})

    system_msg = fake_llm.seen[0].content
    assert "You are a README linter." in system_msg
    assert "required sections: Title, Install, Usage" in system_msg  # knowledge injected
    # citations recorded for provenance
    assert out["knowledge_citations"][0]["citation"] == "workspace:default:handbook"
    assert out["knowledge_citations"][0]["source"] == "spec.md"


@pytest.mark.asyncio
async def test_no_knowledge_roles_means_no_injection(fake_llm, monkeypatch):
    # Guard: compose must NOT be called when a node declares no roles.
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "", []

    monkeypatch.setattr(compiler, "compose_ambient_knowledge", _boom)
    run = compiler._build_prompt(_spec([]), "plain system prompt")

    out = await run({"user_message": "hi"})

    assert called["n"] == 0
    assert fake_llm.seen[0].content == "plain system prompt"  # untouched
    assert "knowledge_citations" not in out


async def test_compose_ambient_knowledge_empty_roles_short_circuits():
    # No roles / empty query → no retrieval, additive no-op.
    assert await compiler.compose_ambient_knowledge([], "q") == ("", [])
    assert await compiler.compose_ambient_knowledge(["handbook"], "  ") == ("", [])
