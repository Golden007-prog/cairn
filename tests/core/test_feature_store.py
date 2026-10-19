"""#1878 — the flag store, resolution order, bucketing and presets."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.features import (
    FEATURE_IDS, GLOBAL, LIGHT_OFF, InMemoryFeatureStore, SqliteFeatureStore, apply_preset, bucket,
    resolve_for, validate_value, workspace_scope,
)


def test_resolution_order_workspace_over_global_over_env():
    env = {"OBS_LIGHT": "on"}                       # env: builder off (the light preset)
    assert resolve_for({}, env=env)["builder"] is False
    rules = {GLOBAL: {"builder": "on"}}
    assert resolve_for(rules, env=env)["builder"] is True                     # global wins over env
    rules[workspace_scope("acme")] = {"builder": "off"}
    assert resolve_for(rules, workspace_id="acme", env=env)["builder"] is False   # workspace wins over global
    assert resolve_for(rules, workspace_id="other", env=env)["builder"] is True   # other workspaces untouched


def test_pipelines_is_accepted_and_ignored():
    """ADR-0105 D5 (#2113): every start is a one-leg pipeline, so the pipeline
    is the substrate a profile runs ON — no env, preset or stored rule can
    switch it off. The key stays valid so old env files and rules still load."""
    from cairn.core.features import ALWAYS_ON, resolve_features
    assert "pipelines" in ALWAYS_ON and "pipelines" in FEATURE_IDS and "pipelines" not in LIGHT_OFF
    assert resolve_features({"OBS_LIGHT": "on"})["pipelines"] is True
    assert resolve_features({"OBS_FEATURE_PIPELINES": "off"})["pipelines"] is True
    rules = {GLOBAL: {"pipelines": "off"}, workspace_scope("acme"): {"pipelines": "off"}}
    assert resolve_for(rules, workspace_id="acme", env={})["pipelines"] is True


def test_bucket_is_stable_and_roughly_uniform():
    assert bucket("maya") == bucket("maya")
    assert 0 <= bucket("maya") < 100
    hits = sum(1 for i in range(2000) if bucket(f"user-{i}") < 50)
    assert 900 < hits < 1100, hits                    # ~50% within a wide band


def test_rollout_enables_the_share_below_the_percentage():
    rules = {GLOBAL: {"builder": "50%"}}
    env = {"OBS_LIGHT": "on"}
    on = [u for u in (f"u{i}" for i in range(200)) if resolve_for(rules, user_id=u, env=env)["builder"]]
    assert all(bucket(u) < 50 for u in on)
    assert 60 < len(on) < 140


def test_value_validation():
    assert validate_value(" ON ") == "on" and validate_value("35%") == "35%" and validate_value("100%") == "100%"
    for bad in ("maybe", "101%", "-1%", "50"):
        with pytest.raises(ValueError):
            validate_value(bad)


@pytest.mark.parametrize("make", [lambda p: InMemoryFeatureStore(), lambda p: SqliteFeatureStore(path=str(p / "f.db"))], ids=["memory", "sqlite"])
async def test_store_roundtrip_and_history(tmp_path, make):
    st = make(tmp_path)
    try:
        await st.set(GLOBAL, "pipelines", "off", by="admin")
        await st.set(workspace_scope("acme"), "pipelines", "on", by="admin")
        await st.set(GLOBAL, "builder", "25%", by="admin")
        rules = await st.rules()
        assert rules[GLOBAL] == {"pipelines": "off", "builder": "25%"}
        assert rules[workspace_scope("acme")] == {"pipelines": "on"}
        await st.set(workspace_scope("acme"), "pipelines", None, by="admin")   # clear the override
        assert workspace_scope("acme") not in (await st.rules()) or "pipelines" not in (await st.rules())[workspace_scope("acme")]
        hist = await st.history()
        assert [(h.flag, h.old, h.new) for h in hist][:2] == [("pipelines", "on", None), ("builder", None, "25%")]
        assert all(h.by == "admin" for h in hist)
        with pytest.raises(ValueError):
            await st.set(GLOBAL, "nope", "on")
        with pytest.raises(ValueError):
            await st.set(GLOBAL, "judges", "sometimes")
    finally:
        st.close()


async def test_presets_write_global_rules():
    st = InMemoryFeatureStore()
    await apply_preset("light", store=st, by="admin")
    rules = (await st.rules())[GLOBAL]
    assert {k for k, v in rules.items() if v == "off"} == set(LIGHT_OFF)
    assert set(rules) == set(FEATURE_IDS)
    await apply_preset("everything", store=st, by="admin")
    assert all(v == "on" for v in (await st.rules())[GLOBAL].values())
    with pytest.raises(ValueError):
        await apply_preset("nope", store=st)
