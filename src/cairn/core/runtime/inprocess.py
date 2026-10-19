"""In-process runtime — the default. Pregel drives, everything stays local.

This is what cairn has always done: the compiler hands back a
``compiled_graph`` (langgraph ``CompiledStateGraph``); the runtime calls
``ainvoke`` on it. No durability, no replay, no activities — same memory space
as the caller.

``InProcessRuntime`` is a thin wrapper around that one call. It exists so the
runner can depend on the :class:`Runtime` protocol instead of langgraph's
``CompiledStateGraph`` directly; swapping in :class:`TemporalRuntime` then
becomes a one-line constructor change with no caller edits.

It ignores node tags (see :class:`NodeKind`) entirely — those are only read by
the durable runtime.
"""

from __future__ import annotations

from typing import Any

from cairn.core import settings
from cairn.core.runtime.checkpointer import checkpoint_session
from cairn.core.runtime.node_stream import adrive


class InProcessRuntime:
    """Run the graph here, now, in this process. The default."""

    async def invoke(
        self,
        compiled_graph: Any,
        initial_state: dict[str, Any],
        *,
        run_id: str | None = None,  # noqa: ARG002 — accepted for protocol parity
        template_name: str | None = None,  # noqa: ARG002 — Temporal keys on these;
        template_sha: str | None = None,   # noqa: ARG002 — ignored in-process
    ) -> dict[str, Any]:
        # A checkpointed graph (one with an approval gate, ADR-0034) needs a
        # thread_id so its interrupt() can pause + later resume; an ungated
        # graph has no checkpointer, so config stays None (byte-identical call).
        # If the result carries ``__interrupt__`` the run paused for approval —
        # the caller resumes via ``resume()`` after the human decides.
        config = self._config(compiled_graph, run_id)
        if config is None:
            # ungated — adrive == ainvoke unless a node sink is bound (ADR-0044).
            return await adrive(compiled_graph, initial_state, None)
        return await self._run_gated(compiled_graph, initial_state, config)

    async def resume(
        self,
        compiled_graph: Any,
        decision: Any,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume a graph paused at an approval interrupt (ADR-0034).

        ``decision`` is the value the paused ``interrupt()`` returns — an
        ApprovalRecord-shaped dict (``{"approved": bool, "approver": ...}``).
        """
        from langgraph.types import Command

        config = self._config(compiled_graph, run_id)
        return await self._run_gated(compiled_graph, Command(resume=decision), config)

    async def has_pending_interrupt(self, compiled_graph: Any, run_id: str | None) -> bool:
        """Whether a checkpoint with a pending (resumable) interrupt exists for
        this run's thread in THIS process/backing store (#365).

        False means the gate is **orphaned** — the memory saver died with a
        previous engine process (or the thread was never checkpointed here), so
        a "resume" would restart the graph from scratch and re-pause rather
        than actually resuming. Callers fail loud / close the run out instead.
        """
        config = self._config(compiled_graph, run_id)
        if config is None:
            return False  # ungated graph — nothing to resume by definition
        if settings.checkpointer() in ("sqlite", "postgres"):
            # same session-scoped async-saver swap as _run_gated — reads the
            # same backing store the paused run checkpointed into.
            async with checkpoint_session() as saver:
                graph = compiled_graph.builder.compile(checkpointer=saver)
                snap = await graph.aget_state(config)
        else:
            snap = await compiled_graph.aget_state(config)
        # a live pause has pending node(s); a missing/finished thread has none.
        return bool(getattr(snap, "next", ()) or ())

    @staticmethod
    async def _run_gated(compiled_graph: Any, command_or_state: Any, config: dict[str, Any]) -> Any:
        """Drive a gated graph via ``ainvoke`` with a session-scoped async
        checkpointer (an internal issue).

        compile_template attaches a SYNC saver (it can't await the async session),
        but ``ainvoke`` needs the ``Async*`` variant. For sqlite/postgres,
        recompile the StateGraph with a checkpointer scoped to this segment via
        :func:`checkpoint_session` — the connection is bound to THIS loop and (for
        postgres) returned to the pool on exit, so the HITL wait between invoke()
        and resume() holds no connection. They read the same backing store, so
        paused state survives the swap.

        ``memory`` is left as-is: MemorySaver is already async-capable, AND the
        compiler-attached instance holds the paused state — swapping in a fresh
        one would drop it mid-run (the resume would find nothing).
        """
        if settings.checkpointer() not in ("sqlite", "postgres"):
            return await adrive(compiled_graph, command_or_state, config)
        async with checkpoint_session() as saver:
            graph = compiled_graph.builder.compile(checkpointer=saver)
            return await adrive(graph, command_or_state, config)

    @staticmethod
    def _config(compiled_graph: Any, run_id: str | None) -> dict[str, Any] | None:
        if getattr(compiled_graph, "checkpointer", None) is None:
            return None  # ungated graph — no thread needed
        return {"configurable": {"thread_id": run_id or "obs-run"}}
