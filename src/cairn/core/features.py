"""Feature flags — which console/API surfaces this deployment offers (#1878 S2, #1879 S3).

This is the THIN carrier: a resolved ``{feature: bool}`` map the console reads
from ``/auth/me`` to decide which nav items, tabs and Settings sections to
render. The store, per-workspace overrides and percentage rollout land in
#1878; until then the map is seeded from the environment only:

  * ``CAIRN_LIGHT=on``            → the ``light`` preset: the held surfaces off
  * ``CAIRN_FEATURE_<NAME>=on|off`` → one flag, wins over the preset

A flag that is absent is ON — flag off is the only change from today's
behaviour (#1828 owner rule: additive gating, nothing removed).
"""

from __future__ import annotations
from cairn.core.settings import getenv

import os
from collections.abc import Mapping

#: Every surface a flag can switch. Names are the console nav ids where one
#: exists, so the sidebar, the router and this list agree without a mapping.
FEATURE_IDS: tuple[str, ...] = (
    "playground", "insights", "gateway", "series", "api", "users",
    "pipelines", "builder", "datasets", "judges", "knowledge", "jobs",
)

#: The ``light`` preset (#1828, locked 2026-09-02): held surfaces are OFF.
LIGHT_OFF: frozenset[str] = frozenset({
    "builder", "datasets", "judges", "knowledge", "jobs",
})

