"""#1349 (ADR-0089) — what makes a Metaflow run outlive ONE box.

The engine's stateless-re-attach property (poll/`resume` by pathspec) held only
on a single machine: artifacts went to local files, and Metaflow's local
metadata provider is CWD-scoped, so a run submitted on box A could not be
resolved from box B. Two axes fix that, and BOTH are needed — a shared S3
datastore for artifacts, and a metadata service for the records.

Scope honesty: real cross-box resume needs an S3 bucket + a running metadata
service, which this environment does not have. These tests pin the CONTRACT
(the exact METAFLOW_* env the engine emits, and the misconfigurations it
refuses); the live proof is gated on that infra, exactly like remote compute.
"""
from __future__ import annotations

import pytest

from cairn.core.dataflow import DataflowSpec

from cairn.core.dataflow.engine_registry import resolve_engine
from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine

SPEC = DataflowSpec.model_validate({"stages": [{"stage": "ingest.normalize"}]})

S3 = dict(datastore="s3", datastore_root="s3://bucket/cairn",
          metadata_service="http://mf-service:8080")


# ── the durability contract ──────────────────────────────────────────────────

def test_local_default_emits_no_durability_env():
    """The single-box default must stay byte-identical — regression guard."""
    assert MetaflowEngine().durability_env() == {}


def test_s3_datastore_carries_its_sysroot():
    env = MetaflowEngine(datastore="s3", datastore_root="s3://bucket/cairn").durability_env()
    assert env["METAFLOW_DEFAULT_DATASTORE"] == "s3"
    assert env["METAFLOW_DATASTORE_SYSROOT_S3"] == "s3://bucket/cairn"


def test_metadata_service_switches_the_provider_off_local():
    """Local metadata is CWD-scoped — that box affinity is the thing a service
    removes, so selecting a URL must also flip the provider."""
    env = MetaflowEngine(metadata_service="http://mf-service:8080").durability_env()
    assert env["METAFLOW_DEFAULT_METADATA"] == "service"
    assert env["METAFLOW_SERVICE_URL"] == "http://mf-service:8080"


def test_full_cross_box_config_emits_all_four():
    assert MetaflowEngine(**S3).durability_env() == {
        "METAFLOW_DEFAULT_DATASTORE": "s3",
        "METAFLOW_DATASTORE_SYSROOT_S3": "s3://bucket/cairn",
        "METAFLOW_DEFAULT_METADATA": "service",
        "METAFLOW_SERVICE_URL": "http://mf-service:8080",
    }


# ── is_cross_box: both axes, not one ─────────────────────────────────────────

def test_cross_box_needs_both_axes():
    assert MetaflowEngine(**S3).is_cross_box is True
    # shared artifacts but box-scoped records → a pathspec still won't resolve
    assert MetaflowEngine(datastore="s3", datastore_root="s3://b/c").is_cross_box is False
    # portable records but local artifacts → resume has nothing to read
    assert MetaflowEngine(metadata_service="http://s:8080").is_cross_box is False
    assert MetaflowEngine().is_cross_box is False


# ── refuse the silently-broken combinations ──────────────────────────────────

def _stub(monkeypatch):
    import cairn.core.dataflow.stage_loader as stages_mod
    monkeypatch.setattr(stages_mod, "load_stages", lambda: {"ingest.normalize": object()})
    import operator_dataflow_metaflow.engines_metaflow as em
    monkeypatch.setattr(em, "_metaflow_available", lambda: True)
    # the backend CLIENT check (#1346) is about the deployment, not this suite:
    # a dev box has no `kubernetes` package and these tests assert other rules.
    monkeypatch.setattr(em, "_lib_available", lambda module: True)


def test_s3_without_a_sysroot_is_refused(monkeypatch):
    """Otherwise the datastore has nowhere shared to write and the failure only
    surfaces when artifacts turn out to be unreadable."""
    _stub(monkeypatch)
    problems = MetaflowEngine(datastore="s3").validate(SPEC)
    assert any("metaflow_datastore_root" in p for p in problems)


@pytest.mark.parametrize("kind", ["kubernetes", "batch"])
def test_remote_compute_with_a_local_datastore_is_refused(monkeypatch, kind):
    """A remote task cannot reach the control plane's filesystem — a local
    datastore loses every artifact the flow produces."""
    _stub(monkeypatch)
    eng = MetaflowEngine(compute={"kind": kind, "image": "i:1"})
    assert any("needs metaflow_datastore=s3" in p for p in eng.validate(SPEC))


def test_remote_compute_with_s3_passes(monkeypatch):
    _stub(monkeypatch)
    eng = MetaflowEngine(compute={"kind": "kubernetes", "image": "i:1"}, **S3)
    assert eng.validate(SPEC) == []


def test_local_compute_with_local_datastore_still_passes(monkeypatch):
    """The zero-config path must not acquire new requirements."""
    _stub(monkeypatch)
    assert MetaflowEngine().validate(SPEC) == []


# ── selection wiring ─────────────────────────────────────────────────────────

def test_selection_carries_datastore_root_and_service(monkeypatch):
    for var in ("OBS_ML_TARGET", "OBS_METAFLOW_ROOT", "OBS_METAFLOW_COMPUTE",
                "OBS_METAFLOW_IMAGE", "OBS_METAFLOW_QUEUE", "OBS_METAFLOW_RETRY",
                "OBS_METAFLOW_SECRETS", "OBS_METAFLOW_ISOLATION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OBS_ML_ENGINE", "metaflow")
    monkeypatch.setenv("OBS_METAFLOW_DATASTORE", "s3")
    monkeypatch.setenv("OBS_METAFLOW_DATASTORE_ROOT", "s3://bucket/cairn")
    monkeypatch.setenv("OBS_METAFLOW_SERVICE_URL", "http://mf-service:8080")
    eng = resolve_engine(None, kind="ml")
    assert eng.is_cross_box is True
    assert eng.durability_env()["METAFLOW_DATASTORE_SYSROOT_S3"] == "s3://bucket/cairn"
