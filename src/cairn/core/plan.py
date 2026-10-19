"""Deterministic plan-assembler (#95 slice 1) — NO LLM.

Given a ``<pack>/<template>`` ref + inputs (+ optional profile), resolve a Plan
from the live registries: the operators the template invokes, their governance
metadata (registered? requires_approval? capabilities?), the profile bindings,
missing pieces, approvals, and the exact command to run. This is the LLM-free
core; the narrow intent→pack LLM step (slice 2) only has to produce the
``template_ref`` + ``inputs`` it feeds here.

Honest gaps (transparent in the Plan): there is no pre-run cost estimator in
cairn today — cost is tracked at runtime and capped via ``max_cost_usd`` /
``BudgetExceeded`` — so ``estimated_cost`` is left null with a note. Full policy
evaluation needs a runtime ActionRequest; the plan surfaces the pack's policies
as *may-apply* warnings rather than evaluating them statically.
"""
from __future__ import annotations
from cairn.core.settings import getenv

from pathlib import Path
from typing import Any, Mapping, Optional

import frontmatter
from pydantic import BaseModel, Field

from cairn.core.engine.workflow_config import collect_template_operators


class OperatorPlan(BaseModel):
    """One operator the template invokes, with its governance metadata."""

    operator: str
    registered: bool
    risk: str = ""
    requires_approval: bool = False
    capabilities: list[str] = Field(default_factory=list)
    mcp_server: str = ""


class CostEstimate(BaseModel):
    min_usd: Optional[float] = None
    max_usd: Optional[float] = None
    note: str = ""


class Plan(BaseModel):
    """The structured plan (#95). Mirrors the spec schema, assembled
    deterministically from cairn's registries + profile."""

    intent: str = "plan"
    selected_pack: str = ""
    template: str = ""
    template_found: bool = False
    inputs: dict[str, Any] = Field(default_factory=dict)
    execution_profile: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    operators: list[OperatorPlan] = Field(default_factory=list)
    selected_operators: dict[str, str] = Field(default_factory=dict)
    estimated_cost: CostEstimate = Field(default_factory=CostEstimate)
    missing_bindings: list[str] = Field(default_factory=list)
    approvals_required: list[str] = Field(default_factory=list)
    policy_warnings: list[str] = Field(default_factory=list)
    recommended_command: str = ""
    can_run: bool = False
    notes: list[str] = Field(default_factory=list)


def _collect_node_capabilities(flow: dict) -> list[str]:
    """Capabilities declared on named flow nodes (agent_loop / prompt tools)."""
    caps: list[str] = []
    for node in (flow.get("nodes") or {}).values():
        if isinstance(node, dict):
            caps.extend(node.get("capabilities") or [])
    return caps


def _resolve_template(template_ref: str):
    """Return (pack, template_path) or (None, None).

    No longer yields an ``operations_dir``: operator discovery goes through
    :func:`collect_template_operators` (#1178), so nothing here needs to know
    that bindings can live in files at all.
    """
    from cairn.core.pack.registry import flow_dirs_of, get_pack, get_packs

    pack_name, _, tmpl = template_ref.partition("/")
    pack = get_pack(pack_name) if tmpl else None
    if pack is None:
        # ref may be a bare template name — search all packs
        for p in get_packs():
            if any((Path(d) / f"{template_ref}.md").exists() for d in flow_dirs_of(p)):
                pack, tmpl = p, template_ref
                break
    # #1163 — `flows/` AND the `templates/` alias; checking only templates_dir
    # made every migrated pack unresolvable, so `cairn ask` / the contract API
    # answered "template not found" for flows that exist.
    dirs = flow_dirs_of(pack) if pack is not None else []
    if not dirs:
        return None, None
    path = next((c for d in dirs if (c := Path(d) / f"{tmpl}.md").exists()),
                Path(dirs[0]) / f"{tmpl}.md")
    return pack, path


