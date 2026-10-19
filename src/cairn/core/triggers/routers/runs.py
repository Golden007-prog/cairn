"""Runs + series surface — run listing/detail/lineage, series, artifacts,
report, pilot review, evaluate, diagnosis. Extracted from server.py in #1191;
handler bodies are verbatim (``@app.…`` → ``@r.…``).
"""
# NO `from __future__ import annotations` here: request models (e.g.
# PilotReviewRequest) are lazily imported inside the factory (cycle rule), so
# PEP 563 string annotations would be unresolvable when FastAPI builds the
# OpenAPI schema; without it, annotations bind the factory-local classes.

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from starlette.responses import Response

from cairn.core.dispatch.pipeline_bridge import findings_of, reconstruct_step_output
from cairn.core.run_failure import explain_failure
from cairn.core.storage import get_storage
from cairn.core.storage.hydration import skipped_rows
from cairn.core.storage.transitions import PIPELINE_TERMINAL
from cairn.core.triggers.auth import require_auth
from cairn.core.workspace import current_workspace

# #1880 — the same rules the console badge used (hooks.ts isAwaitingGate / HUMAN_WAITS)
_AWAITING_RE = re.compile(r"await|paus|pending", re.IGNORECASE)
_HUMAN_WAITS = ("awaiting_approval", "awaiting_advance")


def _with_failure(inv, data: dict) -> dict:
    """Attach the human-readable failure explanation (#1594).

    The engine has always recorded a cause on `Run.error`; no surface rendered
    it, and what it recorded was a raw `str(exc)` with job ids and exception
    classes in it. Every reader — console, CLI, anything else — now gets the
    same explanation from one place rather than each writing its own table of
    error-string guesses.

    Attached in BOTH the listing and the detail: a runs table that shows a red
    row and makes you click to learn anything is the surface this issue is
    about. Cheap enough for a list — a compiled-regex match over one string,
    only for rows in a terminal failure state.
    """
    e = explain_failure(inv.error, sandbox=bool(getattr(inv, "sandbox", False)),
                        status=str(inv.status))
    if e is not None:
        data["failure"] = e.model_dump(mode="json")
    return data


def _first_template_name(results: Any) -> str:
    """``results[0].template_name`` whether the summary is a ``RunResultSummary``
    (what the stores hand back) or a plain dict (older rows / tests). A paused
    run with a summary used to 500 the whole pending-approvals count (#1969)."""
    first = (results or [None])[0]
    if first is None:
        return ""
    if isinstance(first, dict):
        return str(first.get("template_name") or "")
    return str(getattr(first, "template_name", "") or "")


def _find_in_payloads(payloads: Any, key: str) -> Any:
    """The first value stored under ``key`` anywhere in a run's result payloads.

    A run's structured result is a tree whose shape depends on the pipeline
    (a task leg's operator output sits at the top level, a flow leg's under
    ``findings``), so the bundle looks for the model card and the lineage record
    by KEY rather than by path — the alternative is a path table that goes stale
    the first time someone reorders a pipeline. First hit wins, depth-first.
    """
    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                hit = walk(v)
                if hit is not None:
                    return hit
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                hit = walk(v)
                if hit is not None:
                    return hit
        return None

    return walk(payloads or [])



async def run_in_workspace_or_404(dispatcher: Any, run_id: str) -> Any:
    """Fetch a run, 404 if absent OR owned by another workspace (#228 B3/B6).
    Cross-workspace access is indistinguishable from not-found, so run existence
    never leaks across tenants. Module-level so the platform's run-scoped routers
    (#2421) apply the same rule."""
    inv = await dispatcher.get(run_id)
    if inv is None or inv.workspace_id != current_workspace():
        raise HTTPException(status_code=404, detail="run not found")
    return inv


