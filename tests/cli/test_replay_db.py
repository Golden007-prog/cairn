"""DB-backed `cairn replay` (#76 slice 2).

The local-file replay path stays the default; this exercises `--source db`,
which renders a run's trace + full result from the configured run store —
for runs that only live in the DB (e.g. dispatched via the server).
"""
from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.cli.commands import replay
from cairn.core.engine.schema import TriggerEvent
import cairn.core.storage as storage_mod
from cairn.core.storage import Run, SqliteRunStore


def _seed(db: str) -> None:
    async def _go() -> None:
        # Fresh store per loop (sqlite conn is sync; avoids cross-loop reuse).
        s = SqliteRunStore(db)
        await s.create(
            Run(id="run-x", trigger=TriggerEvent(source="manual", matched_pattern="t", service="svc")),
        )
        await s.append_event("run-x", "skill_started", {"skill_name": "t"})
        await s.append_event("run-x", "done", {"matched_skills": 1})
        await s.update(
            "run-x", status="done",
            result_payloads=[{"skill_name": "t", "rca": {"summary": "root cause"}}],
        )
        s.close()
    asyncio.run(_go())


def test_db_replay_renders_run_events_and_result(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "runs.db")
    _seed(db)
    # _run_from_db builds the store via default_store(); point it at our file.
    monkeypatch.setattr(storage_mod, "default_store", lambda: SqliteRunStore(db))

    rc = replay._run("run-x", json=True, source="db")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["status"] == "done"
    assert [e["type"] for e in out["events"]] == ["skill_started", "done"]
    # slice-1 full result rides through DB replay
    assert out["result_payloads"][0]["rca"]["summary"] == "root cause"


def test_db_replay_missing_run_is_clean_error(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "empty.db")
    monkeypatch.setattr(storage_mod, "default_store", lambda: SqliteRunStore(db))
    rc = replay._run("nope", json=True, source="db")
    assert rc == 1
    err = capsys.readouterr().err
    assert "no such run in the DB store" in err
    assert "cairn remote status nope" in err  # #2536 — a server's run is read over the API


# ─── end-to-end via the real env-driven path (no default_store monkeypatch) ──
# The tests above patch default_store; these prove the WHOLE chain on sqlite:
# OBS_STORAGE=sqlite://<file> → default_store() → SqliteRunStore → replay. That
# env→store URL resolution is what was actually broken (#76) — patching around
# default_store hid it.


def _seed_via_env(db, run_id: str, *, with_result: bool) -> None:
    async def _go() -> None:
        from cairn.core.storage import default_store
        s = default_store()                          # resolves from OBS_STORAGE
        assert getattr(s, "path", None) == str(db)   # the URL mapped to our file
        await s.create(
            Run(id=run_id, trigger=TriggerEvent(source="manual", matched_pattern="t", service="svc")),
        )
        await s.append_event(run_id, "skill_started", {"skill_name": "t"})
        await s.append_event(run_id, "done", {"matched_skills": 1})
        fields = {"status": "done"}
        if with_result:
            fields["result_payloads"] = [{"skill_name": "t", "rca": {"summary": "root cause"}}]
        await s.update(run_id, **fields)
        s.close()
    asyncio.run(_go())


def test_db_replay_end_to_end_via_obs_storage_url(tmp_path, monkeypatch, capsys):
    db = tmp_path / "runs.db"
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{db}")   # sqlite:// + abs path
    monkeypatch.delenv("OBS_RUN_DB", raising=False)
    _seed_via_env(db, "run-e2e", with_result=True)

    rc = replay._run("run-e2e", json=True, source="db")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["status"] == "done"
    assert [e["type"] for e in out["events"]] == ["skill_started", "done"]
    assert out["result_payloads"][0]["rca"]["summary"] == "root cause"


def test_db_replay_auto_falls_back_to_db_store_via_env(tmp_path, monkeypatch, capsys):
    """`--source auto` with no on-disk trace falls back to the env-resolved DB
    store — the path a server-dispatched run takes."""
    db = tmp_path / "runs.db"
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{db}")
    monkeypatch.delenv("OBS_RUN_DB", raising=False)
    # force "no local files" deterministically so auto must reach the DB store
    monkeypatch.setattr(replay, "run_dir_exists", lambda rid: False)
    _seed_via_env(db, "run-auto", with_result=False)

    rc = replay._run("run-auto", json=False, source="auto")
    assert rc == 0
    assert "status:      done" in capsys.readouterr().out
