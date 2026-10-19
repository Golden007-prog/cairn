"""What a remote job hands back, and where it lands — core since #2583 (ADR-0129 D1).

Three stdlib-only helpers that are THE result contract of every remote
transport of ``cairn-dataflow-run`` (the SSH engine's ``remote/exec`` fetch,
SkyPilot's rsync — #2102): the workdir entries a completed job must return
(:func:`fetch_list`), the deterministic control-plane directory they land in
(:func:`fetch_dir`), and the rewrite of an on-box path in ``stats`` to the
local copy (:func:`rewrite_fetched`). They lived in the builtin dataflow pack's
SSH engine module until #2583; an adapter outside that pack imports them from
here, so a pipeline hoist and the next leg cannot tell which engine ran a leg.

No SSH library is imported here — the transport is the adapter's; this module
only names what crosses it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from cairn.core.dataflow.contract import DataflowSpec

# stage → the artifact file(s) it writes into the job workdir; fetched back on
# success so the control-side hoist reads LOCAL copies (the fix-remote shape).
# KIND-BLIND like the engines themselves: data kernels and ML kernels (ADR-0082
# D7, #1075) share one table — an SSH-dispatched ML flow must fetch its
# candidate manifest back or the control plane can't read what the box produced.
_STAGE_OUTPUTS = {
    "export.jsonl": lambda cfg: [str(cfg.get("name", "output.jsonl"))],
    "export.parquet": lambda cfg: [str(cfg.get("name", "output.parquet"))],
    "scan.all": lambda cfg: ["scan_findings.json"],
    "decontam.scan": lambda cfg: ["decontam_findings.json"],
    "split": lambda cfg: ["splits"],
    # kind: ml — the flow's chaining record + the packaged candidate manifest.
    "train.qlora": lambda cfg: ["train_result.json"],
    "package.adapter": lambda cfg: ["candidate.json"],
}


#: The two files EVERY dataflow job writes, whatever its stages (the
#: ``cairn-dataflow-run`` entrypoint writes ``result.json``; the runner writes
#: ``metrics.json`` progressively). Named once so a second transport cannot
#: invent a third spelling.
BOOKKEEPING_OUTPUTS = ("result.json", "metrics.json")


def fetch_list(spec: DataflowSpec) -> list[str]:
    """The workdir entries a completed job must hand back — THE result contract.

    Public on purpose (#2102): every remote transport fetches the SAME names, or
    the control-side hoist and the next pipeline leg see a different shape
    depending on which engine ran the leg. The builtin SSH engine and the
    SkyPilot engine both call this.
    """
    pats = list(BOOKKEEPING_OUTPUTS)
    for ref in spec.stages:
        for name in _STAGE_OUTPUTS.get(ref.stage, lambda c: [])(dict(ref.config)):
            if name not in pats:
                pats.append(name)
    return pats


def fetch_dir(workdir: str) -> Path:
    """Where a fetched workdir lands on the control plane.

    Deterministic — mirrors the remote exec operator's ``_poll`` fetch location
    exactly, so a restarted control plane finds the same directory instead of
    concluding the job produced nothing. Shared with the SkyPilot engine (#2102)
    for the same reason :func:`fetch_list` is: one location, one contract.
    """
    return Path(tempfile.gettempdir()) / f"cairn-remote-{workdir.rsplit('/', 1)[-1]}"


def rewrite_fetched(value: Any, workdir: str, fetched: Path) -> Optional[str]:
    """A remote workdir path → the local fetched copy, or None if it isn't one.

    The hoists read these paths control-side, so a stat that still names an
    on-box location is a file the control plane cannot open.
    """
    if not (isinstance(value, str) and workdir):
        return None
    if value.startswith(workdir):
        rel = os.path.relpath(value, workdir)
    else:
        # #1075: the handle records the workdir RELATIVE (job_id is
        # `host:.cairn-jobs/<run>`) while a kernel writes an ABSOLUTE
        # on-box path (`/home/ubuntu/.cairn-jobs/<run>/candidate.json`),
        # so a plain startswith silently no-ops and the control plane is
        # handed a path that exists only on the box. Anchor on the
        # workdir segment instead.
        # lstrip("/") ONLY — the workdir's own leading dot is part of the
        # directory name (`.cairn-jobs`), so stripping "." breaks the match.
        marker = "/" + workdir.lstrip("/") + "/"
        idx = value.find(marker)
        if idx == -1:
            return None
        rel = value[idx + len(marker):]
    candidate = fetched / rel
    return str(candidate) if candidate.exists() else None
