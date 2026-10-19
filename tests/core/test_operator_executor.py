"""Tests for the §Phase-2.7 executor dispatcher.

Pins five contracts:

1. **Picker honors ExecutorPolicy.prefer** when the preferred executor
   is both declared by the operator and implemented by the substrate.
2. **Picker falls back to first-implemented** when the preference is
   unsupported or unset.
3. **UnsupportedExecutorError fires only when nothing matches** — and
   the message names every unimplemented executor with its phase hint.
4. **Missing metadata defaults to in_process** so test fixtures and
   undecorated classes still work via the dispatcher.
5. **End-to-end via invoke_operator** dispatches the same as the
   existing invoke_operator_inprocess for the in_process path.
"""

from __future__ import annotations

import pytest

from cairn.core.operator.binding import OperationBinding
from cairn.core.operator.executor import (
    ExecutorPolicy,
    UnsupportedExecutorError,
    declared_executors,
    implemented_executors,
    is_implemented,
    pick_executor,
)
from cairn.core.operator.protocol import Operation, OperationContext
from cairn.core.operator.registry import OperatorRegistry
from cairn.core.operator.runtime import invoke_operator
from cairn.core.pack.manifest import OperatorEntry
from pydantic import BaseModel


# ─── helpers ────────────────────────────────────────────────────────


class _NoopIn(BaseModel):
    value: str = ""


class _NoopOut(BaseModel):
    echoed: str = ""


def _make_op(name: str):
    """Build a minimal Operation subclass for picker tests.

    Bypasses @register_operator so each test can attach exactly the
    metadata it cares about. The class still satisfies OperatorRegistry
    .register's required-attr checks (name, version, input_schema,
    output_schema).
    """

    class _Op(Operation[_NoopIn, _NoopOut]):
        input_schema = _NoopIn
        output_schema = _NoopOut

        def run(self, inp: _NoopIn, ctx: OperationContext) -> _NoopOut:
            return _NoopOut(echoed=inp.value)

    _Op.__name__ = name.replace("/", "_")
    _Op.name = name
    _Op.version = "0.1.0"
    _Op.capabilities = []
    return _Op


def _attach(
    registry: OperatorRegistry, op_cls, *, executors: list[str], risk: str = "read_only",
):
    registry.register(op_cls)
    registry.attach_metadata(
        OperatorEntry(
            name=op_cls.name,
            risk=risk,
            executors=list(executors),
        ),
    )


# ─── implemented_executors / is_implemented ────────────────────────


def test_implemented_executors_contains_in_process():
    """Phase 2.7 ships exactly one implementation."""
    assert "in_process" in implemented_executors()
    assert is_implemented("in_process") is True


def test_unimplemented_executors_report_false():
    # in_process + mcp_tool (§24.3) + docker & remote_job (issue #7) are
    # implemented; kubernetes_job / temporal_activity / http_call stay pending.
    for name in ("kubernetes_job", "temporal_activity", "http_call"):
        assert is_implemented(name) is False, name


# ─── declared_executors ────────────────────────────────────────────


def test_declared_executors_reads_from_metadata():
    reg = OperatorRegistry()
    op = _make_op("x/y")
    _attach(reg, op, executors=["in_process", "mcp_tool"])
    assert declared_executors(op, registry=reg) == ["in_process", "mcp_tool"]


def test_declared_executors_defaults_to_in_process_when_no_metadata():
    """An operator with no attached metadata still works — the picker
    sees a single-element ["in_process"] list."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    reg.register(op)  # NO metadata attached
    assert declared_executors(op, registry=reg) == ["in_process"]


def test_declared_executors_handles_empty_metadata_list():
    """An OperatorEntry with executors=[] should behave like no
    metadata — the default protects against pack-author oversights."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    reg.register(op)
    reg.attach_metadata(OperatorEntry(name="x/y", executors=[]))
    assert declared_executors(op, registry=reg) == ["in_process"]


# ─── pick_executor — the load-bearing piece ────────────────────────


def test_pick_executor_returns_in_process_for_local_op():
    """An operator declaring only in_process gets in_process."""
    reg = OperatorRegistry()
    op = _make_op("local/op")
    _attach(reg, op, executors=["in_process"])
    assert pick_executor(op, registry=reg) == "in_process"


