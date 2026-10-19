"""Cross-batch dataset fingerprint index (ADR-0080 P2, #1030 — rejected design R3).

Cross-batch dedup state as an INDEXED table, not a parquet glob. A dataset that
grows batch by batch must not re-ingest rows an earlier batch already contributed;
the authoritative check is "has this row's content hash been published before, in
THIS dataset's lineage?". Answering that from a glob of every historical batch's
parquet is O(history) — ~3B row-reads/month at the ADR's target scale — and two
in-flight batches can't see each other's rows (write skew). An indexed table makes
the anti-join O(batch) and a unique key makes concurrent publishes serialize.

    dataset_fingerprints(dataset_id, hash, batch_id, created_at,
                         PRIMARY KEY (dataset_id, hash))

The PK is ``(dataset_id, hash)``, not the bare ``hash`` the issue sketched — a bare
hash PK would treat an identical row in an UNRELATED dataset as a cross-batch dup.
Scoping by ``dataset_id`` keeps each dataset's lineage independent while the primary
key still gives an index-covered lookup and the serialize-on-conflict guarantee.

Two backends behind one async protocol, mirroring the run store: a hermetic
``SqliteFingerprintStore`` (stdlib ``sqlite3`` — real indexed anti-join + upsert,
``:memory:`` for tests) and a production ``PostgresFingerprintStore`` (asyncpg, the
existing ``CAIRN_STORAGE`` Postgres). ``publish`` is TRANSACTION-AWARE: pass an open
connection and the fingerprint upsert commits in the SAME transaction as the fix
job's manifest (P3), so a batch is either fully published-and-indexed or neither.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable

from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import schema_init_lock

# chunk size for the ``hash IN (…)`` anti-join / upsert VALUES — keeps a single
# statement well under any driver parameter limit on a million-row batch.
_CHUNK = 900


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing a batch's hashes against the dataset's history.

    ``checked`` is False when no fingerprint backend is configured — the probe is
    advisory (the authoritative dedup is the fix job), so an unconfigured deployment
    simply reports "not checked" rather than failing the readiness gate."""

    checked: bool
    overlap: int          # batch rows whose hash already exists in history
    probed: int           # total distinct hashes probed

    @property
    def overlap_pct(self) -> float:
        return round(self.overlap / self.probed * 100, 1) if self.probed else 0.0


@runtime_checkable
class FingerprintStore(Protocol):
    async def ensure_schema(self) -> None: ...
    async def probe(
        self, dataset_id: str, hashes: Sequence[str], *, exclude_batch: Optional[str] = None,
    ) -> ProbeResult: ...
    async def publish(
        self, dataset_id: str, batch_id: str, hashes: Sequence[str], *,
        created_at: Optional[str] = None, conn: object = None,
    ) -> int: ...
    async def retain(
        self, *, dataset_id: Optional[str] = None,
        keep_days: Optional[int] = None, keep_batches: Optional[int] = None,
    ) -> int: ...
    async def count(self, dataset_id: Optional[str] = None) -> int: ...
    async def close(self) -> None: ...


def _dedup_preserve(hashes: Sequence[str]) -> list[str]:
    """Distinct hashes in first-seen order (a batch may carry internal dups; the
    cross-batch probe is about DISTINCT content vs history)."""
    seen: set[str] = set()
    out: list[str] = []
    for h in hashes:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


class _NullFingerprintStore:
    """Used when no backend is configured: probe is advisory-only (checked=False),
    publish is a no-op. Lets the cross-batch feature degrade gracefully (hermetic
    fallback pattern) instead of hard-requiring Postgres."""

    async def ensure_schema(self) -> None:
        return None

    async def probe(self, dataset_id, hashes, *, exclude_batch=None) -> ProbeResult:
        return ProbeResult(checked=False, overlap=0, probed=len(_dedup_preserve(hashes)))

    async def publish(self, dataset_id, batch_id, hashes, *, created_at=None, conn=None) -> int:
        return 0

    async def retain(self, *, dataset_id=None, keep_days=None, keep_batches=None) -> int:
        return 0

    async def count(self, dataset_id=None) -> int:
        return 0

    async def close(self) -> None:
        return None


