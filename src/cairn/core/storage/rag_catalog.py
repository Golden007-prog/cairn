"""RAG index catalog — persistent record of what's indexed (ADR-0023 Fork C).

One row per (corpus, document): its content hash + the build fingerprint it was
indexed under + status. The incremental planner diffs the live corpus against
this catalog to decide re-index / skip / tombstone. The catalog is the **source
of truth** for index state (a ``rag-plan.json`` is only optional dry-run output).

Three tiers now, mirroring ``RunStore``: an in-memory store (tests / ephemeral),
a SQLite store (restart-safe single file — required for incremental to pay off
across runs), and a Postgres store (``CAIRN_STORAGE=postgres://…`` — #427, the one
prod instance instead of a per-container file). The full entry is serialized to
a JSON ``data`` column; ``document_id``/``corpus``/``status`` are mirrored into
indexed columns so per-corpus listing stays cheap.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from cairn.core.pg_schema_init import PgSchemaInit

# Status vocabulary for a single catalogued document.
#   pending    — known, not yet indexed
#   indexed    — successfully embedded under (content_hash, build_fingerprint)
#   failed     — last index attempt errored (see `error`)
#   tombstoned — source removed from the corpus; vectors deleted
CatalogStatus = str


class CatalogEntry(BaseModel):
    """One document's index state within a corpus."""

    document_id: str
    corpus: str
    source: str
    content_hash: str = ""
    build_fingerprint: str = ""
    chunk_count: int = 0
    status: CatalogStatus = "pending"
    indexed_at: str = ""
    error: Optional[str] = None
    extra: dict = Field(default_factory=dict)


@runtime_checkable
class RagCatalogStore(Protocol):
    """Persistence contract for the RAG index catalog."""

    async def get(self, document_id: str) -> Optional[CatalogEntry]: ...
    async def list_by_corpus(self, corpus: str) -> list[CatalogEntry]: ...
    async def upsert(self, entry: CatalogEntry) -> None: ...
    async def tombstone(self, document_id: str) -> None: ...
    async def delete(self, document_id: str) -> None: ...


class InMemoryRagCatalog:
    """Volatile catalog — tests + ephemeral runs. Not restart-safe."""

    def __init__(self) -> None:
        self._by_id: dict[str, CatalogEntry] = {}

    async def get(self, document_id: str) -> Optional[CatalogEntry]:
        e = self._by_id.get(document_id)
        return e.model_copy(deep=True) if e else None

    async def list_by_corpus(self, corpus: str) -> list[CatalogEntry]:
        return [e.model_copy(deep=True) for e in self._by_id.values() if e.corpus == corpus]

    async def upsert(self, entry: CatalogEntry) -> None:
        self._by_id[entry.document_id] = entry.model_copy(deep=True)

    async def tombstone(self, document_id: str) -> None:
        e = self._by_id.get(document_id)
        if e:
            e.status = "tombstoned"

    async def delete(self, document_id: str) -> None:
        self._by_id.pop(document_id, None)


class SqliteRagCatalog:
    """Embedded-SQLite catalog — restart-safe, single file, no service.

    Concurrency mirrors ``SqliteRunStore``: an ``asyncio.Lock`` serializes
    access and the blocking sqlite3 work runs on a worker thread via
    ``asyncio.to_thread`` (#86) so it never stalls the event loop (the lock
    keeps one thread on the ``check_same_thread=False`` connection at a time).
    WAL mode guards against mid-write corruption. ``path=":memory:"`` →
    ephemeral.
    """

    def __init__(self, path: str | None = None) -> None:
        import os
        import sqlite3

        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_RAG_CATALOG", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_catalog (
                document_id TEXT PRIMARY KEY,
                corpus      TEXT NOT NULL,
                status      TEXT NOT NULL,
                data        TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalog_corpus ON rag_catalog(corpus)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    # Each method runs its sqlite3 work via ``asyncio.to_thread`` under the lock
    # (#86) so the blocking call leaves the event loop.

    def _get_sync(self, document_id: str) -> Optional[CatalogEntry]:
        row = self._conn.execute(
            "SELECT data FROM rag_catalog WHERE document_id = ?", (document_id,)
        ).fetchone()
        return CatalogEntry.model_validate_json(row[0]) if row else None

    async def get(self, document_id: str) -> Optional[CatalogEntry]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, document_id)

    def _list_by_corpus_sync(self, corpus: str) -> list[CatalogEntry]:
        rows = self._conn.execute(
            "SELECT data FROM rag_catalog WHERE corpus = ?", (corpus,)
        ).fetchall()
        return [CatalogEntry.model_validate_json(r[0]) for r in rows]

    async def list_by_corpus(self, corpus: str) -> list[CatalogEntry]:
        async with self._lock:
            return await asyncio.to_thread(self._list_by_corpus_sync, corpus)

    def _upsert_sync(self, entry: CatalogEntry) -> None:
        self._conn.execute(
            """
            INSERT INTO rag_catalog (document_id, corpus, status, data)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                corpus = excluded.corpus,
                status = excluded.status,
                data   = excluded.data
            """,
            (entry.document_id, entry.corpus, entry.status, entry.model_dump_json()),
        )
        self._conn.commit()

    async def upsert(self, entry: CatalogEntry) -> None:
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, entry)

    async def tombstone(self, document_id: str) -> None:
        entry = await self.get(document_id)
        if entry is None:
            return
        entry.status = "tombstoned"
        await self.upsert(entry)

    def _delete_sync(self, document_id: str) -> None:
        self._conn.execute(
            "DELETE FROM rag_catalog WHERE document_id = ?", (document_id,)
        )
        self._conn.commit()

    async def delete(self, document_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, document_id)


