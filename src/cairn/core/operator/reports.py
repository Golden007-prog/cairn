"""The operator reporting contract (ADR-0103, #2035).

Every operator DECLARES what it reports — stages, series, scalars — as a
class attribute (:class:`ReportSpec`) beside ``risk``, mirrored in
``pack.toml`` and compared by the manifest gates. The context hands the
operator a typed :class:`Reporter` whose methods validate every name against
that declaration and turn each call into a run event the store keeps as the
durable log (the SSE bridge principle: the store is the log, the transport
is a wake-up).

Two rules, both from production experience:

* **Undeclared names raise in tests and ``cairn validate`` and are dropped
  with ONE warning per name in a running job.** A reporting bug must never
  fail a paid GPU run — the same contract ``stage_events`` already keeps.
* **Emitting never raises and never blocks the operator.** The sink is
  best-effort: from inside the event loop it schedules the append; from a
  worker thread it hands the coroutine to the loop it was built on; with no
  loop it logs and drops. T6 (#2025) adds the durable client-side spool for
  remote workers on top of the same ``(run_id, seq)`` contract.

Nothing here knows the word "training": ``train/local`` declares loss and
tokens per second, ``dataset/dedup`` declares rows in and rows out, and the
console renders whatever was declared (ADR-0102 D7).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

# ─── the declaration types live in cairn.core.reporting (stdlib-only) ───
# so cairn.core.pack.manifest can import them without the operator package.
# Re-exported here so an operator imports one name.
from cairn.core.reporting import (  # noqa: F401 — re-export is the point
    CHECKPOINT_EVENT_TYPE,
    DIVERGENCE_EVENT_TYPE,
    METRICS_EVENT_TYPE,
    PARAMETERS_EVENT_TYPE,
    PROGRESS_EVENT_TYPE,
    REPORT_EVENT_TYPES,
    SCALARS_EVENT_TYPE,
    STRICT_ENV,
    ReportSpec,
    Series,
    UndeclaredReportError,
    strict_reports_enabled,
)

_log = logging.getLogger("cairn.core.operator.reports")

# ─── sinks: where an event goes ──────────────────────────────────────────


@runtime_checkable
class ReportSink(Protocol):
    def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...


class ListSink:
    """Collects ``(type, payload)`` in memory — the test sink."""

    def __init__(self, into: Optional[list[tuple[str, dict[str, Any]]]] = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = into if into is not None else []

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))


class NullSink:
    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


AppendFn = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


class StorageSink:
    """Appends run events through an async ``append(run_id, type, payload)``.

    Built on the event loop that owns the run; safe to call from that loop
    (schedules a task) or from a worker thread the operator was moved to
    (hands the coroutine to the captured loop). Never raises; a failure is
    one warning, and the operator continues — the ``stage_events`` rule.
    """

    def __init__(self, append: AppendFn, run_id: str,
                 loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._append = append
        self._run_id = run_id
        self._loop = loop
        self._warned = False

    @classmethod
    def for_run(cls, run_id: str) -> "StorageSink":
        from cairn.core.storage import get_storage  # noqa: PLC0415 — keep the storage stack off the operator import path (runtime.py convention)

        async def _append(rid: str, typ: str, payload: dict[str, Any]) -> None:
            await get_storage().runs.append_event(rid, typ, payload)

        try:
            loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        return cls(_append, run_id, loop)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            coro = self._append(self._run_id, event_type, payload)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            # The store's asyncio.Lock is NOT thread-safe: an operator body
            # moved to a worker thread that runs its own loop (`asyncio.run`
            # inside a kernel) must hand the append to the loop the store was
            # opened on, never schedule it on the thread-local loop — two
            # loops appending at once allocated the same seq and lost events
            # (UNIQUE run_events.run_id, seq — seen live 2026-09-06 on sqlite).
            owner = self._loop if (self._loop is not None and not self._loop.is_closed()) else None
            if owner is not None and running is not owner:
                asyncio.run_coroutine_threadsafe(coro, owner)
            elif running is not None:
                running.create_task(coro)
            else:
                coro.close()
                self._warn("no event loop to deliver run events on")
        except Exception:  # noqa: BLE001 — a report must never fail the operator
            self._warn("run-event append failed")

    def _warn(self, why: str) -> None:
        if not self._warned:
            self._warned = True
            _log.warning("reporter for run %r: %s — further events dropped silently", self._run_id, why, exc_info=True)


# ─── the typed reporter ──────────────────────────────────────────────────


@dataclass
class Reporter:
    """``ctx.report`` — the only way an operator emits progress or metrics.

    Validates every name against the operator's :class:`ReportSpec`. Strict
    mode raises :class:`UndeclaredReportError`; non-strict drops the value
    with one warning per name. Every event carries ``node`` and a monotonic
    ``seq`` so a replay after an outage cannot duplicate.
    """

    spec: ReportSpec = field(default_factory=lambda: ReportSpec.NONE)
    node: str = ""
    sink: Any = field(default_factory=NullSink)
    strict: bool = False
    _seq: int = 0
    _warned: set[str] = field(default_factory=set)

    @classmethod
    def null(cls) -> "Reporter":
        return cls()

    # ── validation ──
    def _check(self, group: str, names: list[str], declared: Any) -> list[str]:
        ok: list[str] = []
        for n in names:
            if n in declared:
                ok.append(n)
                continue
            key = f"{group}:{n}"
            msg = (f"operator {self.node or '?'}: reported {group} {n!r} which its "
                   f"`reports` does not declare (ADR-0103) — declare it or drop the call")
            if self.strict:
                raise UndeclaredReportError(msg)
            if key not in self._warned:
                self._warned.add(key)
                _log.warning("%s; dropping", msg)
        return ok

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._seq += 1
        body = {"node": self.node, "seq": self._seq, **payload}
        try:
            self.sink.emit(event_type, body)
        except Exception:  # noqa: BLE001 — never fail the operator
            _log.warning("report sink raised for %s; event dropped", event_type, exc_info=True)

    # ── the surface ──
    def stage(self, name: str, percent: Optional[float] = None,
              eta_s: Optional[float] = None, cost_usd: Optional[float] = None) -> None:
        if not self._check("stage", [name], self.spec.stages):
            return
        payload: dict[str, Any] = {"stage": name}
        if percent is not None:
            payload["percent"] = max(0.0, min(100.0, float(percent)))
        if eta_s is not None:
            payload["eta_s"] = float(eta_s)
        if cost_usd is not None:
            payload["cost_usd"] = float(cost_usd)
        self._emit(PROGRESS_EVENT_TYPE, payload)

    def series(self, step: int, **values: float) -> None:
        ok = self._check("series", list(values), self.spec.series_names)
        if not ok:
            return
        self._emit(METRICS_EVENT_TYPE, {"step": int(step), "series": {k: float(values[k]) for k in ok}})

    def scalars(self, **values: Any) -> None:
        ok = self._check("scalar", list(values), self.spec.scalars)
        if not ok:
            return
        self._emit(SCALARS_EVENT_TYPE, {"values": {k: values[k] for k in ok}})

    def checkpoint(self, ref: str, step: int, metric: Optional[float] = None) -> None:
        payload: dict[str, Any] = {"ref": str(ref), "step": int(step)}
        if metric is not None:
            payload["metric"] = float(metric)
        self._emit(CHECKPOINT_EVENT_TYPE, payload)

    def divergence(self, kind: str, step: int, value: Optional[float] = None) -> None:
        payload: dict[str, Any] = {"kind": str(kind), "step": int(step)}
        if value is not None:
            payload["value"] = float(value)
        self._emit(DIVERGENCE_EVENT_TYPE, payload)

    @property
    def seq(self) -> int:
        return self._seq


__all__ = [
    "CHECKPOINT_EVENT_TYPE", "DIVERGENCE_EVENT_TYPE", "METRICS_EVENT_TYPE",
    "PARAMETERS_EVENT_TYPE", "PROGRESS_EVENT_TYPE", "REPORT_EVENT_TYPES",
    "SCALARS_EVENT_TYPE", "STRICT_ENV", "ListSink", "NullSink", "ReportSink",
    "ReportSpec", "Reporter", "Series", "StorageSink", "UndeclaredReportError",
    "strict_reports_enabled",
]
