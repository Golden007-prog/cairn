"""Durable remote-job registry — the provisioning seam between a remote-job
operator and the control worker (ADR-0038).

A remote-job operator's ``submit()`` **registers** a :class:`RemoteJobRecord`
here (provider + ``JobHandle`` + the paused run's id) and the run pauses
(``interrupt()``). A separate, generic **control worker** claims active records,
re-attaches by ``JobHandle`` (re-poll, never re-dispatch → crash-safe), drives the
job, and on a terminal state resumes the run via ``RunDispatcher.resume_run``.

This mirrors the trigger inbox (#115) for *outbound* jobs: same sqlite/in-memory
tiering, same claim-loop shape. The operator and worker are decoupled — they share
only this store. Backend selection: ``CAIRN_STORAGE=postgres://…`` routes to the
one prod Postgres (storage unification, #427) — the same mirror as
``PostgresConnectionStore``/``PostgresRunStore``; otherwise falls back to the
legacy ``CAIRN_JOB_STORE`` knob (sqlite default; ``memory``/``:memory:`` for
tests).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.operator.jobs import JobHandle, JobStatus
from cairn.core.workspace import DEFAULT_WORKSPACE
from cairn.core.pg_schema_init import PgSchemaInit

SUBMITTED, RUNNING, DONE, FAILED, CANCELLED = "submitted", "running", "done", "failed", "cancelled"
_ACTIVE = (SUBMITTED, RUNNING)
_TERMINAL = (DONE, FAILED, CANCELLED)

#: ADR-0104 D2 (#565) — ``backend`` of a job a **pull worker** takes off the
#: queue rather than one the control plane pushed at a provider.
PULL_BACKEND = "pull"
#: How long a claim is good for before the job goes back on the queue. A worker
#: heartbeats well inside this; a worker that dies loses the job after it.
DEFAULT_LEASE_S = 300


def _plus(seconds: float, now: Optional[str] = None) -> str:
    base = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat()


class RemoteJobRecord(BaseModel):
    """One remote job a run is paused on, tracked through its lifecycle. Holds
    enough to re-attach (the ``JobHandle``), to resume the run (``run_id`` +
    ``node_name``), and the final ``result`` (handed to ``resume_run``)."""

    id: str = ""               # f"{run_id}:{node_name}" — one per paused node
    run_id: str
    #: Tenancy scope (ADR-0048 rev 2, #239), inherited from the paused run. The
    #: console lists jobs within its workspace; the control worker claims across
    #: all workspaces (shared infra), so ``claim_active`` is intentionally unscoped.
    workspace_id: str = DEFAULT_WORKSPACE
    node_name: str = ""
    provider: str = ""         # the compute provider / operator name
    job_id: str = ""           # JobHandle.job_id (provider job id)
    backend: str = ""          # JobHandle.backend
    job_meta: dict[str, Any] = Field(default_factory=dict)
    compute_target: Optional[str] = None  # the connection name (#121)
    input: dict[str, Any] = Field(default_factory=dict)  # resolved operator input — re-poll
    channel: dict[str, Any] = Field(default_factory=dict)  # event/control subjects
    status: str = SUBMITTED
    attempts: int = 0
    result: Optional[dict[str, Any]] = None  # operator output, set on done
    error: Optional[str] = None
    #: Durable checkpoint location (e.g. s3://…/<run_id>/) once the job reports one
    #: in its poll metrics. The control loop needs it to relaunch on a resumable
    #: death (#552/ADR-0064): None → nothing to resume from → no relaunch.
    checkpoint_uri: Optional[str] = None
    #: Recent stdout tail from the last poll — surfaced to the console so a running
    #: remote job isn't a blind spot (#553).
    last_log_tail: str = ""
    #: ADR-0104 D2 (#565) — the pull lane. ``requires`` are the capabilities a
    #: worker must offer to be handed this job ({"gpu": "A100"}); ``image`` is
    #: the container the worker runs. Both empty for a pushed provider job.
    requires: dict[str, str] = Field(default_factory=dict)
    image: str = ""
    #: Who holds the claim and until when (ISO). Cleared when the lease expires.
    claimed_by: Optional[str] = None
    lease_until: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    #: The last :class:`JobStatus` the worker reported through ``heartbeat`` —
    #: what ``build_default_poll`` reads INSTEAD of polling a provider, because
    #: for a pull job there is no provider to poll.
    worker_status: Optional[str] = None
    worker_error: str = ""
    worker_metrics: dict[str, Any] = Field(default_factory=dict)
    worker_output: Optional[dict[str, Any]] = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: Optional[str] = None

    def model_post_init(self, _ctx: Any) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.run_id}:{self.node_name}" if self.node_name else self.run_id)

    def handle(self) -> JobHandle:
        """Reconstruct the JobHandle so the worker can re-poll without re-dispatch."""
        return JobHandle(job_id=self.job_id, backend=self.backend, meta=dict(self.job_meta))


# ─── the pull lane's pure predicates (ADR-0104 D2, #565) ─────────────────


def capabilities_match(required: Optional[Mapping[str, str]],
                       offered: Optional[Mapping[str, str]]) -> bool:
    """Whether a worker offering ``offered`` may take a job requiring ``required``.

    A subset check on exact values, deliberately: a job that asks for nothing
    fits any worker, and a job that names ``gpu: A100`` is never handed to a T4.
    Anything richer (ordering, ranges) belongs in a scheduler, which D2 is not.
    """
    for key, want in (required or {}).items():
        if str((offered or {}).get(key, "")) != str(want):
            return False
    return True


def lease_expired(rec: "RemoteJobRecord", now: Optional[str] = None) -> bool:
    """Whether ``rec``'s claim has run out (an unclaimed job never has)."""
    if not rec.claimed_by or not rec.lease_until:
        return False
    return rec.lease_until < (now or utc_now_iso())