#: ADR-0105 D5 (#2113) — flags that are ACCEPTED and IGNORED. `pipelines` used
#: to switch the pipeline surface off (the light preset held it); since every
#: start is a one-leg pipeline (D1), the pipeline is the substrate a profile
#: runs ON, not a surface it can hide. The key stays in FEATURE_IDS so an old
#: env file or a stored rule still validates; it just cannot turn anything off.
ALWAYS_ON: frozenset[str] = frozenset({"pipelines"})

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _flag(raw: str | None) -> bool | None:
    v = (raw or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def light_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True when this deployment runs the cairn-light preset (``CAIRN_LIGHT``)."""
    e = os.environ if env is None else env
    return _flag(getenv("CAIRN_LIGHT", environ=e)) is True


def resolve_features(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """The resolved feature map for this deployment.

    Order: everything on → ``light`` preset (if ``CAIRN_LIGHT``) → per-feature
    ``CAIRN_FEATURE_<NAME>`` overrides. Pure; ``env`` is injectable for tests.
    """
    e = os.environ if env is None else env
    features = {name: True for name in FEATURE_IDS}
    if light_enabled(e):
        for name in LIGHT_OFF:
            features[name] = False
    for name in FEATURE_IDS:
        override = _flag(getenv(f"CAIRN_FEATURE_{name.upper()}", environ=e))
        if override is not None:
            features[name] = override
    for name in ALWAYS_ON:
        features[name] = True
    return features


__all__ = ["ALWAYS_ON", "FEATURE_IDS", "LIGHT_OFF", "light_enabled", "resolve_features"]


# ─── the flag store (#1878) ─────────────────────────────────────────────
#
# One row per (scope, flag): scope is ``global`` or ``workspace:<id>``; value is
# ``on``, ``off`` or ``NN%`` (a rollout: on for users whose stable bucket is
# below NN — enable-only by decision, so a workspace/global ``off`` still wins
# below it). Resolution per caller:
#
#     workspace rule  →  global rule  →  env seed (CAIRN_LIGHT / CAIRN_FEATURE_*)
#
# and a ``%`` at either level buckets the user. Every change is kept in a
# history table (who, when, old → new) so the audit question "who turned
# pipelines on for acme" is answerable from the store itself.

import asyncio
import hashlib
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.pg_schema_init import PgSchemaInit
from cairn.core.refs import ref_key  # ONE ref spelling (#2126) — pure, no cycle

FEATURE_META: dict[str, str] = {
    "playground": "Sandbox runs inside Workflows",
    "insights": "Health, failure and cost views",
    "gateway": "Provider routing + budgets (Integrations)",
    "series": "Run comparisons (Runs tab)",
    "api": "Keys and SDK snippets (Settings)",
    "users": "Team management (Settings)",
    "pipelines": "Multi-leg pipelines",
    "builder": "Author a pipeline in the console",
    "datasets": "Governed dataset versions (Data)",
    "judges": "LLM-as-judge configs (Approvals)",
    "knowledge": "Corpora for RAG roles (Data)",
    "jobs": "SSH / Modal remote job control",
}

PRESETS: dict[str, dict[str, str]] = {
    "light": {name: ("off" if name in LIGHT_OFF else "on") for name in FEATURE_IDS},
    "everything": {name: "on" for name in FEATURE_IDS},
}

_VALUE_RE = re.compile(r"^(on|off|(?:100|[1-9]?\d)%)$")
GLOBAL = "global"


def workspace_scope(workspace_id: str) -> str:
    return f"workspace:{workspace_id}"


def validate_value(value: str) -> str:
    v = str(value).strip().lower()
    if not _VALUE_RE.match(v):
        raise ValueError(f"feature value must be 'on', 'off' or 'NN%' (0-100), got {value!r}")
    return v


# ─── admin curation overrides (#2173) ────────────────────────────────────────
#
# "Hide this pipeline for this workspace" is the same shape as a feature rule —
# per workspace, admin-written, audited, resolved on read — so it is a rule in
# THIS store rather than a second table with a second history nobody reads. The
# flag is `hide:<kind>:<ref>` and the value is always `on`; un-hiding CLEARS the
# rule rather than writing `off`, so the store holds exactly the set of hidden
# refs and `GET /admin/hidden` is a filter, not a query with a negation in it.
#
# The ref half is a `ref_key` (#2126), never the spelling the admin typed: a
# pipeline leg carries the pack DIRECTORY's spelling and the flow registry the
# `pack.toml` one, and a rule keyed on either would half-apply.

HIDE_PREFIX = "hide:"

#: The two things an admin can hide. A profile or an operator is machinery, not
#: a shelf item; hiding one would be an access rule, which is a different module
#: (`cairn.core.pack.visibility`) and a different question.
HIDE_KINDS: frozenset[str] = frozenset({"pipeline", "flow"})

#: `hide:<kind>:<pack>/<name>` — anchored, because widening the store's flag
#: check from `flag in FEATURE_IDS` to "…or a hide rule" must not widen it to
#: "anything with a colon in it".
_HIDE_RE = re.compile(r"^hide:(pipeline|flow):([a-z0-9][a-z0-9.\-]*/[a-z0-9][a-z0-9.\-]*)$")


def hide_flag(kind: str, ref: str) -> str:
    """The store flag that hides ``ref`` — ``hide:pipeline:incident/blast-radius``.

    Folds the ref through :func:`cairn.core.refs.ref_key`, so an admin hiding
    ``dataset_readiness/check`` and a leg carrying ``flow:dataset-readiness/check``
    land on ONE rule.
    """
    k = str(kind or "").strip().lower()
    if k not in HIDE_KINDS:
        raise ValueError(f"cannot hide a {kind!r}; one of {sorted(HIDE_KINDS)}")
    flag = f"{HIDE_PREFIX}{k}:{ref_key(ref)}"
    if not _HIDE_RE.match(flag):
        raise ValueError(f"{ref!r} is not a <pack>/<name> ref")
    return flag


def parse_hide_flag(flag: str) -> Optional[tuple[str, str]]:
    """``(kind, ref_key)`` for a hide rule, or ``None`` for anything else —
    including every surface flag, so a caller can walk the whole rule set."""
    m = _HIDE_RE.match(str(flag or ""))
    return (m.group(1), m.group(2)) if m else None


def validate_flag(flag: str) -> str:
    """The flags this store accepts: a surface flag, or a curation override."""
    f = str(flag or "")
    if f in FEATURE_IDS or _HIDE_RE.match(f):
        return f
    raise ValueError(f"unknown feature {flag!r}")


def hidden_in(rules: dict[str, dict[str, str]], *, workspace_id: Optional[str]) -> dict[str, set[str]]:
    """The refs an admin has hidden in ``workspace_id``, as ``{kind: {ref_key}}``.

    Pure, and workspace-scoped ONLY: hiding is a per-tenant curation choice, so
    there is deliberately no global tier to inherit from. A deployment-wide
    "never show this" is the pack's own ``visibility: internal``.
    """
    out: dict[str, set[str]] = {k: set() for k in sorted(HIDE_KINDS)}
    if not workspace_id:
        return out
    for flag, value in (rules.get(workspace_scope(workspace_id), {}) or {}).items():
        parsed = parse_hide_flag(flag)
        if parsed is not None and value == "on":
            out[parsed[0]].add(parsed[1])
    return out


def bucket(user_id: str) -> int:
    """A stable 0-99 bucket for ``user_id`` — the same user always lands in the
    same bucket, so a rollout never flips between requests."""
    return int(hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8], 16) % 100


def _apply(value: Optional[str], user_id: Optional[str], current: bool) -> bool:
    if value is None:
        return current
    if value == "on":
        return True
    if value == "off":
        return False
    pct = int(value[:-1])
    return bucket(user_id or "") < pct


def resolve_for(
    rules: dict[str, dict[str, str]],
    *,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    env: Any = None,
) -> dict[str, bool]:
    """Resolve the feature map for one caller: env seed → global rules →
    workspace rules. ``rules`` is ``{scope: {flag: value}}`` as the store
    returns it. Pure."""
    out = resolve_features(env)
    g = rules.get(GLOBAL, {})
    w = rules.get(workspace_scope(workspace_id), {}) if workspace_id else {}
    for name in FEATURE_IDS:
        out[name] = _apply(g.get(name), user_id, out[name])
        out[name] = _apply(w.get(name), user_id, out[name])
    for name in ALWAYS_ON:  # ADR-0105 D5 — a stored rule cannot switch the substrate off either
        out[name] = True
    return out


class FeatureRule(BaseModel):
    scope: str
    flag: str
    value: str
    updated_at: str = ""
    updated_by: str = ""


class FeatureChange(BaseModel):
    at: str
    by: str
    scope: str
    flag: str
    old: Optional[str] = None
    new: Optional[str] = None


class InMemoryFeatureStore:
    def __init__(self) -> None:
        self._rules: dict[tuple[str, str], FeatureRule] = {}
        self._history: list[FeatureChange] = []

    async def rules(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for (scope, flag), r in self._rules.items():
            out.setdefault(scope, {})[flag] = r.value
        return out

    async def set(self, scope: str, flag: str, value: Optional[str], *, by: str = "") -> None:
        validate_flag(flag)
        old = self._rules.get((scope, flag))
        if value is None:
            self._rules.pop((scope, flag), None)
        else:
            value = validate_value(value)
            self._rules[(scope, flag)] = FeatureRule(scope=scope, flag=flag, value=value, updated_at=utc_now_iso(), updated_by=by)
        self._history.insert(0, FeatureChange(at=utc_now_iso(), by=by, scope=scope, flag=flag, old=old.value if old else None, new=value))

    async def history(self, limit: int = 50) -> list[FeatureChange]:
        return list(self._history[:limit])

    def close(self) -> None:
        pass


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
  scope TEXT NOT NULL, flag TEXT NOT NULL, value TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, PRIMARY KEY (scope, flag));
CREATE TABLE IF NOT EXISTS feature_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, by TEXT NOT NULL,
  scope TEXT NOT NULL, flag TEXT NOT NULL, old TEXT, new TEXT);
"""


class SqliteFeatureStore:
    def __init__(self, path: Optional[str] = None) -> None:
        import sqlite3  # noqa: PLC0415 — stdlib, instance handle (mirrors SqlitePolicyStore)

        if path is None:
            from cairn.core import settings  # noqa: PLC0415 — cycle: settings ↔ storage

            path = getenv("CAIRN_FEATURES_DB", "").strip() or settings.default_sqlite_db()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SQLITE_SCHEMA)
        self._lock = asyncio.Lock()

    def _rules_sync(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for scope, flag, value in self._conn.execute("SELECT scope, flag, value FROM features"):
            out.setdefault(scope, {})[flag] = value
        return out

    async def rules(self) -> dict[str, dict[str, str]]:
        return await asyncio.to_thread(self._rules_sync)

    async def set(self, scope: str, flag: str, value: Optional[str], *, by: str = "") -> None:
        validate_flag(flag)
        if value is not None:
            value = validate_value(value)

        def _w() -> None:
            row = self._conn.execute("SELECT value FROM features WHERE scope=? AND flag=?", (scope, flag)).fetchone()
            old = row[0] if row else None
            if value is None:
                self._conn.execute("DELETE FROM features WHERE scope=? AND flag=?", (scope, flag))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO features (scope, flag, value, updated_at, updated_by) VALUES (?,?,?,?,?)",
                    (scope, flag, value, utc_now_iso(), by))
            self._conn.execute(
                "INSERT INTO feature_changes (at, by, scope, flag, old, new) VALUES (?,?,?,?,?,?)",
                (utc_now_iso(), by, scope, flag, old, value))
            self._conn.commit()
        async with self._lock:
            await asyncio.to_thread(_w)

    async def history(self, limit: int = 50) -> list[FeatureChange]:
        def _r() -> list[FeatureChange]:
            rows = self._conn.execute(
                "SELECT at, by, scope, flag, old, new FROM feature_changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [FeatureChange(at=r[0], by=r[1], scope=r[2], flag=r[3], old=r[4], new=r[5]) for r in rows]
        return await asyncio.to_thread(_r)

    def close(self) -> None:
        self._conn.close()


class PostgresFeatureStore(PgSchemaInit):
    """Postgres tier — lazy asyncpg pool shared per DSN (mirrors PostgresPolicyStore)."""
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._schema_ready = False

    async def _open_pool(self) -> Any:
        try:
            import asyncpg  # noqa: F401, PLC0415 — optional dep (storage-postgres extra)
        except ImportError as e:
            raise RuntimeError("PostgresFeatureStore requires the 'storage-postgres' optional dep group.") from e
        if not self._dsn:
            raise RuntimeError("PostgresFeatureStore needs a DSN (CAIRN_STORAGE=postgres://…).")
        from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415 — optional-dep module

        return await shared_pool(self._dsn)

    async def _ensure_schema(self, pool: Any) -> None:
        async with pool.acquire() as c:
            await c.execute(
                "CREATE TABLE IF NOT EXISTS features (scope TEXT NOT NULL, flag TEXT NOT NULL, value TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, PRIMARY KEY (scope, flag))")
            await c.execute(
                "CREATE TABLE IF NOT EXISTS feature_changes (id BIGSERIAL PRIMARY KEY, at TEXT NOT NULL, by_user TEXT NOT NULL, "
                "scope TEXT NOT NULL, flag TEXT NOT NULL, old TEXT, new TEXT)")

    async def rules(self) -> dict[str, dict[str, str]]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch("SELECT scope, flag, value FROM features")
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            out.setdefault(r["scope"], {})[r["flag"]] = r["value"]
        return out

    async def set(self, scope: str, flag: str, value: Optional[str], *, by: str = "") -> None:
        validate_flag(flag)
        if value is not None:
            value = validate_value(value)
        pool = await self._get_pool()
        async with pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT value FROM features WHERE scope=$1 AND flag=$2", scope, flag)
            old = row["value"] if row else None
            if value is None:
                await c.execute("DELETE FROM features WHERE scope=$1 AND flag=$2", scope, flag)
            else:
                await c.execute(
                    "INSERT INTO features (scope, flag, value, updated_at, updated_by) VALUES ($1,$2,$3,$4,$5) "
                    "ON CONFLICT (scope, flag) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at, updated_by=EXCLUDED.updated_by",
                    scope, flag, value, utc_now_iso(), by)
            await c.execute(
                "INSERT INTO feature_changes (at, by_user, scope, flag, old, new) VALUES ($1,$2,$3,$4,$5,$6)",
                utc_now_iso(), by, scope, flag, old, value)

    async def history(self, limit: int = 50) -> list[FeatureChange]:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            rows = await c.fetch(
                "SELECT at, by_user, scope, flag, old, new FROM feature_changes ORDER BY id DESC LIMIT $1", limit)
        return [FeatureChange(at=r["at"], by=r["by_user"], scope=r["scope"], flag=r["flag"], old=r["old"], new=r["new"]) for r in rows]

    def close(self) -> None:
        pass


_STORE: Any = None


def default_feature_store() -> Any:
    """Process-wide flag store. ``CAIRN_STORAGE=postgres://…`` → Postgres; else
    sqlite in the shared cairn.db (``CAIRN_FEATURES_DB`` overrides the path;
    ``:memory:`` → volatile)."""
    global _STORE
    if _STORE is not None:
        return _STORE
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        _STORE = PostgresFeatureStore(dsn=url)
    elif getenv("CAIRN_FEATURES_DB", "").strip() == ":memory:":
        _STORE = InMemoryFeatureStore()
    else:
        _STORE = SqliteFeatureStore()
    return _STORE


def reset_feature_store() -> None:
    """Drop the process-wide store (tests)."""
    global _STORE
    if _STORE is not None:
        try:
            _STORE.close()
        except Exception:  # noqa: BLE001
            pass
    _STORE = None


async def apply_preset(name: str, *, store: Any = None, by: str = "") -> dict[str, str]:
    """Write a preset as GLOBAL rules (and clear any global rollout)."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; one of {sorted(PRESETS)}")
    st = store or default_feature_store()
    for flag, value in PRESETS[name].items():
        await st.set(GLOBAL, flag, value, by=by)
    return dict(PRESETS[name])


async def features_for(*, workspace_id: Optional[str], user_id: Optional[str], store: Any = None) -> dict[str, bool]:
    """The resolved map for one caller — what ``/auth/me`` returns and what the
    router gate checks. A store hiccup falls back to the env seed (never 500)."""
    try:
        rules = await (store or default_feature_store()).rules()
    except Exception:  # noqa: BLE001 — flags must never take the API down
        rules = {}
    return resolve_for(rules, workspace_id=workspace_id, user_id=user_id)


async def hidden_for(*, workspace_id: Optional[str], store: Any = None) -> dict[str, set[str]]:
    """The refs an admin has hidden in one workspace (#2173).

    Same never-500 contract as :func:`features_for`: a store hiccup means
    "nothing hidden", i.e. the pack's own tiers. Failing the other way would
    empty a tenant's catalogue because sqlite was busy.
    """
    try:
        rules = await (store or default_feature_store()).rules()
    except Exception:  # noqa: BLE001 — curation must never take the list down
        rules = {}
    return hidden_in(rules, workspace_id=workspace_id)


async def set_hidden(
    kind: str, ref: str, hidden: bool, *, workspace_id: str, store: Any = None, by: str = "",
) -> str:
    """Hide (or un-hide) one ref for one workspace; returns the store flag.

    Un-hiding CLEARS the rule instead of writing ``off`` — see the block
    comment above :data:`HIDE_PREFIX`.
    """
    flag = hide_flag(kind, ref)
    st = store or default_feature_store()
    await st.set(workspace_scope(workspace_id), flag, "on" if hidden else None, by=by)
    return flag


__all__ += [
    "FEATURE_META", "PRESETS", "GLOBAL", "workspace_scope", "validate_value", "bucket", "resolve_for",
    "FeatureRule", "FeatureChange", "InMemoryFeatureStore", "SqliteFeatureStore", "PostgresFeatureStore",
    "default_feature_store", "reset_feature_store", "apply_preset", "features_for",
    "HIDE_KINDS", "HIDE_PREFIX", "hide_flag", "parse_hide_flag", "validate_flag", "hidden_in",
    "hidden_for", "set_hidden",
]
