"""The run bundle — one download that makes a run reproducible elsewhere (#2032).

ADR-0102 **D5**: a run's facts already exist, and every one of them lives behind
a different read. The effective config is inside a ``parameters`` event (T4,
ADR-0103 D3); the curve is in ``metrics``/``scalars`` events (T6); the verdicts
are in the eval ledger; the provenance is a ``lineage/record`` finding; the model
card is a file the ``model/card`` operator wrote. Reproducing a run somewhere
else means collecting five things by hand from five surfaces — which is the same
as not being able to.

This module is the assembler. It is **pure**: :func:`build_run_bundle` takes a
:class:`RunBundleSources` value and returns zip bytes. The gathering — which
store, which workspace, which 404 — is the router's job
(``cairn/core/triggers/routers/runs.py``), so the shape of the artifact is
testable without a store, and the acceptance criterion ("the config equals the
run's effective spec") is a statement about a function rather than about a
deployment.

Three rules the tests pin:

**The shape never varies.** Every bundle carries every file, even when the run
has nothing to put in one. A bundle whose file list depends on the run cannot be
scripted against; a placeholder plus a ``MANIFEST.json`` that says *why* it is
empty can.

**Nothing is invented.** A series absent at a step is an empty CSV cell, never a
zero. This is the ADR-0102 D2 rule about the history table, and it applies here
for the same reason: a fabricated number in a reproducibility artifact is worse
than a hole, because a hole is visible.

**It is deterministic.** Same sources, same bytes — the zip entries carry a fixed
timestamp and are written in a fixed order, so a bundle can be checksummed and
two runs' bundles can be diffed.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

from cairn.core.reporting import METRICS_EVENT_TYPE, PARAMETERS_EVENT_TYPE, SCALARS_EVENT_TYPE

#: The five things the ticket names. ``metrics`` is one ITEM in two formats — a
#: CSV for a spreadsheet and a JSON for a script — because those are two
#: audiences, not two items.
BUNDLE_ITEMS = ("config", "metrics", "eval", "lineage", "model_card")

#: item → the file(s) it writes. Ordered; the zip is written in this order.
ITEM_FILES: dict[str, tuple[str, ...]] = {
    "config": ("config.yaml",),
    "metrics": ("metrics.csv", "metrics.json"),
    "eval": ("eval.json",),
    "lineage": ("lineage.json",),
    "model_card": ("MODEL_CARD.md",),
}

MANIFEST_NAME = "MANIFEST.json"

#: A fixed zip timestamp (the DOS epoch) so identical sources produce identical
#: bytes. ZipInfo defaults to *now*, which would make every download differ.
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass
class RunBundleSources:
    """Everything the bundle is assembled from, already read.

    Each field is independently optional: a run can legitimately have no eval,
    no card, or (until T6's trainer callback ships) no curve. The assembler
    reports what is absent rather than refusing to build.
    """

    run_id: str
    #: The run's events, as ``RunEvent`` models or plain dicts.
    events: Any = field(default_factory=list)
    #: Eval ledger rows for this run, newest first, as dicts.
    eval_runs: list[dict[str, Any]] = field(default_factory=list)
    #: ``GET /runs/{id}/lineage``'s payload, plus the ``lineage/record`` finding
    #: under ``record`` when the run produced one.
    lineage: dict[str, Any] = field(default_factory=dict)
    #: The rendered ``MODEL_CARD.md``.
    model_card: str = ""
    #: Extra facts for the manifest (workflow, status, times) — never a source.
    meta: dict[str, Any] = field(default_factory=dict)


# ─── reading the run's events ─────────────────────────────────────────────


def event_pairs(events: Any) -> list[tuple[str, dict[str, Any]]]:
    """``[(type, data), …]`` from either ``RunEvent`` models or plain dicts.

    Lives here, not in ``cairn_platform.training_history`` (which imports it), so a
    bundle download never pulls the training-history storage stack in.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for e in events or []:
        if isinstance(e, dict):
            t, d = e.get("type", ""), e.get("data") or {}
        else:
            t, d = getattr(e, "type", ""), getattr(e, "data", None) or {}
        if isinstance(d, dict):
            out.append((str(t), d))
    return out


def effective_config(events: Any) -> dict[str, Any]:
    """The run's effective spec — the ``inputs`` of its last ``parameters`` event.

    ADR-0103 D3 records one ``parameters`` event per step, carrying the config
    the step actually ran with (which is byte-for-byte the launch preview,
    ``tests/core/test_trainspec_tiers.py``). The LAST one wins, matching
    ``training_history.training_parameters``: in a two-stage recipe, the spec the
    produced artifact came out of is the later one.

    Deliberately NOT re-derived from the run record, the pipeline YAML or the
    history table. Those are three views that can disagree with what ran; the
    event is what ran.
    """
    hits = [d for t, d in event_pairs(events) if t == PARAMETERS_EVENT_TYPE]
    if not hits:
        return {}
    last = hits[-1]
    return dict(last.get("inputs") or {})


def effective_run_config(events: Any) -> dict[str, Any]:
    """The governance/operational ``config`` block of the same event.

    Kept separate from :func:`effective_config` in the YAML because they are
    bound differently: ``inputs`` are per-run and a replayer may set them, while
    a governance-scoped config value is ENV-OWNED and a run-supplied one is
    silently DROPPED (ADR-0068/0070, #905). Flattening them into one block would
    invite exactly that mistake on replay.
    """
    hits = [d for t, d in event_pairs(events) if t == PARAMETERS_EVENT_TYPE]
    return dict((hits[-1].get("config") or {})) if hits else {}


def metrics_rows(events: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """``(rows, series_names)`` — one row per ``metrics`` event, in order.

    Series names are collected in first-seen order rather than sorted: the order
    an operator declared them is more useful to a reader than alphabetical, and
    it is stable for a given operator.
    """
    rows: list[dict[str, Any]] = []
    names: list[str] = []
    for t, d in event_pairs(events):
        if t != METRICS_EVENT_TYPE:
            continue
        series = d.get("series")
        if not isinstance(series, dict):
            continue
        for k in series:
            if k not in names:
                names.append(k)
        rows.append({"step": d.get("step"), "node": str(d.get("node") or ""), **series})
    return rows, names


def end_scalars(events: Any) -> dict[str, Any]:
    """The merged ``scalars`` events — a run's end-of-step summary values."""
    out: dict[str, Any] = {}
    for t, d in event_pairs(events):
        if t == SCALARS_EVENT_TYPE and isinstance(d.get("values"), dict):
            out.update(d["values"])
    return out


# ─── rendering each file ──────────────────────────────────────────────────


def _yaml_dump(data: Any) -> str:
    # PyYAML is a base dependency of the engine (unlike in the operator packs).
    import yaml  # noqa: PLC0415 — heavy-ish and only needed on a download

    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _config_yaml(src: RunBundleSources) -> tuple[str, bool, str]:
    inputs = effective_config(src.events)
    if not inputs:
        return ("# no effective config: this run recorded no `parameters` event\n"
                "# (ADR-0103 D3 — a step records one before it runs).\n", False,
                "the run recorded no `parameters` event")
    body = _yaml_dump(inputs)
    cfg = effective_run_config(src.events)
    text = (
        "# The run's EFFECTIVE spec — the `inputs` of its `parameters` event,\n"
        "# which is what the step actually ran with. Replay with these.\n"
        + body
    )
    if cfg:
        text += (
            "\n# --- resolved config (NOT per-run) ---\n"
            "# Governance-scoped values are environment-owned: a run-supplied\n"
            "# value here is silently DROPPED (ADR-0068/0070). Recorded for\n"
            "# provenance, not for replay.\n"
            + "\n".join("# " + line for line in _yaml_dump(cfg).rstrip().splitlines())
            + "\n"
        )
    return text, True, ""


def _metrics_csv(rows: list[dict[str, Any]], names: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["step", "node", *names], lineterminator="\n",
                       restval="")  # absent at this step ⇒ EMPTY, never 0
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def _metrics_json(rows: list[dict[str, Any]], names: list[str],
                  scalars: dict[str, Any]) -> str:
    series: dict[str, list[list[Any]]] = {}
    for name in names:
        points = [[r["step"], r[name]] for r in rows if name in r]
        if points:
            series[name] = points
    return json.dumps({"series": series, "scalars": scalars, "steps": len(rows)},
                      indent=2, sort_keys=True, default=str) + "\n"


_MISSING_CARD = (
    "<!-- No model card. This run did not produce one: `model/card` was not in\n"
    "     the pipeline, or it ran and could not write. The card is additive\n"
    "     (#536) — its absence says nothing about the model. -->\n"
)


def _validate_run_id(run_id: str) -> str:
    """A run id becomes a filename (the download's name). Refuse a path."""
    rid = (run_id or "").strip()
    if not rid or "/" in rid or "\\" in rid or rid.startswith(".") or "\x00" in rid:
        raise ValueError(f"unsafe run id for a bundle filename: {run_id!r}")
    return rid


# ─── the bundle ───────────────────────────────────────────────────────────


def build_run_bundle(src: RunBundleSources) -> bytes:
    """Assemble ``src`` into zip bytes. Pure, total, deterministic."""
    run_id = _validate_run_id(src.run_id)

    config_text, config_ok, config_why = _config_yaml(src)
    rows, names = metrics_rows(src.events)
    scalars = end_scalars(src.events)
    lineage_present = bool(src.lineage)
    card_present = bool((src.model_card or "").strip())

    contents: dict[str, str] = {
        "config.yaml": config_text,
        "metrics.csv": _metrics_csv(rows, names),
        "metrics.json": _metrics_json(rows, names, scalars),
        "eval.json": json.dumps(src.eval_runs or [], indent=2, sort_keys=True,
                                default=str) + "\n",
        "lineage.json": json.dumps(src.lineage or {}, indent=2, sort_keys=True,
                                   default=str) + "\n",
        "MODEL_CARD.md": src.model_card if card_present else _MISSING_CARD,
    }

    manifest = {
        "run_id": run_id,
        "bundle_version": 1,
        "generated_by": "cairn GET /runs/{id}/bundle (#2032)",
        "meta": dict(src.meta or {}),
        "items": {
            "config": {"present": config_ok, "files": list(ITEM_FILES["config"]),
                       "why": config_why},
            "metrics": {
                "present": bool(rows or scalars), "files": list(ITEM_FILES["metrics"]),
                "why": "" if (rows or scalars) else
                       "the run emitted no `metrics`/`scalars` events — the trainer "
                       "callback that emits them is T6 (#2025)",
                "steps": len(rows), "series": names,
            },
            "eval": {"present": bool(src.eval_runs), "files": list(ITEM_FILES["eval"]),
                     "why": "" if src.eval_runs else "no eval recipe scored this run"},
            "lineage": {"present": lineage_present, "files": list(ITEM_FILES["lineage"]),
                        "why": "" if lineage_present else "no lineage record for this run"},
            "model_card": {"present": card_present, "files": list(ITEM_FILES["model_card"]),
                           "why": "" if card_present else
                                  "the run produced no MODEL_CARD.md (`model/card` "
                                  "was not in the pipeline, or could not write)"},
        },
    }
    contents[MANIFEST_NAME] = json.dumps(manifest, indent=2, sort_keys=True,
                                         default=str) + "\n"

    order = [f for item in BUNDLE_ITEMS for f in ITEM_FILES[item]] + [MANIFEST_NAME]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in order:
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, contents[name].encode("utf-8"))
    return buf.getvalue()


def bundle_filename(run_id: str) -> str:
    return f"{_validate_run_id(run_id)}-bundle.zip"


__all__ = [
    "BUNDLE_ITEMS", "ITEM_FILES", "MANIFEST_NAME", "RunBundleSources",
    "build_run_bundle", "bundle_filename", "effective_config",
    "effective_run_config", "end_scalars", "metrics_rows",
]
