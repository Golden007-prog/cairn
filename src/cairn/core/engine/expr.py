"""ONE expression language for every "pass this value there" site (#1118, R6b).

Before this module, one concept had four grammars and four resolvers:

  =============  ==============================================  =================
  layer          dialect                                         resolver (gone)
  =============  ==============================================  =================
  operations     ``$input.* $state.* $default(...)``             operator/runtime
  flows          ``$config.<key>``                               dataflow/defs
  bundles        ``$input.* $steps.<id>.<field>`` + predicates   engine/bundle
  guards         named refs into condition_registry              (retires w/ #1107)
  =============  ==============================================  =================

The grammar going forward is **JMESPath** (already an installed dependency) —
every dialect above is "path lookup with a default", which is exactly what
JMESPath does; we wire it rather than write a fifth grammar. New-style
expressions are compiled with :func:`jmespath.compile` and evaluated over ONE
scoped document::

    {input, state, nodes, artifacts, env, config, steps}

Two deliberate deviations, both semantic-preserving:

* ``$default(expr, literal)`` is kept as an affordance and handled HERE, not
  translated to JMESPath ``||`` — ``||`` falls back on ANY falsy value
  (``false``/``0``/``''``), where ``$default`` falls back only on
  missing-or-``None``. (#630 was exactly this class of bug.)
* Legacy dotted paths are evaluated by a walk, not by JMESPath, because the
  legacy dialect supports attribute access on Pydantic-shaped objects and
  raise-on-missing with a keys-listing error — JMESPath is dict-only and
  returns ``None`` for missing. The walk lives ONLY here; the four scattered
  copies are deleted.

Back-compat: today's dialect strings keep working through the legacy
translation for one release, so no pack changes the day the resolver does.
Malformed expressions fail at COMPILE time naming the file and the expression.
"""
from __future__ import annotations

import ast
import json
import operator
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import jmespath
from jmespath.exceptions import JMESPathError

__all__ = [
    "ExprError",
    "Scopes",
    "Expr",
    "MISSING",
    "coerce_literal",
    "compile_expr",
    "compile_predicate",
    "resolve_value",
    # the steps-scope lookup, shared so gate predicates and gate `show:`
    # cannot drift into two grammars again (#1689)
    "step_field",
    "step_path",
]


class ExprError(ValueError):
    """A binding/expression failed to compile or resolve.

    Always names the ``source`` (file or node the expression came from) and the
    expression itself — the acceptance bar for #1118 is that a malformed
    expression fails at compile time naming both.
    """

    def __init__(
        self, message: str, *, expr: str | None = None, source: str | None = None
    ) -> None:
        self.expr = expr
        self.source = source
        if expr is not None:
            message = f"{source or '<expr>'}: expression {expr!r}: {message}"
        super().__init__(message)


class _Missing:
    """Sentinel for "the path does not exist" — distinct from an explicit None."""

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "<MISSING>"


MISSING = _Missing()


@dataclass
class Scopes:
    """The ONE evaluation document. Each field is a top-level scope.

    ``state`` is mandatory (it is where operations read/write); everything else
    is optional and falls back to the conventional key inside ``state`` the way
    the legacy operations resolver did (``template_input``/``nodes``/
    ``artifacts``), so existing call sites keep their behavior.
    """

    state: dict[str, Any] = field(default_factory=dict)
    input: Optional[dict[str, Any]] = None
    nodes: Optional[dict[str, Any]] = None
    artifacts: Optional[dict[str, Any]] = None
    env: Optional[dict[str, str]] = None
    config: Optional[dict[str, Any]] = None
    steps: Optional[dict[str, Any]] = None
    #: allow ``$env.NAME`` to fall back to ``os.environ`` (tests set False)
    use_os_env: bool = True

    def scope_input(self) -> dict[str, Any]:
        if self.input is not None:
            return self.input
        return self.state.get("template_input", {}) or {}

    def scope_nodes(self) -> dict[str, Any]:
        if self.nodes is not None:
            return self.nodes
        return self.state.get("nodes", {}) or {}

    def scope_artifacts(self) -> dict[str, Any]:
        if self.artifacts is not None:
            return self.artifacts
        return self.state.get("artifacts", {}) or {}

    def document(self) -> dict[str, Any]:
        """The scoped document new-grammar (JMESPath) expressions evaluate over."""
        return {
            "state": self.state,
            "input": self.scope_input(),
            "nodes": self.scope_nodes(),
            "artifacts": self.scope_artifacts(),
            "env": self.env or {},
            "config": self.config or {},
            "steps": self.steps or {},
        }


