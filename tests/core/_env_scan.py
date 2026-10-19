"""The collector behind the settings-inventory gates (#2412), shared by the public half
(``test_env_inventory.py``) and the platform half (``test_env_inventory_platform.py``) since
the tree splits in two (#2524). It reads source with ``ast``; nothing here imports cairn
except ``settings`` in :func:`_inventory`.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path

PREFIX, LEGACY = "CAIRN_", "OBS_"

#: Names born ``CAIRN_*`` — never spelled ``OBS_*``, so outside the #2412 inventory: the
#: job/box environment a submit hands a worker (run identity, specs, training knobs) and a
#: few internal knobs. Shrink-only: a new cairn variable gets an inventory row instead.
NATIVE = frozenset({
    "CAIRN_APPROVAL_SWEEP_MINUTES", "CAIRN_APPROVAL_TTL_HOURS", "CAIRN_ARTIFACT_RELAY_MAX_BYTES",
    "CAIRN_ARTIFACT_TARGET", "CAIRN_BASE_MODEL", "CAIRN_BATCH_MAX_WORKERS", "CAIRN_BATCH_NICE",
    "CAIRN_BATCH_RESERVED_CORES", "CAIRN_BATCH_THRESHOLD_ROWS", "CAIRN_BUS_RETENTION_DAYS",
    "CAIRN_CAPTURE", "CAIRN_CAPTURE_MAX_BYTES", "CAIRN_CAPTURE_MAX_FILES",
    "CAIRN_CHECKPOINT_BUCKET", "CAIRN_CHECKPOINT_DIR", "CAIRN_CHECKPOINT_PREFIX",
    "CAIRN_CONN_*", "CAIRN_CONN_*_*", "CAIRN_DATAFLOW_OWNER_PID", "CAIRN_DATAFLOW_SPEC",
    "CAIRN_DATASET_PATH",
    "CAIRN_DATASET_URI", "CAIRN_DIST_RANK", "CAIRN_DIST_WORLD", "CAIRN_EC2_CLIENT_FACTORY",
    "CAIRN_ENGINE", "CAIRN_ENGINE_TOKEN", "CAIRN_EPOCHS", "CAIRN_EVAL_SPLIT",
    "CAIRN_FIX_SPEC", "CAIRN_HF_REPO", "CAIRN_LEARNING_RATE", "CAIRN_LIVE_SPOOL",
    "CAIRN_LORA_ALPHA", "CAIRN_LORA_R", "CAIRN_MANAGED_POLL_S", "CAIRN_MAX_SEQ_LEN",
    "CAIRN_MAX_STEPS", "CAIRN_METHOD", "CAIRN_MLFLOW_TRACKING_URI", "CAIRN_NODE",
    "CAIRN_OUTPUT_NAME", "CAIRN_PACK_PATH", "CAIRN_PARENT_RUN", "CAIRN_PII_ANALYZE_CHUNK",
    "CAIRN_PII_BATCH_SIZE", "CAIRN_PII_CACHE_MAX", "CAIRN_PII_N_PROCESS", "CAIRN_PII_SPACY_MODEL",
    "CAIRN_PIN_IMAGE_DIGEST", "CAIRN_PROBE_ALLOW_PRIVATE", "CAIRN_RECONCILE_GRACE_SECONDS",
    "CAIRN_RECONCILE_HEARTBEAT_TTL_SECONDS", "CAIRN_RECONCILE_SWEEP_SECONDS", "CAIRN_REMOTE_JOBS",
    "CAIRN_REPORT_STRICT", "CAIRN_REPORT_TO", "CAIRN_RETENTION_SWEEP_HOURS", "CAIRN_RUN_ID",
    "CAIRN_SAVE_STEPS", "CAIRN_SEED", "CAIRN_SKY_SDK_FACTORY", "CAIRN_SSH_CONNECT_FACTORY",
    "CAIRN_STAGE_DIR", "CAIRN_SUBMIT_CWD", "CAIRN_SYSMETRICS", "CAIRN_SYSMETRICS_INTERVAL_S",
    "CAIRN_TRACKING_PROJECT", "CAIRN_TRACKING_RUN_NAME", "CAIRN_TRAIN_IGNORE_RAM",
    "CAIRN_TRAIN_SPEC", "CAIRN_TRAIN_STUB", "CAIRN_TRAIN_VALIDATE", "CAIRN_TRAIN_VALIDATE_SECONDS",
})

#: The only ``OBS_*`` literals left in the code, each with its reason (ADR-0122, #2429).
LEGACY_LITERALS: dict[str, str] = {
    # the resolver's own fallback prefix is "OBS_" itself (not a name; never collected)
    "OBS_BACKEND": "provider_settings' pydantic alias: the .env file the process env lookup cannot see",
    "OBS_METAFLOW_*": "the Metaflow engine (cairn-operator-dataflow-metaflow) forwards the former prefix to a flow task until 1.0",
    # live.py (stdlib-only) mirrors the resolver's fallback with the prefix itself, so it
    # holds no OBS_ name; these two are what the control plane WRITES for old box images.
    "OBS_JOB_CHANNEL": "the box wire twin of CAIRN_JOB_CHANNEL: a pinned train-gpu image's "
                       "live.py reads only this name; goes at 1.0 with the rebuilt image (#2429)",
    "OBS_JOB_CHANNEL_STREAM": "the box wire twin of CAIRN_JOB_CHANNEL_STREAM, beside "
                              "OBS_JOB_CHANNEL; goes at 1.0 with the rebuilt image (#2429)",
}


def _docstrings(nodes: list[ast.AST]) -> set[int]:
    out: set[int] = set()
    for node in nodes:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _shape(node: ast.JoinedStr, prefix: str = PREFIX) -> str | None:
    """``f"CAIRN_FEATURE_{name.upper()}"`` → ``CAIRN_FEATURE_*``; None when not a name."""
    shape = "".join(v.value if isinstance(v, ast.Constant) else "*" for v in node.values)
    return shape if re.match(prefix + r"[A-Z0-9_*]+\Z", shape) else None


def names_read_in_source(src: str, prefix: str = PREFIX) -> list[tuple[str, int]]:
    """``(name or glob, line)`` for every ``<prefix>*`` name this source reads."""
    exact = re.compile(prefix + r"[A-Z0-9]+(?:_[A-Z0-9]+)*_?\Z")
    nodes = list(ast.walk(ast.parse(src)))  # one walk; every pass below reuses it
    skip = _docstrings(nodes)
    for node in nodes:
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    out: list[tuple[str, int]] = []

    # A built prefix completed by concatenation: ``prefix = f"CAIRN_MCP_{n}_"`` then
    # ``prefix + "URL"`` reads ``CAIRN_MCP_*_URL`` (not the open ``CAIRN_MCP_*_*``).
    prefixes: dict[str, ast.JoinedStr] = {}
    for node in nodes:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.JoinedStr)):
            shape = _shape(node.value, prefix)
            if shape and shape.endswith("_"):
                prefixes[node.targets[0].id] = node.value
    for node in nodes:
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.Name) and node.left.id in prefixes
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            out.append((_shape(prefixes[node.left.id], prefix) + node.right.value, node.lineno))
            skip.add(id(prefixes[node.left.id]))

    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            name = node.value
            if name != prefix and exact.match(name):
                out.append((name + "*" if name.endswith("_") else name, node.lineno))
        elif isinstance(node, ast.JoinedStr) and id(node) not in skip:
            shape = _shape(node, prefix)
            if shape:
                out.append((shape + "*" if shape.endswith("_") else shape, node.lineno))
        elif isinstance(node, ast.ClassDef) and any(
                ast.unparse(b).endswith("BaseSettings") for b in node.bases):
            # pydantic-settings reads a field ``obs_backend`` through its alias ``CAIRN_BACKEND``.
            for stmt in node.body:
                if (prefix == PREFIX and isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name) and stmt.target.id.startswith("obs_")):
                    out.append((PREFIX + stmt.target.id[len("obs_"):].upper(), stmt.lineno))
    return out


@lru_cache(maxsize=None)
def collect_reads(roots: tuple[Path, ...], prefix: str = PREFIX) -> dict[str, list[str]]:
    """``{name or glob: ["path:line", …]}`` over ``roots`` (source directories), tests excluded."""
    reads: dict[str, list[str]] = defaultdict(list)
    for top in roots:
        for path in sorted(top.rglob("*.py")):
            rel = path.relative_to(top.parent)
            if "tests" in rel.parts:
                continue
            src = path.read_text(encoding="utf-8")
            if prefix not in src and "obs_" not in src:
                continue  # nothing to find; skipping the parse keeps the gate fast
            for name, line in names_read_in_source(src, prefix):
                reads[name].append(f"{rel}:{line}")
    return dict(reads)


def missing_rows(reads: dict[str, list[str]], inventory: set[str]) -> dict[str, list[str]]:
    """Read names (and globs) with no inventory row."""
    out: dict[str, list[str]] = {}
    for name, sites in reads.items():
        if "*" in name:
            covered = any(fnmatchcase(row, name) for row in inventory)
        else:
            covered = name in inventory
        if not covered:
            out[name] = sites
    return out


def dead_rows(reads: dict[str, list[str]], rows: dict[str, str]) -> list[str]:
    """Cairn inventory rows nothing reads; ``rows`` is ``{env_var: description}``."""
    globs = [n for n in reads if "*" in n]
    return sorted(
        row for row, description in rows.items()
        if row.startswith((PREFIX, LEGACY))
        and not description.startswith("DEPRECATED")
        and row not in reads
        and not any(fnmatchcase(row, g) for g in globs)
    )


def _inventory() -> dict[str, str]:
    from cairn.core import settings

    return {spec.env_var: spec.description for spec in settings.INVENTORY}
