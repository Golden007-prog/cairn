"""#788 (ADR-0071) — the dispatcher resolves the DEFAULT preset for every run.

The persisted default preset must apply to manual, webhook, schedule and event
runs alike (triggers have no run-form), layered over the legacy per-workflow
config (preset > workflow-config), with the applied preset stamped on run
provenance. These pin the resolver the dispatcher calls at its single config
injection point.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

import cairn.core.workflow_config_store as wcs
from cairn.core.dispatch.dispatcher import RunDispatcher


@pytest.fixture
def store(monkeypatch):
    s = wcs.InMemoryWorkflowConfigStore()
    # point the process singleton (used by _load_workflow_config + default_preset) at ours
    monkeypatch.setattr(wcs, "_default_store", s)
    return s


async def test_default_preset_layers_over_legacy_config(store):
    ref, ws = "demo/wf", "default"
    await store.put(wcs.WorkflowConfigRecord(template_ref=ref, workspace_id=ws, values={"opA": {"x": 1, "y": 2}}))
    await store.put_preset(wcs.WorkflowPreset(
        name="cheap", template_ref=ref, workspace_id=ws,
        values={"opA": {"x": 9}, "opB": {"z": 3}}, is_default=True,
    ))

    values, prov = await RunDispatcher._resolve_workflow_config(ws, ref)

    # provenance stamps the applied preset + its source (ADR-0047; #790 source)
    assert prov == {"name": "cheap", "version": 1, "source": "default"}
    # preset wins on x; legacy y survives; preset-only opB is added (preset > workflow)
    assert values["opA"]["x"] == 9
    assert values["opA"]["y"] == 2
    assert values["opB"]["z"] == 3


async def test_no_default_preset_falls_back_to_legacy(store):
    ref, ws = "demo/wf", "default"
    await store.put(wcs.WorkflowConfigRecord(template_ref=ref, workspace_id=ws, values={"opA": {"x": 1}}))
    # a preset exists but is NOT default → not applied
    await store.put_preset(wcs.WorkflowPreset(name="other", template_ref=ref, workspace_id=ws, values={"opA": {"x": 9}}))

    values, prov = await RunDispatcher._resolve_workflow_config(ws, ref)
    assert prov == {}
    assert values == {"opA": {"x": 1}}


async def test_no_config_at_all(store):
    values, prov = await RunDispatcher._resolve_workflow_config("default", "demo/none")
    assert values == {}
    assert prov == {}


async def test_default_preset_with_no_legacy_config(store):
    """A triggered run with no saved per-workflow config still picks up the
    default preset (the trigger-no-run-form case #788 exists for)."""
    ref, ws = "demo/wf", "default"
    await store.put_preset(wcs.WorkflowPreset(
        name="prod", template_ref=ref, workspace_id=ws, values={"opA": {"x": 5}}, is_default=True,
    ))
    values, prov = await RunDispatcher._resolve_workflow_config(ws, ref)
    assert prov == {"name": "prod", "version": 1, "source": "default"}
    assert values == {"opA": {"x": 5}}


async def test_run_preset_override_wins_over_default(store):
    """#790 — a per-run preset override beats the marked default for THIS run,
    and provenance records source='run'."""
    ref, ws = "demo/wf", "default"
    await store.put_preset(wcs.WorkflowPreset(name="cheap", template_ref=ref, workspace_id=ws, values={"opA": {"x": 1}}, is_default=True))
    await store.put_preset(wcs.WorkflowPreset(name="thorough", template_ref=ref, workspace_id=ws, values={"opA": {"x": 9}}))

    # no override → default preset applies
    _v, prov = await RunDispatcher._resolve_workflow_config(ws, ref)
    assert prov == {"name": "cheap", "version": 1, "source": "default"}

    # explicit override → that preset applies, source='run'
    values, prov = await RunDispatcher._resolve_workflow_config(ws, ref, "thorough")
    assert prov == {"name": "thorough", "version": 1, "source": "run"}
    assert values == {"opA": {"x": 9}}

    # override naming a missing preset → falls back to the default (never fails)
    _v, prov = await RunDispatcher._resolve_workflow_config(ws, ref, "ghost")
    assert prov["name"] == "cheap" and prov["source"] == "default"
