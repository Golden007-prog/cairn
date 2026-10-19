"""Review records (#1468) — one decision object per gated human verdict.

The C2 campaign's motivating evidence: every promotion verdict was assembled
by hand from five places (gate ``show`` fields, measure-run findings, MLflow,
S3 lineage, a GitHub issue comment). This store gives the decision itself a
governed record: the gate snapshot at fire time, references to the evaluation
outputs that informed it, and the human verdict + rationale — created when a
HITL gate fires, decided by the SAME approve path that releases the gate
(never a parallel approval surface).

Owner rulings baked in: platform-wide (any gated decision — model/dataset
promotions, config proposals, red-team sign-off — not just training), and the
review GENERATES no evaluation of its own: ``judge_findings`` strictly
REFERENCES existing auto-eval/measure outputs.

Null tier is a silent no-op (reads empty): the gate queue itself remains the
source of truth for pending approvals — a review record enriches it, and an
unconfigured store must never block a gate.

Workspace scoping (#1639): a review is stamped with the workspace of the
pipeline whose gate opened it, and reads filter on it. ``workspace_id=None``
on a read means "every workspace" — reserved for internal callers that
legitimately span tenants; the HTTP surface always passes a concrete one.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
import sqlite3
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import schema_init_lock
from cairn.core.storage.migrations import add_missing_columns
from cairn.core.workspace import DEFAULT_WORKSPACE


class ReviewRecord(BaseModel):
    """One gated decision — open while the human hasn't ruled."""

    #: Deterministic for pipeline gates (``rev:<pipeline>:<step>``) so the
    #: approve path finds its review without extra state.
    review_id: str
    #: What is being decided: pipeline_step today; model/dataset/config
    #: subjects reuse the same record as their gates adopt it.
    subject_kind: str = "pipeline_step"
    subject_ref: str = ""
    title: str = ""
    pipeline_id: str = ""
    step_id: str = ""
    #: The ``show:`` fields + gate context AT FIRE TIME (what the approver saw).
    gate_snapshot: dict[str, Any] = Field(default_factory=dict)
    #: REFERENCES to existing eval outputs (run ids, finding keys) — never
    #: content this record computed itself.
    judge_findings: dict[str, Any] = Field(default_factory=dict)
    #: Upstream refs (per-step run ids, dataset/model refs) for the drill-down.
    lineage: dict[str, Any] = Field(default_factory=dict)
    #: Tenancy scope (#1639). Taken from the PIPELINE record, not from
    #: ``current_workspace()`` — the bridge also runs inside a Temporal worker
    #: where the request-scoped ContextVar is unset and would silently stamp
    #: every review "default".
    workspace_id: str = DEFAULT_WORKSPACE
    status: str = "open"          # open | approved | rejected
    rationale: str = ""
    reviewer: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    decided_at: Optional[str] = None


@runtime_checkable
class ReviewStore(Protocol):
    async def ensure_schema(self) -> None: ...
    async def open_review(self, record: ReviewRecord) -> ReviewRecord: ...
    async def get_review(
        self, review_id: str, *, workspace_id: Optional[str] = None,
    ) -> Optional[ReviewRecord]: ...
    async def list_reviews(
        self, *, status: Optional[str] = None, limit: int = 100,
        workspace_id: Optional[str] = None,
    ) -> list[ReviewRecord]: ...
    async def decide_review(
        self, review_id: str, verdict: str, *, reviewer: str = "", rationale: str = "",
    ) -> Optional[ReviewRecord]: ...
    async def close(self) -> None: ...


def pipeline_review_id(pipeline_id: str, step_id: str) -> str:
    return f"rev:{pipeline_id}:{step_id}"


def _in_workspace(r: ReviewRecord, workspace_id: Optional[str]) -> bool:
    """#1639 — ``None`` means unscoped (internal callers). Rows written before
    the column existed carry no workspace and read as ``default``."""
    if workspace_id is None:
        return True
    return (r.workspace_id or DEFAULT_WORKSPACE) == workspace_id


# ─── null (unconfigured storage — advisory no-op) ─────────────────────────────


class _NullReviewStore:
    async def ensure_schema(self) -> None:
        return None

    async def open_review(self, record) -> ReviewRecord:
        return record  # gate queue stays the source of truth; nothing persisted

    async def get_review(self, review_id, *, workspace_id=None) -> Optional[ReviewRecord]:
        return None

    async def list_reviews(self, *, status=None, limit=100, workspace_id=None) -> list[ReviewRecord]:
        return []

    async def decide_review(self, review_id, verdict, *, reviewer="", rationale="") -> Optional[ReviewRecord]:
        return None

    async def close(self) -> None:
        return None


# ─── in-memory (tests) ────────────────────────────────────────────────────────


