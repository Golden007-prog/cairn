"""Workflow provenance stores (#220, ADR-0047).

Two records, mirroring the trigger/install store pattern (InMemory + Sqlite, a
JSON ``data`` blob, ``asyncio.to_thread`` under a lock):

1. **Content-addressed schema store** — the workflow input/output JSON-Schema is
   stored here as *data*, keyed by a content hash, **never inline in code**. Runs
   and installed-workflow records reference it only by ``schema_hash``, so a
   reinstall that changes the schema doesn't rewrite history (the old hash still
   resolves).
2. **Versioned installed-workflow record** — one per ``(workflow_ref, version)``:
   the schema it shipped (by hash), its required capabilities + composed operator
   packs, and config (default environment + tracker). A reinstall whose schema
   changed supersedes the prior ``active`` record for that ref (never deleted).

The concrete operator binding is NOT here — it's environment-dependent, so it's
recorded per-run as ``Run.resolved_operators`` (see ADR-0047's capability vs.
resolved-operator split).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from cairn.core.pack.registry import all_pipeline_dirs

from pydantic import BaseModel, Field

from cairn.core.storage import migrations
from cairn.core.workspace import DEFAULT_WORKSPACE, current_workspace
from cairn.core.pg_schema_init import PgSchemaInit

#: Canonical column spec for the SQLite ``workflow_installed`` table — one source of truth
#: for both the fresh ``CREATE TABLE`` and the pre-#223 → composite-PK rebuild
#: (ADR-0050). Workspace-scoped: the composite PK keeps the same ref@version in
#: two workspaces as distinct rows.
_INSTALLED_SCHEMA = (
    "id TEXT, ref TEXT, workspace_id TEXT NOT NULL DEFAULT 'default', "
    "status TEXT, data TEXT NOT NULL, PRIMARY KEY (workspace_id, id)"
)


logger = logging.getLogger("cairn.core.workflow_registry")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def schema_hash(input_schema: dict | None, output_schema: dict | None = None) -> str:
    """Stable content hash of a workflow's I/O schema (sha256 over sorted keys).
    The same shape across versions dedupes to one stored schema."""
    blob = json.dumps({"in": input_schema or {}, "out": output_schema or {}}, sort_keys=True, default=str)
    return "sch_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class WorkflowSchema(BaseModel):
    """A content-addressed schema artifact (#220) — the I/O JSON-Schema as data."""

    schema_hash: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


class WorkflowTemplate(BaseModel):
    """A content-addressed *definition* artifact (#225, ADR-0049) — the compiled
    template AST as data, keyed by ``template_hash`` (the engine's content hash,
    ``compiled.compute_content_hash``). The other half of "the definition is the
    DB system of record": the I/O schema lives in :class:`WorkflowSchema`, the
    runnable shape lives here, and the installed-workflow record references both
    by hash. Never inline in code; dedup by hash."""

    template_hash: str
    ast: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)


class InstalledWorkflow(BaseModel):
    """A versioned installed-workflow record (#220) — environment-agnostic."""

    id: str = ""  # == workflow_ref@version (workspace-scoped via workspace_id, #223)
    workflow_ref: str
    #: Tenancy scope (ADR-0048 rev 2, #223). ``"default"`` for single-node/OSS;
    #: records are keyed + queried by (workspace_id, id) so the same ref@version
    #: in two workspaces never collides.
    workspace_id: str = DEFAULT_WORKSPACE
    version: str = ""
    pack_ref: str = ""
    pack_version: str = ""
    schema_hash: str = ""
    #: Reference into the content-addressed template store (#225, ADR-0049) — the
    #: compiled definition AST this record's workflow runs. Empty until the
    #: definition is stored (run-time provenance or discovery-sync).
    template_hash: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    composes: list[str] = Field(default_factory=list)  # operator refs / packs it binds
    config: dict[str, Any] = Field(default_factory=dict)  # default env + tracker config
    status: str = "active"  # active | superseded
    actor: str = "console"
    installed_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    def model_post_init(self, _ctx: object) -> None:  # pydantic v2 hook
        if not self.id:
            # discriminate versions: the published version when known; else the
            # compiled-definition hash (template), else the schema hash — so a
            # definition change with an unchanged schema is still a distinct row (#225).
            disc = self.template_hash or self.schema_hash
            ver = self.version or (disc[4:12] if disc else "local")
            object.__setattr__(self, "id", f"{self.workflow_ref}@{ver}")


# ── stores ────────────────────────────────────────────────────────────


