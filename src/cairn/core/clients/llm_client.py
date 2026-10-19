"""Core-side LLM client facade.

The *only* place in ``core/`` that reaches into ``services/llm``. Today a
thin re-export over the in-process gateway; tomorrow swappable to an
AI-gateway / MCP-wire client without touching callers.
"""
from __future__ import annotations

from cairn.services.llm.factory import build_chat_model

__all__ = ["build_chat_model"]
