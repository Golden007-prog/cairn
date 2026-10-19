"""§24.3 — mcp_tool executor.

Covers four surfaces:

  1. Manifest schema: the [operator."<n>".mcp] sub-table parses, and an
     operator declaring the mcp_tool executor without it fails loud.
  2. Executor registry: mcp_tool is in the implemented set and pick_executor
     selects it when an operator declares it.
  3. Dispatch: invoke_operator_mcp_tool resolves the manifest server/tool,
     calls the substrate MCP client (stubbed), validates the result against
     output_schema, and applies the output binding.
  4. Routing: invoke_operator() picks mcp_tool and routes to the branch.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
from pydantic import BaseModel

from cairn.core.operator.binding import OperationBinding
from cairn.core.operator.protocol import Operation
from cairn.core.operator import executor as ex
from cairn.core.operator import runtime as rt
from cairn.core.operator.registry import OperatorRegistry, default_registry
from cairn.core.pack.manifest import ManifestError, OperatorEntry, load_manifest


# ─── manifest schema ─────────────────────────────────────────────────


def _write_pack(tmp_path, operator_block: str):
    body = f"""
[pack]
name        = "obs-operator-demo"
version     = "0.1.0"
type        = "operator"
kind        = "operator"
publisher   = "agentops"
trust_level = "builtin"

{operator_block}
"""
    p = tmp_path / "pack.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_mcp_sub_table_parses(tmp_path):
    p = _write_pack(tmp_path, """
[operator."slack/post-message"]
version   = "0.1.0"
executors = ["mcp_tool"]
risk      = "external_write"

[operator."slack/post-message".mcp]
server = "slack"
tool   = "chat_postMessage"
""")
    manifest = load_manifest(p)
    entry = manifest.operators["slack/post-message"]
    assert entry.executors == ["mcp_tool"]
    assert entry.mcp_server == "slack"
    assert entry.mcp_tool == "chat_postMessage"


def test_mcp_tool_executor_without_mapping_is_loud(tmp_path):
    p = _write_pack(tmp_path, """
[operator."slack/post-message"]
version   = "0.1.0"
executors = ["mcp_tool"]
""")
    with pytest.raises(ManifestError, match=r"mcp_tool.*missing.*mcp"):
        load_manifest(p)


def test_mcp_sub_table_unknown_key_rejected(tmp_path):
    p = _write_pack(tmp_path, """
[operator."slack/post-message"]
version   = "0.1.0"
executors = ["mcp_tool"]

[operator."slack/post-message".mcp]
server  = "slack"
tool    = "chat_postMessage"
channel = "#oops"
""")
    with pytest.raises(ManifestError, match=r"unknown key.*mcp.*channel"):
        load_manifest(p)


def test_in_process_operator_needs_no_mcp_mapping(tmp_path):
    # The loud-failure must only fire for mcp_tool — in_process stays free.
    p = _write_pack(tmp_path, """
[operator."local/thing"]
version   = "0.1.0"
executors = ["in_process"]
""")
    manifest = load_manifest(p)
    assert manifest.operators["local/thing"].mcp_server is None


# ─── executor registry ───────────────────────────────────────────────


def test_mcp_tool_is_implemented():
    assert ex.is_implemented("mcp_tool")
    assert "mcp_tool" in ex.implemented_executors()


# ─── dispatch fixtures ───────────────────────────────────────────────


class PostMessageInput(BaseModel):
    channel: str
    text: str


class PostMessageOutput(BaseModel):
    ok: bool
    ts: Optional[str] = None
    channel: Optional[str] = None


class PostMessageOp(Operation[PostMessageInput, PostMessageOutput]):
    name = "slack/post-message"
    version = "0.1.0"
    input_schema = PostMessageInput
    output_schema = PostMessageOutput

    def run(self, inp, ctx):  # never called via mcp_tool
        raise AssertionError("mcp_tool operator's run() must not be invoked")


class _FakeMcpClient:
    def __init__(self, result: Any):
        self._result = result
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, server: str, tool: str, args: dict) -> Any:
        self.calls.append((server, tool, args))
        return self._result


@pytest.fixture
def registered_mcp_op():
    """Register PostMessageOp + its mcp_tool metadata on the default
    registry, then clean both out so the global registry isn't polluted."""
    name = PostMessageOp.name
    default_registry.register(PostMessageOp)
    default_registry.attach_metadata(OperatorEntry(
        name=name,
        version="0.1.0",
        executors=["mcp_tool"],
        risk="external_write",
        mcp_server="slack",
        mcp_tool="chat_postMessage",
    ))
    yield PostMessageOp
    default_registry._operators.pop(name, None)
    default_registry._metadata.pop(name, None)


