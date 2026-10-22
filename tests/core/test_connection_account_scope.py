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
from typing import Any

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
    # auth off: a workspace no store holds is the deployment's (keyless dev)
    assert await workspace.workspace_account("never-created") == "default"
    with workspace_scope("ws-b"):
        assert await workspace.workspace_account() == "acct-b"
    with workspace_scope(""):                            # auth off: no workspace = default
        assert await workspace.workspace_account() == "default"


async def test_auth_on_a_rowless_workspace_has_no_account(world, monkeypatch):
    monkeypatch.setenv("CAIRN_JWT_SECRET", "test-secret")
    assert await workspace.workspace_account("ws-a") == "acct-a"
    with pytest.raises(workspace.WorkspaceForbidden):
        await workspace.workspace_account("never-created")
    with workspace_scope("never-created"), pytest.raises(workspace.WorkspaceForbidden):
        await resolve_connection("gpu")


async def test_auth_on_a_record_with_no_workspace_is_refused(world, monkeypatch):
    from cairn.core.dispatch.pipeline_bridge import _in_record_workspace

    monkeypatch.setenv("CAIRN_JWT_SECRET", "test-secret")
    with pytest.raises(workspace.WorkspaceScopeRequired):
        with workspace_scope(""):
            pass

    class Probe:
        @_in_record_workspace
        async def leg(self, rec):
            return await resolve_connection("gpu")

    with pytest.raises(workspace.WorkspaceScopeRequired):
        await Probe().leg(SimpleNamespace(workspace_id=""))


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


class _RunStore:
    """Just enough of the run store for a leg or a template run to terminalize."""

    def __init__(self, run: Any = None) -> None:
        self.run = run
        self.updates: list[tuple[str, dict]] = []

    async def create(self, run):
        return run

    async def update(self, run_id=None, **kw):
        self.updates.append((run_id, kw))

    async def append_event(self, *a, **kw):
        return None

    async def get_events(self, *a, **kw):
        return []

    async def get(self, run_id, *a, **kw):
        return self.run


def _bridge(run_store: _RunStore):
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge
    from cairn.core.storage.pipeline_store import InMemoryPipelineStore

    async def _emit(*a, **kw):
        return None

    return PipelineBridge(SimpleNamespace(store=run_store, _emit=_emit), store=InMemoryPipelineStore())


async def test_a_task_leg_advanced_from_a_default_context_resolves_in_its_records_account(world, monkeypatch):
    """A leg the bridge advances off a bus event runs in whatever context the
    subscriber has (the deployment's). It still resolves in its record's account."""
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    async def _op(self, op_name, resolved, rec, s):
        conn = await resolve_connection("a-box")
        dep = await resolve_connection("gpu")
        return {"host": conn.config["host"] if conn else "", "deployment_box": dep is not None}

    monkeypatch.setattr(PipelineBridge, "_invoke_task_op", _op)
    runs = _RunStore()
    rec = SimpleNamespace(workspace_id="ws-a", pipeline_id="p1")
    with workspace_scope("default"):
        await _bridge(runs)._run_task_leg(rec, SimpleNamespace(ref="task:x", id="s1", timeout_s=None),
                                          SimpleNamespace(id="r1"), {})
        assert current_workspace() == "default"
    (run_id, done), = runs.updates
    assert done["status"] == "done"
    assert done["result_payloads"] == [{"output": {"host": "a-host", "deployment_box": False}}]


async def test_a_flow_leg_runs_in_its_records_workspace(world, monkeypatch):
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    seen: dict = {}

    async def _settle(self, rec, s, run, resolved, *, adopt=None):
        seen["workspace"] = current_workspace()
        seen["target"] = await resolve_connection("gpu")

    monkeypatch.setattr(PipelineBridge, "_execute_and_settle_flow_leg", _settle)
    monkeypatch.setattr(PipelineBridge, "_enforce_flow_leg_gate", lambda self, rec, s: None)
    rec = SimpleNamespace(workspace_id="ws-a", pipeline_id="p1")
    with workspace_scope("default"):
        await _bridge(_RunStore())._run_flow_leg(rec, SimpleNamespace(ref="flow:x", id="s1"),
                                                 SimpleNamespace(id="r1", status="running"), {})
    assert seen == {"workspace": "ws-a", "target": None}   # the deployment's target is not a's


# ── dispatcher: template runs and resumes ─────────────────────────────


