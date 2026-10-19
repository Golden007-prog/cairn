"""Per-workflow config composition (ADR-0070, #748).

A workflow author thinks in *workflows*, not operators-across-an-environment. This
module composes the config surface for a template: it resolves the workflow's
**reachable operators** (walk the behavior tree + ``subgraph_node`` children →
each ``OperationBTNode``'s operator) and unions their ``config_schema``s into one
surface, each knob partitioned ``governance`` vs ``operational`` (ADR-0070). That
surface backs ``cairn config workflow <ref>``, the console Workflow → Configuration
tab, and (later slices) the stored ``WorkflowConfig``.

Read-only + pure over the installed packs — no run, no I/O beyond reading template
frontmatter + operation bindings (the same files the compiler reads).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def collect_flow_operators(flow_path: Any, *, follow_subgraphs: bool = True) -> list[str]:
    """Every operator ONE flow file can invoke — the canonical walker (#1178).

    Reads ``flow_path``'s frontmatter and walks its behavior tree, taking each
    ``OperationBTNode``'s operator from the node itself (``operator:``, the
    #1107 inline form) or from its binding file (``operations/<ref>.yaml``, the
    ~17 shared survivors). Recurses guard branches, map bodies, and — when
    ``follow_subgraphs`` — child templates.

    THIS IS THE ONE WALKER. Three readers grew private copies of it and two
    drifted: after the #1109 inline conversions they still resolved refs only,
    so ``cairn ask`` reported zero operators and — because the Playground
    derives ``gpu_operators`` from that list — a GPU workflow was marked
    sandbox-eligible (#1178). Copies of a walk cannot be kept in sync by
    discipline; use this.
    """
    return _collect(flow_path, by_ref=False, follow_subgraphs=follow_subgraphs)


def resolve_capability_binding(binding: Any, site: str) -> Any:
    """#2241 C (D5, ADR-0111 D10) — a v1 operation names a capability; the active profile's
    ``[profile.bindings]`` entry for the capability ID names the implementation, which must
    ``implements`` the exact revision. Missing or mismatched bindings refuse; there is no
    fallback provider. Returns the binding in its legacy ``operator`` form (``configuration``
    merged under ``input``) so the node, elaboration, validation and gates run unchanged."""
    capability = getattr(binding, "capability", None)
    if not capability:
        return binding
    from cairn.core.operator import default_registry  # noqa: PLC0415 — operator stack off the import path
    from cairn.core.operator.binding import BindingError  # noqa: PLC0415 — as above
    from cairn.core.pack.profile import load_profile_from_env  # noqa: PLC0415 — engine → pack is an allowed edge

    capability_id = capability[len("contract://"):].rsplit("/", 1)[0]
    name = load_profile_from_env().bindings.get(capability_id)
    if not name:
        raise BindingError(f"{site}: capability {capability!r} has no [profile.bindings] entry for "
                           f"{capability_id!r}; no fallback provider is chosen")
    op_cls = default_registry.get(name)
    if capability not in getattr(op_cls, "implements", ()):
        raise BindingError(f"{site}: the profile binds {capability_id!r} to {name!r}, which implements "
                           f"{list(getattr(op_cls, 'implements', ()))}, not {capability!r}")
    return binding.model_copy(update={"operator": name, "capability": None, "configuration": {},
                                      "input": {**binding.configuration, **binding.input}})


def collect_template_operation_nodes(template_ref: str) -> list[tuple[str, str]]:
    """#2241 C — every operation node a template (and its subgraph children) binds, as
    ``(node path, operator name)``: a child's nodes are prefixed by the subgraph node's
    ref (``sub/child-node``) so the leg's recorded entries key one node each."""
    nodes: list[tuple[str, str]] = []
    _collect(template_ref, by_ref=True, follow_subgraphs=True, nodes=nodes)
    return nodes


def collect_template_flow_stages(template_ref: str) -> list[tuple[str, list[tuple[str, bool]]]]:
    """ADR-0118 D3 — every ``dataflow/run``/``ml/run`` node a template (and its subgraph
    children) binds, as ``(node path, [(stage name, gated), …])`` for the flow the node
    STATICALLY reaches: its inline ``stages`` list or a literal ``dataflow_ref``/``flow_ref``.
    An entry whose stage name is not static is ``("", False)``; a node whose flow is wired
    at run time reaches nothing here (an opted-in stage it submits then refuses, M2a)."""
    found: list[tuple[str, list[tuple[str, bool]]]] = []
    _collect(template_ref, by_ref=True, follow_subgraphs=True, nodes=[], flow_stages=found)
    return found


def _static_flow_stages(binding: Any) -> list[tuple[str, bool]] | None:
    """The stage list a flow-door binding names statically, or ``None``."""
    inp = getattr(binding, "input", None) or {}
    ref = inp.get("dataflow_ref") if binding.operator == "dataflow/run" else inp.get("flow_ref")
    if isinstance(ref, str) and ref and not ref.startswith("$"):
        from cairn.core.dataflow.defs import flow_def_for  # noqa: PLC0415 — cycle: defs → pack registry

        d = flow_def_for(ref)
        return [(s.stage, bool(s.gate)) for s in d.stages] if d is not None else None
    stages = inp.get("stages")
    if not isinstance(stages, list):
        return None
    return [(s["stage"], bool(s.get("gate"))) if isinstance(s, dict) and isinstance(s.get("stage"), str)
            and not s["stage"].startswith("$") else ("", False) for s in stages]


def collect_template_operators(template_ref: str) -> list[str]:
    """Every operator a template — and its ``subgraph_node`` children — can invoke.

    Resolves ``<pack>/<template>`` to a path, then delegates to
    :func:`collect_flow_operators`. Mirrors the compiler's own
    ``_collect_state_keys`` walk (§24.22) so the two can never disagree about
    what a template reaches.
    """
    return _collect(template_ref, by_ref=True, follow_subgraphs=True)


def _collect(target: Any, *, by_ref: bool, follow_subgraphs: bool,
             nodes: list[tuple[str, str]] | None = None,
             flow_stages: list[tuple[str, list[tuple[str, bool]]]] | None = None) -> list[str]:
    """Shared implementation for the two public collectors above."""
    import frontmatter  # noqa: PLC0415 — optional-at-base parser, used by the loader too

    from cairn.core.engine.schema import (  # noqa: PLC0415 — engine schema
        GuardBTNode,
        MapBTNode,
        OperationBTNode,
        StateMachineFlow,
        SubgraphBTNode,
        TemplateSpec,
    )
    from cairn.core.engine.skill_engine import resolve_template_path  # noqa: PLC0415
    from cairn.core.operator import load_binding  # noqa: PLC0415

    ops: set[str] = set()
    seen: set[tuple[str, str]] = set()

    def _walk_template(ref: Any, prefix: str = "") -> None:
        key = str(ref)
        # #2241 C (D4): a child reached under two prefixes is two sets of entries, so
        # dedupe per (prefix, template); a template on its own prefix chain is recursion.
        if (prefix, key) in seen or (nodes is not None and key in prefix.split("/")[:-2]):
            return  # seen on this path, or this template is already on its own chain (recursion)
        if nodes is None and key in {k for _, k in seen}:
            return  # operator survey: one visit per template is enough
        seen.add((prefix, key))
        try:
            # `ref` is a "<pack>/<template>" string on the by-ref path (and for
            # every subgraph child); a Path when a caller hands us a flow file.
            path = resolve_template_path(ref) if isinstance(ref, str) else Path(ref)
            spec = TemplateSpec.model_validate(frontmatter.load(path).metadata)
        except Exception:  # noqa: BLE001 — a missing/broken child template isn't fatal to the survey
            return
        flow = spec.flow
        if not isinstance(flow, StateMachineFlow):
            return
        operations_dir = path.parent.parent / "operations"

        def _walk(bt: Any) -> None:
            if isinstance(bt, OperationBTNode):
                name = bt.operator  # #1107 inline form — the operator is right here
                binding = None
                if not name:
                    bp = operations_dir / f"{bt.ref}.yaml"
                    try:
                        binding = bt.inline_binding() if bt.capability else (load_binding(bp) if bp.exists() else None)
                        binding = resolve_capability_binding(binding, str(bp)) if binding else None
                        name = binding.operator if binding else ""
                    except Exception:  # noqa: BLE001 — skip an unparseable/unbound binding, keep surveying
                        name = ""
                if name:
                    ops.add(name)
                    if nodes is not None:
                        nodes.append((prefix + bt.node_ref, name))
                if flow_stages is not None and name in ("dataflow/run", "ml/run"):
                    try:
                        stages = _static_flow_stages(binding or bt.inline_binding())
                    except Exception:  # noqa: BLE001 — an unreadable flow fails where it fails today, not here
                        stages = None
                    if stages:
                        flow_stages.append((prefix + bt.node_ref, stages))
                return
            if isinstance(bt, SubgraphBTNode):
                if follow_subgraphs:
                    _walk_template(bt.template, prefix + bt.template + "/")
                return
            if isinstance(bt, GuardBTNode):
                _walk(bt.then)
                if bt.else_ is not None:
                    _walk(bt.else_)
                return
            if isinstance(bt, MapBTNode):
                _walk(bt.body)
                return
            for child in getattr(bt, "children", None) or []:
                _walk(child)

        _walk(flow.root)

    _walk_template(target)
    return sorted(ops)


def compose_workflow_config(template_ref: str) -> dict[str, dict[str, Any]]:
    """The composed config surface for a workflow.

    ``{operator: {"governance": [knob…], "operational": [knob…]}}`` across every
    reachable operator that declares a ``config_schema`` (operators with no tunable
    knobs are omitted). Each knob is ``{path, scope, title, description, default}``
    (see ``describe_config_surface``). This is the union the workflow config form /
    ``cairn config workflow`` renders — operational knobs editable per workflow/run,
    governance knobs env-owned + read-only (ADR-0070).
    """
    from cairn.core.operator import load_all_operators  # noqa: PLC0415
    from cairn.core.operator.config import partition_by_scope  # noqa: PLC0415
    from cairn.core.operator.registry import default_registry  # noqa: PLC0415

    load_all_operators(strict=False)
    out: dict[str, dict[str, Any]] = {}
    for op_name in collect_template_operators(template_ref):
        if not default_registry.has(op_name):
            continue
        schema = getattr(default_registry.get(op_name), "config_schema", None)
        if schema is None:
            continue
        out[op_name] = partition_by_scope(schema)
    return out


def compose_pipeline_config(steps: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """The composed config surface **across a bundle pipeline's steps** (ADR-0069/0070).

    ``steps`` is ``[(step_id, template_ref), …]`` (a step's template is its ``pack/template``
    ref). Returns one entry per step — ``{step_id, ref, operators}`` where ``operators`` is
    that step's :func:`compose_workflow_config` surface — so the console/CLI can show ONE
    config view spanning every pack in the pipeline, each step's knobs grouped and
    governance/operational-partitioned. Steps whose operators declare no tunable knobs come
    back with an empty ``operators`` map (kept, so the step still shows in the view).

    Saved config still lives per-workflow (keyed by the step's ``template_ref`` in the
    ``WorkflowConfigStore``) and is applied when the PipelineBridge runs the step via
    ``run_template`` — this is the composed VIEW, not a separate store.
    """
    return [
        {"step_id": step_id, "ref": ref, "operators": compose_workflow_config(ref)}
        for step_id, ref in steps
    ]