def _binding() -> OperationBinding:
    return OperationBinding(
        operator="slack/post-message",
        input={"channel": "$input.channel", "text": "$input.text"},
        output={"ts": "$state.posted_ts", "ok": "$state.post_ok"},
    )


# ─── pick_executor ───────────────────────────────────────────────────


def test_pick_executor_selects_mcp_tool(registered_mcp_op):
    assert ex.pick_executor(PostMessageOp) == "mcp_tool"


# ─── invoke_operator_mcp_tool ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_tool_dispatch_happy_path(registered_mcp_op, monkeypatch):
    fake = _FakeMcpClient({"ok": True, "ts": "1700000000.1", "channel": "C123"})
    monkeypatch.setattr(
        "cairn.services.mcp.client.get_mcp_client", lambda: fake,
    )
    state = {"template_input": {"channel": "#incidents", "text": "p99 spike"}}
    patch = await rt.invoke_operator_mcp_tool(
        _binding(), PostMessageOp,
        state=state,
        pipeline_id="p1", node_name="post",
    )
    # The substrate client was called with the resolved input as args.
    assert fake.calls == [("slack", "chat_postMessage",
                           {"channel": "#incidents", "text": "p99 spike"})]
    # Output mapped from the validated MCP result into the state patch.
    assert patch == {"posted_ts": "1700000000.1", "post_ok": True}


@pytest.mark.asyncio
async def test_mcp_tool_missing_metadata_is_loud(monkeypatch):
    # Registered as an operator but with NO mcp metadata attached.
    reg = OperatorRegistry()
    reg.register(PostMessageOp)
    monkeypatch.setattr(
        "cairn.core.operator.registry.default_registry", reg,
    )
    with pytest.raises(rt.BindingEvalError, match=r"no \[operator.*mcp\]"):
        await rt.invoke_operator_mcp_tool(
            _binding(), PostMessageOp,
            state={"template_input": {"channel": "c", "text": "t"}},
            pipeline_id="p1", node_name="post",
        )


@pytest.mark.asyncio
async def test_mcp_tool_result_validated_against_output_schema(registered_mcp_op, monkeypatch):
    # Tool returns a shape that doesn't satisfy PostMessageOutput (ok missing).
    fake = _FakeMcpClient({"not": "the right shape"})
    monkeypatch.setattr(
        "cairn.services.mcp.client.get_mcp_client", lambda: fake,
    )
    with pytest.raises(rt.BindingEvalError, match=r"output_schema"):
        await rt.invoke_operator_mcp_tool(
            _binding(), PostMessageOp,
            state={"template_input": {"channel": "c", "text": "t"}},
            pipeline_id="p1", node_name="post",
        )


@pytest.mark.asyncio
async def test_mcp_tool_mock_skips_call(registered_mcp_op, monkeypatch):
    def _boom():
        raise AssertionError("mock path must not touch the MCP client")
    monkeypatch.setattr("cairn.services.mcp.client.get_mcp_client", _boom)
    patch = await rt.invoke_operator_mcp_tool(
        _binding(), PostMessageOp,
        state={"template_input": {"channel": "c", "text": "t"}},
        pipeline_id="p1", node_name="post",
        is_mocked=True,
    )
    assert patch == {}


# ─── invoke_operator routes to the branch ────────────────────────────


@pytest.mark.asyncio
async def test_invoke_operator_routes_mcp_tool(registered_mcp_op, monkeypatch):
    fake = _FakeMcpClient({"ok": True, "ts": "42.0", "channel": "C9"})
    monkeypatch.setattr(
        "cairn.services.mcp.client.get_mcp_client", lambda: fake,
    )
    patch = await rt.invoke_operator(
        _binding(), PostMessageOp,
        state={"template_input": {"channel": "#x", "text": "hi"}},
        pipeline_id="p1", node_name="post",
    )
    assert fake.calls and fake.calls[0][1] == "chat_postMessage"
    assert patch == {"posted_ts": "42.0", "post_ok": True}
