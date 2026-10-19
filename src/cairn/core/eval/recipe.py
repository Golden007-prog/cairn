"""ADR-0057 (#442) — the scoring-pass dispatcher for eval recipes.

An eval **recipe** (:class:`cairn.core.pack.manifest.EvalRecipe`) is the same
declarative shape as a demo seed — an operator ref (``uses``) plus input
bindings (``inputs``) — but wired to a run's **output** instead of the
workflow's input. This module runs one: it resolves the recipe's ``$output.*``
selectors against a run's stored output, validates against the eval operator's
input schema, invokes the operator through the ordinary operator machinery, and
returns a normalized verdict.

It is the single primitive behind both eval homes (ADR-0057 D4):

  - **TEST eval** (``[demo.*].eval``) — ``cairn eval`` / CI score a demo tier's
    output against a golden set (``eval/subset``, deterministic).
  - **RUNTIME eval** (``[eval.*]``) — a decoupled post-step over a real run's
    stored output (``model/gate`` / ``eval/judge`` / ``eval/benchmark``); this
    is also the D7 "Evaluate this result" on-demand path.

Because scoring is a **decoupled post-step over a run's stored output** (D6),
this never re-runs the workflow — it works on any completed/historical run.

The recipe is **declarative-only** (the ADR guardrail): a ``uses`` operator ref
plus input bindings. There is no inline scoring here — custom scoring is always
a real operator, resolved through the same registry as workflow operators (D2).
The ``$output.*`` selector is deliberately a small, recipe-local grammar and
does **not** touch the engine's §24.20.6 binding grammar.
"""

from __future__ import annotations

import inspect
import json as _json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.operator import OperationContext, ensure_loaded
from cairn.core.operator.registry import default_registry
from cairn.core.pack.manifest import EvalContract, EvalRecipe

_log = logging.getLogger("cairn.eval.recipe")

#: The recipe-local selector head that binds a run's stored output into an eval
#: operator's inputs. ``$output`` → the whole output; ``$output.a.b`` → a leaf.
_OUTPUT_HEAD = "$output"


class EvalRecipeError(ValueError):
    """Raised when a recipe's ``$output.*`` selector cannot be resolved."""


class EvalRecipeResult(BaseModel):
    """Normalized outcome of one scoring pass.

    ``output`` always carries the eval operator's full result; ``passed`` /
    ``verdict`` are a best-effort normalization across the eval operators
    (``eval/subset`` → ``passed``; ``model/gate`` → ``verdict`` pass/warn/fail),
    so callers can render a single yes/no without knowing the operator.
    """

    uses: str
    #: Tri-state: ``True``/``False`` when a verdict could be derived, ``None``
    #: when the operator's output carries no recognizable pass signal.
    passed: Optional[bool] = None
    #: The raw verdict string when the operator emits one (gates: pass/warn/fail).
    verdict: Optional[str] = None
    #: The eval operator's full output dict (whatever it returned).
    output: dict[str, Any] = Field(default_factory=dict)
    #: Set when the operator opted out (e.g. eval disabled / no golden / no samples).
    skipped_reason: Optional[str] = None
    #: Set when the recipe could not be run at all (bad selector, unknown
    #: operator, input validation failure). ``passed`` stays ``None`` then.
    error: Optional[str] = None

    @property
    def ran(self) -> bool:
        """True when the eval operator actually executed (no dispatch error)."""
        return self.error is None


# ─── $output.* selector resolution ───────────────────────────────────


def _walk_output(output: Any, dotted: str, *, source: str) -> Any:
    """Walk ``output`` along a dotted path, raising on a missing key.

    Mirrors the engine resolver's dict/attr walk but is scoped to the
    recipe-local ``$output`` head only."""
    if not dotted:
        return output
    cur: Any = output
    for part in dotted.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise EvalRecipeError(
                    f"selector {source!r}: missing key {part!r} in output path "
                    f"{dotted!r} (available: {sorted(cur.keys())})"
                )
            cur = cur[part]
        elif hasattr(cur, part):
            cur = getattr(cur, part)
        else:
            raise EvalRecipeError(
                f"selector {source!r}: cannot resolve {part!r} on "
                f"{type(cur).__name__}"
            )
    return cur


def _resolve_selectors(expr: Any, output: dict[str, Any]) -> Any:
    """Recursively replace ``$output.*`` strings in ``expr`` with values read
    from ``output``. Everything else (literals, resolved file paths, other
    strings) passes through untouched."""
    if isinstance(expr, dict):
        return {k: _resolve_selectors(v, output) for k, v in expr.items()}
    if isinstance(expr, list):
        return [_resolve_selectors(v, output) for v in expr]
    if isinstance(expr, str) and (expr == _OUTPUT_HEAD or expr.startswith(_OUTPUT_HEAD + ".")):
        rest = expr[len(_OUTPUT_HEAD):]
        return _walk_output(output, rest.lstrip("."), source=expr)
    return expr


# ─── verdict normalization ───────────────────────────────────────────


