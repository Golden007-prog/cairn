"""cairn Playground eligibility (ADR-0073 §1a).

A workflow is offered in the Playground iff it can run cheap + fast on a sample —
which reduces to a single property: **none of its resolved operators needs a
GPU**. We derive that from the operator's declared ``requires_gpu`` (a first-class
manifest flag surfaced on the ``Operation`` class), so eligibility is a function
of capability metadata, not a hand-maintained allow-list. GPU workflows
(finetune, quantize, GPU-eval, serving) come back ineligible **with a reason** so
the console can show them disabled rather than silently hiding them.

This is the read-only signal the Playground picker consumes; it starts nothing
and has no side effects.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

import frontmatter  # base dep — YAML-frontmatter template metadata
from pydantic import BaseModel, Field

from cairn.core.pack.curation import DEFAULT_VISIBILITY, card_blurb, visibility_of
from cairn.core.plan import _resolve_template, build_plan


class WorkflowMeta(BaseModel):
    """Human-facing metadata for a workflow, read cheaply from its template
    frontmatter (no compile) — for the Playground's descriptive picker."""

    ref: str
    title: str = ""
    description: str = ""
    domain: str = ""
    operators: list[str] = Field(default_factory=list)
    #: #1566 — catalog curation tier (public/internal/deprecated). Read from the
    #: same frontmatter load as the rest, so the gallery can curate without a
    #: second round-trip. See :mod:`cairn.core.pack.curation`.
    visibility: str = DEFAULT_VISIBILITY


def workflow_meta(ref: str) -> WorkflowMeta:
    """Title + one-line description + domain for ``ref`` = ``pack/template``,
    from the template's frontmatter. Cheap (one YAML load); never raises."""
    domain = ref.split("/")[0]
    try:
        _, path = _resolve_template(ref)
    except Exception:  # noqa: BLE001
        path = None
    if not path or not path.exists():
        return WorkflowMeta(ref=ref, domain=domain)
    meta = frontmatter.load(path).metadata or {}
    name = str(meta.get("name") or path.stem).strip()
    _ACR = {"Rca": "RCA", "Pii": "PII", "Qa": "QA", "Llm": "LLM", "Rag": "RAG",
            "Gpu": "GPU", "E2E": "E2E", "Hitl": "HITL", "Api": "API", "Sql": "SQL"}
    title = " ".join(_ACR.get(w, w) for w in re.sub(r"[-_]+", " ", name).strip().title().split())
    # first sentence of the description → a card blurb. Shared with `/packs`
    # (#1566) so the same template reads the same on the installed shelf and in
    # "Available to enable".
    blurb = card_blurb(meta.get("description") or meta.get("summary"))
    for t in meta.get("tags", []) or []:
        if isinstance(t, str) and t.startswith("domain:"):
            domain = t.split(":", 1)[1]
            break
    return WorkflowMeta(ref=ref, title=title, description=blurb, domain=domain,
                        visibility=visibility_of(meta))


class PlaygroundEligibility(BaseModel):
    """Whether ``ref`` can run in the Playground sandbox, and if not, why."""

    ref: str
    found: bool = True
    eligible: bool = True
    #: human reason when ineligible (shown on the disabled picker option)
    reason: str = ""
    #: the operators that forced ineligibility (declare ``requires_gpu``)
    gpu_operators: list[str] = Field(default_factory=list)


def workflow_eligibility(
    ref: str, *, manifests: Optional[Mapping[str, Any]] = None,
) -> PlaygroundEligibility:
    """Compute Playground eligibility for ``ref`` = ``pack/template``.

    Resolves the template's operator plan and flags any operator that declares
    ``requires_gpu``. The authoritative source is the operator's **manifest
    metadata** (from ``pack.toml``, exposed via ``registry.get_metadata`` —
    ``train/*``, ``compress/quantize``, ``serve/vllm``, ``eval/benchmark``,
    ``model/eval``); the ``Operation`` class attribute is a fallback for
    operators that set it in code instead. Profile-independent — ``requires_gpu``
    is static, so the answer doesn't change with the execution environment.
    Unresolved operators are ignored here (a missing binding is a *different*
    problem, reported by doctor / preflight), never mistaken for a GPU
    requirement. ``manifests`` passes through to :func:`build_plan` (#2360).
    """
    # Lazy: keep the operator registry off the import hot path.
    from cairn.core.operator import default_registry  # noqa: PLC0415

    plan = build_plan(ref, {}, manifests=manifests)
    if not plan.template_found:
        return PlaygroundEligibility(
            ref=ref, found=False, eligible=False,
            reason="template not found — check the workflow ref or install its pack",
        )

    def _needs_gpu(name: str) -> bool:
        md = default_registry.get_metadata(name)          # manifest metadata (authoritative)
        if md is not None and getattr(md, "requires_gpu", False):
            return True
        if default_registry.has(name):                    # class-attribute fallback
            return bool(getattr(default_registry.get(name), "requires_gpu", False))
        return False

    gpu_ops: list[str] = []
    for op in plan.operators:
        name = op.operator
        if not name:
            continue
        if _needs_gpu(name):
            gpu_ops.append(name)

    gpu_ops = sorted(set(gpu_ops))
    if gpu_ops:
        return PlaygroundEligibility(
            ref=ref, found=True, eligible=False, gpu_operators=gpu_ops,
            reason=f"needs GPU — not runnable in sandbox ({', '.join(gpu_ops)})",
        )
    return PlaygroundEligibility(ref=ref, found=True, eligible=True)
