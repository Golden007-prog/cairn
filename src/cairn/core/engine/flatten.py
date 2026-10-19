"""Flatten-emitter primitives (#569 / #581, ADR-0066 Phase 2).

Cross-pack composition (a ``subgraph_node``, or a bundle-pipeline step) is fixed
under Temporal by **inlining the child template's nodes as TOP-LEVEL parent
nodes** instead of running the child via one nested ``ainvoke`` inside a single
activity. Then each child node stays its own activity and a child gate's
``interrupt()`` bubbles to the workflow's signal loop, instead of being
sandboxed inside that one activity (the #569 loop). The same emitter compiles
``[[bundle.pipeline]]`` steps (#581).

This module holds the pure-logic building blocks the graph-splice assembles:

- :func:`namespace_child_bt` — rewrite the child's **action** refs with a unique
  prefix. ``_add_action_nodes`` registers action nodes *by ref* (``synthesize``),
  so a child ``synthesize`` would collide with the parent's. Operation nodes get
  unique names from the compiler's ``fresh()`` counter and their refs also
  address ``operations/<ref>.yaml``, so operation refs are left untouched.
- :func:`flatten_entry_update` — the entry-adapter's state update: merge the
  child's mapped inputs into ``template_input`` (where ``$input.<field>``
  resolves at runtime — see ``operator/runtime.py``) so a flattened child's
  operations see the parent-supplied value under the child's own field names.
- :func:`flatten_exit_update` — the exit-adapter's state update: map the child's
  outputs into parent state, findings-aware (top-level then ``findings.<field>``,
  the #585 convention that deterministic operators write to ``state.findings.*``).
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from cairn.core.engine.schema import (
    ActionBTNode,
    GuardBTNode,
    ParallelBTNode,
    SelectorBTNode,
    SequenceBTNode,
)


def namespace_child_bt(root: Any, nodes_map: dict[str, Any], prefix: str) -> tuple[Any, dict[str, Any]]:
    """Return ``(new_root, new_nodes_map)`` with every action ref prefixed.

    Deep-copies the tree so the child's original spec is untouched (a child
    template may be composed by several parents). Only ``ActionBTNode.ref`` and
    its ``nodes_map`` key are renamed; operation/subgraph/python refs are left
    alone (operation nodes are already uniquely named by the compiler)."""
    new_root = copy.deepcopy(root)
    renamed: dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, ActionBTNode):
            old = node.ref
            new = f"{prefix}__{old}"
            node.ref = new
            if old in nodes_map:
                renamed[new] = nodes_map[old]
        elif isinstance(node, (SequenceBTNode, ParallelBTNode, SelectorBTNode)):
            for child in node.children:
                walk(child)
        elif isinstance(node, GuardBTNode):
            walk(node.then)
            if node.else_ is not None:
                walk(node.else_)
        # OperationBTNode / SubgraphBTNode / PythonBTNode: refs untouched.

    walk(new_root)
    return new_root, renamed


def flatten_entry_update(state: dict, in_map: Optional[dict[str, str]]) -> dict:
    """Entry-adapter update: scope the child's inputs into ``template_input``.

    ``in_map`` is ``{child_field: parent_state_key}`` (``SubgraphBTNode.inputs``).
    The value is pulled from parent state (mirroring the legacy
    ``subgraph_node_fn`` ``state.get(parent_key, "")``) and placed under the
    child's field name, so the child's ``$input.<child_field>`` resolves. The
    parent's own ``template_input`` keys are preserved (merge, not replace)."""
    ti = dict(state.get("template_input") or {})
    if in_map:
        for child_field, parent_key in in_map.items():
            ti[child_field] = state.get(parent_key, "")
    return {"template_input": ti, "run_id": state.get("run_id", "")}


def flatten_exit_update(state: dict, out_map: Optional[dict[str, str]]) -> dict:
    """Exit-adapter update: map the child's outputs into parent state.

    ``out_map`` is ``{child_output_field: parent_state_key}``
    (``SubgraphBTNode.outputs``). Resolves each child field top-level first, then
    ``findings.<field>`` — deterministic operators write to ``state.findings.*``
    (dataset/gate → ``findings.verdict``; #585), so a top-level key of that name
    often doesn't exist."""
    update: dict[str, Any] = {"last_action_status": "success"}
    if out_map:
        findings = state.get("findings")
        if not isinstance(findings, dict):
            findings = {}
        mirror: dict[str, Any] = {}
        for child_field, parent_key in out_map.items():
            if child_field in state:
                value = state[child_field]
            elif child_field in findings:
                value = findings[child_field]
            else:
                continue
            update[parent_key] = value
            # #600: also mirror the mapped output under findings.<parent_key>. A
            # top-level channel write from this PURE exit node does NOT survive
            # flatten+Temporal (the composed dataset_verdict came back "unknown"
            # in the FinetuneReport/lineage/card though the gate decided it),
            # whereas findings is a merge channel that threads reliably. Consumers
            # that need the value cross-runtime read $state.findings.<parent_key>.
            mirror[parent_key] = value
        if mirror:
            update["findings"] = mirror
    return update


__all__ = ["namespace_child_bt", "flatten_entry_update", "flatten_exit_update"]
