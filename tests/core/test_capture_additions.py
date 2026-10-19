"""#476 capture additions — additive fields on the obs capture layer.

Covers:
  - ``record_llm``'s new #476 kwargs (finish_reason/cached_tokens/
    time_to_first_token_ms/schema_valid): appear when supplied, default to
    None otherwise (no consumer breakage — old 4-positional-arg call sites
    keep working).
  - ``record_mcp``'s new #476 kwargs (success/error_type/tool_result_used):
    same additive contract.
  - ``compute_tool_result_used`` heuristic: used / ignored / no-following-call.
  - OpenInference span-attribute mapping in ``telemetry.span`` — same spans,
    extra attributes, behind the same tracing-enabled guard; a fake tracer
    stands in for a real OTLP exporter (matches the existing
    ``test_observability_phase1.py`` pattern — no real network/OTLP).

Hermetic: no real network, no real OTLP export.
"""
from __future__ import annotations

from contextlib import contextmanager

from cairn.core.obs import base as B
from cairn.core.obs import cost as C
from cairn.core.obs import telemetry as T


# ─── record_llm: additive fields ────────────────────────────────────

def test_record_llm_new_fields_default_to_none():
    with B.cost_scope("t") as ctx:
        C.record_llm("gpt-4o-mini", 10, 5, 12.0)
        call = ctx.llm_calls[-1]
    assert call["finish_reason"] is None
    assert call["cached_tokens"] is None
    assert call["time_to_first_token_ms"] is None
    assert call["schema_valid"] is None
    # Existing fields untouched.
    assert call["model"] == "gpt-4o-mini"
    assert call["in"] == 10
    assert call["out"] == 5


def test_record_llm_new_fields_populate_when_supplied():
    with B.cost_scope("t") as ctx:
        C.record_llm(
            "gpt-4o-mini", 10, 5, 12.0,
            finish_reason="stop",
            cached_tokens=3,
            time_to_first_token_ms=250.5,
            schema_valid=True,
        )
        call = ctx.llm_calls[-1]
    assert call["finish_reason"] == "stop"
    assert call["cached_tokens"] == 3
    assert call["time_to_first_token_ms"] == 250.5
    assert call["schema_valid"] is True


def test_record_llm_noop_outside_request_scope():
    # No cost_scope/start() open — must not raise (existing contract).
    C.record_llm("gpt-4o-mini", 10, 5, 12.0, finish_reason="stop")


# ─── record_mcp: additive fields ────────────────────────────────────

def test_record_mcp_new_fields_default_to_none():
    with B.cost_scope("t") as ctx:
        B.record_mcp("slack.post", 42.0, 128)
        call = ctx.mcp_calls[-1]
    assert call["success"] is None
    assert call["error_type"] is None
    assert call["tool_result_used"] is None
    assert call["tool"] == "slack.post"
    assert call["ms"] == 42.0
    assert call["bytes"] == 128


def test_record_mcp_new_fields_populate_when_supplied():
    with B.cost_scope("t") as ctx:
        B.record_mcp("slack.post", 42.0, 128, success=False, error_type="TimeoutError", tool_result_used=False)
        call = ctx.mcp_calls[-1]
    assert call["success"] is False
    assert call["error_type"] == "TimeoutError"
    assert call["tool_result_used"] is False


def test_record_mcp_noop_outside_request_scope():
    B.record_mcp("slack.post", 1.0, 1, success=True)  # must not raise


# ─── compute_tool_result_used heuristic ─────────────────────────────

def test_tool_result_used_true_when_result_referenced():
    result_text = '{"weather": "72F sunny"}'
    next_prompt = 'system: use context\nuser: the tool said {"weather": "72F sunny"}, summarize'
    assert B.compute_tool_result_used(result_text, next_prompt) is True


def test_tool_result_used_false_when_ignored():
    result_text = '{"weather": "72F sunny"}'
    next_prompt = "user: what is 2+2?"
    assert B.compute_tool_result_used(result_text, next_prompt) is False


def test_tool_result_used_none_when_no_following_call():
    assert B.compute_tool_result_used('{"weather": "72F sunny"}', None) is None


def test_tool_result_used_none_when_no_result_text():
    assert B.compute_tool_result_used(None, "user: hello") is None


def test_tool_result_used_none_when_both_missing():
    assert B.compute_tool_result_used(None, None) is None


def test_tool_result_used_none_for_empty_string_result():
    # Empty string is falsy — "no capturable result" reads as unknown, not False.
    assert B.compute_tool_result_used("", "user: hello") is None


# ─── OpenInference span attributes (#476, ADR-0059 D4) ──────────────
# Fake tracer/span, matching test_observability_phase1.py's pattern — no
# real OTLP exporter, no network.

class _FakeSpan:
    def __init__(self):
        self.attrs: dict = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v


class _FakeTracer:
    def __init__(self):
        self.last_span: _FakeSpan | None = None

    @contextmanager
    def start_as_current_span(self, name):
        s = _FakeSpan()
        self.last_span = s
        yield s


