"""Suggestion store (#478, ADR-0012 §24.12 Form 4) — the persisted queue of
GOVERNED improvement suggestions the closed loop proposes.

A suggestion is a PROPOSED change to a versioned artifact (a template prompt, a
model-role binding, a policy, an eval dataset) with the evidence that motivated
it — never an applied change. It carries state (proposed → accepted/rejected) so
a rejected cluster fingerprint never re-surfaces and a proposed one isn't
duplicated. Actuation (turning an accepted suggestion into a PR / console
approval) is the #488 actuator's job — this module only persists the queue.

Three tiers mirror the eval ledger (``CAIRN_STORAGE`` selects): InMemory (tests),
SQLite (embedded default), Postgres (prod). Same JSON-in-``data`` + mirrored
filter columns shape as :mod:`cairn.core.storage.eval_ledger`.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
import sqlite3
import uuid
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import schema_init_lock
from cairn.core.storage.migrations import add_missing_columns
from cairn.core.workspace import DEFAULT_WORKSPACE

#: Terminal states a fingerprint is "spoken for" in — a proposed suggestion is
#: live, a rejected one must not re-surface for the same cluster (issue #478).
_SPOKEN_FOR = ("proposed", "accepted", "rejected")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Suggestion(BaseModel):
    """One governed improvement suggestion (ADR-0012 §24.12 schema)."""

    suggestion_id: str = Field(default_factory=lambda: _new_id("sug"))
    #: Tenancy scope (#1644). A suggestion is derived from a workspace's OWN
    #: runs, so it belongs to that workspace — the same treatment reviews got in
    #: #1639. Before this the store had no scope at all, so one workspace could
    #: read AND DECIDE another's suggestions: a write, not just a read.
    workspace_id: str = DEFAULT_WORKSPACE
    #: The dedupe key — the cluster's (failure_type|locus|signature) fingerprint.
    cluster_fingerprint: str = ""
    #: Optimizer category the failure_type routed to (prompt_quality, model_routing, …).
    category: str = ""
    #: The versioned artifact the change lands on (e.g. "template:compliance/report").
    target: str = ""
    title: str = ""
    #: The diagnosis in plain terms.
    description: str = ""
    #: The proposed change (a diff / structured edit). Empty until an optimizer
    #: executor fills it — Phase 1 emits the routed intent + evidence, not a diff.
    proposed_diff: str = ""
    #: Cluster + member runs + expected metric delta — why this suggestion exists.
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    status: str = "proposed"  # proposed | accepted | rejected
    reason: str = ""          # human reason on reject (or note on accept)
    created_at: str = Field(default_factory=utc_now_iso)
    decided_at: Optional[str] = None


def _in_workspace(s: "Suggestion", workspace_id: Optional[str]) -> bool:
    """#1644 — ``None`` means unscoped. Rows written before the field existed
    carry no workspace and read as ``default``, where they were written."""
    if workspace_id is None:
        return True
    return (s.workspace_id or DEFAULT_WORKSPACE) == workspace_id


@runtime_checkable
class SuggestionStore(Protocol):
    """``workspace_id=None`` on a read means EVERY workspace — reserved for
    internal callers that legitimately span tenants. The HTTP surface always
    passes a concrete one."""

    async def add_suggestion(self, suggestion: Suggestion) -> Suggestion: ...
    async def list_suggestions(
        self, status: Optional[str] = None, *, workspace_id: Optional[str] = None,
    ) -> list[Suggestion]: ...
    async def get_suggestion(
        self, suggestion_id: str, *, workspace_id: Optional[str] = None,
    ) -> Optional[Suggestion]: ...
    async def update_status(
        self, suggestion_id: str, status: str, reason: str = "",
        *, workspace_id: Optional[str] = None,
    ) -> Optional[Suggestion]: ...
    async def open_fingerprints(self, *, workspace_id: Optional[str] = None) -> set[str]: ...


# ─── Tier 0: in-memory ─────────────────────────────────────────────────


class InMemorySuggestionStore:
    def __init__(self) -> None:
        self._rows: dict[str, Suggestion] = {}
        self._lock = asyncio.Lock()

    async def add_suggestion(self, suggestion: Suggestion) -> Suggestion:
        async with self._lock:
            self._rows[suggestion.suggestion_id] = suggestion.model_copy(deep=True)
        return suggestion

    async def list_suggestions(self, status: Optional[str] = None, *, workspace_id=None) -> list[Suggestion]:
        async with self._lock:
            rows = [s for s in self._rows.values()
                    if (status is None or s.status == status) and _in_workspace(s, workspace_id)]
        rows.sort(key=lambda s: s.created_at, reverse=True)
        return [s.model_copy(deep=True) for s in rows]

    async def get_suggestion(self, suggestion_id: str, *, workspace_id=None) -> Optional[Suggestion]:
        async with self._lock:
            s = self._rows.get(suggestion_id)
        if s is None or not _in_workspace(s, workspace_id):
            return None
        return s.model_copy(deep=True)

    async def update_status(
        self, suggestion_id: str, status: str, reason: str = "", *, workspace_id=None,
    ) -> Optional[Suggestion]:
        async with self._lock:
            s = self._rows.get(suggestion_id)
            # The sharp end: deciding is a WRITE. Another workspace's suggestion
            # reads as absent so the route 404s rather than silently accepting.
            if s is None or not _in_workspace(s, workspace_id):
                return None
            updated = s.model_copy(update={"status": status, "reason": reason, "decided_at": utc_now_iso()})
            self._rows[suggestion_id] = updated
            return updated.model_copy(deep=True)

    async def open_fingerprints(self, *, workspace_id=None) -> set[str]:
        # Scoped too: this is the dedupe key. Unscoped, one workspace's rejected
        # suggestion would suppress the same finding in every other workspace —
        # a cross-tenant silence rather than a cross-tenant leak.
        async with self._lock:
            return {
                s.cluster_fingerprint
                for s in self._rows.values()
                if s.status in _SPOKEN_FOR and s.cluster_fingerprint
                and _in_workspace(s, workspace_id)
            }


# ─── Tier 1: SQLite ────────────────────────────────────────────────────


class SqliteSuggestionStore:
    def __init__(self, path: str | None = None) -> None:
        if path is None:
            from cairn.core import settings
            path = getenv("CAIRN_EVAL_LEDGER_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                suggestion_id       TEXT PRIMARY KEY,
                cluster_fingerprint TEXT,
                status              TEXT NOT NULL,
                workspace_id        TEXT NOT NULL DEFAULT 'default',
                created_at          TEXT NOT NULL,
                data                TEXT NOT NULL
            )
            """
        )
        # #1644 — CREATE TABLE IF NOT EXISTS is a no-op on a DB made before the
        # column existed, so the next query would raise "no such column".
        # Existing rows land in `default`, where they were written.
        add_missing_columns(self._conn, "suggestions",
                            {"workspace_id": "TEXT NOT NULL DEFAULT 'default'"})
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_suggestions_ws ON suggestions(workspace_id)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _upsert_sync(self, s: Suggestion) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO suggestions "
            "(suggestion_id, cluster_fingerprint, status, workspace_id, created_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (s.suggestion_id, s.cluster_fingerprint, s.status,
             s.workspace_id or DEFAULT_WORKSPACE, s.created_at, s.model_dump_json()),
        )
        self._conn.commit()

    async def add_suggestion(self, suggestion: Suggestion) -> Suggestion:
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, suggestion)
        return suggestion

    def _list_sync(self, status: Optional[str], workspace_id: Optional[str] = None) -> list[Suggestion]:
        clauses, args = [], []
        if status is not None:
            clauses.append("status = ?"); args.append(status)
        if workspace_id is not None:
            clauses.append("workspace_id = ?"); args.append(workspace_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT data FROM suggestions{where} ORDER BY created_at DESC", args).fetchall()
        return [Suggestion.model_validate_json(r[0]) for r in rows]

    async def list_suggestions(self, status: Optional[str] = None, *, workspace_id=None) -> list[Suggestion]:
        async with self._lock:
            return await asyncio.to_thread(self._list_sync, status, workspace_id)

    def _get_sync(self, suggestion_id: str, workspace_id: Optional[str] = None) -> Optional[Suggestion]:
        row = self._conn.execute(
            "SELECT data FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        if row is None:
            return None
        got = Suggestion.model_validate_json(row[0])
        return got if _in_workspace(got, workspace_id) else None

    async def get_suggestion(self, suggestion_id: str, *, workspace_id=None) -> Optional[Suggestion]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, suggestion_id, workspace_id)

    async def update_status(
        self, suggestion_id: str, status: str, reason: str = "", *, workspace_id=None,
    ) -> Optional[Suggestion]:
        async with self._lock:
            # Deciding is a WRITE — another workspace's suggestion reads as
            # absent here so the route 404s instead of accepting it.
            s = await asyncio.to_thread(self._get_sync, suggestion_id, workspace_id)
            if s is None:
                return None
            updated = s.model_copy(update={"status": status, "reason": reason, "decided_at": utc_now_iso()})
            await asyncio.to_thread(self._upsert_sync, updated)
            return updated

    def _open_fp_sync(self, workspace_id: Optional[str] = None) -> set[str]:
        sql = ("SELECT DISTINCT cluster_fingerprint FROM suggestions "
               "WHERE status IN ('proposed','accepted','rejected') AND cluster_fingerprint != ''")
        args: list = []
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            args.append(workspace_id)
        return {r[0] for r in self._conn.execute(sql, args).fetchall()}

    async def open_fingerprints(self, *, workspace_id=None) -> set[str]:
        async with self._lock:
            return await asyncio.to_thread(self._open_fp_sync, workspace_id)

    def close(self) -> None:
        self._conn.close()


# ─── Tier 2: Postgres ──────────────────────────────────────────────────


_CREATE_PG = [
    """
    CREATE TABLE IF NOT EXISTS suggestions (
        suggestion_id       TEXT PRIMARY KEY,
        cluster_fingerprint TEXT,
        status              TEXT NOT NULL,
        workspace_id        TEXT NOT NULL DEFAULT 'default',
        created_at          TEXT NOT NULL,
        data                TEXT NOT NULL
    )
    """,
    # #1644 — the same add-column migration as SQLite, for a table created
    # before the column existed. Postgres supports IF NOT EXISTS here.
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default'",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_ws ON suggestions(workspace_id)",
]


class PostgresSuggestionStore:
    """Postgres tier — mirrors :class:`PostgresEvalLedger` (JSON-in-``data``
    with mirrored filter columns, shared per-DSN pool).

    Added for #1471: without it, ``CAIRN_STORAGE=postgres://…`` still fell
    through to SQLite, so on prod the ``suggestions`` table lived on the
    container filesystem instead of the one cairn database — a table the
    migration chain could never reach.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._schema_ready = False

    async def _pool(self):
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415 - shared process pool (#614)
        pool = await shared_pool(self._dsn)
        if not self._schema_ready:
            async with pool.acquire() as conn, schema_init_lock(conn):  # #2551 — replicas serialise DDL
                for stmt in _CREATE_PG:
                    await conn.execute(stmt)
            self._schema_ready = True
        return pool

    async def add_suggestion(self, suggestion: Suggestion) -> Suggestion:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO suggestions (suggestion_id, cluster_fingerprint, status, workspace_id, created_at, data) "
                "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (suggestion_id) DO UPDATE SET "
                "status=EXCLUDED.status, data=EXCLUDED.data, workspace_id=EXCLUDED.workspace_id",
                suggestion.suggestion_id, suggestion.cluster_fingerprint,
                suggestion.status, suggestion.workspace_id or DEFAULT_WORKSPACE,
                suggestion.created_at, suggestion.model_dump_json())
        return suggestion

    async def list_suggestions(self, status: Optional[str] = None, *, workspace_id=None) -> list[Suggestion]:
        clauses, args = [], []
        if status is not None:
            args.append(status); clauses.append(f"status=${len(args)}")
        if workspace_id is not None:
            args.append(workspace_id); clauses.append(f"workspace_id=${len(args)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT data FROM suggestions{where} ORDER BY created_at DESC", *args)
        return [Suggestion.model_validate_json(r["data"]) for r in rows]

    async def get_suggestion(self, suggestion_id: str, *, workspace_id=None) -> Optional[Suggestion]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM suggestions WHERE suggestion_id=$1", suggestion_id)
        if row is None:
            return None
        got = Suggestion.model_validate_json(row["data"])
        return got if _in_workspace(got, workspace_id) else None

    async def update_status(
        self, suggestion_id: str, status: str, reason: str = "", *, workspace_id=None,
    ) -> Optional[Suggestion]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT data FROM suggestions WHERE suggestion_id=$1 FOR UPDATE", suggestion_id)
                if row is None:
                    return None
                current = Suggestion.model_validate_json(row["data"])
                # Deciding is a WRITE. Another workspace's suggestion reads as
                # absent so the route 404s rather than accepting it.
                if not _in_workspace(current, workspace_id):
                    return None
                updated = current.model_copy(
                    update={"status": status, "reason": reason, "decided_at": utc_now_iso()})
                await conn.execute(
                    "UPDATE suggestions SET status=$2, data=$3 WHERE suggestion_id=$1",
                    suggestion_id, status, updated.model_dump_json())
        return updated

    async def open_fingerprints(self, *, workspace_id=None) -> set[str]:
        # Scoped: this is the dedupe key. Unscoped, one workspace's rejected
        # suggestion would suppress the same finding everywhere else.
        sql = ("SELECT DISTINCT cluster_fingerprint FROM suggestions "
               "WHERE status = ANY($1::text[]) AND cluster_fingerprint <> ''")
        args: list = [list(_SPOKEN_FOR)]
        if workspace_id is not None:
            args.append(workspace_id)
            sql += f" AND workspace_id=${len(args)}"
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return {r["cluster_fingerprint"] for r in rows}


# ─── Selector + process singleton ──────────────────────────────────────

_STORE: Optional[SuggestionStore] = None


def default_suggestion_store() -> SuggestionStore:
    """Pick a store from the environment — mirrors ``default_eval_ledger``:
    ``CAIRN_STORAGE=postgres://…`` → :class:`PostgresSuggestionStore` (the one
    prod database); otherwise ``CAIRN_EVAL_LEDGER_DB`` set → SQLite at that path
    (``:memory:`` ephemeral), else the shared ``cairn.db``."""
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresSuggestionStore(dsn=url)
    path = getenv("CAIRN_EVAL_LEDGER_DB", "").strip()
    return SqliteSuggestionStore(path=path) if path else SqliteSuggestionStore()


def get_suggestion_store() -> SuggestionStore:
    global _STORE
    if _STORE is None:
        _STORE = default_suggestion_store()
    return _STORE


def reset_suggestion_store() -> None:
    """Drop the cached store (tests, or after an env change)."""
    global _STORE
    _STORE = None


__all__ = [
    "InMemorySuggestionStore",
    "PostgresSuggestionStore",
    "SqliteSuggestionStore",
    "Suggestion",
    "SuggestionStore",
    "default_suggestion_store",
    "get_suggestion_store",
    "reset_suggestion_store",
]