class InMemoryReviewStore:
    def __init__(self) -> None:
        self._rows: dict[str, ReviewRecord] = {}
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        return None

    async def open_review(self, record) -> ReviewRecord:
        async with self._lock:
            have = self._rows.get(record.review_id)
            if have is not None and have.status == "open":
                return have.model_copy(deep=True)   # re-fire (restart/replay) — keep the original
            self._rows[record.review_id] = record.model_copy(deep=True)
            return record

    async def get_review(self, review_id, *, workspace_id=None) -> Optional[ReviewRecord]:
        async with self._lock:
            r = self._rows.get(review_id)
        if r is None or not _in_workspace(r, workspace_id):
            return None
        return r.model_copy(deep=True)

    async def list_reviews(self, *, status=None, limit=100, workspace_id=None) -> list[ReviewRecord]:
        async with self._lock:
            rows = [r for r in self._rows.values()
                    if (status is None or r.status == status) and _in_workspace(r, workspace_id)]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return [r.model_copy(deep=True) for r in rows[:limit]]

    async def decide_review(self, review_id, verdict, *, reviewer="", rationale="") -> Optional[ReviewRecord]:
        async with self._lock:
            r = self._rows.get(review_id)
            if r is None or r.status != "open":
                return None
            decided = r.model_copy(update={
                "status": verdict, "reviewer": reviewer, "rationale": rationale,
                "decided_at": utc_now_iso(),
            })
            self._rows[review_id] = decided
            return decided.model_copy(deep=True)

    async def close(self) -> None:
        return None


# ─── sqlite ───────────────────────────────────────────────────────────────────


_CREATE_SQLITE = """
    CREATE TABLE IF NOT EXISTS reviews (
        review_id TEXT PRIMARY KEY, status TEXT NOT NULL,
        subject_kind TEXT NOT NULL, pipeline_id TEXT,
        workspace_id TEXT NOT NULL DEFAULT 'default',
        created_at TEXT NOT NULL, data TEXT NOT NULL
    )
"""


