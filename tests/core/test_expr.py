"""The ONE expression language (#1118, R6b — ADR-0083 D3).

Four dialects, four resolvers existed for "pass this value there". These tests
pin the unified module's contract — INCLUDING the three semantic traps that
made naive unification wrong:

  1. ``$default`` falls back on missing-or-None ONLY — never on ``false``/``0``
     (#630 regression class; JMESPath ``||`` would get this wrong).
  2. ``$config`` omits on missing (the kernel's own default applies) while
     ``$state``/``$input`` raise — one resolver, two policies via try_evaluate.
  3. Legacy paths do attribute access on Pydantic-shaped objects; JMESPath is
     dict-only, so the legacy walk must survive the shim period.
"""
from __future__ import annotations

import pytest

from cairn.core.engine.expr import (
    MISSING,
    Expr,
    ExprError,
    Scopes,
    compile_expr,
    compile_predicate,
    resolve_value,
)


def _scopes(**kw) -> Scopes:
    kw.setdefault("use_os_env", False)
    return Scopes(**kw)


# ─── literals ────────────────────────────────────────────────────────────────

def test_non_dollar_strings_and_non_strings_are_literals():
    s = _scopes(state={})
    assert compile_expr("hello", source="t").evaluate(s) == "hello"
    assert compile_expr(42, source="t").evaluate(s) == 42
    assert compile_expr(None, source="t").evaluate(s) is None


# ─── legacy paths ────────────────────────────────────────────────────────────

def test_state_and_input_dotted_paths():
    s = _scopes(state={"findings": {"verdict": "pass"}}, input={"path": "/d.jsonl"})
    assert compile_expr("$state.findings.verdict", source="t").evaluate(s) == "pass"
    assert compile_expr("$input.path", source="t").evaluate(s) == "/d.jsonl"


def test_input_falls_back_to_state_template_input():
    """The operations resolver read $input from state['template_input'] when no
    explicit input was passed — call sites depend on that."""
    s = _scopes(state={"template_input": {"x": 7}})
    assert compile_expr("$input.x", source="t").evaluate(s) == 7


def test_attribute_access_on_pydantic_shaped_objects():
    class Obj:
        verdict = "warn"
    s = _scopes(state={"gate": Obj()})
    assert compile_expr("$state.gate.verdict", source="t").evaluate(s) == "warn"


def test_missing_path_raises_naming_source_expr_and_available_keys():
    s = _scopes(state={"findings": {"a": 1}})
    with pytest.raises(ExprError) as e:
        compile_expr("$state.findings.zzz", source="ops/run-x.yaml").evaluate(s)
    msg = str(e.value)
    assert "ops/run-x.yaml" in msg and "$state.findings.zzz" in msg and "'a'" in msg


# ─── $default: the falsy trap ────────────────────────────────────────────────

def test_default_falls_back_on_missing_and_on_none():
    s = _scopes(state={}, input={"present_none": None})
    e = compile_expr("$default($input.absent, 50)", source="t")
    assert e.evaluate(s) == 50
    e = compile_expr("$default($input.present_none, 50)", source="t")
    assert e.evaluate(s) == 50


def test_default_does_NOT_fall_back_on_false_or_zero():
    """#630's class: `false` is a value, not an absence. JMESPath `||` would
    swallow it — which is exactly why $default is handled here, not translated."""
    s = _scopes(input={"flag": False, "n": 0})
    assert compile_expr("$default($input.flag, true)", source="t").evaluate(s) is False
    assert compile_expr("$default($input.n, 9)", source="t").evaluate(s) == 0


def test_default_literal_accepts_json_and_python_spellings():
    s = _scopes(state={})
    assert compile_expr("$default($input.x, false)", source="t").evaluate(s) is False
    assert compile_expr("$default($input.x, 'qlora')", source="t").evaluate(s) == "qlora"
    assert compile_expr('$default($input.x, {"a": 1})', source="t").evaluate(s) == {"a": 1}


def test_default_with_a_BINDING_fallback_resolves_it():
    """`$default($state.findings.curated_path, $input.dataset_path)` — the
    #926 chained-default form shipped in run-stage-dataset/run-prep-anonymize.
    The OLD resolver parsed the 2nd arg lazily and only as a literal, so this
    crashed exactly when the fallback was needed — found by the #1118
    equivalence harness. The intent works now."""
    s = _scopes(state={}, input={"dataset_path": "/raw.jsonl"})
    e = compile_expr("$default($state.findings.curated_path, $input.dataset_path)", source="t")
    assert e.evaluate(s) == "/raw.jsonl"
    # curated wins when present
    s2 = _scopes(state={"findings": {"curated_path": "/cur.jsonl"}},
                 input={"dataset_path": "/raw.jsonl"})
    assert e.evaluate(s2) == "/cur.jsonl"


def test_default_with_binding_fallback_BOTH_missing_raises():
    with pytest.raises(ExprError):
        compile_expr("$default($state.a, $input.b)", source="t").evaluate(_scopes())


def test_default_literal_with_top_level_comma_inside_quotes():
    s = _scopes(state={})
    e = compile_expr('$default($input.x, "hello, world")', source="t")
    assert e.evaluate(s) == "hello, world"


# ─── $config: omit-on-missing ────────────────────────────────────────────────

def test_config_missing_is_MISSING_not_an_error_via_try_evaluate():
    """dataflow defs drop the key so the kernel's own default applies."""
    s = _scopes(config={"present": 1})
    assert compile_expr("$config.present", source="t").try_evaluate(s) == 1
    assert compile_expr("$config.absent", source="t").try_evaluate(s) is MISSING


# ─── $env ────────────────────────────────────────────────────────────────────