def build_plan(
    template_ref: str,
    inputs: Optional[dict[str, Any]] = None,
    *,
    profile: Optional[str] = None,
    manifests: Optional[Mapping[str, Any]] = None,
) -> Plan:
    """Assemble a deterministic Plan for ``<pack>/<template>``.

    ``manifests`` is :func:`discover_operator_pack_manifests` already read by a
    caller planning MANY refs (#2360 — the installed-workflows listing re-walked
    every ``pack.toml`` once per ref); ``None`` reads it here, as before."""
    inputs = dict(inputs or {})
    plan = Plan(selected_pack=template_ref.partition("/")[0], inputs=inputs)

    pack, path = _resolve_template(template_ref)
    if pack is None or not path or not path.exists():
        plan.notes.append(f"template {template_ref!r} not found in any registered pack")
        plan.can_run = False
        return plan
    plan.selected_pack = pack.name
    plan.template = path.stem
    plan.template_found = True

    meta = frontmatter.load(path).metadata
    flow = meta.get("flow") or {}

    # 1. operators the template invokes — via the ONE canonical walker (#1178).
    #    The private walk this replaced resolved `operations/<ref>.yaml` only and
    #    recursed `children` only, so it went EMPTY for inline-binding packs
    #    (#1109) and had always missed operations inside guard/map branches.
    #    Because the Playground derives `gpu_operators` from this list, an empty
    #    plan marked GPU workflows sandbox-eligible.
    op_names: list[str] = list(collect_template_operators(template_ref))
    if not op_names:
        plan.notes.append(
            f"no operators resolved for {template_ref!r} — the flow declares none, "
            f"or its bindings are unreadable"
        )

    # 2. enrich each operator from the registry
    from cairn.core.operator.loader import discover_operator_pack_manifests

    if manifests is None:
        manifests = discover_operator_pack_manifests()
    seen: set[str] = set()
    caps: list[str] = list(meta.get("tools", {}).get("required") or [])
    caps.extend(_collect_node_capabilities(flow))
    for name in op_names:
        if name in seen:
            continue
        seen.add(name)
        entry = manifests.get(name)
        registered = entry is not None
        op = OperatorPlan(
            operator=name,
            registered=registered,
            risk=(entry.risk or "") if entry else "",
            requires_approval=bool(entry.requires_approval) if entry else False,
            capabilities=list(entry.provides_capabilities or []) if entry else [],
            mcp_server=(entry.mcp_server or "") if entry else "",
        )
        plan.operators.append(op)
        if not registered:
            plan.missing_bindings.append(f"operator {name!r} is not registered/installed")
        if op.requires_approval:
            plan.approvals_required.append(
                f"operator {name!r} requires approval (risk={op.risk or 'n/a'})"
            )
        for c in op.capabilities:
            plan.selected_operators.setdefault(c, name)
            caps.append(c)

    plan.required_capabilities = sorted(set(caps))

    # 3. profile + missing substrate bindings (reuses doctor's resolution shape)
    bindings = _profile_bindings(profile)
    plan.execution_profile = profile or _active_profile_name()
    from cairn.core.operator import default_registry

    for cap in plan.required_capabilities:
        # substrate capabilities (dotted, e.g. memory.search) resolve via the
        # profile binding; if bound but the operator isn't registered, flag it.
        if cap in bindings:
            plan.selected_operators.setdefault(cap, bindings[cap])
            if not default_registry.has(bindings[cap]):
                plan.missing_bindings.append(
                    f"capability {cap!r} → {bindings[cap]!r} (not registered)"
                )

    # 4. policies that may apply (pack-declared) — static surface, not evaluated
    plan.policy_warnings = _pack_policy_names(pack)

    # 5. cost — honest: no pre-run estimator; capped at runtime
    plan.estimated_cost = CostEstimate(
        note="no pre-run estimate; cost is tracked at runtime and capped via "
             "max_cost_usd (BudgetExceeded). Set a budget to enforce a ceiling."
    )

    # 6. command + can_run
    plan.recommended_command = _recommended_command(plan)
    plan.can_run = not plan.missing_bindings
    if plan.approvals_required:
        plan.notes.append(
            "this run will PAUSE for approval at the gated step(s) above"
        )
    return plan


def _profile_bindings(profile: Optional[str]) -> dict[str, str]:
    try:
        if profile:
            from pathlib import Path as _P

            from cairn.core.pack.profile import load_profile
            return load_profile(_P(profile)).bindings
        from cairn.core.pack.profile import load_profile_from_env
        return load_profile_from_env().bindings
    except Exception:  # noqa: BLE001 — no profile bound = no substrate bindings
        return {}


def _active_profile_name() -> str:
    import os
    return getenv("CAIRN_PROFILE", "") or "(none)"


def _pack_policy_names(pack: Any) -> list[str]:
    pdir = getattr(pack, "policies_dir", None)
    if not pdir or not Path(pdir).is_dir():
        return []
    return sorted(p.stem for p in Path(pdir).glob("*.yaml"))


def _recommended_command(plan: Plan) -> str:
    cmd = f"cairn run {plan.selected_pack}/{plan.template}"
    if plan.inputs:
        cmd += " --input <input.json>"
    if plan.execution_profile and plan.execution_profile not in ("(none)", ""):
        cmd += f"  # profile: {plan.execution_profile}"
    return cmd
