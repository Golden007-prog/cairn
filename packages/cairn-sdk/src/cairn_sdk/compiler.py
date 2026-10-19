"""The compiler: pipeline classes → record YAML (#1253, ADR-0087 P1).

Deterministic by construction (D1): dict insertion order is the emission order,
no timestamps, no randomness — an unchanged class re-compiles byte-identically.
The compiler imports NOTHING from cairn.core; the record loader is the contract
and the golden tests in the engine repo hold the two together.

Decisions enforced here (ADR-0087):
- **D4** — data edges (refs inside wires) are the authoritative ordering; a
  declared ``graph`` may only ADD control edges. Contradicting or omitting a
  data edge is a :class:`CompileError` naming both steps.
- **D7** — anonymous data gates get deterministic names derived from their
  predicate (``gate-prepare-stats-rows-ge-1000``); the audit trail never shows
  ``gate-3``.
- **#1251 optional wires** — a wire reading a ``when:``-carrying leg compiles
  with a bare-optional ``consumes`` mark (``key: "?"``), which the loader
  REQUIRES for reads of routable legs.
- ``until`` (D3) renders against the step ids as written; the runtime's loop
  unroller evaluates it per-iteration against the latest instance (#1129) —
  the compiler's job is only to never emit an unbounded loop.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import inspect
import re
import textwrap
import types
from typing import Any, Optional

import yaml

from .builders import (
    CompileScopeError,
    _AgenticLeg,
    _Chain,
    _FlowBuilder,
    _LiftedStep,
    _RefStep,
    _StepLike,
    cron,
    gate,
    loop,
    parallel,
    watch,
)
from .expr import OutRef, Predicate, Ref, ref_steps, render


class CompileError(ValueError):
    """The class cannot compile to a valid record; the message names the steps."""


_OPS = {"==": "eq", "!=": "ne", ">=": "ge", ">": "gt", "<=": "le", "<": "lt"}


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def _gate_name(pred: Predicate) -> str:
    """D7 — a stable, audited name synthesized from the predicate."""
    parts: list[str] = []
    for side in (pred.left, pred.right):
        if isinstance(side, OutRef):
            parts.append(_slug(side.step_id + "-" + "-".join(side._path)))
    parts.insert(1, _OPS.get(pred.op, _slug(pred.op)))
    for side in (pred.left, pred.right):
        if not isinstance(side, Ref):
            parts.append(_slug(str(side)))
    return "gate-" + "-".join(p for p in parts if p)


def _bare(ref: OutRef) -> str:
    """``outputs:``/``show:`` speak the bare ``step.field`` form."""
    return ref.step_id + "." + ".".join(ref._path)


def _ensure_id(item: _StepLike, fallback: str) -> str:
    if item._id is None:
        item._id = fallback
    return item._id


class CompiledPack:
    """The build product: one pipeline record + any flows defined inline —
    plus, when the class carries lifted ``@step`` defs (#1254), the private-op
    module and the wheel wiring that makes those defs installable code."""

    def __init__(self, name: str, pipeline_yaml: str, flows: dict[str, str],
                 notes: list[str], *, pack_name: str = "",
                 steps_module: str = "",
                 operators: Optional[list[dict]] = None,
                 agentic_docs: Optional[dict[str, str]] = None) -> None:
        self.name = name
        self.pipeline_yaml = pipeline_yaml
        self.flows = flows          # flow name → flows/<name>.yaml text
        self.notes = notes          # honest build notes (deferred constructs)
        self.agentic_docs = dict(agentic_docs or {})  # #1301 — flows/<name>.md
        self.pack_name = pack_name or name
        self.steps_module = steps_module    # generated src/<pkg>/steps_<name>.py
        self.operators = list(operators or [])  # lifted-op metadata for wiring

    @property
    def content_hash(self) -> str:
        """#1255 (ADR-0087 D6) — the drift-guard pin: sha256 over the compiled
        record TEXT, matching the loader's ``source_hash`` for a byte-identical
        installed file. Computable here without importing cairn.core (the
        boundary); pass as ``expected_hash`` on launch and the server 409s if
        the installed record differs from this build."""
        return "sha256:" + hashlib.sha256(
            self.pipeline_yaml.encode("utf-8")).hexdigest()[:16]

    @property
    def _pkg(self) -> str:
        """The Python package the lifted-steps module lives in."""
        return re.sub(r"\W", "_", self.pack_name)

    @property
    def _steps_module_name(self) -> str:
        return "steps_" + re.sub(r"\W", "_", self.name)

    def write(self, pack_dir) -> None:
        """Materialize into ``<pack_dir>``: ``pipelines/`` + ``flows/`` always;
        with lifted steps also ``src/<pkg>/`` (the GENERATED private-op module,
        always overwritten, and — only when absent, #1279 — the ``PACK`` shim
        in ``__init__.py``) and — only when absent, never clobbering an
        author's files — ``pack.toml`` / ``pyproject.toml`` wheel wiring
        (including the ``cairn.packs`` entry point + pipelines/flows
        force-includes, #1279) and a pack-local test scaffold (Rule 5).
        Existing wiring files instead get a NOTE stating the entries they
        must carry."""
        from pathlib import Path  # stdlib; local to keep module import-light

        base = Path(pack_dir)
        (base / "pipelines").mkdir(parents=True, exist_ok=True)
        (base / "pipelines" / f"{self.name}.yaml").write_text(
            self.pipeline_yaml, encoding="utf-8")
        for fname, text in self.flows.items():
            (base / "flows").mkdir(parents=True, exist_ok=True)
            (base / "flows" / f"{fname}.yaml").write_text(text, encoding="utf-8")
        # #1301 — agentic bodies (prompt= on flow.agentic) land as md next to
        # the yaml flows; generated like the steps module, so always rewritten.
        for fname, text in self.agentic_docs.items():
            (base / "flows").mkdir(parents=True, exist_ok=True)
            (base / "flows" / f"{fname}.md").write_text(text, encoding="utf-8")

        if not self.operators:
            return

        pkg_dir = base / "src" / self._pkg
        pkg_dir.mkdir(parents=True, exist_ok=True)
        init = pkg_dir / "__init__.py"
        if init.exists():
            # #1279 (F1) — never clobber an author's __init__.py; the note
            # names exactly what it must export for `cairn.packs` discovery.
            self.notes.append(
                f"src/{self._pkg}/__init__.py exists — ensure it exports "
                "PACK (a cairn.core.pack.protocol.Pack INSTANCE, not a "
                'factory) so [project.entry-points."cairn.packs"] can '
                f'target it:\n"{self.pack_name}" = "{self._pkg}:PACK"')
        else:
            init.write_text(self._init_shim(), encoding="utf-8")
        (pkg_dir / f"{self._steps_module_name}.py").write_text(
            self.steps_module, encoding="utf-8")

        ep_lines = "\n".join(
            f'"{op["name"]}" = "{self._pkg}.{self._steps_module_name}:{op["cls"]}"'
            for op in self.operators)
        op_tables = "\n\n".join(
            f'[operator."{op["name"]}"]\n'
            'version       = "0.1.0"\n'
            'input_schema  = "_Payload"\n'
            'output_schema = "_Payload"\n'
            f'risk = "{op["risk"]}"'
            for op in self.operators)

        pack_toml = base / "pack.toml"
        if pack_toml.exists():
            self.notes.append(
                "pack.toml exists — ensure it carries these operator entries:\n"
                + op_tables)
        else:
            pack_toml.write_text(
                f"# GENERATED by `cairn pack build` (#1254) — extend freely; "
                "the [operator] tables below\n# must match the lifted @step "
                "defs (rebuild regenerates the steps module, not this file).\n"
                "[pack]\n"
                f'name        = "{self.pack_name}"\n'
                'version     = "0.1.0"\n'
                'type        = "operator"\n'
                f'description = "SDK-built pack for pipeline {self.name}."\n'
                'kind        = "operator"\n'
                'trust_level = "local"\n\n'
                + op_tables + "\n", encoding="utf-8")

        # #1279 (F1) — the pipeline record has to travel with the wheel too:
        # force-include pipelines/ (always) and flows/ (only when non-empty —
        # force-including a path that doesn't exist breaks the hatchling
        # build) next to the package, matching where the __init__.py shim
        # looks for them at import time.
        packs_ep_line = f'"{self.pack_name}" = "{self._pkg}:PACK"'
        force_include_lines = [
            f'"pack.toml" = "{self._pkg}/pack.toml"',
            f'"pipelines" = "{self._pkg}/pipelines"',
        ]
        if self.flows:
            force_include_lines.append(f'"flows" = "{self._pkg}/flows"')

        pyproject = base / "pyproject.toml"
        if pyproject.exists():
            self.notes.append(
                "pyproject.toml exists — ensure "
                '[project.entry-points."cairn.operators"] carries:\n'
                + ep_lines + '\nand [project.entry-points."cairn.packs"] '
                "carries:\n" + packs_ep_line)
        else:
            pyproject.write_text(
                "# GENERATED by `cairn pack build` (#1254, #1279).\n"
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n\n'
                "[project]\n"
                f'name = "{self.pack_name}"\n'
                'version = "0.1.0"\n'
                f'description = "SDK-built pack for pipeline {self.name}."\n'
                'requires-python = ">=3.11"\n'
                "dependencies = []\n\n"
                '[project.entry-points."cairn.operators"]\n'
                + ep_lines + "\n\n"
                # #1279 — the loader (cairn/core/pack/loader.py) requires the
                # entry-point value to resolve to a Pack INSTANCE; PACK is
                # constructed by the __init__.py shim below.
                '[project.entry-points."cairn.packs"]\n'
                + packs_ep_line + "\n\n"
                "[tool.hatch.build.targets.wheel]\n"
                f'packages = ["src/{self._pkg}"]\n\n'
                "[tool.hatch.build.targets.wheel.force-include]\n"
                + "\n".join(force_include_lines) + "\n", encoding="utf-8")

        tests_dir = base / "tests"
        test_file = tests_dir / f"test_{self._steps_module_name}.py"
        if not test_file.exists():
            tests_dir.mkdir(parents=True, exist_ok=True)
            test_file.write_text(self._test_scaffold(), encoding="utf-8")

    def _init_shim(self) -> str:
        """GENERATED src/<pkg>/__init__.py — the #1279 (F1) fix: exports
        ``PACK``, a real ``cairn.core.pack.protocol.Pack`` INSTANCE (the
        loader's ``isinstance`` check demands an instance, not a factory),
        so ``[project.entry-points."cairn.packs"]`` has something to target
        and an installed wheel's pipeline records stop being invisible to
        ``load_all_packs`` / ``resolve_or_raise``.

        Directory resolution tries the WHEEL layout first — hatch
        force-includes ``pipelines/``/``flows/`` next to this file (see
        ``write()`` above) — and falls back to the SOURCE/editable layout,
        where they sit two levels up at the pack root (``src/<pkg>/`` →
        pack root). ``cairn.core.pack.protocol`` import is fine in GENERATED
        code: it runs inside the control plane, same rationale as the
        generated steps module importing ``cairn.core.operator`` — the SDK
        itself still imports nothing from ``cairn.core``.
        """
        return (
            f'"""{self.pack_name} — pack package (lifted @step ops, '
            '#1254).\n\n'
            "Exports PACK (#1279) — the `cairn.packs` entry-point target.\n"
            'Rebuilding overwrites this file only when it is ABSENT; once it\n'
            'exists, edit it directly (rebuild notes the required exports '
            'instead)."""\n'
            "from pathlib import Path\n\n"
            "from cairn.core.pack.protocol import Pack\n\n"
            "_here = Path(__file__).resolve().parent\n"
            '_pipelines = _here / "pipelines"          # wheel: force-included\n'
            "if not _pipelines.is_dir():\n"
            '    _pipelines = _here.parent.parent / "pipelines"  # source/editable\n'
            '_flows = _here / "flows"\n'
            "if not _flows.is_dir():\n"
            '    _flows = _here.parent.parent / "flows"\n\n'
            "PACK = Pack(\n"
            f"    name={self.pack_name!r},\n"
            '    version="0.1.0",\n'
            f'    description="SDK-built pack for pipeline {self.name}.",\n'
            "    pipelines_dir=str(_pipelines) if _pipelines.is_dir() else None,\n"
            "    flows_dir=str(_flows) if _flows.is_dir() else None,\n"
            ")\n"
        )

    def _test_scaffold(self) -> str:
        """Golden-fixture test scaffold for the lifted ops (Rule 5) — imports
        the generated module by path so it runs pre-install."""
        checks = "\n".join(
            f'    assert m.{op["cls"]}.name == "{op["name"]}"\n'
            f'    assert m.{op["cls"]}.requires == {op["requires"]!r}'
            for op in self.operators)
        return (
            f'"""Pack-local tests for the lifted @step ops of \'{self.name}\' '
            "(scaffolded by\n`cairn pack build`, #1254) — extend with golden "
            'fixtures for each op\'s real behavior."""\n'
            "import importlib.util\n"
            "import pathlib\n\n\n"
            "def _load():\n"
            "    p = (pathlib.Path(__file__).parent.parent / 'src' / "
            f"'{self._pkg}' / '{self._steps_module_name}.py')\n"
            f"    spec = importlib.util.spec_from_file_location("
            f"'{self._pkg}.{self._steps_module_name}', p)\n"
            "    mod = importlib.util.module_from_spec(spec)\n"
            "    spec.loader.exec_module(mod)\n"
            "    return mod\n\n\n"
            "def test_lifted_ops_register_with_declared_requires():\n"
            "    m = _load()\n"
            + checks + "\n")


def _collect(cls: type) -> list[_StepLike]:
    items = [v for v in vars(cls).values() if isinstance(v, _StepLike)]
    for attr, v in vars(cls).items():
        if callable(v) and not isinstance(v, type) and attr != "__init__":
            if not getattr(v, "__cairn_ok__", False) and not attr.startswith("_"):
                raise CompileScopeError(
                    f"def {attr} in a pipeline class: decorate with "
                    "@step(requires=[...]) to LIFT it into a private op "
                    "(#1254), or prefix with _ for a plain helper."
                )
    return items


#: Mirror of ``cairn.core.operator.guard.AMBIENT_CONTEXT_ATTRS`` — the SDK
#: imports nothing from cairn.core (the record loader is the contract), so the
#: ambient surface is duplicated here and the golden tests in the engine repo
#: assert the two sets stay equal.
_AMBIENT_CTX = frozenset({
    "pipeline_id", "node_name", "is_mocked",
    "logger", "emit_heartbeat", "check_cancelled",
})


def _lift(s: _LiftedStep, sid: str) -> dict:
    """D5 lint + source extraction for one lifted def.

    The lint walks the AST for first-level ``ctx.X`` uses and errors on any
    not declared in ``requires`` — it ASSISTS declaration, it never infers it
    (dynamic access slips past any lint; the runtime guard is the enforcement
    of record). ``requires`` is mandatory the moment the body touches ctx.
    """
    try:
        src = textwrap.dedent(inspect.getsource(s.fn))
    except (OSError, TypeError) as e:
        raise CompileError(
            f"step '{sid}': cannot read the def's source ({e}) — lifted "
            "steps need an importable module (not a REPL)."
        ) from None
    node = ast.parse(src).body[0]
    if isinstance(node, ast.AsyncFunctionDef):
        raise CompileScopeError(
            f"step '{sid}': async lifted defs are not supported yet — "
            "Operation.run is sync (lift to a real operator pack for async)."
        )
    params = [a.arg for a in node.args.args]
    ctx_name = "ctx" if "ctx" in params else (params[1] if len(params) > 1 else "")

    used: set[str] = set()
    for n in ast.walk(node):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == ctx_name):
            used.add(n.attr)
    needed = used - _AMBIENT_CTX
    if needed and s.requires is None:
        raise CompileError(
            f"step '{sid}' touches ctx.{{{', '.join(sorted(needed))}}} — "
            "requires=[...] is MANDATORY for a def that uses capabilities "
            "(fail-closed, ADR-0053/D5)."
        )
    undeclared = sorted(needed - set(s.requires or []))
    if undeclared:
        raise CompileError(
            f"step '{sid}' uses ctx.{{{', '.join(undeclared)}}} not declared "
            f"in requires={sorted(s.requires or [])} — declare every "
            "capability the body touches (D5; the lint assists, it never "
            "infers)."
        )

    # def-line onward (decorators excluded — node.lineno is the `def` line);
    # a line slice, not ast.unparse, so the author's comments survive.
    lines = src.splitlines()
    fn_src = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return {
        "fn_src": fn_src, "fn_name": node.name, "params": params,
        "ctx_param": ctx_name, "requires": sorted(s.requires or []),
        "risk": s.risk, "imports": _lifted_imports(s, node, sid),
    }


def _lifted_imports(s: _LiftedStep, node: ast.FunctionDef, sid: str) -> list[str]:
    """#1302 — a lifted body is pasted verbatim into a fresh module, so any
    name it borrowed from the AUTHORING module vanishes (a runtime NameError
    that compiles clean — the worst failure shape). Free names are resolved
    at BUILD: a plain module binding re-emits as an import line; anything
    else fails loud — a lifted def is self-contained (D5), and the fix the
    error names (import inside the body, or wire the value in) survives the
    paste. No inference beyond the unambiguous module case.
    """
    bound: set[str] = {a.arg for a in node.args.args}
    free: set[str] = set()
    # walk the BODY only: the decorator expressions live in node.decorator_list
    # and reference authoring-scope names (`@step(requires=requires)`) that the
    # source paste deliberately excludes — they must not count as free.
    for stmt in node.body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    bound.add(n.id)
                else:
                    free.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                bound.update(a.arg for a in n.args.args)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in n.names)

    module_globals = getattr(s.fn, "__globals__", {}) or {}
    out: list[str] = []
    for name in sorted(free - bound - set(dir(builtins))):
        if name not in module_globals:
            raise CompileError(
                f"step '{sid}': the body references {name!r}, which is neither "
                f"a parameter nor defined in the authoring module — it would "
                f"NameError at runtime."
            )
        val = module_globals[name]
        if isinstance(val, types.ModuleType):
            mod_name = getattr(val, "__name__", name)
            out.append(f"import {mod_name}" if mod_name == name
                       else f"import {mod_name} as {name}")
        else:
            raise CompileError(
                f"step '{sid}': the body references module-level {name!r} "
                f"({type(val).__name__}) — a lifted def is SELF-CONTAINED "
                f"(#1302/D5): import it inside the def body, or pass the value "
                f"through a wire."
            )
    return out


def _emit_steps_module(pipeline_name: str, cls_name: str,
                       lifted: list[dict]) -> str:
    """The GENERATED private-op module: each def pasted verbatim, wrapped in a
    registered Operation whose ctx is guarded to the declared requires."""
    # #1302 — module bindings the defs borrowed from the authoring module,
    # re-emitted so the verbatim paste keeps resolving them.
    carried = sorted({imp for e in lifted for imp in e.get("imports", ())})
    carried_block = ("\n".join(carried) + "\n\n") if carried else ""
    parts = [
        f'"""Lifted @step defs of pipeline \'{pipeline_name}\' — GENERATED '
        "by `cairn pack build`\n(#1254, ADR-0087 P2). Rebuilding overwrites "
        'this file; edit the pipeline class instead."""\n'
        "from __future__ import annotations\n\n"
        + carried_block +
        "from pydantic import BaseModel, ConfigDict\n\n"
        "from cairn.core.operator import Operation, guard_context, "
        "register_operator\n\n\n"
        "class _Payload(BaseModel):\n"
        '    """Free-form payload — a lifted def\'s contract is its returned '
        'dict."""\n'
        "    model_config = ConfigDict(extra=\"allow\")\n"
    ]
    for entry in lifted:
        params, fn = entry["params"], entry["fn_name"]
        args = []
        for i, p in enumerate(params):
            if i == 0 and p == "self":
                args.append("None")
            elif p == entry["ctx_param"]:
                args.append("guard_context(ctx, self.requires)")
            else:
                args.append("inp.model_dump()")
        call = f"{fn}({', '.join(args)})"
        parts.append(
            f"\n\n{entry['fn_src']}\n\n\n"
            f'@register_operator(name="{entry["op_name"]}", version="0.1.0")\n'
            f"class {entry['op_cls']}(Operation[_Payload, _Payload]):\n"
            f'    """Lifted from {cls_name}.{entry["sid"]} — risk: '
            f'{entry["risk"]}."""\n'
            "    input_schema = _Payload\n"
            "    output_schema = _Payload\n"
            f"    requires = {entry['requires']!r}\n\n"
            "    def run(self, inp, ctx):\n"
            f"        out = {call}\n"
            "        return _Payload(**(out or {}))\n"
        )
    return "".join(parts)


def _inputs_of(cls: type) -> dict[str, dict]:
    spec: dict[str, dict] = {}
    inputs_cls = getattr(cls, "Inputs", None)
    if inputs_cls is None:
        return spec
    for name, ann in getattr(inputs_cls, "__annotations__", {}).items():
        entry: dict[str, Any] = {"type": getattr(ann, "__name__", str(ann))}
        if hasattr(inputs_cls, name):
            entry["default"] = getattr(inputs_cls, name)
        spec[name] = entry
    return spec


def _outputs_of(cls: type) -> dict[str, str]:
    """#1286 — mirrors ``_inputs_of`` for the output side. ``class Outputs``
    annotations are a PROMISE (``model_uri: str``); the promise is only kept
    when the name also carries a class-level OutRef value binding it to a
    step (``model_uri: str = train.out.model_uri``) — annotate-only means the
    field was never wired to anything, which fails loud instead of silently
    producing an empty projection."""
    spec: dict[str, str] = {}
    outputs_cls = getattr(cls, "Outputs", None)
    if outputs_cls is None:
        return spec
    for name in getattr(outputs_cls, "__annotations__", {}):
        val = getattr(outputs_cls, name, None)
        if not isinstance(val, OutRef):
            raise CompileError(
                f"`class Outputs` field {name!r} is annotated but never bound "
                f"to a step output — annotate AND bind it: `{name}: <type>` on "
                f"`Outputs`, then `Outputs.{name} = some_step.out.{name}` right "
                f"after (a nested class body can't see the outer class's step "
                f"names, so the bind is a second statement)."
            )
        spec[name] = _bare(val)
    return spec


def _chain_of(cls: type) -> list[_StepLike]:
    g = getattr(cls, "graph", None)
    if g is None:
        return []
    if isinstance(g, parallel):     # a graph of ONE concurrent group
        return [g]
    if not isinstance(g, _Chain):
        raise CompileError("`graph` must be a `a >> b >> ...` chain of steps/gates.")
    return list(g.items)


def compile_pipeline(cls: type) -> CompiledPack:
    meta = getattr(cls, "__cairn_pipeline__", None)
    if meta is None:
        raise CompileError(f"{cls.__name__} is not decorated with @pipeline(...)")
    name = _slug(re.sub(r"(?<!^)(?=[A-Z])", "-", cls.__name__))
    pack = str(meta.get("pack") or "") or name
    notes: list[str] = []

    items = _collect(cls)
    chain = _chain_of(cls)
    # parallel(...) groups expand into their members; other items are their
    # own group. Group INDEX is the chain position for D4 + dep computation.
    groups: list[list[_StepLike]] = []
    for c in chain:
        groups.append(list(c.steps) if isinstance(c, parallel) else [c])
    # anonymous chain members (data gates built inline) join the item list
    for grp in groups:
        for c in grp:
            if isinstance(c, gate) and c._id is None and c.predicate is not None:
                _ensure_id(c, _gate_name(c.predicate))
            if c not in items:
                items.append(c)
    by_id = { _ensure_id(s, f"step{idx}"): s for idx, s in enumerate(items) }

    # lifted defs (#1254): lint each body, fix its task ref, collect emission
    lifted: list[dict] = []
    for sid, s in by_id.items():
        if isinstance(s, _LiftedStep):
            entry = _lift(s, sid)
            entry["sid"] = sid
            entry["op_name"] = f"{pack}/_steps/{name}.{sid}"
            entry["op_cls"] = "Lifted_" + re.sub(r"\W", "_", sid)
            s.ref = entry["op_name"]
            lifted.append(entry)

    # ── D4: ordering — data edges authoritative, chain may only add ─────────
    data_edges: set[tuple[str, str]] = set()
    for sid, s in by_id.items():
        srcs: list[str] = []
        for value in (getattr(s, "wires", {}) or {}).values():
            srcs += ref_steps(value)
        for r in getattr(s, "reads", []) or []:
            srcs += ref_steps(r)
        if getattr(s, "when_pred", None) is not None:
            srcs += s.when_pred.ref_step_ids()
        if isinstance(s, gate) and s.predicate is not None:
            srcs += s.predicate.ref_step_ids()
        if isinstance(s, loop):
            srcs += s.until.ref_step_ids()
        for src in srcs:
            if src != sid:
                data_edges.add((src, sid))

    pos = {c.id: i for i, grp in enumerate(groups) for c in grp}
    if pos:
        for src, dst in sorted(data_edges):
            if dst in pos and src not in pos:
                raise CompileError(
                    f"graph omits step '{src}', which '{dst}' consumes — the "
                    "picture would lie about the data (D4)."
                )
            if src in pos and dst in pos and pos[src] > pos[dst]:
                raise CompileError(
                    f"graph orders '{dst}' before '{src}' but '{dst}' consumes "
                    f"'{src}' output — data edges are authoritative (D4)."
                )
            if src in pos and dst in pos and pos[src] == pos[dst]:
                raise CompileError(
                    f"parallel groups '{src}' and '{dst}' as concurrent but "
                    f"'{dst}' consumes '{src}' output — data edges are "
                    "authoritative (D4)."
                )

    # ── emit steps in declaration order (chain adds control deps) ───────────
    routable = {sid for sid, s in by_id.items() if getattr(s, "when_pred", None) is not None}
    flows: dict[str, str] = {}
    agentic_docs: dict[str, str] = {}   # #1301 — flow name → flows/<name>.md
    steps_out: list[dict] = []

    for sid, s in by_id.items():
        deps = sorted({src for src, dst in data_edges if dst == sid})
        if sid in pos and pos[sid] > 0:
            # ALL of the previous chain group precedes each member of this one
            for prev in (c.id for c in groups[pos[sid] - 1]):
                if prev not in deps and prev != sid:
                    deps.append(prev)

        entry: dict[str, Any] = {}
        if isinstance(s, gate):
            entry["gate"] = sid
            if s.predicate is not None:
                # #1275 (G1, ADR-0087 Amendment 1) — the def-level data-STOP
                # landed: the gate leg's `predicate:` field, evaluated
                # fail-closed by the bridge the moment the gate is ready.
                # D7's synthesized name (already on `sid`) is what the audit
                # trail shows.
                entry["predicate"] = s.predicate._render()
            if s.approvers:
                entry["approvers"] = s.approvers
            if s.timeout:
                entry["timeout"] = s.timeout
            if s.show:
                entry["show"] = [_bare(r) if isinstance(r, OutRef) else str(r) for r in s.show]
            if s.when_pred is not None:
                # #1285 — `when()` on a gate ROUTES it (verdict `routed_off`)
                # instead of stopping; the predicate already feeds D4 above
                # (`ref_step_ids`) so the dep is there — this is the missing
                # emission of the field the bridge already evaluates first.
                entry["when"] = s.when_pred._render()
            if deps:
                entry["depends_on"] = deps
            steps_out.append(entry)
            continue

        if isinstance(s, loop):
            entry["loop"] = sid
            inner: list[dict] = []
            for idx, inner_step in enumerate(s.steps):
                if isinstance(inner_step, _LiftedStep):
                    raise CompileScopeError(
                        f"loop '{sid}': lifted defs inside loop(...) are not "
                        "supported — lift the def to a top-level @step and "
                        "reference it, or use a shared op ref."
                    )
                iid = _ensure_id(inner_step, _slug(inner_step.ref) or f"{sid}-{idx}")
                ientry: dict[str, Any] = {"id": iid}
                ientry["task" if inner_step.leg_kind == "task" else "flow"] = inner_step.ref
                if inner_step.wires:
                    ientry["with"] = render(inner_step.wires)
                inner.append(ientry)
            entry["steps"] = inner
            entry["until"] = s.until._render()
            entry["max_iterations"] = s.max_iterations
            entry["on_exhausted"] = s.on_exhausted
            if s.when_pred is not None:
                entry["when"] = s.when_pred._render()
            if deps:
                entry["depends_on"] = deps
            steps_out.append(entry)
            continue

        entry["id"] = sid
        if isinstance(s, _FlowBuilder):
            entry["flow"] = s.flow_name
            flows[s.flow_name] = _emit_flow(s, notes)
            if s.wires:
                entry["with"] = render({k: v for k, v in s.wires.items()})
        elif isinstance(s, _AgenticLeg):
            entry["template"] = s.flow_name
            # #1301 — the leg's BODY: prompt= compiles to flows/<name>.md
            # (frontmatter + prose, one schema-validated prompt node). Without
            # it the ref would dangle unless the author ships the md — say so.
            if s.prompt:
                front: dict[str, Any] = {
                    "name": s.flow_name, "kind": "agentic",
                    "nodes": [{"id": "respond", "type": "prompt",
                               **({"output": s.output} if s.output else {})}],
                }
                agentic_docs[s.flow_name] = (
                    "---\n"
                    + yaml.safe_dump(front, sort_keys=False).rstrip() + "\n"
                    + "---\n" + textwrap.dedent(s.prompt).strip() + "\n")
            else:
                notes.append(
                    f"agentic leg '{sid}' has no prompt= — provide one, or "
                    f"ship flows/{s.flow_name}.md in the pack yourself "
                    f"(#1301: the leg references that body).")
            wires = dict(s.wires)
            if s.model:
                wires["model"] = s.model
            for idx, r in enumerate(s.reads):
                wires[f"reads_{r.step_id}"] = r
            if wires:
                entry["with"] = render(wires)
        elif isinstance(s, (_RefStep, _LiftedStep)):
            entry[s.leg_kind] = s.ref
            wires = dict(s.wires)
            if s.retry:
                wires["retry"] = s.retry          # H2 (#1244) leg retry block
            if wires:
                entry["with"] = render(wires)
            if s.timeout:
                entry["timeout"] = s.timeout
            if s.environment:
                entry["environment"] = s.environment
            if s.trigger:
                entry["trigger"] = s.trigger
        else:  # pragma: no cover - future builder kinds fail loud
            raise CompileError(f"unknown builder for step '{sid}': {type(s).__name__}")

        if s.when_pred is not None:
            entry["when"] = s.when_pred._render()
        # #1251 — reads of a routable leg MUST be marked optional
        optional_keys = sorted(
            k for k, v in (getattr(s, "wires", {}) or {}).items()
            if any(src in routable for src in ref_steps(v))
        )
        if optional_keys:
            entry["consumes"] = {k: "?" for k in optional_keys}
        if deps:
            entry["depends_on"] = deps
        steps_out.append(entry)

    doc_steps = steps_out

    # entry triggers: captured, not yet expressible in the pipeline record
    for trig in getattr(cls, "on", []) or []:
        if isinstance(trig, cron):
            notes.append(f"entry trigger cron('{trig.expr}') — schedule records "
                         "are a separate store; wire via `cairn schedule` (P1 note).")
        elif isinstance(trig, watch):
            notes.append(f"entry trigger watch('{trig.uri}') — wire via the "
                         "watch trigger (#992) (P1 note).")

    governance: dict[str, Any] = {}
    if meta.get("budget_usd"):
        governance["budget"] = {"usd": meta["budget_usd"]}
    if meta.get("series"):
        governance["series"] = meta["series"]
    if meta.get("approvers"):
        governance["approvers"] = meta["approvers"]

    # #1287 — `notify=`/`env=` are captured on `__cairn_pipeline__` but the
    # pipeline record has no field for either (verified against
    # cairn.core.engine.pipeline / pipeline_bridge / bundle_pipeline: nothing
    # reads a record-level `notify`). Captured-never-dropped (ADR-0087
    # Amendment 1): each word gets an honest BUILD NOTE instead of vanishing.
    if meta.get("notify"):
        notes.append(
            f"notify={meta['notify']!r} — pipeline records carry no notify "
            "field; wire channels via the notifier registry / gate approvers "
            "channels."
        )
    if meta.get("env"):
        notes.append(
            f"env={meta['env']!r} is authoring intent; environments are "
            "chosen at LAUNCH (ADR-0070) — pass environment= when launching."
        )

    # #1286 — `outputs = {...}` (dict) and `class Outputs:` (annotated) are
    # the SAME declaration in two spellings; only one may be present, or
    # there is no single source of truth for what the pipeline promises.
    dict_outputs = getattr(cls, "outputs", None)
    class_outputs = getattr(cls, "Outputs", None)
    if dict_outputs is not None and class_outputs is not None:
        raise CompileError(
            f"{cls.__name__} declares both `outputs = {{...}}` and "
            "`class Outputs:` — one source of truth for a pipeline's "
            "outputs; keep whichever form you prefer and drop the other."
        )
    outputs = (
        _outputs_of(cls) if class_outputs is not None else
        {
            k: (_bare(v) if isinstance(v, OutRef) else str(v))
            for k, v in (dict_outputs or {}).items()
        }
    )

    doc: dict[str, Any] = {"name": name, "kind": "pipeline"}
    if cls.__doc__:
        doc["description"] = " ".join(cls.__doc__.split())
    inputs = _inputs_of(cls)
    if inputs:
        doc["inputs"] = inputs
    if governance:
        doc["governance"] = governance
    doc["steps"] = doc_steps
    if outputs:
        doc["outputs"] = outputs

    return CompiledPack(
        name=name,
        pipeline_yaml=yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        flows=flows,
        notes=notes,
        pack_name=pack,
        steps_module=(_emit_steps_module(name, cls.__name__, lifted)
                      if lifted else ""),
        operators=[{"name": e["op_name"], "cls": e["op_cls"],
                    "risk": e["risk"], "requires": e["requires"]}
                   for e in lifted],
        agentic_docs=agentic_docs,
    )


#: The engine-neutral resource axes (`cairn.core.dataflow.StageResources`).
#: Kept as a literal so the SDK does not import core — the projection compiles
#: standalone (ADR-0087) — and so an unknown axis is a BUILD note rather than a
#: pydantic error surfacing at submit, hours later.
_RESOURCE_KEYS = ("cpu", "memory_mb", "gpu")


def _stage_resources(raw: Any, stage_ref: str, notes: list[str]) -> dict:
    """Validate a ``resources={...}`` hint at BUILD time (ADR-0089 #1350).

    The contract accepts only int axes; a typo (`memory=`, `gpus=`) would
    otherwise ride into the record and be rejected by ``StageResources``'s
    ``extra="forbid"`` at submit — after the pipeline was already launched.
    """
    if not raw:
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if key not in _RESOURCE_KEYS:
            notes.append(
                f"stage {stage_ref!r}: resources key {key!r} is not an axis the "
                f"contract carries {_RESOURCE_KEYS} — DROPPED (it would be "
                "rejected at submit)"
            )
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            notes.append(
                f"stage {stage_ref!r}: resources[{key!r}]={value!r} is not an "
                "integer — DROPPED"
            )
    return out


def _emit_flow(fb: _FlowBuilder, notes: list[str]) -> str:
    """A ``flow.data/.ml`` builder → ``flows/<name>.yaml``. Refs inside stage
    config are hoisted to ``$config.<key>`` with the binding moved onto the
    leg's ``with:`` — flow defs are reusable records, never run-specific."""
    stages_out: list[dict] = []
    for st in fb.stages:
        cfg: dict[str, Any] = {}
        for k, v in st["config"].items():
            if isinstance(v, (Ref, Predicate)):
                hoist_key = _slug(st["stage"]) .replace("-", "_") + "_" + k
                fb.wires[hoist_key] = v
                cfg[k] = f"$config.{hoist_key}"
            else:
                cfg[k] = v
        entry: dict[str, Any] = {"stage": st["stage"]}
        if cfg:
            entry["config"] = cfg
        if st.get("checkpoint"):
            entry["checkpoint"] = True   # H6a (#1248) — declared durability
        res = _stage_resources(st.get("resources"), st["stage"], notes)
        if res:
            entry["resources"] = res     # ADR-0089 (#1350) — engine-neutral hint
        stages_out.append(entry)
    doc = {"name": fb.flow_name, "kind": fb.kind, "stages": stages_out}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


__all__ = ["CompiledPack", "CompileError", "compile_pipeline"]
