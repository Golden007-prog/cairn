"""data/profile operator — thin node over cairn.services.data.profile_data."""
from __future__ import annotations

import asyncio

from cairn.core.operator import OperationContext
from cairn.services.data import DataExpectation, Role

from operator_data_profile.operators.profile import DataProfileOp
from operator_data_profile.schemas.profile import DataProfileInput


def _ctx() -> OperationContext:
    return OperationContext(pipeline_id="t", node_name="profile")


EXP = DataExpectation(kind="rows", roles=[
    Role(name="input", required=True, aliases=["prompt"]),
    Role(name="output", required=True, aliases=["answer", "completion"]),
    Role(name="reference", required=False, enables="correctness scoring"),
])


def _run(**kw):
    return asyncio.run(DataProfileOp().run(DataProfileInput(expectation=EXP, **kw), _ctx()))


def test_op_pass_inline_rows():
    out = _run(rows=[{"input": "hi", "output": "hello", "reference": "hello"}])
    assert out.ok and out.verdict == "pass" and out.record_count == 1


def test_op_block_missing_required():
    out = _run(rows=[{"input": "hi"}])  # no output/answer/completion
    assert not out.ok and out.verdict == "block" and "output" in out.missing_required


def test_op_degrade_optional_absent():
    out = _run(rows=[{"prompt": "hi", "answer": "hello"}])  # required via alias, no reference
    assert out.verdict == "degrade" and "correctness scoring" in out.degraded


def test_op_loads_from_source(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"input":"hi","output":"hello","reference":"hello"}\n')
    out = _run(source=str(p))
    assert out.ok and out.record_count == 1


def test_op_mock_is_trivial():
    out = DataProfileOp().mock(DataProfileInput(expectation=EXP, rows=[{"a": 1}]), _ctx())
    assert out.ok and out.verdict == "pass"
