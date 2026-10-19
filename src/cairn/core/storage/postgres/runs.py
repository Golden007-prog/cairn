"""Postgres-backed ``RunStore`` implementation.

Mirrors :class:`~cairn.core.storage.sqlite.runs.SqliteRunStore`'s shape
so consumers see no behavioral difference — same Protocol, same coupled
runs+events store, same subscribe-with-backlog semantics.

What differs from the SQLite version:

- ``data`` columns are ``JSONB`` (indexable, queryable) instead of ``TEXT``.
- ``created_at`` stays ``TEXT`` (ISO-8601) to keep the marshalling identical
  with the Pydantic ``Run`` model and SQLite path. Lexicographic ISO sort
  gives the same ordering as a timestamp sort.
- Concurrency uses asyncpg's pool + transaction context (multiple
  connections, vs the single SQLite connection guarded by ``asyncio.Lock``).
- Live event fan-out (``self._hub``) is still in-process only. Cross-process
  live delivery requires PostgresBus (§24.10 Tier 2) — a separate
  follow-up.

Lives at ``CAIRN_STORAGE=postgres://user:pass@host:5432/db``; pass the DSN
explicitly via :class:`PostgresRunStore(dsn=…)` for tests.

Requires the ``storage-postgres`` optional dep group.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

from cairn.core.storage.hydration import hydrate_all, record_skipped, validated_update
from cairn.core.storage.transitions import guard_run_update
from cairn.core.pg_schema_init import PgSchemaInit
from cairn.core.storage.protocols import (
    ACTIVE_STATUSES,
    Lane,
    Run,
    RunEvent,
    RunStore,
    Status,
    _EventHub,
)

logger = logging.getLogger(__name__)


def _require_asyncpg():
    """Lazy import — keep the dep optional. Clean error if not installed."""
    try:
        import asyncpg  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Postgres run store requires the 'storage-postgres' optional dep "
            "group. Install with: pip install 'cairndev[storage-postgres]'"
        ) from e


# ─── DDL ────────────────────────────────────────────────────────────────


#: #2128 item C — "this run belongs to no pipeline", as Postgres sees it. The
#: SQLite twin is ``sqlite/runs.py::_PIPELINE_LESS_PREDICATE``; both read the
#: JSON ``data`` blob because ``bundle_pipeline_id`` was never worth a column
#: (nothing filters on it outside this one-shot sweep), and both COALESCE
#: because a row written before the field existed has no such key.
_PIPELINE_LESS_PREDICATE = "COALESCE(data->>'bundle_pipeline_id', '') = ''"

_CREATE_RUNS_SQL = """
    CREATE TABLE IF NOT EXISTS runs (
        id                TEXT PRIMARY KEY,
        fingerprint       TEXT,
        lane              TEXT,
        status            TEXT,
        workspace_id      TEXT NOT NULL DEFAULT 'default',
        created_at        TEXT,
        failure_type      JSONB,
        cost_baseline_usd NUMERIC,
        final_verdict     TEXT,
        data              JSONB NOT NULL,
        compute_target    TEXT
    )
"""

_CREATE_RUNS_FP_STATUS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_fp_status ON runs(fingerprint, status)"
)

_CREATE_RUNS_CREATED_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC)"
)

# workspace_id is a real INDEXED column (ADR-0051). For a pre-existing table the
# ADD COLUMN runs in place; back-fill from the JSONB blob once.
_ADD_RUNS_WS_COL = (
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default'"
)
_BACKFILL_RUNS_WS = (
    "UPDATE runs SET workspace_id = COALESCE(data->>'workspace_id','default') "
    "WHERE workspace_id = 'default' AND data->>'workspace_id' IS NOT NULL"
)
_CREATE_RUNS_WS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_ws_created ON runs(workspace_id, created_at DESC)"
)

# Eval ledger delta (ADR-0059 §D3, #244): ADD COLUMN IF NOT EXISTS for pre-existing
# tables; back-fill from the JSONB blob once per column (idempotent — only touches
# rows where the mirrored column is still null but the blob has a value).
_ADD_RUNS_FAILURE_TYPE_COL = (
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS failure_type JSONB"
)
_ADD_RUNS_COST_BASELINE_COL = (
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS cost_baseline_usd NUMERIC"
)
_ADD_RUNS_FINAL_VERDICT_COL = (
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS final_verdict TEXT"
)
_BACKFILL_RUNS_EVAL_LEDGER_COLS = """
    UPDATE runs SET
        failure_type = COALESCE(failure_type, data->'failure_type'),
        cost_baseline_usd = COALESCE(cost_baseline_usd, (data->>'cost_baseline_usd')::numeric),
        final_verdict = COALESCE(final_verdict, data->>'final_verdict')
    WHERE failure_type IS NULL OR cost_baseline_usd IS NULL OR final_verdict IS NULL
