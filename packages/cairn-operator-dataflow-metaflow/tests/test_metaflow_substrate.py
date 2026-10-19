"""ADR-0089 slice 1 (#1345 + #1346) — Metaflow as substrate, not DAG.

Pins the two mechanisms:

  * ``build_with_args`` — the pure compute-spec → ``--with`` decospec mapping.
    Runtime attach (`run --with`) is what lets the substrate vary per
    environment while the bridge flow stays the ONE static CairnFlow (D1), so
    the exact argv is the contract worth pinning.
  * the selection seam — ``metaflow_metadata_root`` et al. have their own keys
    now; a bare ``<kind>_target`` (the pre-ADR-0089 overload) still works as
    the metadata root so existing deployments keep re-attaching.

No metaflow install needed: ``engines_metaflow`` imports metaflow only inside
the bridge, and ``resolve_engine``'s metaflow branch constructs the engine
without touching it.
"""
from __future__ import annotations

import pytest

from cairn.core.dataflow import DataflowSpec

from cairn.core.dataflow.engine_registry import resolve_engine
from operator_dataflow_metaflow.engines_metaflow import (
    MetaflowEngine,
    aggregate_resources,
    build_with_args,
)


def _spec(stages: list[dict]) -> DataflowSpec:
    return DataflowSpec.model_validate({"stages": stages})


PLAIN = _spec([{"stage": "ingest.normalize"}, {"stage": "dedup.exact"}])
SIZED = _spec([
    {"stage": "ingest.normalize", "resources": {"cpu": 2, "memory_mb": 4096}},
    {"stage": "train.qlora", "resources": {"cpu": 8, "memory_mb": 32000, "gpu": 1}},
    {"stage": "package.adapter", "resources": {"cpu": 4, "gpu": 0}},
])


# ── aggregate_resources ──────────────────────────────────────────────────────

def test_envelope_is_the_max_across_stages():
    """One execute step must fit its HEAVIEST stage (D1: no per-stage steps)."""
    assert aggregate_resources(SIZED) == {"cpu": 8, "memory": 32000, "gpu": 1}


def test_gpu_zero_is_explicitly_none_not_a_request():
    s = _spec([{"stage": "a", "resources": {"cpu": 1, "gpu": 0}}])
    assert aggregate_resources(s) == {"cpu": 1}


def test_no_hints_no_envelope():
    assert aggregate_resources(PLAIN) == {}


# ── build_with_args ──────────────────────────────────────────────────────────

def test_local_unsized_flow_adds_nothing():
    """The pre-ADR-0089 submission is byte-identical — regression guard for
    every existing metaflow deployment."""
    assert build_with_args(PLAIN, {"kind": "local"}, 0) == []


def test_resources_ride_even_on_local():
    assert build_with_args(SIZED, {"kind": "local"}, 0) == [
        "--with", "resources:cpu=8,gpu=1,memory=32000",
    ]


def test_kubernetes_gets_image_and_resources():
    args = build_with_args(SIZED, {"kind": "kubernetes", "image": "ghcr.io/x/cairn-flow:1"}, 0)
    assert args == [
        "--with", "resources:cpu=8,gpu=1,memory=32000",
        "--with", "kubernetes:image=ghcr.io/x/cairn-flow:1",
    ]


def test_batch_gets_image_and_queue():
    args = build_with_args(PLAIN, {"kind": "batch", "image": "img:1", "queue": "gpu-q"}, 2)
    assert args == [
        "--with", "batch:image=img:1,queue=gpu-q",
        "--with", "retry:times=2",
    ]


def test_retry_zero_means_no_retry_decorator():
    assert build_with_args(PLAIN, {"kind": "local"}, 0) == []
    assert build_with_args(PLAIN, {"kind": "local"}, 3) == ["--with", "retry:times=3"]


# ── validate() — substrate config fails at submit, not mid-run ───────────────

