"""Human labels over judge-graded rows (#1656) — the ground truth a scorecard needs.

`closedbook_answers.jsonl` already carries what the JUDGE said about each row.
This store carries what a HUMAN said about the same row, so
:func:`cairn_platform.judge_scorecard.score_judge` can compare them.

Deliberately NOT a `ReviewRecord`. A review's approve path IS the pipeline gate
release (``/pipelines/{id}/steps/{id}/approve``) — making a label batch a review
would mean "approving" labels released gates. The Decisions inbox is the right
PLACE for a human to do this work; it is not the right RECORD.

Workspace-scoped from the first commit rather than retrofitted: reviews shipped
without it and leaked every gated decision across tenants until #1639.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import json
import os
import sqlite3
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cairn.core._time import utc_now_iso
from cairn.core.storage.migrations import add_missing_columns
from cairn.core.workspace import DEFAULT_WORKSPACE


class JudgeLabel(BaseModel):
    """One human verdict on one graded row."""

    #: Deterministic: re-labelling the same row REPLACES rather than double-counts.
    label_id: str = ""
    workspace_id: str = DEFAULT_WORKSPACE
    #: The judge whose grade was on screen when the human ruled — provenance,
    #: NOT identity. Scoring a different judge over these rows means supplying
    #: that judge's labels, not re-collecting the human's.
    judge_name: str
    judge_version: int = 0
    prompt_hash: str = ""
    #: WHERE the labelled row came from. A registered dataset version is the
    #: durable coordinate: it is hash-verified and reproducible, so "row 42 of
    #: dataset:facts-eval@3" still resolves long after the measure run's answers
    #: file has been collected. Manually uploaded data is the same thing once it
    #: has been through `/datasets/import/stage` — both sources converge on a
    #: registered version, so there is one identity scheme, not two.
    dataset_ref: str = ""          # "name@version"
    split: str = ""                # "eval" | "test" | … — the post-processing split
    #: The measure run whose judge grades were on screen. Provenance when a
    #: dataset coordinate exists; identity only when one does not.
    run_id: str = ""
    row_id: str = ""
    #: What the judge said at labelling time. Kept so a scorecard for THAT judge
    #: needs no access to the measure run's answers file — which may live on a
    #: box, or be gone.
    judge_label: str = ""
    #: The human's verdict on the row. The durable half.
    human_label: str = ""
    #: Why, in the labeller's words — the part a confusion matrix cannot hold.
    note: str = ""
    reviewer: str = ""
    created_at: str = Field(default_factory=utc_now_iso)

    def key(self) -> str:
        """Identity is the ROW, not the judge and not the run.

        A human's verdict is a property of the question, the gold answer and the
        model's answer — none of which change when a rubric is edited. Keying it
        by judge version (as this first did) threw away every human label on a
        prompt edit, i.e. exactly when you most want to compare v3 with v4 over
        the same rows. You re-run the JUDGE, not the human.

        A registered dataset version is the durable coordinate and wins when
        present. Falling back to the run is not a second scheme so much as the
        degraded case: labels taken straight off a measure run are still useful,
        they just stop resolving when that run's artifacts go.
        """
        if self.label_id:
            return self.label_id
        ws = self.workspace_id or DEFAULT_WORKSPACE
        if self.dataset_ref:
            return f"{ws}:{self.dataset_ref}:{self.split}:{self.row_id}"
        return f"{ws}:run:{self.run_id}:{self.row_id}"


@runtime_checkable
class JudgeLabelStore(Protocol):
    async def ensure_schema(self) -> None: ...
    async def put_label(self, label: JudgeLabel) -> JudgeLabel: ...
    async def list_labels(
        self, *, judge_name: Optional[str] = None, run_id: Optional[str] = None,
        dataset_ref: Optional[str] = None, workspace_id: Optional[str] = None,
        limit: int = 1000,
    ) -> list[JudgeLabel]: ...
    async def close(self) -> None: ...


def _matches(row: JudgeLabel, judge_name, run_id, workspace_id, dataset_ref=None) -> bool:
    if workspace_id is not None and (row.workspace_id or DEFAULT_WORKSPACE) != workspace_id:
        return False
    if judge_name is not None and row.judge_name != judge_name:
        return False
    if dataset_ref is not None and row.dataset_ref != dataset_ref:
        return False
    return not (run_id is not None and row.run_id != run_id)


class _NullJudgeLabelStore:
    """Unconfigured storage: reads empty, writes are dropped. A scorecard then
    reports 'not measured' — which is true — rather than inventing agreement."""

    async def ensure_schema(self) -> None: return None
    async def put_label(self, label): return label
    async def list_labels(self, **_): return []
    async def close(self) -> None: return None


class InMemoryJudgeLabelStore:
    def __init__(self) -> None:
        self._rows: dict[str, JudgeLabel] = {}
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None: return None

    async def put_label(self, label: JudgeLabel) -> JudgeLabel:
        stored = label.model_copy(update={"label_id": label.key()})
        async with self._lock:
            self._rows[stored.key()] = stored
        return stored

    async def list_labels(self, *, judge_name=None, run_id=None, dataset_ref=None,
                          workspace_id=None, limit=1000):
        async with self._lock:
            rows = [r for r in self._rows.values()
                    if _matches(r, judge_name, run_id, workspace_id, dataset_ref)]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return [r.model_copy(deep=True) for r in rows[:limit]]

    async def close(self) -> None: return None


_CREATE_SQLITE = """
    CREATE TABLE IF NOT EXISTS judge_labels (
        label_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL DEFAULT 'default',
        judge_name TEXT NOT NULL, run_id TEXT, dataset_ref TEXT,
        created_at TEXT NOT NULL, data TEXT NOT NULL
    )