# ─── SQLite (hermetic; real indexed anti-join + upsert) ───────────────────────


_CREATE_SQLITE = """
    CREATE TABLE IF NOT EXISTS dataset_fingerprints (
        dataset_id TEXT NOT NULL,
        hash       TEXT NOT NULL,
        batch_id   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (dataset_id, hash)
    )
"""
# retention scans by (dataset_id, batch_id) / created_at — index them.
_CREATE_SQLITE_BATCH_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fp_dataset_batch "
    "ON dataset_fingerprints(dataset_id, batch_id)"
)
_CREATE_SQLITE_CREATED_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fp_created ON dataset_fingerprints(created_at)"
)


class SqliteFingerprintStore:
    """Stdlib-``sqlite3`` fingerprint index — the hermetic path (tests use
    ``:memory:``) and a real single-file local backend. Sync ``sqlite3`` behind an
    ``asyncio.Lock`` so one coroutine touches the connection at a time (mirrors
    :class:`SqliteRunStore`)."""

    def __init__(self, path: str = ":memory:") -> None:
        import os
        import sqlite3

        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_SQLITE)
        self._conn.execute(_CREATE_SQLITE_BATCH_IDX)
        self._conn.execute(_CREATE_SQLITE_CREATED_IDX)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        return None  # created in __init__

    async def probe(self, dataset_id, hashes, *, exclude_batch=None) -> ProbeResult:
        uniq = _dedup_preserve(hashes)
        if not uniq:
            return ProbeResult(checked=True, overlap=0, probed=0)
        async with self._lock:
            overlap = 0
            for i in range(0, len(uniq), _CHUNK):
                chunk = uniq[i:i + _CHUNK]
                ph = ",".join("?" * len(chunk))
                sql = (
                    f"SELECT COUNT(*) FROM dataset_fingerprints "
                    f"WHERE dataset_id=? AND hash IN ({ph})"
                )
                params: list[object] = [dataset_id, *chunk]
                if exclude_batch is not None:
                    sql += " AND batch_id<>?"
                    params.append(exclude_batch)
                overlap += self._conn.execute(sql, params).fetchone()[0]
        return ProbeResult(checked=True, overlap=overlap, probed=len(uniq))

    async def publish(self, dataset_id, batch_id, hashes, *, created_at=None, conn=None) -> int:
        uniq = _dedup_preserve(hashes)
        if not uniq:
            return 0
        ts = created_at or utc_now_iso()
        async with self._lock:
            before = self._conn.total_changes
            # ON CONFLICT DO NOTHING → re-publishing an already-indexed row is a
            # no-op; the newly-inserted count is the delta in total_changes.
            for i in range(0, len(uniq), _CHUNK):
                chunk = uniq[i:i + _CHUNK]
                self._conn.executemany(
                    "INSERT INTO dataset_fingerprints(dataset_id, hash, batch_id, created_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(dataset_id, hash) DO NOTHING",
                    [(dataset_id, h, batch_id, ts) for h in chunk],
                )
            self._conn.commit()
            return self._conn.total_changes - before

    async def retain(self, *, dataset_id=None, keep_days=None, keep_batches=None) -> int:
        async with self._lock:
            before = self._conn.total_changes
            scope = "WHERE dataset_id=?" if dataset_id else "WHERE 1=1"
            base_params: list[object] = [dataset_id] if dataset_id else []
            if keep_days is not None:
                cutoff = self._cutoff(keep_days)
                self._conn.execute(
                    f"DELETE FROM dataset_fingerprints {scope} AND created_at < ?",
                    [*base_params, cutoff],
                )
            if keep_batches is not None:
                # keep the most-recent ``keep_batches`` batch_ids per dataset; drop
                # fingerprints from older batches.
                rows = self._conn.execute(
                    "SELECT dataset_id, batch_id, MAX(created_at) c "
                    "FROM dataset_fingerprints "
                    + ("WHERE dataset_id=? " if dataset_id else "")
                    + "GROUP BY dataset_id, batch_id ORDER BY c DESC",
                    base_params,
                ).fetchall()
                keep: dict[str, set] = {}
                for ds, batch, _c in rows:
                    keep.setdefault(ds, set())
                    if len(keep[ds]) < keep_batches:
                        keep[ds].add(batch)
                for ds, batches in keep.items():
                    ph = ",".join("?" * len(batches)) or "''"
                    self._conn.execute(
                        f"DELETE FROM dataset_fingerprints WHERE dataset_id=? "
                        f"AND batch_id NOT IN ({ph})",
                        [ds, *batches],
                    )
            self._conn.commit()
            return self._conn.total_changes - before

    @staticmethod
    def _cutoff(keep_days: int) -> str:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()

    async def count(self, dataset_id=None) -> int:
        async with self._lock:
            if dataset_id:
                return self._conn.execute(
                    "SELECT COUNT(*) FROM dataset_fingerprints WHERE dataset_id=?",
                    [dataset_id],
                ).fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM dataset_fingerprints"
            ).fetchone()[0]

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()


