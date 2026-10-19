"""Idempotent ingest of a worker's report events, and the control log (#2025, ADR-0102 D4).

A remote training worker cannot append to the run store itself: it sits on a
box the control plane does not trust with a database handle, behind a network
that will drop. So it spools its events locally and ships them in batches —
over NATS JetStream (the declared service) or over plain HTTPS to
``POST /runs/{id}/events/ingest`` — and **replays the spool on every
reconnect**. Replay means duplicates, so ingest is idempotent on the key every
typed report event already carries (:class:`cairn.core.operator.reports.Reporter`
stamps ``node`` and a per-node monotonic ``seq`` into each payload):

    accept an event  ⇔  its ``seq`` is above the node's high-water mark

The high-water mark is read from the store itself the first time a run is
seen and kept in-process afterwards; the store's own ``seq`` stays the
authoritative order, exactly as the SSE bridge already treats it — **the store
is the log, the channel is only transport**.

Control runs the other way on the same principle. ``abort`` and
``checkpoint-and-stop`` are appended as ``control`` run events, so any process
(the console, the learning gate, a CLI) can request one and any worker can
read the latest one back, over the same two transports, without a second
channel to keep consistent.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from cairn.core.reporting import REPORT_EVENT_TYPES

_log = logging.getLogger("cairn.core.operator.report_ingest")

#: The event types a worker may ship = the typed reporter's own set
#: (:data:`cairn.core.reporting.REPORT_EVENT_TYPES`). Anything else in a batch is
#: refused — ``parameters`` is the runtime's, lifecycle events are the executor's;
#: a box writes neither.

#: A control request appended to the run's own log.
CONTROL_EVENT_TYPE = "control"
CONTROL_ACTIONS: tuple[str, ...] = ("abort", "checkpoint-and-stop")

#: When a worker names no node, its events file under this one.
DEFAULT_NODE = "worker"

#: Per-run, per-node high-water marks, keyed ``run_id → {node → seq}``. In-process
#: on purpose: a restart re-reads the store once for that run (``_load``), so
#: the mark can only ever be *behind* the truth, never ahead of it.
_HWM: dict[str, dict[str, int]] = {}


@dataclass
class IngestResult:
    """What one batch did. ``last_seq`` is the ack the worker advances its spool by."""

    accepted: int = 0
    dropped: int = 0
    refused: list[str] = field(default_factory=list)
    last_seq: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "dropped": self.dropped,
                "refused": list(self.refused), "last_seq": dict(self.last_seq)}


def _node_seq(payload: dict[str, Any]) -> tuple[str, Optional[int]]:
    node = str(payload.get("node") or DEFAULT_NODE)
    raw = payload.get("seq")
    try:
        seq = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        seq = None
    return node, seq


def high_water_from_events(events: list[Any]) -> dict[str, int]:
    """The per-node high-water mark implied by a run's stored events."""
    hwm: dict[str, int] = {}
    for ev in events:
        typ = getattr(ev, "type", None) if not isinstance(ev, dict) else ev.get("type")
        if typ not in REPORT_EVENT_TYPES:
            continue
        data = getattr(ev, "data", None) if not isinstance(ev, dict) else ev.get("data")
        node, seq = _node_seq(dict(data or {}))
        if seq is not None and seq > hwm.get(node, 0):
            hwm[node] = seq
    return hwm


async def _load(store: Any, run_id: str) -> dict[str, int]:
    if run_id in _HWM:
        return _HWM[run_id]
    try:
        events = await store.get_events(run_id)
    except Exception:  # noqa: BLE001 — an unreadable log means "nothing seen yet"; duplicates cost less than a lost batch
        _log.warning("ingest: could not read events of run %s to seed the high-water mark", run_id, exc_info=True)
        events = []
    _HWM[run_id] = high_water_from_events(events)
    return _HWM[run_id]


def forget_run(run_id: str) -> None:
    """Drop the in-process mark (tests; a terminal run)."""
    _HWM.pop(run_id, None)