"""

# ADR-0091 tier-2 (#1531): compute_target is a real INDEXED column, lifted for
# exactly the same reason as workspace_id above — the per-target concurrency cap
# (ADR-0086 H5) COUNTS on it once per admission decision, and a count that has to
# hydrate rows is a count with a row cap, i.e. an under-count, i.e. an
# over-admit. Nullable (most runs have no target); back-filled from the JSONB
# blob once, with '' normalized to NULL so the partial index only carries runs
# that actually hold a target.
_ADD_RUNS_TARGET_COL = (
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS compute_target TEXT"
)
_BACKFILL_RUNS_TARGET = (
    "UPDATE runs SET compute_target = NULLIF(data->>'compute_target','') "
    "WHERE compute_target IS NULL AND NULLIF(data->>'compute_target','') IS NOT NULL"
)
_CREATE_RUNS_TARGET_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_target_status ON runs(compute_target, status) "
    "WHERE compute_target IS NOT NULL"
)

_CREATE_EVENTS_SQL = """
    CREATE TABLE IF NOT EXISTS run_events (
        run_id TEXT NOT NULL,
        seq    INTEGER NOT NULL,
        ts     TEXT NOT NULL,
        type   TEXT NOT NULL,
        data   JSONB NOT NULL,
        PRIMARY KEY (run_id, seq)
    )
"""


# ─── DML ────────────────────────────────────────────────────────────────


#: ``compute_target`` is bound LAST ($11, after ``data``) on purpose: appending
#: the new column leaves every pre-existing parameter position untouched (#1531).
_INSERT_RUN_SQL = """
    INSERT INTO runs (id, fingerprint, lane, status, workspace_id, created_at,
                       failure_type, cost_baseline_usd, final_verdict, data,
                       compute_target)
    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10::jsonb, $11)
    ON CONFLICT (id) DO UPDATE
    SET fingerprint       = EXCLUDED.fingerprint,
        lane              = EXCLUDED.lane,
        status            = EXCLUDED.status,
        workspace_id      = EXCLUDED.workspace_id,
        created_at        = EXCLUDED.created_at,
        failure_type      = EXCLUDED.failure_type,
        cost_baseline_usd = EXCLUDED.cost_baseline_usd,
        final_verdict     = EXCLUDED.final_verdict,
        data              = EXCLUDED.data,
        compute_target    = EXCLUDED.compute_target
