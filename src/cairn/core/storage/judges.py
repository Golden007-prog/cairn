"""Judge registry (#1467) — named, immutable-versioned LLM-judge definitions.

The owner ruling that shapes this module: judge DEFINITIONS live in the cairn
database (memory / sqlite / postgres, like runs and artifacts), while judge
EXECUTION stays in operators through the governed ``llm_complete`` gateway.
DB = config store, operator = execution body, gateway = governed call.

Rows are immutable versions: editing a judge appends the next version, it
never rewrites one — so every measure run can stamp the exact
``name@version`` + prompt hash it was graded with and stay comparable across
judge edits (the flash→pro re-basing of #1463 becomes a queryable fact).
Re-putting a definition identical to the head is a no-op, not a new version
(same idempotency-by-content as the dataset registry).

Consumers reference judges as ``judge:<name>[@N|@latest]`` config values;
the leg executor resolves them at LAUNCH on the control plane (remote boxes
have no DB access, D4) into the full definition dict the operator executes.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import hashlib
import json
import os
import sqlite3
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.judge_agreement import JudgeAgreement
from cairn.core.pg_schema_init import schema_init_lock
from cairn.core.workspace import DEFAULT_WORKSPACE


def _filter_agreements(rows, judge_name, judge_version, workspace_id) -> list[JudgeAgreement]:
    """Shared filter so all three backends answer ``list_agreements`` alike."""
    out = rows
    if workspace_id is not None:
        out = [a for a in out if (a.workspace_id or DEFAULT_WORKSPACE) == workspace_id]
    if judge_name is not None:
        out = [a for a in out if a.judge_name == judge_name]
    if judge_version is not None:
        out = [a for a in out if a.judge_version == int(judge_version)]
    return sorted(out, key=lambda a: (a.judge_name, -a.judge_version, a.golden_ref))


class JudgeDefinition(BaseModel):
    """One immutable version of a named judge."""

    name: str
    #: Assigned by the store on put — 0 means "not yet stored".
    version: int = 0
    #: Gateway model id (e.g. ``deepseek/deepseek-v4-pro``) or a model-role.
    model: str
    #: The rubric / prompt template. Placeholders are the consuming operator's
    #: contract (e.g. closedbook formats ``{q}``/``{gold}``/``{ans}``).
    prompt: str
    #: Label contract for classification judges; empty = free-form / score.
    labels: list[str] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 16
    #: Per-judge budget (advisory until the operator-side enforcement slice).
    budget_usd: Optional[float] = None
    budget_calls: Optional[int] = None
    description: str = ""
    created_at: str = ""
    created_by: str = ""

    def prompt_hash(self) -> str:
        """Hash of everything that changes what a grade MEANS — model, prompt,
        contract, sampling. Stamped into findings for comparability."""
        canon = json.dumps(
            [self.model, self.prompt, self.labels, self.temperature, self.max_tokens],
            sort_keys=True,
        )
        return hashlib.sha256(canon.encode()).hexdigest()


@runtime_checkable
class JudgeStore(Protocol):
    async def ensure_schema(self) -> None: ...
    async def put_judge(self, definition: JudgeDefinition, *, actor: str = "") -> JudgeDefinition: ...
    async def get_judge(self, name: str, version: Optional[int] = None) -> Optional[JudgeDefinition]: ...
    async def list_judges(self) -> list[JudgeDefinition]: ...
    async def list_judge_versions(self, name: str) -> list[JudgeDefinition]: ...
    #: #2024 (ADR-0102 D3) — judge-vs-human agreement on a golden set, stored
    #: on the judge VERSION. Every backend implements both.
    async def put_agreement(self, agreement: "JudgeAgreement") -> "JudgeAgreement": ...
    async def list_agreements(
        self, *, judge_name: Optional[str] = None, judge_version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> list["JudgeAgreement"]: ...
    async def close(self) -> None: ...


# ─── null (unconfigured storage) ──────────────────────────────────────────────


class _NullJudgeStore:
    """No storage configured: reads return nothing; a put fails loudly — a
    judge definition must never silently vanish (same ethos as the dataset
    registry's null tier)."""

    async def ensure_schema(self) -> None:
        return None

    async def put_judge(self, definition, *, actor="") -> JudgeDefinition:
        raise RuntimeError("judge registry not configured (CAIRN_STORAGE unset) — "
                           f"cannot store judge {definition.name!r}")

    async def get_judge(self, name, version=None) -> Optional[JudgeDefinition]:
        return None

    async def list_judges(self) -> list[JudgeDefinition]:
        return []

    async def list_judge_versions(self, name) -> list[JudgeDefinition]:
        return []

    async def put_agreement(self, agreement):
        raise RuntimeError("judge registry not configured (CAIRN_STORAGE unset) — "
                           f"cannot store an agreement for judge {agreement.judge_name!r}")

    async def list_agreements(self, **_) -> list[JudgeAgreement]:
        return []

    async def close(self) -> None:
        return None


# ─── in-memory (tests) ────────────────────────────────────────────────────────


class InMemoryJudgeStore:
    def __init__(self) -> None:
        self._rows: dict[str, list[JudgeDefinition]] = {}
        self._agreements: dict[str, JudgeAgreement] = {}
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        return None

    async def put_judge(self, definition, *, actor="") -> JudgeDefinition:
        async with self._lock:
            history = self._rows.setdefault(definition.name, [])
            if history and history[-1].prompt_hash() == definition.prompt_hash():
                return history[-1].model_copy(deep=True)
            stored = definition.model_copy(update={
                "version": (history[-1].version + 1) if history else 1,
                "created_at": utc_now_iso(), "created_by": actor,
            })
            history.append(stored)
            return stored.model_copy(deep=True)

    async def get_judge(self, name, version=None) -> Optional[JudgeDefinition]:
        async with self._lock:
            history = self._rows.get(name) or []
            if not history:
                return None
            if version is None:
                return history[-1].model_copy(deep=True)
            hit = next((d for d in history if d.version == version), None)
            return hit.model_copy(deep=True) if hit else None

    async def list_judges(self) -> list[JudgeDefinition]:
        async with self._lock:
            return [h[-1].model_copy(deep=True) for _, h in sorted(self._rows.items()) if h]

    async def list_judge_versions(self, name) -> list[JudgeDefinition]:
        async with self._lock:
            return [d.model_copy(deep=True) for d in reversed(self._rows.get(name) or [])]

    async def put_agreement(self, agreement: JudgeAgreement) -> JudgeAgreement:
        async with self._lock:
            self._agreements[agreement.key()] = agreement.model_copy(deep=True)
        return agreement

    async def list_agreements(self, *, judge_name=None, judge_version=None,
                              workspace_id=None) -> list[JudgeAgreement]:
        async with self._lock:
            rows = [a.model_copy(deep=True) for a in self._agreements.values()]
        return _filter_agreements(rows, judge_name, judge_version, workspace_id)

    async def close(self) -> None:
        return None


# ─── sqlite (hermetic tests + local single-file) ──────────────────────────────


_CREATE_SQLITE = """
    CREATE TABLE IF NOT EXISTS judges (
        name TEXT NOT NULL, version INTEGER NOT NULL,
        prompt_hash TEXT NOT NULL, data TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by TEXT,
        PRIMARY KEY (name, version)
    )
"""

#: #2024 — a SEPARATE table rather than a column on ``judges``: the judges rows
#: are immutable by contract ("editing a judge appends the next version, it
#: never rewrites one"), and an agreement is recomputed. Keeping the measurement
#: beside the definition instead of inside it is what lets both stay true, and
#: it is the migration too — a deployment that predates this gets the new table
#: on first use and every existing judge keeps loading, untrusted by default.
_CREATE_SQLITE_AGREEMENTS = """
    CREATE TABLE IF NOT EXISTS judge_agreements (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        judge_name TEXT NOT NULL, judge_version INTEGER NOT NULL,
        golden_ref TEXT NOT NULL, data TEXT NOT NULL,
        computed_at TEXT NOT NULL
    )
"""


class SqliteJudgeStore:
    """Stdlib-``sqlite3`` behind an ``asyncio.Lock`` — JSON-in-``data`` with
    mirrored filter columns (the suggestions / eval-ledger shape)."""

    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_SQLITE)
        self._conn.execute(_CREATE_SQLITE_AGREEMENTS)
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        return None  # done in __init__

    async def put_judge(self, definition, *, actor="") -> JudgeDefinition:
        async with self._lock:
            head = self._conn.execute(
                "SELECT version, prompt_hash, data FROM judges WHERE name=? "
                "ORDER BY version DESC LIMIT 1", (definition.name,)).fetchone()
            if head and head[1] == definition.prompt_hash():
                return JudgeDefinition.model_validate_json(head[2])
            stored = definition.model_copy(update={
                "version": (int(head[0]) + 1) if head else 1,
                "created_at": utc_now_iso(), "created_by": actor,
            })
            self._conn.execute(
                "INSERT INTO judges (name, version, prompt_hash, data, created_at, created_by) "
                "VALUES (?,?,?,?,?,?)",
                (stored.name, stored.version, stored.prompt_hash(),
                 stored.model_dump_json(), stored.created_at, actor))
            self._conn.commit()
        return stored

    async def get_judge(self, name, version=None) -> Optional[JudgeDefinition]:
        async with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT data FROM judges WHERE name=? ORDER BY version DESC LIMIT 1",
                    (name,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT data FROM judges WHERE name=? AND version=?",
                    (name, int(version))).fetchone()
        return JudgeDefinition.model_validate_json(row[0]) if row else None

    async def list_judges(self) -> list[JudgeDefinition]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM judges j WHERE version = "
                "(SELECT MAX(version) FROM judges WHERE name = j.name) ORDER BY name"
            ).fetchall()
        return [JudgeDefinition.model_validate_json(r[0]) for r in rows]

    async def list_judge_versions(self, name) -> list[JudgeDefinition]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM judges WHERE name=? ORDER BY version DESC", (name,)
            ).fetchall()
        return [JudgeDefinition.model_validate_json(r[0]) for r in rows]

    async def put_agreement(self, agreement: JudgeAgreement) -> JudgeAgreement:
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO judge_agreements "
                "(id, workspace_id, judge_name, judge_version, golden_ref, data, computed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (agreement.key(), agreement.workspace_id or DEFAULT_WORKSPACE,
                 agreement.judge_name, agreement.judge_version, agreement.golden_ref,
                 agreement.model_dump_json(), agreement.computed_at))
            self._conn.commit()
        return agreement

    async def list_agreements(self, *, judge_name=None, judge_version=None,
                              workspace_id=None) -> list[JudgeAgreement]:
        async with self._lock:
            rows = self._conn.execute("SELECT data FROM judge_agreements").fetchall()
        return _filter_agreements([JudgeAgreement.model_validate_json(r[0]) for r in rows],
                                  judge_name, judge_version, workspace_id)

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()