class InMemoryWorkflowRegistry:
    """Volatile store (tests / no-config default)."""

    def __init__(self) -> None:
        # schemas stay globally content-addressed (immutable data, identical
        # across workspaces → dedup is correct); installed records are scoped by
        # (workspace_id, id) so the same ref@version in two workspaces is distinct.
        self._schemas: dict[str, WorkflowSchema] = {}
        self._templates: dict[str, WorkflowTemplate] = {}
        self._installed: dict[tuple[str, str], InstalledWorkflow] = {}

    async def put_schema(self, s: WorkflowSchema) -> WorkflowSchema:
        self._schemas.setdefault(s.schema_hash, s)  # content-addressed → first write wins
        return self._schemas[s.schema_hash]

    async def get_schema(self, h: str) -> Optional[WorkflowSchema]:
        return self._schemas.get(h)

    async def put_template(self, t: WorkflowTemplate) -> WorkflowTemplate:
        self._templates.setdefault(t.template_hash, t)  # content-addressed → first write wins
        return self._templates[t.template_hash]

    async def get_template(self, h: str) -> Optional[WorkflowTemplate]:
        return self._templates.get(h)

    async def record_installed(self, rec: InstalledWorkflow) -> InstalledWorkflow:
        # supersede the prior active record for this ref *in the same workspace*.
        for other in self._installed.values():
            if (other.workspace_id == rec.workspace_id and other.workflow_ref == rec.workflow_ref
                    and other.status == "active" and other.id != rec.id):
                other.status = "superseded"
                other.updated_at = _now()
        self._installed[(rec.workspace_id, rec.id)] = rec
        return rec

    async def get_installed(self, rid: str, workspace_id: str = DEFAULT_WORKSPACE) -> Optional[InstalledWorkflow]:
        return self._installed.get((workspace_id, rid))

    async def active_for(self, workflow_ref: str, workspace_id: str = DEFAULT_WORKSPACE) -> Optional[InstalledWorkflow]:
        for rec in self._installed.values():
            if rec.workspace_id == workspace_id and rec.workflow_ref == workflow_ref and rec.status == "active":
                return rec
        return None

    async def list_installed(self, workspace_id: Optional[str] = None) -> list[InstalledWorkflow]:
        recs = self._installed.values()
        if workspace_id is not None:
            recs = [r for r in recs if r.workspace_id == workspace_id]
        return sorted(recs, key=lambda r: (r.workspace_id, r.id))

    async def set_workspace_records_status(self, workspace_id: str, from_status: str, to_status: str) -> int:
        n = 0
        for rec in self._installed.values():
            if rec.workspace_id == workspace_id and rec.status == from_status:
                rec.status = to_status
                rec.updated_at = _now()
                n += 1
        return n

    def close(self) -> None:
        pass


