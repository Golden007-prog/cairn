"""Shared per-node event payload builder (#172 / #415).

The in-process node sink and the Temporal run_node activity both use these, so
they must produce identical, size-capped ``node`` payloads regardless of runtime.
"""
from __future__ import annotations

from cairn.core.runtime.node_events import bound_node_output, node_event_data


def test_bound_node_output_drops_empty():
    assert bound_node_output(None) is None
    assert bound_node_output({}) is None
    assert bound_node_output([]) is None
    assert bound_node_output("") is None


def test_bound_node_output_passes_small_dict():
    assert bound_node_output({"verdict": "pass", "n": 3}) == {"verdict": "pass", "n": 3}


def test_bound_node_output_caps_size():
    big = bound_node_output({"blob": "x" * 5000})
    # A 5k string is trimmed to the per-string cap with a marker.
    assert "chars)" in big["blob"]
    many = bound_node_output({"items": list(range(100))})
    assert any(isinstance(x, str) and "more)" in x for x in many["items"])


def test_node_event_data_shape():
    d = node_event_data("synthesize", {"findings": "root cause"})
    assert d["node"] == "synthesize"
    assert d["state"] == "done"
    assert d["output"] == {"findings": "root cause"}


def test_node_event_data_omits_empty_output():
    d = node_event_data("dispatch", {})
    assert d == {"node": "dispatch", "state": "done"}
    assert "output" not in d