def _stub_registry(monkeypatch, names):
    import cairn.core.dataflow.stage_loader as stages_mod
    monkeypatch.setattr(stages_mod, "load_stages", lambda: {n: object() for n in names})


def test_remote_kind_without_image_is_a_named_problem(monkeypatch):
    _stub_registry(monkeypatch, ["ingest.normalize", "dedup.exact"])
    eng = MetaflowEngine(compute={"kind": "kubernetes"})
    assert any("metaflow_compute_image" in p for p in eng.validate(PLAIN))


def test_unknown_compute_kind_is_a_named_problem(monkeypatch):
    _stub_registry(monkeypatch, ["ingest.normalize", "dedup.exact"])
    eng = MetaflowEngine(compute={"kind": "argo"})
    assert any("not a compute kind" in p for p in eng.validate(PLAIN))


def test_unparseable_retry_is_a_named_problem(monkeypatch):
    _stub_registry(monkeypatch, ["ingest.normalize", "dedup.exact"])
    eng = MetaflowEngine(retry_times=-1)  # the resolve_engine sentinel for a typo'd profile value
    assert any("metaflow_retry_times" in p for p in eng.validate(PLAIN))


def test_defaults_add_no_substrate_problems(monkeypatch):
    _stub_registry(monkeypatch, ["ingest.normalize", "dedup.exact"])
    import operator_dataflow_metaflow.engines_metaflow as em
    monkeypatch.setattr(em, "_metaflow_available", lambda: True)
    assert MetaflowEngine().validate(PLAIN) == []


# ── resolve_engine — the identity/compute split (#1345) ──────────────────────

_MF_ENV = ["OBS_ML_ENGINE", "OBS_ML_TARGET", "OBS_METAFLOW_ROOT", "OBS_METAFLOW_DATASTORE",
           "OBS_METAFLOW_COMPUTE", "OBS_METAFLOW_IMAGE", "OBS_METAFLOW_QUEUE", "OBS_METAFLOW_RETRY"]


@pytest.fixture()
def mf_env(monkeypatch):
    for var in _MF_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OBS_ML_ENGINE", "metaflow")
    return monkeypatch


def test_selection_carries_identity_and_compute(mf_env):
    mf_env.setenv("OBS_METAFLOW_ROOT", "/srv/mf")
    mf_env.setenv("OBS_METAFLOW_COMPUTE", "kubernetes")
    mf_env.setenv("OBS_METAFLOW_IMAGE", "ghcr.io/x/img:1")
    mf_env.setenv("OBS_METAFLOW_RETRY", "2")
    eng = resolve_engine(None, kind="ml")
    assert isinstance(eng, MetaflowEngine)
    assert eng.metadata_root == "/srv/mf"
    assert eng._compute == {"kind": "kubernetes", "image": "ghcr.io/x/img:1", "queue": ""}
    assert eng._retry_times == 2


def test_bare_ml_target_still_works_as_metadata_root(mf_env):
    """Back-compat: pre-ADR-0089 profiles overloaded `<kind>_target` as the
    metadata root — they must keep re-attaching to their runs."""
    mf_env.setenv("OBS_ML_TARGET", "/legacy/root")
    eng = resolve_engine(None, kind="ml")
    assert eng.metadata_root == "/legacy/root"
    assert eng._compute["kind"] == "local"


def test_dedicated_root_wins_over_legacy_target(mf_env):
    mf_env.setenv("OBS_ML_TARGET", "/legacy/root")
    mf_env.setenv("OBS_METAFLOW_ROOT", "/srv/mf")
    assert resolve_engine(None, kind="ml").metadata_root == "/srv/mf"


def test_typoed_retry_becomes_the_validate_sentinel(mf_env):
    mf_env.setenv("OBS_METAFLOW_RETRY", "twice")
    eng = resolve_engine(None, kind="ml")
    assert eng._retry_times == -1  # dispatch survives; validate() names the problem at submit