async def ingest_events(store: Any, run_id: str, events: list[dict[str, Any]]) -> IngestResult:
    """Append the new events of ``events`` to ``run_id``'s log, in order, once.

    Each item is ``{"type": <report type>, "data": {node, seq, …}}``. Within a
    batch, events are applied per node in ``seq`` order; an event at or below
    the node's mark is dropped (a replay), an event of a non-report type or
    without a ``seq`` is refused by name — never silently.
    """
    res = IngestResult()
    hwm = await _load(store, run_id)
    by_node: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    for i, item in enumerate(events or []):
        typ = str((item or {}).get("type") or "")
        data = dict((item or {}).get("data") or {})
        if typ not in REPORT_EVENT_TYPES:
            res.refused.append(f"event {i}: type {typ!r} is not a report event")
            continue
        node, seq = _node_seq(data)
        if seq is None:
            res.refused.append(f"event {i}: no integer seq")
            continue
        data["node"] = node
        data["seq"] = seq
        by_node.setdefault(node, []).append((seq, typ, data))
    for node, items in by_node.items():
        items.sort(key=lambda t: t[0])
        mark = hwm.get(node, 0)
        for seq, typ, data in items:
            if seq <= mark:
                res.dropped += 1
                continue
            await store.append_event(run_id, typ, data)
            mark = seq
            res.accepted += 1
        hwm[node] = mark
        res.last_seq[node] = mark
    return res


# ─── control, the other direction ───────────────────────────────────────


async def record_control(store: Any, run_id: str, action: str, *,
                         node: str = "", reason: str = "", by: str = "") -> dict[str, Any]:
    """Append a control request to the run's log. ``action`` must be one of
    :data:`CONTROL_ACTIONS`; raises ``ValueError`` otherwise."""
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"unknown control action {action!r} (one of {', '.join(CONTROL_ACTIONS)})")
    payload = {"action": action, "node": node, "reason": reason, "by": by}
    ev = await store.append_event(run_id, CONTROL_EVENT_TYPE, payload)
    return {"seq": getattr(ev, "seq", None), **payload}


async def latest_control(store: Any, run_id: str, *, after_seq: int = 0) -> Optional[dict[str, Any]]:
    """The newest control request with store ``seq > after_seq``, or ``None``."""
    events = await store.get_events(run_id, after_seq=after_seq)
    latest = None
    for ev in events:
        if ev.type == CONTROL_EVENT_TYPE:
            latest = {"seq": ev.seq, **dict(ev.data or {})}
    return latest


async def wait_control(store: Any, run_id: str, *, after_seq: int = 0,
                       timeout_s: float = 25.0) -> Optional[dict[str, Any]]:
    """Long-poll: the newest control request after ``after_seq``, waiting up to
    ``timeout_s`` for one to arrive. Returns ``None`` on timeout — the worker
    simply asks again. A store without live subscription degrades to one read."""
    found = await latest_control(store, run_id, after_seq=after_seq)
    if found is not None or timeout_s <= 0:
        return found
    subscribe = getattr(store, "subscribe", None)
    if subscribe is None:
        return None

    async def _tail() -> Optional[dict[str, Any]]:
        # ``subscribe`` is an async generator on the stores and a coroutine on
        # some facades (the dispatcher) — accept either.
        it = subscribe(run_id, after_seq=after_seq)
        if inspect.isawaitable(it):
            it = await it
        async for ev in it:
            if ev.type == CONTROL_EVENT_TYPE:
                return {"seq": ev.seq, **dict(ev.data or {})}
        return None

    try:
        return await asyncio.wait_for(_tail(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


__all__ = [
    "CONTROL_ACTIONS", "CONTROL_EVENT_TYPE", "DEFAULT_NODE", "REPORT_EVENT_TYPES",
    "IngestResult", "forget_run", "high_water_from_events", "ingest_events",
    "latest_control", "record_control", "wait_control",
]