def is_pull_job(rec: "RemoteJobRecord") -> bool:
    """Whether this record belongs to the pull lane — a worker has claimed it,
    reported on it, or it was registered for claiming."""
    return bool(rec.claimed_by or rec.worker_status or rec.backend == PULL_BACKEND)


def heartbeat_status(rec: "RemoteJobRecord") -> Optional[JobStatus]:
    """The worker's last reported :class:`JobStatus`, or ``None`` when this job
    is not a pull job. A claimed job that has not reported yet is ``running`` —
    it is being worked on, which is exactly what the reconciler should believe."""
    if not is_pull_job(rec):
        return None
    state = rec.worker_status or "running"
    return JobStatus(state=state, output=rec.worker_output, error=rec.worker_error,
                     metrics=dict(rec.worker_metrics))


def _claim_stamp(rec: "RemoteJobRecord", worker_id: str, lease_s: float,
                 now: Optional[str] = None) -> dict[str, Any]:
    return {"claimed_by": worker_id, "lease_until": _plus(lease_s, now), "updated_at": utc_now_iso()}


def _requeue_stamp(rec: "RemoteJobRecord") -> dict[str, Any]:
    """A lost worker's job goes back on the queue: the claim is dropped, the
    attempt is counted (so a job cannot be re-queued forever unnoticed) and any
    half-reported worker status is cleared."""
    return {"claimed_by": None, "lease_until": None, "status": SUBMITTED,
            "attempts": rec.attempts + 1, "worker_status": None, "worker_error": "",
            "worker_metrics": {}, "worker_output": None, "updated_at": utc_now_iso()}


def _heartbeat_stamp(worker_id: str, status: JobStatus, lease_s: float,
                     now: Optional[str] = None) -> dict[str, Any]:
    out = status.output if isinstance(status.output, dict) else (
        status.output.model_dump() if hasattr(status.output, "model_dump") else None)
    ts = utc_now_iso()  # ONE stamp: the door reads the max of these and must not tie-break
    return {
        "status": RUNNING,  # the RECONCILER closes a job out, never the worker
        "lease_until": _plus(lease_s, now),
        "last_heartbeat_at": ts,
        "worker_status": status.state,
        "worker_error": status.error or "",
        "worker_metrics": dict(status.metrics or {}),
        "worker_output": out,
        "updated_at": ts,
    }