# ─── Postgres (production; the existing CAIRN_STORAGE database) ──────────────────


_CREATE_PG = """
    CREATE TABLE IF NOT EXISTS dataset_fingerprints (
        dataset_id TEXT NOT NULL,
        hash       TEXT NOT NULL,
        batch_id   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (dataset_id, hash)
    )
"""
_CREATE_PG_BATCH_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fp_dataset_batch "
    "ON dataset_fingerprints(dataset_id, batch_id)"
)
_CREATE_PG_CREATED_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fp_created ON dataset_fingerprints(created_at)"
)


class PostgresFingerprintStore:
    """asyncpg-backed fingerprint index in the existing ``CAIRN_STORAGE`` Postgres.

    Concurrency (closes R3 write-skew): the ``(dataset_id, hash)`` primary key
    makes two in-flight batches that share a row SERIALIZE at publish — the second
    ``INSERT … ON CONFLICT DO NOTHING`` sees the first's committed row and inserts
    nothing for it. ``publish(conn=…)`` runs the upsert on a caller-supplied
    connection so the fix job commits fingerprints in the SAME transaction as its
    manifest (P3): the batch is atomically published-and-indexed, or neither."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._schema_ready = False

    async def _pool(self):
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415
        return await shared_pool(self._dsn)

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        pool = await self._pool()
        async with pool.acquire() as conn, schema_init_lock(conn):  # #2551 — replicas serialise DDL
            await conn.execute(_CREATE_PG)
            await conn.execute(_CREATE_PG_BATCH_IDX)
            await conn.execute(_CREATE_PG_CREATED_IDX)
        self._schema_ready = True

    async def probe(self, dataset_id, hashes, *, exclude_batch=None) -> ProbeResult:
        uniq = _dedup_preserve(hashes)
        if not uniq:
            return ProbeResult(checked=True, overlap=0, probed=0)
        await self.ensure_schema()
        pool = await self._pool()
        overlap = 0
        async with pool.acquire() as conn:
            # unnest the batch hashes and count how many already exist — an indexed
            # anti-join, O(batch) regardless of history size.
            sql = (
                "SELECT COUNT(*) FROM dataset_fingerprints f "
                "JOIN unnest($2::text[]) AS b(hash) ON f.hash = b.hash "
                "WHERE f.dataset_id = $1"
            )
            params: list[object] = [dataset_id, uniq]
            if exclude_batch is not None:
                sql += " AND f.batch_id <> $3"
                params.append(exclude_batch)
            overlap = await conn.fetchval(sql, *params)
        return ProbeResult(checked=True, overlap=int(overlap or 0), probed=len(uniq))

    async def publish(self, dataset_id, batch_id, hashes, *, created_at=None, conn=None) -> int:
        uniq = _dedup_preserve(hashes)
        if not uniq:
            return 0
        ts = created_at or utc_now_iso()
        await self.ensure_schema()
        sql = (
            "INSERT INTO dataset_fingerprints(dataset_id, hash, batch_id, created_at) "
            "SELECT $1, b.hash, $3, $4 FROM unnest($2::text[]) AS b(hash) "
            "ON CONFLICT (dataset_id, hash) DO NOTHING"
        )
        args = (dataset_id, uniq, batch_id, ts)

        async def _run(c) -> int:
            # asyncpg returns e.g. 'INSERT 0 5' → newly-inserted count.
            status = await c.execute(sql, *args)
            try:
                return int(status.split()[-1])
            except (ValueError, IndexError):
                return 0

        if conn is not None:  # transaction-aware: caller owns the txn (P3 manifest)
            return await _run(conn)
        pool = await self._pool()
        async with pool.acquire() as own:
            async with own.transaction():
                return await _run(own)

    async def retain(self, *, dataset_id=None, keep_days=None, keep_batches=None) -> int:
        await self.ensure_schema()
        pool = await self._pool()
        deleted = 0
        async with pool.acquire() as conn:
            if keep_days is not None:
                from datetime import timedelta
                cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
                scope = "dataset_id=$2 AND " if dataset_id else ""
                params: list[object] = [cutoff] + ([dataset_id] if dataset_id else [])
                status = await conn.execute(
                    f"DELETE FROM dataset_fingerprints WHERE {scope}created_at < $1", *params,
                )
                deleted += _rowcount(status)
            if keep_batches is not None:
                where_ds = "WHERE dataset_id=$1 " if dataset_id else ""
                ds_params = [dataset_id] if dataset_id else []
                rows = await conn.fetch(
                    "SELECT dataset_id, batch_id, MAX(created_at) c "
                    f"FROM dataset_fingerprints {where_ds}"
                    "GROUP BY dataset_id, batch_id ORDER BY c DESC", *ds_params,
                )
                keep: dict[str, list] = {}
                for r in rows:
                    keep.setdefault(r["dataset_id"], [])
                    if len(keep[r["dataset_id"]]) < keep_batches:
                        keep[r["dataset_id"]].append(r["batch_id"])
                for ds, batches in keep.items():
                    status = await conn.execute(
                        "DELETE FROM dataset_fingerprints WHERE dataset_id=$1 "
                        "AND batch_id <> ALL($2::text[])", ds, batches,
                    )
                    deleted += _rowcount(status)
        return deleted

    async def count(self, dataset_id=None) -> int:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            if dataset_id:
                return int(await conn.fetchval(
                    "SELECT COUNT(*) FROM dataset_fingerprints WHERE dataset_id=$1", dataset_id,
                ) or 0)
            return int(await conn.fetchval("SELECT COUNT(*) FROM dataset_fingerprints") or 0)

    async def close(self) -> None:
        return None  # pool is process-shared (#614); not owned here


def _rowcount(status: str) -> int:
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


# ─── resolver ─────────────────────────────────────────────────────────────────


def build_fingerprint_store(dsn: Optional[str] = None) -> FingerprintStore:
    """Pick a backend by ``dsn`` (falls back to ``CAIRN_STORAGE``), mirroring the run
    store's resolution:

      - ``postgres://…`` / ``postgresql://…`` → :class:`PostgresFingerprintStore`
      - ``sqlite:///path`` / ``sqlite://:memory:`` → :class:`SqliteFingerprintStore`
      - nothing set → :class:`_NullFingerprintStore` (advisory-only; cross-batch
        dedup is disabled but the readiness gate still runs)
    """
    import os

    url = (dsn or getenv("CAIRN_STORAGE", "")).strip()
    if not url:
        return _NullFingerprintStore()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresFingerprintStore(dsn=url)
    if url.startswith("sqlite://"):
        # #1471 — ONE parser (settings.sqlite_path_from_url) so every store
        # resolves the same CAIRN_STORAGE to the same file.
        from cairn.core import settings  # noqa: PLC0415 - cycle: settings imports storage on some paths
        return SqliteFingerprintStore(path=settings.sqlite_path_from_url(url) or ":memory:")
    return _NullFingerprintStore()
