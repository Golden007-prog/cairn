"""Rule-based failure clustering (#486, ADR-0060 §D4)."""

from cairn.core.eval.cluster import cluster_failures


def _item(rid, ft, ev, created="2026-07-05T12:00:00Z"):
    return {"run_id": rid, "created_at": created, "failure_type": ft, "evidence": ev}


def test_same_root_cause_groups_into_one_cluster():
    # 8 timeouts on the same operator → ONE cluster.
    items = [
        _item(f"r{i}", "performance_failure",
              {"action_type": "search", "reason": "operation timed out"})
        for i in range(8)
    ]
    clusters = cluster_failures(items)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["size"] == 8
    assert c["signature"] == "timeout"
    assert c["locus"] == "search"
    assert set(c["member_run_ids"]) == {f"r{i}" for i in range(8)}


def test_different_root_causes_split():
    # 5 timeouts + 3 rate-limits on the same operator → TWO clusters.
    items = [_item(f"t{i}", "system_failure",
                   {"action_type": "search", "reason": "timed out"}) for i in range(5)]
    items += [_item(f"rl{i}", "system_failure",
                    {"action_type": "search", "reason": "rate limit 429"}) for i in range(3)]
    clusters = cluster_failures(items)
    assert len(clusters) == 2
    assert [c["size"] for c in clusters] == [5, 3]  # largest-first
    assert {c["signature"] for c in clusters} == {"timeout", "rate_limit"}


def test_different_failure_types_never_merge():
    items = [
        _item("a", "tool_failure", {"reason": "boom"}),
        _item("b", "safety_failure", {"reason": "boom"}),
    ]
    assert len(cluster_failures(items)) == 2


def test_dedupes_run_ids_and_tracks_first_last_seen():
    items = [
        _item("r1", "tool_failure", {"action_type": "x", "reason": "boom"}, "2026-07-05T10:00:00Z"),
        _item("r1", "tool_failure", {"action_type": "x", "reason": "boom"}, "2026-07-05T11:00:00Z"),
        _item("r2", "tool_failure", {"action_type": "x", "reason": "boom"}, "2026-07-05T12:00:00Z"),
    ]
    clusters = cluster_failures(items)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["size"] == 2  # r1 counted once
    assert c["representative_run_id"] == "r1"
    assert c["first_seen"] == "2026-07-05T10:00:00Z"
    assert c["last_seen"] == "2026-07-05T12:00:00Z"


def test_empty_input():
    assert cluster_failures([]) == []