# ─── postgres (production; the existing CAIRN_STORAGE database) ─────────────────


_CREATE_PG = """
    CREATE TABLE IF NOT EXISTS judges (
        name TEXT NOT NULL, version INTEGER NOT NULL,
        prompt_hash TEXT NOT NULL, data TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by TEXT,
        PRIMARY KEY (name, version)
    )
"""

_CREATE_PG_AGREEMENTS = """
    CREATE TABLE IF NOT EXISTS judge_agreements (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        judge_name TEXT NOT NULL, judge_version INTEGER NOT NULL,
        golden_ref TEXT NOT NULL, data TEXT NOT NULL,
        computed_at TEXT NOT NULL
    )
"""


class PostgresJudgeStore:
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
            await conn.execute(_CREATE_PG_AGREEMENTS)
        self._schema_ready = True

    async def put_judge(self, definition, *, actor="") -> JudgeDefinition:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                head = await conn.fetchrow(
                    "SELECT version, prompt_hash, data FROM judges WHERE name=$1 "
                    "ORDER BY version DESC LIMIT 1 FOR UPDATE", definition.name)
                if head and head["prompt_hash"] == definition.prompt_hash():
                    return JudgeDefinition.model_validate_json(head["data"])
                stored = definition.model_copy(update={
                    "version": (int(head["version"]) + 1) if head else 1,
                    "created_at": utc_now_iso(), "created_by": actor,
                })
                await conn.execute(
                    "INSERT INTO judges (name, version, prompt_hash, data, created_at, created_by) "
                    "VALUES ($1,$2,$3,$4,$5,$6)",
                    stored.name, stored.version, stored.prompt_hash(),
                    stored.model_dump_json(), stored.created_at, actor)
        return stored

    async def get_judge(self, name, version=None) -> Optional[JudgeDefinition]:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            if version is None:
                row = await conn.fetchrow(
                    "SELECT data FROM judges WHERE name=$1 ORDER BY version DESC LIMIT 1", name)
            else:
                row = await conn.fetchrow(
                    "SELECT data FROM judges WHERE name=$1 AND version=$2", name, int(version))
        return JudgeDefinition.model_validate_json(row["data"]) if row else None

    async def list_judges(self) -> list[JudgeDefinition]:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT data FROM judges j WHERE version = "
                "(SELECT MAX(version) FROM judges WHERE name = j.name) ORDER BY name")
        return [JudgeDefinition.model_validate_json(r["data"]) for r in rows]

    async def list_judge_versions(self, name) -> list[JudgeDefinition]:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT data FROM judges WHERE name=$1 ORDER BY version DESC", name)
        return [JudgeDefinition.model_validate_json(r["data"]) for r in rows]

    async def put_agreement(self, agreement: JudgeAgreement) -> JudgeAgreement:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO judge_agreements "
                "(id, workspace_id, judge_name, judge_version, golden_ref, data, computed_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (id) DO UPDATE SET "
                "data = EXCLUDED.data, computed_at = EXCLUDED.computed_at",
                agreement.key(), agreement.workspace_id or DEFAULT_WORKSPACE,
                agreement.judge_name, agreement.judge_version, agreement.golden_ref,
                agreement.model_dump_json(), agreement.computed_at)
        return agreement

    async def list_agreements(self, *, judge_name=None, judge_version=None,
                              workspace_id=None) -> list[JudgeAgreement]:
        await self.ensure_schema()
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT data FROM judge_agreements")
        return _filter_agreements([JudgeAgreement.model_validate_json(r["data"]) for r in rows],
                                  judge_name, judge_version, workspace_id)

    async def close(self) -> None:
        return None  # shared pool is process-owned