def _stub_template(monkeypatch, seen: dict, *, result: Any = None):
    import cairn.core.engine.compiler as C
    import cairn.core.engine.runner as R
    import cairn.core.engine.skill_engine as SE
    import cairn.core.operator as OP
    import cairn.core.pack.loader as PL
    from cairn.core.dispatch.dispatcher import RunDispatcher

    monkeypatch.setattr(SE, "resolve_template_path", lambda ref: "/nonexistent.yaml")
    monkeypatch.setattr(C, "compile_template", lambda path: object())
    monkeypatch.setattr(OP, "load_all_operators", lambda **kw: None)
    monkeypatch.setattr(PL, "load_all_packs", lambda **kw: None)

    async def _cfg(*a, **kw):
        return {}, None

    async def _contracts(self, inv):
        return {}

    monkeypatch.setattr(RunDispatcher, "_resolve_workflow_config", staticmethod(_cfg))
    monkeypatch.setattr(RunDispatcher, "_contract_invocations", _contracts)

    async def _run(compiled, inputs):
        seen["workspace"] = current_workspace()
        seen["box"] = await resolve_connection("a-box")
        return result if result is not None else {"output": "ok"}

    monkeypatch.setattr(R, "run_within_budget", _run)


async def test_a_template_run_executes_in_its_records_workspace(world, monkeypatch):
    from cairn.core.dispatch.dispatcher import RunDispatcher
    from cairn.core.engine.schema import TriggerEvent
    from cairn.core.storage.protocols import Run

    seen: dict = {}
    _stub_template(monkeypatch, seen)
    inv = Run(id="r-a", trigger=TriggerEvent(source="manual"), workspace_id="ws-a")
    disp = RunDispatcher(runner=object(), store=_RunStore())
    with workspace_scope("default"):
        await disp._run_template_task(inv, "demo/tmpl", {}, None)
    assert seen["workspace"] == "ws-a"
    assert seen["box"].config["host"] == "a-host"


async def test_an_in_process_resume_runs_in_its_records_workspace(world, monkeypatch):
    from cairn.core.dispatch.dispatcher import RunDispatcher
    from cairn.core.runtime.inprocess import InProcessRuntime

    seen: dict = {}

    async def _pending(self, graph, run_id):
        return True

    async def _resume(self, graph, decision, *, run_id):
        seen["workspace"] = current_workspace()
        seen["box"] = await resolve_connection("a-box")
        return {"__interrupt__": [SimpleNamespace(value=None)]}   # paused again: least follow-on work

    monkeypatch.setattr(InProcessRuntime, "has_pending_interrupt", _pending)
    monkeypatch.setattr(InProcessRuntime, "resume", _resume)
    monkeypatch.setattr(RunDispatcher, "_recompile_graph", staticmethod(lambda name: object()))
    run = SimpleNamespace(id="r-a", status="awaiting_approval", workspace_id="ws-a",
                          results=[SimpleNamespace(template_name="demo/tmpl")],
                          bundle_pipeline_id="", owner="")
    disp = RunDispatcher(runner=object(), store=_RunStore(run))
    with workspace_scope("default"):
        await disp.resume_run("r-a", {"approved": True})
    assert seen["workspace"] == "ws-a"
    assert seen["box"].config["host"] == "a-host"


# ── Temporal owner: the activity twins of the bridge ──────────────────


async def test_temporal_template_leg_runs_in_the_pipeline_records_workspace(world, monkeypatch):
    pytest.importorskip("temporalio")
    from temporalio.testing import ActivityEnvironment

    import cairn.core.runtime.temporal.pipeline_activities as PA
    from cairn.core.storage.pipeline_store import InMemoryPipelineStore, PipelineRecord

    pstore = InMemoryPipelineStore()
    await pstore.put(PipelineRecord(pipeline_id="p1", workspace_id="ws-a"))
    monkeypatch.setattr("cairn.core.storage.pipeline_store.default_pipeline_store", lambda: pstore)
    seen: dict = {}

    async def _run_template(ref, inputs, **kw):
        seen["workspace"] = current_workspace()
        seen["box"] = await resolve_connection("a-box")
        return SimpleNamespace(id="run-1")

    monkeypatch.setattr(PA, "_dispatcher", lambda: SimpleNamespace(run_template=_run_template))
    with workspace_scope("default"):
        assert await ActivityEnvironment().run(PA.pipeline_submit_step, "demo/tmpl", {}, "p1", "s1", None) == "run-1"
    assert seen["workspace"] == "ws-a"
    assert seen["box"].config["host"] == "a-host"


async def test_temporal_template_leg_without_a_record_is_refused(world, monkeypatch):
    pytest.importorskip("temporalio")
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import ActivityEnvironment

    import cairn.core.runtime.temporal.pipeline_activities as PA
    from cairn.core.storage.pipeline_store import InMemoryPipelineStore

    pstore = InMemoryPipelineStore()
    monkeypatch.setattr("cairn.core.storage.pipeline_store.default_pipeline_store", lambda: pstore)
    called: list = []

    async def _run_template(ref, inputs, **kw):
        called.append(ref)
        return SimpleNamespace(id="run-1")

    monkeypatch.setattr(PA, "_dispatcher", lambda: SimpleNamespace(run_template=_run_template))
    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(PA.pipeline_submit_step, "demo/tmpl", {}, "p-missing", "s1", None)
    assert called == []