def _normalize_verdict(out: dict[str, Any]) -> tuple[Optional[bool], Optional[str]]:
    """Best-effort (passed, verdict) from an eval operator's output.

    - ``eval/subset`` → boolean ``passed``.
    - ``model/gate`` (and any pass/warn/fail gate) → ``verdict``; ``fail`` is the
      only non-pass (``warn`` is non-blocking, so it counts as passed).
    Returns ``(None, None)`` when neither signal is present (e.g. a bare judge
    score) — the caller still gets the full ``output``.
    """
    passed = out.get("passed")
    verdict = out.get("verdict")
    if isinstance(verdict, str):
        return (verdict != "fail", verdict)
    if isinstance(passed, bool):
        return (passed, None)
    return (None, None)


# ─── the dispatcher ──────────────────────────────────────────────────


async def run_eval_recipe(
    recipe: EvalRecipe,
    output: dict[str, Any],
    *,
    pack_root: Optional[Path] = None,
    is_mocked: bool = False,
    registry: Any = None,
) -> EvalRecipeResult:
    """Score a run's ``output`` with one eval ``recipe``.

    :param recipe: the parsed recipe (``uses`` + ``inputs``).
    :param output: the run's stored output — the ``$output.*`` selector root.
    :param pack_root: when given, resolve the recipe's pack-relative golden
        paths against it first (``recipe.resolved(root)``); pass ``None`` if the
        recipe is already resolved or uses no file paths.
    :param is_mocked: take the operator's ``mock()`` path when it declares
        ``supports_mock`` — hermetic, no LLM/GPU/file I/O.
    :param registry: operator registry (defaults to the process
        :data:`default_registry`).

    Never raises for expected conditions — a bad selector, an unknown operator,
    or an input-validation failure come back as ``EvalRecipeResult.error`` so a
    scoring pass over many runs doesn't abort on one bad recipe.
    """
    reg = registry or default_registry
    ensure_loaded()  # entry-point operators (idempotent)

    if pack_root is not None:
        recipe = recipe.resolved(Path(pack_root))

    # 1. Resolve $output.* selectors against the run's output.
    try:
        resolved_inputs = _resolve_selectors(recipe.inputs, output)
    except EvalRecipeError as e:
        return EvalRecipeResult(uses=recipe.uses, error=str(e))

    # 2. Look up the eval operator (D2 — resolved like any operator).
    if not reg.has(recipe.uses):
        return EvalRecipeResult(
            uses=recipe.uses,
            error=(
                f"eval operator {recipe.uses!r} is not registered — install the "
                f"pack that provides it (recipe `uses`)."
            ),
        )
    op_cls = reg.get(recipe.uses)

    # 3. Validate the resolved inputs against the operator's input schema.
    try:
        inp = op_cls.input_schema.model_validate(resolved_inputs)
    except Exception as e:  # noqa: BLE001 — surface pydantic detail as a soft error
        return EvalRecipeResult(
            uses=recipe.uses,
            error=f"eval input failed validation for {recipe.uses!r}: {e}",
        )

    # 4. Invoke through the ordinary operator surface (async-aware).
    ctx = OperationContext(
        pipeline_id="eval-recipe",
        node_name=recipe.uses,
        is_mocked=is_mocked,
        logger=_log,
    )
    op = op_cls()
    if is_mocked and getattr(op_cls, "supports_mock", False):
        out = op.mock(inp, ctx)
    else:
        out = op.run(inp, ctx)
    if inspect.iscoroutine(out):
        out = await out
    out_dict = out.model_dump() if hasattr(out, "model_dump") else dict(out)

    passed, verdict = _normalize_verdict(out_dict)
    return EvalRecipeResult(
        uses=recipe.uses,
        passed=passed,
        verdict=verdict,
        output=out_dict,
        skipped_reason=out_dict.get("skipped_reason"),
    )


# ─── run-output extraction + recipe resolution (D7 on-demand eval) ───


def _canonical_output(chosen: dict[str, Any]) -> dict[str, Any]:
    """A run's **canonical declared output** — the honest-eval fix (#458).

    A report-producing template (one whose ``output`` block declares a schema,
    e.g. ``ReadinessReport`` / ``FinetuneReport``) synthesizes that report into
    ``state.final_output`` (``compiler.py``); the raw run state ALSO carries
    ``findings`` / ``map_results`` / ``template_input`` / …. The **declared**
    output is the report, not the raw state — so when ``final_output`` is a JSON
    object (a dict, or a JSON string that parses to one) it IS that report and we
    return it, giving ``$output`` the clean top-level shape the recipe's
    ``$output.<field>`` selectors expect (``$output.verdict`` /
    ``$output.metrics``), instead of a state where the verdict is buried under
    ``$output.findings``.

    Back-compat: a run with no report (no ``final_output``, or one that isn't a
    JSON object — e.g. the ``{"metrics": {...}}`` shape some templates/tests
    already store directly) falls through to ``chosen`` unchanged.

    Scoped to the eval read path (``extract_run_output``) on purpose: pilot,
    artifacts, and replay read ``result_payloads`` directly and keep the full
    state. Projecting the STORED run output for every consumer is the broader
    canonical-output refactor (#278)."""
    fo = chosen.get("final_output")
    if isinstance(fo, dict) and fo:
        return fo
    if isinstance(fo, str) and fo.strip().startswith("{"):
        try:
            parsed = _json.loads(fo)
        except (ValueError, TypeError):
            return chosen
        if isinstance(parsed, dict) and parsed:
            return parsed
    return chosen


