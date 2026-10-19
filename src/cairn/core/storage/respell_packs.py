"""One-shot respell of the hyphenated packs' recorded identity (#2527).

**Why this exists.** A pipeline's pack is named by its ``pack.toml`` (#2513). Until
#2527 a source checkout kept the directory spelling where it was the same pack in its
other spelling, so it recorded ``dataset_readiness/check`` while a pack wheel recorded
``dataset-readiness/check``: one pipeline, two refs, depending on how the pack was
installed. The owner's call (2026-10-13) is the manifest spelling everywhere, plus a
one-time rewrite of what was recorded under the directory spelling. Three packs are
affected: ``dataset-readiness``, ``security-triage`` and ``security-kb``.

**Only identity moves.** Each table names the exact fields that address a row or say
what it runs (:data:`TARGETS`): a run's ``template_ref`` and ``fingerprint``, a pipeline
record's ``bundle_ref`` and plan step refs, a schedule's ``workflow`` and
``declared_by``, and so on. Inputs, results, outputs and messages are never read, so a
relative path or a sentence that happens to start ``dataset_readiness/`` stays as it
was. A field is rewritten only when it STARTS with an optional kind prefix and one of
the three directory names followed by ``/`` (:data:`REF`).

**Ids that embed a ref are re-keyed:** workflow configs and presets
(``<ws>:<template_ref>[:<name>]``), workflow registry rows (``<workflow_ref>@<ver>``),
and declared schedules and triggers (``pipeline--<pack>--…``, ``declared--<slug>--…``).
A re-key whose target id already exists is not applied; it is reported as a conflict for
a person to resolve, because silently overwriting either row loses data.

**What is never touched** (:data:`NEVER`): the audit log (hash-chained, so a rewrite reads
as tampering), run event logs (append-only history), the content-addressed registry
tables, artifacts and fingerprint records.

**Safety.** Like :mod:`cairn.core.storage.backfill_pipelines` it is a command that dry-runs
by default (``cairn pipeline respell-packs``), never a connect-time migration. Each write
is compare-and-set on the row's old content, so a row a live process changed in between
is skipped and reported rather than overwritten, and ``--apply`` additionally requires
the operator to say the server is stopped. Tables are read in pages. There is no exact
inverse, so take a backup first. A second ``--apply`` finds nothing.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from cairn.core.settings import getenv

#: The three packs whose directory spelling differs from their manifest name.
RESPELL = {"dataset_readiness": "dataset-readiness", "security_triage": "security-triage",
           "security_kb": "security-kb"}
_DIRS = "|".join(RESPELL)
#: A ref in the directory spelling: an optional kind prefix, then the pack, then ``/``.
REF = re.compile(rf"^((?:pipeline|flow|template|workflow):)?({_DIRS})/")
#: A declared or pipeline-minted id carrying the directory spelling as a token.
_PIPELINE_ID = re.compile(rf"^pipeline--({_DIRS})--")
_DECLARED_ID = re.compile(rf"^(declared--(?:(?:pipeline|flow|template|workflow)-)?)({_DIRS})-")
#: Recorded data this command never touches, and why.
NEVER = (
    "the audit log: hash-chained, a rewrite breaks the chain",
    "run_events: append-only history",
    "workflow_templates / workflow_schemas: content-addressed by a hash of the data",
    "inputs, results, outputs and messages anywhere: not identity",
    "artifacts and fingerprint records",
)
SAMPLES = 10
PAGE = 500


def respell(value: Any) -> Any:
    """``value`` in the manifest spelling when it is a directory-spelled ref, else unchanged."""
    if not isinstance(value, str):
        return value
    m = REF.match(value)
    return value if m is None else f"{m[1] or ''}{RESPELL[m[2]]}/{value[m.end():]}"


#: A ref inside machine-written YAML text (a declared trigger's spec): the token after
#: whitespace, a quote or a ``:``.
_TEXT_REF = re.compile(rf"(?<=[\s'\":])((?:pipeline|flow|template|workflow):)?({_DIRS})/")


def respell_pack(value: Any) -> Any:
    """A bare pack name (``dataset_readiness``) in the manifest spelling, else unchanged."""
    return RESPELL.get(value, value) if isinstance(value, str) else value


def respell_text(value: Any) -> Any:
    """Every directory-spelled ref token inside a text field respelled."""
    if not isinstance(value, str):
        return value
    return _TEXT_REF.sub(lambda m: f"{m[1] or ''}{RESPELL[m[2]]}/", value)


def respell_id(value: str) -> str:
    """A minted schedule/trigger id with the pack token respelled, else unchanged."""
    if m := _PIPELINE_ID.match(value):
        return f"pipeline--{RESPELL[m[1]]}--{value[m.end():]}"
    if m := _DECLARED_ID.match(value):
        return f"{m[1]}{RESPELL[m[2]]}-{value[m.end():]}"
    return value


def respell_path(doc: Any, path: str) -> int:
    """Respell the field at ``path`` in ``doc`` in place (``a.b[*].c``, with an optional
    ``!pack`` or ``!text`` mode); returns how many values changed. A missing field is not
    an error: not every row carries every field."""
    path, _, mode = path.partition("!")
    return _respell_path(doc, path, {"": respell, "pack": respell_pack, "text": respell_text}[mode])


def _respell_path(doc: Any, path: str, fn: Callable[[Any], Any]) -> int:
    head, _, rest = path.partition(".")
    many = head.endswith("[*]")
    key = head[:-3] if many else head
    if not isinstance(doc, dict) or key not in doc:
        return 0
    if many:
        items = doc[key] if isinstance(doc[key], list) else []
        return sum(_respell_path({"_": item}, f"_.{rest}", fn) if rest else 0 for item in items)
    if rest:
        return _respell_path(doc[key], rest, fn)
    new = fn(doc[key])
    if new != doc[key]:
        doc[key] = new
        return 1
    return 0


# ── what moves, per table ─────────────────────────────────────────────────


def _rekey_config(row: dict[str, Any]) -> tuple[str, ...]:
    from cairn.core.workflow_config_store import _key  # noqa: PLC0415 - the store's own id rule
    ref = row.get("template_ref")
    if respell(ref) == ref:          # nothing to move (a NULL ref included): the key stays
        return (row["id"],)
    return (_key(row["workspace_id"], respell(ref)),)


def _rekey_preset(row: dict[str, Any]) -> tuple[str, ...]:
    from cairn.core.workflow_config_store import _pkey  # noqa: PLC0415
    ref = row.get("template_ref")
    if respell(ref) == ref:
        return (row["id"],)
    return (_pkey(row["workspace_id"], respell(ref), row["data"].get("name", "")),)


def _rekey_installed(row: dict[str, Any]) -> tuple[str, ...]:
    old_ref, rid = row.get("ref"), row["id"]
    new_ref = respell(old_ref)
    if new_ref == old_ref or not rid.startswith(old_ref):
        return (row["workspace_id"], rid)
    return (row["workspace_id"], new_ref + rid[len(old_ref):])


def _rekey_minted(row: dict[str, Any]) -> tuple[str, ...]:
    return (respell_id(row["id"]),)


@dataclass(frozen=True)
class Target:
    """A table holding recorded identity: its key, its plain ref columns, the identity
    paths inside its JSON ``data``, how a ref-bearing key is recomputed, and the
    environment variable its store reads for a dedicated SQLite file."""
    table: str
    key: tuple[str, ...]
    columns: tuple[str, ...]
    paths: tuple[str, ...]
    db_env: str
    extra: tuple[str, ...] = ()            # columns read only to recompute the key
    rekey: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    data_id: bool = False                  # the JSON repeats the id: keep it equal to the key


TARGETS = (
    Target("runs", ("id",), ("fingerprint",), ("template_ref", "fingerprint"), "CAIRN_RUN_DB"),
    Target("pipelines", ("pipeline_id",), ("bundle_ref",),
           ("bundle_ref", "plan.steps[*].ref", "trigger.ref", "trigger.declared_by", "trigger.workflow"),
           "CAIRN_PIPELINE_DB"),
    Target("workflow_configs", ("id",), ("template_ref",), ("template_ref",), "CAIRN_WORKFLOW_CONFIG_DB",
           extra=("workspace_id",), rekey=_rekey_config, data_id=True),
    Target("workflow_presets", ("id",), ("template_ref",), ("template_ref",), "CAIRN_WORKFLOW_CONFIG_DB",
           extra=("workspace_id",), rekey=_rekey_preset, data_id=True),
    Target("input_presets", ("id",), ("template_ref",), ("template_ref",), "CAIRN_WORKFLOW_CONFIG_DB",
           extra=("workspace_id",), rekey=_rekey_preset, data_id=True),
    Target("workflow_installed", ("workspace_id", "id"), ("ref",), ("workflow_ref", "ref", "pack_ref!pack"),
           "CAIRN_WORKFLOW_REGISTRY_DB", rekey=_rekey_installed, data_id=True),
    Target("installs", ("id",), ("ref",), ("ref",), "CAIRN_INSTALLS_DB"),
    Target("schedules", ("id",), ("workflow",), ("workflow", "declared_by"), "CAIRN_SCHEDULES_DB",
           rekey=_rekey_minted, data_id=True),
    Target("triggers", ("id",), (), ("declared_by", "yaml!text"), "CAIRN_TRIGGERS_DB", rekey=_rekey_minted,
           data_id=True),
    Target("inbox", ("id",), (), ("template",), "CAIRN_TRIGGER_INBOX_DB"),
)


@dataclass
class Report:
    applied: bool
    scanned: dict[str, int] = field(default_factory=dict)
    changed: dict[str, int] = field(default_factory=dict)
    rekeyed: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)      # a re-key whose target exists
    raced: list[str] = field(default_factory=list)          # changed underneath us: skipped
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.changed.values())

    def summary(self) -> str:
        verb = "respelled" if self.applied else "would respell"
        per = ", ".join(f"{t} {n}/{self.scanned[t]}" for t, n in self.changed.items() if n) or "nothing"
        tail = []
        if any(self.rekeyed.values()):
            tail.append(f"{sum(self.rekeyed.values())} re-keyed")
        if self.conflicts:
            tail.append(f"{len(self.conflicts)} conflict(s) left for a person")
        if self.raced:
            tail.append(f"{len(self.raced)} changed underneath and skipped")
        return f"{verb} {self.total} row(s): {per}" + (f" ({'; '.join(tail)})" if tail else "")


def plan_row(target: Target, row: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """``(column → new value, new key)`` for one row; empty changes when nothing moves.
    ``row["data"]`` is the decoded JSON and is not mutated."""
    changes: dict[str, Any] = {}
    for col in target.columns:
        if (new := respell(row.get(col))) != row.get(col):
            changes[col] = new
    data = json.loads(json.dumps(row["data"]))
    moved = sum(respell_path(data, p) for p in target.paths)
    key = tuple(row[k] for k in target.key)
    new_key = target.rekey(row) if target.rekey else key
    if target.data_id and new_key != key and isinstance(data, dict) and "id" in data:
        data["id"] = new_key[-1]
        moved += 1
    if moved:
        changes["data"] = json.dumps(data)
    if new_key != key and not changes:
        changes["data"] = json.dumps(data)
    return changes, new_key


def _note(report: Report, target: Target, key: tuple[str, ...], new_key: tuple[str, ...],
          row: dict[str, Any], changes: dict[str, Any]) -> None:
    report.changed[target.table] = report.changed.get(target.table, 0) + 1
    if new_key != key:
        report.rekeyed[target.table] = report.rekeyed.get(target.table, 0) + 1
    rows = report.samples.setdefault(target.table, [])
    if len(rows) < SAMPLES:
        what = [f"key {'/'.join(key)} → {'/'.join(new_key)}"] if new_key != key else []
        what += [f"{c}: {row.get(c)} → {v}" for c, v in changes.items() if c != "data"]
        if not what:
            new = json.loads(changes["data"])
            paths = [p.partition("!")[0] for p in target.paths]
            what = [f"{p}: {_get(row['data'], p)} → {_get(new, p)}" for p in paths
                    if _get(row["data"], p) != _get(new, p)]
        rows.append(("/".join(key), "; ".join(what)))


def _get(doc: Any, path: str) -> Any:
    for part in path.replace("[*]", "").split("."):
        if isinstance(doc, list):
            doc = [d.get(part) if isinstance(d, dict) else None for d in doc]
        elif isinstance(doc, dict):
            doc = doc.get(part)
        else:
            return None
    return doc


# ── SQLite ────────────────────────────────────────────────────────────────


def sqlite_path(target: Target) -> str | None:
    """The file ``target``'s store opens: its own ``CAIRN_*_DB``, else the shared default.
    The run store is the exception: a ``sqlite:`` ``CAIRN_STORAGE`` URL, else
    ``CAIRN_RUN_DB``, else in-memory (nothing durable to respell)."""
    from cairn.core import settings  # noqa: PLC0415 - cycle: settings ↔ storage

    if target.table == "runs":
        url = getenv("CAIRN_STORAGE", "").strip()
        return (settings.sqlite_path_from_url(url) if url.startswith("sqlite") else None) or settings.run_db_path()
    if target.table == "inbox":   # the trigger inbox keeps its own default file
        import os  # noqa: PLC0415

        return getenv(target.db_env, "").strip() or os.path.expanduser("~/.cairn/trigger_inbox.db")
    return getenv(target.db_env, "").strip() or settings.default_sqlite_db()


def _pages_sqlite(conn: sqlite3.Connection, target: Target):
    cols = list(dict.fromkeys((*target.key, *target.columns, *target.extra, "data")))
    order = ", ".join(target.key)
    last: tuple | None = None
    while True:
        where = "" if last is None else f"WHERE ({order}) > ({', '.join('?' * len(target.key))})"
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {target.table} {where} ORDER BY {order} LIMIT {PAGE}",
                            last or ()).fetchall()
        if not rows:
            return
        for values in rows:
            yield dict(zip(cols, values))
        last = tuple(rows[-1][: len(target.key)])


def respell_sqlite(conn: sqlite3.Connection, target: Target, report: Report, *, apply: bool) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target.table,)).fetchone() is None:
        report.missing.append(target.table)
        return
    report.scanned[target.table] = report.changed[target.table] = 0
    pending: list[tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]] = []
    for raw in _pages_sqlite(conn, target):
        report.scanned[target.table] += 1
        row = {**raw, "data": json.loads(raw["data"])}
        changes, new_key = plan_row(target, row)
        if not changes:
            continue
        key = tuple(raw[k] for k in target.key)
        _note(report, target, key, new_key, row, changes)
        pending.append((raw, changes, new_key))
    if not apply:
        return
    where = " AND ".join(f"{k} = ?" for k in target.key)
    for raw, changes, new_key in pending:
        key = tuple(raw[k] for k in target.key)
        with conn:
            if new_key != key and conn.execute(f"SELECT 1 FROM {target.table} WHERE {where}", new_key).fetchone():
                report.conflicts.append(f"{target.table}: {'/'.join(key)} → {'/'.join(new_key)} exists")
                continue
            sets = dict(changes)
            sets.update({k: v for k, v, o in zip(target.key, new_key, key) if v != o})
            cur = conn.execute(
                f"UPDATE {target.table} SET {', '.join(f'{c} = ?' for c in sets)} WHERE {where} AND data = ?",
                (*sets.values(), *key, raw["data"]))
            if cur.rowcount != 1:
                report.raced.append(f"{target.table}: {'/'.join(key)}")


# ── Postgres ──────────────────────────────────────────────────────────────


async def respell_postgres(dsn: str, report: Report, *, apply: bool) -> None:
    import asyncpg  # noqa: PLC0415 - the storage-postgres extra

    conn = await asyncpg.connect(dsn)
    try:
        for target in TARGETS:
            if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", target.table):
                report.missing.append(target.table)
                continue
            report.scanned[target.table] = report.changed[target.table] = 0
            cols = list(dict.fromkeys((*target.key, *target.columns, *target.extra)))
            order = ", ".join(target.key)
            pending = []
            last: tuple | None = None
            while True:
                n = len(target.key)
                where = "" if last is None else f"WHERE ({order}) > ({', '.join(f'${i}' for i in range(1, n + 1))})"
                recs = await conn.fetch(f"SELECT {', '.join(cols)}, data::text AS data FROM {target.table} "
                                        f"{where} ORDER BY {order} LIMIT {PAGE}", *(last or ()))
                if not recs:
                    break
                for rec in recs:
                    raw = dict(rec)
                    report.scanned[target.table] += 1
                    row = {**raw, "data": json.loads(raw["data"])}
                    changes, new_key = plan_row(target, row)
                    if changes:
                        key = tuple(raw[k] for k in target.key)
                        _note(report, target, key, new_key, row, changes)
                        pending.append((raw, changes, new_key))
                last = tuple(recs[-1][k] for k in target.key)
            if not apply:
                continue
            for raw, changes, new_key in pending:
                key = tuple(raw[k] for k in target.key)
                async with conn.transaction():
                    kw = " AND ".join(f"{k} = ${i}" for i, k in enumerate(target.key, 1))
                    if new_key != key and await conn.fetchval(f"SELECT 1 FROM {target.table} WHERE {kw}", *new_key):
                        report.conflicts.append(f"{target.table}: {'/'.join(key)} → {'/'.join(new_key)} exists")
                        continue
                    sets = dict(changes)
                    sets.update({k: v for k, v, o in zip(target.key, new_key, key) if v != o})
                    names = list(sets)
                    assign = ", ".join(f"{c} = ${i}{'::jsonb' if c == 'data' else ''}" for i, c in enumerate(names, 1))
                    base = len(names)
                    cond = " AND ".join(f"{k} = ${base + i}" for i, k in enumerate(target.key, 1))
                    status = await conn.execute(
                        f"UPDATE {target.table} SET {assign} WHERE {cond} AND data = ${base + len(key) + 1}::jsonb",
                        *(sets[c] for c in names), *key, raw["data"])
                    if status != "UPDATE 1":
                        report.raced.append(f"{target.table}: {'/'.join(key)}")
    finally:
        await conn.close()


# ── the command's entry point ─────────────────────────────────────────────


def respell_packs(*, apply: bool = False) -> Report:
    """Respell every target table where this environment's stores keep it."""
    report = Report(applied=apply)
    url = getenv("CAIRN_STORAGE", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        import asyncio  # noqa: PLC0415

        asyncio.run(respell_postgres(url, report, apply=apply))
        return report
    by_path: dict[str, list[Target]] = {}
    for target in TARGETS:
        path = sqlite_path(target)
        if not path or path == ":memory:":
            report.missing.append(f"{target.table} (in-memory store: nothing recorded)")
            continue
        by_path.setdefault(path, []).append(target)
    for path, targets in by_path.items():
        conn = sqlite3.connect(path)
        try:
            for target in targets:
                respell_sqlite(conn, target, report, apply=apply)
        finally:
            conn.close()
    return report
