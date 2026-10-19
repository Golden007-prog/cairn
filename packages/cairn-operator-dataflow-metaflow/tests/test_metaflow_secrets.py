"""#1344 (ADR-0089) — what environment a Metaflow task actually receives.

Before this, `submit` did ``env = dict(os.environ)``: every credential the
control plane happened to hold went to the flow, while a credential stored in
the connection catalog did NOT (nothing resolved it). Both halves are wrong for
a governance product — it over-shares and under-provisions at the same time.

The default stays permissive for LOCAL compute on purpose: flipping it would
break every deployment relying on ambient AWS/HF/MLflow vars. Remote compute is
new in #1346, has no back-compat to preserve, and defaults to strict.
"""
from __future__ import annotations

import asyncio

import pytest

from cairn.core.dataflow import DataflowSpec

from cairn.core.dataflow.engine_registry import resolve_engine
from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine, _connection_env

SPEC = DataflowSpec.model_validate({"stages": [{"stage": "ingest.normalize"}]})


class _Resolved:
    def __init__(self, name, config=None, secrets=None):
        self.name, self.config, self.secrets = name, config or {}, secrets or {}


@pytest.fixture()
def catalog(monkeypatch):
    """Stub the connection catalog `resolve_connection` seam."""
    import cairn.core.connections as conns

    async def _resolve(name, **kw):
        if name == "prod-s3":
            return _Resolved("prod-s3", {"region": "eu-central-1"},
                             {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh"})
        return None

    monkeypatch.setattr(conns, "resolve_connection", _resolve)
    return monkeypatch


# ── isolation posture ────────────────────────────────────────────────────────

def test_local_inherits_by_default_so_existing_deployments_keep_working():
    """Back-compat: ambient AWS/HF/MLflow vars still reach a local flow."""
    eng = MetaflowEngine()
    assert eng._strict() is False


@pytest.mark.parametrize("kind", ["kubernetes", "batch"])
def test_remote_is_strict_by_default(kind):
    """Shipping every control-plane secret into a container is not a default."""
    assert MetaflowEngine(compute={"kind": kind, "image": "i:1"})._strict() is True


def test_isolation_can_be_pinned_either_way():
    assert MetaflowEngine(env_isolation="strict")._strict() is True
    assert MetaflowEngine(compute={"kind": "kubernetes", "image": "i:1"},
                          env_isolation="inherit")._strict() is False


# ── what strict actually withholds / keeps ───────────────────────────────────

def test_strict_withholds_ambient_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-travel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-should-not-travel")
    env = asyncio.run(MetaflowEngine(env_isolation="strict")._build_env())
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_a_cairn_namespaced_ambient_var_does_not_travel(monkeypatch):
    """`CAIRN_` was a blanket allowlist prefix — a wildcard hole in the middle
    of an allowlist, admitting every cairn-namespaced value the control plane
    happened to hold. The engine needs six SPECIFIC vars; found by the
    ADR-0089 acceptance run."""
    monkeypatch.setenv("CAIRN_SOME_TOKEN", "must-not-travel")
    env = asyncio.run(MetaflowEngine(env_isolation="strict")._build_env())
    assert "CAIRN_SOME_TOKEN" not in env


def test_the_enumerated_spec_contract_still_travels(monkeypatch):
    """Narrowing must not strand the vars the runner genuinely reads."""
    monkeypatch.setenv("CAIRN_DATAFLOW_SPEC", "{}")
    monkeypatch.setenv("CAIRN_PARENT_RUN", "run-1")
    monkeypatch.setenv("CAIRN_CHECKPOINT_DIR", "/ckpt")
    env = asyncio.run(MetaflowEngine(env_isolation="strict")._build_env())
    assert env["CAIRN_DATAFLOW_SPEC"] == "{}"
    assert env["CAIRN_PARENT_RUN"] == "run-1"
    assert env["CAIRN_CHECKPOINT_DIR"] == "/ckpt"


def test_strict_keeps_what_a_subprocess_cannot_run_without(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("METAFLOW_DEFAULT_DATASTORE", "s3")
    monkeypatch.setenv("CAIRN_SUBMIT_CWD", "/work")
    env = asyncio.run(MetaflowEngine(env_isolation="strict")._build_env())
    assert env["PATH"] == "/usr/bin"
    # the engine's OWN contract namespaces survive — the flow is unrunnable
    # without them, and they carry no third-party credentials
    assert env["METAFLOW_DEFAULT_DATASTORE"] == "s3"
    assert env["CAIRN_SUBMIT_CWD"] == "/work"


def test_inherit_passes_the_ambient_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-travels-under-inherit")
    env = asyncio.run(MetaflowEngine(env_isolation="inherit")._build_env())
    assert env["OPENAI_API_KEY"] == "sk-travels-under-inherit"


# ── declared connections are resolved IN ─────────────────────────────────────

def test_declared_connection_secrets_reach_the_flow(catalog):
    env = asyncio.run(
        MetaflowEngine(secrets=["prod-s3"], env_isolation="strict")._build_env())
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA"
    assert env["AWS_SECRET_ACCESS_KEY"] == "shh"


def test_connection_config_is_namespaced_not_dumped_raw(catalog):
    """Config values must not collide with unrelated ambient names."""
    env = asyncio.run(MetaflowEngine(secrets=["prod-s3"])._build_env())
    assert env["CAIRN_CONN_PROD_S3_REGION"] == "eu-central-1"


def test_an_undeclared_connection_contributes_nothing(catalog):
    env = asyncio.run(MetaflowEngine(env_isolation="strict")._build_env())
    assert "AWS_ACCESS_KEY_ID" not in env


def test_an_unresolvable_connection_is_not_a_submit_failure(catalog):
    """A missing credential should surface as the kernel's own 'not configured'
    error, which names the real problem — not a submit-time catalog trace."""
    assert asyncio.run(_connection_env("does-not-exist")) == {}


def test_an_unavailable_catalog_is_not_a_submit_failure(monkeypatch):
    import cairn.core.connections as conns

    async def _boom(name, **kw):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(conns, "resolve_connection", _boom)
    assert asyncio.run(_connection_env("prod-s3")) == {}


# ── fail closed: remote cannot deliver declared secrets yet ──────────────────

def _stub_registry(monkeypatch):
    import cairn.core.dataflow.stage_loader as stages_mod
    monkeypatch.setattr(stages_mod, "load_stages", lambda: {"ingest.normalize": object()})
    import operator_dataflow_metaflow.engines_metaflow as em
    monkeypatch.setattr(em, "_metaflow_available", lambda: True)
    # see the twin in test_metaflow_cross_box: the #1346 client check is a
    # deployment concern, and a dev box has no `kubernetes` package installed.
    monkeypatch.setattr(em, "_lib_available", lambda module: True)


def test_remote_with_declared_secrets_refuses_at_submit(monkeypatch):
    """Env built here reaches the local Runner subprocess; a @kubernetes/@batch
    TASK gets its env from the container. Resolving secrets that never arrive
    would fail deep in the run, after the cluster was already paid for."""
    _stub_registry(monkeypatch)
    eng = MetaflowEngine(compute={"kind": "kubernetes", "image": "i:1"}, secrets=["prod-s3"])
    assert any("can only" in p and "LOCAL" in p for p in eng.validate(SPEC))


def test_remote_without_secrets_is_fine(monkeypatch):
    _stub_registry(monkeypatch)
    # #1349 added the other half of a valid remote config: a remote task cannot
    # reach the control plane's filesystem, so s3 + sysroot is required too.
    eng = MetaflowEngine(compute={"kind": "kubernetes", "image": "i:1"},
                         datastore="s3", datastore_root="s3://bucket/cairn")
    assert eng.validate(SPEC) == []


def test_local_with_secrets_is_fine(monkeypatch):
    _stub_registry(monkeypatch)
    assert MetaflowEngine(secrets=["prod-s3"]).validate(SPEC) == []


# ── selection wiring ─────────────────────────────────────────────────────────

def test_selection_carries_secrets_and_isolation(monkeypatch):
    for var in ("OBS_ML_TARGET", "OBS_METAFLOW_ROOT", "OBS_METAFLOW_COMPUTE",
                "OBS_METAFLOW_IMAGE", "OBS_METAFLOW_QUEUE", "OBS_METAFLOW_RETRY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OBS_ML_ENGINE", "metaflow")
    monkeypatch.setenv("OBS_METAFLOW_SECRETS", "prod-s3, hf-token")
    monkeypatch.setenv("OBS_METAFLOW_ISOLATION", "strict")
    eng = resolve_engine(None, kind="ml")
    assert eng._secrets == ["prod-s3", "hf-token"]  # whitespace trimmed
    assert eng._env_isolation == "strict"