class SqliteReviewStore:
    """JSON-in-``data`` with mirrored filter columns (suggestions shape)."""

    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_SQLITE)
        # #1639 — CREATE TABLE IF NOT EXISTS is a no-op on a DB created before
        # workspace_id existed, so the next query would raise "no such column".
        # Existing rows land in `default`, which is where they were written.
        add_missing_columns(self._conn, "reviews",
                            {"workspace_id": "TEXT NOT NULL DEFAULT 'default'"})
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_reviews_ws ON reviews(workspace_id)")
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        return None

    def _get_sync(self, review_id: str, workspace_id: Optional[str] = None) -> Optional[ReviewRecord]:
        row = self._conn.execute(
            "SELECT data FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if row is None:
            return None
        rec = ReviewRecord.model_validate_json(row[0])
        return rec if _in_workspace(rec, workspace_id) else None

    def _put_sync(self, r: ReviewRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO reviews (review_id, status, subject_kind, pipeline_id, "
            "workspace_id, created_at, data) VALUES (?,?,?,?,?,?,?)",
            (r.review_id, r.status, r.subject_kind, r.pipeline_id,
             r.workspace_id or DEFAULT_WORKSPACE, r.created_at, r.model_dump_json()))
        self._conn.commit()

    async def open_review(self, record) -> ReviewRecord:
        async with self._lock:
            have = await asyncio.to_thread(self._get_sync, record.review_id)
            if have is not None and have.status == "open":
                return have
            await asyncio.to_thread(self._put_sync, record)
        return record

    async def get_review(self, review_id, *, workspace_id=None) -> Optional[ReviewRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, review_id, workspace_id)

    async def list_reviews(self, *, status=None, limit=100, workspace_id=None) -> list[ReviewRecord]:
        def go() -> list[ReviewRecord]:
            clauses, args = [], []
            if status is not None:
                clauses.append("status=?"); args.append(status)
            if workspace_id is not None:
                clauses.append("workspace_id=?"); args.append(workspace_id)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            args.append(limit)
            rows = self._conn.execute(
                f"SELECT data FROM reviews{where} ORDER BY created_at DESC LIMIT ?", args).fetchall()
            return [ReviewRecord.model_validate_json(r[0]) for r in rows]
        async with self._lock:
            return await asyncio.to_thread(go)

    async def decide_review(self, review_id, verdict, *, reviewer="", rationale="") -> Optional[ReviewRecord]:
        async with self._lock:
            r = await asyncio.to_thread(self._get_sync, review_id)
            if r is None or r.status != "open":
                return None
            decided = r.model_copy(update={
                "status": verdict, "reviewer": reviewer, "rationale": rationale,
                "decided_at": utc_now_iso(),
            })
            await asyncio.to_thread(self._put_sync, decided)
            return decided

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()


# ─── postgres ─────────────────────────────────────────────────────────────────


_CREATE_PG = """
    CREATE TABLE IF NOT EXISTS reviews (
        review_id TEXT PRIMARY KEY, status TEXT NOT NULL,
        subject_kind TEXT NOT NULL, pipeline_id TEXT,
        workspace_id TEXT NOT NULL DEFAULT 'default',
        created_at TEXT NOT NULL, data TEXT NOT NULL
    )
"""

# #1639 — the same add-column migration as SQLite, for a table created before
# workspace_id existed. Postgres supports IF NOT EXISTS here, so no probe.
_MIGRATE_PG = (
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default'"
)
_CREATE_IX_PG = "CREATE INDEX IF NOT EXISTS ix_reviews_ws ON reviews(workspace_id)"


class PostgresReviewStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._schema_ready = False

    async def _pool(self):
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415 - shared process pool (#614)
        return await shared_pool(self._dsn)

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        pool = await self._pool()
        async with pool.acquire() as conn, schema_init_lock(conn):  # #2551 — replicas serialise DDL
            await conn.execute(_CREATE_PG)
            await conn.execute(_MIGRATE_PG)
            await conn.execute(_CREATE_IX_PG)
        self._schema_ready = True

    async def open_review(self, record) -> ReviewRecord:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT data FROM reviews WHERE review_id=$1 FOR UPDATE", record.review_id)
                if row is not None:
                    have = ReviewRecord.model_validate_json(row["data"])
                    if have.status == "open":
                        return have
                await conn.execute(
                    "INSERT INTO reviews (review_id, status, subject_kind, pipeline_id, "
                    "workspace_id, created_at, data) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                    "ON CONFLICT (review_id) DO UPDATE SET status=EXCLUDED.status, "
                    "data=EXCLUDED.data, created_at=EXCLUDED.created_at, "
                    "workspace_id=EXCLUDED.workspace_id",
                    record.review_id, record.status, record.subject_kind,
                    record.pipeline_id, record.workspace_id or DEFAULT_WORKSPACE,
                    record.created_at, record.model_dump_json())
        return record

    async def get_review(self, review_id, *, workspace_id=None) -> Optional[ReviewRecord]:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM reviews WHERE review_id=$1", review_id)
        if row is None:
            return None
        rec = ReviewRecord.model_validate_json(row["data"])
        return rec if _in_workspace(rec, workspace_id) else None

    async def list_reviews(self, *, status=None, limit=100, workspace_id=None) -> list[ReviewRecord]:
        await self.ensure_schema()
        clauses, args = [], []
        if status is not None:
            args.append(status); clauses.append(f"status=${len(args)}")
        if workspace_id is not None:
            args.append(workspace_id); clauses.append(f"workspace_id=${len(args)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT data FROM reviews{where} ORDER BY created_at DESC LIMIT ${len(args)}", *args)
        return [ReviewRecord.model_validate_json(r["data"]) for r in rows]

    async def decide_review(self, review_id, verdict, *, reviewer="", rationale="") -> Optional[ReviewRecord]:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT data FROM reviews WHERE review_id=$1 FOR UPDATE", review_id)
                if row is None:
                    return None
                r = ReviewRecord.model_validate_json(row["data"])
                if r.status != "open":
                    return None
                decided = r.model_copy(update={
                    "status": verdict, "reviewer": reviewer, "rationale": rationale,
                    "decided_at": utc_now_iso(),
                })
                await conn.execute(
                    "UPDATE reviews SET status=$2, data=$3 WHERE review_id=$1",
                    review_id, decided.status, decided.model_dump_json())
        return decided

    async def close(self) -> None:
        return None  # shared pool is process-owned


def build_review_store(dsn: Optional[str] = None) -> ReviewStore:
    """Pick a backend by ``dsn`` (falls back to ``CAIRN_STORAGE``), mirroring
    :func:`cairn.core.storage.artifacts.build_artifact_store`."""
    url = (dsn or getenv("CAIRN_STORAGE", "")).strip()
    if not url:
        return _NullReviewStore()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresReviewStore(dsn=url)
    if url.startswith("sqlite://"):
        # #1471 — ONE parser (settings.sqlite_path_from_url) so every store
        # resolves the same CAIRN_STORAGE to the same file.
        from cairn.core import settings  # noqa: PLC0415 - cycle: settings imports storage on some paths
        return SqliteReviewStore(path=settings.sqlite_path_from_url(url) or ":memory:")
    return _NullReviewStore()


__all__ = [
    "InMemoryReviewStore",
    "PostgresReviewStore",
    "ReviewRecord",
    "ReviewStore",
    "SqliteReviewStore",
    "build_review_store",
    "pipeline_review_id",
]
