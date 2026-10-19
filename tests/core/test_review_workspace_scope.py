"""#1639 — a gated decision belongs to exactly one workspace.

Before this, `reviews` was a single global table with no workspace column: the
console keyed its query on the active workspace, so the CACHE was scoped while
the SERVER was not, and switching workspace refetched the same global rows.

These tests run against every persistent tier the store ships, because the leak
lived in the SQL, not in the model.
"""
from __future__ import annotations

import pytest

from cairn.core.storage.reviews import (
    InMemoryReviewStore,
    ReviewRecord,
    SqliteReviewStore,
    pipeline_review_id,
)


def _rec(ws: str, pid: str, step: str = "gate", status: str = "open") -> ReviewRecord:
    return ReviewRecord(
        review_id=pipeline_review_id(pid, step),
        subject_ref=f"{pid}/{step}",
        title=f"Approve {step}",
        pipeline_id=pid, step_id=step,
        gate_snapshot={"secret": f"{ws}-only"},
        workspace_id=ws, status=status,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryReviewStore()
    return SqliteReviewStore(path=str(tmp_path / "reviews.db"))


@pytest.mark.asyncio
async def test_list_returns_only_the_named_workspace(store):
    await store.open_review(_rec("acme", "pipe-a"))
    await store.open_review(_rec("globex", "pipe-b"))

    acme = await store.list_reviews(workspace_id="acme")
    assert [r.pipeline_id for r in acme] == ["pipe-a"]
    assert acme[0].gate_snapshot["secret"] == "acme-only"

    globex = await store.list_reviews(workspace_id="globex")
    assert [r.pipeline_id for r in globex] == ["pipe-b"]


@pytest.mark.asyncio
async def test_status_and_workspace_filter_compose(store):
    await store.open_review(_rec("acme", "pipe-a"))
    await store.open_review(_rec("acme", "pipe-closed", status="approved"))
    await store.open_review(_rec("globex", "pipe-b"))

    got = await store.list_reviews(status="open", workspace_id="acme")
    assert [r.pipeline_id for r in got] == ["pipe-a"]


@pytest.mark.asyncio
async def test_get_by_id_from_another_workspace_reads_as_absent(store):
    """404-not-403: the id must not confirm its own existence (ADR-0051 §4)."""
    other = await store.open_review(_rec("globex", "pipe-b"))
    assert await store.get_review(other.review_id, workspace_id="acme") is None
    assert await store.get_review(other.review_id, workspace_id="globex") is not None


@pytest.mark.asyncio
async def test_unscoped_read_still_spans_workspaces(store):
    """`workspace_id=None` stays the internal cross-tenant read — the HTTP
    surface never passes it, but callers like retention sweeps rely on it."""
    await store.open_review(_rec("acme", "pipe-a"))
    await store.open_review(_rec("globex", "pipe-b"))
    assert len(await store.list_reviews()) == 2


@pytest.mark.asyncio
async def test_rows_written_before_the_column_existed_read_as_default(tmp_path):
    """The pre-#1639 schema had no workspace_id. `CREATE TABLE IF NOT EXISTS` is
    a no-op on such a DB, so without the migration the next query raises
    "no such column" — and the rows must land in `default`, where they were
    written, not vanish."""
    import json
    import sqlite3

    path = str(tmp_path / "legacy.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE reviews (review_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
        "subject_kind TEXT NOT NULL, pipeline_id TEXT, created_at TEXT NOT NULL, data TEXT NOT NULL)")
    legacy = _rec("default", "pipe-old").model_dump(mode="json")
    legacy.pop("workspace_id")                      # the field did not exist yet
    old.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)",
                ("rev:pipe-old:gate", "open", "pipeline_step", "pipe-old",
                 legacy["created_at"], json.dumps(legacy)))
    old.commit(); old.close()

    store = SqliteReviewStore(path=path)
    assert [r.pipeline_id for r in await store.list_reviews(workspace_id="default")] == ["pipe-old"]
    assert await store.list_reviews(workspace_id="acme") == []


# ─── through the HTTP surface ────────────────────────────────────────────────
# The store tests above pin the SQL. These drive the actual route, because the
# leak was only visible end to end: the console keys its query on the active
# workspace, so a scoped cache over an unscoped server looks correct until you
# switch workspaces and see the other tenant's rows.

import types  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402


class _FakeRunner:
    def __init__(self) -> None:
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(list_skills=lambda: [])

    async def run(self, *a, **k):
        return []


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)   # keyless — workspace only
    monkeypatch.setenv("OBS_EVAL_LEDGER_DB", ":memory:")
    db = str(tmp_path / "reg.db")
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{db}")
    seed = SqliteReviewStore(db)
    await seed.open_review(_rec("acme", "pipe-a"))
    await seed.open_review(_rec("globex", "pipe-b"))
    await seed.close()
    from cairn.core.triggers.server import build_app
    app = build_app(_FakeRunner())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_api_list_shows_only_the_active_workspace(client):
    acme = (await client.get("/reviews", headers={"X-Cairn-Workspace": "acme"})).json()["reviews"]
    assert [r["pipeline_id"] for r in acme] == ["pipe-a"]
    assert all(r["gate_snapshot"]["secret"] == "acme-only" for r in acme)

    globex = (await client.get("/reviews", headers={"X-Cairn-Workspace": "globex"})).json()["reviews"]
    assert [r["pipeline_id"] for r in globex] == ["pipe-b"]


async def test_api_get_across_workspaces_is_404_not_403(client):
    rid = pipeline_review_id("pipe-b", "gate")            # globex's review
    r = await client.get(f"/reviews/{rid}", headers={"X-Cairn-Workspace": "acme"})
    assert r.status_code == 404, "an id from another workspace must not confirm it exists"
    ok = await client.get(f"/reviews/{rid}", headers={"X-Cairn-Workspace": "globex"})
    assert ok.status_code == 200
