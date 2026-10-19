"""Saved per-workflow config (ADR-0070, #748 slice 2).

A :class:`WorkflowConfigRecord` is the **operational** config a user saved for a
workflow — the ``operational``-scoped knobs across its operators (governance knobs
stay env-owned, ADR-0070). Keyed by ``(workspace_id, template_ref)`` — one saved
config per workflow per workspace. ``values`` is the config-overrides shape
(``{operator: {knob: value}}``) so it drops straight into the operator-config
precedence stack as the *workflow* layer (below a run override, above the env
operational default) once run-time wiring lands (next slice).

Mirrors :class:`~cairn.core.profile_store` (JSON blob, in-memory / sqlite).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.pg_schema_init import PgSchemaInit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(workspace_id: str, template_ref: str) -> str:
    return f"{workspace_id or 'default'}:{template_ref}"


def _pkey(workspace_id: str, template_ref: str, name: str) -> str:
    """Preset primary key — (workspace, workflow, name). Names are constrained to
    a safe slug at the API edge, so ``:`` never appears inside a segment."""
    return f"{workspace_id or 'default'}:{template_ref}:{name}"


class WorkflowConfigRecord(BaseModel):
    """A saved operational config for one workflow, one workspace."""

    template_ref: str
    workspace_id: str = "default"
    # config-overrides shape: {operator: {knob-or-nested: value}} — OPERATIONAL only.
    values: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    updated_at: Optional[str] = None

    @property
    def id(self) -> str:
        return _key(self.workspace_id, self.template_ref)


class WorkflowPreset(BaseModel):
    """A named, versioned config preset for one workflow, one workspace (ADR-0071,
    #787). ``values`` is the SAME config-overrides shape as
    :class:`WorkflowConfigRecord` and is **operational-only** — the save path
    rejects governance knobs (env-owned, ADR-0068/0070). One preset per (workspace,
    workflow) may be ``is_default``; the dispatcher applies the default to every run
    (incl. triggers) with no run-form (#788)."""

    name: str
    template_ref: str
    workspace_id: str = "default"
    values: dict[str, dict[str, Any]] = Field(default_factory=dict)
    version: int = 1
    is_default: bool = False
    created_at: str = Field(default_factory=_now)
    created_by: str = ""
    updated_at: Optional[str] = None

    @property
    def id(self) -> str:
        return _pkey(self.workspace_id, self.template_ref, self.name)


def _bump(existing: Optional[WorkflowPreset], incoming: WorkflowPreset) -> WorkflowPreset:
    """Merge an incoming preset over an existing one on save: bump the version,
    preserve creation provenance, stamp ``updated_at``. A brand-new preset keeps
    version 1 and its own ``created_at``/``created_by``."""
    if existing is None:
        return incoming.model_copy(update={"version": 1, "updated_at": _now()})
    return incoming.model_copy(update={
        "version": existing.version + 1,
        "created_at": existing.created_at,
        "created_by": existing.created_by or incoming.created_by,
        "updated_at": _now(),
    })


class InputPreset(BaseModel):
    """A named set of INPUT values (ADR-0071, #793) — the user-savable
    generalization of a pack's ``[demo]`` block. ``inputs`` is the same flat
    input-values shape the Run form submits (already coerced against the input
    schema at save). Distinct from :class:`WorkflowPreset` (operator CONFIG); kept
    in its own table so the config-preset surface is untouched. No governance and
    no default — inputs aren't scoped, and picking one is always explicit.

    ``template_ref`` is the thing the preset belongs to, and #1789 widened what
    that may be: a workflow template ref (``demo/finetune``) as before, OR a
    PIPELINE ref in its canonical ``pipeline:<pack>/<name>`` spelling. One table,
    one key shape, one CRUD surface — the ``pipeline:`` prefix is already the
    engine's disambiguator (``PIPELINE_PREFIX``, used verbatim as a launch
    record's ``bundle_ref``), so the two namespaces cannot collide and no
    ``kind`` discriminator is needed.
    """

    name: str
    template_ref: str
    workspace_id: str = "default"
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: #1789 — free text for the picker ("what is this preset for"). Pack-shipped
    #: seed presets carry theirs from the YAML; a saved preset may set one.
    description: str = ""
    created_at: str = Field(default_factory=_now)
    created_by: str = ""
    updated_at: Optional[str] = None
    #: #1789 — stamped by the launch door each time this preset resolves a launch,
    #: so the listing can answer "which of these is still in use". Absent (None)
    #: on a preset that has never launched anything; rows written before this
    #: field deserialize to None and behave exactly as they did.
    last_used_at: Optional[str] = None

    @property
    def id(self) -> str:
        return _pkey(self.workspace_id, self.template_ref, self.name)


class InMemoryWorkflowConfigStore:
    def __init__(self) -> None:
        self._by_key: dict[str, WorkflowConfigRecord] = {}
        self._presets: dict[str, WorkflowPreset] = {}
        self._input_presets: dict[str, InputPreset] = {}

    async def get(self, workspace_id: str, template_ref: str) -> Optional[WorkflowConfigRecord]:
        return self._by_key.get(_key(workspace_id, template_ref))

    async def put(self, rec: WorkflowConfigRecord) -> WorkflowConfigRecord:
        rec = rec.model_copy(update={"updated_at": _now()})
        self._by_key[rec.id] = rec
        return rec

    async def delete(self, workspace_id: str, template_ref: str) -> bool:
        return self._by_key.pop(_key(workspace_id, template_ref), None) is not None

    # ── presets (#787) ──
    async def list_presets(self, workspace_id: str, template_ref: str) -> list[WorkflowPreset]:
        wid = workspace_id or "default"
        return sorted(
            [p for p in self._presets.values() if p.workspace_id == wid and p.template_ref == template_ref],
            key=lambda p: p.name,
        )

    async def get_preset(self, workspace_id: str, template_ref: str, name: str) -> Optional[WorkflowPreset]:
        return self._presets.get(_pkey(workspace_id, template_ref, name))

    async def put_preset(self, preset: WorkflowPreset) -> WorkflowPreset:
        merged = _bump(self._presets.get(preset.id), preset)
        self._presets[merged.id] = merged
        return merged

    async def delete_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        return self._presets.pop(_pkey(workspace_id, template_ref, name), None) is not None

    async def set_default_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        wid = workspace_id or "default"
        if _pkey(workspace_id, template_ref, name) not in self._presets:
            return False
        for p in list(self._presets.values()):
            if p.workspace_id == wid and p.template_ref == template_ref:
                self._presets[p.id] = p.model_copy(update={"is_default": p.name == name})
        return True

    # ── input presets (#793) ──
    async def list_input_presets(self, workspace_id: str, template_ref: str) -> list[InputPreset]:
        wid = workspace_id or "default"
        return sorted(
            [p for p in self._input_presets.values() if p.workspace_id == wid and p.template_ref == template_ref],
            key=lambda p: p.name,
        )

    async def get_input_preset(self, workspace_id: str, template_ref: str, name: str) -> Optional[InputPreset]:
        return self._input_presets.get(_pkey(workspace_id, template_ref, name))

    async def put_input_preset(self, preset: InputPreset) -> InputPreset:
        preset = preset.model_copy(update={"updated_at": _now()})
        self._input_presets[preset.id] = preset
        return preset

    async def delete_input_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        return self._input_presets.pop(_pkey(workspace_id, template_ref, name), None) is not None

    def close(self) -> None:
        pass


class SqliteWorkflowConfigStore:
    """JSON-blob sqlite store, ``to_thread``'d (mirrors SqliteProfileStore)."""

    def __init__(self, path: str | None = None) -> None:
        import sqlite3  # noqa: PLC0415 — stdlib, lazy like the sibling stores

        if path is None:
            from cairn.core import settings  # noqa: PLC0415

            path = getenv("CAIRN_WORKFLOW_CONFIG_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS workflow_configs "
            "(id TEXT PRIMARY KEY, workspace_id TEXT, template_ref TEXT, data TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS workflow_presets "
            "(id TEXT PRIMARY KEY, workspace_id TEXT, template_ref TEXT, name TEXT, data TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS input_presets "
            "(id TEXT PRIMARY KEY, workspace_id TEXT, template_ref TEXT, name TEXT, data TEXT NOT NULL)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _put_sync(self, rec: WorkflowConfigRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO workflow_configs (id, workspace_id, template_ref, data) VALUES (?,?,?,?)",
            (rec.id, rec.workspace_id, rec.template_ref, rec.model_dump_json()),
        )
        self._conn.commit()

    async def put(self, rec: WorkflowConfigRecord) -> WorkflowConfigRecord:
        rec = rec.model_copy(update={"updated_at": _now()})
        async with self._lock:
            await asyncio.to_thread(self._put_sync, rec)
        return rec

    def _get_sync(self, wid: str, ref: str) -> Optional[WorkflowConfigRecord]:
        row = self._conn.execute(
            "SELECT data FROM workflow_configs WHERE id=?", (_key(wid, ref),)
        ).fetchone()
        return WorkflowConfigRecord.model_validate_json(row[0]) if row else None

    async def get(self, workspace_id: str, template_ref: str) -> Optional[WorkflowConfigRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, workspace_id, template_ref)

    def _delete_sync(self, wid: str, ref: str) -> bool:
        cur = self._conn.execute("DELETE FROM workflow_configs WHERE id=?", (_key(wid, ref),))
        self._conn.commit()
        return cur.rowcount > 0

    async def delete(self, workspace_id: str, template_ref: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, workspace_id, template_ref)

    # ── presets (#787) ──
    _P_UPSERT = (
        "INSERT OR REPLACE INTO workflow_presets (id, workspace_id, template_ref, name, data) "
        "VALUES (?,?,?,?,?)"
    )

    def _write_preset_sync(self, p: WorkflowPreset) -> None:
        self._conn.execute(self._P_UPSERT, (p.id, p.workspace_id or "default", p.template_ref, p.name, p.model_dump_json()))

    def _list_presets_sync(self, wid: str, ref: str) -> list[WorkflowPreset]:
        rows = self._conn.execute(
            "SELECT data FROM workflow_presets WHERE workspace_id=? AND template_ref=? ORDER BY name",
            (wid or "default", ref),
        ).fetchall()
        return [WorkflowPreset.model_validate_json(r[0]) for r in rows]

    def _get_preset_sync(self, wid: str, ref: str, name: str) -> Optional[WorkflowPreset]:
        row = self._conn.execute(
            "SELECT data FROM workflow_presets WHERE id=?", (_pkey(wid, ref, name),)
        ).fetchone()
        return WorkflowPreset.model_validate_json(row[0]) if row else None

    def _put_preset_sync(self, preset: WorkflowPreset) -> WorkflowPreset:
        existing = self._get_preset_sync(preset.workspace_id, preset.template_ref, preset.name)
        merged = _bump(existing, preset)
        self._write_preset_sync(merged)
        self._conn.commit()
        return merged

    def _set_default_sync(self, wid: str, ref: str, name: str) -> bool:
        presets = self._list_presets_sync(wid, ref)
        if not any(p.name == name for p in presets):
            return False
        for p in presets:
            self._write_preset_sync(p.model_copy(update={"is_default": p.name == name}))
        self._conn.commit()
        return True

    async def list_presets(self, workspace_id: str, template_ref: str) -> list[WorkflowPreset]:
        async with self._lock:
            return await asyncio.to_thread(self._list_presets_sync, workspace_id, template_ref)

    async def get_preset(self, workspace_id: str, template_ref: str, name: str) -> Optional[WorkflowPreset]:
        async with self._lock:
            return await asyncio.to_thread(self._get_preset_sync, workspace_id, template_ref, name)

    async def put_preset(self, preset: WorkflowPreset) -> WorkflowPreset:
        async with self._lock:
            return await asyncio.to_thread(self._put_preset_sync, preset)

    async def delete_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        def _d() -> bool:
            cur = self._conn.execute("DELETE FROM workflow_presets WHERE id=?", (_pkey(workspace_id, template_ref, name),))
            self._conn.commit()
            return cur.rowcount > 0
        async with self._lock:
            return await asyncio.to_thread(_d)

    async def set_default_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._set_default_sync, workspace_id, template_ref, name)

    # ── input presets (#793) ──
    async def list_input_presets(self, workspace_id: str, template_ref: str) -> list[InputPreset]:
        def _l() -> list[InputPreset]:
            rows = self._conn.execute(
                "SELECT data FROM input_presets WHERE workspace_id=? AND template_ref=? ORDER BY name",
                (workspace_id or "default", template_ref),
            ).fetchall()
            return [InputPreset.model_validate_json(r[0]) for r in rows]
        async with self._lock:
            return await asyncio.to_thread(_l)

    async def get_input_preset(self, workspace_id: str, template_ref: str, name: str) -> Optional[InputPreset]:
        def _g() -> Optional[InputPreset]:
            row = self._conn.execute(
                "SELECT data FROM input_presets WHERE id=?", (_pkey(workspace_id, template_ref, name),)
            ).fetchone()
            return InputPreset.model_validate_json(row[0]) if row else None
        async with self._lock:
            return await asyncio.to_thread(_g)

    async def put_input_preset(self, preset: InputPreset) -> InputPreset:
        preset = preset.model_copy(update={"updated_at": _now()})
        def _w() -> None:
            self._conn.execute(
                "INSERT OR REPLACE INTO input_presets (id, workspace_id, template_ref, name, data) VALUES (?,?,?,?,?)",
                (preset.id, preset.workspace_id or "default", preset.template_ref, preset.name, preset.model_dump_json()),
            )
            self._conn.commit()
        async with self._lock:
            await asyncio.to_thread(_w)
        return preset

    async def delete_input_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        def _d() -> bool:
            cur = self._conn.execute("DELETE FROM input_presets WHERE id=?", (_pkey(workspace_id, template_ref, name),))
            self._conn.commit()
            return cur.rowcount > 0
        async with self._lock:
            return await asyncio.to_thread(_d)

    def close(self) -> None:
        self._conn.close()


class PostgresWorkflowConfigStore(PgSchemaInit):
    """Postgres-backed store — mirrors :class:`SqliteWorkflowConfigStore` (full
    record in a JSONB ``data`` column) and :class:`PostgresProfileStore` (lazy
    asyncpg pool). Selected when ``CAIRN_STORAGE=postgres://…`` so saved workflow
    config lives in the one prod Postgres, not a per-container sqlite file (#427)."""

    _CREATE = (
        "CREATE TABLE IF NOT EXISTS workflow_configs ("
        " id TEXT PRIMARY KEY, workspace_id TEXT, template_ref TEXT, data JSONB NOT NULL)"
    )
    _P_CREATE = (
        "CREATE TABLE IF NOT EXISTS workflow_presets ("
        " id TEXT PRIMARY KEY, workspace_id TEXT, template_ref TEXT, name TEXT, data JSONB NOT NULL)"
    )
    _P_UPSERT = (
        "INSERT INTO workflow_presets (id, workspace_id, template_ref, name, data) VALUES ($1,$2,$3,$4,$5::jsonb)"
        " ON CONFLICT (id) DO UPDATE SET workspace_id=EXCLUDED.workspace_id,"
        " template_ref=EXCLUDED.template_ref, name=EXCLUDED.name, data=EXCLUDED.data"
    )
    _P_LIST = "SELECT data FROM workflow_presets WHERE workspace_id=$1 AND template_ref=$2 ORDER BY name"
    _P_GET = "SELECT data FROM workflow_presets WHERE id=$1"
    _P_DELETE = "DELETE FROM workflow_presets WHERE id=$1"
    _IP_CREATE = (
        "CREATE TABLE IF NOT EXISTS input_presets ("
        " id TEXT PRIMARY KEY, workspace_id TEXT, template_ref TEXT, name TEXT, data JSONB NOT NULL)"
    )
    _IP_UPSERT = (
        "INSERT INTO input_presets (id, workspace_id, template_ref, name, data) VALUES ($1,$2,$3,$4,$5::jsonb)"
        " ON CONFLICT (id) DO UPDATE SET workspace_id=EXCLUDED.workspace_id,"
        " template_ref=EXCLUDED.template_ref, name=EXCLUDED.name, data=EXCLUDED.data"
    )
    _IP_LIST = "SELECT data FROM input_presets WHERE workspace_id=$1 AND template_ref=$2 ORDER BY name"
    _IP_GET = "SELECT data FROM input_presets WHERE id=$1"
    _IP_DELETE = "DELETE FROM input_presets WHERE id=$1"
    _UPSERT = (
        "INSERT INTO workflow_configs (id, workspace_id, template_ref, data) VALUES ($1,$2,$3,$4::jsonb)"
        " ON CONFLICT (id) DO UPDATE SET workspace_id=EXCLUDED.workspace_id,"
        " template_ref=EXCLUDED.template_ref, data=EXCLUDED.data"
    )
    _SELECT = "SELECT data FROM workflow_configs WHERE id=$1"
    _DELETE = "DELETE FROM workflow_configs WHERE id=$1"

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    # #1833 — the pool used to be published BEFORE the schema statements ran, so
    # the second of two concurrent first requests saw a pool and queried a table
    # that did not exist yet: on a fresh Postgres the console's first paint
    # answered two 500s (`relation "input_presets" does not exist`) and never
    # again. This store's hand-rolled fix is now :class:`PgSchemaInit`, shared by
    # the whole family (#1904) — it owns the lock, the `_schema_ready` guard, and
    # the rule that `self._pool` is assigned only once the tables are there.

    async def _open_pool(self):
        try:
            import asyncpg  # noqa: F401, PLC0415 — optional dep (storage-postgres)
        except ImportError as e:
            raise RuntimeError(
                "Postgres workflow-config store requires the 'storage-postgres' optional dep."
            ) from e
        if not self._dsn:
            raise RuntimeError("PostgresWorkflowConfigStore needs a DSN (CAIRN_STORAGE=postgres://…).")
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(self._CREATE)
            await c.execute(self._P_CREATE)
            await c.execute(self._IP_CREATE)

    async def get(self, workspace_id: str, template_ref: str) -> Optional[WorkflowConfigRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(self._SELECT, _key(workspace_id, template_ref))
        return WorkflowConfigRecord.model_validate_json(row["data"]) if row else None

    async def put(self, rec: WorkflowConfigRecord) -> WorkflowConfigRecord:
        rec = rec.model_copy(update={"updated_at": _now()})
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(self._UPSERT, rec.id, rec.workspace_id, rec.template_ref, rec.model_dump_json())
        return rec

    async def delete(self, workspace_id: str, template_ref: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(self._DELETE, _key(workspace_id, template_ref))
        return status.split()[-1] != "0"  # asyncpg returns e.g. "DELETE 1"

    # ── presets (#787) ──
    async def list_presets(self, workspace_id: str, template_ref: str) -> list[WorkflowPreset]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(self._P_LIST, workspace_id or "default", template_ref)
        return [WorkflowPreset.model_validate_json(r["data"]) for r in rows]

    async def get_preset(self, workspace_id: str, template_ref: str, name: str) -> Optional[WorkflowPreset]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(self._P_GET, _pkey(workspace_id, template_ref, name))
        return WorkflowPreset.model_validate_json(row["data"]) if row else None

    async def put_preset(self, preset: WorkflowPreset) -> WorkflowPreset:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(self._P_GET, preset.id)
                existing = WorkflowPreset.model_validate_json(row["data"]) if row else None
                merged = _bump(existing, preset)
                await c.execute(self._P_UPSERT, merged.id, merged.workspace_id or "default",
                                merged.template_ref, merged.name, merged.model_dump_json())
        return merged

    async def delete_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(self._P_DELETE, _pkey(workspace_id, template_ref, name))
        return status.split()[-1] != "0"

    async def set_default_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            async with c.transaction():
                rows = await c.fetch(self._P_LIST, workspace_id or "default", template_ref)
                presets = [WorkflowPreset.model_validate_json(r["data"]) for r in rows]
                if not any(p.name == name for p in presets):
                    return False
                for p in presets:
                    upd = p.model_copy(update={"is_default": p.name == name})
                    await c.execute(self._P_UPSERT, upd.id, upd.workspace_id or "default",
                                    upd.template_ref, upd.name, upd.model_dump_json())
        return True

    # ── input presets (#793) ──
    async def list_input_presets(self, workspace_id: str, template_ref: str) -> list[InputPreset]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(self._IP_LIST, workspace_id or "default", template_ref)
        return [InputPreset.model_validate_json(r["data"]) for r in rows]

    async def get_input_preset(self, workspace_id: str, template_ref: str, name: str) -> Optional[InputPreset]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(self._IP_GET, _pkey(workspace_id, template_ref, name))
        return InputPreset.model_validate_json(row["data"]) if row else None

    async def put_input_preset(self, preset: InputPreset) -> InputPreset:
        preset = preset.model_copy(update={"updated_at": _now()})
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(self._IP_UPSERT, preset.id, preset.workspace_id or "default",
                            preset.template_ref, preset.name, preset.model_dump_json())
        return preset

    async def delete_input_preset(self, workspace_id: str, template_ref: str, name: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            status = await c.execute(self._IP_DELETE, _pkey(workspace_id, template_ref, name))
        return status.split()[-1] != "0"

    def close(self) -> None:
        pass  # pool lives for the process (parity with the sibling stores)


_default_store: Any = None


def default_workflow_config_store() -> Any:
    """Process-wide store singleton — mirrors the other console stores. **`CAIRN_STORAGE`
    is the selector** (#427): ``postgres://…`` → Postgres (the one prod DB); else the
    shared cairn.db sqlite. Swappable for the in-memory store in tests."""
    global _default_store
    if _default_store is None:
        url = getenv("CAIRN_STORAGE", "").strip()
        if url.startswith(("postgres://", "postgresql://")):
            _default_store = PostgresWorkflowConfigStore(dsn=url)
        else:
            _default_store = SqliteWorkflowConfigStore()
    return _default_store


def _coerce_knob(value: Any, default: Any) -> tuple[Any, bool]:
    """Coerce a saved config value to the type the operator's default implies
    (ADR-0070 #865). Consoles/CLIs pass strings, so a ``list[str]`` knob saved as
    a CSV string ("trl,unsloth") is coerced to ``["trl", "unsloth"]`` rather than
    silently persisted as a string that later blows up the operator at run time.
    Returns ``(coerced, ok)``; ``ok=False`` on a mismatch nothing can salvage.
    """
    if default is None or value is None:
        return value, True  # no type to coerce toward
    # bool first — it's a subclass of int
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", True
        return value, False
    if isinstance(default, list):
        if isinstance(value, list):
            return value, True
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()], True
        return value, False
    if isinstance(default, (int, float)):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value, True
        if isinstance(value, str):
            try:
                return (int(value) if isinstance(default, int) else float(value)), True
            except ValueError:
                return value, False
        return value, False
    if isinstance(default, str):
        return value if isinstance(value, str) else str(value), True
    return value, type(default) is type(value)


def split_operational_governance(
    template_ref: str, values: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Split a proposed workflow-config ``values`` into the ``operational`` part
    that may be saved and a list of **rejected** paths (named, with the reason).
    Uses the composed surface to classify each knob by scope. Rejected =
    governance knobs (env-owned, ADR-0070), knobs not in the workflow's config
    surface, and values whose type can't be coerced to the operator's schema
    (#865 — a fail-closed guard so a malformed knob can't silently break a run).
    Coercible console/CLI values (e.g. a CSV string for a ``list[str]`` knob) are
    coerced to the schema type before saving.
    """
    from cairn.core.engine.workflow_config import compose_workflow_config  # noqa: PLC0415

    surface = compose_workflow_config(template_ref)
    allowed: dict[str, set[str]] = {}
    governance: dict[str, set[str]] = {}
    defaults: dict[str, dict[str, Any]] = {}
    for op, part in surface.items():
        allowed[op] = {k["path"] for k in part.get("operational", [])}
        governance[op] = {k["path"] for k in part.get("governance", [])}
        defaults[op] = {k["path"]: k.get("default") for k in part.get("operational", [])}

    kept: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for op, knobs in (values or {}).items():
        for path, val in _flatten(knobs):
            if op in governance and path in governance[op]:
                rejected.append(f"{op}.{path} (governance — env-owned)")
            elif op in allowed and path in allowed[op]:
                coerced, ok = _coerce_knob(val, defaults.get(op, {}).get(path))
                if not ok:
                    want = type(defaults.get(op, {}).get(path)).__name__
                    rejected.append(f"{op}.{path} (type mismatch — expected {want}, got {type(val).__name__})")
                    continue
                kept.setdefault(op, {})
                _set_nested(kept[op], path, coerced)
            else:
                rejected.append(f"{op}.{path} (not an operational knob of this workflow)")
    return kept, rejected


def _flatten(d: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for k, v in (d or {}).items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            out.extend(_flatten(v, f"{path}."))
        else:
            out.append((path, v))
    return out


def _set_nested(d: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = d
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


class PresetValidationError(ValueError):
    """A preset carried knobs that can't be saved operationally (governance —
    env-owned — or not a knob of this workflow). Raised **fail-closed** by
    :func:`save_preset` so a governance knob can never be smuggled in through a
    preset (ADR-0068/0070/0071). Carries the rejected paths for the caller (→ 400)."""

    def __init__(self, rejected: list[str]):
        self.rejected = list(rejected)
        super().__init__("preset contains non-operational knobs: " + "; ".join(self.rejected))


async def save_preset(
    *,
    workspace_id: str,
    template_ref: str,
    name: str,
    values: dict[str, dict[str, Any]],
    created_by: str = "",
    make_default: bool = False,
    store: Any = None,
) -> WorkflowPreset:
    """Governed preset save (#787): split the proposed ``values`` into operational
    vs governance via :func:`split_operational_governance`; **raise
    :class:`PresetValidationError` fail-closed** if ANY knob is non-operational
    (governance or not-a-knob), then persist the operational remainder as a named,
    versioned preset. Optionally mark it the workflow's default in the same call."""
    kept, rejected = split_operational_governance(template_ref, values or {})
    if rejected:
        raise PresetValidationError(rejected)
    store = store or default_workflow_config_store()
    preset = WorkflowPreset(
        name=name, template_ref=template_ref, workspace_id=workspace_id or "default",
        values=kept, created_by=created_by, is_default=bool(make_default),
    )
    saved = await store.put_preset(preset)
    if make_default:
        await store.set_default_preset(workspace_id, template_ref, name)
        saved = await store.get_preset(workspace_id, template_ref, name) or saved
    return saved


async def default_preset(workspace_id: str, template_ref: str, store: Any = None) -> Optional[WorkflowPreset]:
    """The workflow's marked-default preset for a workspace, or ``None`` (#788 will
    apply this to every run — incl. triggers — with no run-form)."""
    store = store or default_workflow_config_store()
    for p in await store.list_presets(workspace_id, template_ref):
        if p.is_default:
            return p
    return None
