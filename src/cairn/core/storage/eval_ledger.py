"""Eval ledger — verdicts, per-case results, and failure labels (#244, ADR-0059 §D3).

The transactional system-of-record subset for the closed eval loop (ADR-0059
D2): what you **gate or audit on** lives here in Postgres/SQLite, never the
raw spans (those stay in ClickHouse — out of scope for this module).

Three tables, mirroring the shape of ``rag_catalog.py`` (full Pydantic model
serialized to a JSON ``data`` column; the handful of columns actually
filtered on are mirrored into real indexed columns):

  - ``eval_runs``         — one row per eval invocation against a run
                             (recipe, tier, verdict, aggregate score).
  - ``eval_case_results``  — one row per test case within an eval run.
  - ``failure_labels``     — the diagnosis primitive (#474): why a run failed,
                             tagged by which classifier produced the label.

Three tiers, mirroring ``RagCatalogStore`` / ``RunStore``:
  - :class:`InMemoryEvalLedger` — volatile, tests only.
  - :class:`SqliteEvalLedger` — embedded single file, restart-safe default.
  - :class:`PostgresEvalLedger` — ``CAIRN_STORAGE=postgres://…``, the one prod
    instance (#427/ADR-0058).

Nothing here computes a verdict or a failure label — that's the eval/taxonomy
modules' job (parallel work, ADR-0059 D6: cairn computes and owns the verdict,
this module only persists it).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import json as _json
import os
import sqlite3
import uuid
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import PgSchemaInit

# classifier vocabulary for a failure label — "who/what produced the label".
#   deterministic — a rule/gate (schema check, cost threshold, PII regex, …)
#   llm           — an LLM-judge classification
Classifier = str


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ─── Records ─────────────────────────────────────────────────────────


class EvalRun(BaseModel):
    """One eval invocation against a run (ADR-0059 §D3)."""

    eval_run_id: str = Field(default_factory=lambda: _new_id("eval"))
    run_id: str
    recipe: str = ""
    tier: str = ""
    verdict: str = ""              # pass | fail | degraded
    aggregate_score: Optional[float] = None
    summary: Optional[str] = None  # #499 human-readable result text (per-gate scores + reason)
    judge_meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class EvalCaseResult(BaseModel):
    """One test case's result within an :class:`EvalRun`."""

    case_result_id: str = Field(default_factory=lambda: _new_id("case"))
    eval_run_id: str
    case_id: str = ""
    slice: dict[str, Any] = Field(default_factory=dict)
    passed: bool = False
    score: Optional[float] = None
    expected_ref: str = ""
    actual_ref: str = ""


class FailureLabel(BaseModel):
    """The diagnosis primitive (#474): why a run failed, and who said so.

    ``run_id`` defaults to "" and is always overwritten by
    :meth:`EvalLedgerStore.add_failure_labels` to match its ``run_id``
    argument, so the taxonomy module can build a label without plumbing the
    run id through every call site."""

    label_id: str = Field(default_factory=lambda: _new_id("flabel"))
    run_id: str = ""
    failure_type: str = ""
    classifier: Classifier = "deterministic"   # "deterministic" | "llm"
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


@runtime_checkable
class EvalLedgerStore(Protocol):
    """Persistence contract for the eval ledger.

    Method names/signatures are the parallel-agent seam: the taxonomy module
    (#474) builds :class:`FailureLabel` objects and passes them straight to
    ``add_failure_labels``; the eval/recipe runner builds :class:`EvalRun` +
    :class:`EvalCaseResult` and passes them to ``record_eval_run``.
    """

    async def record_eval_run(
        self, eval_run: EvalRun, case_results: Optional[list[EvalCaseResult]] = None
    ) -> EvalRun:
        """Persist one eval run and its (optional) case results. Upsert on
        ``eval_run_id``; case results are appended (not deduped) since a
        re-eval of the same ``eval_run_id`` is not expected — callers mint a
        fresh id per invocation. Each case result's ``eval_run_id`` is
        overwritten to match ``eval_run.eval_run_id`` (as ``add_failure_labels``
        stamps ``run_id``), so callers may build cases without threading the id."""
        ...

    async def get_eval_runs(self, run_id: str) -> list[EvalRun]:
        """All eval runs recorded against ``run_id``, newest first."""
        ...

    async def get_case_results(self, eval_run_id: str) -> list[EvalCaseResult]:
        """All case results for one eval run."""
        ...

    async def add_failure_labels(self, run_id: str, labels: list[FailureLabel]) -> None:
        """Append failure labels for ``run_id``. Each label's own ``run_id``
        is overwritten to match the argument, so callers may build labels
        without knowing it up front."""
        ...

    async def get_failure_labels(self, run_id: str) -> list[FailureLabel]:
        """All failure labels recorded against ``run_id``, newest first."""
        ...


