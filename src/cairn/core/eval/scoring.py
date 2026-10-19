"""Shared run-scoring — the label + auto-eval sequence, callable from BOTH the
dispatcher's inline completion hook (#484, single-process dev) and the
continuous-eval bus consumer (#477, durable/out-of-process topologies).

Extracted so the two paths can't drift: whatever the inline hook does on a
terminal run, the bus consumer does identically. ``skip_if_scored`` makes the
consumer idempotent — it no-ops a run the inline hook already scored, so the two
compose without double-scoring (ADR-0060 §D1).

Notification-only (ADR-0010): scoring OBSERVES a completed run and writes labels
+ a verdict; it never triggers a skill or mutates a workflow. Best-effort by the
same contract as ``label_run_failures``/``auto_eval_run`` — nothing here raises.

The training-history row (#2034) and the divergence reaction (#2030) that used
to follow the verdict here are the platform's: its ``scoring`` run hook writes
them right after calling this (#2421 decision 7).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from cairn.core.eval.auto_eval import auto_eval_run
from cairn.core.eval.diagnosis import label_run_failures

_log = logging.getLogger("cairn.eval.scoring")


def run_template_ref(inv: Any) -> str:
    """The workflow a Run executed. THE canonical resolver — every surface
    (dispatcher, ``triggers.server``, console) delegates here.

    ``Run.template_ref`` is authoritative (#1096): the dispatcher records it at
    admission for a direct template run, or as soon as skill matching resolves
    one. Everything below it is the LEGACY inference chain, kept only for rows
    written before #1096 — ``results[].template_name``, then the manual/web
    trigger payload's ``template``, then a path-like fingerprint. ``""`` when
    none apply.
    """
    recorded = getattr(inv, "template_ref", "") or ""
    if recorded:
        return str(recorded)
    for s in getattr(inv, "results", None) or []:
        if getattr(s, "template_name", ""):
            return str(s.template_name)
    trigger = getattr(inv, "trigger", None)
    tmpl = (trigger and (trigger.payload or {}).get("template")) or ""
    if tmpl:
        return str(tmpl)
    fp = getattr(inv, "fingerprint", "") or ""
    return fp if "/" in fp else ""


async def score_run(
    run_id: str, *, store: Any, ledger: Any, skip_if_scored: bool = False
) -> Optional[str]:
    """Label + auto-eval one terminal run, best-effort. Returns the auto-eval
    verdict (persisted to ``runs.final_verdict``) or ``None``.

    ``store`` owns the run (read ``get``/``get_events``, write ``update``);
    ``ledger`` is the eval ledger to write labels + verdicts to — pass the pair
    that co-locate (the dispatcher passes its own backend; the consumer passes
    the process singleton).

    ``skip_if_scored``: when True, no-op if the run already carries any eval-run
    or failure label — the idempotency guard the bus consumer needs so it never
    re-scores what the inline hook already did.
    """
    try:
        inv = await store.get(run_id)
        if inv is None:
            return None
        if skip_if_scored:
            if await ledger.get_eval_runs(run_id) or await ledger.get_failure_labels(run_id):
                return None  # already scored inline (or by a prior consume)
        events = await store.get_events(run_id)
        run_dict = inv.model_dump(mode="json")
        await label_run_failures(
            run_dict, [e.model_dump(mode="json") for e in events], ledger=ledger
        )
        # auto_eval_run keys off `template_ref` (the plain Run dump carries none).
        run_dict["template_ref"] = run_template_ref(inv)
        verdict = await auto_eval_run(run_dict, ledger=ledger)
        if verdict:
            try:
                await store.update(run_id, final_verdict=verdict)
            except Exception:  # noqa: BLE001 — best-effort denormalization
                _log.warning("failed to persist final_verdict for run %s", run_id, exc_info=True)
        return verdict
    except Exception:  # noqa: BLE001 — best-effort, never break the caller
        _log.warning("score_run failed for %s", run_id or "<unknown>", exc_info=True)
        return None


__all__ = ["run_template_ref", "score_run"]
