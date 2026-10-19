"""Render a compiled template's plan — textual (``run --dry-run``) and Mermaid
(``cairn visualize``, §24.16 / ADR-0016).

We do NOT hand-roll a Mermaid renderer: the state-machine graph is a LangGraph
``CompiledStateGraph``, so we delegate to its native ``get_graph().draw_mermaid()``
(ADR-0016 decision). The compiler stashes the compiled graph on the runnable
(``run._compiled_graph``); ``prompt`` / ``agent_loop`` flows have no graph and
render as a single step.
"""
from __future__ import annotations

from typing import Any, Optional

_TERMINALS = ("__start__", "__end__")


def _compiled_graph(compiled: Any) -> Optional[Any]:
    """The LangGraph drawable graph, or None for non-state-machine flows."""
    cg = getattr(getattr(compiled, "runnable", None), "_compiled_graph", None)
    if cg is None:
        return None
    try:
        return cg.get_graph()
    except Exception:  # noqa: BLE001 — never let visualization break a command
        return None


def template_mermaid(compiled: Any) -> str:
    """Mermaid text for the compiled template (stdout-pasteable into GitHub/PRs)."""
    graph = _compiled_graph(compiled)
    if graph is None:
        # prompt / agent_loop — a single conceptual step, not a DAG.
        return (
            "flowchart LR\n"
            f"    START([start]) --> S[\"{compiled.name}<br/>({compiled.flow_type})\"]\n"
            "    S --> END([end])\n"
        )
    return graph.draw_mermaid()


def template_plan_lines(compiled: Any) -> list[str]:
    """Concise textual plan for ``run --dry-run`` — identity + the node DAG,
    no execution, no side effects."""
    lines = [
        f"template : {compiled.name} v{compiled.version}",
        f"hash     : {compiled.short_hash}",
        f"flow     : {compiled.flow_type}",
    ]
    graph = _compiled_graph(compiled)
    if graph is None:
        lines.append("plan     : single-step flow (no graph)")
        return lines

    nodes = [n for n in graph.nodes if n not in _TERMINALS]
    edges = [
        (e.source, e.target)
        for e in getattr(graph, "edges", [])
        if e.source not in _TERMINALS and e.target not in _TERMINALS
    ]
    lines.append(f"plan     : {len(nodes)} node(s), {len(edges)} edge(s) — DRY RUN, nothing executed")
    for n in nodes:
        lines.append(f"  • {n}")
    return lines
