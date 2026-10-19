"""#569 / #581 — flatten-emitter primitives.

These pure-logic pieces underpin inlining a composed child template's nodes as
top-level parent nodes (so a child gate's interrupt reaches the Temporal signal
loop instead of looping inside one activity). Pinned here before the graph-splice
that assembles them.
"""
from __future__ import annotations

from cairn.core.engine.flatten import (
    flatten_entry_update,
    flatten_exit_update,
    namespace_child_bt,
)
from cairn.core.engine.schema import ActionBTNode, GuardBTNode, OperationBTNode, SequenceBTNode


# ── namespace_child_bt: rename action refs, leave operation refs alone ────
def test_namespace_prefixes_action_refs_only():
    child = SequenceBTNode(
        type="sequence",
        children=[
            OperationBTNode(type="operation", ref="run-gate"),
            ActionBTNode(type="action", ref="synthesize"),
        ],
    )
    nodes_map = {"synthesize": {"type": "prompt", "model": "gpt-4o-mini"}}
    new_root, renamed = namespace_child_bt(child, nodes_map, prefix="flat_dsr_1")

    op, act = new_root.children
    assert op.ref == "run-gate"                       # operation ref untouched
    assert act.ref == "flat_dsr_1__synthesize"        # action ref prefixed
    assert renamed == {"flat_dsr_1__synthesize": {"type": "prompt", "model": "gpt-4o-mini"}}


def test_namespace_does_not_mutate_original():
    child = ActionBTNode(type="action", ref="synthesize")
    new_root, _ = namespace_child_bt(child, {"synthesize": {}}, prefix="p")
    assert child.ref == "synthesize"                  # original untouched (deep-copied)
    assert new_root.ref == "p__synthesize"


def test_namespace_recurses_into_guard_branches():
    child = GuardBTNode(
        type="guard", condition="notion_page_id_provided",
        then=ActionBTNode(type="action", ref="publish"),
    )
    new_root, renamed = namespace_child_bt(child, {"publish": {"x": 1}}, prefix="pfx")
    assert new_root.then.ref == "pfx__publish"
    assert "pfx__publish" in renamed


# ── flatten_entry_update: scope child inputs into template_input ──────────
def test_entry_merges_child_inputs_into_template_input():
    state = {
        "dataset_path": "s3://bucket/raw.jsonl",
        "run_id": "r-1",
        "template_input": {"base_model": "Qwen", "dataset_path": "s3://bucket/raw.jsonl"},
    }
    upd = flatten_entry_update(state, {"path": "dataset_path"})
    # the child reads $input.path -> template_input["path"]
    assert upd["template_input"]["path"] == "s3://bucket/raw.jsonl"
    # parent's own template_input keys are preserved (merge, not replace)
    assert upd["template_input"]["base_model"] == "Qwen"
    assert upd["run_id"] == "r-1"


def test_entry_no_map_preserves_template_input():
    state = {"template_input": {"a": 1}, "run_id": "r"}
    upd = flatten_entry_update(state, None)
    assert upd["template_input"] == {"a": 1}


# ── flatten_exit_update: findings-aware child->parent output mapping ──────
def test_exit_maps_findings_field_up():
    state = {"findings": {"verdict": "warn", "dataset_uri": "s3://clean"}}
    upd = flatten_exit_update(state, {"verdict": "dataset_verdict", "dataset_uri": "dataset_uri"})
    assert upd["dataset_verdict"] == "warn"           # resolved via findings.verdict (#585)
    assert upd["dataset_uri"] == "s3://clean"
    assert upd["last_action_status"] == "success"


def test_exit_prefers_top_level_over_findings():
    state = {"verdict": "TOP", "findings": {"verdict": "nested"}}
    upd = flatten_exit_update(state, {"verdict": "dataset_verdict"})
    assert upd["dataset_verdict"] == "TOP"


def test_exit_mirrors_mapped_output_into_findings():
    # #600: every mapped output is ALSO written under findings.<parent_key> so a
    # downstream consumer can read it from the reliable findings merge channel
    # (the top-level write from this PURE exit node doesn't survive
    # flatten+Temporal). findings is Annotated[dict, _merge_dicts] → additive.
    state = {"findings": {"verdict": "fail", "dataset_uri": "s3://clean"}}
    upd = flatten_exit_update(state, {"verdict": "dataset_verdict", "dataset_uri": "dataset_uri"})
    assert upd["dataset_verdict"] == "fail"                    # top-level (back-compat)
    assert upd["findings"]["dataset_verdict"] == "fail"        # mirror (#600)
    assert upd["findings"]["dataset_uri"] == "s3://clean"


def test_exit_absent_field_is_not_mirrored():
    # A mapped field the child never produced is skipped entirely — no top-level
    # key, no findings mirror (so `$default(...)` in the consumer kicks in).
    upd = flatten_exit_update({"findings": {}}, {"verdict": "dataset_verdict"})
    assert "dataset_verdict" not in upd
    assert "findings" not in upd


def test_exit_no_map_is_just_success():
    assert flatten_exit_update({}, None) == {"last_action_status": "success"}