def test_env_flat_name_and_no_dots_rule():
    s = _scopes(env={"TOKEN": "abc"})
    assert compile_expr("$env.TOKEN", source="t").evaluate(s) == "abc"
    with pytest.raises(ExprError):
        compile_expr("$env.A.B", source="t")


def test_env_missing_raises_without_os_fallback():
    with pytest.raises(ExprError) as e:
        compile_expr("$env.CAIRN_TEST_SURELY_UNSET", source="t").evaluate(_scopes())
    assert "not set" in str(e.value)


# ─── compile-time failures name file + expression ────────────────────────────

@pytest.mark.parametrize("bad", [
    "$default($input.x)",          # one arg
    "$bogus.path",                 # unknown scope
    "$state",                      # no path
    "$state..double",              # empty segment
    "$jmes:a..b",                  # malformed JMESPath
])
def test_malformed_expressions_fail_at_COMPILE_time(bad):
    with pytest.raises(ExprError) as e:
        compile_expr(bad, source="packs/x/flows/y.md")
    assert "packs/x/flows/y.md" in str(e.value)


# ─── predicates (bundle gates → future when:) ────────────────────────────────

def test_legacy_steps_predicate_translates_and_evaluates():
    s = _scopes(steps={"gate": {"verdict": "pass"}})
    p = compile_predicate("$steps.gate.verdict == 'pass'", source="t")
    assert p.evaluate(s) is True
    p = compile_predicate("$steps.gate.verdict != 'pass'", source="t")
    assert p.evaluate(s) is False


def test_legacy_predicate_with_hyphenated_step_id():
    """Step/pack ids carry hyphens; JMESPath identifiers can't unquoted — the
    translation must quote segments or every hyphenated pack breaks."""
    s = _scopes(steps={"check-data": {"verdict": "warn"}})
    p = compile_predicate("$steps.check-data.verdict == 'warn'", source="t")
    assert p.evaluate(s) is True


def test_new_grammar_predicate_is_raw_jmespath_over_the_scoped_document():
    s = _scopes(state={"dataset_verdict": "warn"}, input={"budget": 10})
    assert compile_predicate("state.dataset_verdict != 'fail'", source="t").evaluate(s) is True
    assert compile_predicate("input.budget > `5`", source="t").evaluate(s) is True


def test_bare_legacy_ref_predicate_is_truthiness():
    s = _scopes(state={"findings": {"watch_crossed": True}})
    p = compile_predicate("$state.findings.watch_crossed", source="t")
    assert p.evaluate(s) is True


def test_steps_scope_reads_findings_FALLBACK():
    """Deterministic operators write verdicts under findings (ADR-0066/#585);
    `$steps.gate.verdict` must find findings.verdict or every bundle gate whose
    step is operator-backed silently compares None."""
    s = _scopes(steps={"gate": {"findings": {"verdict": "pass"}}})
    assert compile_expr("$steps.gate.verdict", source="t").try_evaluate(s) == "pass"
    p = compile_predicate("$steps.gate.verdict == 'pass'", source="t")
    assert p.evaluate(s) is True


def test_steps_top_level_wins_over_findings():
    s = _scopes(steps={"g": {"verdict": "warn", "findings": {"verdict": "pass"}}})
    assert compile_expr("$steps.g.verdict", source="t").try_evaluate(s) == "warn"


def test_predicate_missing_lhs_compares_as_None_not_raise():
    """Bundle-gate semantic: an absent step field is None, so `!= 'pass'` is
    True and `== 'pass'` is False — never an exception mid-pipeline."""
    s = _scopes(steps={})
    assert compile_predicate("$steps.gone.verdict == 'pass'", source="t").evaluate(s) is False
    assert compile_predicate("$steps.gone.verdict != 'pass'", source="t").evaluate(s) is True


def test_predicate_bare_word_literal_is_a_string():
    """Gate predicates predate the JSON literal rule: `== pass` means the
    string 'pass' — shipped bundles rely on the loose coercion."""
    s = _scopes(steps={"g": {"verdict": "pass"}})
    assert compile_predicate("$steps.g.verdict == pass", source="t").evaluate(s) is True


def test_empty_predicate_is_vacuously_true():
    assert compile_predicate("", source="t").evaluate(_scopes()) is True
    assert compile_predicate("   ", source="t").evaluate(_scopes()) is True


# ─── resolve_value (container recursion, the operations entry point) ─────────

def test_resolve_value_recurses_containers_and_passes_literals():
    s = _scopes(state={"findings": {"usd": 3.5}}, input={"m": "loss"})
    out = resolve_value(
        {"usd": "$state.findings.usd", "metric": "$input.m",
         "flat": 1, "nested": ["$input.m", "lit"]},
        s, source="t",
    )
    assert out == {"usd": 3.5, "metric": "loss", "flat": 1, "nested": ["loss", "lit"]}


def test_resolve_value_omit_mode_drops_missing_config_keys():
    s = _scopes(config={"a": 1})
    out = resolve_value(
        {"a": "$config.a", "b": "$config.absent"}, s, source="t", on_missing="omit"
    )
    assert out == {"a": 1}          # 'b' omitted → kernel default applies


def test_resolve_value_raise_mode_raises_on_missing():
    with pytest.raises(ExprError):
        resolve_value({"x": "$state.absent"}, _scopes(state={}), source="t")


# ─── references (static wire validation feed) ────────────────────────────────

def test_references_expose_scoped_paths_for_wire_validation():
    e = compile_expr("$steps.train.adapter_uri", source="t")
    assert e.references() == [("steps", ["train", "adapter_uri"])]
    e = compile_expr("$default($input.x, 1)", source="t")
    assert e.references() == [("input", ["x"])]
