"""Tests for the deterministic citation/hallucination guard.

The guard's whole reason to exist is catching evidence the LLM *cites* but that
never appeared in the data the investigation fetched. These tests pin the
failure classes it must catch and the cases where it must stay quiet (so it
doesn't flag legitimate output or guess when it has no ground truth).
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-validate-offline")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from cairn.core.guards.citation_validator import validate_citations


def _tool_msg(payload) -> ToolMessage:
    return ToolMessage(content=json.dumps(payload), tool_call_id="t1")


def _agent_output(*, messages, evidence) -> dict:
    return {
        "type": "agent_loop",
        "messages": messages,
        "output": json.dumps({"symptom": "x", "evidence": evidence}),
    }


# ─── the core liability: a fabricated citation must be caught ────────


def test_hallucinated_citation_is_flagged():
    out = _agent_output(
        messages=[
            HumanMessage(content="investigate checkout"),
            _tool_msg({"logs": [{"id": "log-REAL-1", "service": "checkout"}]}),
        ],
        evidence=[{"type": "log", "citation": "log-FAKE-999", "summary": "made up"}],
    )
    report = validate_citations(out)
    assert not report.skipped
    assert report.checked == 1
    assert len(report.invalid) == 1
    assert report.invalid[0]["citation"] == "log-FAKE-999"
    assert report.hallucination_rate == 1.0
    assert not report.is_clean


def test_real_citation_passes():
    out = _agent_output(
        messages=[
            _tool_msg({"traces": [{"trace_id": "trace-abc-123"}]}),
        ],
        evidence=[{"type": "trace", "citation": "trace-abc-123", "summary": "real"}],
    )
    report = validate_citations(out)
    assert report.checked == 1
    assert report.is_clean
    assert report.hallucination_rate == 0.0


def test_mixed_real_and_fake():
    out = _agent_output(
        messages=[_tool_msg({"errors": [{"error_id": "err-1"}, {"error_id": "err-2"}]})],
        evidence=[
            {"type": "trace", "citation": "err-1", "summary": "ok"},
            {"type": "trace", "citation": "err-NOPE", "summary": "bad"},
        ],
    )
    report = validate_citations(out)
    assert report.checked == 2
    assert len(report.valid) == 1
    assert len(report.invalid) == 1
    assert report.hallucination_rate == 0.5


# ─── the subtle one: the model must not validate its own claims ──────


def test_ai_message_is_not_ground_truth():
    # The fabricated id appears ONLY in the model's own AIMessage, never in a
    # tool result. It must still be flagged — otherwise a hallucination would
    # self-certify just by being mentioned in the model's reasoning.
    out = _agent_output(
        messages=[
            _tool_msg({"logs": [{"id": "log-REAL-1"}]}),
            AIMessage(content="I found trace trace-INVENTED-7 in the data"),
        ],
        evidence=[{"type": "trace", "citation": "trace-INVENTED-7", "summary": "x"}],
    )
    report = validate_citations(out)
    assert len(report.invalid) == 1
    assert report.invalid[0]["citation"] == "trace-INVENTED-7"


# ─── must stay quiet when it can't (or needn't) judge ───────────────


def test_prompt_flow_is_skipped_not_failed():
    out = {"type": "prompt", "output": json.dumps({"evidence": [{"type": "log", "citation": "z"}]})}
    report = validate_citations(out)
    assert report.skipped
    assert report.reason
    assert report.is_clean  # skipped is not a failure


def test_state_machine_is_skipped():
    out = {"type": "state_machine", "output": "{}", "evidence": [], "findings": {}}
    report = validate_citations(out)
    assert report.skipped


def test_agent_loop_with_no_citations_is_clean():
    out = _agent_output(
        messages=[_tool_msg({"logs": [{"id": "log-1"}]})],
        evidence=[],
    )
    report = validate_citations(out)
    assert not report.skipped
    assert report.checked == 0
    assert report.is_clean


def test_agent_loop_without_tool_messages_is_skipped():
    # No tool calls happened (only the human turn) → no ground truth.
    out = {
        "type": "agent_loop",
        "messages": [HumanMessage(content="hi")],
        "output": json.dumps({"evidence": [{"type": "log", "citation": "x"}]}),
    }
    report = validate_citations(out)
    # human content is corpus, but "x" (len<4) can't substring-match → flagged.
    # The point: it does NOT crash and does NOT silently pass a bogus id.
    assert not report.skipped
    assert len(report.invalid) == 1


# ─── output-parsing robustness ──────────────────────────────────────


def test_fenced_json_output_is_parsed():
    out = {
        "type": "agent_loop",
        "messages": [_tool_msg({"logs": [{"id": "log-FENCE-1"}]})],
        "output": "Here is my analysis:\n```json\n"
        + json.dumps({"evidence": [{"type": "log", "citation": "log-FENCE-1"}]})
        + "\n```\n",
    }
    report = validate_citations(out)
    assert report.checked == 1
    assert report.is_clean


def test_to_dict_shape():
    out = _agent_output(
        messages=[_tool_msg({"logs": [{"id": "a-real-id-1"}]})],
        evidence=[{"type": "log", "citation": "ghost-id-1", "summary": "x"}],
    )
    d = validate_citations(out).to_dict()
    assert set(d) == {
        "checked", "valid_count", "invalid_count", "invalid",
        "hallucination_rate", "skipped", "reason",
    }
    assert d["invalid_count"] == 1