class SqliteWorkflowRegistry:
    """Embedded-SQLite store — three tables (``workflow_schemas``,
    ``workflow_templates``, ``workflow_installed``), JSON ``data`` blob; async ops
    via ``asyncio.to_thread`` under a lock. Mirrors SqliteInstallStore.

    Table names are prefixed with ``workflow_`` (epic #427) so they're safe to
    live alongside other stores' tables in one shared ``~/.cairn/cairn.db`` —
    matching the already-prefixed :class:`PostgresWorkflowRegistry` names. Older
    ``~/.cairn/workflows.db`` files with the original bare names (``schemas``,
    ``templates``, ``installed``) are renamed in place on connect by
    :meth:`_migrate_table_names`, so no data is lost.
    """

    def __init__(self, path: str | None = None) -> None:
        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_WORKFLOW_REGISTRY_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate_table_names()
        self._conn.execute("CREATE TABLE IF NOT EXISTS workflow_schemas (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
        # content-addressed template-definition store (#225, ADR-0049)
        self._conn.execute("CREATE TABLE IF NOT EXISTS workflow_templates (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
        # workspace-scoped (ADR-0048 rev 2, #223): composite PK so the same
        # ref@version in two workspaces is a distinct row. Fresh DBs get this
        # schema directly; pre-#223 DBs created before the column are migrated by
        # _migrate_installed() below (the CREATE here is a no-op for them).
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS workflow_installed ({_INSTALLED_SCHEMA})")
        self._migrate_installed()
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _migrate_table_names(self) -> None:
        """One-time upgrade (#427): rename the original bare table names —
        ``schemas``/``templates``/``installed`` — to the ``workflow_``-prefixed
        names, so a pre-existing ``~/.cairn/workflows.db`` (or a future shared
        ``~/.cairn/cairn.db``) upgrades in place without losing data. Guarded per
        table so it's a no-op once renamed, and safe even if only some of the
        three old tables exist (e.g. a DB created mid-migration)."""
        c = self._conn
        for old, new in (("schemas", "workflow_schemas"), ("templates", "workflow_templates"), ("installed", "workflow_installed")):
            if migrations.table_exists(c, old) and not migrations.table_exists(c, new):
                c.execute(f"ALTER TABLE {old} RENAME TO {new}")

    def _migrate_installed(self) -> None:
        """Bring a pre-#223 ``workflow_installed`` table (PK ``(id)``, no
        ``workspace_id``) up to the current ``(workspace_id, id)`` composite-PK
        schema (#255).

        SQLite can't ALTER a primary key, so this rebuilds the table, back-filling
        ``workspace_id='default'`` for existing rows. No-op once current — so it's
        safe on every connect. Without it, ``SELECT … WHERE workspace_id=?`` raised
        ``no such column: workspace_id`` and took down the Workflows page.
        """
        c = self._conn
        if "workspace_id" in migrations.columns(c, "workflow_installed") and \
                migrations.pk_columns(c, "workflow_installed") == ["workspace_id", "id"]:
            return  # already on the current schema
        migrations.rebuild_table(
            c, "workflow_installed",
            new_schema=_INSTALLED_SCHEMA,
            select_columns="id, ref, workspace_id, status, data",
            # the old table has no workspace_id column → back-fill the literal.
            from_select="SELECT id, ref, 'default', status, data FROM workflow_installed",
        )

    async def put_schema(self, s: WorkflowSchema) -> WorkflowSchema:
        async with self._lock:
            def _w() -> None:
                self._conn.execute("INSERT OR IGNORE INTO workflow_schemas (hash, data) VALUES (?,?)", (s.schema_hash, s.model_dump_json()))
                self._conn.commit()
            await asyncio.to_thread(_w)
        return s

    async def get_schema(self, h: str) -> Optional[WorkflowSchema]:
        row = self._conn.execute("SELECT data FROM workflow_schemas WHERE hash=?", (h,)).fetchone()
        return WorkflowSchema.model_validate_json(row[0]) if row else None

    async def put_template(self, t: WorkflowTemplate) -> WorkflowTemplate:
        async with self._lock:
            def _w() -> None:
                self._conn.execute("INSERT OR IGNORE INTO workflow_templates (hash, data) VALUES (?,?)", (t.template_hash, t.model_dump_json()))
                self._conn.commit()
            await asyncio.to_thread(_w)
        return t

    async def get_template(self, h: str) -> Optional[WorkflowTemplate]:
        row = self._conn.execute("SELECT data FROM workflow_templates WHERE hash=?", (h,)).fetchone()
        return WorkflowTemplate.model_validate_json(row[0]) if row else None

    async def record_installed(self, rec: InstalledWorkflow) -> InstalledWorkflow:
        async with self._lock:
            def _w() -> None:
                # supersede prior active records for this ref *in the same workspace*.
                for (rid, data) in self._conn.execute(
                    "SELECT id, data FROM workflow_installed WHERE ref=? AND workspace_id=? AND status='active'",
                    (rec.workflow_ref, rec.workspace_id),
                ).fetchall():
                    if rid == rec.id:
                        continue
                    other = InstalledWorkflow.model_validate_json(data)
                    other.status = "superseded"
                    other.updated_at = _now()
                    self._conn.execute(
                        "UPDATE workflow_installed SET status='superseded', data=? WHERE workspace_id=? AND id=?",
                        (other.model_dump_json(), rec.workspace_id, rid),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO workflow_installed (id, ref, workspace_id, status, data) VALUES (?,?,?,?,?)",
                    (rec.id, rec.workflow_ref, rec.workspace_id, rec.status, rec.model_dump_json()),
                )
                self._conn.commit()
            await asyncio.to_thread(_w)
        return rec

    async def get_installed(self, rid: str, workspace_id: str = DEFAULT_WORKSPACE) -> Optional[InstalledWorkflow]:
        row = self._conn.execute(
            "SELECT data FROM workflow_installed WHERE id=? AND workspace_id=?", (rid, workspace_id)
        ).fetchone()
        return InstalledWorkflow.model_validate_json(row[0]) if row else None

    async def active_for(self, workflow_ref: str, workspace_id: str = DEFAULT_WORKSPACE) -> Optional[InstalledWorkflow]:
        row = self._conn.execute(
            "SELECT data FROM workflow_installed WHERE ref=? AND workspace_id=? AND status='active' LIMIT 1",
            (workflow_ref, workspace_id),
        ).fetchone()
        return InstalledWorkflow.model_validate_json(row[0]) if row else None

    async def list_installed(self, workspace_id: Optional[str] = None) -> list[InstalledWorkflow]:
        if workspace_id is not None:
            rows = self._conn.execute("SELECT data FROM workflow_installed WHERE workspace_id=?", (workspace_id,)).fetchall()
        else:
            rows = self._conn.execute("SELECT data FROM workflow_installed").fetchall()
        return sorted((InstalledWorkflow.model_validate_json(r[0]) for r in rows), key=lambda x: (x.workspace_id, x.id))

    async def set_workspace_records_status(self, workspace_id: str, from_status: str, to_status: str) -> int:
        async with self._lock:
            def _w() -> int:
                rows = self._conn.execute(
                    "SELECT id, data FROM workflow_installed WHERE workspace_id=? AND status=?", (workspace_id, from_status)
                ).fetchall()
                for rid, data in rows:
                    rec = InstalledWorkflow.model_validate_json(data)
                    rec.status, rec.updated_at = to_status, _now()
                    self._conn.execute(
                        "UPDATE workflow_installed SET status=?, data=? WHERE workspace_id=? AND id=?",
                        (to_status, rec.model_dump_json(), workspace_id, rid),
                    )
                self._conn.commit()
                return len(rows)
            return await asyncio.to_thread(_w)

    def close(self) -> None:
        self._conn.close()


class PostgresWorkflowRegistry(PgSchemaInit):
    """Postgres-backed workflow registry (#236) — the fleet-shared backend.

    Mirrors :class:`SqliteWorkflowRegistry` (schemas + templates content-addressed
    + workspace-scoped installed records, same supersede semantics) over an
    asyncpg pool with JSONB ``data`` columns, in the shape of
    :class:`~cairn.core.storage.postgres.runs.PostgresRunStore`.

    Why (ADR-0048 / #236): the sqlite registry is per-node, so a multi-instance
    fleet wouldn't share installed-workflows. With ``CAIRN_STORAGE=postgres://…``
    the installed/definition set is authoritative for the whole fleet. Requires
    the ``storage-postgres`` optional dep group (asyncpg)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._schema_ready = False

    async def _open_pool(self) -> Any:
        # Missing DEP before missing CONFIG (#1184) — parity with
        # PostgresWorkspaceStore; shared_pool would ModuleNotFoundError anyway.
        try:
            import asyncpg  # noqa: F401, PLC0415 — optional dep (storage-postgres extra)
        except ImportError as e:
            raise RuntimeError(
                "Postgres workflow registry requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresWorkflowRegistry needs a DSN. Pass dsn= explicitly or set "
                "CAIRN_STORAGE=postgres://… before constructing the store."
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS workflow_schemas (hash TEXT PRIMARY KEY, data JSONB NOT NULL)")
            await conn.execute("CREATE TABLE IF NOT EXISTS workflow_templates (hash TEXT PRIMARY KEY, data JSONB NOT NULL)")
            # composite PK so the same ref@version in two workspaces is distinct (#223).
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_installed "
                "(workspace_id TEXT NOT NULL DEFAULT 'default', id TEXT NOT NULL, "
                "ref TEXT, status TEXT, data JSONB NOT NULL, PRIMARY KEY (workspace_id, id))"
            )
            # Defensive migration (#255, ADR-0050): a workflow_installed created
            # before #223 lacks workspace_id → add it so list/get queries don't
            # 500. (Postgres can ADD COLUMN IF NOT EXISTS in place; an existing
            # single-column PK would need a manual rebuild, but the PG backend
            # postdates #223 so fresh tables already carry the composite PK.)
            await conn.execute(
                "ALTER TABLE workflow_installed "
                "ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default'"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wf_installed_ref "
                "ON workflow_installed(workspace_id, ref, status)"
            )

    async def put_schema(self, s: WorkflowSchema) -> WorkflowSchema:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # content-addressed → first write wins (ON CONFLICT DO NOTHING).
            await conn.execute(
                "INSERT INTO workflow_schemas (hash, data) VALUES ($1, $2::jsonb) ON CONFLICT (hash) DO NOTHING",
                s.schema_hash, s.model_dump_json(),
            )
        return s

    async def get_schema(self, h: str) -> Optional[WorkflowSchema]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data::text AS data FROM workflow_schemas WHERE hash=$1", h)
        return WorkflowSchema.model_validate_json(row["data"]) if row else None

    async def put_template(self, t: WorkflowTemplate) -> WorkflowTemplate:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO workflow_templates (hash, data) VALUES ($1, $2::jsonb) ON CONFLICT (hash) DO NOTHING",
                t.template_hash, t.model_dump_json(),
            )
        return t

    async def get_template(self, h: str) -> Optional[WorkflowTemplate]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data::text AS data FROM workflow_templates WHERE hash=$1", h)
        return WorkflowTemplate.model_validate_json(row["data"]) if row else None

    async def record_installed(self, rec: InstalledWorkflow) -> InstalledWorkflow:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # supersede the prior active record for this ref *in the same workspace*.
                rows = await conn.fetch(
                    "SELECT id, data::text AS data FROM workflow_installed "
                    "WHERE ref=$1 AND workspace_id=$2 AND status='active'",
                    rec.workflow_ref, rec.workspace_id,
                )
                for r in rows:
                    if r["id"] == rec.id:
                        continue
                    other = InstalledWorkflow.model_validate_json(r["data"])
                    other.status = "superseded"
                    other.updated_at = _now()
                    await conn.execute(
                        "UPDATE workflow_installed SET status='superseded', data=$3::jsonb "
                        "WHERE workspace_id=$1 AND id=$2",
                        rec.workspace_id, r["id"], other.model_dump_json(),
                    )
                await conn.execute(
                    "INSERT INTO workflow_installed (workspace_id, id, ref, status, data) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb) "
                    "ON CONFLICT (workspace_id, id) DO UPDATE "
                    "SET ref=EXCLUDED.ref, status=EXCLUDED.status, data=EXCLUDED.data",
                    rec.workspace_id, rec.id, rec.workflow_ref, rec.status, rec.model_dump_json(),
                )
        return rec

    async def get_installed(self, rid: str, workspace_id: str = DEFAULT_WORKSPACE) -> Optional[InstalledWorkflow]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data::text AS data FROM workflow_installed WHERE id=$1 AND workspace_id=$2", rid, workspace_id
            )
        return InstalledWorkflow.model_validate_json(row["data"]) if row else None

    async def active_for(self, workflow_ref: str, workspace_id: str = DEFAULT_WORKSPACE) -> Optional[InstalledWorkflow]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data::text AS data FROM workflow_installed "
                "WHERE ref=$1 AND workspace_id=$2 AND status='active' LIMIT 1",
                workflow_ref, workspace_id,
            )
        return InstalledWorkflow.model_validate_json(row["data"]) if row else None

    async def list_installed(self, workspace_id: Optional[str] = None) -> list[InstalledWorkflow]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if workspace_id is not None:
                rows = await conn.fetch("SELECT data::text AS data FROM workflow_installed WHERE workspace_id=$1", workspace_id)
            else:
                rows = await conn.fetch("SELECT data::text AS data FROM workflow_installed")
        return sorted((InstalledWorkflow.model_validate_json(r["data"]) for r in rows), key=lambda x: (x.workspace_id, x.id))

    async def set_workspace_records_status(self, workspace_id: str, from_status: str, to_status: str) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, data::text AS data FROM workflow_installed WHERE workspace_id=$1 AND status=$2",
                workspace_id, from_status,
            )
            for r in rows:
                rec = InstalledWorkflow.model_validate_json(r["data"])
                rec.status, rec.updated_at = to_status, _now()
                await conn.execute(
                    "UPDATE workflow_installed SET status=$1, data=$2::jsonb WHERE workspace_id=$3 AND id=$4",
                    to_status, rec.model_dump_json(), workspace_id, r["id"],
                )
        return len(rows)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False

    def close(self) -> None:  # parity with the sync stores (best-effort)
        pass


_store: object | None = None


def get_workflow_registry() -> Any:
    """Process-wide workflow registry. Honors ``CAIRN_STORAGE`` (#236):
    ``postgres://…`` → :class:`PostgresWorkflowRegistry` (fleet-shared, same DB
    as runs + workspaces); else :class:`SqliteWorkflowRegistry` (per-node;
    ``CAIRN_WORKFLOW_REGISTRY_DB`` path knob still applies). Same dispatch as
    :func:`cairn.core.storage.default_store`. Tests pass their own store in."""
    global _store
    if _store is None:
        url = getenv("CAIRN_STORAGE", "").strip()
        if url.startswith(("postgres://", "postgresql://")):
            _store = PostgresWorkflowRegistry(dsn=url)
        else:
            _store = SqliteWorkflowRegistry()
    return _store


# ── orchestration: snapshot schema + record the installed-workflow at run time ──


async def record_provenance(
    *,
    workflow_ref: str,
    input_schema: dict | None,
    output_schema: dict | None,
    required_capabilities: list[str] | None = None,
    composes: list[str] | None = None,
    pack_ref: str = "",
    version: str = "",
    pack_version: str = "",
    config: dict[str, Any] | None = None,
    template_hash: str = "",
    template_ast: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    store: Any | None = None,
) -> str:
    """Snapshot the I/O schema (dedup by hash) + upsert the versioned installed-
    workflow record (supersede the prior active for this ref if the schema
    changed). Returns the ``schema_hash`` to pin on the run. Best-effort: never
    raises into the run path — provenance must not block a run."""
    store = store or get_workflow_registry()
    ws = workspace_id or current_workspace()
    h = schema_hash(input_schema, output_schema)
    # best-effort version from the registry catalog (the pack's published version);
    # absent that, the schema hash itself is the drift discriminator.
    if not version and pack_ref:
        try:
            from cairn.core.registry_catalog import load_registry_catalog  # lazy: httpx + discovery
            for e in load_registry_catalog(annotate=False).get("entries", []):
                if e.get("ref") == pack_ref:
                    version = pack_version = str(e.get("version", ""))
                    break
        except Exception:  # noqa: BLE001
            pass
    try:
        await store.put_schema(WorkflowSchema(schema_hash=h, input_schema=input_schema or {}, output_schema=output_schema or {}))
        # store the compiled definition AST content-addressed too (#225, ADR-0049),
        # when the caller has it — the schema is the I/O half, the AST the runnable half.
        if template_hash and template_ast is not None:
            await store.put_template(WorkflowTemplate(template_hash=template_hash, ast=template_ast))
        prior = await store.active_for(workflow_ref, ws)
        # (re)write the installed record when new, or the schema OR definition changed.
        if prior is None or prior.schema_hash != h or (template_hash and prior.template_hash != template_hash):
            await store.record_installed(InstalledWorkflow(
                workflow_ref=workflow_ref, workspace_id=ws, version=version, pack_ref=pack_ref, pack_version=pack_version,
                schema_hash=h, template_hash=template_hash, required_capabilities=required_capabilities or [], composes=composes or [],
                config=config or {},
            ))
    except Exception:  # noqa: BLE001 — provenance is additive; a store hiccup mustn't fail the run
        pass
    return h


async def enable_workflow(ref: str, *, workspace_id: str | None = None, store: Any | None = None) -> bool:
    """Record one *discovered* workflow's provenance into a workspace (#258 — the
    per-workspace enable). Resolves ``ref``'s I/O schema + capabilities from the
    ``UIContract`` and its content-addressed definition from the compiler, then
    upserts via :func:`record_provenance` scoped to the workspace. Returns True if
    recorded, False if the template wasn't found / errored. This is the unit
    :func:`sync_discovered_workflows` loops over, and what the
    ``/workspaces/{id}/…/enable`` endpoint calls to populate a workspace."""
    store = store or get_workflow_registry()
    ws = workspace_id or current_workspace()
    ref = (ref or "").strip()
    if not ref:
        return False
    # lazy: cairn.ask is kept off the core import path; the engine compiler is
    # imported here to avoid an import cycle (workflow_registry ← dispatcher ← engine).
    from cairn.core.ui_contract import build_ui_contract
    from cairn.core.engine.skill_engine import resolve_template_path
    from cairn.core.engine.compiler import compile_template
    from cairn.core.engine.compiled import ast_dict
    try:
        contract = build_ui_contract(ref)
        if not contract.plan.get("template_found"):
            return False
        # the content-addressed definition (template_hash + AST), best-effort:
        # prompt/agent templates with no static graph stay schema-only.
        t_hash, t_ast = "", None
        declared: list[Any] = []
        try:
            compiled = compile_template(resolve_template_path(ref))
            t_hash, t_ast = compiled.content_hash, ast_dict(compiled.spec)
            declared = list(getattr(compiled.spec, "triggers", None) or [])
        except Exception:  # noqa: BLE001
            pass
        await record_provenance(
            workflow_ref=ref,
            input_schema=contract.inputs, output_schema=contract.outputs,
            required_capabilities=[i.capability for i in contract.integrations],
            composes=sorted({i.operator for i in contract.integrations if i.operator}),
            pack_ref=contract.pack, template_hash=t_hash, template_ast=t_ast,
            workspace_id=ws, store=store,
        )
    except Exception:  # noqa: BLE001 — enable is best-effort, never blocks
        return False

    # ADR-0101 D1 (#1973) — a trigger belongs to the WORKFLOW, so enabling the
    # workflow into a workspace is what arms it. Reported separately from the
    # registry write for the same reason enable_pipeline reports its `on:`
    # block (#1159): a bad declaration must not silently un-enable an otherwise
    # valid workflow, and it must not be swallowed either, or the author sees a
    # schedule that never fires and no reason why.
    try:
        # lazy: the compiler path above already paid for the parse; keeping the
        # trigger stores off this module's import path avoids a store handle
        # being opened by every importer of workflow_registry.
        from cairn.core.engine.triggers_grammar import (
            declared_triggers_for,
            materialise_declared_triggers,
        )

        # compile failed above (prompt-only template, missing operator, …) but
        # the front matter may still declare triggers — parse it directly.
        if not declared and t_hash == "":
            declared = declared_triggers_for(ref)
        # Runs even for a workflow that declares NOTHING: that is exactly when a
        # trigger the flow USED to declare has to stop firing (convergent, not
        # additive — the #1159 rule). The two store handles cost far less than
        # the contract build and compile above.
        res = await materialise_declared_triggers(ref, declared, workspace_id=ws)
        if res.touched:
            logger.info("armed declared triggers for %s in %s: %s",
                        ref, ws, res.model_dump(exclude_defaults=True))
    except Exception as e:  # noqa: BLE001
        logger.warning("workflow %s enabled, but its declared triggers did NOT arm: %s", ref, e)
    return True


async def active_workflow_refs(workspace_id: str | None = None, *, store: Any | None = None) -> set[str]:
    """The set of workflow refs ACTIVE in a workspace — its enabled set. Mirrors the
    ``/registry/installed-workflows`` read: ``default`` lazily auto-syncs from discovery
    if empty (#258), other workspaces reflect only their explicit enables. Returned set
    includes both full refs (``incident/incident-rca``) and short template names
    (``incident-rca``) so callers can match either form (#271). Used to scope which
    global pack triggers / scheduled jobs surface in a workspace."""
    store = store or get_workflow_registry()
    ws = workspace_id or current_workspace()
    recs = await store.list_installed(ws)
    if not recs and ws == DEFAULT_WORKSPACE:
        try:
            await sync_discovered_workflows(workspace_id=ws, store=store)
            recs = await store.list_installed(ws)
        except Exception:  # noqa: BLE001 — scoping is best-effort, never blocks the view
            pass
    refs = {r.workflow_ref for r in recs if r.status == "active"}
    return refs | {r.rsplit("/", 1)[-1] for r in refs}


async def disable_workflow(ref: str, *, workspace_id: str | None = None, store: Any | None = None) -> bool:
    """Inverse of :func:`enable_workflow` — soft-disable one workflow in a workspace
    by flipping its ``active`` record to ``disabled`` (the active listing, and thus
    the Workflows/Library UI, shows only ``active``). Reversible: re-enabling records
    a fresh ``active`` provenance via :func:`enable_workflow`. Never deletes the record
    (or any run/event/audit) — same soft-delete contract as :func:`archive_workspace`,
    but scoped to a single workflow rather than the whole workspace. Distinct status
    (``disabled`` vs ``archived``) so a workspace restore doesn't silently re-enable a
    workflow a user turned off. Returns True if a record was disabled, False if none
    was active here."""
    store = store or get_workflow_registry()
    ws = workspace_id or current_workspace()
    ref = (ref or "").strip()
    if not ref:
        return False
    rec = await store.active_for(ref, ws)
    if rec is None:
        return False
    rec.status = "disabled"
    rec.updated_at = _now()
    await store.record_installed(rec)
    return True


async def sync_discovered_workflows(*, workspace_id: str | None = None, store: Any | None = None) -> int:
    """Populate the installed-workflow registry from *discovered* templates
    (#225, ADR-0049 — the populate-on-discovery shift) by enabling each discovered
    template into the workspace. Idempotent (content-addressed → unchanged
    templates dedup); best-effort per template. Returns the count synced.

    This is how the DB becomes the system of record on a code-backed engine:
    discovery *syncs into* the DB rather than being a parallel read path. Only the
    ``default`` workspace auto-syncs (#258) — others start empty and are populated
    by the explicit per-workspace :func:`enable_workflow`."""
    store = store or get_workflow_registry()
    ws = workspace_id or current_workspace()
    from cairn.core.pack.discovery import candidate_templates  # lazy (off the core import path)
    count = 0
    for cand in candidate_templates():
        if await enable_workflow(cand.get("ref") or "", workspace_id=ws, store=store):
            count += 1
    return count


async def archive_workspace(workspace_id: str, *, store: Any | None = None) -> int:
    """Soft-delete (#258): mark all of a workspace's ``active`` installed-workflow
    records ``archived`` so they drop out of the active listing. Reversible via
    :func:`restore_workspace`. Never deletes records (or runs/events/audit).
    Returns the count flipped."""
    store = store or get_workflow_registry()
    return await store.set_workspace_records_status(workspace_id, "active", "archived")


async def restore_workspace(workspace_id: str, *, store: Any | None = None) -> int:
    """Reverse :func:`archive_workspace` — flip ``archived`` records back to
    ``active``. Returns the count restored."""
    store = store or get_workflow_registry()
    return await store.set_workspace_records_status(workspace_id, "archived", "active")


# ── pipelines (#1125, ADR-0084 D4/D9) ─────────────────────────────────


def pipeline_ref(pack: str, name: str) -> str:
    """The registry ref for a pipeline: ``pipeline:<pack>/<name>``.

    Namespaced for the same reason a flow leg is (ADR-0084 D0): a pack may hold
    both a flow and a pipeline called ``check``, and the audit trail should say
    which one ran. The prefix keeps them distinct without a schema change.
    """
    return f"pipeline:{pack}/{name}"


def _pipeline_input_schema(d: Any) -> dict[str, Any]:
    """JSON-schema-shaped inputs so the console Run form generates from the
    installed record (P8) exactly as it does for a template's UIContract."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for key, spec in (getattr(d, "inputs", None) or {}).items():
        spec = spec if isinstance(spec, dict) else {"type": spec}
        prop = {"type": str(spec.get("type", "string"))}
        if "default" in spec:
            prop["default"] = spec["default"]
        else:
            required.append(str(key))
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        props[str(key)] = prop
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


async def enable_pipeline(
    d: Any, *, workspace_id: str | None = None, store: Any | None = None,
) -> bool:
    """Install one :class:`~cairn.core.engine.pipeline.PipelineDef` into a workspace.

    Rides the SAME record as a template — content-hash identity, superseding-active
    semantics, schema snapshot — because D9 makes the pipeline the registry's
    installed unit rather than a parallel concept. The definition's
    ``content_hash`` is its ``template_hash``: a pipeline IS its content, so an
    edited pipeline is a different pipeline.
    """
    try:
        ref = pipeline_ref(getattr(d, "pack", ""), getattr(d, "name", ""))
        await record_provenance(
            workflow_ref=ref,
            input_schema=_pipeline_input_schema(d),
            output_schema={"type": "object",
                           "properties": {k: {} for k in (getattr(d, "outputs", None) or {})}},
            # what this pipeline BINDS — the console's "what's inside" answer
            composes=sorted({
                (f"flow:{s.ref}" if s.leg_kind == "flow" else s.ref)
                for s in (getattr(d, "steps", None) or [])
                if s.leg_kind in ("flow", "template", "task") and s.ref
            }),
            pack_ref=getattr(d, "pack", ""),
            template_hash=getattr(d, "content_hash", ""),
            # Identity = CONTENT (D4/D9). Without an explicit version,
            # record_provenance resolves the PACK's catalog version, and two
            # edits of one pipeline inside a pack release collapse onto a single
            # id — losing the supersede history the content-hash rule exists for.
            version=(getattr(d, "content_hash", "") or "")[7:15],
            config={"kind": "pipeline"},
            workspace_id=workspace_id, store=store,
        )
    except Exception:  # noqa: BLE001 — install is best-effort, never blocks a run
        return False

    # #1159 — arm the pipeline's declared triggers (`on:` and, since ADR-0101 S7
    # / #1987, `triggers:`). A trigger belongs to a pipeline, so install is what
    # arms it. Reported separately from the registry write: a bad declaration
    # must not silently un-install an otherwise valid pipeline, but it must also
    # not be swallowed, or the author sees a schedule that never fires and no
    # reason why.
    #
    # Runs even when the pipeline declares NOTHING — that is exactly when a
    # trigger it USED to declare has to stop firing (convergent, not additive).
    try:
        from cairn.core.engine.pipeline_triggers import (  # noqa: PLC0415 - cycle
            materialise_pipeline_triggers,
        )

        res = await materialise_pipeline_triggers(
            d, workspace_id=workspace_id or "default")
        if res.touched or res.unarmed:
            logger.info("armed triggers for pipeline %s/%s: %s",
                        getattr(d, "pack", ""), getattr(d, "name", ""),
                        res.model_dump(exclude_defaults=True))
    except Exception as e:  # noqa: BLE001
        logger.warning("pipeline %s/%s installed, but its triggers did NOT arm: %s",
                       getattr(d, "pack", ""), getattr(d, "name", ""), e)
    return True


async def sync_discovered_pipelines(
    *, workspace_id: str | None = None, store: Any | None = None,
) -> int:
    """Install every discovered pack pipeline. Returns the count installed.

    Best-effort per FILE: one unparseable pipeline is a lint problem, not an
    outage, so it never stops its neighbours from installing.
    """
    from pathlib import Path  # noqa: PLC0415 - stdlib, kept local to the lazy block

    from cairn.core.engine.pipeline import load_pipeline_def  # noqa: PLC0415 - cycle: registry ← engine

    count = 0
    for dir_path in all_pipeline_dirs():
        for yml in sorted(Path(dir_path).glob("*.yaml")):
            try:
                d = load_pipeline_def(yml)
            except Exception:  # noqa: BLE001 — a bad file must not stop the sync
                logger.warning("skipping unparseable pipeline %s", yml)
                continue
            if await enable_pipeline(d, workspace_id=workspace_id, store=store):
                count += 1
    return count