def test_openinference_kind_llm_complete(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(T, "_tracer", tracer)
    with T.span("llm.complete", {"gen_ai.request.model": "claude-opus-4-8"}) as s:
        assert s is not None
    assert s.attrs["openinference.span.kind"] == "LLM"
    assert s.attrs["llm.model_name"] == "claude-opus-4-8"


def test_openinference_kind_cairn_operator_is_chain(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(T, "_tracer", tracer)
    with T.span("cairn.operator", {"operator": "dataset_checks/pii"}) as s:
        pass
    assert s.attrs["openinference.span.kind"] == "CHAIN"


def test_openinference_kind_mcp_prefixed_is_tool(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(T, "_tracer", tracer)
    with T.span("mcp.slack.post", {"tool": "slack.post"}) as s:
        pass
    assert s.attrs["openinference.span.kind"] == "TOOL"
    assert s.attrs["tool.name"] == "slack.post"


def test_openinference_kind_absent_for_unknown_span_name(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(T, "_tracer", tracer)
    with T.span("some.other.span", {}) as s:
        pass
    assert "openinference.span.kind" not in s.attrs


def test_openinference_input_output_value_mirrors_gen_ai_content(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(T, "_tracer", tracer)
    with T.span("llm.complete", {"gen_ai.prompt": "hello"}) as s:
        pass
    assert s.attrs["input.value"] == "hello"
    assert s.attrs["input.mime_type"] == "application/json"
    # Completion content is set post-hoc (after the span starts) by client.py
    # via set_span_attr_mirrored — exercised in the next test.


def test_openinference_no_attrs_when_tracing_off(monkeypatch):
    monkeypatch.setattr(T, "_tracer", None)
    with T.span("llm.complete", {"gen_ai.prompt": "hello"}) as s:
        assert s is None  # unchanged no-op contract


def test_set_span_attr_mirrored_sets_both_keys():
    s = _FakeSpan()
    T.set_span_attr_mirrored(s, "gen_ai.completion", "the answer", openinference_key="output.value")
    assert s.attrs["gen_ai.completion"] == "the answer"
    assert s.attrs["output.value"] == "the answer"


def test_set_span_attr_mirrored_noop_for_none_value():
    s = _FakeSpan()
    T.set_span_attr_mirrored(s, "gen_ai.response.finish_reason", None)
    assert "gen_ai.response.finish_reason" not in s.attrs


def test_set_span_attr_mirrored_noop_for_none_span():
    # Must not raise when tracing is off (s is None).
    T.set_span_attr_mirrored(None, "gen_ai.response.finish_reason", "stop")


def test_set_span_attr_mirrored_without_openinference_key():
    s = _FakeSpan()
    T.set_span_attr_mirrored(s, "llm.schema_valid", True)
    assert s.attrs["llm.schema_valid"] is True


# ─── end-to-end: llm_complete wires #476 fields live off litellm usage ──

async def test_llm_complete_wires_finish_reason_cached_tokens_schema_valid(monkeypatch):
    """The real call path (core/llm/client.py) is out of core/obs/ scope but
    is the only place these values are observable — verify it actually
    populates the new record_llm kwargs from a litellm-shaped response,
    rather than leaving them accepted-but-unused."""
    import cairn.core.llm.client as llm_client

    tracer = _FakeTracer()
    monkeypatch.setattr(T, "_tracer", tracer)

    async def _fake_acompletion(**params):
        class _Details:
            cached_tokens = 4

        class _Usage:
            prompt_tokens = 20
            completion_tokens = 6
            total_tokens = 26
            prompt_tokens_details = _Details()

        class _Msg:
            content = '{"ok": true}'
            tool_calls = None

        class _Choice:
            message = _Msg()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()

        return _Resp()

    monkeypatch.setattr(llm_client, "acompletion", _fake_acompletion)

    with B.cost_scope("t") as ctx:
        await llm_client.llm_complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-5",
            response_format={"type": "json_object"},
        )
        call = ctx.llm_calls[-1]

    assert call["finish_reason"] == "stop"
    assert call["cached_tokens"] == 4
    assert call["schema_valid"] is True
    assert call["time_to_first_token_ms"] is None  # best-effort-None: no streaming wrapper wired


async def test_llm_complete_schema_valid_none_without_response_format(monkeypatch):
    import cairn.core.llm.client as llm_client

    monkeypatch.setattr(T, "_tracer", None)  # tracing off — irrelevant to this check

    async def _fake_acompletion(**params):
        class _Usage:
            prompt_tokens = 5
            completion_tokens = 2
            total_tokens = 7
            prompt_tokens_details = None

        class _Msg:
            content = "plain text answer"
            tool_calls = None

        class _Choice:
            message = _Msg()
            finish_reason = "stop"

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()

        return _Resp()

    monkeypatch.setattr(llm_client, "acompletion", _fake_acompletion)

    with B.cost_scope("t") as ctx:
        await llm_client.llm_complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-5",
        )
        call = ctx.llm_calls[-1]

    # No response_format requested → schema_valid stays None (not applicable),
    # not False.
    assert call["schema_valid"] is None
    assert call["cached_tokens"] is None
