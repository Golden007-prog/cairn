"""SQLite-backed ``RunStore`` implementation.

Embedded single-file store: restart-safe, queryable, no service. The full
``Run`` is serialized to JSON in a ``data`` column; ``id`` / ``fingerprint``
/ ``lane`` / ``status`` / ``created_at`` are mirrored into indexed columns
so listing and dedup stay cheap. This keeps schema evolution trivial — new
model fields ride along in the JSON blob, and only the handful of columns
we actually filter on need to exist.

Concurrency: an ``asyncio.Lock`` serializes access and the blocking sqlite3
work runs on a worker thread via ``asyncio.to_thread`` so it never stalls the
event loop (#86). The lock guarantees exactly one thread touches the
connection at a time (it's ``check_same_thread=False``). WAL mode is enabled
so a crash mid-write can't corrupt the file. Pass ``path=":memory:"`` for an
ephemeral store. Construction (``__init__``) stays synchronous — it's one-time
startup, not the hot path.
"""

from __future__ import annotations

import asyncio
import json as _json
import sqlite3
from typing import Optional, Sequence

from cairn.core.storage.hydration import hydrate_all, record_skipped, validated_update
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


#: #2128 item C — "this run belongs to no pipeline", as SQLite sees it.
#: ``bundle_pipeline_id`` rides the JSON ``data`` blob (nothing filters or
#: indexes on it), so the predicate is a ``json_extract``, and a row written
#: before the field existed reads NULL — hence the COALESCE. This is also the
#: shape of the invariant's own count
#: (:data:`cairn.core.storage.backfill_pipelines.ORPHAN_COUNT_SQL_SQLITE`).
_PIPELINE_LESS_PREDICATE = "COALESCE(json_extract(data, '$.bundle_pipeline_id'), '') = ''"


def _hydrate(row) -> Run:
    """Rebuild a Run, treating the ``status`` COLUMN (row[1]) as authoritative
    over the ``data`` blob (row[0]). #977 — a terminal transition that updates
    the indexed column without round-tripping the serialized Run (the #976
    zombie path) leaves the blob's status stale, so a cancelled/failed/done run
    reads back as running and shows "running" in the console forever. The column
    is the transactionally-filtered field and only moves forward, so it wins.
    Callers that SELECT only ``data`` (row has one element) get the blob as-is.
    """
    run = Run.model_validate_json(row[0])
    col = row[1] if len(row) > 1 else None
    if col and col != run.status:
        run.status = col
    return run