@runtime_checkable
class RemoteJobStore(Protocol):
    """Durable registry of in-flight remote jobs (the provisioning seam)."""

    async def register(self, record: RemoteJobRecord) -> RemoteJobRecord: ...
    async def get(self, record_id: str) -> Optional[RemoteJobRecord]: ...
    async def claim_active(self, *, now: Optional[str] = None, limit: int = 50) -> list[RemoteJobRecord]: ...
    async def mark_running(self, record_id: str) -> None: ...
    async def mark_done(self, record_id: str, result: dict[str, Any]) -> None: ...
    async def mark_failed(self, record_id: str, error: str) -> None: ...
    async def mark_cancelled(self, record_id: str) -> None: ...
    #: #552 — record a durable checkpoint location the job reported.
    async def set_checkpoint_uri(self, record_id: str, uri: str) -> None: ...
    #: #553 — record the latest stdout tail so the console can show it.
    async def set_log_tail(self, record_id: str, tail: str) -> None: ...
    #: ADR-0104 D2 (#565) — the PULL direction. ``claim_next`` hands at most one
    #: queued job to a worker that dialled out, ``heartbeat`` extends its lease
    #: and records the status the reconciler reads, ``requeue_expired`` puts a
    #: lost worker's job back on the queue.
    async def claim_next(self, worker_id: str, *, capabilities: Optional[Mapping[str, str]] = None,
                         lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> Optional[RemoteJobRecord]: ...
    async def heartbeat(self, record_id: str, worker_id: str, status: JobStatus, *,
                        lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> bool: ...
    async def requeue_expired(self, *, now: Optional[str] = None) -> int: ...
    #: #552 — re-arm a resubmitted job: point at the new handle, bump attempts,
    #: back to active. The run stays paused; the relaunched job drives resume.
    async def mark_relaunch(self, record_id: str, handle: JobHandle) -> None: ...
    async def list(self, *, status: Optional[str] = None, since: Optional[str] = None, workspace_id: Optional[str] = None, limit: int = 200) -> list[RemoteJobRecord]: ...
    def close(self) -> None: ...


# ─── in-memory (tests / dev) ─────────────────────────────────────────────


class InMemoryRemoteJobStore:
    """Volatile store (tests / no-config default)."""

    def __init__(self) -> None:
        self._by_id: dict[str, RemoteJobRecord] = {}

    async def register(self, record: RemoteJobRecord) -> RemoteJobRecord:
        self._by_id[record.id] = record
        return record

    async def get(self, record_id: str) -> Optional[RemoteJobRecord]:
        return self._by_id.get(record_id)

    async def claim_active(self, *, now: Optional[str] = None, limit: int = 50) -> list[RemoteJobRecord]:
        active = [r for r in self._by_id.values() if r.status in _ACTIVE]
        active.sort(key=lambda r: r.created_at)
        return active[:limit]

    def _update(self, record_id: str, **fields: Any) -> None:
        cur = self._by_id.get(record_id)
        if cur is not None:
            self._by_id[record_id] = cur.model_copy(update={**fields, "updated_at": utc_now_iso()})

    async def mark_running(self, record_id: str) -> None:
        self._update(record_id, status=RUNNING)

    async def mark_done(self, record_id: str, result: dict[str, Any]) -> None:
        self._update(record_id, status=DONE, result=dict(result))

    async def mark_failed(self, record_id: str, error: str) -> None:
        self._update(record_id, status=FAILED, error=error)

    async def mark_cancelled(self, record_id: str) -> None:
        self._update(record_id, status=CANCELLED)

    async def claim_next(self, worker_id: str, *, capabilities: Optional[Mapping[str, str]] = None,
                         lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> Optional[RemoteJobRecord]:
        # No ``await`` between the scan and the stamp — one event loop never
        # interleaves that pair, so two coroutines cannot claim the same job.
        await self.requeue_expired(now=now)
        for rec in sorted(self._by_id.values(), key=lambda r: r.created_at):
            if rec.status != SUBMITTED or rec.claimed_by:
                continue
            # #2085 — the LANE, not just the status. A job the control plane
            # already pushed to a provider is SUBMITTED too; handing it to a
            # worker would run a paid job a second time on another machine.
            if rec.backend != PULL_BACKEND:
                continue
            if not capabilities_match(rec.requires, capabilities):
                continue
            self._update(rec.id, **_claim_stamp(rec, worker_id, lease_s, now))
            return self._by_id[rec.id]
        return None

    async def heartbeat(self, record_id: str, worker_id: str, status: JobStatus, *,
                        lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> bool:
        cur = self._by_id.get(record_id)
        if cur is None or cur.claimed_by != worker_id:
            return False
        self._update(record_id, **_heartbeat_stamp(worker_id, status, lease_s, now))
        return True

    async def requeue_expired(self, *, now: Optional[str] = None) -> int:
        n = 0
        for rec in list(self._by_id.values()):
            if rec.status in _ACTIVE and lease_expired(rec, now):
                self._update(rec.id, **_requeue_stamp(rec))
                n += 1
        return n

    async def set_checkpoint_uri(self, record_id: str, uri: str) -> None:
        self._update(record_id, checkpoint_uri=uri)

    async def set_log_tail(self, record_id: str, tail: str) -> None:
        self._update(record_id, last_log_tail=tail)

    async def mark_relaunch(self, record_id: str, handle: JobHandle) -> None:
        cur = self._by_id.get(record_id)
        if cur is None:
            return
        self._update(
            record_id, job_id=handle.job_id, backend=handle.backend,
            job_meta=dict(handle.meta), status=SUBMITTED, error=None,
            attempts=cur.attempts + 1,
        )

    async def list(self, *, status: Optional[str] = None, since: Optional[str] = None, workspace_id: Optional[str] = None, limit: int = 200) -> list[RemoteJobRecord]:
        rows = [
            r for r in self._by_id.values()
            if (status is None or r.status == status)
            and (since is None or r.created_at >= since)
            and (workspace_id is None or r.workspace_id == workspace_id)  # tenancy scope (#239)
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def close(self) -> None:
        pass


# ─── sqlite (durable default — mirrors SqliteTriggerInbox) ───────────────


class SqliteRemoteJobStore:
    """Embedded-SQLite store — restart-safe, single file. Full record in a JSON
    ``data`` blob; ``status`` mirrored to a column so the claim query is indexable.
    Sqlite work runs in a thread under a lock so it never blocks the loop."""

    def __init__(self, path: str | None = None) -> None:
        import sqlite3

        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_JOB_STORE_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS remote_jobs ("
            "id TEXT PRIMARY KEY, run_id TEXT, status TEXT, "
            "workspace_id TEXT NOT NULL DEFAULT 'default', created_at TEXT, data TEXT NOT NULL)"
        )
        # workspace_id is a real INDEXED column (ADR-0051): migrate pre-existing
        # DBs (add column + back-fill from the JSON blob once) + index the predicate.
        from cairn.core.storage import migrations
        if "workspace_id" in migrations.add_missing_columns(
            self._conn, "remote_jobs", {"workspace_id": "TEXT NOT NULL DEFAULT 'default'"}
        ):
            self._conn.execute(
                "UPDATE remote_jobs SET workspace_id=COALESCE(json_extract(data,'$.workspace_id'),'default')"
            )
        # ADR-0104 D2 (#565): the claim is a CONDITIONAL UPDATE on real columns,
        # so two workers racing on one job cannot both win. Mirrored into the
        # JSON blob like `status`; migrated in place on a pre-existing DB.
        migrations.add_missing_columns(
            self._conn, "remote_jobs", {"claimed_by": "TEXT", "lease_until": "TEXT"}
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS remote_jobs_active ON remote_jobs(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ws_created ON remote_jobs(workspace_id, created_at)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_claim ON remote_jobs(status, claimed_by, created_at)")
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _put_sync(self, rec: RemoteJobRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO remote_jobs (id, run_id, status, workspace_id, created_at, "
            "claimed_by, lease_until, data) VALUES (?,?,?,?,?,?,?,?)",
            (rec.id, rec.run_id, rec.status, rec.workspace_id, rec.created_at,
             rec.claimed_by, rec.lease_until, rec.model_dump_json()),
        )
        self._conn.commit()

    async def register(self, record: RemoteJobRecord) -> RemoteJobRecord:
        async with self._lock:
            await asyncio.to_thread(self._put_sync, record)
        return record

    def _get_sync(self, record_id: str) -> Optional[RemoteJobRecord]:
        row = self._conn.execute("SELECT data FROM remote_jobs WHERE id=?", (record_id,)).fetchone()
        return RemoteJobRecord.model_validate_json(row[0]) if row else None

    async def get(self, record_id: str) -> Optional[RemoteJobRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, record_id)

    def _claim_sync(self, limit: int) -> list[RemoteJobRecord]:
        rows = self._conn.execute(
            "SELECT data FROM remote_jobs WHERE status IN (?,?) ORDER BY created_at ASC LIMIT ?",
            (SUBMITTED, RUNNING, limit),
        ).fetchall()
        return [RemoteJobRecord.model_validate_json(r[0]) for r in rows]

    async def claim_active(self, *, now: Optional[str] = None, limit: int = 50) -> list[RemoteJobRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._claim_sync, limit)

    def _update_sync(self, record_id: str, fields: dict[str, Any]) -> None:
        row = self._conn.execute("SELECT data FROM remote_jobs WHERE id=?", (record_id,)).fetchone()
        if row is None:
            return
        rec = RemoteJobRecord.model_validate_json(row[0]).model_copy(update={**fields, "updated_at": utc_now_iso()})
        self._conn.execute(
            "UPDATE remote_jobs SET status=?, claimed_by=?, lease_until=?, data=? WHERE id=?",
            (rec.status, rec.claimed_by, rec.lease_until, rec.model_dump_json(), record_id),
        )
        self._conn.commit()

    async def mark_running(self, record_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_sync, record_id, {"status": RUNNING})

    async def mark_done(self, record_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_sync, record_id, {"status": DONE, "result": dict(result)})

    async def mark_failed(self, record_id: str, error: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_sync, record_id, {"status": FAILED, "error": error})

    async def mark_cancelled(self, record_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_sync, record_id, {"status": CANCELLED})

    # ── the pull lane (ADR-0104 D2, #565) ──
    #
    # The claim is a conditional UPDATE (`WHERE id=? AND claimed_by IS NULL`)
    # rather than a read-then-write, so two workers — in this process or in two
    # processes sharing the file — cannot both win the same job.

    def _requeue_expired_sync(self, now: Optional[str]) -> int:
        cutoff = now or utc_now_iso()
        rows = self._conn.execute(
            "SELECT data FROM remote_jobs WHERE claimed_by IS NOT NULL AND lease_until IS NOT NULL "
            "AND lease_until < ? AND status IN (?,?)",
            (cutoff, SUBMITTED, RUNNING),
        ).fetchall()
        for (data,) in rows:
            rec = RemoteJobRecord.model_validate_json(data)
            self._update_sync(rec.id, _requeue_stamp(rec))
        return len(rows)

    def _claim_next_sync(self, worker_id: str, capabilities: Optional[Mapping[str, str]],
                         lease_s: float, now: Optional[str]) -> Optional[RemoteJobRecord]:
        self._requeue_expired_sync(now)
        rows = self._conn.execute(
            # #2085 — pull-lane rows only; a pushed provider job is SUBMITTED
            # too and must never be offered to a worker. Filtered in SQL rather
            # than after the LIMIT, so a queue full of pushed jobs cannot push
            # claimable work outside the window.
            "SELECT id, data FROM remote_jobs WHERE status=? AND claimed_by IS NULL "
            "AND json_extract(data, '$.backend')=? "
            "ORDER BY created_at ASC LIMIT 100",
            (SUBMITTED, PULL_BACKEND),
        ).fetchall()
        for rid, data in rows:
            rec = RemoteJobRecord.model_validate_json(data)
            if not capabilities_match(rec.requires, capabilities):
                continue
            stamp = _claim_stamp(rec, worker_id, lease_s, now)
            claimed = rec.model_copy(update=stamp)
            cur = self._conn.execute(
                "UPDATE remote_jobs SET claimed_by=?, lease_until=?, data=? "
                "WHERE id=? AND claimed_by IS NULL AND status=?",
                (claimed.claimed_by, claimed.lease_until, claimed.model_dump_json(), rid, SUBMITTED),
            )
            self._conn.commit()
            if cur.rowcount == 1:
                return claimed
        return None

    async def claim_next(self, worker_id: str, *, capabilities: Optional[Mapping[str, str]] = None,
                         lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> Optional[RemoteJobRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._claim_next_sync, worker_id, capabilities, lease_s, now)

    def _heartbeat_sync(self, record_id: str, worker_id: str, status: JobStatus,
                        lease_s: float, now: Optional[str]) -> bool:
        row = self._conn.execute(
            "SELECT id FROM remote_jobs WHERE id=? AND claimed_by=?", (record_id, worker_id)
        ).fetchone()
        if row is None:
            return False
        self._update_sync(record_id, _heartbeat_stamp(worker_id, status, lease_s, now))
        return True

    async def heartbeat(self, record_id: str, worker_id: str, status: JobStatus, *,
                        lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._heartbeat_sync, record_id, worker_id, status, lease_s, now)

    async def requeue_expired(self, *, now: Optional[str] = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._requeue_expired_sync, now)

    async def set_checkpoint_uri(self, record_id: str, uri: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_sync, record_id, {"checkpoint_uri": uri})

    async def set_log_tail(self, record_id: str, tail: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_sync, record_id, {"last_log_tail": tail})

    def _relaunch_sync(self, record_id: str, handle: JobHandle) -> None:
        row = self._conn.execute("SELECT data FROM remote_jobs WHERE id=?", (record_id,)).fetchone()
        if row is None:
            return
        cur = RemoteJobRecord.model_validate_json(row[0])
        self._update_sync(record_id, {
            "job_id": handle.job_id, "backend": handle.backend, "job_meta": dict(handle.meta),
            "status": SUBMITTED, "error": None, "attempts": cur.attempts + 1,
        })

    async def mark_relaunch(self, record_id: str, handle: JobHandle) -> None:
        async with self._lock:
            await asyncio.to_thread(self._relaunch_sync, record_id, handle)

    def _list_sync(self, status: Optional[str], since: Optional[str], workspace_id: Optional[str], limit: int) -> list[RemoteJobRecord]:
        sql, params, clauses = "SELECT data FROM remote_jobs", [], []
        if status is not None:
            clauses.append("status=?"); params.append(status)
        if since is not None:
            clauses.append("created_at>=?"); params.append(since)
        if workspace_id is not None:
            # filtered on the real indexed workspace_id column (ADR-0051)
            clauses.append("workspace_id=?"); params.append(workspace_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [RemoteJobRecord.model_validate_json(r[0]) for r in rows]

    async def list(self, *, status: Optional[str] = None, since: Optional[str] = None, workspace_id: Optional[str] = None, limit: int = 200) -> list[RemoteJobRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._list_sync, status, since, workspace_id, limit)

    def close(self) -> None:
        self._conn.close()


# ── Postgres backend (mirrors storage/postgres/runs.py + PostgresConnectionStore) — #427 ──
_CREATE_JOBS_PG_SQL = """
    CREATE TABLE IF NOT EXISTS remote_jobs (
        id           TEXT PRIMARY KEY,
        run_id       TEXT,
        status       TEXT,
        workspace_id TEXT NOT NULL DEFAULT 'default',
        created_at   TEXT,
        data         JSONB NOT NULL
    )
"""
_CREATE_JOBS_PG_STATUS_IDX_SQL = "CREATE INDEX IF NOT EXISTS remote_jobs_active ON remote_jobs(status)"
_CREATE_JOBS_PG_WS_IDX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_ws_created ON remote_jobs(workspace_id, created_at)"
)
_INSERT_JOB_PG_SQL = """
    INSERT INTO remote_jobs (id, run_id, status, workspace_id, created_at, claimed_by, lease_until, data)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
    ON CONFLICT (id) DO UPDATE
    SET run_id = EXCLUDED.run_id, status = EXCLUDED.status,
        workspace_id = EXCLUDED.workspace_id, created_at = EXCLUDED.created_at,
        claimed_by = EXCLUDED.claimed_by, lease_until = EXCLUDED.lease_until,
        data = EXCLUDED.data
"""
_SELECT_JOB_PG_BY_ID = "SELECT data::text FROM remote_jobs WHERE id = $1"
_CLAIM_JOBS_PG_SQL = (
    "SELECT data::text FROM remote_jobs WHERE status IN ($1, $2) ORDER BY created_at ASC LIMIT $3"
)
_UPDATE_JOB_PG_SQL = (
    "UPDATE remote_jobs SET status = $2, claimed_by = $4, lease_until = $5, data = $3::jsonb WHERE id = $1"
)
# ADR-0104 D2 (#565) — the pull lane's schema delta + its two atomic writes.
# ADD COLUMN IF NOT EXISTS migrates a table that predates the claim (the #2021
# pattern); the claim itself is a CONDITIONAL UPDATE, so two workers racing on
# one job cannot both win — the loser's rowcount is 0 and it moves on.
_MIGRATE_JOBS_PG_SQL = (
    "ALTER TABLE remote_jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT",
    "ALTER TABLE remote_jobs ADD COLUMN IF NOT EXISTS lease_until TEXT",
)
_CREATE_JOBS_PG_CLAIM_IDX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON remote_jobs(status, claimed_by, created_at)"
)
_CLAIMABLE_JOBS_PG_SQL = (
    # #2085 — pull-lane rows only (the backend lives inside the jsonb document);
    # a pushed provider job is SUBMITTED too and is not claimable work.
    "SELECT data::text FROM remote_jobs WHERE status = $1 AND claimed_by IS NULL "
    "AND data->>'backend' = $2 "
    "ORDER BY created_at ASC LIMIT 100"
)
_TAKE_JOB_PG_SQL = (
    "UPDATE remote_jobs SET claimed_by = $2, lease_until = $3, data = $4::jsonb "
    "WHERE id = $1 AND claimed_by IS NULL AND status = $5 RETURNING data::text"
)
_EXPIRED_JOBS_PG_SQL = (
    "SELECT data::text FROM remote_jobs WHERE claimed_by IS NOT NULL AND lease_until IS NOT NULL "
    "AND lease_until < $1 AND status IN ($2, $3)"
)
_HEARTBEAT_OWNER_PG_SQL = "SELECT data::text FROM remote_jobs WHERE id = $1 AND claimed_by = $2"


class PostgresRemoteJobStore(PgSchemaInit):
    """Postgres-backed remote-job registry — mirrors :class:`SqliteRemoteJobStore`
    (full :class:`RemoteJobRecord` in a JSONB ``data`` column, ``status`` +
    ``workspace_id`` + ``created_at`` mirrored to indexed columns) and
    :class:`~cairn.core.connections.PostgresConnectionStore` (lazy asyncpg pool,
    created on first call). Selected by :func:`get_remote_job_store` when
    ``CAIRN_STORAGE=postgres://…`` so the control worker's claim-loop reads from
    the one prod Postgres instead of a per-container SQLite file — the
    storage-unification move for this store (#427)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)
        except ImportError as e:
            raise RuntimeError(
                "Postgres remote-job store requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresRemoteJobStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_JOBS_PG_SQL)
            for stmt in _MIGRATE_JOBS_PG_SQL:
                await c.execute(stmt)
            await c.execute(_CREATE_JOBS_PG_STATUS_IDX_SQL)
            await c.execute(_CREATE_JOBS_PG_WS_IDX_SQL)
            await c.execute(_CREATE_JOBS_PG_CLAIM_IDX_SQL)

    async def register(self, record: RemoteJobRecord) -> RemoteJobRecord:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(
                _INSERT_JOB_PG_SQL,
                record.id, record.run_id, record.status, record.workspace_id,
                record.created_at, record.claimed_by, record.lease_until, record.model_dump_json(),
            )
        return record

    async def get(self, record_id: str) -> Optional[RemoteJobRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_JOB_PG_BY_ID, record_id)
        return RemoteJobRecord.model_validate_json(row["data"]) if row else None

    async def claim_active(self, *, now: Optional[str] = None, limit: int = 50) -> list[RemoteJobRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_CLAIM_JOBS_PG_SQL, SUBMITTED, RUNNING, limit)
        return [RemoteJobRecord.model_validate_json(r["data"]) for r in rows]

    async def _update(self, record_id: str, **fields: Any) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(_SELECT_JOB_PG_BY_ID, record_id)
                if row is None:
                    return
                rec = RemoteJobRecord.model_validate_json(row["data"]).model_copy(
                    update={**fields, "updated_at": utc_now_iso()}
                )
                await c.execute(_UPDATE_JOB_PG_SQL, record_id, rec.status, rec.model_dump_json(),
                                rec.claimed_by, rec.lease_until)

    # ── the pull lane (ADR-0104 D2, #565) ──

    async def claim_next(self, worker_id: str, *, capabilities: Optional[Mapping[str, str]] = None,
                         lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> Optional[RemoteJobRecord]:
        await self.requeue_expired(now=now)
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_CLAIMABLE_JOBS_PG_SQL, SUBMITTED, PULL_BACKEND)
            for row in rows:
                rec = RemoteJobRecord.model_validate_json(row["data"])
                if not capabilities_match(rec.requires, capabilities):
                    continue
                claimed = rec.model_copy(update=_claim_stamp(rec, worker_id, lease_s, now))
                got = await c.fetchrow(_TAKE_JOB_PG_SQL, rec.id, claimed.claimed_by,
                                       claimed.lease_until, claimed.model_dump_json(), SUBMITTED)
                if got is not None:  # someone else won the race → try the next one
                    return RemoteJobRecord.model_validate_json(got["data"])
        return None

    async def heartbeat(self, record_id: str, worker_id: str, status: JobStatus, *,
                        lease_s: float = DEFAULT_LEASE_S, now: Optional[str] = None) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_HEARTBEAT_OWNER_PG_SQL, record_id, worker_id)
        if row is None:
            return False
        await self._update(record_id, **_heartbeat_stamp(worker_id, status, lease_s, now))
        return True

    async def requeue_expired(self, *, now: Optional[str] = None) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_EXPIRED_JOBS_PG_SQL, now or utc_now_iso(), SUBMITTED, RUNNING)
        for row in rows:
            rec = RemoteJobRecord.model_validate_json(row["data"])
            await self._update(rec.id, **_requeue_stamp(rec))
        return len(rows)

    async def set_checkpoint_uri(self, record_id: str, uri: str) -> None:
        await self._update(record_id, checkpoint_uri=uri)

    async def set_log_tail(self, record_id: str, tail: str) -> None:
        await self._update(record_id, last_log_tail=tail)

    async def mark_relaunch(self, record_id: str, handle: JobHandle) -> None:
        cur = await self.get(record_id)
        if cur is None:
            return
        await self._update(record_id, job_id=handle.job_id, backend=handle.backend,
                           job_meta=dict(handle.meta), status=SUBMITTED, error=None,
                           attempts=cur.attempts + 1)

    async def mark_running(self, record_id: str) -> None:
        await self._update(record_id, status=RUNNING)

    async def mark_done(self, record_id: str, result: dict[str, Any]) -> None:
        await self._update(record_id, status=DONE, result=dict(result))

    async def mark_failed(self, record_id: str, error: str) -> None:
        await self._update(record_id, status=FAILED, error=error)

    async def mark_cancelled(self, record_id: str) -> None:
        await self._update(record_id, status=CANCELLED)

    async def list(self, *, status: Optional[str] = None, since: Optional[str] = None, workspace_id: Optional[str] = None, limit: int = 200) -> list[RemoteJobRecord]:
        pool = await self._get_pool()
        sql, params, clauses = "SELECT data::text FROM remote_jobs", [], []
        if status is not None:
            params.append(status); clauses.append(f"status = ${len(params)}")
        if since is not None:
            params.append(since); clauses.append(f"created_at >= ${len(params)}")
        if workspace_id is not None:
            # filtered on the real indexed workspace_id column (ADR-0051)
            params.append(workspace_id); clauses.append(f"workspace_id = ${len(params)}")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        params.append(limit)
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)}"
        async with pool.acquire() as c:
            rows = await c.fetch(sql, *params)
        return [RemoteJobRecord.model_validate_json(r["data"]) for r in rows]

    def close(self) -> None:
        # The pool lives for the process (parity with SqliteRemoteJobStore.close,
        # which closes its single conn); pool teardown happens with the event loop.
        pass


_STORE: Optional[RemoteJobStore] = None


def get_remote_job_store() -> RemoteJobStore:
    """The process remote-job store. **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…``/``postgresql://…`` → :class:`PostgresRemoteJobStore` (the
    one prod Postgres, shared with the run/connection stores). Otherwise falls
    back to the legacy ``CAIRN_JOB_STORE`` knob — durable sqlite by default (so a
    registered job survives a restart and the worker re-attaches);
    ``memory``/``:memory:`` opts into the volatile store (tests)."""
    global _STORE
    storage_url = getenv("CAIRN_STORAGE", "").strip()
    if storage_url.startswith(("postgres://", "postgresql://")):
        return PostgresRemoteJobStore(dsn=storage_url)  # not cached — mirrors PostgresConnectionStore
    spec = getenv("CAIRN_JOB_STORE", "").strip().lower()
    if spec in ("memory", ":memory:"):
        return InMemoryRemoteJobStore()  # tests construct explicitly; not cached
    if _STORE is None:
        _STORE = SqliteRemoteJobStore()
    return _STORE


def reset_remote_job_store() -> None:
    global _STORE
    _STORE = None


__all__ = [
    "SUBMITTED", "RUNNING", "DONE", "FAILED", "CANCELLED",
    "PULL_BACKEND", "DEFAULT_LEASE_S",
    "capabilities_match", "heartbeat_status", "is_pull_job", "lease_expired",
    "RemoteJobRecord", "RemoteJobStore",
    "InMemoryRemoteJobStore", "SqliteRemoteJobStore", "PostgresRemoteJobStore",
    "get_remote_job_store", "reset_remote_job_store",
]