# ─── legacy dialect recognition ───────────────────────────────────────────

_DEFAULT_RE = re.compile(r"^\$default\((.*)\)$", re.DOTALL)
#: scopes that take a dotted path after ``$<scope>.``
_DOTTED_SCOPES = ("state", "input", "nodes", "artifacts", "config", "steps")
#: scopes that take exactly one flat name
_FLAT_SCOPES = ("env",)
_LEGACY_HEAD_RE = re.compile(r"^\$(state|input|nodes|artifacts|config|steps|env)\.(.+)$", re.DOTALL)
#: bundle gate predicates: ``<lhs> ==|!= <literal>`` where lhs is a legacy ref
# #1275 — ordered comparators joined ==/!= for data gates (`$steps.prep.rows
# >= 1000`). Alternation order matters: `>=`/`<=` before `>`/`<`, or the
# two-char ops match as one-char + a literal starting with "=".
_LEGACY_PRED_RE = re.compile(
    r"^\s*(\$[A-Za-z0-9_.$()-]+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")


def _walk(obj: Any, parts: list[str], *, expr: str, source: str) -> Any:
    """Walk ``obj`` along path parts: dict lookup first, attribute fallback.

    Returns :data:`MISSING` when a segment does not exist — the caller decides
    whether that raises (operations), omits (``$config``), or defaults.
    """
    cur: Any = obj
    for part in parts:
        if isinstance(cur, dict):
            if part not in cur:
                return MISSING
            cur = cur[part]
        else:
            if not hasattr(cur, part):
                return MISSING
            cur = getattr(cur, part)
    return cur


def _step_lookup(step_out: Any, parts: list[str], *, expr: str, source: str) -> Any:
    """The ``steps`` scope's lookup: top-level path first, else the same path
    under ``findings`` (deterministic operators write verdicts there,
    ADR-0066/#585). Missing → :data:`MISSING`."""
    val = _walk(step_out, parts, expr=expr, source=source)
    if val is MISSING and parts and isinstance(step_out, dict):
        findings = step_out.get("findings")
        if isinstance(findings, dict):
            val = _walk(findings, parts, expr=expr, source=source)
    return val


def step_field(step_output: Any, field: str) -> Any:
    """Public helper: read ``field`` from ONE step's output with the
    ``steps``-scope semantics (top-level, else ``findings.<field>``; missing →
    ``None``). The pipeline bridge reads verdicts this way — same lookup, same
    module, zero copies."""
    if not isinstance(step_output, dict):
        return None
    val = _step_lookup(step_output, [field], expr=field, source="step_field")
    return None if val is MISSING else val


def step_path(step_output: Any, parts: list[str], *, source: str = "step_path") -> Any:
    """:func:`step_field` for a DOTTED path — same semantics, more than one
    segment (``findings.dedup_exact_rows``, ``mix.0.source``).

    Exists so the gate's ``show:`` renderer can share this rule instead of
    walking the path itself (#1689). It had its own walk with no ``findings``
    fallback, so a predicate and a ``show:`` ref with identical spelling
    disagreed: the predicate resolved a flow leg's stat, the approver's view
    showed ``None``. Two surfaces of one gate must not speak two grammars.
    """
    val = _step_lookup(step_output, list(parts), expr=".".join(parts), source=source)
    return None if val is MISSING else val


def _split_default_args(body: str, *, expr: str, source: str) -> tuple[str, str]:
    """Split ``$default(a, b)``'s body on the first top-level comma, respecting
    parens/brackets and quoted strings (so ``"hello, world"`` doesn't split)."""
    depth = 0
    in_str: str | None = None
    escaped = False
    for i, ch in enumerate(body):
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return body[:i].strip(), body[i + 1 :].strip()
    raise ExprError(
        "$default(...) requires two args separated by a top-level comma",
        expr=expr, source=source,
    )


