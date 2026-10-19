"""Shardable operator contract + the sharded executor (#962).

The generic, workflow-agnostic layer that turns a CPU-bound dataset operator into
a sharded, multi-core, non-blocking job. An operator opts in by subclassing
:class:`Shardable` and implementing three methods:

  - ``shard_total(inp)``  — how many rows (drives the threshold + the row ranges).
  - ``run_shard(inp, spec)`` — process rows ``[spec.start:spec.end)``, return a
    picklable partial result. Pure compute: reads a range, writes its own shard
    output (if any), returns counts. No run-store / cost-meter access.
  - ``merge(inp, parts)`` — fan-in: combine the shard partials into the operator's
    Output (sum counts, concat shard files, …).

:func:`run_sharded` (called from the operator runtime when a Shardable op exceeds
``CAIRN_BATCH_THRESHOLD_ROWS``) plans the ranges, dispatches the shards through
:func:`cairn.core.operator.batch.map_shards_local` (a budgeted, nice'd process
pool — never all cores, container-aware), and merges. Below the threshold the
runtime just calls the operator's normal ``run`` — small datasets pay no tax.

Picklability: a ProcessPool ships work to children by pickle, and a live operator
instance / OperationContext generally will not pickle. So a shard is a plain
:class:`ShardTask` (operator NAME + input JSON + row range); ``_child_run_shard``
re-resolves the operator from the registry **inside the child** and calls
``run_shard`` there. This is why ``run_shard`` must be self-contained compute.

C (this module, local pool) bounds + de-prioritizes batch CPU but still shares the
box. A (a dedicated ``prod-worker-dataset`` container, ``reserved=0``) is the
physical isolation — it will reuse this same contract with a Temporal-activity
dispatcher instead of the local pool. Same ``Shardable``, swappable dispatch.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
import pickle
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cairn.core.operator.batch import map_shards_local
from cairn.core.operator.registry import default_registry

_ENV_THRESHOLD = "CAIRN_BATCH_THRESHOLD_ROWS"
DEFAULT_THRESHOLD_ROWS = 50_000

# ── durable stage store (#1010, ADR-0078 S4) ─────────────────────────────────
# A sharded run stages its work under a DETERMINISTIC dir keyed by a config
# digest, committing a manifest entry (+ pickled partial) per completed shard —
# so a crashed run's re-invocation skips finished shards instead of redoing
# everything. The dir is removed only after a successful merge.
_ENV_STAGE_DIR = "CAIRN_STAGE_DIR"
_STAGE_GC_SECONDS = 7 * 24 * 3600  # orphaned stage dirs older than this are GC'd
_MANIFEST = "manifest.json"


def threshold_rows() -> int:
    raw = os.environ.get(_ENV_THRESHOLD)
    if raw is None or raw.strip() == "":
        return DEFAULT_THRESHOLD_ROWS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_THRESHOLD_ROWS


@dataclass(frozen=True)
class ShardSpec:
    """A unit of work handed to ``run_shard`` — plain, picklable data.

    ``[start, end)`` is a 0-based half-open row range over the operator's input
    dataset. ``output_dir`` is where a writing operator drops its shard file
    (``None`` for read-only operators like pii_scan).
    """
    index: int
    start: int
    end: int
    output_dir: Optional[str] = None


@dataclass(frozen=True)
class ShardTask:
    """Everything a child process needs to run one shard — fully picklable."""
    operator_name: str
    input_json: str
    spec: ShardSpec


class Shardable(abc.ABC):
    """Mixin an :class:`Operation` inherits to opt into batch sharding (#962)."""

    #: sharding only applies to real runs (never mock/sandbox), gated by
    #: ``shard_total(inp) >= threshold_rows()``.
    supports_sharding: bool = True

    @abc.abstractmethod
    def shard_total(self, inp: Any) -> int:
        """Total shardable rows in ``inp`` — drives the threshold + row ranges."""

    @abc.abstractmethod
    def run_shard(self, inp: Any, spec: ShardSpec) -> Any:
        """Process rows ``[spec.start:spec.end)``; return a picklable partial."""

    @abc.abstractmethod
    def merge(self, inp: Any, parts: list[Any]) -> Any:
        """Combine shard partials (in shard order) into the operator Output."""


def shard_ranges(total: int, k: int) -> list[tuple[int, int]]:
    """Split ``total`` rows into ``k`` contiguous, near-equal ``[start, end)``
    ranges. Extra rows go to the earliest shards. Never yields an empty range."""
    if total <= 0 or k <= 0:
        return []
    k = min(k, total)
    base, extra = divmod(total, k)
    ranges: list[tuple[int, int]] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        ranges.append((start, start + size))
        start += size
    return ranges


_LOADED = False


def _ensure_operators_loaded() -> None:
    """Populate the registry in a child process. Under ``fork`` the child inherits
    the parent's registry (no-op); under ``spawn`` (macOS/Windows) it must reload."""
    global _LOADED
    # Keyed on whether a full load was ATTEMPTED (#1500), not on a probe operator:
    # spawn platforms start empty and a fork parent may hold only a partial registry
    # (#1018), and core names no operator pack's op as the probe (ADR-0120 D5, #2415).
    if _LOADED or default_registry.load_attempted:
        _LOADED = True
        return
    try:
        from cairn.core.operator.loader import load_all_operators
        load_all_operators(strict=False)
    except Exception:  # noqa: BLE001 — best effort; a missing op surfaces at .get() below
        pass
    _LOADED = True


def _child_run_shard(task: ShardTask) -> Any:
    """Module-level (picklable) child entry point: re-resolve the operator by name
    and run its shard. Runs in a pool worker process, not the parent."""
    _ensure_operators_loaded()
    if not default_registry.has(task.operator_name):
        raise RuntimeError(
            f"sharded exec: operator {task.operator_name!r} not found in child registry"
        )
    op_cls = default_registry.get(task.operator_name)
    inp = op_cls.input_schema.model_validate_json(task.input_json)
    op = op_cls()
    return op.run_shard(inp, task.spec)


def should_shard(op: Any, inp: Any) -> tuple[bool, int]:
    """(shard?, total) — True when ``op`` is Shardable and big enough to bother.
    Returns the total so the caller doesn't recount the dataset."""
    if not isinstance(op, Shardable) or not getattr(op, "supports_sharding", False):
        return (False, 0)
    total = op.shard_total(inp)
    # total <= 0 is the operator's "don't shard me" signal (e.g. pii_scan's
    # sampled path, or an empty dataset) — never shard it, even if the threshold
    # is 0. Otherwise shard once the row count reaches the threshold.
    return (total > 0 and total >= threshold_rows(), total)


def stage_root() -> Path:
    """Root of the durable stage store (#1010). ``CAIRN_STAGE_DIR`` overrides;
    default ``~/.cairn/stage``."""
    raw = os.environ.get(_ENV_STAGE_DIR)
    return Path(raw) if raw else Path.home() / ".cairn" / "stage"


def _source_stamp(inp: Any) -> list:
    """(size, mtime_ns) of the input's dataset file(s), when it has any.

    Folded into the config digest so editing the SOURCE FILE in place between
    runs changes the digest — stale shard partials from the old content are
    never reused for the new content. Best-effort and generic: dataset inputs
    conventionally carry ``path`` / ``eval_path`` string fields."""
    stamp: list = []
    for attr in ("path", "eval_path"):
        v = getattr(inp, attr, None)
        if isinstance(v, str) and v:
            try:
                st = os.stat(v)
                stamp.append([attr, st.st_size, st.st_mtime_ns])
            except OSError:
                stamp.append([attr, None, None])  # remote/Hub source — digest on the ref alone
    return stamp


def _config_digest(op_name: str, input_json: str, ranges: list, stamp: list) -> str:
    payload = json.dumps(
        {"op": op_name, "input": input_json, "ranges": ranges, "src": stamp},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _write_manifest(workdir: Path, manifest: dict) -> None:
    """Atomic manifest commit (tmp + os.replace) — a crash mid-write leaves the
    previous manifest, never a truncated one."""
    tmp = workdir / f"{_MANIFEST}.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    os.replace(tmp, workdir / _MANIFEST)


def _load_manifest(workdir: Path, digest: str) -> dict:
    """The prior run's manifest when it exists AND matches this config digest;
    ``{}`` otherwise (fresh run, or the config/source changed)."""
    try:
        m = json.loads((workdir / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return m if m.get("digest") == digest else {}


def _gc_stale_stages(root: Path, keep: Path) -> None:
    """Remove orphaned stage dirs older than the GC window (a crashed run whose
    config never re-ran). Best-effort; never touches the active dir."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    cutoff = time.time() - _STAGE_GC_SECONDS
    for d in entries:
        if d == keep or not d.is_dir():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def run_sharded(op: Any, inp: Any, total: int) -> Any:
    """Plan → dispatch (budgeted nice'd pool) → merge. SYNCHRONOUS and blocking —
    the caller runs it in a thread so the worker's event loop stays free while the
    child processes burn CPU. ``total`` comes from :func:`should_shard``.

    DURABLE + RESUMABLE (#1010, ADR-0078 S4). The workdir is no longer a
    throwaway tempdir: it is a DETERMINISTIC stage dir keyed by
    ``hash(operator + input + shard plan + source-file stamp)`` under
    :func:`stage_root`. As each shard completes, its partial result is pickled
    to ``part_NNNNN.pkl`` and recorded in an atomically-committed
    ``manifest.json`` — so when a run dies at 90%, the re-invocation loads the
    finished shards from the manifest and dispatches ONLY the missing ones.
    Shard-written files (e.g. anonymize's ``shard_i.jsonl``) live in the same
    dir and survive with it. The dir is removed only after a successful
    ``merge``; a failed merge keeps it for the retry. Editing the source file
    in place changes the digest, so stale partials are never reused. Caveat: a
    re-run with a different worker budget re-plans the ranges → new digest →
    fresh run (the plan is part of the identity).

    On the local dispatcher (C) the stage root is on the local FS; the
    Temporal/Ray dispatcher (A) will point it at shared storage instead.
    """
    from cairn.core.operator.batch import resolve_batch_workers

    k = resolve_batch_workers()  # honors headroom; the pool caps concurrency anyway
    ranges = shard_ranges(total, k)
    op_name = getattr(op, "name")
    input_json = inp.model_dump_json()
    digest = _config_digest(op_name, input_json, [list(r) for r in ranges], _source_stamp(inp))
    workdir = stage_root() / digest
    workdir.mkdir(parents=True, exist_ok=True)
    _gc_stale_stages(stage_root(), keep=workdir)

    manifest = _load_manifest(workdir, digest)
    shards_done: dict = manifest.get("shards", {})
    parts: list = [None] * len(ranges)
    todo: list[int] = []
    for i in range(len(ranges)):
        entry = shards_done.get(str(i))
        part_file = workdir / entry["part"] if entry else None
        if part_file is not None and part_file.exists():
            # trusted local cache this process (or its crashed predecessor) wrote
            with part_file.open("rb") as fh:
                parts[i] = pickle.load(fh)
        else:
            todo.append(i)

    if todo:
        manifest = {"digest": digest, "op": op_name, "shards": shards_done}
        tasks = [
            ShardTask(op_name, input_json, ShardSpec(i, ranges[i][0], ranges[i][1], str(workdir)))
            for i in todo
        ]

        def _checkpoint(pos: int, result: Any) -> None:
            """Per-shard commit: pickle the partial, then atomically record it."""
            i = todo[pos]
            part_name = f"part_{i:05d}.pkl"
            tmp = workdir / f"{part_name}.tmp-{os.getpid()}"
            with tmp.open("wb") as fh:
                pickle.dump(result, fh)
            os.replace(tmp, workdir / part_name)
            shards_done[str(i)] = {"start": ranges[i][0], "end": ranges[i][1], "part": part_name}
            _write_manifest(workdir, manifest)

        fresh = map_shards_local(_child_run_shard, tasks, on_result=_checkpoint)
        for pos, i in enumerate(todo):
            parts[i] = fresh[pos]

    out = op.merge(inp, parts)  # reads shard files from the stage dir
    shutil.rmtree(workdir, ignore_errors=True)  # only after a SUCCESSFUL merge
    return out


__all__ = [
    "Shardable",
    "ShardSpec",
    "ShardTask",
    "shard_ranges",
    "should_shard",
    "run_sharded",
    "stage_root",
    "threshold_rows",
    "DEFAULT_THRESHOLD_ROWS",
]
