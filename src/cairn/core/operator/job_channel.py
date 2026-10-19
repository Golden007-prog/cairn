"""Remote-job worker channel (ADR-0043 Slice 2) — events up, control down.

Slice 1 (``remote/exec``) drives a remote job by SSH-polling ``exit_code`` +
``metrics.json``. Slice 2 adds a **live channel** so a long-running remote worker
streams heartbeat/loss **up** and listens for an abort **down** — without the
local loop SSH-polling each tick:

* the worker publishes events to the run's **events** subject;
* the local control loop's ``poll`` reads the **latest** event → :class:`JobStatus`
  (carrying ``metrics['eval_losses']`` so the learning gate (ADR-0039 Gate 2)
  works **unchanged**);
* ``cancel`` publishes ``{"action":"abort"}`` to the run's **control** subject,
  which the worker polls and obeys.

The bridge to the workflow is the **existing** control loop: ``drive_once`` calls
this ``poll``/``cancel``; on a terminal event it resumes the paused run via
``RunDispatcher.resume_run`` (Temporal signals/continues it). The workflow stays
transport-agnostic — same pause/resume whether the job is SSH-polled (Slice 1) or
NATS-streamed (Slice 2).

Substrate: the same JetStream the trigger inbox (#115) uses; ``nats-py`` is the
optional ``[nats]`` extra. :class:`RemoteJobChannel` is a Protocol so tests use the
in-memory impl with zero infra.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import json
import os
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from cairn.core._time import utc_now_iso
from cairn.core.operator.job_store import RemoteJobRecord
from cairn.core.operator.jobs import JobStatus

# Subject layout: one events + one control subject per run, under a shared prefix
# so a single JetStream stream (cairn.jobs.>) covers them all.
_PREFIX = "cairn.jobs"


def channel_subjects(run_id: str) -> dict[str, str]:
    """The events/control subjects for a run — stash in ``RemoteJobRecord.channel``
    at submit time so the worker and the loop agree on names."""
    safe = run_id.replace(" ", "_")
    return {"events": f"{_PREFIX}.{safe}.events", "control": f"{_PREFIX}.{safe}.control"}


@runtime_checkable
class RemoteJobChannel(Protocol):
    """Events up, control down. ``events_since`` (#2025) is the drain the control
    loop uses to move a worker's spooled report events into the run store — the
    store, not the channel, is the log; the channel is transport."""

    """Bidirectional run channel: events up (worker→loop), control down (loop→worker)."""

    async def publish_event(self, run_id: str, event: dict[str, Any]) -> None: ...
    async def latest_event(self, run_id: str) -> Optional[dict[str, Any]]: ...
    async def publish_control(self, run_id: str, msg: dict[str, Any]) -> None: ...
    async def latest_control(self, run_id: str) -> Optional[dict[str, Any]]: ...
    async def events_since(self, run_id: str, cursor: Any = None) -> tuple[list[dict[str, Any]], Any]:
        """Report events published after ``cursor`` (opaque, per channel) and the new cursor."""
        ...
    async def aclose(self) -> None: ...


# ─── in-memory (tests / single-process dev) ──────────────────────────────


class InMemoryJobChannel:
    """Single-process channel (tests / dev): keeps the latest event + control per
    run, and every event in order so ``events_since`` can drain them."""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}
        self._control: dict[str, dict[str, Any]] = {}
        self._log: dict[str, list[dict[str, Any]]] = {}

    async def publish_event(self, run_id: str, event: dict[str, Any]) -> None:
        ev = {**event, "ts": event.get("ts") or utc_now_iso()}
        self._events[run_id] = ev
        self._log.setdefault(run_id, []).append(ev)

    async def latest_event(self, run_id: str) -> Optional[dict[str, Any]]:
        return self._events.get(run_id)

    async def publish_control(self, run_id: str, msg: dict[str, Any]) -> None:
        self._control[run_id] = {**msg, "ts": msg.get("ts") or utc_now_iso()}

    async def latest_control(self, run_id: str) -> Optional[dict[str, Any]]:
        return self._control.get(run_id)

    async def events_since(self, run_id: str, cursor: Any = None) -> tuple[list[dict[str, Any]], Any]:
        log = self._log.get(run_id, [])
        start = int(cursor or 0)
        return list(log[start:]), len(log)

    async def aclose(self) -> None:
        return None


class NatsJobChannel:
    """JetStream-backed channel — durable last-value retrieval via ``get_last_msg``.

    Events/control are published to per-run subjects under one stream
    (``cairn.jobs.>``); ``latest_*`` reads the last message on a subject. Reuses
    the same JetStream the trigger inbox uses (#115). ``nats`` is the optional
    ``[nats]`` extra, imported lazily."""

    STREAM = "CAIRN_REMOTE_JOBS"

    def __init__(self, url: str, *, stream: Optional[str] = None) -> None:
        self._url = url
        self._stream = stream or getenv("CAIRN_JOB_CHANNEL_STREAM", self.STREAM)
        self._nc = None
        self._js = None

    async def _ensure(self) -> None:
        if self._js is not None:
            return
        # lazy: nats-py is the optional `[nats]` extra — keep it off the base import path
        import nats  # noqa: PLC0415
        from nats.js.api import RetentionPolicy, StreamConfig  # noqa: PLC0415

        self._nc = await nats.connect(self._url)
        self._js = self._nc.jetstream()
        try:
            await self._js.add_stream(
                StreamConfig(
                    name=self._stream, subjects=[f"{_PREFIX}.>"],
                    retention=RetentionPolicy.LIMITS,
                )
            )
        except Exception:  # noqa: BLE001 — stream already exists
            pass

    async def _publish(self, subject: str, payload: dict[str, Any]) -> None:
        await self._ensure()
        await self._js.publish(subject, json.dumps(payload).encode())

    async def _last(self, subject: str) -> Optional[dict[str, Any]]:
        await self._ensure()
        import nats.js.errors  # noqa: PLC0415

        try:
            msg = await self._js.get_last_msg(self._stream, subject)
        except nats.js.errors.NotFoundError:
            return None
        except Exception:  # noqa: BLE001 — no message yet / transient
            return None
        data = getattr(msg, "data", None)
        if not data:
            return None
        try:
            return json.loads(data)
        except Exception:  # noqa: BLE001
            return None

    async def publish_event(self, run_id: str, event: dict[str, Any]) -> None:
        await self._publish(channel_subjects(run_id)["events"], {**event, "ts": event.get("ts") or utc_now_iso()})

    async def latest_event(self, run_id: str) -> Optional[dict[str, Any]]:
        return await self._last(channel_subjects(run_id)["events"])

    async def publish_control(self, run_id: str, msg: dict[str, Any]) -> None:
        await self._publish(channel_subjects(run_id)["control"], {**msg, "ts": msg.get("ts") or utc_now_iso()})

    async def latest_control(self, run_id: str) -> Optional[dict[str, Any]]:
        return await self._last(channel_subjects(run_id)["control"])

    async def events_since(self, run_id: str, cursor: Any = None,
                           *, batch: int = 500) -> tuple[list[dict[str, Any]], Any]:
        """Every message on the run's events subject with stream sequence above
        ``cursor``, oldest first, and the last sequence read. An ordered,
        ephemeral consumer starting at ``cursor + 1`` — the stream itself keeps
        the messages (LIMITS retention), so a restarted drain re-reads from the
        cursor the store told it; the store's idempotent ingest drops replays."""
        await self._ensure()
        from nats.js.api import ConsumerConfig, DeliverPolicy  # noqa: PLC0415 — optional [nats] extra

        start = int(cursor or 0) + 1
        cfg = ConsumerConfig(deliver_policy=DeliverPolicy.BY_START_SEQUENCE, opt_start_seq=start)
        sub = await self._js.pull_subscribe(channel_subjects(run_id)["events"], stream=self._stream, config=cfg)
        out: list[dict[str, Any]] = []
        last = int(cursor or 0)
        try:
            try:
                msgs = await sub.fetch(batch, timeout=1)
            except Exception:  # noqa: BLE001 — timeout = nothing new
                msgs = []
            for m in msgs:
                try:
                    out.append(json.loads(m.data))
                except Exception:  # noqa: BLE001 — a malformed frame is skipped, not fatal
                    pass
                seq = getattr(getattr(m, "metadata", None), "sequence", None)
                stream_seq = getattr(seq, "stream", None) if seq is not None else None
                if stream_seq:
                    last = max(last, int(stream_seq))
                await m.ack()
        finally:
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        return out, last

    async def aclose(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            await self._nc.close()
        self._nc = self._js = None


class StoreJobChannel:
    """The ``http`` channel (#2025): the run store IS the channel.

    A worker on the ``http`` backend talks to the control plane's own API —
    ``POST /runs/{id}/events/ingest`` and ``GET /runs/{id}/control`` — and those
    routes append to / read from the run store. So engine-side there is nothing
    to relay: ``publish_event`` ingests straight into the store (idempotent on
    the worker's ``(node, seq)``), ``publish_control`` appends a ``control``
    event, and the ``latest_*`` reads come back out of the same log. The
    remote_job control loop's poll/cancel wiring is unchanged.
    """

    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _runs(self) -> Any:
        if self._store is None:
            from cairn.core.storage import get_storage  # noqa: PLC0415 — keep the storage stack off the operator import path
            self._store = get_storage().runs
        return self._store

    async def publish_event(self, run_id: str, event: dict[str, Any]) -> None:
        from cairn.core.operator.report_ingest import REPORT_EVENT_TYPES, ingest_events  # noqa: PLC0415 — cycle: ingest imports reporting types only, but keep it lazy beside the store import
        typ = str(event.get("type") or "")
        if typ in REPORT_EVENT_TYPES:
            await ingest_events(self._runs(), run_id, [{"type": typ, "data": event.get("data") or {}}])
            return
        # a lifecycle-shaped frame (JobReporter heartbeat/succeeded/failed) is
        # recorded as a `job` event so `latest_event` can answer the poll
        await self._runs().append_event(run_id, "job", {**event, "ts": event.get("ts") or utc_now_iso()})

    async def latest_event(self, run_id: str) -> Optional[dict[str, Any]]:
        latest = None
        for ev in await self._runs().get_events(run_id):
            if ev.type == "job":
                latest = dict(ev.data or {})
        return latest

    async def publish_control(self, run_id: str, msg: dict[str, Any]) -> None:
        from cairn.core.operator.report_ingest import record_control  # noqa: PLC0415 — same
        await record_control(self._runs(), run_id, str(msg.get("action") or ""),
                             node=str(msg.get("node") or ""), reason=str(msg.get("reason") or ""),
                             by=str(msg.get("by") or "job-channel"))

    async def latest_control(self, run_id: str) -> Optional[dict[str, Any]]:
        from cairn.core.operator.report_ingest import latest_control  # noqa: PLC0415 — same
        return await latest_control(self._runs(), run_id)

    async def events_since(self, run_id: str, cursor: Any = None) -> tuple[list[dict[str, Any]], Any]:
        return [], cursor  # already in the store — nothing to drain

    async def aclose(self) -> None:
        return None


def get_job_channel() -> RemoteJobChannel:
    """The process job channel from ``CAIRN_JOB_CHANNEL`` — ``nats://…`` for the
    JetStream channel, ``http`` for the store-backed channel the control plane's
    own API fronts (#2025), else the in-memory channel (tests / single-process)."""
    spec = getenv("CAIRN_JOB_CHANNEL", "").strip()
    if spec.startswith("nats://"):
        return NatsJobChannel(spec)
    if spec.lower() == "http":
        return StoreJobChannel()
    return InMemoryJobChannel()


async def drain_channel_events(channel: RemoteJobChannel, store: Any, run_id: str,
                               cursor: Any = None) -> tuple[int, Any]:
    """Move the report events a worker published on ``channel`` into the run
    store — once each, whatever the channel replayed. Returns ``(accepted,
    new_cursor)``. Never raises: a channel hiccup costs one tick, not the loop."""
    from cairn.core.operator.report_ingest import REPORT_EVENT_TYPES, ingest_events  # noqa: PLC0415 — keep the store off this module's import path
    try:
        events, new_cursor = await channel.events_since(run_id, cursor)
    except Exception:  # noqa: BLE001 — transport trouble is not the loop's failure
        return 0, cursor
    batch = [{"type": e.get("type"), "data": e.get("data") or {}}
             for e in events if e.get("type") in REPORT_EVENT_TYPES]
    if not batch:
        return 0, new_cursor
    res = await ingest_events(store, run_id, batch)
    return res.accepted, new_cursor


# ─── control-loop wiring (channel-backed poll + cancel) ──────────────────
#
# These return the SAME PollFn/CancelFn shapes the control loop's
# build_default_poll/build_default_cancel return — drop-in replacements that read
# the channel instead of SSH-polling. drive_once, the learning gate, and resume
# via the dispatcher are all unchanged.


def build_channel_poll(channel: RemoteJobChannel) -> Callable[[RemoteJobRecord], Awaitable[JobStatus]]:
    """A ``poll`` that maps the latest streamed event → :class:`JobStatus`. No
    event yet ⇒ ``running`` (the worker hasn't reported its first heartbeat)."""

    async def poll(rec: RemoteJobRecord) -> JobStatus:
        ev = await channel.latest_event(rec.run_id)
        if ev is None:
            return JobStatus(state="running")
        state = ev.get("state", "running")
        return JobStatus(
            state=state,
            output=ev.get("output"),
            metrics=ev.get("metrics") or {},
            error=ev.get("error") or "",
        )

    return poll


def build_channel_cancel(channel: RemoteJobChannel) -> Callable[[RemoteJobRecord], Awaitable[None]]:
    """A ``cancel`` that publishes an abort on the run's control subject — the
    learning gate's L2 kill, delivered to the live worker."""

    async def cancel(rec: RemoteJobRecord) -> None:
        await channel.publish_control(rec.run_id, {"action": "abort"})

    return cancel


# ─── worker-side reporter (runs ON the remote box) ───────────────────────


class JobReporter:
    """The remote worker's end of the channel: stream progress **up**, check for an
    abort **down**. A long-running remote training loop uses it like::

        rep = JobReporter(get_job_channel(), run_id)
        for step in training:
            await rep.heartbeat({"eval_losses": losses})   # up
            if await rep.aborted():                          # down (learning-gate kill)
                return await rep.failed("aborted by control")
        await rep.succeeded(result, metrics)
    """

    def __init__(self, channel: RemoteJobChannel, run_id: str) -> None:
        self._ch = channel
        self._run_id = run_id

    async def heartbeat(self, metrics: Optional[dict[str, Any]] = None) -> None:
        await self._ch.publish_event(self._run_id, {"state": "running", "metrics": metrics or {}})

    async def succeeded(self, output: dict[str, Any], metrics: Optional[dict[str, Any]] = None) -> None:
        await self._ch.publish_event(
            self._run_id, {"state": "succeeded", "output": output, "metrics": metrics or {}}
        )

    async def failed(self, error: str) -> None:
        await self._ch.publish_event(self._run_id, {"state": "failed", "error": error})

    async def aborted(self) -> bool:
        ctl = await self._ch.latest_control(self._run_id)
        return bool(ctl and ctl.get("action") == "abort")


__all__ = [
    "RemoteJobChannel", "InMemoryJobChannel", "NatsJobChannel", "StoreJobChannel",
    "get_job_channel", "channel_subjects", "drain_channel_events",
    "build_channel_poll", "build_channel_cancel", "JobReporter",
]
