"""#1544 — the operator test harness drives the REAL invoke path.

The claim under test is not "the helper returns something"; it is that
:func:`run_operator_for_test` routes through ``invoke_operator_core`` and so
carries everything an operator gets under the engine — config layering
(ADR-0068), the ADR-0070 precedence order, the #905 governance-strip, input
validation, secret SCOPING (§24.20.13) and the mock decision. Each test below
pins one of those; if the harness ever degrades to a bare ``op.run()``, they go
red here as well as in the packs that use it.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from cairn.core.operator import Operation, run_operator_for_test
from cairn.core.operator.runtime import BindingEvalError


class _Knobs(BaseModel):
    model_config = {"extra": "forbid"}
    limit: int = 1
    # ADR-0070: `governance` scope = env-only, never settable by a run (#905).
    ceiling: int = Field(default=10, json_schema_extra={"scope": "governance"})


class _In(BaseModel):
    payload: str
    limit: int = 1
    ceiling: int = 10


class _Out(BaseModel):
    payload: str
    limit: int
    ceiling: int
    secret: str = ""
    compute_name: str = ""
    mocked: bool = False


class _Probe(Operation[_In, _Out]):
    """Echoes back exactly what the invoke path handed it."""

    name = "test/probe"
    version = "0.1.0"
    input_schema = _In
    output_schema = _Out
    config_schema = _Knobs
    supports_mock = True

    def run(self, inp: _In, ctx) -> _Out:
        secret = ctx.secrets.get("TOKEN") if "TOKEN" in ctx.secrets.available_keys() else ""
        return _Out(
            payload=inp.payload, limit=inp.limit, ceiling=inp.ceiling, secret=secret,
            compute_name=getattr(getattr(ctx, "compute", None), "name", "") or "",
        )

    def mock(self, inp: _In, ctx) -> _Out:
        return _Out(payload=inp.payload, limit=inp.limit, ceiling=inp.ceiling, mocked=True)


async def test_input_reaches_the_operator_validated():
    out = await run_operator_for_test(_Probe, {"payload": "hi"})
    assert out.payload == "hi" and out.limit == 1


async def test_bad_input_fails_at_the_engine_boundary():
    # the harness must NOT pre-build the model — validation is the invoke path's
    # step, so a wrong shape fails the same way it would mid-run.
    with pytest.raises(BindingEvalError):
        await run_operator_for_test(_Probe, {"payload": 1, "limit": "not-an-int"})


async def test_config_layers_under_the_input():
    # explicit input wins; config supplies only what the input left unset.
    out = await run_operator_for_test(_Probe, {"payload": "p", "limit": 7}, config={"limit": 3})
    assert out.limit == 7
    out = await run_operator_for_test(_Probe, {"payload": "p"}, config={"limit": 3})
    assert out.limit == 3


async def test_layer_precedence_is_run_over_workflow_over_profile():
    out = await run_operator_for_test(
        _Probe, {"payload": "p"},
        config={"limit": 1}, workflow_config={"limit": 2}, run_config={"limit": 3},
    )
    assert out.limit == 3
    out = await run_operator_for_test(
        _Probe, {"payload": "p"}, config={"limit": 1}, workflow_config={"limit": 2},
    )
    assert out.limit == 2


async def test_run_supplied_governance_knob_is_stripped():
    # #905 — `ceiling` is governance-scoped: a run cannot set it via the input.
    out = await run_operator_for_test(_Probe, {"payload": "p", "ceiling": 99})
    assert out.ceiling == 10  # schema default, not the run's 99
    # …the profile layer still owns it.
    out = await run_operator_for_test(_Probe, {"payload": "p"}, config={"ceiling": 42})
    assert out.ceiling == 42


async def test_secrets_are_scoped_not_ambient():
    out = await run_operator_for_test(_Probe, {"payload": "p"}, secrets={"TOKEN": "t0k"})
    assert out.secret == "t0k"
    # nothing declared → the operator can read nothing
    out = await run_operator_for_test(_Probe, {"payload": "p"})
    assert out.secret == ""


async def test_ctx_overrides_bind_a_compute_connection():
    from cairn.core.connections import ResolvedConnection

    conn = ResolvedConnection(name="box", kind="compute.ssh", config={"host": "h"})
    out = await run_operator_for_test(
        _Probe, {"payload": "p"}, ctx_overrides={"compute": conn}
    )
    assert out.compute_name == "box"


async def test_unknown_ctx_override_fails_loud():
    with pytest.raises(BindingEvalError, match="not an OperationContext attribute"):
        await run_operator_for_test(_Probe, {"payload": "p"}, ctx_overrides={"nope": 1})


async def test_mocked_takes_the_operators_mock_path():
    out = await run_operator_for_test(_Probe, {"payload": "p"}, mocked=True)
    assert out.mocked is True