def _parse_literal(text: str, *, expr: str, source: str) -> Any:
    """Parse a literal accepting BOTH JSON and Python spellings.

    Pack YAML is JSON-shaped so authors write ``false``/``true``/``null``;
    ``ast.literal_eval`` covers the Python-only forms (single-quoted strings,
    tuples). Both are safe — neither executes anything. (#630.)
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError) as e:
        raise ExprError(
            f"literal {text!r} is not a valid Python/JSON literal: {e}",
            expr=expr, source=source,
        ) from e


# ─── compiled forms ───────────────────────────────────────────────────────


@dataclass
class _LegacyPath:
    scope: str                       # "state" | "input" | ... | "env"
    parts: list[str]                 # dotted segments (env: exactly one)

    def references(self) -> list[tuple[str, list[str]]]:
        return [(self.scope, self.parts)]


@dataclass
class _Default:
    inner: "Expr"
    literal: Any = None
    #: ``$default(a, $input.b)`` — a BINDING fallback (#926's chained-default
    #: intent). The old resolver parsed the 2nd arg lazily and ONLY as a
    #: literal, so this form crashed exactly when the fallback was needed —
    #: a latent bug in run-stage-dataset + run-prep-anonymize, found by the
    #: #1118 equivalence harness. Now it resolves.
    fallback: Optional["Expr"] = None

    def references(self) -> list[tuple[str, list[str]]]:
        refs = self.inner.references()
        if self.fallback is not None:
            refs = refs + self.fallback.references()
        return refs


@dataclass
class _Jmes:
    compiled: Any                    # jmespath.parser.ParsedResult
    raw: str

    def references(self) -> list[tuple[str, list[str]]]:
        # New-grammar references are opaque to static wire validation for now;
        # bundle-wire validation still runs on the legacy form during the shim
        # period, and #1107's typed contract supersedes it after.
        return []


@dataclass
class _Cmp:
    """A legacy gate predicate: ``<legacy-ref> <op> <literal>``.

    Kept out of JMESPath deliberately: the lhs must go through the legacy walk
    (missing → None, ``$steps`` findings-fallback) — translating to a JMESPath
    comparison would silently lose the fallback and break every
    ``$steps.gate.verdict == 'pass'`` gate whose verdict sits under findings.

    #1275 added the ordered ops for data gates. Ordering over a missing or
    non-comparable lhs is FALSE, never a crash — a gate over garbage fails
    closed, exactly like a gate over nothing.
    """

    lhs: "Expr"
    op: str                          # "==" | "!=" | ">=" | "<=" | ">" | "<"
    rhs: Any

    def references(self) -> list[tuple[str, list[str]]]:
        return self.lhs.references()


@dataclass
class Expr:
    """One compiled expression. Compile once (validated), evaluate many.

    ``evaluate`` raises :class:`ExprError` on a missing path;
    ``try_evaluate`` returns :data:`MISSING` instead — that is how ``$config``
    keeps its omit-on-missing semantics without a second resolver.
    """

    raw: str
    source: str
    _form: Any = None                # _LegacyPath | _Default | _Jmes | literal passthrough
    _is_literal: bool = False

    # -- evaluation ----------------------------------------------------------

    def try_evaluate(self, scopes: Scopes) -> Any:
        """Evaluate; missing path → :data:`MISSING` (never raises for missing)."""
        if self._is_literal:
            return self._form
        form = self._form
        if isinstance(form, _Default):
            val = form.inner.try_evaluate(scopes)
            if val is not MISSING and val is not None:
                return val
            if form.fallback is not None:
                return form.fallback.try_evaluate(scopes)
            return form.literal
        if isinstance(form, _LegacyPath):
            return self._eval_legacy(form, scopes)
        if isinstance(form, _Cmp):
            lhs = form.lhs.try_evaluate(scopes)
            if lhs is MISSING:
                lhs = None           # bundle-gate semantic: missing compares as None
            if form.op == "==":
                return lhs == form.rhs
            if form.op == "!=":
                return lhs != form.rhs
            # #1275 — ordered comparators (data gates). None/mismatched types
            # → False (fail-closed), never a TypeError out of a gate.
            ordered = {">=": operator.ge, "<=": operator.le,
                       ">": operator.gt, "<": operator.lt}
            try:
                return bool(ordered[form.op](lhs, form.rhs))
            except TypeError:
                return False
        if isinstance(form, _Jmes):
            try:
                return form.compiled.search(scopes.document())
            except JMESPathError as e:  # runtime type errors etc.
                raise ExprError(str(e), expr=self.raw, source=self.source) from e
        raise ExprError("unrecognized compiled form", expr=self.raw, source=self.source)

    def evaluate(self, scopes: Scopes) -> Any:
        val = self.try_evaluate(scopes)
        if val is MISSING:
            raise ExprError(
                self._missing_message(scopes), expr=self.raw, source=self.source
            )
        return val

    # -- helpers -------------------------------------------------------------

    def references(self) -> list[tuple[str, list[str]]]:
        """Scoped paths this expression reads — used by static wire validation."""
        if self._is_literal:
            return []
        return self._form.references()

    def _eval_legacy(self, form: _LegacyPath, scopes: Scopes) -> Any:
        if form.scope == "env":
            name = form.parts[0]
            if scopes.env is not None and name in scopes.env:
                return scopes.env[name]
            if scopes.use_os_env and name in os.environ:
                return os.environ[name]
            return MISSING
        root: Any
        if form.scope == "state":
            root = scopes.state
        elif form.scope == "input":
            root = scopes.scope_input()
        elif form.scope == "nodes":
            root = scopes.scope_nodes()
        elif form.scope == "artifacts":
            root = scopes.scope_artifacts()
        elif form.scope == "config":
            root = scopes.config or {}
        elif form.scope == "steps":
            # ``$steps.<id>.<field>`` reads the step's output top-level first,
            # then ``findings.<field>`` — deterministic operators write their
            # verdicts under findings (ADR-0066/#585). The fallback is a scope
            # semantic, not a bundle-local hack, so it lives HERE.
            steps = scopes.steps or {}
            if not form.parts:
                return MISSING
            return _step_lookup(
                steps.get(form.parts[0], {}), form.parts[1:],
                expr=self.raw, source=self.source,
            )
        else:  # pragma: no cover - compile guards against this
            raise ExprError(f"unknown scope {form.scope!r}", expr=self.raw, source=self.source)
        return _walk(root, form.parts, expr=self.raw, source=self.source)

    def _missing_message(self, scopes: Scopes) -> str:
        form = self._form
        if isinstance(form, _LegacyPath):
            if form.scope == "env":
                return f"env var {form.parts[0]!r} not set"
            # Re-walk to find the first missing segment and list neighbors —
            # this preserves the actionable error the old resolver gave.
            root_expr = Expr(raw=self.raw, source=self.source,
                             _form=_LegacyPath(form.scope, []))
            cur = root_expr._eval_legacy(_LegacyPath(form.scope, []), scopes)
            for i, part in enumerate(form.parts):
                if isinstance(cur, dict):
                    if part not in cur:
                        return (
                            f"missing key {part!r} in path "
                            f"{'.'.join(form.parts)!r} (available: {sorted(cur.keys())})"
                        )
                    cur = cur[part]
                elif hasattr(cur, part):
                    cur = getattr(cur, part)
                else:
                    return f"cannot resolve {part!r} on {type(cur).__name__}"
        if isinstance(form, _Default) and form.fallback is not None:
            return (
                f"neither {form.inner.raw!r} nor its fallback "
                f"{form.fallback.raw!r} resolved"
            )
        return "path resolved to nothing"


# ─── compilation ──────────────────────────────────────────────────────────


def compile_expr(raw: Any, *, source: str) -> Expr:
    """Compile ONE expression. Non-``$``-strings and non-strings are literals.

    Legacy dialect heads (``$state. $input. $nodes. $artifacts. $config.
    $steps. $env.`` and ``$default(...)``) compile to the translation shim;
    anything else starting with ``$jmes:`` or given via
    :func:`compile_predicate` compiles as JMESPath. Malformed → ExprError at
    COMPILE time, naming ``source`` and the expression.
    """
    if not isinstance(raw, str) or not raw.startswith("$"):
        return Expr(raw=repr(raw), source=source, _form=raw, _is_literal=True)

    m = _DEFAULT_RE.match(raw)
    if m:
        first, second = _split_default_args(m.group(1), expr=raw, source=source)
        inner = compile_expr(first, source=source)
        if second.strip().startswith("$"):
            # binding fallback: $default($state.x, $input.y) — see _Default
            fb = compile_expr(second.strip(), source=source)
            return Expr(raw=raw, source=source, _form=_Default(inner, fallback=fb))
        literal = _parse_literal(second, expr=raw, source=source)
        return Expr(raw=raw, source=source, _form=_Default(inner, literal))

    # Explicit new-grammar escape hatch during the shim period.
    if raw.startswith("$jmes:"):
        return _compile_jmes(raw[len("$jmes:"):], raw=raw, source=source)

    m = _LEGACY_HEAD_RE.match(raw)
    if m:
        scope, rest = m.group(1), m.group(2)
        if scope == "env":
            if "." in rest:
                raise ExprError(
                    "$env takes a flat NAME with no dots", expr=raw, source=source
                )
            return Expr(raw=raw, source=source, _form=_LegacyPath("env", [rest]))
        parts = [p for p in rest.split(".") if p != ""]
        if not parts or len(parts) != rest.count(".") + 1:
            raise ExprError(
                "empty path segment (double dot?)", expr=raw, source=source
            )
        return Expr(raw=raw, source=source, _form=_LegacyPath(scope, parts))

    if "." not in raw:
        raise ExprError("form must be $<scope>.<path>", expr=raw, source=source)
    raise ExprError(
        f"unsupported scope {raw.split('.', 1)[0]!r} "
        f"(allowed: $state, $input, $nodes, $artifacts, $config, $steps, "
        f"$env, $default, $jmes:)",
        expr=raw, source=source,
    )


def _compile_jmes(text: str, *, raw: str, source: str) -> Expr:
    try:
        compiled = jmespath.compile(text)
    except JMESPathError as e:
        raise ExprError(f"invalid JMESPath: {e}", expr=raw, source=source) from e
    return Expr(raw=raw, source=source, _form=_Jmes(compiled, text))


def coerce_literal(tok: str) -> Any:
    """Bundle-gate literal coercion: quoted string, ``true``/``false``,
    int/float — and a BARE WORD is a string (``== pass`` means ``== "pass"``).
    Looser than :func:`_parse_literal` on purpose: gate predicates predate the
    JSON-shaped literal rule and shipped bundles rely on the loose form."""
    t = tok.strip()
    if (t[:1], t[-1:]) in (("'", "'"), ('"', '"')):
        return t[1:-1]
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return t


def compile_predicate(raw: str, *, source: str) -> Expr:
    """Compile a boolean predicate (bundle/pipeline gates, future ``when:``).

    Accepts BOTH forms:
    - legacy: ``$steps.<id>.<field> == 'pass'`` / ``$input.x != 'y'`` / a bare
      legacy ref (truthiness). The lhs resolves through the legacy walk so the
      ``$steps`` findings-fallback and missing→None semantics are preserved;
    - new grammar: any JMESPath expression over the scoped document
      ``{input, state, config, steps, ...}``, e.g. ``steps.gate.verdict == 'pass'``.

    An empty/blank predicate is vacuously true (bundle-gate convention).
    """
    if not raw or not raw.strip():
        return Expr(raw=raw, source=source, _form=True, _is_literal=True)
    m = _LEGACY_PRED_RE.match(raw)
    if m and _LEGACY_HEAD_RE.match(m.group(1)):
        lhs = compile_expr(m.group(1), source=source)
        rhs = coerce_literal(m.group(3))
        return Expr(raw=raw, source=source, _form=_Cmp(lhs, m.group(2), rhs))
    if raw.strip().startswith("$"):
        # a bare legacy ref used as a truthiness predicate
        return compile_expr(raw.strip(), source=source)
    return _compile_jmes(raw, raw=raw, source=source)


def resolve_value(
    value: Any,
    scopes: Scopes,
    *,
    source: str,
    on_missing: str = "raise",
) -> Any:
    """Resolve one value: containers recursively, ``$``-strings as expressions,
    everything else literal. ``on_missing``: ``"raise"`` (operations) |
    ``"omit"`` (``$config`` — the caller drops :data:`MISSING` entries) |
    ``"none"``.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            r = resolve_value(v, scopes, source=source, on_missing=on_missing)
            if r is MISSING:
                continue  # omit semantics bubble up naturally for dict entries
            out[k] = r
        return out
    if isinstance(value, list):
        return [
            r for r in (
                resolve_value(v, scopes, source=source, on_missing=on_missing)
                for v in value
            )
            if r is not MISSING
        ]
    expr = compile_expr(value, source=source)
    if on_missing == "raise":
        return expr.evaluate(scopes)
    val = expr.try_evaluate(scopes)
    if val is MISSING:
        return MISSING if on_missing == "omit" else None
    return val
