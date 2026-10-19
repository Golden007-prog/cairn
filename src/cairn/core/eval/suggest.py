"""Suggestion engine (#478, ADR-0012 Form 4) — clusters recurring labeled
failures, routes each cluster to the responsible optimizer *category* by
failure_type, and emits a governed :class:`Suggestion` (evidence + routed
target) that a human accepts or rejects. Diagnosis is automated; **actuation is
always human-gated** (the ADR-0012 line — never self-modification).

Rule-based + explainable (no LLM in the detection path). The routing below is
the ADR-0012 §24.12 / issue-#478 table: failure_type → optimizer category →
the versioned artifact the fix would land on.

⚠️ TRUST GATE (#458): suggestions are only as honest as the verdicts they
aggregate. Until the honest-eval fix (#458) lands, generation is OFF by default
(``CAIRN_SUGGEST=on`` to enable) and every suggestion carries ``untrusted_metric``
in its evidence — a suggestion from a lying metric optimizes the lie.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import os
from typing import Any

from cairn.core.workspace import current_workspace
from cairn.core.eval.cluster import cluster_failures
from cairn.core.storage.suggestions import Suggestion

#: failure_type → (category, target-kind, human template). Target-kind names the
#: versioned artifact a fix lands on; the concrete target interpolates the wf.
_ROUTING: dict[str, tuple[str, str, str]] = {
    "generation_failure": ("prompt_quality", "template", "Tighten the prompt/format contract for {wf} (new template version)"),
    "performance_failure": ("model_routing", "model_role", "Re-route {wf} to a cheaper/faster model role (#352 registry)"),
    "retrieval_failure": ("rag_config", "rag", "Tune retrieval for {wf} (corpus / top_k / chunking)"),
    "tool_failure": ("tool_binding", "operator", "Fix the tool binding for {wf} (operator description / schema / exclusion)"),
    "quality_failure": ("dataset", "eval_set", "Add {wf} failing cases to the eval set + few-shot library (#473)"),
    "input_failure": ("input_validation", "schema", "Harden {wf}'s input validation / schema"),
    "safety_failure": ("policy_guard", "policy", "Review {wf}'s policy guard (a safety gate is firing)"),
    "eval_failure": ("eval_recipe", "eval_recipe", "Fix {wf}'s eval recipe (the scorer itself is erroring)"),
}
#: Fallback for unrouted / recurring incidents → propose a regression golden.
_FALLBACK = ("regression_suite", "evals", "Add a regression golden for {wf} (recurring {ft})")


def suggestions_enabled() -> bool:
    """Generation is OFF by default (gated on #458 — see module docstring)."""
    return getenv("CAIRN_SUGGEST", "").strip().lower() in ("1", "on", "true", "yes")


def cluster_fingerprint(cluster: dict[str, Any]) -> str:
    """The dedupe key — a cluster's (failure_type|locus|signature). A suggestion
    is emitted once per fingerprint; a rejected one never re-surfaces (#478)."""
    return f"{cluster.get('failure_type','')}|{cluster.get('locus','')}|{cluster.get('signature','')}"


def _confidence(size: int) -> float:
    """More member runs ⇒ higher confidence the pattern is real, capped at 0.9
    (never 1.0 — the metric itself may be untrustworthy until #458)."""
    return round(min(0.9, 0.4 + 0.1 * size), 2)


def route_cluster(wf: str, cluster: dict[str, Any]) -> Suggestion:
    """Route ONE cluster to a governed :class:`Suggestion` (pure — no I/O)."""
    ft = cluster.get("failure_type", "")
    category, target_kind, tmpl = _ROUTING.get(ft, _FALLBACK)
    title = tmpl.format(wf=wf, ft=ft)
    target = f"{target_kind}:{wf}"
    size = int(cluster.get("size", 0) or 0)
    return Suggestion(
        cluster_fingerprint=cluster_fingerprint(cluster),
        category=category,
        target=target,
        title=title,
        description=(
            f"{size} run(s) of {wf} share failure_type={ft!r} "
            f"({cluster.get('signature','')} at {cluster.get('locus','') or 'workflow'}). "
            f"Routed to the {category} optimizer."
        ),
        proposed_diff="",  # Phase 1: routed intent + evidence, not a diff (optimizer executors = #468/#469)
        evidence={
            "cluster": {
                "failure_type": ft,
                "locus": cluster.get("locus", ""),
                "signature": cluster.get("signature", ""),
                "size": size,
                "member_run_ids": cluster.get("member_run_ids", []),
                "first_seen": cluster.get("first_seen"),
                "last_seen": cluster.get("last_seen"),
            },
            "workflow": wf,
            # Honesty marker — remove once #458 proves the metric (see module docstring).
            "untrusted_metric": True,
        },
        confidence=_confidence(size),
    )


def build_suggestions(
    wf: str, cluster_items: list[dict[str, Any]], *, min_cluster_size: int = 2
) -> list[Suggestion]:
    """Cluster ``cluster_items`` (run_id/created_at/failure_type/evidence dicts,
    the shape :func:`cluster_failures` takes) and route each cluster at/above
    ``min_cluster_size`` to a Suggestion. Pure — no store I/O, no dedupe (that's
    :func:`generate_and_store`'s job). Sorted by confidence, highest first."""
    clusters = cluster_failures(cluster_items)
    out = [route_cluster(wf, c) for c in clusters if int(c.get("size", 0) or 0) >= min_cluster_size]
    out.sort(key=lambda s: s.confidence, reverse=True)
    return out


async def generate_and_store(
    wf: str, cluster_items: list[dict[str, Any]], store: Any, *,
    min_cluster_size: int = 2, workspace_id: str | None = None,
) -> list[Suggestion]:
    """Build suggestions for ``wf``, drop any whose fingerprint is already
    spoken-for in ``store`` (proposed/accepted/rejected — so a rejected cluster
    never re-surfaces), persist the rest, and return the freshly-created ones."""
    # #1644 — everything here is workspace-scoped, including the dedupe read.
    # Unscoped, one workspace's REJECTED suggestion suppressed the same finding
    # in every other workspace: a cross-tenant silence rather than a leak, and
    # harder to notice than one.
    ws = workspace_id or current_workspace()
    seen = await store.open_fingerprints(workspace_id=ws)
    fresh = [s.model_copy(update={"workspace_id": ws})
             for s in build_suggestions(wf, cluster_items, min_cluster_size=min_cluster_size)
             if s.cluster_fingerprint not in seen]
    for s in fresh:
        await store.add_suggestion(s)
    return fresh


__all__ = [
    "build_suggestions",
    "cluster_fingerprint",
    "generate_and_store",
    "route_cluster",
    "suggestions_enabled",
]