# ── Postgres backend (mirrors connections.py / storage/postgres/runs.py) — #427 ──
_CREATE_CATALOG_PG_SQL = """
    CREATE TABLE IF NOT EXISTS rag_catalog (
        document_id TEXT PRIMARY KEY,
        corpus      TEXT NOT NULL,
        status      TEXT NOT NULL,
        data        JSONB NOT NULL
    )
"""
_CREATE_CATALOG_PG_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_catalog_corpus ON rag_catalog(corpus)"
)
_INSERT_CATALOG_PG_SQL = """
    INSERT INTO rag_catalog (document_id, corpus, status, data)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (document_id) DO UPDATE
    SET corpus = EXCLUDED.corpus, status = EXCLUDED.status, data = EXCLUDED.data
"""
_SELECT_CATALOG_PG_BY_ID = "SELECT data::text FROM rag_catalog WHERE document_id = $1"
_SELECT_CATALOG_PG_BY_CORPUS = "SELECT data::text FROM rag_catalog WHERE corpus = $1"
_DELETE_CATALOG_PG_SQL = "DELETE FROM rag_catalog WHERE document_id = $1"


class PostgresRagCatalog(PgSchemaInit):
    """Postgres-backed RAG catalog — mirrors :class:`SqliteRagCatalog` (full
    ``CatalogEntry`` in a JSONB ``data`` column, ``document_id``/``corpus``/
    ``status`` mirrored into indexed columns) and :class:`PostgresConnectionStore`
    (lazy asyncpg pool, created on first call). Selected by
    :func:`default_rag_catalog` when ``CAIRN_STORAGE=postgres://…`` so the catalog
    lives in the one prod Postgres instead of a per-container SQLite file — the
    storage-unification move for this store (#427)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: PLC0415,F401 — dep guard; shared_pool opens the pool
        except ImportError as e:
            raise RuntimeError(
                "Postgres RAG catalog requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresRagCatalog needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        # #614: share one pool per DSN across every Postgres store in this process.
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_CATALOG_PG_SQL)
            await c.execute(_CREATE_CATALOG_PG_INDEX_SQL)

    async def get(self, document_id: str) -> Optional[CatalogEntry]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_CATALOG_PG_BY_ID, document_id)
        return CatalogEntry.model_validate_json(row["data"]) if row else None

    async def list_by_corpus(self, corpus: str) -> list[CatalogEntry]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_SELECT_CATALOG_PG_BY_CORPUS, corpus)
        return [CatalogEntry.model_validate_json(r["data"]) for r in rows]

    async def upsert(self, entry: CatalogEntry) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(
                _INSERT_CATALOG_PG_SQL,
                entry.document_id, entry.corpus, entry.status, entry.model_dump_json(),
            )

    async def tombstone(self, document_id: str) -> None:
        entry = await self.get(document_id)
        if entry is None:
            return
        entry.status = "tombstoned"
        await self.upsert(entry)

    async def delete(self, document_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(_DELETE_CATALOG_PG_SQL, document_id)

    def close(self) -> None:
        # The pool lives for the process (parity with PostgresConnectionStore.close);
        # pool teardown happens with the event loop.
        pass


def default_rag_catalog() -> RagCatalogStore:
    """Pick a catalog from the environment. **`CAIRN_STORAGE` is the selector**
    (#427): ``postgres://…`` / ``postgresql://…`` → :class:`PostgresRagCatalog`
    (the one prod Postgres). Otherwise preserves the existing opt-in behavior:
    ``CAIRN_RAG_CATALOG`` set → :class:`SqliteRagCatalog` at that path
    (``:memory:`` for ephemeral); unset → volatile :class:`InMemoryRagCatalog`.

    Persistence stays **opt-in** for the sqlite/in-memory branch, mirroring the
    run store's ``CAIRN_STORAGE`` knob: constructing the default backend (every
    dispatcher / ``build_storage`` call) must have no disk side effects when
    neither knob is set. Incremental indexing only pays off across runs when
    the catalog persists, so a deployment that schedules re-indexing sets
    ``CAIRN_STORAGE=postgres://…`` (prod) or ``CAIRN_RAG_CATALOG=~/.cairn/rag_catalog.db``
    (single-file) in its profile/env.
    """
    import os
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresRagCatalog(dsn=url)
    path = getenv("CAIRN_RAG_CATALOG", "").strip()
    return SqliteRagCatalog(path=path) if path else InMemoryRagCatalog()