class SqliteRunStore(RunStore):
    """Embedded-SQLite store — restart-safe, queryable, single file, no service.

    The full ``Run`` is serialized to JSON in a ``data`` column;
    ``id``/``fingerprint``/``lane``/``status``/``created_at`` are mirrored into
    indexed columns so listing and dedup stay cheap. This keeps schema evolution
    trivial: new model fields just ride along in the JSON blob, and only the
    handful of columns we actually filter on need to exist.

    Concurrency: an ``asyncio.Lock`` serializes access and the blocking sqlite3
    work runs on a worker thread via ``asyncio.to_thread`` so it never stalls
    the event loop (#86). The lock guarantees one thread touches the connection
    at a time (it's ``check_same_thread=False``). WAL mode is enabled so a crash
    mid-write can't corrupt the file. Pass ``path=":memory:"`` for an ephemeral
    store.
    """

    def __init__(self, path: str | None = None) -> None:
        import os
        import sqlite3

        if path is None:
            from cairn.core import settings as _settings
            path = _settings.run_db_path() or os.path.expanduser(
                "~/.cairn/runs.db"
            )
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.path = path
        # check_same_thread=False: a single asyncio loop may hop threads (e.g.
        # under uvicorn/TestClient worker pools); the lock guarantees we never
        # touch the connection concurrently regardless.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id                TEXT PRIMARY KEY,
                fingerprint       TEXT,
                lane              TEXT,
                status            TEXT,
                workspace_id      TEXT NOT NULL DEFAULT 'default',
                created_at        TEXT,
                failure_type      TEXT,
                cost_baseline_usd NUMERIC,
                final_verdict     TEXT,
                data              TEXT NOT NULL,
                compute_target    TEXT
            )
            """
        )
        # workspace_id is a real INDEXED column (ADR-0051), not a json_extract on
        # the blob: migrate pre-existing DBs by adding the column + back-filling
        # from the JSON `data` once (ADR-0050 primitive).
        from cairn.core.storage import migrations
        if "workspace_id" in migrations.add_missing_columns(
            self._conn, "runs", {"workspace_id": "TEXT NOT NULL DEFAULT 'default'"}
        ):
            self._conn.execute(
                "UPDATE runs SET workspace_id=COALESCE(json_extract(data,'$.workspace_id'),'default')"
            )
        # Eval ledger delta (ADR-0059 §D3, #244): migrate pre-existing DBs by
        # adding the three columns + back-filling from the JSON `data` once.
        added = migrations.add_missing_columns(
            self._conn,
            "runs",
            {
                "failure_type": "TEXT",
                "cost_baseline_usd": "NUMERIC",
                "final_verdict": "TEXT",
            },
        )
        if added:
            self._conn.execute(
                "UPDATE runs SET "
                "failure_type=COALESCE(failure_type, json_extract(data,'$.failure_type')), "
                "cost_baseline_usd=COALESCE(cost_baseline_usd, json_extract(data,'$.cost_baseline_usd')), "
                "final_verdict=COALESCE(final_verdict, json_extract(data,'$.final_verdict'))"
            )
        # ADR-0091 tier-2 (#1531): compute_target is a real column too, for the
        # SAME reason as workspace_id — it is FILTERED on (the per-target
        # concurrency count), and counting it in SQL is what makes the H5 cap
        # exact instead of "exact up to the first 500 hydrated runs". NULL, not
        # "", for a run with no target, so the index only carries targeted runs.
        if "compute_target" in migrations.add_missing_columns(
            self._conn, "runs", {"compute_target": "TEXT"}
        ):
            self._conn.execute(
                "UPDATE runs SET compute_target="
                "NULLIF(json_extract(data,'$.compute_target'),'')"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_fp_status ON runs(fingerprint, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_created ON runs(created_at)"
        )
        # workspace-scoped listing predicate (ADR-0051): WHERE workspace_id=? ORDER BY created_at.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_ws_created ON runs(workspace_id, created_at)"
        )
        # the H5 count predicate (#1531): WHERE compute_target=? AND status IN (…).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_target_status "
            "ON runs(compute_target, status) WHERE compute_target IS NOT NULL"
        )
        # Append-only event log. One row per event; (run_id, seq) is the PK so
        # seq stays monotonic per run and restart-safe.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL,
                seq    INTEGER NOT NULL,
                ts     TEXT NOT NULL,
                type   TEXT NOT NULL,
                data   TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
            """
        )
        self._conn.commit()
        self._lock = asyncio.Lock()
        self._hub = _EventHub()

    def _row_cols(self, run: Run) -> tuple:
        return (
            run.id,
            run.fingerprint,
            run.lane,
            run.status,
            run.workspace_id,
            run.created_at,
            _json.dumps(run.failure_type) if run.failure_type else None,
            run.cost_baseline_usd,
            run.final_verdict or None,
            run.model_dump_json(),
            # appended LAST so every existing binding keeps its position (#1531)
            run.compute_target or None,
        )

    # ── sync DB bodies ──────────────────────────────────────────────────
    # Each runs under the lock via ``asyncio.to_thread`` (#86) so the blocking
    # sqlite3 work executes on a worker thread, off the event loop. Pydantic
    # (de)serialization rides along in the same thread — it touches the row the
    # connection just produced and keeps each op a single atomic hop.

    def _create_sync(self, run: Run) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(id, fingerprint, lane, status, workspace_id, created_at, "
            "failure_type, cost_baseline_usd, final_verdict, data, compute_target) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            self._row_cols(run),
        )
        self._conn.commit()

    async def create(self, run: Run) -> None:
        async with self._lock:
            await asyncio.to_thread(self._create_sync, run)

    def _update_sync(self, run_id: str, fields: dict) -> Run:
        row = self._conn.execute(
            "SELECT data, status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        current = _hydrate(row)
        if not guard_run_update(current.status, fields, run_id=run_id):
            return current  # illegal transition — whole update refused (#1245)
        # #1601 — `model_copy(update=)` does NOT validate. That is how a dict
        # was written into `results` (typed `list[RunResult]`): it serialized
        # fine and only raised on the way back out, in another process, with
        # nothing left to say who wrote it. Validate the CHANGED fields so a bad
        # write fails at the callsite that caused it.
        updated = validated_update(current, fields)
        self._conn.execute(
            "UPDATE runs SET fingerprint=?, lane=?, status=?, "
            "workspace_id=?, created_at=?, failure_type=?, cost_baseline_usd=?, "
            "final_verdict=?, data=?, compute_target=? WHERE id=?",
            (
                updated.fingerprint, updated.lane, updated.status,
                updated.workspace_id, updated.created_at,
                _json.dumps(updated.failure_type) if updated.failure_type else None,
                updated.cost_baseline_usd,
                updated.final_verdict or None,
                updated.model_dump_json(),
                updated.compute_target or None, run_id,
            ),
        )
        self._conn.commit()
        return updated

    async def update(self, run_id: str, **fields) -> Run:
        async with self._lock:
            return await asyncio.to_thread(self._update_sync, run_id, fields)

    def _get_sync(self, run_id: str, workspace_id: Optional[str] = None) -> Optional[Run]:
        # query-enforced scoping (ADR-0051 §4): when a workspace is given, a run
        # from another workspace is indistinguishable from not-found.
        if workspace_id is not None:
            row = self._conn.execute(
                "SELECT data, status FROM runs WHERE id=? AND workspace_id=?", (run_id, workspace_id)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT data, status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return _hydrate(row) if row else None

    async def get(self, run_id: str, workspace_id: Optional[str] = None) -> Optional[Run]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, run_id, workspace_id)

    def _list_sync(self, where: str, params: list):
        # #1601 — `id` only so a skipped row can be NAMED in the log; the
        # listing itself does not use it.
        rows = self._conn.execute(
            f"SELECT data, status, id FROM runs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        # One corrupt row used to take down the whole response: `[_hydrate(r)
        # for r in rows]` raised on the first unparseable blob and 500'd the
        # listing, so 38 healthy runs were unreachable because of 1 bad one.
        return hydrate_all(rows, _hydrate, source="runs(sqlite)",
                           row_id=lambda r: str(r[2]))

    async def list(
        self,
        *,
        lane: Optional[Lane] = None,
        status: Optional[Status] = None,
        workspace_id: Optional[str] = None,
        limit: int = 100,
        include_payloads: bool = True,
    ) -> list[Run]:
        """List runs. ``include_payloads`` is accepted for Protocol parity with
        the Postgres tier (#1508) and ignored: sqlite stores the run as one TEXT
        blob with no server-side key removal, and the dev tier's data volumes
        do not make the parse worth a second code path. Sqlite therefore always
        returns payloads — which is the SAFE direction (never silently empty).
        """
        clauses, params = [], []
        if lane is not None:
            clauses.append("lane=?")
            params.append(lane)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if workspace_id is not None:
            # workspace_id is a real INDEXED column (ADR-0051) — index-served filter.
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with self._lock:
            hydrated = await asyncio.to_thread(self._list_sync, where, params)
        # Publish the skip count HERE, in the request's context — `to_thread`
        # gives the worker a copy, so a ContextVar set inside it is discarded.
        record_skipped(hydrated.skipped)
        return hydrated.items

    def _count_on_target_sync(self, target: str, statuses: tuple[str, ...],
                              exclude_run_id: str) -> int:
        placeholders = ",".join("?" for _ in statuses)
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM runs WHERE compute_target=? "
            f"AND status IN ({placeholders}) AND id<>?",
            (target, *statuses, exclude_run_id),
        ).fetchone()
        return int(row[0] or 0)

    async def count_in_flight_on_target(
        self,
        target: str,
        statuses: Sequence[str],
        *,
        exclude_run_id: str = "",
    ) -> int:
        """One indexed ``COUNT(*)`` over the lifted ``compute_target`` column (#1531).

        No row cap and no hydration: the base-class fallback listed up to 500
        runs per status and rebuilt each one through Pydantic just to read two
        fields off it, which was both the slow path and — past 500 — the WRONG
        one. Sqlite mirrors the column for the same reason it mirrors
        ``workspace_id``: this is a filtered field, and ``json_extract`` on the
        blob cannot use an index.
        """
        name = str(target or "").strip()
        if not name:
            return 0
        stats = tuple(statuses)
        if not stats:
            return 0
        async with self._lock:
            return await asyncio.to_thread(
                self._count_on_target_sync, name, stats, exclude_run_id
            )

    def _find_active_sync(self, fp: str) -> Optional[Run]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self._conn.execute(
            f"SELECT data, status FROM runs WHERE fingerprint=? "
            f"AND status IN ({placeholders}) ORDER BY created_at ASC LIMIT 1",
            (fp, *sorted(ACTIVE_STATUSES)),
        ).fetchone()
        return _hydrate(row) if row else None

    async def find_active_by_fingerprint(self, fp: str) -> Optional[Run]:
        async with self._lock:
            return await asyncio.to_thread(self._find_active_sync, fp)

    def _list_pipeline_less_sync(
        self, after_id: str, limit: int, workspace_id: Optional[str]
    ):
        clauses = [_PIPELINE_LESS_PREDICATE, "id > ?"]
        params: list = [after_id]
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        params.append(limit)
        rows = self._conn.execute(
            "SELECT data, status, id FROM runs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()
        # #1601 — one unreadable blob must not stop the sweep; it is reported as
        # a skipped row, and the backfill's next pass sees it again.
        return hydrate_all(rows, _hydrate, source="runs(sqlite)",
                           row_id=lambda r: str(r[2]))

    async def list_pipeline_less_runs(
        self,
        *,
        after_id: str = "",
        limit: int = 200,
        workspace_id: Optional[str] = None,
    ) -> list[Run]:
        """Keyset page of pipeline-less runs (#2128 C) — see the Protocol."""
        async with self._lock:
            hydrated = await asyncio.to_thread(
                self._list_pipeline_less_sync, after_id, limit, workspace_id
            )
        # published in the CALLER's context, as `list` does — `to_thread` hands
        # the worker a copy, so a ContextVar set inside it is discarded.
        record_skipped(hydrated.skipped)
        return hydrated.items

    def _append_sync(self, run_id: str, type: str, data: dict) -> RunEvent:
        # MAX(seq)+1 then INSERT is two statements; another writer (a second
        # process on the same file, or a caller that slipped past the loop
        # lock) can take the same seq in between. The primary key catches it —
        # re-read and retry rather than drop the event (#2025 follow-up; the
        # Postgres store allocates in one statement, #1509).
        for attempt in range(8):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            seq = int(row[0] or 0) + 1
            ev = RunEvent(run_id=run_id, seq=seq, type=type, data=dict(data))
            try:
                self._conn.execute(
                    "INSERT INTO run_events (run_id, seq, ts, type, data) "
                    "VALUES (?,?,?,?,?)",
                    (run_id, seq, ev.ts, ev.type, _json.dumps(ev.data)),
                )
                self._conn.commit()
                return ev
            except sqlite3.IntegrityError:
                self._conn.rollback()
                if attempt == 7:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    async def append_event(
        self, run_id: str, type: str, data: Optional[dict] = None
    ) -> RunEvent:
        async with self._lock:
            ev = await asyncio.to_thread(
                self._append_sync, run_id, type, dict(data or {})
            )
        await self._hub.publish(ev)
        return ev

    def _get_events_sync(self, run_id: str, after_seq: int) -> list[RunEvent]:
        rows = self._conn.execute(
            "SELECT run_id, seq, ts, type, data FROM run_events "
            "WHERE run_id=? AND seq>? ORDER BY seq ASC",
            (run_id, after_seq),
        ).fetchall()
        return [
            RunEvent(
                run_id=r[0], seq=r[1], ts=r[2], type=r[3], data=_json.loads(r[4])
            )
            for r in rows
        ]

    async def get_events(
        self, run_id: str, *, after_seq: int = 0
    ) -> list[RunEvent]:
        async with self._lock:
            return await asyncio.to_thread(self._get_events_sync, run_id, after_seq)

    def close(self) -> None:
        self._conn.close()
