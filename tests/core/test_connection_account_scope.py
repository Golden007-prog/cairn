"""Connections and declared secrets resolve in the run's account.

A run's account is the owner of its workspace (``Workspace.account_id``). A
tenant account's run resolves only its own connections by name and reads only
its own secret values; the deployment's (``default``) run resolves only the
deployment's. The deployment's behaviour is otherwise unchanged. Hermetic:
in-memory connection and workspace stores, a temp SQLite vault, no network.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel

import cairn.core.connections as connections
from cairn.core import workspace
from cairn.core.connections import (
    Connection,
    HealthState,
    InMemoryConnectionStore,
    declared_secret_values,
    inject_declared_secrets,
    resolve_connection,
)
from cairn.core.operator.binding import ExecutionField, OperationBinding
from cairn.core.operator.runtime import BindingEvalError
from cairn.core.operator.secrets import build_scoped_secrets
from cairn.core.secrets_store import mint_ref, put_secret
from cairn.core.workspace import InMemoryWorkspaceStore, Workspace, current_workspace, workspace_scope


class _Provider:
    """A bound deployment SecretStore that records every read."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[str] = []

    def get(self, name: str) -> str:
        self.reads.append(name)
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def available_keys(self) -> list[str]:
        return sorted(self.values)


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_STORAGE", raising=False)
    monkeypatch.delenv("CAIRN_SECRET_BACKEND", raising=False)
    monkeypatch.setenv("CAIRN_SECRETS_DB", str(tmp_path / "secrets.db"))
    monkeypatch.setenv("CAIRN_SECRET_KEY", Fernet.generate_key().decode())
    for var in ("KAGGLE_USERNAME", "KAGGLE_KEY", "HF_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    wstore = InMemoryWorkspaceStore()
    await wstore.put(Workspace(id="ws-a", account_id="acct-a"))
    await wstore.put(Workspace(id="ws-b", account_id="acct-b"))
    monkeypatch.setattr(workspace, "_store", wstore)

    store = InMemoryConnectionStore()
    # the deployment's own rows
    await store.create(Connection(name="gpu", kind="compute.ssh", config={"host": "deploy-host"},
                                  max_in_flight=2,
                                  health=HealthState(ok=False, detail="deploy box down")))
    await store.create(Connection(name="dep-kaggle", kind="api_key",
                                  secrets={"USERNAME": "KAGGLE_USERNAME", "KEY": "KAGGLE_KEY"}))
    await store.create(Connection(name="huggingface", kind="api_key", secrets={"TOKEN": "HF_TOKEN"}))
    # tenant rows
    await store.create(Connection(name="a-box", kind="compute.ssh", account_id="acct-a",
                                  config={"host": "a-host"}))
    await store.create(Connection(name="b-box", kind="compute.ssh", account_id="acct-b",
                                  config={"host": "b-host"}))
    a_key = put_secret(mint_ref("kaggle", "KEY", account_id="acct-a"), "a-kaggle-key", account_id="acct-a")
    await store.create(Connection(name="kaggle", kind="api_key", account_id="acct-a",
                                  secrets={"KEY": a_key}))
    b_tok = put_secret(mint_ref("b-hf", "HF_TOKEN", account_id="acct-b"), "b-hf-token", account_id="acct-b")
    await store.create(Connection(name="b-hf", kind="api_key", account_id="acct-b",
                                  secrets={"HF_TOKEN": b_tok}))
    monkeypatch.setattr(connections, "default_connection_store", lambda: store)

    provider = _Provider({"KAGGLE_USERNAME": "dep-user", "KAGGLE_KEY": "dep-key", "HF_TOKEN": "dep-hf"})
    monkeypatch.setattr("cairn.core.operator.secrets.get_secret_provider", lambda: provider)
    return SimpleNamespace(store=store, provider=provider)


def _env_snapshot() -> dict[str, str]:
    return dict(os.environ)


# ── workspace → account ───────────────────────────────────────────────


async def test_workspace_account_maps_owner_and_defaults(world):
    assert await workspace.workspace_account("ws-a") == "acct-a"
    assert await workspace.workspace_account("default") == "default"
    assert await workspace.workspace_account("never-created") == "default"
    with workspace_scope("ws-b"):
        assert await workspace.workspace_account() == "acct-b"


def test_workspace_scope_restores_the_outer_workspace():
    outer = current_workspace()
    with workspace_scope("ws-a"):
        assert current_workspace() == "ws-a"
    assert current_workspace() == outer


# ── resolve_connection ────────────────────────────────────────────────


async def test_tenant_resolves_only_its_own_rows_by_name(world):
    assert (await resolve_connection("a-box", account_id="acct-a")).config["host"] == "a-host"
    assert await resolve_connection("b-box", account_id="acct-a") is None   # another account's
    assert await resolve_connection("gpu", account_id="acct-a") is None     # the deployment's


async def test_deployment_resolves_only_deployment_rows(world):
    assert (await resolve_connection("gpu", account_id="default")).config["host"] == "deploy-host"
    assert (await resolve_connection("gpu")).config["host"] == "deploy-host"  # no context: deployment
    assert await resolve_connection("a-box", account_id="default") is None
    assert await resolve_connection("a-box") is None


async def test_omitted_account_is_the_run_workspace_owner(world):
    with workspace_scope("ws-a"):
        assert (await resolve_connection("a-box")).config["host"] == "a-host"
        assert await resolve_connection("gpu") is None
        assert await resolve_connection("b-box") is None
    with workspace_scope("default"):
        assert (await resolve_connection("gpu")).config["host"] == "deploy-host"


# ── operator runtime: compute_target ──────────────────────────────────


class _Inp(BaseModel):
    x: int = 1


class _Out(BaseModel):
    host: str = ""
    key: str = ""
    hf: str = ""


def _op(seen: dict):
    class Probe:
        name = "probe/op"
        input_schema = _Inp
        output_schema = _Out
        supports_mock = False

        def run(self, inp, ctx):
            seen["compute"] = getattr(ctx, "compute", None)
            try:
                seen["key"] = ctx.secrets.get("KAGGLE_KEY")
            except KeyError as e:
                seen["key_error"] = str(e)
            try:
                seen["hf"] = ctx.secrets.get("HF_TOKEN")
            except KeyError as e:
                seen["hf_error"] = str(e)
            return _Out()

    return Probe


async def test_tenant_compute_target_cannot_name_the_deployment_box(world):
    from cairn.core.operator.runtime import invoke_operator_inprocess

    binding = OperationBinding(operator="probe/op", input={}, output={},
                               execution=ExecutionField(compute_target="gpu"))
    with workspace_scope("ws-a"), pytest.raises(BindingEvalError, match="did not resolve"):
        await invoke_operator_inprocess(binding, _op({}), state={}, pipeline_id="p", node_name="n")

    seen: dict = {}
    with workspace_scope("default"):
        await invoke_operator_inprocess(binding, _op(seen), state={}, pipeline_id="p", node_name="n")
    assert seen["compute"].config["host"] == "deploy-host"


async def test_tenant_compute_target_resolves_its_own_box(world):
    from cairn.core.operator.runtime import invoke_operator_inprocess

    seen: dict = {}
    binding = OperationBinding(operator="probe/op", input={}, output={},
                               execution=ExecutionField(compute_target="a-box"))
    with workspace_scope("ws-a"):
        await invoke_operator_inprocess(binding, _op(seen), state={}, pipeline_id="p", node_name="n")
    assert seen["compute"].config["host"] == "a-host"


# ── declared secrets ──────────────────────────────────────────────────


async def test_tenant_injection_never_writes_the_process_env(world):
    before = _env_snapshot()
    with workspace_scope("ws-a"):
        assert await inject_declared_secrets(["KAGGLE_USERNAME", "KAGGLE_KEY", "HF_TOKEN"]) == []
    assert await inject_declared_secrets(["KAGGLE_KEY"], account_id="acct-b") == []
    assert _env_snapshot() == before


async def test_deployment_injection_is_unchanged(world, monkeypatch):
    injected = await inject_declared_secrets(["KAGGLE_USERNAME", "KAGGLE_KEY"])
    try:
        assert sorted(injected) == ["KAGGLE_KEY", "KAGGLE_USERNAME"]
        assert os.environ["KAGGLE_KEY"] == "dep-key"
    finally:
        for var in injected:
            monkeypatch.delenv(var, raising=False)


async def test_declared_secret_values_come_from_the_account_only(world):
    assert await declared_secret_values(["KAGGLE_KEY", "HF_TOKEN"], account_id="acct-a") == {
        "KAGGLE_KEY": "a-kaggle-key"}                     # catalog: kaggle.KEY → KAGGLE_KEY
    assert await declared_secret_values(["KAGGLE_KEY", "HF_TOKEN"], account_id="acct-b") == {
        "HF_TOKEN": "b-hf-token"}                         # the logical key is the target


def test_build_scoped_secrets_for_a_tenant_never_reads_the_bound_provider(world):
    scoped = build_scoped_secrets(["KAGGLE_KEY", "HF_TOKEN"], account_id="acct-a",
                                  values={"KAGGLE_KEY": "a-kaggle-key", "OTHER": "x"})
    assert scoped.get("KAGGLE_KEY") == "a-kaggle-key"
    with pytest.raises(KeyError):
        scoped.get("HF_TOKEN")          # declared, but this account supplies none
    with pytest.raises(KeyError):
        scoped.get("OTHER")             # supplied, but not declared
    assert world.provider.reads == []


def test_build_scoped_secrets_for_the_deployment_is_unchanged(world):
    scoped = build_scoped_secrets(["HF_TOKEN"])
    assert scoped.get("HF_TOKEN") == "dep-hf"
    assert build_scoped_secrets(["HF_TOKEN"], account_id="default").get("HF_TOKEN") == "dep-hf"


async def test_tenant_operator_sees_its_own_secret_and_not_the_deployments(world):
    from cairn.core.operator.runtime import invoke_operator_core

    seen: dict = {}
    before = _env_snapshot()
    with workspace_scope("ws-a"):
        await invoke_operator_core(_op(seen), {}, pipeline_id="p", node_name="n",
                                   secret_keys=["KAGGLE_KEY", "HF_TOKEN"])
    assert seen["key"] == "a-kaggle-key"
    assert "hf" not in seen and "HF_TOKEN" in seen["hf_error"]   # the deployment's is not reachable
    assert world.provider.reads == []
    assert _env_snapshot() == before


async def test_deployment_operator_secrets_are_unchanged(world, monkeypatch):
    from cairn.core.operator.runtime import invoke_operator_core

    seen: dict = {}
    try:
        await invoke_operator_core(_op(seen), {}, pipeline_id="p", node_name="n",
                                   secret_keys=["KAGGLE_KEY", "HF_TOKEN"])
        assert seen["key"] == "dep-key"
        assert seen["hf"] == "dep-hf"
    finally:
        monkeypatch.delenv("KAGGLE_KEY", raising=False)


# ── remote-job control worker ─────────────────────────────────────────


async def test_job_control_resolves_compute_in_the_jobs_workspace(world):
    from cairn.core.operator.job_control import build_default_poll
    from cairn.core.operator.job_store import RemoteJobRecord
    from cairn.core.operator.jobs import JobStatus

    seen: dict = {}

    class StubOp:
        name = "stub/gpu"
        input_schema = _Inp
        output_schema = _Out

        async def poll(self, handle, inp, ctx):
            seen.setdefault("compute", []).append(ctx.compute)
            seen.setdefault("workspace", []).append(current_workspace())
            return JobStatus(state="running")

    registry = SimpleNamespace(get=lambda name: StubOp)
    poll = build_default_poll(registry)
    for target in ("gpu", "a-box"):
        await poll(RemoteJobRecord(run_id="r", workspace_id="ws-a", node_name="n", provider="stub/gpu",
                                   job_id="j", compute_target=target))
    assert seen["compute"][0] is None                      # the deployment's box: not this tenant's
    assert seen["compute"][1].config["host"] == "a-host"
    assert seen["workspace"] == ["ws-a", "ws-a"]           # the job's workspace, not the worker's


# ── admission-side lookups ────────────────────────────────────────────


async def test_target_cap_is_read_in_the_run_account(world):
    from cairn.core.dispatch.target_concurrency import target_cap

    assert await target_cap("gpu") == 2
    with workspace_scope("ws-a"):
        assert await target_cap("gpu") is None


async def test_license_gate_token_check_is_per_account(world, monkeypatch):
    from cairn.core.model_license_gate import _has_hf_token

    monkeypatch.setenv("HF_TOKEN", "dep-hf")        # the deployment's ref resolves from its env
    assert await _has_hf_token() is True
    with workspace_scope("ws-a"):
        assert await _has_hf_token() is False        # the deployment's huggingface row is not a's


async def test_preflight_connection_warnings_are_per_account(world):
    from cairn.core.preflight import _connection_warnings

    assert any("gpu" in w for w in await _connection_warnings())
    with workspace_scope("ws-a"):
        assert await _connection_warnings() == []


# ── dispatch: legs run in their record's workspace ────────────────────


async def test_bridge_leg_methods_run_in_the_record_workspace():
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge, _in_record_workspace

    for name in ("_submit_step", "_run_task_leg", "_run_flow_leg"):
        assert getattr(getattr(PipelineBridge, name), "__wrapped__", None) is not None, name

    class Probe:
        @_in_record_workspace
        async def leg(self, rec):
            return current_workspace()

    outer = current_workspace()
    assert await Probe().leg(SimpleNamespace(workspace_id="ws-b")) == "ws-b"
    assert current_workspace() == outer
