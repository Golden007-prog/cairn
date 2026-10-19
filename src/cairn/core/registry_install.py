"""Server-side pack install — the v1 *write* half of the registry (#211, ADR-0045).

v1 scope (ADR-0045, Accepted): install a pack from the registry into the engine's
**shared env** via ``uv pip install``, record the lifecycle in a DB install-record
(discovery stays the source of truth for *existence* — this records the
console-initiated install), validate it out-of-process, and surface real status.
**Restart-to-discover**: the freshly-installed pack activates on the next engine
start (no hot-reload in v1).

Isolation: shared env; a conflicting/heavy pack is *flagged* for a container
target (not auto-built in v1). Security: a **source allowlist** — the registry
catalog is the allowlist for official refs; ``CAIRN_REGISTRY_ALLOWLIST`` (comma-
separated prefixes) admits manual refs. Installing arbitrary packages on the
engine is RCE-class, so the route is auth-gated and only allowed refs proceed.

Deferred (ADR-0045): OCI materialization, capability version-solving, auto
container build on conflict, hot-reload, signature verification.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import os
import subprocess
import sys
from typing import Callable, Optional

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import PgSchemaInit

#: lifecycle, in order. Terminal: installed | removed | failed.
STATUSES = ("requested", "installing", "validating", "installed", "removing", "removed", "failed")


class InstallRecord(BaseModel):
    """One console-initiated install + its lifecycle. Reconciled against live
    discovery by the reader (this records *what was asked for*, not the truth of
    what's importable)."""

    id: str = ""  # == ref
    ref: str
    kind: str = ""  # operator_pack | template_pack | ""
    version: str = ""
    source: str = ""  # catalog | manual
    status: str = "requested"
    actor: str = "console"
    detail: str = ""  # human note / error / "restart to activate"
    #: Content hash of the validated pack (dist name+version+entry-points), set on
    #: a successful deep validation (ADR-0055 §5). "" until validated / when an
    #: injected validator returns no hash. Pack-install provenance — distinct from
    #: the run-time template_hash.
    content_hash: str = ""
    needs_restart: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    def model_post_init(self, _ctx: object) -> None:  # pydantic v2 hook
        if not self.id:
            object.__setattr__(self, "id", self.ref)

    def _to(self, status: str, detail: str = "") -> "InstallRecord":
        self.status = status
        if detail:
            self.detail = detail
        self.updated_at = utc_now_iso()
        return self


# ── stores (mirror cairn/core/triggers/store.py exactly) ──────────────


class InMemoryInstallStore:
    """Volatile store (tests / no-config default)."""

    def __init__(self) -> None:
        self._by_id: dict[str, InstallRecord] = {}

    async def upsert(self, r: InstallRecord) -> InstallRecord:
        self._by_id[r.id] = r
        return r

    async def get(self, rid: str) -> Optional[InstallRecord]:
        return self._by_id.get(rid)

    async def list(self) -> list[InstallRecord]:
        return sorted(self._by_id.values(), key=lambda r: r.ref)

    def close(self) -> None:
        pass


class SqliteInstallStore:
    """Embedded-SQLite store — restart-safe, single file; the record serializes
    to a JSON ``data`` blob, async ops run via ``asyncio.to_thread`` under a lock.
    Mirrors :class:`~cairn.core.storage.trigger_store.SqliteTriggerStore`."""

    def __init__(self, path: str | None = None) -> None:
        import sqlite3  # stdlib, but the connection is an instance handle

        if path is None:
            from cairn.core import settings

            # single shared cairn.db (#427/ADR-0058); CAIRN_*_DB overrides
            path = getenv("CAIRN_INSTALLS_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS installs (id TEXT PRIMARY KEY, ref TEXT, status TEXT, data TEXT NOT NULL)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _upsert_sync(self, r: InstallRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO installs (id, ref, status, data) VALUES (?,?,?,?)",
            (r.id, r.ref, r.status, r.model_dump_json()),
        )
        self._conn.commit()

    async def upsert(self, r: InstallRecord) -> InstallRecord:
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, r)
        return r

    async def get(self, rid: str) -> Optional[InstallRecord]:
        row = self._conn.execute("SELECT data FROM installs WHERE id=?", (rid,)).fetchone()
        return InstallRecord.model_validate_json(row[0]) if row else None

    async def list(self) -> list[InstallRecord]:
        rows = self._conn.execute("SELECT data FROM installs").fetchall()
        return sorted((InstallRecord.model_validate_json(r[0]) for r in rows), key=lambda x: x.ref)

    def close(self) -> None:
        self._conn.close()


