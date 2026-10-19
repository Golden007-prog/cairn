"""Failure clustering (#486, ADR-0060 §D4) — rule-based, deterministic.

Turns "8 performance_failures" into "these 8 share root cause X". It groups
failure LABELS (mechanical + quality), not quality scores, so it needs no
trustworthy metric — UNGATED, ships before #458. The embedding-similarity
upgrade over free-text ``reason`` variance is the gated #478 follow-up; this is
the deterministic key + fallback.

    cluster_key = (failure_type, node/operator locus, evidence signature)

The signature is what separates "8 timeouts on one operator" (one cluster) from
"5 timeouts + 3 rate-limits" (two): it buckets the error class rather than
matching the raw free-text reason.
"""

from __future__ import annotations

import re
from typing import Any

#: Error-class buckets, matched in order against the evidence text. The first
#: hit wins, so a "connection reset after timeout" reads as a timeout. Keyed on
#: the same keyword families the taxonomy uses (taxonomy.py `_*_KEYWORDS`).
_KEYWORD_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("timeout", ("timeout", "timed out", "wall_seconds")),
    ("rate_limit", ("rate limit", "rate_limit", "429", "too many requests")),
    ("provider_outage", ("503", "502", "504", "service unavailable", "bad gateway",
                         "connection reset", "connection error", "upstream")),
    ("budget", ("over budget", "budget exceeded", "cost budget", "max_cost")),
    ("pii", ("pii", "redact", "credential", "secret")),
    ("schema", ("json_parse", "schema", "not valid json", "instruction")),
]


def _evidence_text(evidence: dict) -> str:
    return " ".join(
        str(v) for v in evidence.values() if isinstance(v, (str, int, float))
    ).lower()


def _signature(evidence: dict) -> str:
    """Normalized digest of the discriminating evidence field: the error-class
    bucket when present (so different root causes don't merge), else the
    normalized ``reason``/``status``/``verdict`` string."""
    text = _evidence_text(evidence)
    for bucket, kws in _KEYWORD_BUCKETS:
        if any(k in text for k in kws):
            return bucket
    raw = evidence.get("reason") or evidence.get("status") or evidence.get("verdict") or ""
    norm = re.sub(r"\s+", " ", str(raw).strip().lower())
    return norm[:80] or "unclassified"


def _locus(evidence: dict) -> str:
    """The responsible node/operator, from fields the taxonomy already
    populates (``action_type``/``candidate_id`` for tool, ``recipe`` for a
    quality failure, ``event_type`` otherwise). Empty when not derivable — such
    labels still cluster by ``(failure_type, signature)``."""
    for k in ("action_type", "operator", "candidate_id", "recipe", "event_type"):
        v = evidence.get(k)
        if v:
            return str(v)
    return ""


def cluster_failures(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group failure-label instances into root-cause clusters.

    ``items``: ``[{run_id, created_at, failure_type, evidence}]`` — one entry per
    label instance (a run with N labels contributes N entries). Returns clusters
    sorted largest-first (``size`` = distinct-run count):
    ``{failure_type, locus, signature, size, member_run_ids,
    representative_run_id, shared_evidence, first_seen, last_seen}``.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for it in items:
        ft = it.get("failure_type", "")
        ev = it.get("evidence") or {}
        key = (ft, _locus(ev), _signature(ev))
        g = groups.setdefault(key, {
            "failure_type": ft, "locus": key[1], "signature": key[2],
            "run_ids": [], "created": [], "shared_evidence": ev,
        })
        rid = it.get("run_id", "")
        if rid and rid not in g["run_ids"]:
            g["run_ids"].append(rid)
        ca = it.get("created_at")
        if ca:
            g["created"].append(ca)

    clusters: list[dict[str, Any]] = []
    for g in groups.values():
        created = sorted(g["created"])
        clusters.append({
            "failure_type": g["failure_type"],
            "locus": g["locus"],
            "signature": g["signature"],
            "size": len(g["run_ids"]),
            "member_run_ids": g["run_ids"],
            "representative_run_id": g["run_ids"][0] if g["run_ids"] else None,
            "shared_evidence": g["shared_evidence"],
            "first_seen": created[0] if created else None,
            "last_seen": created[-1] if created else None,
        })
    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


__all__ = ["cluster_failures"]