async def test_temporal_flow_leg_runs_in_the_pipeline_records_workspace(world, monkeypatch):
    pytest.importorskip("temporalio")
    from temporalio.testing import ActivityEnvironment

    import cairn.core.dispatch.leg_executor as LE
    import cairn.core.dispatch.leg_hooks as LH
    import cairn.core.runtime.temporal.pipeline_activities as PA
    from cairn.core.storage.pipeline_store import InMemoryPipelineStore, PipelineRecord

    pstore = InMemoryPipelineStore()
    await pstore.put(PipelineRecord(pipeline_id="p1", workspace_id="ws-a"))
    monkeypatch.setattr("cairn.core.storage.pipeline_store.default_pipeline_store", lambda: pstore)

    async def _emit(*a, **kw):
        return None

    monkeypatch.setattr(PA, "_dispatcher", lambda: SimpleNamespace(store=_RunStore(), _emit=_emit))

    async def _no_gate(*a, **kw):
        return None

    monkeypatch.setattr(PA, "_gate_flow_leg", _no_gate)
    monkeypatch.setattr(LE, "parse_flow_ref", lambda ref: None)          # kind "data", no def lookup
    monkeypatch.setattr("cairn.core.dataflow.engine_registry.resolve_engine", lambda *a, **kw: object())

    class _Hooks:
        def __init__(self, **kw):
            pass

        def execute_kwargs(self, **kw):
            return {}

        async def relay_checkpoints(self):
            return None

    monkeypatch.setattr(LH, "FlowLegHooks", _Hooks)
    seen: dict = {}

    async def _execute_leg(ref, **kw):
        seen["workspace"] = current_workspace()
        seen["target"] = await resolve_connection("gpu")
        seen["own"] = await resolve_connection("a-box")
        return LE.LegOutcome(state="done")

    monkeypatch.setattr(LE, "execute_leg", _execute_leg)
    with workspace_scope("default"):
        await ActivityEnvironment().run(PA.pipeline_submit_step, "flow:demo", {}, "p1", "s1", None, {})
    assert seen["workspace"] == "ws-a"
    assert seen["target"] is None                         # the deployment's target is not a's
    assert seen["own"].config["host"] == "a-host"


# ── templates contract and profile-bound backends ────────────────────


async def test_templates_contract_lists_only_the_accounts_mcp_connections(world, monkeypatch):
    from cairn.core.triggers.routers.templates import build_templates_router

    await world.store.create(Connection(name="dep-mcp", kind="mcp"))
    await world.store.create(Connection(name="a-mcp", kind="mcp", account_id="acct-a"))
    seen: dict = {}

    def _contract(ref, *, connected_names, profile):
        seen["connected"] = set(connected_names)
        return SimpleNamespace(plan={"template_found": True}, model_dump=lambda **kw: {})

    monkeypatch.setattr("cairn.core.ui_contract.build_ui_contract", _contract)
    route = next(r for r in build_templates_router().routes if r.path == "/packs/{pack}/{template}/contract")
    with workspace_scope("ws-a"):
        await route.endpoint(pack="demo", template="tmpl", profile=None)
    assert seen["connected"] == {"a-mcp"}
    with workspace_scope("default"):
        await route.endpoint(pack="demo", template="tmpl", profile=None)
    assert seen["connected"] == {"dep-mcp"}


async def test_profile_bound_backend_connection_is_always_the_deployments(world, tmp_path, monkeypatch):
    import cairn.core.backend_connections as bc

    for var in ("QDRANT_URL", "QDRANT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    await world.store.create(Connection(name="prod-qdrant", kind="api_key",
                                        config={"url": "https://deploy-qdrant:6333"}))
    await world.store.create(Connection(name="a-qdrant", kind="api_key", account_id="acct-a",
                                        config={"url": "https://a-qdrant:6333"}))
    profile = tmp_path / "env.toml"
    monkeypatch.setenv("OBS_PROFILE", str(profile))
    bc.reset_applied()
    try:
        profile.write_text('[profile.backends]\nrag_vector_store = "qdrant"\n'
                           'rag_vector_store_connection = "prod-qdrant"\n', encoding="utf-8")
        with workspace_scope("ws-a"):                    # even inside a tenant's run
            assert await bc.apply_backend_connection("rag_vector_store") == "prod-qdrant"
        assert os.environ["QDRANT_URL"] == "https://deploy-qdrant:6333"

        bc.reset_applied()
        monkeypatch.delenv("QDRANT_URL", raising=False)
        profile.write_text('[profile.backends]\nrag_vector_store = "qdrant"\n'
                           'rag_vector_store_connection = "a-qdrant"\n', encoding="utf-8")
        with workspace_scope("ws-a"):
            assert await bc.apply_backend_connection("rag_vector_store") is None   # a tenant row never binds
        assert "QDRANT_URL" not in os.environ
    finally:
        bc.reset_applied()
