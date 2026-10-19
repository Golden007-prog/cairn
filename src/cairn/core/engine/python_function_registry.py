"""
Python function registry for PythonBTNode escape hatch.

Functions are async callables that take the current state dict and arbitrary
config kwargs, returning a partial state update dict that LangGraph merges
via the state schema's reducers.

Signature: async def fn(state: dict, **config) -> dict

Register via @python_function decorator. Built-in functions are at the
bottom of this module.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

PythonNodeFn = Callable[..., Awaitable[dict]]

_REGISTRY: dict[str, PythonNodeFn] = {}


def python_function(name: str | None = None):
    """Decorator that registers a function as a named python_node target.

    The function may be sync or async; sync functions are wrapped automatically.
    """
    def wrap(fn) -> PythonNodeFn:
        key = name or fn.__name__
        if inspect.iscoroutinefunction(fn):
            _REGISTRY[key] = fn
        else:
            async def async_wrap(*args, **kwargs):
                return fn(*args, **kwargs)
            async_wrap.__name__ = fn.__name__
            _REGISTRY[key] = async_wrap
        return fn
    return wrap


def resolve_python_function(name: str) -> PythonNodeFn:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY.keys()))
        raise KeyError(f"Unknown python function '{name}'. Known: {known}")
    return _REGISTRY[name]


def known_functions() -> list[str]:
    return sorted(_REGISTRY.keys())


def register(name: str, fn: PythonNodeFn) -> None:
    """Programmatic registration (used by skill custom_logic loader)."""
    _REGISTRY[name] = fn


# ─── Built-in python_node functions ─────────────────────────────────


@python_function("temporal_correlation")
def temporal_correlation(state: dict, *, window_minutes: int = 30) -> dict:
    """Deterministic correlation: count distinct evidence types in state.
    Used to validate that python_node receives state and writes a state update.
    """
    evidence = state.get("evidence") or []
    counts: dict[str, int] = {}
    for item in evidence:
        t = item.get("type", "unknown")
        counts[t] = counts.get(t, 0) + 1

    return {
        "findings": {
            "temporal_correlation": {
                "window_minutes": window_minutes,
                "evidence_counts_by_type": counts,
                "total_evidence_items": len(evidence),
            }
        }
    }


@python_function("parse_synthesize_output")
def parse_synthesize_output(state: dict, *, finding_key: str = "synthesize") -> dict:
    """Parse the synthesize prompt's JSON output into typed state fields.

    The synthesize prompt is instructed to emit an RCA JSON shape — but
    the compiler stores that response as a single string at
    ``findings[finding_key]``. This helper parses it and hoists the
    top-level fields (symptom, root_cause, evidence, blast_radius,
    suggested_actions, timeline) back into state so the parent
    ``subgraph_node`` mapping (Phase 2b — field-by-field) can pluck
    them. Tolerant: if the body isn't valid JSON or the finding is
    missing, returns an empty patch so the flow proceeds with
    defaults.
    """
    import json as _json

    findings = state.get("findings") or {}
    raw = findings.get(finding_key, "")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    # Strip a leading ```json fence if the LLM included one.
    text = raw.strip()
    if text.startswith("```"):
        # remove first and (optionally) last fence
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        parsed = _json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    patch: dict = {}
    for key in (
        "symptom", "root_cause", "evidence", "blast_radius",
        "suggested_actions", "timeline",
    ):
        if key in parsed:
            patch[key] = parsed[key]
    return patch


@python_function("assemble_serve_report")
def assemble_serve_report(state: dict) -> dict:
    """Hoist the serve/bench findings to TOP-LEVEL state as the ServeReport (#703).
    Deterministic (no LLM, no API key) — the operators decided the facts (endpoint,
    health, throughput), so this narrates them + dodges the #706 top-level gap.
    Reads the ``findings`` merge channel the operations wrote to."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}

    def _g(key, default=None):
        return f[key] if f.get(key) is not None else default

    endpoint = str(_g("serve_endpoint_url", "") or "")
    status = str(_g("serve_status", "failed") or "failed")
    model = str(_g("serve_model_name", "") or "")
    bench_ok = bool(_g("bench_ok", False))
    tps = float(_g("bench_tokens_per_sec", 0.0) or 0.0)
    p50 = float(_g("bench_latency_p50", 0.0) or 0.0)
    p95 = float(_g("bench_latency_p95", 0.0) or 0.0)

    if status == "ready":
        bench_desc = (
            f" — bench {'OK' if bench_ok else 'FAIL'} ({tps:.1f} tok/s, p50 {p50:.0f}ms)"
        )
        summary = f"Serve READY @ {endpoint} (model={model}){bench_desc}"
    else:
        summary = f"Serve FAILED (model={model or 'n/a'})"

    return {
        "endpoint_url": endpoint,
        "serve_status": status,
        "served_model_name": model,
        "bench_ok": bench_ok,
        "tokens_per_sec": round(tps, 2),
        "latency_ms_p50": round(p50, 2),
        "latency_ms_p95": round(p95, 2),
        "summary": summary,
    }


@python_function("assemble_dataset_prep_report")
def assemble_dataset_prep_report(state: dict) -> dict:
    """Hoist the dataset-prep findings to TOP-LEVEL state as the DatasetPrepReport
    (#711). Deterministic — the prep operators decided the facts (redaction counts,
    split paths, leakage), so this narrates them without an LLM (no API key needed,
    fully local-runnable) and dodges the top-level-vs-findings report gap (#706).
    Reads the ``findings`` merge channel the operations wrote to."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}

    def _g(key, default=None):
        return f[key] if f.get(key) is not None else default

    anon_path = _g("prep_anon_path", "")
    redacted = int(_g("prep_redacted", 0) or 0)
    total = int(_g("prep_total", 0) or 0)
    pii_backend = str(_g("prep_anon_backend", "") or "")
    split_paths = _g("prep_split_paths", {}) or {}
    no_leakage = bool(_g("prep_no_leakage", False))
    split_counts = _g("prep_split_counts", {}) or {}
    prov_ok = bool(_g("prov_ok", False))
    # #870 — the POST-SCRUB invariant: re-scan of the SCRUBBED output (run-prep-verify).
    # 0 residual PII rows on a correct scrub; > 0 means anonymize missed something and
    # the bytes about to train still carry PII.
    post_scrub_pii_rows = int(_g("post_scrub_pii_rows", 0) or 0)
    post_scrub_pii_clean = post_scrub_pii_rows == 0
    # #882 — the config-gated post-scrub PII verdict (dataset/prep_gate). `pass`
    # under mode=off or a clean scrub; `warn`/`fail` when enforced. Defaults to `pass`
    # for older runs that predate the gate node.
    prep_gate_verdict = str(_g("prep_gate_verdict", "pass") or "pass")
    # #889/#890 — provenance: `clean` is only a GUARANTEE (`verified`) when the re-scan
    # was FULL (not sampled) AND on a certifying NER backend. Surface the backend +
    # sampled flag so the report never presents an estimate as a guarantee.
    post_scrub_pii_verified = bool(_g("prep_gate_verified", False))
    post_scrub_pii_backend = str(_g("post_scrub_pii_backend", "") or "")
    post_scrub_pii_sampled = bool(_g("post_scrub_pii_sampled", False))

    # Honesty: the zero-dep `regex` fallback only scrubs secrets+email+IP — warn
    # loudly when a run degraded to it so a "PII-scrubbed" dataset is never a
    # silent half-measure (install the [pii] extra for presidio's human-PII recall).
    warnings: list[str] = []
    if pii_backend == "regex":
        warnings.append(
            "PII scrub ran on the zero-dep 'regex' backend (secrets+email+IP only) — "
            "phone/SSN/person/location NOT redacted. Install the [pii] extra "
            "(presidio) for full coverage."
        )
    # #870 — surface a FAILED invariant loudly: the scrubbed artifact that trains
    # still has PII. The finetune prep front-stage can guard on post_scrub_pii_clean
    # to hold the promote (fail-closed) rather than train on leaked PII.
    if not post_scrub_pii_clean:
        warnings.append(
            f"POST-SCRUB PII INVARIANT FAILED: {post_scrub_pii_rows} row(s) still carry "
            f"PII after anonymize — the scrubbed dataset is NOT clean. Do not train on it; "
            f"widen the redaction backend/kinds and re-run prep."
        )
    elif not post_scrub_pii_verified and post_scrub_pii_backend:
        # #889/#890 — 0 PII FOUND, but the scan can't CERTIFY it. Only when a re-scan
        # actually ran (backend set); say so loudly so a sampled/regex estimate is
        # never mistaken for a guarantee.
        reason = "sampled scan" if post_scrub_pii_sampled else f"'{post_scrub_pii_backend or 'regex'}' backend has no NER"
        warnings.append(
            f"POST-SCRUB PII NOT CERTIFIED: 0 PII found, but this was a {reason} — it "
            f"ESTIMATES clean, it does not PROVE it. A governed run needs a FULL presidio "
            f"scan (`[pii]` extra) to guarantee 0 PII before training."
        )

    split_desc = ", ".join(f"{k}={v}" for k, v in split_counts.items()) or "n/a"
    summary = (
        f"Prepped {total} rows: redacted {redacted} (backend={pii_backend or 'n/a'}), "
        f"split ({split_desc}), leakage-free={no_leakage}, provenance_ok={prov_ok}, "
        f"post-scrub-clean={post_scrub_pii_clean} (certified={post_scrub_pii_verified})."
        + (f" ⚠️ {len(warnings)} warning(s)." if warnings else "")
    )
    return {
        "anonymized_path": anon_path,
        "redacted_rows": redacted,
        "total_rows": total,
        "pii_backend": pii_backend,
        "warnings": warnings,
        "split_paths": split_paths,
        "no_leakage": no_leakage,
        "split_counts": split_counts,
        "provenance_ok": prov_ok,
        "post_scrub_pii_rows": post_scrub_pii_rows,
        "post_scrub_pii_clean": post_scrub_pii_clean,
        "post_scrub_pii_verified": post_scrub_pii_verified,
        "post_scrub_pii_backend": post_scrub_pii_backend,
        "post_scrub_pii_sampled": post_scrub_pii_sampled,
        "prep_gate_verdict": prep_gate_verdict,
        "summary": summary,
    }


@python_function("hoist_clean_stats")
def hoist_clean_stats(state: dict) -> dict:
    """Bridge a CLEAN dataflow (ADR-0081 P1c, #1050) into the assembler's flat
    findings. `dataflow/run` lands its scalar stats dict at
    ``findings.clean_stats`` (keys like ``dedup.exact.dropped_exact``); this
    splays them onto the SAME ``fix_*`` keys :func:`assemble_dataset_fix_report`
    reads — so the clean template reuses the fix report path byte-unchanged,
    exactly like the fix-remote hoist did (PR #1044)."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}
    stats = f.get("clean_stats")
    if not isinstance(stats, dict):
        return {"findings": {}}
    return {
        "findings": {
            "fix_anon_path": str(stats.get("export.jsonl.output")
                                 or stats.get("export.parquet.output") or ""),
            "fix_dedup_total": int(stats.get("ingest.normalize.rows", 0) or 0),
            "fix_total": int(stats.get("pii.redact.rows")
                             or stats.get("dedup.exact.rows", 0) or 0),
            "fix_dedup_dropped": int(stats.get("dedup.exact.dropped", 0) or 0),
            "fix_decontam_dropped": int(stats.get("decontam.apply.dropped", 0) or 0),
            "fix_decontam_checked": bool(stats.get("decontam.apply.checked", False)),
            "fix_redacted": int(stats.get("pii.redact.redacted_rows", 0) or 0),
            "fix_anon_backend": str(stats.get("pii.redact.pii_backend", "") or ""),
        }
    }


@python_function("assemble_artifact_promote_report")
def assemble_artifact_promote_report(state: dict) -> dict:
    """The governed-promotion record (ADR-0081 P2b, #1052): where the tag now
    points and what it displaced — deterministic, no LLM. ``promoted`` stays
    False when the gate rejected (the operator never ran), so the report never
    claims a move that didn't happen."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}
    return {
        "tag": str(f.get("promote_tag", "") or ""),
        "artifact_id": str(f.get("promote_artifact_id", "") or ""),
        "previous_artifact_id": str(f.get("promote_previous", "") or ""),
        "uri": str(f.get("promote_uri", "") or ""),
        "promoted": bool(f.get("promoted", False)),
    }


@python_function("hoist_scan_findings")
def hoist_scan_findings(state: dict) -> dict:
    """Bridge the CHECK dataflow (ADR-0081 P2c, #1053) into the gate's findings.

    ``dataflow/run`` lands the scan.all kernel's scalar stats at
    ``findings.scan_stats`` (keys like ``scan.all.dedup_exact``); the rich
    payload (pii_counts, trainer_compat, per-row findings, profile_lengths)
    rides the ``scan_findings.json`` artifact the kernel wrote. This node
    splays BOTH onto the EXACT ``findings.*`` keys run-scan-all.yaml bound —
    so run-gate.yaml and the synthesize prompt are byte-unchanged whether the
    scan ran in-process or on an engine."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}
    stats = f.get("scan_stats")
    if not isinstance(stats, dict):
        return {"findings": {}}

    def _s(key, default=None):
        return stats.get(f"scan.all.{key}", default)

    # the rich payload: authoritative for dict/list-shaped findings. Local-path
    # engines (builtin rung 1) share a filesystem with the control
    # plane; when the file is unreachable (future remote engines) we fall back
    # to scalars + empty shapes rather than fail the whole check.
    payload: dict = {}
    findings_path = str(_s("findings_path") or "")
    if findings_path and Path(findings_path).exists():
        try:
            payload = json.loads(Path(findings_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}

    return {
        "findings": {
            # ── format ────────────────────────────────────────────────────
            "format_detected": str(_s("format_detected", "") or ""),
            "format_conformant_pct": float(_s("format_conformant_pct", 0.0) or 0.0),
            "format_findings": payload.get("format_findings", []),
            "format_trainer_compat": payload.get("format_trainer_compat", {}),
            # ── dedup ─────────────────────────────────────────────────────
            "dedup_exact": int(_s("dedup_exact", 0) or 0),
            "dedup_pct": float(_s("dedup_pct", 0.0) or 0.0),
            "dedup_effective_size": int(_s("dedup_effective_size", 0) or 0),
            "dedup_total": int(_s("dedup_total", 0) or 0),
            # ── pii ───────────────────────────────────────────────────────
            "pii_rows": int(_s("pii_rows", 0) or 0),
            "pii_density": float(_s("pii_density", 0.0) or 0.0),
            "pii_counts": payload.get("pii_counts", {}),
            "pii_backend": str(_s("pii_backend", "") or ""),
            "pii_secret_rows": _s("secret_rows"),
            # ── profile ───────────────────────────────────────────────────
            "profile_lengths": payload.get("profile_lengths", {}),
            "profile_refusal_pct": float(_s("profile_refusal_pct", 0.0) or 0.0),
            "profile_short": int(_s("profile_short", 0) or 0),
            "profile_total": int(_s("profile_total", 0) or 0),
        }
    }


@python_function("assemble_dataset_fix_report")
def assemble_dataset_fix_report(state: dict) -> dict:
    """Hoist the dataset-FIX findings to TOP-LEVEL state as the DatasetFixReport
    (#991, ADR-0076 D3). The repair pipeline (dedup_apply → decontam_apply →
    anonymize) each dropped/redacted rows and wrote its counts to ``findings``;
    this narrates them deterministically (no LLM) into the fixed-dataset receipt,
    and emits an ``outcome`` channel entry so the console + `cairn gate` see the
    fix verdict. Reads only the ``findings`` merge channel (#600)."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}

    def _g(key, default=None):
        return f[key] if f.get(key) is not None else default

    fixed_path = str(_g("fix_anon_path", "") or "")
    original_rows = int(_g("fix_dedup_total", 0) or 0)
    fixed_rows = int(_g("fix_total", 0) or 0)  # anonymize's total = rows in the final file
    dropped_dups = int(_g("fix_dedup_dropped", 0) or 0)
    dropped_contaminated = int(_g("fix_decontam_dropped", 0) or 0)
    redacted_rows = int(_g("fix_redacted", 0) or 0)
    decontam_checked = bool(_g("fix_decontam_checked", False))
    pii_backend = str(_g("fix_anon_backend", "") or "")

    size_delta_pct = (
        round((fixed_rows - original_rows) / original_rows * 100, 1)
        if original_rows else 0.0
    )

    warnings: list[str] = []
    if pii_backend == "regex":
        warnings.append(
            "PII redaction ran on the zero-dep 'regex' backend (secrets+email+IP only) — "
            "install the [pii] extra (presidio) for phone/SSN/person coverage."
        )
    if not decontam_checked:
        warnings.append("No eval_path given — train↔eval contamination was NOT removed.")

    # The domain outcome channel (ADR-0075): a headline + the per-fix counts, so
    # the console's domainVerdict + `cairn gate` classify it (a fix always 'pass'es
    # once the writes are approved — it's the remediation, not the gate).
    outcome = [
        {"name": "readiness", "value": "FIXED", "severity": "ok"},
        {"name": "rows before", "value": original_rows, "severity": "ok"},
        {"name": "rows after", "value": fixed_rows, "severity": "ok"},
        {"name": "duplicates removed", "value": dropped_dups, "severity": "ok"},
        {"name": "contaminated removed", "value": dropped_contaminated, "severity": "ok"},
        {"name": "PII rows redacted", "value": redacted_rows, "severity": "ok"},
    ]
    summary = (
        f"Fixed {original_rows} rows → {fixed_rows} ({size_delta_pct:+}%): dropped "
        f"{dropped_dups} duplicate(s) + {dropped_contaminated} contaminated, redacted "
        f"PII in {redacted_rows} row(s) (backend={pii_backend or 'n/a'})."
        + (f" ⚠️ {len(warnings)} warning(s)." if warnings else "")
    )
    return {
        "fixed_path": fixed_path,
        "original_rows": original_rows,
        "fixed_rows": fixed_rows,
        "dropped_dups": dropped_dups,
        "dropped_contaminated": dropped_contaminated,
        "redacted_rows": redacted_rows,
        "size_delta_pct": size_delta_pct,
        "decontam_checked": decontam_checked,
        "pii_backend": pii_backend,
        "warnings": warnings,
        "outcome": outcome,
        "summary": summary,
    }


@python_function("assemble_incident_timeline")
def assemble_incident_timeline(state: dict) -> dict:
    """#994 (ADR-0076 D6) — reconstruct the incident CHRONOLOGY deterministically
    from the timestamped evidence the operators already collected, and merge it
    into the RCA's ``timeline`` (which the LLM synthesize step otherwise narrates
    thinly). The timeline is the standard RCA artifact; building it from facts
    (deploy time, error onset, alert time) — not LLM narration — is nearly free
    because the evidence-gather already timestamps everything it collects.

    Reads ``findings.change_correlation`` (git/correlate: deploy_time, error_onset,
    suspect_deploy, lag_human) + the alert's ``started_at`` (template_input). Keeps
    whatever ``symptom``/``root_cause`` the synthesize step produced (this node runs
    after it and only OVERRIDES ``timeline``)."""
    f = state.get("findings")
    f = f if isinstance(f, dict) else {}
    ti = state.get("template_input") or {}
    cc = f.get("change_correlation") if isinstance(f.get("change_correlation"), dict) else {}

    events: list[dict] = []

    def _add(ts: str, kind: str, label: str, detail: str = "") -> None:
        if ts:
            events.append({"ts": str(ts), "kind": kind, "label": label, "detail": detail})

    # alert fired (from the trigger's AlertContext).
    _add(str(ti.get("started_at", "") or ""), "alert",
         f"alert fired: {ti.get('symptom', 'incident')}", str(ti.get("service", "") or ""))
    # the suspect deploy (change/correlate).
    dep = cc.get("suspect_deploy") if isinstance(cc.get("suspect_deploy"), dict) else {}
    dep_label = dep.get("message") or dep.get("sha") or "deploy"
    _add(str(cc.get("deploy_time", "") or ""), "deploy",
         f"deploy: {str(dep_label)[:80]}",
         f"confidence {cc.get('confidence', '?')}" if cc.get("confidence") else "")
    # errors began.
    _add(str(cc.get("error_onset", "") or ""), "error",
         f"errors began ({cc.get('error_count', 0)} in window)" if cc.get("error_count")
         else "errors began",
         f"{cc.get('lag_human', '')} after deploy" if cc.get("lag_human") else "")

    # sort oldest-first; ISO-8601 strings sort lexicographically = chronologically.
    events.sort(key=lambda e: e["ts"])
    started_at = events[0]["ts"] if events else str(cc.get("error_onset", "") or "")

    # preserve the LLM's evolution read if present; else infer from the deploy→error lag.
    prior = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
    evolution = prior.get("evolution") or ("sudden" if cc.get("lag_seconds", 0) else "unknown")

    return {"timeline": {"started_at": started_at, "evolution": evolution, "chronology": events}}


@python_function("assemble_watch_report")
def assemble_watch_report(state: dict) -> dict:
    """Hoist the WATCH findings to TOP-LEVEL state as the WatchReport (#992, ADR-0076
    D4). The detector wrote whether the signal crossed; the (guarded) proposal wrote
    whether a human approved. Deterministic — no LLM. Emits an ADR-0075 outcome so
    the console + `cairn gate` see the watch verdict. Reads the findings channel."""
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}

    def _g(key, default=None):
        return f[key] if f.get(key) is not None else default

    metric = str(_g("watch_metric", "signal") or "signal")
    value = float(_g("watch_value", 0.0) or 0.0)
    threshold = float(_g("watch_threshold", 0.0) or 0.0)
    crossed = bool(_g("watch_crossed", False))
    approved = bool(_g("watch_approved", False))
    proposed_workflow = str(_g("watch_proposed_workflow", "") or "")

    if crossed:
        verdict = "APPROVED" if approved else "PROPOSED"
        summary = (
            f"{metric} = {value} crossed {threshold} → proposed {proposed_workflow or 'a retrain'} "
            f"({'approved' if approved else 'awaiting approval'})."
        )
    else:
        verdict = "OK"
        summary = f"{metric} = {value} within {threshold} — no drift, no proposal."

    outcome = [
        {"name": "watch", "value": verdict, "severity": "ok" if not crossed else "warn"},
        {"name": metric, "value": value, "target": threshold,
         "severity": "warn" if crossed else "ok"},
    ]
    return {
        "metric": metric, "value": value, "threshold": threshold,
        "crossed": crossed, "approved": approved,
        "proposed_workflow": proposed_workflow if crossed else "",
        "outcome": outcome, "summary": summary,
    }


@python_function("assemble_finetune_report")
def assemble_finetune_report(state: dict) -> dict:
    """Hoist the FinetuneReport fields from the ``findings`` merge channel to
    TOP-LEVEL state so the run's structured report carries them (#706).

    Every governed op writes its verdict/metric under ``findings.*`` — the
    reliable cross-runtime merge channel (#600). But the run's structured report
    (``RunResult.raw_output``, read at ``/runs/{id}/report``) is projected from
    TOP-LEVEL state keys, so only ``dataset_verdict`` (promoted top-level by the
    dataset subgraph's ``out_map``) ever threaded; ``cost_verdict``,
    ``model_verdict``, ``adapter_uri``, ``metrics`` and ``registered`` came back
    ``None``. This node reads ``findings`` (fully merged by the time it runs — it
    is the last node) and re-emits those values at top level, **deterministically**
    — the verdicts are FACTS the operators decided, not LLM narration. It also
    composes the schema's one required field, ``summary``, from those same facts,
    so the report is complete even when the LLM synthesize step is off/absent.

    Runtime-safe: reads only the findings channel #600 already proved threads
    under flatten+Temporal, and its ``outputs`` are declared on the python_node so
    LangGraph keeps the top-level channels (undeclared writes are dropped).
    """
    f = state.get("findings")
    if not isinstance(f, dict):
        f = {}

    def _g(key: str, default: Any = None) -> Any:
        # findings first (where the ops wrote), then a top-level fallback
        # (dataset_verdict lives top-level too, from the subgraph out_map).
        if f.get(key) is not None:
            return f[key]
        if state.get(key) is not None:
            return state[key]
        return default

    dataset_verdict = _g("dataset_verdict")
    cost_verdict = _g("cost_verdict")
    estimated_usd = _g("cost_usd")
    model_verdict = _g("model_verdict")
    # #895 — the baseline-regression verdict (eval/delta): this run's task metrics
    # vs the prior promoted model. None ⇒ no run-eval-delta step; "pass" with an
    # empty `regressions` ⇒ no baseline supplied / no regression over budget.
    regression_verdict = _g("delta_verdict")
    regressions = _g("delta_regressions", default=[]) or []
    adapter_uri = _g("adapter_uri")
    model_uri = _g("model_uri")
    registered = bool(_g("model_registered", default=False))
    metrics = _g("train_metrics", default={}) or {}
    benchmark_metrics = _g("benchmark_metrics", default={}) or {}
    lineage = _g("lineage_record", default={}) or {}
    lineage_hash = _g("lineage_hash", default="") or ""
    # #726 — serve-in-loop results (present only when serve_enabled ran a serve/vllm
    # step after promote; keys absent ⇒ no serve leg, and the report omits them).
    endpoint_url = _g("serve_endpoint_url")
    serve_status = _g("serve_status")
    served_tps = _g("bench_tokens_per_sec")

    # Deterministic one-line summary from the facts (mirrors the synthesize
    # prompt's shape, but never depends on the LLM having run).
    parts: list[str] = []
    if dataset_verdict:
        parts.append(f"Dataset {str(dataset_verdict).upper()}")
    if estimated_usd is not None:
        cv = f" {str(cost_verdict).upper()}" if cost_verdict else ""
        try:
            parts.append(f"cost ~${float(estimated_usd):.2f}{cv}")
        except (TypeError, ValueError):
            pass
    eval_loss = metrics.get("eval_loss") if isinstance(metrics, dict) else None
    if adapter_uri:
        tail = (
            f" (eval_loss {float(eval_loss):.3f})"
            if isinstance(eval_loss, (int, float))
            else ""
        )
        parts.append(f"trained → {adapter_uri}{tail}")
    if model_verdict:
        parts.append(f"model-eval {str(model_verdict).upper()}")
    if regressions:
        parts.append(f"{len(regressions)} task-metric regression(s) vs baseline")
    # A promotion status is always meaningful, so `parts` is never empty; an
    # otherwise-blank run reports just "not promoted".
    parts.append("promoted" if registered else "not promoted")
    if serve_status:
        served_tail = (
            f" ({float(served_tps):.1f} tok/s)"
            if isinstance(served_tps, (int, float))
            else ""
        )
        parts.append(f"served {str(serve_status).upper()}{served_tail}")
    summary = " → ".join(parts)

    # Required + dict fields always emitted; optional string/number fields only
    # when present (keep the report clean — absent stays absent, not "").
    out: dict = {
        "registered": registered,
        "metrics": metrics,
        "benchmark_metrics": benchmark_metrics,
        "lineage": lineage,
        "lineage_hash": lineage_hash,
        "summary": summary,
    }
    if dataset_verdict is not None:
        out["dataset_verdict"] = dataset_verdict
    if cost_verdict is not None:
        out["cost_verdict"] = cost_verdict
    if estimated_usd is not None:
        out["estimated_usd"] = estimated_usd
    if model_verdict is not None:
        out["model_verdict"] = model_verdict
    if regression_verdict is not None:
        out["regression_verdict"] = regression_verdict
    if regressions:
        out["regressions"] = regressions
    if adapter_uri is not None:
        out["adapter_uri"] = adapter_uri
    if model_uri is not None:
        out["model_uri"] = model_uri
    if endpoint_url is not None:
        out["endpoint_url"] = endpoint_url
    if serve_status is not None:
        out["serve_status"] = serve_status
    if served_tps is not None:
        try:
            out["served_tokens_per_sec"] = float(served_tps)
        except (TypeError, ValueError):
            pass
    return out


@python_function("explode_root_cause")
def explode_root_cause(state: dict) -> dict:
    """Flatten ``state.rca_root_cause`` (an RCA.RootCause object) into the
    flat ``root_cause_*`` shape the post-mortem template's
    ``PostmortemRequest`` schema expects.

    Transition helper: the Phase 2b ``subgraph_node`` mapping is
    field-by-field — it can't pluck nested object members. Until a
    generic ``transform/explode`` operator pack lands, the orchestrated
    template uses this built-in to bridge the gap.
    """
    rc = state.get("rca_root_cause") or {}
    if not isinstance(rc, dict):
        return {}
    return {
        "root_cause_description": rc.get("description", ""),
        "root_cause_service":     rc.get("service", ""),
        "root_cause_confidence":  rc.get("confidence", 0.0),
        "root_cause_category":    rc.get("category", "unknown"),
    }


@python_function("explode_timeline")
def explode_timeline(state: dict) -> dict:
    """#994 (ADR-0076 D6) — lift the RCA's deterministic CHRONOLOGY out of the
    ``rca_timeline`` object (a Timeline with a ``.chronology`` list, built by
    ``assemble_incident_timeline``) into a flat top-level ``chronology`` key, so the
    orchestrated template's ``subgraph_node`` mapping can pass it into the
    post-mortem's ``PostmortemRequest.chronology``.

    Same bridge role as ``explode_root_cause``: the Phase 2b subgraph mapping is
    field-by-field and can't pluck a nested list. Empty/malformed => ``[]`` so the
    post-mortem draft falls back to an evidence-based timeline."""
    tl = state.get("rca_timeline") or {}
    chronology = tl.get("chronology") if isinstance(tl, dict) else None
    return {"chronology": chronology if isinstance(chronology, list) else []}


@python_function("confidence_scorer")
def confidence_scorer(state: dict) -> dict:
    """Deterministic confidence score based on evidence count and diversity."""
    evidence = state.get("evidence") or []
    if not evidence:
        score = 0.0
    else:
        # 0.2 per evidence item, capped at 1.0, with diversity bonus
        types_seen = {e.get("type") for e in evidence}
        diversity_bonus = min(0.2, 0.05 * len(types_seen))
        score = min(1.0, 0.2 * len(evidence) + diversity_bonus)

    return {
        "findings": {
            "confidence": round(score, 2),
            "evidence_count": len(evidence),
        }
    }