async def pipeline_budget_usd(pipeline_bridge: Any, inv: Any) -> Optional[float]:
    """#2311 — the owning pipeline's ``governance.budget.usd``: the cap a leg runs
    under when it sets no ``max_cost_usd`` of its own. None when the run has no
    pipeline or the pipeline declares no budget (never invented)."""
    pid = getattr(inv, "bundle_pipeline_id", None)
    if not pid or pipeline_bridge is None:
        return None
    try:
        rec = await pipeline_bridge.store.get(pid)
    except Exception:  # noqa: BLE001 — a pipeline-store hiccup must not break the report
        return None
    budget = ((getattr(rec, "governance", None) or {}).get("budget") or {}) if rec else {}
    usd = budget.get("usd") if isinstance(budget, dict) else None
    return float(usd) if usd else None


def build_runs_router(dispatcher, pipeline_bridge=None, review_store=None) -> APIRouter:
    """The /runs reads and run-scoped actions, closed over the shared dispatcher.
    (The pilot report, the series ledger and the adapter chain are the platform's
    routers since #2421 — ``cairn_platform.routers.pilot``.)

    ``pipeline_bridge`` is passed for its ``store`` alone (#1630): the
    adapter-chain read joins each train run back to the PipelineRecord that owns
    its cycle, and the bridge already holds the one resolved pipelines store.
    ``None`` falls back to the process-wide default — the same store, resolved
    the same way — so the older single-argument call still works."""
    # lazy: server.py is fully imported by the time build_app calls this —
    # module-level helpers + request models the inline block shared.
    from cairn.core.triggers.server import (  # noqa: PLC0415 — routers ↔ server cycle at import time
        PilotReviewRequest,
        _harvest_artifacts,
        _langfuse_trace_url,
        _normalize_run_status,
        _run_failure_types,
        _run_template_ref,
        _s3_presigned_url,
        logger,
    )

    r = APIRouter()

    async def _run_in_ws_or_404(run_id: str):
        return await run_in_workspace_or_404(dispatcher, run_id)

    @r.get("/runs", dependencies=[Depends(require_auth)])
    async def list_investigations(
        lane: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        # workspace-scoped (#228 B3): only this request's workspace's runs.
        # Normalize status synonyms → canonical vocab so an agent's `completed`
        # matches `done` instead of silently returning 0 runs (#651).
        invs = await dispatcher.list(lane=lane, status=_normalize_run_status(status), workspace_id=current_workspace(), limit=limit)  # type: ignore[arg-type]
        # Exclude the full result_payloads from the list projection so listing
        # stays cheap (#76); the full structured result is on GET /runs/{id}.
        out: dict[str, Any] = {
            "count": len(invs),
            "investigations": [_with_failure(i, i.model_dump(mode="json", exclude={"result_payloads"}))
                               for i in invs],
        }
        # #1601 — a row too corrupt to rebuild is SKIPPED rather than 500ing the
        # listing, but silently dropping it would make the list quietly wrong.
        # Say so, so "I have 39 runs and see 38" is answerable.
        skipped = skipped_rows()
        if skipped:
            out["unreadable"] = skipped
        return out

    # #1880 — ONE read for "what is waiting on a human", computed server-side.
    # The top-bar badge used to poll three full lists (runs 5 s, reviews 10 s,
    # pipelines 5 s) on EVERY route to derive one number; the Approvals page
    # reads the same answer. Same rule as the console's tallyApprovals: runs
    # paused at a gate (non-sandbox), pipeline steps parked on a human, and open
    # reviews not already counted through their pipeline step. Declared before
    # /runs/{run_id} so the literal path is not captured by the parameter route.
    @r.get("/runs/pending-approvals", dependencies=[Depends(require_auth)])
    async def pending_approvals() -> dict[str, Any]:
        ws = current_workspace()
        invs = await dispatcher.list(workspace_id=ws, limit=500)
        awaiting = [i for i in invs
                    if _AWAITING_RE.search(str(getattr(i, "status", "") or "")) and not getattr(i, "sandbox", False)]
        # (#2421: mid-training proposals are the platform's — `GET /runs/pending-proposals`.)
        gates: list[dict[str, Any]] = []
        if pipeline_bridge is not None:
            try:
                recs = await pipeline_bridge.store.list(workspace_id=ws)
            except Exception:  # noqa: BLE001 — a pipeline-store hiccup must not blank the badge
                recs = []
            for rec in recs:
                # #2541 — a finished pipeline collects no more approvals, whatever
                # its step_state still says (records cancelled before the fix
                # kept their gate at `awaiting_approval`).
                if str(getattr(rec, "status", "")) in PIPELINE_TERMINAL:
                    continue
                steps = [sid for sid, st in (getattr(rec, "step_state", None) or {}).items()
                         if str(getattr(st, "status", "")) in _HUMAN_WAITS]
                if steps:
                    gates.append({"pipeline_id": rec.pipeline_id, "name": getattr(rec, "pipeline_name", "") or rec.pipeline_id, "steps": steps})
        counted = {f"{g['pipeline_id']}/{s}" for g in gates for s in g["steps"]}
        reviews = 0
        if review_store is not None:
            try:
                rows = await review_store.list_reviews(status="open", limit=500, workspace_id=ws)
            except Exception:  # noqa: BLE001
                rows = []
            reviews = sum(1 for x in rows
                          if f"{getattr(x, 'pipeline_id', '') or ''}/{getattr(x, 'step_id', '') or ''}" not in counted)
        steps_total = sum(len(g["steps"]) for g in gates)
        return {
            "runs": len(awaiting), "reviews": reviews, "pipelines": steps_total,
            "total": len(awaiting) + reviews + steps_total,
            "pipeline_gates": gates,
            "awaiting_runs": [{
                "id": i.id, "status": i.status, "created_at": getattr(i, "created_at", None),
                "template_name": getattr(i, "template_ref", "") or _first_template_name(getattr(i, "results", None)),
            } for i in awaiting],
        }

    @r.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def get_run(run_id: str) -> dict[str, Any]:
        inv = await _run_in_ws_or_404(run_id)
        data = _with_failure(inv, inv.model_dump(mode="json"))
        # #572: under CAIRN_RUNTIME=temporal a composed run pauses INSIDE the durable
        # workflow, so the dispatcher's post-run block that persists `results`/
        # `result_payloads` never runs while paused — GET /runs/{id} was
        # observability-dark (empty `results`, no visible gate). The live event log
        # IS recorded, so mid-flight (and only when the terminal rollup isn't there
        # yet) project the per-node steps + pending gate from it, so the record
        # self-describes regardless of runtime. A finished run already carries the
        # real `results`, and an in_process pause already persisted them — both skip.
        if inv.status not in ("done", "failed", "cancelled") and not inv.results:
            from cairn.core.run_progress import run_progress  # noqa: PLC0415 — core's own projection (#2421)
            evs = await dispatcher.events(run_id)
            data["progress"] = run_progress([e.model_dump(mode="json") for e in evs])
        return data

    @r.get("/runs/{run_id}/findings", dependencies=[Depends(require_auth)])
    async def get_run_findings(run_id: str) -> dict[str, Any]:
        """The run's findings — the SAME dict the pipeline gates read (#1790).

        A door over ``reconstruct_step_output``, never a second implementation:
        what a gate/wire sees and what a human reads over HTTP must be the same
        normalization of ``result_payloads``, or "why did the gate block?" has
        two answers. Before this route the only way to read a leg's findings was
        `docker exec` + an in-container python call into that function.

        Shape: ``{run_id, status, output, findings}`` — ``output`` is the whole
        normalized dict (top-level produce names included) and ``findings`` is
        its ``findings`` sub-dict, the flat scalars a gate cites. Both are
        always dicts: a findings-less run is ``{}``, never ``null`` (a caller
        that gets None cannot tell "no findings" from "route is broken"), and
        only an unknown/cross-workspace run is a 404.
        """
        inv = await _run_in_ws_or_404(run_id)
        output = reconstruct_step_output(inv) or {}
        found = findings_of(output)  # #1804 — task legs publish at top level
        return {
            "run_id": run_id,
            "status": str(getattr(inv, "status", "") or ""),
            "output": output,
            "findings": found,
        }

    @r.get("/runs/{run_id}/lineage", dependencies=[Depends(require_auth)])
    async def get_run_lineage(run_id: str) -> dict[str, Any]:
        """#989 (ADR-0076, D1) — the ad-hoc run-lineage chain for a run: its
        ancestors (walked up via ``parent_run``) and its immediate children.

        Ancestors are resolved by following ``parent_run`` pointers with
        ``store.get`` (each hop hydrates the blob — no index needed), capped at a
        depth to bound a cycle/pathological chain. Children are the runs in this
        workspace whose ``parent_run == run_id`` (a bounded ``list`` scan — the
        console only needs the immediate generation, not the whole subtree)."""
        inv = await _run_in_ws_or_404(run_id)

        def _summ(r) -> dict[str, Any]:
            return {
                "id": r.id,
                "status": r.status,
                "workflow": _run_template_ref(r),
                "created_at": r.created_at,
                "parent_run": getattr(r, "parent_run", "") or "",
            }

        # walk ancestors: nearest parent first, root last. Cap depth + guard cycles.
        ancestors: list[dict[str, Any]] = []
        seen = {run_id}
        cur = getattr(inv, "parent_run", "") or ""
        for _ in range(50):
            if not cur or cur in seen:
                break
            seen.add(cur)
            parent = await dispatcher.get(cur)
            if parent is None:
                # dangling pointer (parent purged) — record the id, stop the walk.
                ancestors.append({"id": cur, "status": "unknown", "workflow": "",
                                  "created_at": "", "parent_run": ""})
                break
            ancestors.append(_summ(parent))
            cur = getattr(parent, "parent_run", "") or ""

        # immediate children: workspace-scoped list scan for parent_run == run_id.
        siblings = await dispatcher.list(workspace_id=current_workspace(), limit=500)  # type: ignore[arg-type]
        children = [_summ(r) for r in siblings if (getattr(r, "parent_run", "") or "") == run_id]

        # ADR-0082 P0 (#1072): the focal run's reproducibility pins ride along
        # (ancestors/children stay light — fetch a run's own lineage for its pins).
        return {
            "run": _summ(inv),
            "ancestors": ancestors,
            "children": children,
            "train_pins": [dict(p) for p in (getattr(inv, "train_pins", None) or [])],
        }

    @r.get("/runs/{run_id}/artifacts")
    async def get_run_artifacts(run_id: str) -> dict[str, Any]:
        """What a run produced (#179 Results): the workflow it ran, whether a
        structured report is available (downloadable at ``/runs/{id}/report``),
        and the artifact references its result carries (models, datasets, files).
        Empty `artifacts` + `report_available:false` is normal for a run still in
        flight or one that didn't emit a structured result."""
        inv = await _run_in_ws_or_404(run_id)
        payloads = inv.result_payloads or []
        return {
            "run_id": run_id,
            "workflow": _run_template_ref(inv),
            "status": inv.status,
            "report_available": bool(payloads),
            "report_url": f"/runs/{run_id}/report" if payloads else None,
            "artifacts": _harvest_artifacts(payloads),
        }

    @r.get("/runs/{run_id}/artifacts/download")
    async def download_run_artifact(run_id: str, uri: str) -> Response:
        """Resolve one of a run's artifact refs to a download (#353): local
        ``file://`` refs stream from disk, ``s3://`` redirects to a short-lived
        presigned URL, other remote schemes are an honest 501.

        SECURITY: ``uri`` must exactly match one of the run's harvested artifact
        refs — this is the containment check (an arbitrary ``?uri=/etc/passwd``
        is a 404, never a stream). The run itself is workspace-guarded."""
        inv = await _run_in_ws_or_404(run_id)
        harvested = {a["uri"] for a in _harvest_artifacts(inv.result_payloads or [])}
        if uri not in harvested:
            raise HTTPException(status_code=404, detail="artifact not found on this run")
        # local file — file:// URI or a bare path harvested from a known key
        if uri.startswith("file://") or uri.startswith("/"):
            path = Path(uri[len("file://"):] if uri.startswith("file://") else uri)
            if not path.is_file():
                raise HTTPException(status_code=404, detail="artifact file missing on the engine host")
            return FileResponse(path, filename=path.name)
        if uri.startswith("s3://"):
            return RedirectResponse(_s3_presigned_url(uri), status_code=307)
        scheme = uri.split("://", 1)[0]
        raise HTTPException(
            status_code=501,
            detail=f"{scheme}:// artifacts can't be downloaded via the engine yet — copy the ref and fetch it with the {scheme} tooling",
        )

    @r.get("/runs/{run_id}/bundle")
    async def get_run_bundle(run_id: str) -> Response:
        """#2032 (ADR-0102 D5) — ONE download that makes this run reproducible
        somewhere else: the effective config (YAML), the metrics (CSV + JSON),
        the eval results, the lineage record and the model card, as a zip.

        Assembled SERVER-SIDE on purpose. Each of the five already has a read,
        and a console that fired five requests and zipped them in the browser
        would be a fifth place that decides what "the config" is — the exact
        drift ADR-0103 D3 exists to stop. The gathering lives here; the shape
        lives in ``cairn.core.run_bundle``, which is pure and tested without a
        store.

        Never 404s for a run that exists but produced little: a bundle always
        carries every file, with ``MANIFEST.json`` saying what is absent and
        why. A run in another workspace is a 404 like every other run read."""
        from cairn.core.run_bundle import (  # noqa: PLC0415 — zip/yaml work, only on a download
            RunBundleSources,
            build_run_bundle,
            bundle_filename,
        )

        inv = await _run_in_ws_or_404(run_id)
        payloads = inv.result_payloads or []

        try:
            events = await dispatcher.events(run_id)
        except Exception as e:  # noqa: BLE001 — a store without events must not lose the bundle
            logger.warning("bundle: events unavailable for %s: %s", run_id, e)
            events = []

        # (#2421: eval verdicts are the platform's ledger; the public bundle carries none.)
        eval_rows: list[dict[str, Any]] = []
        lineage: dict[str, Any] = {
            "run_id": run_id,
            "parent_run": getattr(inv, "parent_run", "") or "",
            "workflow": _run_template_ref(inv),
            "train_pins": [dict(p) for p in (getattr(inv, "train_pins", None) or [])],
        }
        record = _find_in_payloads(payloads, "lineage_record") or _find_in_payloads(payloads, "record")
        if isinstance(record, dict):
            lineage["record"] = record

        card = _find_in_payloads(payloads, "card_markdown")
        if not isinstance(card, str):
            card = ""

        blob = build_run_bundle(RunBundleSources(
            run_id=run_id, events=events, eval_runs=eval_rows, lineage=lineage,
            model_card=card,
            meta={"workflow": _run_template_ref(inv), "status": inv.status,
                  "created_at": getattr(inv, "created_at", "") or "",
                  "ended_at": getattr(inv, "ended_at", "") or ""},
        ))
        return Response(
            content=blob, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{bundle_filename(run_id)}"'},
        )

    @r.get("/runs/{run_id}/report")
    async def get_run_report(run_id: str) -> Response:
        """Download a run's structured result (the RunResult payload) as JSON —
        the 'report file' the run produced. 404 if the run has no structured
        result yet (queued/running, or a run that never emitted one)."""
        inv = await _run_in_ws_or_404(run_id)
        payloads = inv.result_payloads or []
        if not payloads:
            raise HTTPException(status_code=404, detail="run has no structured result")
        body = json.dumps(payloads[0] if len(payloads) == 1 else payloads, indent=2, default=str)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{run_id}-report.json"'},
        )

    async def _pipeline_budget(inv: Any) -> Optional[float]:
        return await pipeline_budget_usd(pipeline_bridge, inv)

    return r