# ─── Tier 0: in-memory (tests / ephemeral) ────────────────────────────


class InMemoryEvalLedger:
    """Volatile ledger — tests only. Not restart-safe."""

    def __init__(self) -> None:
        self._eval_runs: dict[str, EvalRun] = {}
        self._case_results: dict[str, list[EvalCaseResult]] = {}
        self._labels: dict[str, list[FailureLabel]] = {}
        self._lock = asyncio.Lock()

    async def record_eval_run(
        self, eval_run: EvalRun, case_results: Optional[list[EvalCaseResult]] = None
    ) -> EvalRun:
        async with self._lock:
            self._eval_runs[eval_run.eval_run_id] = eval_run.model_copy(deep=True)
            if case_results:
                bucket = self._case_results.setdefault(eval_run.eval_run_id, [])
                bucket.extend(
                    c.model_copy(update={"eval_run_id": eval_run.eval_run_id}, deep=True)
                    for c in case_results
                )
        return eval_run

    async def get_eval_runs(self, run_id: str) -> list[EvalRun]:
        async with self._lock:
            rows = [e for e in self._eval_runs.values() if e.run_id == run_id]
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return [e.model_copy(deep=True) for e in rows]

    async def get_case_results(self, eval_run_id: str) -> list[EvalCaseResult]:
        async with self._lock:
            return [c.model_copy(deep=True) for c in self._case_results.get(eval_run_id, [])]

    async def add_failure_labels(self, run_id: str, labels: list[FailureLabel]) -> None:
        async with self._lock:
            bucket = self._labels.setdefault(run_id, [])
            for label in labels:
                stamped = label.model_copy(update={"run_id": run_id})
                bucket.append(stamped)

    async def get_failure_labels(self, run_id: str) -> list[FailureLabel]:
        async with self._lock:
            rows = list(self._labels.get(run_id, []))
        rows.sort(key=lambda l: l.created_at, reverse=True)
        return [l.model_copy(deep=True) for l in rows]


# ─── Tier 1: SQLite (embedded, restart-safe default) ──────────────────