def extract_run_output(result_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``$output`` root for a completed run: the operator's structured output.

    A run stores one or more ``RunResult`` payloads (``Run.result_payloads``);
    the domain operator's result lives under each payload's ``output`` key (same
    source :func:`cairn_platform.pilot._outcome_from_payloads` reads). We take the
    **last** payload that carries a non-empty ``output`` dict — the terminal
    step's output — falling back to the last payload itself when none nest an
    ``output``, then project to the run's **canonical declared output**
    (:func:`_canonical_output`, #458). Returns ``{}`` for a run with no
    structured result."""
    chosen: dict[str, Any] = {}
    for p in result_payloads or []:
        if not isinstance(p, dict):
            continue
        out = p.get("output")
        if isinstance(out, dict) and out:
            chosen = out  # keep the last non-empty one
        elif not chosen:
            chosen = p  # fallback: the payload itself
    return _canonical_output(chosen)


def _pack_root_for(pack: Any) -> Path:
    """Pack root = parent of any known asset dir (mirrors the CLI + contract)."""
    for attr in ("flows_dir", "templates_dir", "evals_dir", "examples_dir",
                 "schemas_dir", "skills_dir"):
        d = getattr(pack, attr, None)
        if d:
            return Path(d).parent
    return Path(".")


def _resolve_pack_and_manifest(template_ref: str):
    """Find the installed pack that owns ``template_ref`` (``pack/template`` or a
    bare template name). Returns ``(pack, manifest, template_name)`` or ``None``.

    Mirrors :func:`cairn.core.ui_contract._demos`' pack iteration."""
    from cairn.core.pack import ensure_loaded as _ensure_packs
    from cairn.core.pack.registry import get_packs

    _ensure_packs()  # idempotent — populate the pack registry if not yet loaded
    if not template_ref:
        return None
    pack_name, _, tmpl = template_ref.partition("/")
    if not tmpl:  # bare template name — search every pack
        pack_name, tmpl = None, template_ref
    for p in get_packs():
        if pack_name and p.name != pack_name:
            continue
        manifest = getattr(p, "manifest", None)
        if manifest is None:
            continue
        # A pack owns the template if it declares a demo or runtime eval for it,
        # or (bare-name search) simply names it — good enough to locate recipes.
        # Demo tiers moved next to the flow (ADR-0106 D5), so `demos_for` is the
        # question, not the manifest table it used to live in.
        from cairn.core.pack.demos import demos_for  # noqa: PLC0415 - cycle

        if tmpl in (getattr(manifest, "runtime_evals", None) or {}) or demos_for(p, tmpl):
            return p, manifest, tmpl
    return None


def resolve_runtime_contract(template_ref: str) -> Optional[tuple["EvalContract", Path]]:
    """The workflow's reference-free ``[eval.*]`` runtime CONTRACT (#487 — gates
    + objectives + slices) + its pack root, or ``None`` when none is declared."""
    found = _resolve_pack_and_manifest(template_ref)
    if not found:
        return None
    pack, manifest, tmpl = found
    contract = (getattr(manifest, "runtime_evals", None) or {}).get(tmpl)
    return (contract, _pack_root_for(pack)) if contract else None


def resolve_runtime_recipe(template_ref: str) -> Optional[tuple[EvalRecipe, Path]]:
    """Back-compat single-recipe view: the contract's PRIMARY (first) gate + its
    pack root, or ``None``. Used by the one-shot ``POST /runs/{id}/evaluate``
    path; auto-eval scores the whole contract via :func:`resolve_runtime_contract`."""
    found = resolve_runtime_contract(template_ref)
    if not found:
        return None
    contract, root = found
    gate = contract.primary_gate
    return (gate, root) if gate else None


def resolve_test_recipe(
    template_ref: str, tier: Optional[str] = None
) -> Optional[tuple[EvalRecipe, Path]]:
    """A demo tier's ``eval:`` TEST recipe + its pack root. Picks the
    tier named by ``tier`` (or the first tier that carries a recipe when ``tier``
    is ``None``). ``None`` when no matching tier declares a recipe."""
    found = _resolve_pack_and_manifest(template_ref)
    if not found:
        return None
    pack, _manifest, tmpl = found
    from cairn.core.pack.demos import demos_for  # noqa: PLC0415 - cycle

    for spec in demos_for(pack, tmpl):
        if spec.eval is None:
            continue
        if tier and spec.tier != tier:
            continue
        return spec.eval, _pack_root_for(pack)
    return None


__all__ = [
    "EvalRecipeError",
    "EvalRecipeResult",
    "run_eval_recipe",
    "extract_run_output",
    "resolve_runtime_recipe",
    "resolve_runtime_contract",
    "resolve_test_recipe",
]
