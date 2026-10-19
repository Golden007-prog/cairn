"""#793 (ADR-0071) — user-savable INPUT presets (the generalization of demos).

Kept in their own table so the config-preset surface (#787) is untouched. These
pin the store round-trip across backends AND that config presets and input
presets don't leak into each other's lists.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.workflow_config_store import (
    InMemoryWorkflowConfigStore,
    InputPreset,
    SqliteWorkflowConfigStore,
    WorkflowPreset,
)


def _stores():
    return [InMemoryWorkflowConfigStore(), SqliteWorkflowConfigStore(":memory:")]


@pytest.mark.parametrize("store", _stores(), ids=["memory", "sqlite"])
async def test_input_preset_round_trip(store):
    ref, ws = "demo/wf", "default"
    await store.put_input_preset(InputPreset(name="small", template_ref=ref, inputs={"path": "a.jsonl", "epochs": 1}))
    await store.put_input_preset(InputPreset(name="big", template_ref=ref, inputs={"path": "b.jsonl", "epochs": 5}))

    got = await store.list_input_presets(ws, ref)
    assert [p.name for p in got] == ["big", "small"]  # sorted
    assert next(p for p in got if p.name == "small").inputs == {"path": "a.jsonl", "epochs": 1}

    assert await store.delete_input_preset(ws, ref, "small") is True
    assert [p.name for p in await store.list_input_presets(ws, ref)] == ["big"]
    store.close()


@pytest.mark.parametrize("store", _stores(), ids=["memory", "sqlite"])
async def test_input_and_config_presets_do_not_leak(store):
    """A config preset and an input preset with the same (ws, ref, name) live in
    separate tables and never appear in each other's list."""
    ref, ws = "demo/wf", "default"
    await store.put_preset(WorkflowPreset(name="shared", template_ref=ref, values={"op": {"k": 1}}))
    await store.put_input_preset(InputPreset(name="shared", template_ref=ref, inputs={"path": "x"}))

    cfg = await store.list_presets(ws, ref)
    inp = await store.list_input_presets(ws, ref)
    assert [p.name for p in cfg] == ["shared"] and hasattr(cfg[0], "values")
    assert [p.name for p in inp] == ["shared"] and inp[0].inputs == {"path": "x"}

    # deleting the input preset leaves the config preset intact
    await store.delete_input_preset(ws, ref, "shared")
    assert [p.name for p in await store.list_presets(ws, ref)] == ["shared"]
    assert await store.list_input_presets(ws, ref) == []
    store.close()