"""


class SqliteJudgeLabelStore:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_SQLITE)
        add_missing_columns(self._conn, "judge_labels", {
            "workspace_id": "TEXT NOT NULL DEFAULT 'default'",
            "dataset_ref": "TEXT",
        })
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_judge_labels_ws ON judge_labels(workspace_id, judge_name)")
        self._conn.commit()
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None: return None

    def _put_sync(self, r: JudgeLabel) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO judge_labels (label_id, workspace_id, judge_name, run_id, "
            "dataset_ref, created_at, data) VALUES (?,?,?,?,?,?,?)",
            (r.key(), r.workspace_id or DEFAULT_WORKSPACE, r.judge_name, r.run_id,
             r.dataset_ref, r.created_at, r.model_dump_json()))
        self._conn.commit()

    async def put_label(self, label: JudgeLabel) -> JudgeLabel:
        stored = label.model_copy(update={"label_id": label.key()})
        async with self._lock:
            await asyncio.to_thread(self._put_sync, stored)
        return stored

    async def list_labels(self, *, judge_name=None, run_id=None, dataset_ref=None,
                          workspace_id=None, limit=1000):
        def go() -> list[JudgeLabel]:
            clauses, args = [], []
            if workspace_id is not None:
                clauses.append("workspace_id=?"); args.append(workspace_id)
            if judge_name is not None:
                clauses.append("judge_name=?"); args.append(judge_name)
            if run_id is not None:
                clauses.append("run_id=?"); args.append(run_id)
            if dataset_ref is not None:
                clauses.append("dataset_ref=?"); args.append(dataset_ref)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            args.append(limit)
            rows = self._conn.execute(
                f"SELECT data FROM judge_labels{where} ORDER BY created_at DESC LIMIT ?", args).fetchall()
            return [JudgeLabel.model_validate_json(r[0]) for r in rows]
        async with self._lock:
            return await asyncio.to_thread(go)

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()


def build_judge_label_store(dsn: Optional[str] = None) -> JudgeLabelStore:
    """Backend by ``dsn`` (falls back to ``CAIRN_STORAGE``), mirroring the review
    store. Postgres deployments share the SQLite path today — labels are small
    and low-traffic, and a second backend is not worth its migration until the
    volume asks for one."""
    url = (dsn or getenv("CAIRN_STORAGE", "")).strip()
    if not url:
        return _NullJudgeLabelStore()
    if url.startswith("sqlite://"):
        from cairn.core import settings  # noqa: PLC0415 - cycle: settings imports storage on some paths
        return SqliteJudgeLabelStore(path=settings.sqlite_path_from_url(url) or ":memory:")
    if url.startswith(("postgres://", "postgresql://")):
        return SqliteJudgeLabelStore(path=str(_pg_sidecar_path()))
    return _NullJudgeLabelStore()


def _pg_sidecar_path():
    from pathlib import Path  # noqa: PLC0415 - stdlib, kept local to the fallback
    return Path.home() / ".cairn" / "judge_labels.db"


def labelled_rows(answers: list[dict[str, Any]], labels: list[JudgeLabel]) -> list[dict[str, Any]]:
    """Join a measure run's graded rows with the human labels recorded for them.

    Returns the rows the scorecard consumes: the judge's own output, plus
    ``human_label`` where a human has ruled. Rows nobody labelled come back
    untouched, and `score_judge` correctly treats them as no evidence.
    """
    by_row = {lb.row_id: lb for lb in labels if lb.row_id}
    out = []
    for i, row in enumerate(answers):
        rid = str(row.get("row_id") or row.get("id") or i)
        merged = {**row, "row_id": rid}
        hit = by_row.get(rid)
        if hit:
            merged["human_label"] = hit.human_label
            if hit.note:
                merged["human_note"] = hit.note
        out.append(merged)
    return out


__all__ = [
    "InMemoryJudgeLabelStore", "JudgeLabel", "JudgeLabelStore", "SqliteJudgeLabelStore",
    "build_judge_label_store", "labelled_rows",
]
