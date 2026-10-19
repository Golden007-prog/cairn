"""Core-side memory client facade.

The *only* place in ``core/`` that reaches into ``services/memory``. Today a
thin re-export over the in-process episodic store writer; tomorrow swappable
to an MCP-wire memory service without touching callers.
"""
from __future__ import annotations

from cairn.services.memory.writer import write_investigation_to_memory

__all__ = ["write_investigation_to_memory"]