class SqliteEvalLedger:
    """Embedded-SQLite eval ledger — restart-safe, single file, no service.

    Mirrors :class:`~cairn.core.storage.rag_catalog.SqliteRagCatalog`: full
    record serialized to JSON in a ``data`` column; the columns callers
    actually filter on (``run_id``, ``eval_run_id``) are mirrored into real
    indexed columns. Concurrency: one ``asyncio.Lock`` + blocking sqlite3
    work on a worker thread via ``asyncio.to_thread`` (#86).
    """

    def __init__(self, path: str | None = None) -> None:
        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_EVAL_LEDGER_DB overrides
            path = getenv("CAIRN_EVAL_LEDGER_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        self._lock = asyncio.Lock()

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_runs (
                eval_run_id TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                data        TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_runs_run_id ON eval_runs(run_id)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_case_results (
                case_result_id TEXT PRIMARY KEY,
                eval_run_id    TEXT NOT NULL,
                data           TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_case_results_eval_run_id "
            "ON eval_case_results(eval_run_id)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS failure_labels (
                label_id   TEXT PRIMARY KEY,
                run_id     TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data       TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_failure_labels_run_id ON failure_labels(run_id)"
        )
        self._conn.commit()

    # ── sync bodies (run under the lock via asyncio.to_thread, #86) ────

    def _record_eval_run_sync(
        self, eval_run: EvalRun, case_results: list[EvalCaseResult]
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO eval_runs (eval_run_id, run_id, created_at, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(eval_run_id) DO UPDATE SET
                run_id     = excluded.run_id,
                created_at = excluded.created_at,
                data       = excluded.data
            """,
            (eval_run.eval_run_id, eval_run.run_id, eval_run.created_at, eval_run.model_dump_json()),
        )
        for c in case_results:
            c = c.model_copy(update={"eval_run_id": eval_run.eval_run_id})
            self._conn.execute(
                "INSERT OR REPLACE INTO eval_case_results (case_result_id, eval_run_id, data) "
                "VALUES (?, ?, ?)",
                (c.case_result_id, c.eval_run_id, c.model_dump_json()),
            )
        self._conn.commit()

    async def record_eval_run(
        self, eval_run: EvalRun, case_results: Optional[list[EvalCaseResult]] = None
    ) -> EvalRun:
        async with self._lock:
            await asyncio.to_thread(
                self._record_eval_run_sync, eval_run, list(case_results or [])
            )
        return eval_run

    def _get_eval_runs_sync(self, run_id: str) -> list[EvalRun]:
        rows = self._conn.execute(
            "SELECT data FROM eval_runs WHERE run_id = ? ORDER BY created_at DESC",
            (run_id,),
        ).fetchall()
        return [EvalRun.model_validate_json(r[0]) for r in rows]

    async def get_eval_runs(self, run_id: str) -> list[EvalRun]:
        async with self._lock:
            return await asyncio.to_thread(self._get_eval_runs_sync, run_id)

    def _get_case_results_sync(self, eval_run_id: str) -> list[EvalCaseResult]:
        rows = self._conn.execute(
            "SELECT data FROM eval_case_results WHERE eval_run_id = ?",
            (eval_run_id,),
        ).fetchall()
        return [EvalCaseResult.model_validate_json(r[0]) for r in rows]

    async def get_case_results(self, eval_run_id: str) -> list[EvalCaseResult]:
        async with self._lock:
            return await asyncio.to_thread(self._get_case_results_sync, eval_run_id)

    def _add_failure_labels_sync(self, run_id: str, labels: list[FailureLabel]) -> None:
        for label in labels:
            stamped = label.model_copy(update={"run_id": run_id})
            self._conn.execute(
                "INSERT OR REPLACE INTO failure_labels (label_id, run_id, created_at, data) "
                "VALUES (?, ?, ?, ?)",
                (stamped.label_id, stamped.run_id, stamped.created_at, stamped.model_dump_json()),
            )
        self._conn.commit()

    async def add_failure_labels(self, run_id: str, labels: list[FailureLabel]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._add_failure_labels_sync, run_id, list(labels))

    def _get_failure_labels_sync(self, run_id: str) -> list[FailureLabel]:
        rows = self._conn.execute(
            "SELECT data FROM failure_labels WHERE run_id = ? ORDER BY created_at DESC",
            (run_id,),
        ).fetchall()
        return [FailureLabel.model_validate_json(r[0]) for r in rows]

    async def get_failure_labels(self, run_id: str) -> list[FailureLabel]:
        async with self._lock:
            return await asyncio.to_thread(self._get_failure_labels_sync, run_id)

    def close(self) -> None:
        self._conn.close()


# ─── Tier 2: Postgres (CAIRN_STORAGE=postgres://…) ──────────────────────

_CREATE_EVAL_RUNS_PG_SQL = """
    CREATE TABLE IF NOT EXISTS eval_runs (
        eval_run_id TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        data        JSONB NOT NULL
    )
"""
_CREATE_EVAL_RUNS_PG_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_eval_runs_run_id ON eval_runs(run_id)"
)
_CREATE_CASE_RESULTS_PG_SQL = """
    CREATE TABLE IF NOT EXISTS eval_case_results (
        case_result_id TEXT PRIMARY KEY,
        eval_run_id    TEXT NOT NULL,
        data           JSONB NOT NULL
    )
"""
_CREATE_CASE_RESULTS_PG_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_case_results_eval_run_id ON eval_case_results(eval_run_id)"
)
_CREATE_FAILURE_LABELS_PG_SQL = """
    CREATE TABLE IF NOT EXISTS failure_labels (
        label_id   TEXT PRIMARY KEY,
        run_id     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        data       JSONB NOT NULL
    )
"""
_CREATE_FAILURE_LABELS_PG_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_failure_labels_run_id ON failure_labels(run_id)"
)

_UPSERT_EVAL_RUN_PG_SQL = """
    INSERT INTO eval_runs (eval_run_id, run_id, created_at, data)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (eval_run_id) DO UPDATE
    SET run_id = EXCLUDED.run_id, created_at = EXCLUDED.created_at, data = EXCLUDED.data
"""
_UPSERT_CASE_RESULT_PG_SQL = """
    INSERT INTO eval_case_results (case_result_id, eval_run_id, data)
    VALUES ($1, $2, $3::jsonb)
    ON CONFLICT (case_result_id) DO UPDATE
    SET eval_run_id = EXCLUDED.eval_run_id, data = EXCLUDED.data
"""
_UPSERT_FAILURE_LABEL_PG_SQL = """
    INSERT INTO failure_labels (label_id, run_id, created_at, data)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (label_id) DO UPDATE
    SET run_id = EXCLUDED.run_id, created_at = EXCLUDED.created_at, data = EXCLUDED.data
"""
_SELECT_EVAL_RUNS_PG_SQL = (
    "SELECT data::text FROM eval_runs WHERE run_id = $1 ORDER BY created_at DESC"
)
_SELECT_CASE_RESULTS_PG_SQL = (
    "SELECT data::text FROM eval_case_results WHERE eval_run_id = $1"
)
_SELECT_FAILURE_LABELS_PG_SQL = (
    "SELECT data::text FROM failure_labels WHERE run_id = $1 ORDER BY created_at DESC"
)


class PostgresEvalLedger(PgSchemaInit):
    """Postgres-backed eval ledger — mirrors :class:`SqliteEvalLedger`
    (full record in a JSONB ``data`` column, filtered columns mirrored out)
    and :class:`~cairn.core.storage.rag_catalog.PostgresRagCatalog` (lazy
    asyncpg pool, created on first call). Selected when
    ``CAIRN_STORAGE=postgres://…`` so the ledger lives in the one prod
    Postgres (ADR-0059 D2: gate/audit data is transactional truth)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: PLC0415,F401 — dep guard; shared_pool opens the pool
        except ImportError as e:
            raise RuntimeError(
                "Postgres eval ledger requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresEvalLedger needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        # #614: share one pool per DSN across every Postgres store in this process.
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_EVAL_RUNS_PG_SQL)
            await c.execute(_CREATE_EVAL_RUNS_PG_INDEX_SQL)
            await c.execute(_CREATE_CASE_RESULTS_PG_SQL)
            await c.execute(_CREATE_CASE_RESULTS_PG_INDEX_SQL)
            await c.execute(_CREATE_FAILURE_LABELS_PG_SQL)
            await c.execute(_CREATE_FAILURE_LABELS_PG_INDEX_SQL)

    async def record_eval_run(
        self, eval_run: EvalRun, case_results: Optional[list[EvalCaseResult]] = None
    ) -> EvalRun:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                await c.execute(
                    _UPSERT_EVAL_RUN_PG_SQL,
                    eval_run.eval_run_id, eval_run.run_id, eval_run.created_at,
                    eval_run.model_dump_json(),
                )
                for case in case_results or []:
                    case = case.model_copy(update={"eval_run_id": eval_run.eval_run_id})
                    await c.execute(
                        _UPSERT_CASE_RESULT_PG_SQL,
                        case.case_result_id, case.eval_run_id, case.model_dump_json(),
                    )
        return eval_run

    async def get_eval_runs(self, run_id: str) -> list[EvalRun]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_SELECT_EVAL_RUNS_PG_SQL, run_id)
        return [EvalRun.model_validate_json(r["data"]) for r in rows]

    async def get_case_results(self, eval_run_id: str) -> list[EvalCaseResult]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_SELECT_CASE_RESULTS_PG_SQL, eval_run_id)
        return [EvalCaseResult.model_validate_json(r["data"]) for r in rows]

    async def add_failure_labels(self, run_id: str, labels: list[FailureLabel]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                for label in labels:
                    stamped = label.model_copy(update={"run_id": run_id})
                    await c.execute(
                        _UPSERT_FAILURE_LABEL_PG_SQL,
                        stamped.label_id, stamped.run_id, stamped.created_at,
                        stamped.model_dump_json(),
                    )

    async def get_failure_labels(self, run_id: str) -> list[FailureLabel]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_SELECT_FAILURE_LABELS_PG_SQL, run_id)
        return [FailureLabel.model_validate_json(r["data"]) for r in rows]

    def close(self) -> None:
        # The pool lives for the process (parity with PostgresRagCatalog.close);
        # pool teardown happens with the event loop.
        pass


# ─── Selector ──────────────────────────────────────────────────────────


def default_eval_ledger() -> EvalLedgerStore:
    """Pick an eval ledger from the environment. **`CAIRN_STORAGE` is the
    selector** (ADR-0058/#427): ``postgres://…`` / ``postgresql://…`` →
    :class:`PostgresEvalLedger` (the one prod Postgres). Otherwise preserves
    the config-family default: ``CAIRN_EVAL_LEDGER_DB`` set →
    :class:`SqliteEvalLedger` at that path (``:memory:`` for ephemeral);
    unset → durable :class:`SqliteEvalLedger` on the shared ``cairn.db``
    (config-family stores are durable-by-default per ADR-0058 §5 — unlike
    the run store, which stays opt-in for historical/back-compat reasons).
    """
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresEvalLedger(dsn=url)
    path = getenv("CAIRN_EVAL_LEDGER_DB", "").strip()
    return SqliteEvalLedger(path=path) if path else SqliteEvalLedger()