# ─── refs: judge:<name>[@N|@latest] ───────────────────────────────────────────


def parse_judge_ref(ref: str) -> Optional[tuple[str, Optional[int]]]:
    """``(name, version)`` for a ``judge:`` ref, ``None`` for anything else.
    ``@latest`` / no suffix → version ``None`` (the head). Raises on a
    malformed version — a typo must fail loudly (mirrors dataset refs)."""
    if not isinstance(ref, str) or not ref.startswith("judge:"):
        return None
    body = ref[len("judge:"):].strip()
    name, _, ver = body.partition("@")
    if not name:
        raise ValueError(f"judge ref {ref!r} has no name")
    if ver in ("", "latest"):
        return name, None
    if not ver.isdigit():
        raise ValueError(f"judge ref {ref!r}: version must be a number or 'latest'")
    return name, int(ver)


async def resolve_judge_ref(store: JudgeStore, ref: str) -> Optional[dict]:
    """The full definition a ``judge:`` ref names, as the plain dict an
    operator executes (model, prompt, contract, sampling) plus the provenance
    stamp (name, version, prompt_hash). ``None`` when ``ref`` isn't a judge
    ref; raises ``KeyError`` on an unknown name/version (fail-closed — never
    silently grade with a judge that doesn't exist)."""
    parsed = parse_judge_ref(ref)
    if parsed is None:
        return None
    name, version = parsed
    d = await store.get_judge(name, version)
    if d is None:
        raise KeyError(f"judge ref {ref!r} not found in the registry")
    return {
        "name": d.name, "version": d.version, "prompt_hash": d.prompt_hash(),
        "model": d.model, "prompt": d.prompt, "labels": list(d.labels),
        "temperature": d.temperature, "max_tokens": d.max_tokens,
        "budget_usd": d.budget_usd, "budget_calls": d.budget_calls,
    }