# ── Postgres backend (mirrors storage/postgres/runs.py) — #427 ─────────────
_CREATE_INSTALLS_PG_SQL = """
    CREATE TABLE IF NOT EXISTS installs (
        id     TEXT PRIMARY KEY,
        ref    TEXT,
        status TEXT,
        data   JSONB NOT NULL
    )
"""
_UPSERT_INSTALL_PG_SQL = """
    INSERT INTO installs (id, ref, status, data)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (id) DO UPDATE
    SET ref = EXCLUDED.ref, status = EXCLUDED.status, data = EXCLUDED.data
"""
_SELECT_INSTALL_PG_BY_ID = "SELECT data::text FROM installs WHERE id = $1"
_LIST_INSTALLS_PG_SQL = "SELECT data::text FROM installs"


class PostgresInstallStore(PgSchemaInit):
    """Postgres-backed install store — mirrors :class:`SqliteInstallStore` (full
    :class:`InstallRecord` in a JSONB ``data`` column) and
    :class:`~cairn.core.storage.postgres.runs.PostgresRunStore` (lazy asyncpg
    pool). Selected by :func:`get_install_store` when ``CAIRN_STORAGE=postgres://…``
    so console-initiated install records live in the one prod Postgres instead of
    a per-container SQLite file (#427)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool = None
        self._schema_ready = False

    async def _open_pool(self):
        # Missing DEP before missing CONFIG (#1184) — parity with
        # PostgresWorkspaceStore; shared_pool would ModuleNotFoundError anyway.
        try:
            import asyncpg  # noqa: F401, PLC0415 — optional dep (storage-postgres extra)
        except ImportError as e:
            raise RuntimeError(
                "Postgres install store requires the 'storage-postgres' optional "
                "dep group. Install with: pip install 'cairndev[storage-postgres]'"
            ) from e
        if not self._dsn:
            raise RuntimeError(
                "PostgresInstallStore needs a DSN. Pass dsn= or set CAIRN_STORAGE=postgres://…"
            )
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415

        # #614: one pool per DSN, shared across every Postgres store in this process.
        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool) -> None:
        async with pool.acquire() as c:
            await c.execute(_CREATE_INSTALLS_PG_SQL)

    async def upsert(self, r: InstallRecord) -> InstallRecord:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.execute(_UPSERT_INSTALL_PG_SQL, r.id, r.ref, r.status, r.model_dump_json())
        return r

    async def get(self, rid: str) -> Optional[InstallRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            row = await c.fetchrow(_SELECT_INSTALL_PG_BY_ID, rid)
        return InstallRecord.model_validate_json(row["data"]) if row else None

    async def list(self) -> list[InstallRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(_LIST_INSTALLS_PG_SQL)
        return sorted((InstallRecord.model_validate_json(r["data"]) for r in rows), key=lambda x: x.ref)

    def close(self) -> None:
        pass  # pool lives for the process (parity with SqliteInstallStore.close)


_store: object | None = None


def get_install_store() -> object:
    """Process-wide install store. **`CAIRN_STORAGE` is the selector** (#427):
    ``postgres://…`` → :class:`PostgresInstallStore` (the one prod Postgres),
    else SQLite. Tests pass their own to install_pack."""
    global _store
    if _store is None:
        url = getenv("CAIRN_STORAGE", "").strip()
        if url.startswith(("postgres://", "postgresql://")):
            _store = PostgresInstallStore(dsn=url)
        else:
            _store = SqliteInstallStore()
    return _store


# ── allowlist + the default (real) installer / validator ──────────────


def _catalog_index() -> dict[str, dict]:
    """ref / install-name → catalog entry. The catalog is the allowlist + the
    metadata (kind/version/install-name) source for an install."""
    from cairn.core.registry_catalog import load_registry_catalog  # lazy: pulls httpx + discovery

    idx: dict[str, dict] = {}
    for e in load_registry_catalog(annotate=False).get("entries", []):
        idx[e["ref"]] = e
        name = (e.get("install") or {}).get("name")
        if name:
            idx[name] = e
    return idx


def _env_allowlist() -> list[str]:
    raw = getenv("CAIRN_REGISTRY_ALLOWLIST", "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def is_allowed(ref: str, catalog_idx: dict[str, dict]) -> bool:
    """A ref is installable iff it's in the registry catalog or matches an
    CAIRN_REGISTRY_ALLOWLIST prefix. (The catalog *is* the allowlist for official.)"""
    return ref in catalog_idx or any(ref.startswith(p) for p in _env_allowlist())


class PackInstallUnsupportedError(RuntimeError):
    """Runtime pack installation is not a supported posture (#1545 D2).

    Raised by the DEFAULT installer/uninstaller so the door refuses honestly
    instead of appearing to work. The route maps it to 501.
    """


#: The posture, stated once and surfaced verbatim to whoever called the door.
_POSTURE = (
    "installing a pack into a running control plane is not supported "
    "(#1545 D2, provisional). Packs enter production through the image: "
    "`cairn pack add <dir>` → CI → image rebuild → redeploy. This is a "
    "deliberate governance stance, not a missing feature — an immutable image "
    "means every run's code surface is pinned and auditable, and hot-loading "
    "third-party code into the process that enforces approval gates is a "
    "supply-chain risk the product exists to argue against. Locally, use "
    "CAIRN_PACK_PATH for a zero-install dev loop. To revisit this, see #1545."
)


def _uv_install(install_name: str) -> None:
    """The DEFAULT installer: refuse, loudly, with the posture (#1545 D2).

    This used to shell out to ``uv pip install`` into the engine env. It could
    never have worked in production: ``Dockerfile.engine`` copies only
    ``/app/.venv`` out of the builder stage, so ``uv`` is not in the runtime
    image — and even on success it wrote into a container filesystem that
    restart-to-discover then destroyed. The 17 lifecycle tests stayed green
    throughout because every one of them injects its own ``installer=``, so
    they exercised the bookkeeping and never this.

    The orchestration around it (allowlist → record → validate → rollback) is
    deliberately KEPT and still tested: if the posture is revisited, that
    lifecycle is the reusable half. What is not kept is this subprocess call,
    because a real option-A installer needs signed wheels and a recorded
    digest — restoring these two lines would not be the fix.
    """
    raise PackInstallUnsupportedError(_POSTURE)


# Deep validation script, run in a FRESH interpreter (restart-to-discover: the
# running engine hasn't imported the new dist yet). It (1) confirms the dist is
# installed, (2) imports each of the dist's own ``cairn.packs`` entry points —
# ``load_all_packs`` swallows a broken pack's import error, so we surface it here
# instead — (3) runs the loader the way the engine does at startup (NON-strict:
# composition conflicts are logged, not raised) so the newcomer is exercised
# through registration+composition without failing on a PRE-EXISTING, unrelated
# conflict elsewhere in the set, and (4) prints a content_hash over the dist's
# name/version/entry-points as pack-install provenance (ADR-0055 §5).
_VALIDATE_SCRIPT = """
import hashlib, importlib.metadata as m, json, sys
name = sys.argv[1]
dist = m.distribution(name)  # raises PackageNotFoundError if the install didn't land
eps = [ep for ep in dist.entry_points if ep.group == "cairn.packs"]
for ep in eps:  # force-import the pack modules; a broken pack raises here
    ep.load()
from cairn.core.pack.loader import load_all_packs
load_all_packs()  # non-strict, mirroring engine startup — a hard loader crash still raises
payload = json.dumps(
    {"name": name, "version": dist.version,
     "entry_points": sorted(f"{ep.name}={ep.value}" for ep in eps)},
    sort_keys=True,
)
print(hashlib.sha256(payload.encode()).hexdigest()[:16])
"""


def _validate(install_name: str) -> str:
    """Out-of-process deep validation (ADR-0055 §5): a fresh interpreter confirms
    the dist installed, its pack entry points import, and the full pack set still
    composes (strict). Returns a 16-hex content_hash of the installed pack;
    **raises** on any failure so the caller can roll the install back."""
    proc = subprocess.run(
        [sys.executable, "-c", _VALIDATE_SCRIPT, install_name],
        check=True, capture_output=True, text=True, timeout=180,
    )
    return proc.stdout.strip()


def _uv_uninstall(install_name: str) -> None:
    """The DEFAULT uninstaller: refuse with the same posture as ``_uv_install``.

    Symmetry matters here — a door that cannot install must not offer to
    uninstall, or an operator can remove a pack the image will restore on the
    next deploy and conclude the control plane is lying to them.
    """
    raise PackInstallUnsupportedError(_POSTURE)


Installer = Callable[[str], None]
#: returns an optional content_hash of the validated pack; raises on failure.
Validator = Callable[[str], Optional[str]]
Uninstaller = Callable[[str], None]


def _err(exc: Exception) -> str:
    out = getattr(exc, "stderr", "") or ""
    return (f"{exc}" + (f" :: {out[-200:]}" if out else ""))[:300]


async def install_pack(
    ref: str,
    actor: str = "console",
    *,
    store: object | None = None,
    installer: Installer = _uv_install,
    validator: Validator = _validate,
    uninstaller: Uninstaller = _uv_uninstall,
    catalog_idx: dict[str, dict] | None = None,
) -> InstallRecord:
    """Install ``ref`` from the registry, recording the lifecycle. installer /
    validator / uninstaller are injected (defaults shell out to uv / a fresh
    interpreter) so the orchestration is unit-testable without touching the env.
    On a post-install validation failure the just-installed dist is rolled back
    (uninstalled) so a broken pack never lingers (ADR-0055 §5)."""
    store = store or get_install_store()
    idx = catalog_idx if catalog_idx is not None else _catalog_index()
    entry = idx.get(ref, {})
    install_name = (entry.get("install") or {}).get("name", ref)
    rec = InstallRecord(
        ref=ref, kind=entry.get("kind", ""), version=str(entry.get("version", "")),
        source="catalog" if entry else "manual", actor=actor,
    )

    if not is_allowed(ref, idx):
        await store.upsert(rec._to("failed", f"'{ref}' is not in the registry catalog or the install allowlist"))
        return rec

    await store.upsert(rec._to("installing"))
    try:
        await asyncio.to_thread(installer, install_name)
    except PackInstallUnsupportedError:
        # #1545 D2 — categorically different from "this install failed": the
        # door is closed by posture, so there is nothing to retry and a
        # `failed` row would be misleading noise in the install history.
        # Propagate so the route can answer 501 with the reason.
        await store.upsert(rec._to("failed", "runtime install unsupported (#1545 D2)"))
        raise
    except Exception as exc:  # noqa: BLE001 — surface any installer failure as a record
        await store.upsert(rec._to("failed", f"install failed: {_err(exc)}"))
        return rec

    await store.upsert(rec._to("validating"))
    try:
        content_hash = await asyncio.to_thread(validator, install_name)
    except Exception as exc:  # noqa: BLE001
        # Roll back: uninstall the just-installed-but-broken dist so it can't
        # linger / half-activate on the next restart (ADR-0055 §5). Best-effort —
        # a rollback failure is appended, never masks the original error.
        try:
            await asyncio.to_thread(uninstaller, install_name)
            rolled = " (rolled back)"
        except Exception as unexc:  # noqa: BLE001
            rolled = f" (rollback FAILED: {_err(unexc)})"
        await store.upsert(rec._to("failed", f"validation failed{rolled}: {_err(exc)}"))
        return rec

    rec.content_hash = content_hash or ""
    rec.needs_restart = True
    await store.upsert(rec._to("installed", "installed — restart the engine to activate (restart-to-discover)"))
    return rec


async def uninstall_pack(
    ref: str,
    actor: str = "console",
    *,
    store: object | None = None,
    uninstaller: Uninstaller = _uv_uninstall,
    catalog_idx: dict[str, dict] | None = None,
) -> InstallRecord:
    """Uninstall a *console-installed* pack — the inverse of :func:`install_pack`.

    Guard: only refs with an existing ``installed`` record are removable. Engine-
    bundled / editable packs have no install record, so they're refused
    (``PermissionError``) — ``uv pip uninstall``-ing those would break the dev
    checkout / the shipped engine. Idempotent: an already-``removed`` ref returns
    its record untouched. Restart-to-deactivate — the running engine keeps the
    dist imported until its next start, mirroring install's restart-to-discover.
    uninstaller is injected (default shells out to uv) so this is unit-testable."""
    store = store or get_install_store()
    rec = await store.get(ref)
    if rec is None:
        raise PermissionError(
            f"'{ref}' was not installed via the console (engine-bundled or editable) — can't uninstall it here"
        )
    if rec.status == "removed":
        return rec  # idempotent — already gone
    if rec.status != "installed":
        raise PermissionError(f"'{ref}' is not in an installed state (status={rec.status}) — nothing to uninstall")

    idx = catalog_idx if catalog_idx is not None else _catalog_index()

    install_name = (idx.get(ref, {}).get("install") or {}).get("name", ref)

    await store.upsert(rec._to("removing"))
    try:
        await asyncio.to_thread(uninstaller, install_name)
    except Exception as exc:  # noqa: BLE001 — revert to installed + surface the error
        await store.upsert(rec._to("installed", f"uninstall failed: {_err(exc)}"))
        raise
    rec.needs_restart = True
    await store.upsert(rec._to("removed", "removed — restart the engine to deactivate (restart-to-discover)"))
    return rec
