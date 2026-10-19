"""Runtime protocol — how a compiled template's graph actually executes.

Today there is one runtime: ``InProcessRuntime``, which calls
``compiled_graph.ainvoke(state)`` directly. The langgraph Pregel engine drives
node execution in-process; this is the path every existing test exercises.

Tomorrow there will be a second runtime: ``TemporalRuntime`` (in
``cairn.core.runtime.temporal``). It runs the *same* StateGraph but routes
each node through Temporal — pure nodes run inline inside the workflow, IO/LLM
nodes get reified as Temporal Activities with retries and timeouts. Only one
runtime is in play at a time; LangGraph is the topology DSL either way.

The compiler stays runtime-agnostic. It tags each node with a :class:`NodeKind`
via :func:`tag` (attribute on the node fn). The in-process runtime ignores the
tag entirely. The Temporal runtime reads it to decide ``execute_in='activity'``
vs ``execute_in='workflow'`` per node.

Choosing a kind, rules of thumb:

  ``PURE``  — deterministic glue: routers, joiners, dispatchers, ``lambda s: {}``
              passthroughs. Safe to run inside Temporal's deterministic sandbox.
  ``LLM``   — anything that calls a chat model. Network, non-deterministic,
              must be an activity under Temporal.
  ``TOOL``  — anything that calls an MCP tool, a backend, or a nested template.
  ``IO``    — anything else that touches the clock, the network, or user code.

If unsure, pick ``IO``. The cost of mis-tagging IO as PURE is a Temporal
non-determinism error at runtime; over-tagging is harmless (the activity
roundtrip is cheap relative to an LLM call).
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Protocol, TypeVar

if TYPE_CHECKING:
    from cairn.core.runtime.retry import RetryPolicy


class NodeKind(str, Enum):
    """How a StateGraph node should execute under a durable runtime."""

    PURE = "pure"
    LLM = "llm"
    TOOL = "tool"
    IO = "io"


_NODE_KIND_ATTR = "_obs_node_kind"
_NODE_RETRY_ATTR = "_obs_node_retry"
_NODE_QUEUE_ATTR = "_obs_node_queue"

# Logical routing label for a node whose activity should run on the dedicated
# remote/GPU task queue (a pull-based worker polls it) rather than the default
# queue (#565). The worker resolves this label to a real queue name from
# settings; unresolved → the node stays on the default queue (no-op).
QUEUE_REMOTE = "remote"

# Logical routing label for a CPU-bound dataset-batch node (a Shardable operator)
# whose activity should run on the dedicated `cairn-dataset` worker pool — an
# isolated container with its own cgroup CPU quota — instead of starving the
# interactive worker (#962, dispatcher "A"). Resolved to a real queue name from
# settings (CAIRN_TEMPORAL_DATASET_TASK_QUEUE); unset → the node stays on the
# default queue and runs the local budgeted pool there (dispatcher "C"), a no-op.
QUEUE_DATASET = "dataset"

F = TypeVar("F", bound=Callable[..., Any])


def tag(
    fn: F,
    kind: NodeKind,
    *,
    retry: Optional["RetryPolicy"] = None,
    queue: Optional[str] = None,
) -> F:
    """Stamp ``fn`` with its :class:`NodeKind` (and optional retry policy +
    routing queue label).

    Mutates the function object: sets ``fn._obs_node_kind`` and, when provided,
    ``fn._obs_node_retry`` / ``fn._obs_node_queue``. Functions and lambdas
    accept attribute assignment; built-in callables do not.

    The in-process runtime never reads these attributes. The durable runtime
    introspects them to translate into Temporal's per-node metadata — ``queue``
    (e.g. :data:`QUEUE_REMOTE`) routes the node's activity to a separate task
    queue so a dedicated worker can pull it (#565).
    """
    setattr(fn, _NODE_KIND_ATTR, kind)
    if retry is not None:
        setattr(fn, _NODE_RETRY_ATTR, retry)
    if queue is not None:
        setattr(fn, _NODE_QUEUE_ATTR, queue)
    return fn


def get_kind(fn: Callable[..., Any], default: NodeKind = NodeKind.IO) -> NodeKind:
    """Return the :class:`NodeKind` previously stamped via :func:`tag`.

    Defaults to ``IO`` for an untagged node — the safe choice under Temporal
    (it'll round-trip through an activity instead of risking a determinism
    error in the workflow sandbox).
    """
    return getattr(fn, _NODE_KIND_ATTR, default)


def get_retry(fn: Callable[..., Any]) -> Optional["RetryPolicy"]:
    """Return the explicit :class:`RetryPolicy` stamped on ``fn``, or ``None``.

    A ``None`` return means "use the kind's default". The temporal runtime
    resolves the effective policy via :func:`cairn.core.runtime.retry.resolve`.
    """
    return getattr(fn, _NODE_RETRY_ATTR, None)


def get_queue(fn: Callable[..., Any]) -> Optional[str]:
    """Return the routing queue label stamped on ``fn`` (e.g. :data:`QUEUE_REMOTE`),
    or ``None`` for the default queue (#565)."""
    return getattr(fn, _NODE_QUEUE_ATTR, None)


class Runtime(Protocol):
    """How a compiled langgraph StateGraph gets executed.

    Implementations:

      - :class:`cairn.core.runtime.inprocess.InProcessRuntime` — today's
        Pregel-in-process path.
      - ``cairn.core.runtime.temporal.TemporalRuntime`` — submits a workflow
        to a Temporal cluster; nodes route to activities by their tagged kind.

    The contract is intentionally narrow: take an already-compiled graph plus
    an initial state, return the final state dict. Anything richer (streaming,
    cancellation, checkpointing) is the runtime's own concern and exposed via
    runtime-specific helpers — not promoted into this protocol.
    """

    async def invoke(
        self,
        compiled_graph: Any,
        initial_state: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...


InvokeFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
