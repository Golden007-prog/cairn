"""
Tool adapter: resolves capability names to LangChain tools.

Templates request tools by capability name (e.g. ``read_logs``) rather than by
concrete function. The concrete tools are contributed by *packs* — this adapter
never imports a pack module. It reads :class:`~cairn.core.pack.ToolDescriptor`
contributions from the pack registry (loading installed packs on first use) and
builds LangChain ``StructuredTool`` instances from their factories.

``CapabilityBinding`` is the generic data shape a pack uses to declare a
capability (name + coroutine + LLM-facing name + description); packs adapt their
binding lists into ToolDescriptors (see
``cairn.packs.incident.capabilities``).
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

# CapabilityBinding moved to cairn.core.pack (where it conceptually belongs
# — it's a pack-contract data shape, not a service-layer concern). Re-exported
# from here for backward compatibility with any code still importing it from
# the old location.
from cairn.core.pack import (
    CapabilityBinding,
    ToolDescriptor,
    all_tools,
    ensure_loaded,
)

__all__ = [
    "CapabilityBinding",
    "list_capabilities",
    "make_langchain_tool",
    "make_tools_for_capabilities",
]


def _descriptors() -> dict[str, ToolDescriptor]:
    """All capability descriptors contributed by installed packs, by name."""
    ensure_loaded()
    return {d.name: d for d in all_tools()}


def list_capabilities() -> list[str]:
    """All capability names contributed by installed packs."""
    return list(_descriptors().keys())


def make_langchain_tool(capability: str) -> StructuredTool:
    """Build the LangChain tool for one capability via its pack factory."""
    descriptors = _descriptors()
    if capability not in descriptors:
        known = ", ".join(sorted(descriptors)) or "(none — is a pack installed?)"
        raise KeyError(f"Unknown capability '{capability}'. Known: {known}")
    tool = descriptors[capability].factory()
    if not isinstance(tool, StructuredTool):
        raise TypeError(
            f"ToolDescriptor for '{capability}' produced {type(tool).__name__}, "
            "expected a langchain StructuredTool"
        )
    return tool


def make_tools_for_capabilities(capabilities: list[str]) -> list[StructuredTool]:
    """Build LangChain tools for several capabilities at once.

    Used by the template compiler when building an agent_loop. Deduplicates
    while preserving input order.
    """
    seen: set[str] = set()
    tools: list[StructuredTool] = []
    for cap in capabilities:
        if cap in seen:
            continue
        seen.add(cap)
        tools.append(make_langchain_tool(cap))
    return tools
