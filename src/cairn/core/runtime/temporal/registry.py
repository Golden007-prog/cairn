"""Worker-side compiled-template registry — keyed by (name, content_sha).

The :class:`TemporalRuntime` (caller side) submits a workflow with
``(template_name, content_sha, initial_state)``. The worker side looks the
template up here. Compile happens at worker startup, not inside the workflow —
the compiler builds LLM clients eagerly, which would fail Temporal's
deterministic workflow sandbox if done inline.

The sha gate is a deploy-safety check: if a worker has a different version of
the template than the one a workflow was submitted with (because someone
deployed mid-run), the workflow fails fast on lookup instead of running with
shifted code.

Two keys per template are also exposed: a graph-level handle (for the plugin's
``graphs={"name": built_graph}`` config) and a per-node fn lookup (for the
``run_node`` activity to dispatch to the right closure).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from cairn.core.engine.compiled import CompiledTemplate
from cairn.core.engine.compiler import compile_template

logger = logging.getLogger("cairn.runtime.temporal.registry")


@dataclass(frozen=True)
class TemplateEntry:
    """One compiled template available to the worker."""

    name: str
    sha: str
    path: Path
    compiled: CompiledTemplate
    # Reverse lookup: node_name → the langgraph node fn. Populated only for
    # state_machine flows (the only flow type that has multiple nodes worth
    # routing to activities individually).
    node_fns: dict[str, Callable[..., Any]] = field(default_factory=dict)


def sha_for(path: Path) -> str:
    """Content sha of a template file. The deploy-safety identifier."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class TemplateRegistry:
    """Process-local registry. One instance per worker."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], TemplateEntry] = {}
        self._latest_sha: dict[str, str] = {}

    def register(self, path: Path) -> TemplateEntry:
        """Compile ``path`` and add it to the registry. Last-writer-wins on sha."""
        compiled = compile_template(path)
        sha = sha_for(path)
        node_fns = _extract_node_fns(compiled)
        entry = TemplateEntry(
            name=compiled.name,
            sha=sha,
            path=path,
            compiled=compiled,
            node_fns=node_fns,
        )
        self._by_key[(compiled.name, sha)] = entry
        self._latest_sha[compiled.name] = sha
        logger.info("registered template name=%s sha=%s", compiled.name, sha)
        return entry

    def get(self, name: str, sha: Optional[str] = None) -> TemplateEntry:
        """Look up by (name, sha). If sha omitted, return the latest registered."""
        if sha is None:
            sha = self._latest_sha.get(name)
            if sha is None:
                raise KeyError(f"template not registered: {name!r}")
        try:
            return self._by_key[(name, sha)]
        except KeyError as exc:
            raise KeyError(
                f"template {name!r} sha {sha!r} not registered on this worker — "
                "the workflow was likely submitted against a different worker version."
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._latest_sha.keys())


def _extract_node_fns(compiled: CompiledTemplate) -> dict[str, Callable[..., Any]]:
    """Pull per-node fns out of a compiled langgraph StateGraph, if applicable.

    Returns an empty dict for prompt/agent_loop flows (single-runnable shape).
    For state_machine flows, walks the underlying graph builder to expose every
    ``add_node``'d callable by name — that's what the ``run_node`` activity
    dispatches to.
    """
    # Temporal Step 3 — _build_state_machine stamps the per-node fn map on
    # the run closure (``run._node_fns``); prompt/agent_loop runnables don't
    # carry it and fall back to the empty dict.
    return dict(getattr(compiled.runnable, "_node_fns", None) or {})


# Module-level singleton — bound at worker startup by
# cairn.core.runtime.temporal.worker.build_registry().
_REGISTRY: Optional[TemplateRegistry] = None


def bind(registry: TemplateRegistry) -> None:
    global _REGISTRY
    _REGISTRY = registry


def current() -> TemplateRegistry:
    if _REGISTRY is None:
        raise RuntimeError(
            "TemplateRegistry not bound — the Temporal worker bootstrap should "
            "call cairn.core.runtime.temporal.worker.build_registry() before "
            "starting the worker loop."
        )
    return _REGISTRY
