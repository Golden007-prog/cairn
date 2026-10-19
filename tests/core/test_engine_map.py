"""Tests for the `map` BT primitive — dynamic fan-out / fan-in (map-reduce).

These exercise the primitive end-to-end *offline*: the per-item body and the
aggregate are both deterministic `python_node` functions, so no LLM is called.
That makes the fan-out (one branch per runtime list item), the per-item
isolation of results, and the fan-in (aggregate) all verifiable in CI.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine.compiler import compile_template
from cairn.core.engine.python_function_registry import python_function


# ─── Deterministic helpers registered for the map tests ──────────────


@python_function("_map_seed")
def _map_seed(state, *, items):
    """Seed a list into findings (a persisted channel) for `over` to find."""
    return {"findings": {"services": list(items)}}


@python_function("_map_per_item")
def _map_per_item(state):
    """Per-item body: reads the current item bound under `svc`, transforms it.

    The map body stores this whole dict as the item's `output`.
    """
    svc = state.get("svc")
    return {"upper": svc.upper(), "n": len(svc)}


@python_function("_map_agg")
def _map_agg(state):
    """Fan-in: reduce the per-item results (scoped to this map) to a summary."""
    results = state.get("map_results", [])
    uppers = sorted(r["output"]["upper"] for r in results)
    return {"findings": {"map_summary": {"count": len(results), "uppers": uppers}}}


def _write_template(text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        return f.name


_EXEC_TEMPLATE = """---
name: map-exec-test
version: 0.0.1
flow:
  type: state_machine
  root:
    type: sequence
    children:
      - type: python_node
        function: _map_seed
        config:
          items: [svc-a, svc-b, svc-c]
      - type: map
        over: services
        as: svc
        aggregate: _map_agg
        body:
          type: python_node
          function: _map_per_item
  nodes: {}
tools:
  required: []
---

# Map exec test
"""


# ─── Compile-time ────────────────────────────────────────────────────


def test_map_compiles():
    ct = compile_template(_write_template(_EXEC_TEMPLATE))
    assert ct.flow_type == "state_machine"


def test_map_bad_body_raises():
    bad = """---
name: map-bad-body
version: 0.0.1
flow:
  type: state_machine
  root:
    type: map
    over: services
    body:
      type: sequence
      children:
        - { type: python_node, function: _map_per_item }
  nodes: {}
tools:
  required: []
---

# Bad map body
"""
    with pytest.raises(ValueError, match="map body must be"):
        compile_template(_write_template(bad))


# ─── Execution (fan-out → per-item → fan-in) ─────────────────────────


async def test_map_fan_out_and_aggregate():
    ct = compile_template(_write_template(_EXEC_TEMPLATE))
    result = await ct.run({"symptom": "test", "user_message": "go"})

    # Fan-out: one result entry per list item.
    map_results = result["map_results"]
    assert len(map_results) == 3
    assert {r["item"] for r in map_results} == {"svc-a", "svc-b", "svc-c"}
    # Each branch ran the body in isolation (no key collision).
    assert all(r["output"]["upper"] == r["item"].upper() for r in map_results)

    # Fan-in: the aggregate reduced them into one finding.
    summary = result["findings"]["map_summary"]
    assert summary["count"] == 3
    assert summary["uppers"] == ["SVC-A", "SVC-B", "SVC-C"]


async def test_map_empty_list_runs_aggregate_over_zero():
    """An empty `over` list must skip the body and still run the aggregate."""
    template = _EXEC_TEMPLATE.replace(
        "items: [svc-a, svc-b, svc-c]", "items: []"
    )
    ct = compile_template(_write_template(template))
    result = await ct.run({"symptom": "test", "user_message": "go"})

    assert result["map_results"] == []
    assert result["findings"]["map_summary"] == {"count": 0, "uppers": []}


async def test_map_default_collect_without_aggregate():
    """Without an `aggregate`, results are collected under the body label."""
    template = """---
name: map-no-agg
version: 0.0.1
flow:
  type: state_machine
  root:
    type: sequence
    children:
      - type: python_node
        function: _map_seed
        config:
          items: [a, bb, ccc]
      - type: map
        over: services
        as: svc
        body:
          type: python_node
          function: _map_per_item
  nodes: {}
tools:
  required: []
---

# Map default collect
"""
    ct = compile_template(_write_template(template))
    result = await ct.run({"symptom": "t", "user_message": "go"})

    # Default reduction collects each item's output under the body fn label.
    collected = result["findings"]["_map_per_item"]
    assert len(collected) == 3
    ns = sorted(o["n"] for o in collected)
    assert ns == [1, 2, 3]


async def test_map_over_dotted_findings_path():
    """`over` accepts a dotted path into a persisted channel (findings.X)."""
    template = _EXEC_TEMPLATE.replace("over: services", "over: findings.services")
    ct = compile_template(_write_template(template))
    result = await ct.run({"symptom": "t", "user_message": "go"})
    assert len(result["map_results"]) == 3
