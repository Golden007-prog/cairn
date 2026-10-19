"""Workflow graph view — the compiled LangGraph structure for a template.

LangGraph compiles a template to a ``StateGraph``; ``compiled.get_graph()`` yields
the real *executable* structure (gates, fan-out, subgraph nodes) and
``draw_mermaid()`` renders it as a flowchart. cairn's state-machine runnable
already exposes the compiled graph as ``_compiled_graph``
(:mod:`cairn.core.engine.compiler`), so we read it directly — no graph rebuild.

Surface-agnostic: the web renders ``mermaid`` (via Mermaid.js, zero build); the
``nodes``/``edges`` projection is React-Flow-ready if a richer UI is added later.

#187 enrichment — the minimal ``{id, label}`` / ``{source, target, conditional}``
projection is too thin for the console's Workflows canvas (it could only show the
raw langgraph node id, e.g. ``operation_run_train__5``). Each node now also carries
the resolved **operator ref** (``train/qlora``), a clean human **ref** for the
label, and the node **kind** (the runtime :class:`NodeKind`); each edge carries the
**type** of artifact produced by its source node where derivable. These are
re-derived from the compiled spec — the template AST (``flow.root`` + ``flow.nodes``)
gives the per-node operation ref, the binding YAML gives ``binding.operator``, the
registered operator's ``output_schema`` name gives the artifact type, and the
node functions' :func:`tag`-stamped kind gives the kind. All new fields fall back
to ``None`` when a node has no resolvable operator (terminals, synthesize, glue),
and the original fields are unchanged for back-compat.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class GraphNode(BaseModel):
    """One node in a template's workflow graph.

    ``id`` / ``label`` are the original back-compat fields (``label`` is the raw
    langgraph node id, e.g. ``operation_run_train__5``). The #187 additions:

      - ``ref``       — the clean operation/template/action ref the SPA should
                        label the node with (``run-train``, ``dataset-readiness/check``,
                        ``synthesize``). ``None`` when unresolvable (glue nodes,
                        ``__start__``/``__end__``).
      - ``operator``  — the bound operator ref for operation nodes
                        (``train/qlora``, ``cost/gate``). ``None`` for subgraph /
                        action / glue / terminal nodes.
      - ``kind``      — the runtime :class:`cairn.core.runtime.NodeKind` value
                        (``"io"``, ``"llm"``, ``"tool"``, ``"pure"``). ``None``
                        for terminal markers.
    """

    id: str
    label: str
    ref: Optional[str] = None
    operator: Optional[str] = None
    kind: Optional[str] = None
    #: ADR-0082 D8b (#1077) — for a flow-door node (``dataflow/run`` /
    #: ``ml/run``) whose binding names a resolvable ``flow_ref``/``dataflow_ref``,
    #: the ordered KERNEL names of the flow it runs, read from the pack YAML def.
    #: The console expands the node into these stages and overlays live status
    #: from the ``stage`` run events (#1076). ``None`` for every non-flow node —
    #: the expansion is declarative, so it works identically for any engine.
    stages: Optional[list[str]] = None


class GraphEdge(BaseModel):
    """One edge in a template's workflow graph.

    ``source`` / ``target`` / ``conditional`` are the original back-compat fields.
    The #187 addition:

      - ``type`` — the artifact type the *source* node produces, where derivable
                   (an operator's ``output_schema`` class name, e.g. ``TrainResult``).
                   ``None`` when the source node has no resolvable operator output
                   (glue, action, subgraph, terminal).
    """

    source: str
    target: str
    conditional: bool = False
    type: Optional[str] = None


class GraphView(BaseModel):
    """A template's workflow graph, ready for any renderer."""

    template_ref: str
    available: bool = True  # False = prompt/agent_loop flow (no static node graph)
    mermaid: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


def _mermaid_id(name: str) -> str:
    """A Mermaid-safe node id (alphanumerics + underscore)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name) or "node"


# A resolved per-node descriptor: ref + operator ref + produced artifact type.
# kind is read separately off the tagged node fns (authoritative for every node,
# including the synthetic glue/terminal ones the AST walk doesn't see).
class _NodeMeta(BaseModel):
    ref: Optional[str] = None
    operator: Optional[str] = None
    artifact_type: Optional[str] = None
    stages: Optional[list[str]] = None  # flow-door kernels (ADR-0082 D8b, #1077)


# The flow-door operators whose binding names a `flow_ref`/`dataflow_ref` we can
# expand into stages (ADR-0082 D8b). Kept as a mapping so a new engine-neutral
# door only adds one entry — the expansion never hard-codes an engine.
_FLOW_DOOR_REF_KEYS: dict[str, str] = {
    "dataflow/run": "dataflow_ref",
    "ml/run": "flow_ref",
}


def _flow_stages_for_binding(operator_ref: str, binding_input: dict) -> Optional[list[str]]:
    """The ordered kernel names of the flow a door binding runs, or ``None``.

    Reads the LITERAL ``flow_ref``/``dataflow_ref`` from the binding input and
    looks it up in the pack flow defs (``$``-templated refs can't be resolved
    statically → ``None``, the node just doesn't expand). Best-effort: any
    failure yields ``None`` so the graph still renders."""
    ref_key = _FLOW_DOOR_REF_KEYS.get(operator_ref)
    if ref_key is None:
        return None
    ref = binding_input.get(ref_key) if isinstance(binding_input, dict) else None
    if not isinstance(ref, str) or not ref or ref.startswith("$"):
        return None
    try:
        from cairn.core.dataflow.defs import load_flow_defs  # noqa: PLC0415 - flow defs pull the pack registry; keep off the light import path

        d = load_flow_defs().get(ref)
        return [s.stage for s in d.stages] if d is not None else None
    except Exception:  # noqa: BLE001 - expansion is a nicety, never fail the graph
        return None


def _operator_artifact_type(operator_ref: str) -> Optional[str]:
    """The artifact *type* an operator produces — its ``output_schema`` class name.

    Best-effort: returns ``None`` if the operator isn't registered (the compile
    would already have failed loud in that case, so this is belt-and-suspenders)
    or declares no output schema.
    """
    # lazy: the operator registry pulls in the whole operator-pack surface; keep
    # it off the import path of light callers (mirrors build_graph_view's lazies).
    from cairn.core.operator import default_registry as operator_registry

    try:
        op_cls = operator_registry.get(operator_ref)
    except Exception:
        return None
    schema = getattr(op_cls, "output_schema", None)
    return getattr(schema, "__name__", None)


def _build_node_meta(compiled: Any, operations_dir: Optional[Path]) -> dict[str, _NodeMeta]:
    """Walk the compiled template's AST → ``synthetic-name-stem → _NodeMeta``.

    The compiler names operation nodes ``operation_{ref.replace('-','_')}__{N}``,
    subgraph nodes ``subgraph_{template}__{N}``, and action nodes by their bare
    ref (no ``__N`` suffix). We reconstruct those stems from ``flow.root`` /
    ``flow.nodes`` so a graph node id can be matched back to its operation/operator.

    Failure is graceful: anything that can't be resolved (no operations_dir, a
    binding that won't load, an unknown operator) simply yields a ``_NodeMeta``
    with the fields it *could* fill — the caller falls back to the raw label.
    """
    # imports here, not module-top: engine.schema / operator.binding pull the
    # whole compiler/operator surface; build_graph_view already lazy-imports the
    # compiler for the same reason.
    from cairn.core.engine.schema import (
        ActionBTNode,
        BTNode,
        GuardBTNode,
        MapBTNode,
        OperationBTNode,
        ParallelBTNode,
        SelectorBTNode,
        SequenceBTNode,
        StateMachineFlow,
        SubgraphBTNode,
    )
    from cairn.core.engine.workflow_config import resolve_capability_binding
    from cairn.core.operator.binding import load_binding

    meta: dict[str, _NodeMeta] = {}
    flow = getattr(compiled.spec, "flow", None)
    if not isinstance(flow, StateMachineFlow):
        return meta

    def resolve_operation(ref: str) -> _NodeMeta:
        operator_ref: Optional[str] = None
        artifact_type: Optional[str] = None
        stages: Optional[list[str]] = None
        if operations_dir is not None:
            binding_path = operations_dir / f"{ref}.yaml"
            if binding_path.exists():
                try:
                    # #2241 D — a v1 `capability:` file names no operator; show the one the
                    # profile binds, through the compiler's own resolver.
                    binding = resolve_capability_binding(load_binding(binding_path), str(binding_path))
                    operator_ref = binding.operator
                    artifact_type = _operator_artifact_type(binding.operator)
                    stages = _flow_stages_for_binding(binding.operator, binding.input)
                except Exception:  # noqa: BLE001 — best-effort projection; say what was skipped
                    logger.debug("graph view: no operator for %s", binding_path, exc_info=True)
        return _NodeMeta(ref=ref, operator=operator_ref, artifact_type=artifact_type, stages=stages)

    def walk(bt: BTNode) -> None:
        if isinstance(bt, OperationBTNode):
            # synthetic stem mirrors the compiler: operation_{node_ref with - → _}
            stem = f"operation_{bt.node_ref.replace('-', '_')}"
            inline = bt
            if getattr(bt, "capability", None):  # #2241 C inline v1 form — resolve like the compiler
                try:
                    inline = resolve_capability_binding(bt, bt.node_ref)
                except Exception:  # noqa: BLE001 — best-effort projection, like the file form
                    logger.debug("graph view: no operator for inline node %s", bt.node_ref, exc_info=True)
                    inline = None
            if inline is None:  # an unbound v1 node: no operator to show (the compile refuses it)
                meta[stem] = _NodeMeta(ref=bt.node_ref)
                return
            if inline.operator:  # #1107 inline form — no file to resolve
                bt = inline
                meta[stem] = _NodeMeta(
                    ref=bt.node_ref,
                    operator=bt.operator,
                    artifact_type=_operator_artifact_type(bt.operator),
                    stages=_flow_stages_for_binding(bt.operator, bt.input),
                )
                return
            meta[stem] = resolve_operation(bt.ref)
            return
        if isinstance(bt, SubgraphBTNode):
            # subgraph nodes wrap a nested template; no single operator/artifact.
            stem = f"subgraph_{bt.template}"
            meta[stem] = _NodeMeta(ref=bt.template)
            return
        if isinstance(bt, ActionBTNode):
            # action nodes are added under their bare ref (no __N suffix).
            meta[bt.ref] = _NodeMeta(ref=bt.ref)
            return
        if isinstance(bt, MapBTNode):
            walk(bt.body)
            return
        if isinstance(bt, GuardBTNode):
            walk(bt.then)
            if bt.else_ is not None:
                walk(bt.else_)
            return
        if isinstance(bt, (SequenceBTNode, ParallelBTNode, SelectorBTNode)):
            for child in bt.children:
                walk(child)
            return

    walk(flow.root)
    return meta


def _match_meta(node_id: str, meta: dict[str, _NodeMeta]) -> Optional[_NodeMeta]:
    """Resolve a graph node id to its AST descriptor.

    Action nodes match by exact id; operation/subgraph nodes carry a trailing
    ``__{N}`` counter the AST walk can't predict, so we strip it and match the stem.
    """
    if node_id in meta:
        return meta[node_id]
    stem = node_id.rsplit("__", 1)[0]
    return meta.get(stem)


def build_graph_view(template_ref: str) -> Optional[GraphView]:
    """Compile ``template_ref`` and extract its LangGraph structure.

    Returns ``None`` if the template can't be resolved (→ HTTP 404 upstream).
    For ``prompt``/``agent_loop`` flows (no multi-node state machine) returns a
    trivial start→node→end graph with ``available=False``.

    #187: each node also carries its resolved operator ref + clean ref + kind, and
    each edge the artifact type its source node produces (where derivable) — see
    :class:`GraphNode` / :class:`GraphEdge`. Back-compat fields (``id``/``label`` on
    nodes, ``source``/``target``/``conditional`` on edges) are unchanged.
    """
    # lazy: keep the engine/compiler off the import path of light callers
    from cairn.core.engine.compiler import compile_template
    from cairn.core.engine.skill_engine import resolve_template_path
    from cairn.core.runtime import get_kind

    try:
        path = resolve_template_path(template_ref)
    except FileNotFoundError:
        return None
    if path is None:
        return None

    compiled = compile_template(path)
    cg = getattr(compiled.runnable, "_compiled_graph", None)
    if cg is None:
        name = compiled.name
        nid = _mermaid_id(name)
        return GraphView(
            template_ref=template_ref,
            available=False,
            nodes=[{"id": name, "label": name}],
            edges=[],
            mermaid=f"graph TD;\n  __start__([start]) --> {nid}({name});\n  {nid} --> __end__([end]);",
        )

    # §24.20.5: operations/ lives alongside templates/ under the pack root — the
    # same place the compiler resolves operation bindings from. Re-derive it so we
    # can map each operation node id → operator ref + produced artifact type.
    operations_dir = path.parent.parent / "operations"
    if not operations_dir.is_dir():
        operations_dir = None
    node_meta = _build_node_meta(compiled, operations_dir)

    # node-id → produced artifact type, so an edge can carry its source's output.
    artifact_by_node: dict[str, Optional[str]] = {}
    # node-id → tagged NodeKind (authoritative for every node incl. synthetic glue).
    node_fns = getattr(compiled.runnable, "_node_fns", {}) or {}

    g = cg.get_graph()

    nodes: list[dict[str, Any]] = []
    for nid, node in g.nodes.items():
        label = getattr(node, "name", nid) or nid
        m = _match_meta(nid, node_meta)
        fn = node_fns.get(nid)
        kind = get_kind(fn).value if fn is not None else None
        artifact_by_node[nid] = m.artifact_type if m is not None else None
        nodes.append(
            GraphNode(
                id=nid,
                label=label,
                ref=(m.ref if m is not None else None),
                operator=(m.operator if m is not None else None),
                kind=kind,
                stages=(m.stages if m is not None else None),
            ).model_dump()
        )

    edges = [
        GraphEdge(
            source=e.source,
            target=e.target,
            conditional=bool(getattr(e, "conditional", False)),
            type=artifact_by_node.get(e.source),
        ).model_dump()
        for e in g.edges
    ]
    return GraphView(template_ref=template_ref, mermaid=g.draw_mermaid(), nodes=nodes, edges=edges)