def test_pick_executor_honors_supported_preference():
    """When policy.prefer is in BOTH the declared list AND the
    implemented set, the picker picks it."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    # Operator declares both, both are in declared list. Only in_process
    # is implemented today. With prefer=in_process, picker chooses it.
    _attach(reg, op, executors=["in_process", "mcp_tool"])
    picked = pick_executor(
        op,
        policy=ExecutorPolicy(prefer="in_process"),
        registry=reg,
    )
    assert picked == "in_process"


def test_pick_executor_ignores_unimplemented_preference():
    """When policy.prefer names an unimplemented executor, the picker
    silently falls back to first-implemented. This is intentional —
    Phase 3+ will harden into "prefer fails if unsupported", but Phase
    2.7 keeps the soft fallback so adding env policy hints doesn't
    accidentally break running deployments."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    _attach(reg, op, executors=["in_process", "temporal_activity"])
    picked = pick_executor(
        op,
        policy=ExecutorPolicy(prefer="temporal_activity"),  # not implemented
        registry=reg,
    )
    assert picked == "in_process"


def test_pick_executor_ignores_preference_not_declared_by_op():
    """policy.prefer = "docker" but op doesn't declare docker → fall
    back to first-implemented from the declared list."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    _attach(reg, op, executors=["in_process"])
    picked = pick_executor(
        op,
        policy=ExecutorPolicy(prefer="docker"),
        registry=reg,
    )
    assert picked == "in_process"


def test_pick_executor_walks_declared_list_in_order():
    """Without a preference, picks the first executor in the operator's
    declared order that's implemented. Pack authors control fallback by
    ordering."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    # temporal_activity not implemented → skip; in_process implemented → pick.
    _attach(reg, op, executors=["temporal_activity", "in_process"])
    assert pick_executor(op, registry=reg) == "in_process"


def test_pick_executor_raises_when_nothing_implemented():
    """An op declaring only unimplemented executors raises with a
    multi-line diagnostic naming every gap + its expected phase."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    _attach(reg, op, executors=["temporal_activity", "kubernetes_job"])
    with pytest.raises(UnsupportedExecutorError) as exc_info:
        pick_executor(op, registry=reg)
    msg = str(exc_info.value)
    assert "x/y" in msg
    assert "temporal_activity" in msg
    assert "kubernetes_job" in msg
    # Phase hints surfaced so the env owner knows when to expect support.
    assert "Phase 4" in msg


def test_pick_executor_uses_default_in_process_when_no_metadata():
    """An operator with no attached metadata still picks — the default
    declared list (["in_process"]) keeps test fixtures working."""
    reg = OperatorRegistry()
    op = _make_op("x/y")
    reg.register(op)
    assert pick_executor(op, registry=reg) == "in_process"


# ─── invoke_operator — end-to-end dispatcher ───────────────────────


@pytest.mark.asyncio
async def test_invoke_operator_dispatches_to_in_process(monkeypatch):
    """Smoke: invoke_operator() picks in_process and the run
    completes through the same path as invoke_operator_inprocess.
    Validates the wrapper doesn't transform the call shape."""
    reg = OperatorRegistry()

    # Build a real registered operator that produces a typed output.
    class _IntInput(BaseModel):
        n: int

    class _IntOutput(BaseModel):
        doubled: int

    class _DoubleOp(Operation[_IntInput, _IntOutput]):
        input_schema = _IntInput
        output_schema = _IntOutput

        def run(self, inp, ctx):
            return _IntOutput(doubled=inp.n * 2)

    _DoubleOp.name = "x/double"
    _DoubleOp.version = "0.1.0"
    _DoubleOp.capabilities = []
    _attach(reg, _DoubleOp, executors=["in_process"])

    # Point pick_executor at the test registry. The dispatcher reads
    # the default registry by default; swap by monkeypatching.
    import cairn.core.operator.executor as ex
    monkeypatch.setattr(ex, "default_registry", reg)

    binding = OperationBinding(
        operator="x/double",
        input={"n": "$input.n"},
        output={"doubled": "$state.result"},
    )

    patch = await invoke_operator(
        binding,
        _DoubleOp,
        state={},
        template_input={"n": 5},
        pipeline_id="t",
        node_name="double",
    )
    assert patch == {"result": 10}


@pytest.mark.asyncio
async def test_invoke_operator_raises_when_no_implemented_executor(monkeypatch):
    """An operator declaring only unimplemented executors surfaces
    UnsupportedExecutorError out of invoke_operator (not via the
    inner inprocess function — the dispatcher catches it before
    delegation)."""
    reg = OperatorRegistry()
    op = _make_op("x/temporal-only")
    _attach(reg, op, executors=["temporal_activity"])

    import cairn.core.operator.executor as ex
    monkeypatch.setattr(ex, "default_registry", reg)

    binding = OperationBinding(
        operator="x/temporal-only",
        input={},
        output={},
    )

    with pytest.raises(UnsupportedExecutorError) as exc_info:
        await invoke_operator(
            binding,
            op,
            state={},
            pipeline_id="t",
            node_name="n",
        )
    assert "temporal_activity" in str(exc_info.value)
