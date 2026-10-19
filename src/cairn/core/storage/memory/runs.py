"""In-memory ``RunStore`` implementation.

Dict-backed, guarded by an ``asyncio.Lock``. Volatile across restarts —
the right tier for tests and the realtime lane (alert sources re-fire so
losing in-flight state is acceptable). For restart-safety use
:class:`cairn.core.storage.sqlite.runs.SqliteRunStore`.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Sequence

from cairn.core.storage.transitions import guard_run_update
from cairn.core.storage.protocols import (
    ACTIVE_STATUSES,
    Lane,
    Run,
    RunEvent,
    RunStore,
    Status,
    _EventHub,
)


class InMemoryRunStore(RunStore):
    """Dict-backed store guarded by an asyncio.Lock. Volatile across restarts."""

    def __init__(self) -> None:
        self._d: dict[str, Run] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._lock = asyncio.Lock()
        self._hub = _EventHub()

    async def create(self, run: Run) -> None:
        async with self._lock:
            self._d[run.id] = run

    async def update(self, run_id: str, **fields) -> Run:
        async with self._lock:
            run = self._d.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if not guard_run_update(run.status, fields, run_id=run_id):
                return run  # illegal transition — whole update refused (#1245)
            updated = run.model_copy(update=fields)
            self._d[run_id] = updated
            return updated

    async def get(self, run_id: str, workspace_id: Optional[str] = None) -> Optional[Run]:
        async with self._lock:
            run = self._d.get(run_id)
        # query-enforced scoping (ADR-0051 §4): cross-workspace reads are not-found.
        if run is not None and workspace_id is not None and run.workspace_id != workspace_id:
            return None
        return run

    async def list(
        self,
        *,
        lane: Optional[Lane] = None,
        status: Optional[Status] = None,
        workspace_id: Optional[str] = None,
        limit: int = 100,
        include_payloads: bool = True,
    ) -> list[Run]:
        async with self._lock:
            items = list(self._d.values())
        if lane is not None:
            items = [i for i in items if i.lane == lane]
        if status is not None:
            items = [i for i in items if i.status == status]
        if workspace_id is not None:
            items = [i for i in items if i.workspace_id == workspace_id]
        # newest first by created_at
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:limit]

    async def list_pipeline_less_runs(
        self,
        *,
        after_id: str = "",
        limit: int = 200,
        workspace_id: Optional[str] = None,
    ) -> list[Run]:
        """Exact dict scan — no ``limit`` ceiling, no hydration (#2128 C).

        Overrides the Protocol default for the same reason
        :meth:`count_in_flight_on_target` does: the default is capped at
        ``SCAN_COUNT_LIMIT`` newest rows, and a backfill that silently stops at
        500 leaves orphans behind while reporting success.
        """
        async with self._lock:
            items = list(self._d.values())
        out = [
            r for r in items
            if not r.bundle_pipeline_id
            and r.id > after_id
            and (workspace_id is None or r.workspace_id == workspace_id)
        ]
        out.sort(key=lambda r: r.id)
        return out[:limit]

    async def count_in_flight_on_target(
        self,
        target: str,
        statuses: Sequence[str],
        *,
        exclude_run_id: str = "",
    ) -> int:
        """Exact dict scan — no ``limit``, no hydration (#1531).

        The base-class fallback would go through :meth:`list`, which sorts and
        truncates at 500; here the whole dict IS the index, so counting it is
        both cheaper and exact regardless of how many runs are in flight.
        """
        name = str(target or "").strip()
        if not name:
            return 0
        wanted = set(statuses)
        async with self._lock:
            items = list(self._d.values())
        return sum(
            1 for r in items
            if r.status in wanted
            and r.id != exclude_run_id
            and str(r.compute_target or "") == name
        )

    async def find_active_by_fingerprint(self, fp: str) -> Optional[Run]:
        async with self._lock:
            # Prefer the oldest active match so coalescing folds onto the
            # original, not a later duplicate.
            matches = [
                i for i in self._d.values()
                if i.fingerprint == fp and i.status in ACTIVE_STATUSES
            ]
        if not matches:
            return None
        matches.sort(key=lambda i: i.created_at)
        return matches[0]

    async def append_event(
        self, run_id: str, type: str, data: Optional[dict] = None
    ) -> RunEvent:
        async with self._lock:
            log = self._events.setdefault(run_id, [])
            ev = RunEvent(
                run_id=run_id, seq=len(log) + 1, type=type, data=dict(data or {})
            )
            log.append(ev)
        await self._hub.publish(ev)
        return ev

    async def get_events(
        self, run_id: str, *, after_seq: int = 0
    ) -> list[RunEvent]:
        async with self._lock:
            log = list(self._events.get(run_id, ()))
        return [e for e in log if e.seq > after_seq]
