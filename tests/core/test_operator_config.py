"""ADR-0068 (#742) — layered operator config surface.

Covers the four Phase-1 seams:
  1. the pure resolver (deep_merge / precedence / input-wins),
  2. the profile `[profile.config]` parse + `extends` per-operator deep-merge,
  3. the runtime injection (config merged UNDER the bound input, gated on
     `config_schema`), via a real `invoke_operator_inprocess` round-trip,
  4. the doctor validation (valid / typo / unknown-op / no-config-schema).
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict, Field

from cairn.core.operator import register_operator
from cairn.core.operator.binding import OperationBinding
from cairn.core.operator.config import (
    OperatorConfigError,
    apply_operator_config,
    deep_merge,
    preflight_operator_configs,
    resolve_operator_config,
    validate_operator_configs,
)
from cairn.core.operator.protocol import Operation, OperationContext
from cairn.core.operator.runtime import invoke_operator_inprocess


# ─── 1. pure resolver ──────────────────────────────────────────────────────


def test_deep_merge_is_recursive_and_nonmutating():
    base = {"a": 1, "t": {"x": 1, "y": 2}}
    over = {"t": {"y": 20, "z": 30}, "b": 9}
    out = deep_merge(base, over)
    assert out == {"a": 1, "b": 9, "t": {"x": 1, "y": 20, "z": 30}}
    assert base == {"a": 1, "t": {"x": 1, "y": 2}}  # inputs untouched


def test_resolve_precedence_run_over_profile():
    prof = {"dataset/gate": {"thresholds": {"format_min_conformant_pct": 90.0, "refusal_warn_pct": 20.0}}}
    run = {"dataset/gate": {"thresholds": {"format_min_conformant_pct": 80.0}}}
    eff = resolve_operator_config("dataset/gate", profile_config=prof, run_overrides=run)
    # run wins on the key it sets; profile's other key survives the deep-merge
    assert eff == {"thresholds": {"format_min_conformant_pct": 80.0, "refusal_warn_pct": 20.0}}


def test_resolve_empty_when_operator_absent():
    assert resolve_operator_config("x/y", profile_config={"a/b": {"k": 1}}, run_overrides=None) == {}
    assert resolve_operator_config("x/y", profile_config=None, run_overrides=None) == {}


def test_resolve_precedence_run_over_workflow_over_profile():
    # ADR-0070 — operational precedence run > workflow > profile, per-key deep-merge
    prof = {"op": {"t": {"a": 1, "b": 1, "c": 1}}}
    wf = {"op": {"t": {"b": 2, "c": 2}}}
    run = {"op": {"t": {"c": 3}}}
    eff = resolve_operator_config("op", profile_config=prof, workflow_config=wf, run_overrides=run)
    assert eff == {"t": {"a": 1, "b": 2, "c": 3}}


def test_apply_config_is_under_input():
    resolved_input = {"sample_size": 500}  # explicitly bound by the YAML/run
    config = {"sample_size": 1000, "backend": "presidio"}
    out = apply_operator_config(resolved_input, config)
    assert out == {"sample_size": 500, "backend": "presidio"}  # bound input wins; config fills the gap


# ─── 2. profile parse + extends ────────────────────────────────────────────


def test_profile_config_parse_and_extends_deepmerge(tmp_path):
    from cairn.core.pack.profile import load_profile

    (tmp_path / "base.toml").write_text(
        '[profile]\n'
        '[profile.config."dataset/gate".thresholds]\n'
        'format_min_conformant_pct = 90.0\n'
        'decontam_max_overlap_pct = 10.0\n',
        encoding="utf-8",
    )
    (tmp_path / "prod.toml").write_text(
        '[profile]\n'
        'extends = "base.toml"\n'
        '[profile.config."dataset/gate".thresholds]\n'
        'format_min_conformant_pct = 99.0\n',  # override ONE knob
        encoding="utf-8",
    )
    prof = load_profile(tmp_path / "prod.toml")
    # child overrides format floor; parent's decontam knob survives (deep-merge)
    assert prof.config["dataset/gate"]["thresholds"] == {
        "format_min_conformant_pct": 99.0,
        "decontam_max_overlap_pct": 10.0,
    }


def test_profile_config_rejects_non_table(tmp_path):
    from cairn.core.pack.profile import ProfileError, load_profile

    (tmp_path / "bad.toml").write_text(
        '[profile]\n[profile.config]\n"dataset/gate" = "oops"\n', encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="must be a table"):
        load_profile(tmp_path / "bad.toml")


def test_profile_no_config_key_is_empty(tmp_path):
    from cairn.core.pack.profile import load_profile

    (tmp_path / "p.toml").write_text('[profile]\n[profile.backends]\nmemory = true\n', encoding="utf-8")
    assert load_profile(tmp_path / "p.toml").config == {}  # back-compat


# ─── 3. runtime injection round-trip ───────────────────────────────────────


class _Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")  # config surface forbids extras → typos fail
    limit: int = Field(100, title="Row limit")


class _CfgInput(BaseModel):
    payload: str
    thresholds: _Thresholds = Field(default_factory=_Thresholds)


class _CfgOutput(BaseModel):
    seen_limit: int
    payload: str


class _CfgConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thresholds: _Thresholds = Field(default_factory=_Thresholds)


@register_operator(name="test/cfg_op", version="0.1.0")
class _CfgOp(Operation[_CfgInput, _CfgOutput]):
    input_schema = _CfgInput
    output_schema = _CfgOutput
    config_schema = _CfgConfig

    def run(self, inp: _CfgInput, ctx: OperationContext) -> _CfgOutput:
        return _CfgOutput(seen_limit=inp.thresholds.limit, payload=inp.payload)


class _NoCfgOp_Input(BaseModel):
    payload: str
    thresholds: _Thresholds = Field(default_factory=_Thresholds)


@register_operator(name="test/nocfg_op", version="0.1.0")
class _NoCfgOp(Operation[_NoCfgOp_Input, _CfgOutput]):
    input_schema = _NoCfgOp_Input
    output_schema = _CfgOutput
    # NO config_schema — config must be ignored for this operator.

    def run(self, inp: _NoCfgOp_Input, ctx: OperationContext) -> _CfgOutput:
        return _CfgOutput(seen_limit=inp.thresholds.limit, payload=inp.payload)


def _invoke(op_cls, *, state, template_input=None):
    binding = OperationBinding(
        operator=op_cls.name,
        input={"payload": "$input.payload"},
        output={"seen_limit": "$state.seen_limit", "payload": "$state.out_payload"},
    )
    return asyncio.run(
        invoke_operator_inprocess(
            binding, op_cls,
            state=state, template_input=template_input,
            pipeline_id="p", node_name="n",
        )
    )


def test_injection_config_under_input():
    # profile config sets the knob; the binding doesn't bind thresholds → config supplies it
    state = {"template_input": {"payload": "hi"}, "operator_config": {"test/cfg_op": {"thresholds": {"limit": 7}}}}
    patch = _invoke(_CfgOp, state=state, template_input={"payload": "hi"})
    assert patch["seen_limit"] == 7  # came from profile config
    assert patch["out_payload"] == "hi"


def test_injection_run_override_beats_profile():
    state = {
        "template_input": {"payload": "hi", "config_overrides": {"test/cfg_op": {"thresholds": {"limit": 3}}}},
        "operator_config": {"test/cfg_op": {"thresholds": {"limit": 7}}},
    }
    patch = _invoke(_CfgOp, state=state, template_input=state["template_input"])
    assert patch["seen_limit"] == 3  # run override wins over profile


def test_injection_workflow_layer_beats_profile():
    # ADR-0070 — the seeded workflow_config channel overrides the profile layer
    state = {
        "template_input": {"payload": "hi"},
        "operator_config": {"test/cfg_op": {"thresholds": {"limit": 7}}},
        "workflow_config": {"test/cfg_op": {"thresholds": {"limit": 9}}},
    }
    patch = _invoke(_CfgOp, state=state, template_input={"payload": "hi"})
    assert patch["seen_limit"] == 9  # workflow > profile


def test_injection_run_beats_workflow():
    state = {
        "template_input": {"payload": "hi", "config_overrides": {"test/cfg_op": {"thresholds": {"limit": 3}}}},
        "operator_config": {"test/cfg_op": {"thresholds": {"limit": 7}}},
        "workflow_config": {"test/cfg_op": {"thresholds": {"limit": 9}}},
    }
    patch = _invoke(_CfgOp, state=state, template_input=state["template_input"])
    assert patch["seen_limit"] == 3  # run > workflow > profile


def test_seed_lifts_reserved_workflow_config_key():
    from cairn.core.engine.compiler import seed_initial_state

    st = seed_initial_state({"payload": "hi", "__workflow_config__": {"op": {"k": 1}}})
    assert st["workflow_config"] == {"op": {"k": 1}}
    assert "__workflow_config__" not in st["template_input"]  # kept out of $input


def test_injection_defaults_when_no_config():
    state = {"template_input": {"payload": "hi"}}  # no operator_config channel
    patch = _invoke(_CfgOp, state=state, template_input={"payload": "hi"})
    assert patch["seen_limit"] == 100  # the config_schema/input default — byte-for-byte back-compat


def test_injection_skipped_for_operator_without_config_schema():
    # even if a profile names it, an operator with no config_schema is not injected
    state = {"template_input": {"payload": "hi"}, "operator_config": {"test/nocfg_op": {"thresholds": {"limit": 7}}}}
    patch = _invoke(_NoCfgOp, state=state, template_input={"payload": "hi"})
    assert patch["seen_limit"] == 100  # config ignored → input default stands


# ─── 4. doctor validation ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _ensure_fake_ops_registered():
    """The doctor path looks the fake ops up in the registry, but another test in
    the suite may call ``default_registry.clear()`` + reload only entry-point
    operators, dropping our in-test fakes. Re-register (idempotently) before each
    test so these are isolation-proof."""
    from cairn.core.operator.registry import default_registry

    for cls in (_CfgOp, _NoCfgOp):
        if not default_registry.has(cls.name):
            default_registry.register(cls)
    yield


def _write_profile(tmp_path, body: str):
    p = tmp_path / "prof.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_doctor_config_valid(tmp_path, monkeypatch):
    from cairn.cli.commands.doctor import check_operator_config

    monkeypatch.setenv("OBS_PROFILE", str(_write_profile(
        tmp_path, '[profile]\n[profile.config."test/cfg_op".thresholds]\nlimit = 5\n',
    )))
    res = {r.name: r for r in check_operator_config()}
    assert res["config:test/cfg_op"].status == "ok"


def test_doctor_config_typo_fails(tmp_path, monkeypatch):
    from cairn.cli.commands.doctor import check_operator_config

    monkeypatch.setenv("OBS_PROFILE", str(_write_profile(
        tmp_path, '[profile]\n[profile.config."test/cfg_op".thresholds]\nlimitt = 5\n',  # typo
    )))
    r = {x.name: x for x in check_operator_config()}["config:test/cfg_op"]
    assert r.status == "fail"


def test_doctor_config_unknown_operator_fails(tmp_path, monkeypatch):
    from cairn.cli.commands.doctor import check_operator_config

    monkeypatch.setenv("OBS_PROFILE", str(_write_profile(
        tmp_path, '[profile]\n[profile.config."no/such_op"]\nx = 1\n',
    )))
    r = {x.name: x for x in check_operator_config()}["config:no/such_op"]
    assert r.status == "fail"
    assert "isn't installed" in r.detail


def test_doctor_config_no_config_schema_fails(tmp_path, monkeypatch):
    from cairn.cli.commands.doctor import check_operator_config

    monkeypatch.setenv("OBS_PROFILE", str(_write_profile(
        tmp_path, '[profile]\n[profile.config."test/nocfg_op"]\nthresholds = {limit = 5}\n',
    )))
    r = {x.name: x for x in check_operator_config()}["config:test/nocfg_op"]
    assert r.status == "fail"
    assert "no config_schema" in r.detail


# ─── 5. run-submit preflight (#744, fail-closed) ───────────────────────────


def test_preflight_passes_valid_config():
    preflight_operator_configs({"test/cfg_op": {"thresholds": {"limit": 5}}})  # no raise


def test_preflight_empty_is_noop():
    preflight_operator_configs({})
    preflight_operator_configs(None)


def test_preflight_raises_on_typo():
    with pytest.raises(OperatorConfigError) as ei:
        preflight_operator_configs({"test/cfg_op": {"thresholds": {"limitt": 5}}})
    assert "test/cfg_op" in str(ei.value)
    assert ei.value.errors  # per-operator error list carried for the caller


def test_preflight_raises_on_unknown_operator():
    with pytest.raises(OperatorConfigError):
        preflight_operator_configs({"no/such_op": {"x": 1}})


def test_preflight_raises_on_operator_without_config_schema():
    with pytest.raises(OperatorConfigError):
        preflight_operator_configs({"test/nocfg_op": {"thresholds": {"limit": 5}}})


def test_validator_and_doctor_agree_on_bad_config(tmp_path, monkeypatch):
    # drift guard: the SAME bad config fails both the shared validator (the
    # dispatcher preflight) and the doctor check — enforcement + advisory agree.
    from cairn.cli.commands.doctor import check_operator_config

    bad = {"test/cfg_op": {"thresholds": {"limitt": 5}}}
    assert validate_operator_configs(bad)  # non-empty → the preflight would raise
    p = tmp_path / "p.toml"
    p.write_text(
        '[profile]\n[profile.config."test/cfg_op".thresholds]\nlimitt = 5\n', encoding="utf-8",
    )
    monkeypatch.setenv("OBS_PROFILE", str(p))
    r = {x.name: x for x in check_operator_config()}["config:test/cfg_op"]
    assert r.status == "fail"
