"""#1539 parts 1+3 — the operator CLASS is the single source of truth.

Adding one operator used to touch three declaration systems restating the
same facts: the ``@register_operator`` class, a ~13-line
``[operator."<n>"]`` table in ``pack.toml``, and a ``cairn.operators``
entry point in ``pyproject.toml`` — with nothing keeping them in sync.
The lifecycle flags lived as BOTH class attrs and TOML strings.

This module closes the loop the other way around: the class (which now
also carries ``risk`` / ``executors`` / ``requires_approval`` /
``supports_dry_run|verify|compensate`` / ``mcp_server|mcp_tool``, see
``protocol.Operation``) DERIVES the table, and:

  - ``cairn pack sync <pack-dir>`` writes missing/stale tables and
    entry-point lines (table-level rewrites, so the narrative comments
    between tables survive);
  - ``cairn doctor`` runs :func:`check_metadata_parity` — a SEMANTIC
    diff of every attached manifest entry against its class — so a
    hand-edited stale ``pack.toml`` fails loud instead of shipping.

The manifest stays the on-disk RECORD (doctor/console/registry walkers
read it without importing operator code); it just can no longer drift.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Type

from cairn.core.operator.protocol import Operation
from cairn.core.operator.registry import default_registry
from cairn.core.operator.reports import ReportSpec
from cairn.core.pack.manifest import (
    _OPERATOR_EXECUTOR_VALUES,
    _OPERATOR_RISK_VALUES,
    OperatorEntry,
)

logger = logging.getLogger("cairn.operator.manifest_sync")

# ─── derive: class → canonical table ───────────────────────────────────


def derive_entry(op_cls: Type[Operation]) -> OperatorEntry:
    """The ``[operator."<n>"]`` entry this class defines.

    Raises ``ValueError`` when the class declares values outside the locked
    enums — generation must never emit a table load_manifest would reject.
    """
    name = getattr(op_cls, "name", None)
    if not name:
        raise ValueError(f"{op_cls.__name__} has no `name` — not decorated?")
    risk = op_cls.risk
    if risk not in _OPERATOR_RISK_VALUES:
        raise ValueError(
            f"{name}: risk {risk!r} is not in the locked enum "
            f"{sorted(_OPERATOR_RISK_VALUES)!r}"
        )
    executors = list(op_cls.executors)
    bad = [e for e in executors if e not in _OPERATOR_EXECUTOR_VALUES]
    if bad:
        raise ValueError(
            f"{name}: executors {bad!r} not in the locked enum "
            f"{sorted(_OPERATOR_EXECUTOR_VALUES)!r}"
        )
    if (
        "mcp_tool" in executors
        and "in_process" not in executors
        and not (op_cls.mcp_server and op_cls.mcp_tool)
    ):
        raise ValueError(
            f"{name}: mcp_tool-only operator must set mcp_server + mcp_tool"
        )
    # ADR-0103 — the reporting declaration is REQUIRED, like risk. Absence is
    # the signal: every operator declares it on the class (#2415).
    spec = getattr(op_cls, "reports", None)
    if spec is None:
        raise ValueError(
            f"{name}: no `reports` declaration — set `reports = ReportSpec.NONE` "
            f"or a ReportSpec(stages=…, series=…, scalars=…) on the class (ADR-0103)"
        )
    if not isinstance(spec, ReportSpec):
        raise ValueError(f"{name}: `reports` must be a ReportSpec, got {type(spec).__name__}")
    reports_entry = None if spec.is_none else spec
    return OperatorEntry(
        name=name,
        version=op_cls.version,
        input_schema=op_cls.input_schema.__name__,
        output_schema=op_cls.output_schema.__name__,
        long_running=bool(op_cls.long_running),
        requires_gpu=bool(op_cls.requires_gpu),
        supports_cancel=bool(op_cls.supports_cancel),
        supports_heartbeat=bool(op_cls.supports_heartbeat),
        supports_mock=bool(op_cls.supports_mock),
        risk=risk,
        executors=executors,
        requires_approval=bool(op_cls.requires_approval),
        supports_dry_run=bool(op_cls.supports_dry_run),
        supports_verify=bool(op_cls.supports_verify),
        supports_compensate=bool(op_cls.supports_compensate),
        provides_capabilities=list(getattr(op_cls, "capabilities", []) or []),
        implements=list(getattr(op_cls, "implements", ()) or ()),
        mcp_server=op_cls.mcp_server,
        mcp_tool=op_cls.mcp_tool,
        reports=reports_entry,
    )


def entry_drift(declared: OperatorEntry, derived: OperatorEntry) -> list[str]:
    """Field-level mismatches between the on-disk table and the class.

    Returns ``["field: declared=… class=…", …]``; empty = in sync.
    ``executors`` compares ordered (the runtime walks it in order);
    ``provides_capabilities`` compares as a set (order is meaningless).
    """
    diffs: list[str] = []

    def _cmp(fname: str, a: Any, b: Any) -> None:
        if a != b:
            diffs.append(f"{fname}: declared={a!r} class={b!r}")

    _cmp("version", declared.version, derived.version)
    _cmp("input_schema", declared.input_schema, derived.input_schema)
    _cmp("output_schema", declared.output_schema, derived.output_schema)
    for flag in (
        "long_running", "requires_gpu", "supports_cancel",
        "supports_heartbeat", "supports_mock", "requires_approval",
        "supports_dry_run", "supports_verify", "supports_compensate",
    ):
        _cmp(flag, getattr(declared, flag), getattr(derived, flag))
    _cmp("risk", declared.risk, derived.risk)
    _cmp("executors", list(declared.executors), list(derived.executors))
    if set(declared.provides_capabilities) != set(derived.provides_capabilities):
        diffs.append(
            f"capabilities.provides: declared={sorted(declared.provides_capabilities)!r} "
            f"class={sorted(derived.provides_capabilities)!r}"
        )
    _cmp("implements", list(declared.implements), list(derived.implements))  # ordered: revisions are exact
    _cmp("mcp.server", declared.mcp_server, derived.mcp_server)
    _cmp("mcp.tool", declared.mcp_tool, derived.mcp_tool)
    # ADR-0103 — an absent table and an explicit NONE are the same declaration.
    d_rep = (declared.reports or ReportSpec.NONE).to_manifest()
    c_rep = (derived.reports or ReportSpec.NONE).to_manifest()
    if d_rep != c_rep:
        diffs.append(f"reports: declared={d_rep!r} class={c_rep!r}")
    return diffs


def _toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_operator_table(op_cls: Type[Operation]) -> str:
    """Canonical TOML block for one operator (header + sub-tables)."""
    e = derive_entry(op_cls)
    lines = [f'[operator."{e.name}"]']
    lines.append(f"version       = {_toml_str(e.version or '')}")
    lines.append(f"input_schema  = {_toml_str(e.input_schema or '')}")
    lines.append(f"output_schema = {_toml_str(e.output_schema or '')}")
    for flag in ("long_running", "requires_gpu", "supports_cancel",
                 "supports_heartbeat", "supports_mock"):
        lines.append(f"{flag} = {str(getattr(e, flag)).lower()}")
    lines.append(f"risk      = {_toml_str(e.risk or 'read_only')}")
    lines.append(
        "executors = [" + ", ".join(_toml_str(x) for x in e.executors) + "]")
    for flag in ("requires_approval", "supports_dry_run",
                 "supports_verify", "supports_compensate"):
        if getattr(e, flag):  # defaults are false — only emit the exceptions
            lines.append(f"{flag} = true")
    if e.implements:  # #2241 B — legacy operators emit nothing here
        lines.append("implements = [" + ", ".join(_toml_str(r) for r in e.implements) + "]")
    block = "\n".join(lines)
    if e.provides_capabilities:
        block += (
            f'\n\n[operator."{e.name}".capabilities]\nprovides = ['
            + ", ".join(_toml_str(c) for c in e.provides_capabilities) + "]"
        )
    if e.mcp_server or e.mcp_tool:
        block += f'\n\n[operator."{e.name}".mcp]'
        if e.mcp_server:
            block += f"\nserver = {_toml_str(e.mcp_server)}"
        if e.mcp_tool:
            block += f"\ntool   = {_toml_str(e.mcp_tool)}"
    if e.reports is not None and not e.reports.is_none:  # ADR-0103
        block += f'\n\n[operator."{e.name}".reports]'
        if e.reports.stages:
            block += "\nstages  = [" + ", ".join(_toml_str(s) for s in e.reports.stages) + "]"
        if e.reports.series:
            block += "\nseries  = [" + ", ".join(
                "{ name = " + _toml_str(s.name) + ", unit = " + _toml_str(s.unit) + " }"
                for s in e.reports.series) + "]"
        if e.reports.scalars:
            block += "\nscalars = [" + ", ".join(_toml_str(s) for s in e.reports.scalars) + "]"
    return block + "\n"


# ─── discover: walk an operator pack's source package ──────────────────


def discover_pack_operators(pack_dir: Path) -> tuple[dict[str, Type[Operation]], list[str]]:
    """Import every module under ``<pack_dir>/src/<package>/`` and return
    ``{op_name: class}`` for the operators DEFINED in that package, plus a
    list of module-import problems (a module that won't import means its
    operators are invisible — reported, never silently skipped).
    """
    src = pack_dir / "src"
    pkgs = [p for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists()] \
        if src.is_dir() else []
    if not pkgs:
        return {}, [f"no src/<package>/ found under {pack_dir}"]

    problems: list[str] = []
    found: dict[str, Type[Operation]] = {}
    for pkg_root in pkgs:
        pkg = pkg_root.name
        for py in sorted(pkg_root.rglob("*.py")):
            rel = py.relative_to(pkg_root).with_suffix("")
            parts = [pkg, *rel.parts]
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mod_name = ".".join(parts)
            try:
                importlib.import_module(mod_name)
            except Exception as exc:  # noqa: BLE001 — report and keep walking
                problems.append(f"{mod_name}: {type(exc).__name__}: {exc}")
        for name, cls in default_registry.all().items():
            if (cls.__module__ or "").split(".")[0] == pkg:
                found[name] = cls
    return found, problems


# ─── check / sync one pack directory ───────────────────────────────────


@dataclass
class SyncReport:
    """What `check_pack` found (and `sync_pack` fixed)."""

    pack_dir: Path
    missing_tables: list[str] = field(default_factory=list)
    drifted: dict[str, list[str]] = field(default_factory=dict)
    orphan_tables: list[str] = field(default_factory=list)
    missing_entry_points: list[str] = field(default_factory=list)
    import_problems: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing_tables or self.drifted
                    or self.missing_entry_points or self.import_problems)


def _entry_point_map(pyproject: Path) -> dict[str, str]:
    import tomllib
    with open(pyproject, "rb") as f:
        raw = tomllib.load(f)
    return dict(
        raw.get("project", {}).get("entry-points", {}).get("cairn.operators", {}))


def check_pack(pack_dir: Path) -> SyncReport:
    """Semantic comparison of one operator pack's three declaration systems."""
    from cairn.core.pack.manifest import load_manifest

    pack_dir = Path(pack_dir).resolve()
    report = SyncReport(pack_dir=pack_dir)
    manifest = load_manifest(pack_dir / "pack.toml")
    classes, problems = discover_pack_operators(pack_dir)
    report.import_problems = problems

    for name, cls in sorted(classes.items()):
        declared = manifest.operators.get(name)
        if declared is None:
            report.missing_tables.append(name)
            continue
        diffs = entry_drift(declared, derive_entry(cls))
        if diffs:
            report.drifted[name] = diffs
    # orphans only when the walk itself was healthy — a failed import must
    # not turn its operators into "orphans"
    if not problems:
        report.orphan_tables = sorted(set(manifest.operators) - set(classes))

    eps = _entry_point_map(pack_dir / "pyproject.toml")
    for name, cls in sorted(classes.items()):
        target = f"{cls.__module__}:{cls.__name__}"
        if eps.get(name) != target:
            report.missing_entry_points.append(f'"{name}" = "{target}"')
    return report