"""

_SELECT_RUN_DATA_SQL = "SELECT data::text, status FROM runs WHERE id = $1"
_SELECT_RUN_DATA_WS_SQL = "SELECT data::text, status FROM runs WHERE id = $1 AND workspace_id = $2"


def _hydrate(row) -> Run:
    """Rebuild a Run from its row, treating the ``status`` COLUMN as authoritative.

    #977 — the Run is serialized whole into the ``data`` JSONB blob, but the
    lifecycle status is ALSO kept in the indexed ``status`` column (the one
    ``list(status=…)`` / ``find_active_by_fingerprint`` filter on). A terminal
    transition that updates the column without round-tripping the Run object
    (e.g. the #976 zombie path) leaves the blob's ``status`` stale — so a
    cancelled/failed/done run reads back as ``running``/``queued``/
    ``awaiting_approval`` and shows "running" in the console forever. The column
    is the field that's transactionally filtered and can only move forward, so
    it wins: override the hydrated status with it whenever present.
    """
    run = Run.model_validate_json(row["data"])
    col = row.get("status") if hasattr(row, "get") else row["status"]
    if col and col != run.status:
        run.status = col
    return run

_UPDATE_RUN_SQL = """
    UPDATE runs
    SET fingerprint       = $2,
        lane              = $3,
        status            = $4,
        workspace_id      = $5,
        created_at        = $6,
        failure_type      = $7::jsonb,
        cost_baseline_usd = $8,
        final_verdict     = $9,
        data              = $10::jsonb,
        compute_target    = $11
    WHERE id = $1
"""

#: The whole point of #1531: ONE indexed aggregate, no row cap, no hydration.
#: ``id <> $3`` is the ``exclude_run_id`` predicate — a leg waiting for a slot
#: must not be blocked by its own ``queued``/``running`` row.
_COUNT_ON_TARGET_SQL = """
    SELECT COUNT(*) FROM runs
    WHERE compute_target = $1 AND status = ANY($2::text[]) AND id <> $3
"""

#: Allocate the sequence and insert in ONE statement (#1509).
#:
#: The old shape was `SELECT MAX(seq)` then a separate INSERT. Under
#: READ COMMITTED that is not serialized — no `FOR UPDATE`, and there is no row
#: to lock for a seq that does not exist yet — so the API server and the
#: Temporal worker could read the same MAX and compute the same seq. The second
#: INSERT then violated the `(run_id, seq)` primary key, and BOTH call sites
#: caught the error as "best-effort", so the event was lost rather than retried.
#:
#: On its own this still races — two concurrent statements can pick the same seq
#: — and the loser is *told* (no row returned) instead of raising. #2544 showed
#: that a retry loop alone does not converge: with 40 writers on one run, one
#: writer can lose 8 rounds in a row. So the insert now runs in a transaction
#: that first takes ``_LOCK_RUN_EVENTS_SQL`` (below); each statement under READ
#: COMMITTED takes a fresh snapshot, so the MAX read after the lock sees every
#: earlier writer's committed row and the insert cannot conflict. The
#: ``ON CONFLICT`` + retry stay as the fallback for a writer that does not take
#: the lock (an older replica mid rolling upgrade).
_INSERT_EVENT_NEXT_SEQ_SQL = """
    INSERT INTO run_events (run_id, seq, ts, type, data)
    SELECT $1,
           COALESCE((SELECT MAX(seq) FROM run_events WHERE run_id = $1), 0) + 1,
           $2, $3, $4::jsonb
    ON CONFLICT (run_id, seq) DO NOTHING
    RETURNING seq
"""

#: Serialise one run's appenders for the length of the insert transaction —
#: the ``budget_reservations._LOCK_SERIES_SQL`` pattern (an xact lock is dropped
#: on COMMIT or ROLLBACK, so nothing can strand it). Two-``int4`` form: Postgres
#: keeps it in a key space apart from the one-``bigint`` locks of
#: ``dispatch/coordination.py`` and ``leadership.py``, so a ``hashtext`` collision
#: with a session-held pipeline section can never park an append. A collision
#: between two run ids costs only spurious serialisation.
_LOCK_RUN_EVENTS_SQL = "SELECT pg_advisory_xact_lock(hashtext('cairn:run_events'), hashtext($1))"

#: How many times to re-attempt a lost sequence race before giving up. Under
#: ``_LOCK_RUN_EVENTS_SQL`` the first attempt wins; a lost round now means a
#: writer that bypasses the lock.
_EVENT_SEQ_MAX_ATTEMPTS = 8

_SELECT_EVENTS_SQL = """
    SELECT run_id, seq, ts, type, data::text AS data
    FROM run_events
    WHERE run_id = $1 AND seq > $2
    ORDER BY seq ASC
"""


# ─── Store ──────────────────────────────────────────────────────────────


class PostgresRunStore(RunStore, PgSchemaInit):
    """Postgres-backed runs + events. Mirrors :class:`SqliteRunStore`.

    The DSN can be passed explicitly (tests) or read from ``CAIRN_STORAGE`` env
    by :func:`cairn.core.storage.build_storage`. asyncpg pool is created
    lazily on first call — same lifecycle as :class:`PGVectorStore`.
    """

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn: Optional[str] = dsn
        self._pool = None
        self._hub = _EventHub()
        self._schema_ready = False

    # ── Lazy pool + schema ─────────────────────────────────────────────

    async def _open_pool(self):
        _require_asyncpg()
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        dsn = self._dsn
        if not dsn:
            # Tolerate `CAIRN_STORAGE` not being set when the caller used
            # default_store() but expected Postgres — refuse loudly.
            raise RuntimeError(
                "PostgresRunStore needs a DSN. Pass dsn= explicitly or set "
                "CAIRN_STORAGE=postgres://… before constructing the store."
            )
        # #614: one pool per DSN, shared across all Postgres stores in this process.
        return await shared_pool(dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as conn:
            await conn.execute(_CREATE_RUNS_SQL)
            await conn.execute(_ADD_RUNS_WS_COL)        # migrate pre-ADR-0051 tables
            await conn.execute(_BACKFILL_RUNS_WS)
            await conn.execute(_CREATE_RUNS_FP_STATUS_IDX)
            await conn.execute(_CREATE_RUNS_CREATED_IDX)
            await conn.execute(_CREATE_RUNS_WS_IDX)
            # eval ledger delta (ADR-0059 §D3, #244): migrate pre-existing tables
            await conn.execute(_ADD_RUNS_FAILURE_TYPE_COL)
            await conn.execute(_ADD_RUNS_COST_BASELINE_COL)
            await conn.execute(_ADD_RUNS_FINAL_VERDICT_COL)
            await conn.execute(_BACKFILL_RUNS_EVAL_LEDGER_COLS)
            # per-target concurrency delta (ADR-0091 tier-2, #1531)
            await conn.execute(_ADD_RUNS_TARGET_COL)
            await conn.execute(_BACKFILL_RUNS_TARGET)
            await conn.execute(_CREATE_RUNS_TARGET_IDX)
            await conn.execute(_CREATE_EVENTS_SQL)
        logger.info("PostgresRunStore schema ready")

    # ── Run CRUD ───────────────────────────────────────────────────────

    async def create(self, run: Run) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_RUN_SQL,
                run.id, run.fingerprint, run.lane, run.status,
                run.workspace_id, run.created_at,
                json.dumps(run.failure_type) if run.failure_type else None,
                run.cost_baseline_usd,
                run.final_verdict or None,
                run.model_dump_json(),
                run.compute_target or None,   # NULL, never '' — see the index
            )

    async def update(self, run_id: str, **fields) -> Run:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(_SELECT_RUN_DATA_SQL, run_id)
                if row is None:
                    raise KeyError(run_id)
                current = _hydrate(row)
                if not guard_run_update(current.status, fields, run_id=run_id):
                    return current  # illegal transition — whole update refused (#1245)
                # #1601 — `model_copy(update=)` does NOT validate; that is how a
                # dict reached `results` (typed `list[RunResult]`) and only blew
                # up on read. Fail at the callsite that writes the wrong shape.
                updated = validated_update(current, fields)
                await conn.execute(
                    _UPDATE_RUN_SQL,
                    run_id, updated.fingerprint, updated.lane, updated.status,
                    updated.workspace_id, updated.created_at,
                    json.dumps(updated.failure_type) if updated.failure_type else None,
                    updated.cost_baseline_usd,
                    updated.final_verdict or None,
                    updated.model_dump_json(),
                    updated.compute_target or None,
                )
                return updated

    async def get(self, run_id: str, workspace_id: Optional[str] = None) -> Optional[Run]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if workspace_id is not None:  # query-enforced scoping (ADR-0051 §4)
                row = await conn.fetchrow(_SELECT_RUN_DATA_WS_SQL, run_id, workspace_id)
            else:
                row = await conn.fetchrow(_SELECT_RUN_DATA_SQL, run_id)
        return _hydrate(row) if row else None

    async def list(
        self,
        *,
        lane: Optional[Lane] = None,
        status: Optional[Status] = None,
        workspace_id: Optional[str] = None,
        limit: int = 100,
        include_payloads: bool = False,
    ) -> list[Run]:
        """List runs, WITHOUT their result payloads by default (#1508).

        ``result_payloads`` is persisted uncapped, and list views strip it at
        serialization anyway — so hydrating it meant parsing potentially
        multi-MB blobs to render list rows. ``/runs?limit=100`` (the console's
        poll) and the insights endpoints at ``limit=500`` all paid that, and
        the cost grew with accumulated history rather than with load.

        The projection happens in SQL (``data - 'result_payloads'``), so the
        payloads never leave the database.

        ⚠ ``include_payloads=True`` is required by any caller that actually
        READS ``run.result_payloads`` off the result — the field defaults to
        ``[]``, so a projected row reports "no payloads" rather than raising.
        The Playground's what-if history (``routers/playground.py``) is the one
        such caller today and passes it explicitly.
        """
        clauses, params = [], []
        if lane is not None:
            params.append(lane)
            clauses.append(f"lane = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if workspace_id is not None:
            # workspace_id is a real INDEXED column (ADR-0051) — index-served filter.
            params.append(workspace_id)
            clauses.append(f"workspace_id = ${len(params)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        # `-` on a jsonb drops the key server-side; the payloads are never
        # serialized, shipped over the wire, or parsed by pydantic.
        data_expr = "data::text" if include_payloads else "(data - 'result_payloads')::text"
        sql = (
            f"SELECT {data_expr} AS data, status, id FROM runs{where} "
            f"ORDER BY created_at DESC LIMIT ${len(params)}"
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        # #1601 — one corrupt row must not 500 the whole listing. `id` is
        # selected only so a skipped row can be NAMED in the log.
        hydrated = hydrate_all(rows, _hydrate, source="runs(postgres)",
                               row_id=lambda r: str(r["id"]))
        record_skipped(hydrated.skipped)
        return hydrated.items

    async def list_pipeline_less_runs(
        self,
        *,
        after_id: str = "",
        limit: int = 200,
        workspace_id: Optional[str] = None,
    ) -> list[Run]:
        """Keyset page of pipeline-less runs (#2128 C) — see the Protocol.

        Same predicate as :data:`_PIPELINE_LESS_PREDICATE`, which is also the
        invariant's own count (``ORPHAN_COUNT_SQL_POSTGRES``).
        ``result_payloads`` is projected out server-side: the backfill reads a
        run's ref, status, timestamps and inputs, never its payloads, and this
        tier's rows are the multi-MB ones (#1508).
        """
        params: list = [after_id]
        clauses = [_PIPELINE_LESS_PREDICATE, "id > $1"]
        if workspace_id is not None:
            params.append(workspace_id)
            clauses.append(f"workspace_id = ${len(params)}")
        params.append(limit)
        sql = (
            "SELECT (data - 'result_payloads')::text AS data, status, id FROM runs "
            f"WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ${len(params)}"
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        hydrated = hydrate_all(rows, _hydrate, source="runs(postgres)",
                               row_id=lambda r: str(r["id"]))
        record_skipped(hydrated.skipped)
        return hydrated.items

    async def count_in_flight_on_target(
        self,
        target: str,
        statuses: Sequence[str],
        *,
        exclude_run_id: str = "",
    ) -> int:
        """The fleet-wide per-target count, as ONE indexed aggregate (#1531).

        This is the tier the acceptance criterion is about: with two control
        planes sharing one database, the cap is only a cap if both replicas ask
        the SAME database the SAME exact question. The scan this replaces read
        the 500 newest runs per status and filtered them in Python — so past 500
        concurrent runs a target's own legs could fall off the end of the list
        entirely and the count came back too low, which admits over the cap.

        Served by ``idx_runs_target_status``; ``compute_target`` is stored NULL
        (never ``''``) for an untargeted run, so those rows are not even in the
        index.
        """
        name = str(target or "").strip()
        if not name:
            return 0
        stats = list(statuses)
        if not stats:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            n = await conn.fetchval(_COUNT_ON_TARGET_SQL, name, stats, exclude_run_id)
        return int(n or 0)

    async def find_active_by_fingerprint(self, fp: str) -> Optional[Run]:
        # ACTIVE_STATUSES is a frozenset; convert to a list with deterministic
        # order so the query plan is stable.
        active = sorted(ACTIVE_STATUSES)
        sql = (
            "SELECT data::text AS data, status FROM runs "
            "WHERE fingerprint = $1 AND status = ANY($2::text[]) "
            "ORDER BY created_at ASC LIMIT 1"
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, fp, active)
        return _hydrate(row) if row else None

    # ── Event log ──────────────────────────────────────────────────────

    async def append_event(
        self, run_id: str, type: str, data: Optional[dict] = None
    ) -> RunEvent:
        pool = await self._get_pool()
        payload = dict(data or {})
        ev = RunEvent(run_id=run_id, seq=0, type=type, data=payload)
        blob = json.dumps(payload)
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(_LOCK_RUN_EVENTS_SQL, run_id)  # #2544: one appender per run
            for attempt in range(_EVENT_SEQ_MAX_ATTEMPTS):
                seq = await conn.fetchval(
                    _INSERT_EVENT_NEXT_SEQ_SQL, run_id, ev.ts, type, blob
                )
                if seq is not None:
                    ev.seq = int(seq)
                    break
                # Another writer took this seq. Retry — the whole point of
                # #1509 is that a lost race must not silently drop the event.
                logger.debug(
                    "run_events seq contention on %s (attempt %d)", run_id, attempt + 1
                )
            else:
                # Sustained contention. Raise rather than return a bogus seq:
                # the callers treat append_event as best-effort and log, so a
                # silent success with seq=0 would corrupt the timeline ordering
                # instead of merely losing one event.
                raise RuntimeError(
                    f"could not allocate a run_events sequence for {run_id} after "
                    f"{_EVENT_SEQ_MAX_ATTEMPTS} attempts — persistent writer contention"
                )
        await self._hub.publish(ev)
        return ev

    async def get_events(
        self, run_id: str, *, after_seq: int = 0
    ) -> list[RunEvent]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_EVENTS_SQL, run_id, after_seq)
        return [
            RunEvent(
                run_id=r["run_id"],
                seq=r["seq"],
                ts=r["ts"],
                type=r["type"],
                data=json.loads(r["data"]),
            )
            for r in rows
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False
