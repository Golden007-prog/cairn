"""``cairn replay`` — render a past run's trace.

Two sources (``--source``, default ``auto``):
- the on-disk ``.cairn/runs/<run_id>/{meta.json, events.jsonl}`` trace that
  :mod:`cairn.cli.runs` writes (the original, primary path), and
- the configured **DB run store** (sqlite/postgres) — for runs that only live
  in the store, e.g. dispatched via the server (#76 DB-backed replay).
``auto`` reads the local files if present, else falls back to the store.
``--json`` dumps the raw event stream (+ the full structured result on the DB
path) for piping into other tools.

Read-only. Replay never mutates the trace — DB replay only reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
import sys
from typing import Optional

import typer

from cairn.cli.runs import read_events, read_meta, resolve_run_dir
from cairn.cli.runs.reader import run_dir_exists


# ─── rendering ──────────────────────────────────────────────────────


def _short_ts(iso: str) -> str:
    """Strip the date + tz for the per-line timestamp prefix.

    ``meta.json`` already carries the run's anchor time; per-event the user
    cares about elapsed wall-clock vs absolute date.
    """
    # ISO is "YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00"
    if "T" in iso:
        return iso.split("T", 1)[1].split("+", 1)[0][:12]
    return iso


def _summarize_event(type: str, data: dict) -> str:
    """One-line summary per event type. Unknown types fall through to a
    truncated JSON dump so future event kinds still render."""
    if type == "skill_started":
        return f"started skill={data.get('skill_name','?')}"
    if type == "findings":
        rc = (data.get("root_cause") or {}).get("description", "")
        n_ev = len(data.get("evidence") or [])
        return (f"findings skill={data.get('skill_name','?')} evidence={n_ev}"
                + (f" root_cause={rc!r}" if rc else ""))
    if type == "action_decision":
        return (f"action_decision id={data.get('candidate_id','?')} "
                f"decision={data.get('decision','?')}")
    if type == "action_outcome":
        return (f"action_outcome id={data.get('candidate_id','?')} "
                f"status={data.get('status','?')}")
    if type == "skill_completed":
        dur = data.get("duration_seconds")
        # §24.11.3b: include the template's content-addressed short-hash
        # so replay shows *which version* of the template ran. Helps with
        # "this run looks weird, what prompt did it use?" debugging.
        hash_str = ""
        h = data.get("template_hash") or ""
        if h.startswith("sha256:"):
            hash_str = f" template={data.get('template_name','?')}@{h[7:19]}"
        return (f"completed skill={data.get('skill_name','?')}"
                + hash_str
                + (f" duration={dur:.1f}s" if isinstance(dur, (int, float)) else ""))
    # Fallback for unknown types.
    blob = _json.dumps(data, default=str)
    if len(blob) > 80:
        blob = blob[:77] + "..."
    return f"{type} {blob}"


def _render_text(run_id: str, meta: dict, events: list) -> str:
    lines: list[str] = []
    lines.append(f"run {run_id}")
    lines.append(f"  status:      {meta.get('status','?')}")
    lines.append(f"  environment: {meta.get('environment','?')}")
    trig = meta.get("trigger") or {}
    lines.append(
        f"  trigger:     source={trig.get('source','?')} "
        f"service={trig.get('service','?')}"
    )
    if meta.get("matched_skills") is not None:
        lines.append(f"  matched:     {meta['matched_skills']} skill(s)")
    if meta.get("error"):
        lines.append(f"  error:       {meta['error']}")
    lines.append("")

    if not events:
        lines.append("(no events recorded)")
    else:
        lines.append(f"timeline ({len(events)} event(s)):")
        for ev in events:
            lines.append(f"  {_short_ts(ev.ts)}  {_summarize_event(ev.type, ev.data)}")
    return "\n".join(lines) + "\n"


# ─── shared impl ────────────────────────────────────────────────────


def _run_from_db(run_id: str, *, json: bool) -> int:
    """Render a run's trace from the configured DB run store (#76 DB replay).

    Additive to the local-file path: lets ``cairn replay`` reach runs that only
    live in the store (e.g. dispatched via the server) when ``CAIRN_STORAGE`` /
    ``CAIRN_RUN_DB`` points at sqlite/postgres. The in-memory store (nothing
    configured) has no durable trace to read.
    """
    # Deferred import: keep the storage/DB stack (asyncpg etc.) off the hot path
    # of every `cairn` invocation (Rule 4 — measured CLI-startup deferral).
    from cairn.core.storage import default_store

    store = default_store()

    async def _read():
        run = await store.get(run_id)
        if run is None:
            return None, []
        return run, await store.get_events(run_id)

    try:
        run, events = asyncio.run(_read())
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    if run is None:
        print(f"no such run in the DB store: {run_id}", file=sys.stderr)
        print("  (point CAIRN_STORAGE / CAIRN_RUN_DB at the store the run was "
              "written to)", file=sys.stderr)
        print(f"  a run on a server: cairn remote status {run_id}  ·  "
              f"cairn remote logs {run_id}", file=sys.stderr)
        return 1

    meta = {
        "status": run.status,
        "environment": "?",  # not recorded on the Run record (DB tier)
        "trigger": run.trigger.model_dump(mode="json") if run.trigger else {},
        "matched_skills": run.matched_skills,
        "error": run.error,
    }
    if json:
        payload = {
            "meta": meta,
            "events": [{"ts": e.ts, "type": e.type, "data": e.data} for e in events],
            "result_payloads": run.result_payloads,  # full structured result(s) (#76)
        }
        print(_json.dumps(payload, indent=2, default=str))
    else:
        print(_render_text(run_id, meta, events))
        if run.result_payloads:
            print(f"result: {len(run.result_payloads)} structured result(s) "
                  f"recorded (see --json or GET /runs/{run_id})")
    return 0


def _run(run_id: str, *, json: bool, source: str = "auto") -> int:
    """Dispatch to the file-based or DB-based replay.

    - ``auto`` (default): local trace files if present, else the DB store.
    - ``file``: only the on-disk ``.cairn/runs/<id>/`` trace (original).
    - ``db``: only the configured run store.
    """
    if source == "db":
        return _run_from_db(run_id, json=json)

    have_files = run_dir_exists(run_id)
    if source == "file" and not have_files:
        print(f"no such run: {run_id}", file=sys.stderr)
        print(f"  (looked in: {resolve_run_dir(run_id)})", file=sys.stderr)
        return 1
    if not have_files:  # source == "auto", no local trace → try the DB store
        return _run_from_db(run_id, json=json)

    meta = read_meta(run_id) or {}
    events = list(read_events(run_id))
    if json:
        payload = {
            "meta": meta,
            "events": [{"ts": e.ts, "type": e.type, "data": e.data} for e in events],
        }
        print(_json.dumps(payload, indent=2, default=str))
    else:
        print(_render_text(run_id, meta, events))
    return 0


# ─── entry points ───────────────────────────────────────────────────


def cmd(
    run_id: str = typer.Argument(..., help="Run id from a prior `cairn run`."),
    json: bool = typer.Option(
        False, "--json",
        help="Emit raw meta+events JSON instead of the human-readable timeline.",
    ),
    source: str = typer.Option(
        "auto", "--source",
        help="Trace source: auto (local files, else DB store) | file | db.",
    ),
) -> None:
    """Replay a past run by rendering its persisted event trace (local files or DB store)."""
    raise typer.Exit(code=_run(run_id, json=json, source=source))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cairn replay",
        description="Replay a past run by rendering its persisted event trace.",
    )
    parser.add_argument("run_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--source", choices=["auto", "file", "db"], default="auto")
    args = parser.parse_args(argv)
    return _run(args.run_id, json=args.json, source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())
