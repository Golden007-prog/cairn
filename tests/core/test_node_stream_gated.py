"""#183 — `adrive` on the gated/checkpointed path, against a REAL LangGraph graph.

`tests/core/test_node_stream.py` pins the `adrive` contract with a hand-rolled
fake. This module proves the actual LangGraph 1.x behavior the fix depends on,
so the fake can't silently drift from reality:

  - a gated parent graph whose FIRST node embeds a gated child (compiled WITHOUT
    its own checkpointer, ainvoke'd under the PARENT's config — the
    `subgraph_node_fn` mechanism, #104) emits **zero** parent-namespace node
    updates when the child interrupts: the parent stream collapses to a single
    `__interrupt__`. This is the #183 bug.
  - `adrive` (which streams `subgraphs=True`) surfaces the inner nodes anyway,
    so each completed node lights the canvas, and the parent `__interrupt__`
    still surfaces for the HITL pause path (ADR-0034). The returned terminal
    value matches `ainvoke`.
"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from cairn.core.runtime.node_stream import adrive, node_sink_scope


class _S(TypedDict, total=False):
    steps: list


def _appender(name: str):
    def _fn(state: _S) -> _S:
        steps = list(state.get("steps") or [])
        steps.append(name)
        return {"steps": steps}

    return _fn


def _build_gated_child():
    """A child template with a gate, compiled WITHOUT its own checkpointer —
    mirrors `embedded_child` in compiler.subgraph_node_fn (#104)."""

    def _gate(_state: _S) -> _S:
        interrupt({"operator": "model/register", "risk": "high"})
        return {}

    g = StateGraph(_S)
    g.add_node("c_prepare", _appender("c_prepare"))
    g.add_node("c_check", _appender("c_check"))
    g.add_node("c_gate", _gate)
    g.add_edge(START, "c_prepare")
    g.add_edge("c_prepare", "c_check")
    g.add_edge("c_check", "c_gate")
    g.add_edge("c_gate", END)
    return g.compile()


def _build_gated_parent():
    """Parent graph whose first node embeds the gated child under the parent's
    config (so the child interrupt pauses the parent), then more parent nodes.
    Compiled WITH a checkpointer, like a gated template (ADR-0034)."""
    child = _build_gated_child()

    async def _subgraph_node(state: _S, config=None) -> _S:
        raw = await child.ainvoke({"steps": list(state.get("steps") or [])}, config)
        return {"steps": raw.get("steps", [])}

    g = StateGraph(_S)
    g.add_node("subgraph_child", _subgraph_node)
    g.add_node("op_after", _appender("op_after"))
    g.add_edge(START, "subgraph_child")
    g.add_edge("subgraph_child", "op_after")
    g.add_edge("op_after", END)
    return g.compile(checkpointer=MemorySaver())


@pytest.mark.asyncio
async def test_real_parent_stream_collapses_to_interrupt_without_subgraphs():
    """Documents the bug: a plain parent-namespace updates stream sees ONLY
    __interrupt__ when an embedded child interrupts (zero node events)."""
    graph = _build_gated_parent()
    config = {"configurable": {"thread_id": "no-subgraphs"}}
    parent_updates: list[str] = []
    async for mode, chunk in graph.astream(
        {"steps": []}, config, stream_mode=["updates", "values"]
    ):
        if mode == "updates" and isinstance(chunk, dict):
            parent_updates.extend(chunk.keys())
    # the bug: no "subgraph_child" node-done, only the interrupt.
    assert parent_updates == ["__interrupt__"]


@pytest.mark.asyncio
async def test_adrive_emits_inner_node_events_and_surfaces_interrupt():
    """The fix: adrive (subgraphs=True) emits a node event per completed inner
    node AND surfaces __interrupt__, matching ainvoke's terminal value."""
    graph = _build_gated_parent()
    config = {"configurable": {"thread_id": "with-adrive"}}
    seen: list[str] = []

    async def sink(node: str, output: object = None) -> None:
        seen.append(node)

    with node_sink_scope(sink):
        out = await adrive(graph, {"steps": []}, config)

    # inner child nodes light up even though the parent node never completes.
    assert seen == ["c_prepare", "c_check"]
    assert "__interrupt__" in out  # HITL pause path (ADR-0034) intact

    # ainvoke parity: same terminal value (minus the streamed node events).
    inv = await graph.ainvoke({"steps": []}, {"configurable": {"thread_id": "inv"}})
    assert out.get("steps") == inv.get("steps")
    assert ("__interrupt__" in out) == ("__interrupt__" in inv)


@pytest.mark.asyncio
async def test_adrive_no_sink_is_ainvoke_on_real_gated_graph():
    """No sink bound → byte-identical ainvoke on the real gated graph (no
    streaming, durable/CLI paths unchanged)."""
    graph = _build_gated_parent()
    config = {"configurable": {"thread_id": "no-sink"}}
    out = await adrive(graph, {"steps": []}, config)
    inv = await graph.ainvoke({"steps": []}, {"configurable": {"thread_id": "no-sink-inv"}})
    assert out.get("steps") == inv.get("steps")
    assert "__interrupt__" in out and "__interrupt__" in inv
