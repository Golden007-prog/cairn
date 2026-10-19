"""#2128 item A — `cairn run --series` and the LOCAL DEV LOOP are exclusive.

What this file used to pin (#993, ADR-0076 D5): the in-process `cairn run` path
checked the series budget before compiling, and on finishing persisted a `Run`
to the run store with its cost so `series compare` could read it.

That store write was the LAST producer of a run row no pipeline owns (#2128 §4),
and the chokepoint the invariant now lives at — `RunDispatcher.run_template` —
cannot reach it, because this path never calls the dispatcher: it compiles and
runs the flow in this process. So the row could only ever be an orphan.

The owner's decision recorded in #2128 A, option (b): a dev-loop run is a
debugger, not a governed run, so it gets no `Run` row at all — and `--series`,
which means "count this governed run's cost against that budget", is REFUSED
alongside `--interactive` / `--mock` rather than silently ignored. The series
behaviour is one flag away: drop the dev-loop flag and the same command starts
the flow's one-leg pipeline, whose leg reserves against the series inside
`run_template` (pinned by `packages/cairn-platform/tests/test_series_admission.py`).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-validate-offline")


def test_series_is_refused_for_a_dev_loop_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path}/runs.db")
    monkeypatch.setenv("OBS_RUNS_DIR", str(tmp_path / "traces"))

    from cairn.cli.commands.run import _run_template_sync

    def _boom(_path):  # the refusal must land BEFORE any compile
        raise AssertionError("compiled a run that should have been refused")

    monkeypatch.setattr("cairn.core.engine.compiler.compile_template", _boom)

    inp = tmp_path / "in.json"
    inp.write_text("{}")
    rc = _run_template_sync(
        "dataset-readiness/check", str(inp), environment="production",
        mock=True, series_id="ser-dev-loop",
    )
    assert rc == 2, f"--series with --mock must be refused (exit 2), got {rc}"
    err = capsys.readouterr().err
    assert "--series" in err and "not governed runs" in err

    from cairn.core.storage import default_store
    assert asyncio.run(default_store().list(workspace_id="default")) == []


def test_dev_loop_run_writes_no_run_row_and_keeps_its_cost_in_the_trace(
    tmp_path, monkeypatch,
):
    """The positive half of the decision: the dev loop still runs, still leaves a
    replayable trace, and leaves the run store untouched."""
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path}/runs.db")
    monkeypatch.setenv("OBS_RUNS_DIR", str(tmp_path / "traces"))

    async def _run(_compiled, input_data):
        assert "run_id" in input_data
        return {"summary": "ok"}

    monkeypatch.setattr("cairn.core.engine.skill_engine.resolve_template_path", lambda ref: "X")
    monkeypatch.setattr("cairn.core.engine.compiler.compile_template",
                        lambda path: SimpleNamespace(content_hash="h", spec=None))
    monkeypatch.setattr("cairn.core.engine.compiled.ast_dict", lambda spec: {})
    monkeypatch.setattr("cairn.core.engine.runner.run_within_budget", _run)
    monkeypatch.setattr("cairn.core.operator.load_all_operators", lambda **k: [])
    monkeypatch.setattr("cairn.core.pack.loader.load_all_packs", lambda **k: None)

    from cairn.cli.commands.run import _run_template_async

    inp = tmp_path / "in.json"
    inp.write_text("{}")
    run_id, rc = asyncio.run(_run_template_async(
        "dataset-readiness/check", inp, environment="production", mock=True,
    ))
    assert rc == 0

    from cairn.core.storage import default_store
    assert asyncio.run(default_store().list(workspace_id="default")) == [], (
        "a dev-loop run wrote a Run row — an orphan by construction (#2128 A)")

    meta = json.loads((Path(os.environ["OBS_RUNS_DIR"]) / run_id / "meta.json").read_text())
    assert meta["status"] == "done"
    assert "llm_cost_usd" in meta, "the dev loop's cost has to live in its own trace"