async def resolve_judge_refs_in_config(store: JudgeStore, config: dict) -> dict:
    """Rewrite every ``judge:`` string value in a leg's config to the resolved
    definition DICT — the launch-time half of #1467. Runs on the control plane
    (leg executor) because remote boxes have no DB access (D4). Values that
    aren't judge refs pass through untouched; unknown refs raise so a typo'd
    name fails at LAUNCH, before any compute or paid judge call is spent."""
    out = dict(config)
    for key, value in config.items():
        if isinstance(value, str) and value.startswith("judge:"):
            resolved = await resolve_judge_ref(store, value)
            if resolved is not None:
                out[key] = resolved
    return out


def build_judge_store(dsn: Optional[str] = None) -> JudgeStore:
    """Pick a backend by ``dsn`` (falls back to ``CAIRN_STORAGE``), mirroring
    :func:`cairn.core.storage.artifacts.build_artifact_store`."""
    url = (dsn or getenv("CAIRN_STORAGE", "")).strip()
    if not url:
        return _NullJudgeStore()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresJudgeStore(dsn=url)
    if url.startswith("sqlite://"):
        # #1471 — ONE parser (settings.sqlite_path_from_url) so every store
        # resolves the same CAIRN_STORAGE to the same file.
        from cairn.core import settings  # noqa: PLC0415 - cycle: settings imports storage on some paths
        return SqliteJudgeStore(path=settings.sqlite_path_from_url(url) or ":memory:")
    return _NullJudgeStore()


__all__ = [
    "InMemoryJudgeStore",
    "JudgeAgreement",
    "JudgeDefinition",
    "JudgeStore",
    "PostgresJudgeStore",
    "SqliteJudgeStore",
    "build_judge_store",
    "parse_judge_ref",
    "resolve_judge_ref",
    "resolve_judge_refs_in_config",
]