def _replace_or_append_table(toml_text: str, op_name: str, new_block: str) -> str:
    """Replace the ``[operator."<name>"]`` block (header through its
    sub-tables) with ``new_block``, or append it. Line-based so every other
    line — including the narrative comments BETWEEN tables — is untouched.
    """
    lines = toml_text.splitlines()
    prefix = f'[operator."{op_name}"'
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith(prefix)), None)
    if start is None:
        sep = "" if toml_text.endswith("\n\n") else ("\n" if toml_text.endswith("\n") else "\n\n")
        return toml_text + sep + new_block
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and not s.startswith(prefix):
            end = j
            # back off trailing blank/comment lines so they stay with the
            # NEXT table (they document it, not this one)
            while end > start + 1 and (not lines[end - 1].strip()
                                       or lines[end - 1].strip().startswith("#")):
                end -= 1
            break
    return "\n".join([*lines[:start], new_block.rstrip("\n"), *lines[end:]]) + "\n"


def sync_pack(pack_dir: Path) -> SyncReport:
    """Regenerate stale/missing tables + entry points in place."""
    pack_dir = Path(pack_dir).resolve()
    report = check_pack(pack_dir)
    classes, _ = discover_pack_operators(pack_dir)

    to_write = [*report.missing_tables, *report.drifted.keys()]
    if to_write:
        toml_path = pack_dir / "pack.toml"
        text = toml_path.read_text(encoding="utf-8")
        for name in to_write:
            text = _replace_or_append_table(text, name, render_operator_table(classes[name]))
            report.synced.append(f"pack.toml: [operator.\"{name}\"]")
        toml_path.write_text(text, encoding="utf-8")

    if report.missing_entry_points:
        py_path = pack_dir / "pyproject.toml"
        text = py_path.read_text(encoding="utf-8")
        header = '[project.entry-points."cairn.operators"]'
        lines = text.splitlines()
        try:
            at = next(i for i, ln in enumerate(lines) if ln.strip() == header)
        except StopIteration:
            lines += ["", header]
            at = len(lines) - 1
        insert_at = at + 1
        while insert_at < len(lines) and (lines[insert_at].strip().startswith(('"', "#"))
                                          or ("=" in lines[insert_at] and not lines[insert_at].strip().startswith("["))):
            insert_at += 1
        for ep in report.missing_entry_points:
            name = ep.split(" = ")[0]
            # a stale line for the same name must go, not linger above the fix
            lines = [ln for ln in lines[:insert_at] if not ln.strip().startswith(name)] \
                + [ep] + lines[insert_at:]
            report.synced.append(f"pyproject.toml: {ep}")
        py_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return check_pack(pack_dir) if report.synced else report


