"""#1376 — redaction must reach the LANGCHAIN path, not just `llm_complete`.

The defect: ADR-0028 documents redaction as the chokepoint before anything
leaves to the vendor, and `llm_complete` implements it — but NOTHING in the
agentic run path calls `llm_complete`. Template prompt nodes, the chat router
and `cairn ask`'s ReAct loop all build a LangChain model via
`build_chat_model` and invoke it directly, so an active `redact.text` binding
redacted nothing on the paths that actually run.

These tests pin the contract at the constructor every one of those callers
shares, and they exercise the REAL LangChain machinery (bind_tools, streaming)
rather than asserting on our own wrapper in isolation — the tool-calling turn
is exactly where unredacted tool RESULTS would egress.
"""
from __future__ import annotations

import asyncio

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from cairn.services.llm.redacting import RedactingChatOpenAI, redact_lc_messages


class _Result:
    def __init__(self, text: str, redacted: bool) -> None:
        self.text, self.redacted, self.spans = text, redacted, []


class _Redactor:
    """Stand-in for a bound redact.text operator."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def redact_text(self, text: str):
        self.seen.append(text)
        return _Result(text.replace("SECRET", "[REDACTED]"), "SECRET" in text)


@pytest.fixture()
def bound(monkeypatch):
    import cairn.core.obs.redact as redact_mod
    r = _Redactor()
    monkeypatch.setattr(redact_mod, "get_redactor", lambda: r)
    return r


@pytest.fixture()
def unbound(monkeypatch):
    import cairn.core.obs.redact as redact_mod
    monkeypatch.setattr(redact_mod, "get_redactor", lambda: redact_mod._NOOP)


# ── the message-level contract ───────────────────────────────────────────────

def test_content_is_redacted_before_egress(bound):
    out = asyncio.run(redact_lc_messages([HumanMessage(content="my SECRET value")]))
    assert out[0].content == "my [REDACTED] value"
    assert bound.seen == ["my SECRET value"]


def test_tool_results_are_redacted(bound):
    """The finding's sharpest edge: tool RESULTS (run records, audit rows,
    knowledge chunks) are what the ReAct loop feeds back to the vendor."""
    out = asyncio.run(redact_lc_messages(
        [ToolMessage(content="rows: SECRET", tool_call_id="c1")]))
    assert out[0].content == "rows: [REDACTED]"


def test_message_class_and_tool_metadata_survive(bound):
    """Rebuilding as a generic message would drop tool_call_id and silently
    break the ReAct loop — copy(), don't reconstruct."""
    out = asyncio.run(redact_lc_messages(
        [ToolMessage(content="SECRET", tool_call_id="call-42")]))
    assert isinstance(out[0], ToolMessage)
    assert out[0].tool_call_id == "call-42"


def test_unbound_is_identity_and_costs_nothing(unbound):
    msgs = [HumanMessage(content="my SECRET value")]
    out = asyncio.run(redact_lc_messages(msgs))
    assert out is msgs, "no binding → the SAME list, not a rebuilt copy"


def test_non_string_content_passes_through_untouched(bound):
    """Multimodal/content-block payloads are passed through rather than
    mangled — a stated limitation, pinned so it stays deliberate."""
    blocks = [{"type": "text", "text": "SECRET"}]
    out = asyncio.run(redact_lc_messages([HumanMessage(content=blocks)]))
    assert out[0].content == blocks


def test_unredacted_message_is_not_copied(bound):
    """Only messages the redactor CHANGED are rebuilt."""
    msgs = [HumanMessage(content="nothing sensitive")]
    out = asyncio.run(redact_lc_messages(msgs))
    assert out[0] is msgs[0]


# ── the factory returns the guarded model ────────────────────────────────────

def test_build_chat_model_returns_the_redacting_model(monkeypatch):
    """The regression that matters: every agentic caller shares this
    constructor, so this assertion covers templates, chat and ask at once."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from cairn.services.llm.factory import build_chat_model

    assert isinstance(build_chat_model("gpt-4o-mini"), RedactingChatOpenAI)


def test_compile_time_placeholder_path_still_works(monkeypatch):
    """#412: the Temporal worker compiles every template at startup, before a
    key may exist. Construction must not require credentials."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from cairn.services.llm.factory import build_chat_model

    assert isinstance(build_chat_model("gpt-4o-mini"), RedactingChatOpenAI)


# ── real LangChain integration: the redaction survives the wiring ───────────

def test_redaction_applies_through_a_real_agenerate(bound, monkeypatch):
    """Drive the actual LangChain call path: what super() receives must
    already be redacted."""
    seen: dict = {}

    async def _fake_super(self, messages, stop=None, run_manager=None, **kw):
        seen["messages"] = messages
        return "ok"

    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI._agenerate", _fake_super, raising=True)
    llm = RedactingChatOpenAI(model="gpt-4o-mini", api_key="sk-test")
    asyncio.run(llm._agenerate([HumanMessage(content="a SECRET thing")]))
    assert seen["messages"][0].content == "a [REDACTED] thing"


def test_bind_tools_keeps_the_guard(bound, monkeypatch):
    """A tool-bound model is a RunnableBinding over the SAME instance — the
    guard must still be the thing that talks to the vendor."""
    seen: dict = {}

    async def _fake_super(self, messages, stop=None, run_manager=None, **kw):
        seen["messages"] = messages
        return "ok"

    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI._agenerate", _fake_super, raising=True)

    def a_tool(x: str) -> str:
        """A tool."""
        return x

    llm = RedactingChatOpenAI(model="gpt-4o-mini", api_key="sk-test")
    bound_llm = llm.bind_tools([a_tool])
    inner = getattr(bound_llm, "bound", None)
    assert isinstance(inner, RedactingChatOpenAI), "bind_tools must not unwrap the guard"
    asyncio.run(inner._agenerate([AIMessage(content="SECRET plan")]))
    assert seen["messages"][0].content == "[REDACTED] plan"


# ── fail loud rather than silently skip ─────────────────────────────────────

def test_sync_invocation_refuses_when_a_redactor_is_bound(bound):
    """Redaction is async; a sync call would skip it. Refuse instead of
    quietly egressing unredacted content."""
    llm = RedactingChatOpenAI(model="gpt-4o-mini", api_key="sk-test")
    with pytest.raises(RuntimeError, match="SYNCHRONOUSLY"):
        llm._generate([HumanMessage(content="SECRET")])


def test_sync_invocation_is_fine_when_unbound(unbound, monkeypatch):
    """No binding → nothing to skip → the sync path stays available."""
    called: list[str] = []
    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI._generate",
        lambda self, messages, stop=None, run_manager=None, **kw: called.append("super") or "ok",
        raising=True)
    llm = RedactingChatOpenAI(model="gpt-4o-mini", api_key="sk-test")
    llm._generate([HumanMessage(content="plain")])
    assert called == ["super"]


# ── #1439: the OpenAI key must not be forced onto other providers ───────────

def test_openai_key_only_injected_for_openai_routed_models():
    from cairn.core.llm.client import _is_openai_model
    assert _is_openai_model("gpt-4o-mini")
    assert _is_openai_model("openai/gpt-4o-mini")
    assert not _is_openai_model("deepseek/deepseek-v4-flash")
    assert not _is_openai_model("anthropic/claude-3-5-haiku")
