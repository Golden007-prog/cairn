"""#1687 — task legs resolve `dataset:` refs like flow legs do.

Launch-time dataset-ref resolution (#1466, list-aware since #1625) was wired
into ``execute_leg`` — the FLOW-leg path — only. A ``task:`` leg's inputs went
straight from ``resolve_step_inputs`` to the operator, so a governed
``dataset:<name>@<v>`` ref reached it as a literal string and ``load_rows``
treated it as a filename. That broke ``dataset/replay``'s whole design premise:
`sources` as a list of prior-cycle governed VERSIONS worked on flow legs and
silently didn't on the leg kind the flagship actually uses.

Pinned here through the REAL bridge with the REAL resolver (an in-memory
artifact store carrying registered versions), per the issue's regression ask:

* a task leg with ``sources: ["dataset:x@1"]`` receives the resolved
  ``uri#sha256=`` form — the SAME rewriting the flow path does, from the same
  functions (no second copy to drift, #1402);
* an unknown name fails the LEG at submit with the resolver's cited reason —
  never the operator mid-read.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel, ConfigDict

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.engine.pipeline import parse_pipeline_def, plan_pipeline
from cairn.core.operator import Operation, register_operator
from cairn.core.storage import build_storage
from cairn.core.storage.artifacts import ArtifactDescriptor, SqliteArtifactStore
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from tests.core.test_trigger_server_dispatch import FakeRunner


class _P(BaseModel):
    model_config = ConfigDict(extra="allow")


@register_operator(name="_t1687/echo", version="0.1.0")
class _Echo(Operation[_P, _P]):
    """Returns its inputs — the test reads what actually REACHED the operator."""

    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        return _P(**inp.model_dump())


@pytest.fixture(autouse=True)
def _op_registered():
    from cairn.core.operator.registry import default_registry
    try:
        default_registry.register(_Echo, strict=False)
    except Exception:  # noqa: BLE001 — already registered is fine
        pass


_YAML = """
name: t1687
kind: pipeline
inputs:
  sources: {type: list}
  dataset_uri: {type: str}
steps:
  - id: replay
    task: _t1687/echo
    with:
      sources: $input.sources
      dataset_uri: $input.dataset_uri
      ratio: 0.2
"""


@pytest.fixture()
async def registry(monkeypatch):
    """An artifact store with two governed versions of `facts-train`, patched in
    as THE store the bridge's resolver builds."""
    import cairn.core.storage.artifacts as A

    s = SqliteArtifactStore(":memory:")
    for h, uri in (("hash-v1", "s3://b/facts-train/one.jsonl"),
                   ("hash-v2", "s3://b/facts-train/two.jsonl")):
        await s.register(ArtifactDescriptor(kind="dataset", uri=uri, content_hash=h),
                         producer_run_id="run-x")
    await s.register_dataset_version("facts-train", "hash-v1")
    await s.register_dataset_version("facts-train", "hash-v2")
    monkeypatch.setattr(A, "build_artifact_store", lambda: s)
    yield s
    await s.close()


async def _run(inputs: dict):
    d = parse_pipeline_def(_YAML, pack="t1687", source="<test>")
    dispatcher = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(dispatcher, store=InMemoryPipelineStore())
    await bridge.start()
    try:
        rec = await bridge.start_pipeline(plan_pipeline(d), inputs=inputs)
        deadline = asyncio.get_event_loop().time() + 8.0
        while True:
            rec = await bridge.store.get(rec.pipeline_id)
            if rec.status in ("done", "failed"):
                break
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError(f"stuck: {rec.status} "
                                     + str({k: v.status for k, v in rec.step_state.items()}))
            await asyncio.sleep(0.02)
        run = await dispatcher.store.get(rec.step_state["replay"].run_id)
        return rec, run
    finally:
        bridge.stop()


async def test_task_leg_receives_resolved_uris_not_literal_refs(registry):
    """THE regression. Before #1687 the operator received the strings
    'dataset:facts-train@1' verbatim and treated them as filenames."""
    rec, run = await _run({
        "sources": ["dataset:facts-train@1", "s3://plain/uri.jsonl", "dataset:facts-train"],
        "dataset_uri": "dataset:facts-train@2",
    })
    assert rec.status == "done", rec.trigger.get("failure_reason")
    out = run.result_payloads[0]["output"]
    assert out["sources"] == [
        "s3://b/facts-train/one.jsonl#sha256=hash-v1",   # pinned version
        "s3://plain/uri.jsonl",                          # non-refs pass through
        "s3://b/facts-train/two.jsonl#sha256=hash-v2",   # @latest → head
    ]
    assert out["dataset_uri"] == "s3://b/facts-train/two.jsonl#sha256=hash-v2"
    assert out["ratio"] == 0.2                           # untouched values ride along
    # provenance: the run RECORD keeps what was declared, the op got the resolution
    assert run.inputs["dataset_uri"] == "dataset:facts-train@2"


async def test_unknown_ref_fails_the_leg_with_the_cited_reason(registry):
    """Fail-closed at submit: the operator never runs against a typo'd name —
    the same behavior the flow path has had since #1466."""
    rec, run = await _run({"sources": ["dataset:nope@1"], "dataset_uri": "s3://x.jsonl"})
    assert rec.status == "failed"
    assert rec.step_state["replay"].status == "failed"
    # the cited reason lives on the leg's RUN record (the pipeline-level
    # failure_reason is the generic "step failed" for every leg kind)
    assert "dataset:nope@1" in str(run.error), f"run must cite the ref: {run.error!r}"
    assert run.result_payloads == [], "the operator must never have produced output"


async def test_a_task_leg_with_no_refs_never_touches_the_store(monkeypatch):
    """The gate is the resolver's own predicate — plain inputs must not pay a
    store construction (and must work with NO store configured at all)."""
    import cairn.core.storage.artifacts as A

    def _boom():
        raise AssertionError("store built for a leg with no dataset refs")

    monkeypatch.setattr(A, "build_artifact_store", _boom)
    rec, run = await _run({"sources": ["s3://plain/a.jsonl"], "dataset_uri": "s3://b.jsonl"})
    assert rec.status == "done"
    assert run.result_payloads[0]["output"]["sources"] == ["s3://plain/a.jsonl"]
