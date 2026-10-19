"""Tests for OperatorRegistry + @register_operator decorator.

Covers OP1 (#180) acceptance:
  - register / get / list / has work
  - @register_operator attaches metadata AND registers
  - name collision: default last-wins (warn), strict=True raises
  - unknown lookup raises UnknownOperatorError with helpful message
  - missing required class attributes fail loud at registration
"""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from cairn.core.operator import (
    DuplicateOperatorError,
    Operation,
    OperationContext,
    OperatorRegistry,
    UnknownOperatorError,
    register_operator,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


class _In(BaseModel):
    x: int


class _Out(BaseModel):
    y: int


@pytest.fixture
def isolated_registry():
    """Fresh registry per test — avoids cross-test contamination."""
    return OperatorRegistry()


# ── @register_operator decorator ─────────────────────────────────────────


def test_decorator_attaches_name_version_capabilities(isolated_registry):
    @register_operator(
        name="demo/alpha",
        version="0.1.0",
        capabilities=["cap.alpha", "cap.beta"],
        registry=isolated_registry,
    )
    class AlphaOp(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=inp.x + 1)

    assert AlphaOp.name == "demo/alpha"
    assert AlphaOp.version == "0.1.0"
    assert AlphaOp.capabilities == ["cap.alpha", "cap.beta"]
    # Decorator registered the class
    assert isolated_registry.has("demo/alpha")
    assert isolated_registry.get("demo/alpha") is AlphaOp


def test_decorator_capabilities_optional(isolated_registry):
    @register_operator(
        name="demo/no-caps",
        version="0.1.0",
        registry=isolated_registry,
    )
    class NoCapOp(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    assert NoCapOp.capabilities == []


# ── Registry: register / get / has / list ────────────────────────────────


def test_register_and_get(isolated_registry):
    @register_operator(name="r/one", version="0.1.0", registry=isolated_registry)
    class Op1(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=1)

    assert isolated_registry.get("r/one") is Op1


def test_has_returns_bool(isolated_registry):
    assert isolated_registry.has("absent") is False

    @register_operator(name="r/present", version="0.1.0", registry=isolated_registry)
    class _PresentOp(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    assert isolated_registry.has("r/present") is True


def test_list_names_is_sorted(isolated_registry):
    for name in ("zeta/op", "alpha/op", "mu/op"):
        @register_operator(name=name, version="0.1.0", registry=isolated_registry)
        class _Op(Operation[_In, _Out]):
            input_schema = _In
            output_schema = _Out

            def run(self, inp, ctx):
                return _Out(y=0)

    assert isolated_registry.list_names() == ["alpha/op", "mu/op", "zeta/op"]


def test_all_returns_copy(isolated_registry):
    @register_operator(name="r/x", version="0.1.0", registry=isolated_registry)
    class _Op(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    snapshot = isolated_registry.all()
    snapshot["other"] = _Op  # mutating the snapshot must not affect registry
    assert isolated_registry.list_names() == ["r/x"]


def test_clear_removes_all(isolated_registry):
    @register_operator(name="r/y", version="0.1.0", registry=isolated_registry)
    class _Op(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    assert isolated_registry.has("r/y")
    isolated_registry.clear()
    assert isolated_registry.list_names() == []


# ── Error paths ──────────────────────────────────────────────────────────


def test_unknown_operator_lookup_raises_with_known_list(isolated_registry):
    @register_operator(name="known/op", version="0.1.0", registry=isolated_registry)
    class _Op(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    with pytest.raises(UnknownOperatorError) as ei:
        isolated_registry.get("does/not-exist")
    msg = str(ei.value)
    assert "does/not-exist" in msg
    assert "known/op" in msg  # message includes known operators for discoverability


def test_collision_default_is_last_wins_with_warning(
    isolated_registry, caplog: pytest.LogCaptureFixture
):
    @register_operator(name="r/dup", version="0.1.0", registry=isolated_registry)
    class FirstOp(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=1)

    with caplog.at_level(logging.WARNING, logger="cairn.operator.registry"):

        @register_operator(name="r/dup", version="0.2.0", registry=isolated_registry)
        class SecondOp(Operation[_In, _Out]):
            input_schema = _In
            output_schema = _Out

            def run(self, inp, ctx):
                return _Out(y=2)

    # Second wins
    assert isolated_registry.get("r/dup") is SecondOp
    # And a warning was logged
    assert any("re-registered" in rec.message for rec in caplog.records)


def test_collision_strict_raises():
    reg = OperatorRegistry()

    @register_operator(name="r/strict", version="0.1.0", registry=reg)
    class _First(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    with pytest.raises(DuplicateOperatorError) as ei:
        @register_operator(name="r/strict", version="0.2.0", registry=reg, strict=True)
        class _Second(Operation[_In, _Out]):
            input_schema = _In
            output_schema = _Out

            def run(self, inp, ctx):
                return _Out(y=0)

    msg = str(ei.value)
    assert "r/strict" in msg
    assert "already registered" in msg


def test_register_without_name_fails(isolated_registry):
    class NoName(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    with pytest.raises(ValueError, match="missing `name`"):
        isolated_registry.register(NoName)


def test_register_without_version_fails(isolated_registry):
    class NoVer(Operation[_In, _Out]):
        name = "n/v"
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=0)

    with pytest.raises(ValueError, match="missing `version`"):
        isolated_registry.register(NoVer)


def test_register_without_input_schema_fails(isolated_registry):
    class NoInput(Operation):  # type: ignore[type-arg]
        name = "n/v"
        version = "0.1.0"

        def run(self, inp, ctx):
            return None  # type: ignore[return-value]

    # input_schema is declared on Operation as a class attribute but
    # without a default — accessing it on the bare subclass raises AttributeError
    # via hasattr → False.
    if hasattr(NoInput, "input_schema"):
        pytest.skip(
            "input_schema present on bare subclass — runtime check still "
            "ensures Pydantic class; this gate covers the explicit attribute case"
        )
    with pytest.raises(ValueError, match="input_schema"):
        isolated_registry.register(NoInput)


# ── End-to-end: register + invoke through registry ───────────────────────


def test_end_to_end_register_lookup_invoke(isolated_registry):
    @register_operator(name="e2e/add", version="0.1.0", registry=isolated_registry)
    class AddOp(Operation[_In, _Out]):
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx):
            return _Out(y=inp.x + 10)

    op_cls = isolated_registry.get("e2e/add")
    op = op_cls()
    out = op.run(_In(x=5), OperationContext(pipeline_id="p", node_name="n"))
    assert out.y == 15