# ─── doctor half: the governance fields, for pack/drift.py ─────────────


def governance_drift(declared: Any, op_cls: Type[Operation]) -> list[str]:
    """Mismatches on the fields #1105 could NOT check (its "option B" scope
    note): risk / executors / requires_approval / the supports_* flags /
    mcp. #1539 made the class carry them, so ``check_operator_drift``
    (the shipped doctor + CI chokepoint) now compares them via this helper
    — version/capabilities/schemas stay with #1105's existing kinds.
    """
    # getattr throughout: `declared` may be any manifest-shaped object (the
    # #1105 check passes duck-typed entries), and a pre-#1539 third-party
    # class simply has the protocol defaults.
    diffs: list[str] = []
    d_risk = getattr(declared, "risk", None) or "read_only"
    c_risk = getattr(op_cls, "risk", "read_only")
    if d_risk != c_risk:
        diffs.append(f"risk: manifest={d_risk!r} class={c_risk!r}")
    d_exec = list(getattr(declared, "executors", None) or []) or ["in_process"]
    c_exec = list(getattr(op_cls, "executors", None) or ["in_process"])
    if d_exec != c_exec:
        diffs.append(f"executors: manifest={d_exec!r} class={c_exec!r}")
    for flag in (
        "long_running", "requires_gpu", "supports_cancel",
        "supports_heartbeat", "supports_mock", "requires_approval",
        "supports_dry_run", "supports_verify", "supports_compensate",
    ):
        d_val = bool(getattr(declared, flag, False))
        c_val = bool(getattr(op_cls, flag, False))
        if d_val != c_val:
            diffs.append(f"{flag}: manifest={d_val} class={c_val}")
    d_srv, d_tool = getattr(declared, "mcp_server", None), getattr(declared, "mcp_tool", None)
    c_srv, c_tool = getattr(op_cls, "mcp_server", None), getattr(op_cls, "mcp_tool", None)
    if d_srv != c_srv or d_tool != c_tool:
        diffs.append(f"mcp: manifest={d_srv!r}/{d_tool!r} class={c_srv!r}/{c_tool!r}")
    return diffs
