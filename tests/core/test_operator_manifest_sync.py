"""#1539 parts 1+3 — the operator class generates its own metadata.

`derive_entry` / `render_operator_table` / `sync_pack` turn the Operation
class into the ``[operator.*]`` table + entry-point line; `governance_drift`
extends the shipped #1105 doctor check to the fields it could not compare
(risk / executors / flags / mcp). The real-tree test at the bottom is the
migration prover: every shipped operator pack must be in sync.
"""
from __future__ import annotations

import os
import textwrap
import tomllib
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel

from cairn.core.operator.manifest_sync import (
    check_pack,
    derive_entry,
    entry_drift,
    governance_drift,
    render_operator_table,
    sync_pack,
)
from cairn.core.operator.protocol import Operation
from cairn.core.operator.reports import ReportSpec
from cairn.core.pack.manifest import load_manifest
from tests import _layout


class _In(BaseModel):
    x: int = 0


class _Out(BaseModel):
    y: int = 0


class _TrainOp(Operation[_In, _Out]):
    name = "t1539/train"
    version = "0.3.0"
    capabilities = ["t1539.train"]
    input_schema = _In
    output_schema = _Out
    long_running = True
    requires_gpu = True
    supports_cancel = True
    risk = "paid_compute"
    executors = ["remote_job", "in_process"]
    reports = ReportSpec.NONE  # ADR-0103: required, explicit

    def run(self, inp, ctx):  # pragma: no cover - never invoked
        return _Out()


class _McpOnlyOp(Operation[_In, _Out]):
    name = "t1539/mcp"
    version = "0.1.0"
    input_schema = _In
    output_schema = _Out
    executors = ["mcp_tool"]
    reports = ReportSpec.NONE

    def run(self, inp, ctx):  # pragma: no cover
        return _Out()


def test_derive_entry_reads_every_field_from_the_class():
    e = derive_entry(_TrainOp)
    assert (e.name, e.version) == ("t1539/train", "0.3.0")
    assert (e.input_schema, e.output_schema) == ("_In", "_Out")
    assert e.long_running and e.requires_gpu and e.supports_cancel
    assert not e.supports_mock and not e.requires_approval
    assert e.risk == "paid_compute"
    assert e.executors == ["remote_job", "in_process"]
    assert e.provides_capabilities == ["t1539.train"]


def test_derive_rejects_values_outside_the_locked_enums():
    class BadRisk(_TrainOp):
        name = "t1539/bad"
        risk = "yolo"

    with pytest.raises(ValueError, match="locked enum"):
        derive_entry(BadRisk)

    with pytest.raises(ValueError, match="mcp_server"):
        derive_entry(_McpOnlyOp)  # mcp_tool-only without server/tool named


def test_rendered_table_parses_back_to_the_same_entry():
    text = render_operator_table(_TrainOp)
    raw = tomllib.loads(text)
    tbl = raw["operator"]["t1539/train"]
    assert tbl["risk"] == "paid_compute"
    assert tbl["executors"] == ["remote_job", "in_process"]
    assert tbl["long_running"] is True and tbl["supports_mock"] is False
    assert raw["operator"]["t1539/train"]["capabilities"]["provides"] == ["t1539.train"]
    # defaults-off flags are omitted, not stated
    assert "requires_approval" not in tbl


def test_entry_drift_and_governance_drift_flag_a_stale_table():
    stale = derive_entry(_TrainOp)
    fresh = derive_entry(_TrainOp)
    assert entry_drift(stale, fresh) == []
    assert governance_drift(stale, _TrainOp) == []

    class Widened(_TrainOp):
        risk = "read_only"
        executors = ["in_process"]

    assert any("risk" in d for d in governance_drift(stale, Widened))
    assert any("executors" in d for d in governance_drift(stale, Widened))


def test_governance_drift_treats_missing_risk_as_the_default():
    e = derive_entry(_TrainOp)
    object.__setattr__(e, "risk", None)  # frozen dataclass; simulate an old table
    assert any("risk" in d for d in governance_drift(e, _TrainOp))

    class DefaultOp(_TrainOp):
        risk = "read_only"
        executors = ["in_process"]
        long_running = False
        requires_gpu = False
        supports_cancel = False

    e2 = derive_entry(DefaultOp)
    object.__setattr__(e2, "risk", None)
    object.__setattr__(e2, "executors", [])
    assert governance_drift(e2, DefaultOp) == []  # omitted == default: no drift


# ─── sync round-trip on a scratch pack ─────────────────────────────────


def _scratch_pack(tmp_path: Path) -> Path:
    pd = tmp_path / "opdemo"
    (pd / "src" / "operator_t1539demo").mkdir(parents=True)
    (pd / "pack.toml").write_text(textwrap.dedent("""\
        # narrative header comment — must survive a sync
        [pack]
        name        = "obs-operator-t1539demo"
        version     = "0.1.0"
        type        = "operator"
        description = "sync round-trip fixture"
    """))
    (pd / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "operator-t1539demo"
        version = "0.1.0"
    """))
    (pd / "src" / "operator_t1539demo" / "__init__.py").write_text("")
    (pd / "src" / "operator_t1539demo" / "op.py").write_text(textwrap.dedent("""\
        from pydantic import BaseModel
        from cairn.core.operator.protocol import Operation
        from cairn.core.operator.registry import register_operator
        from cairn.core.operator.reports import ReportSpec


        class DemoIn(BaseModel):
            x: int = 0


        class DemoOut(BaseModel):
            y: int = 0


        @register_operator(name="t1539demo/do", version="0.2.0",
                           capabilities=["t1539demo.do"])
        class DemoOp(Operation[DemoIn, DemoOut]):
            input_schema = DemoIn
            output_schema = DemoOut
            risk = "external_write"
            requires_approval = True
            reports = ReportSpec.NONE  # ADR-0103: a new operator must declare

            def run(self, inp, ctx):
                return DemoOut()
    """))
    return pd


def test_sync_writes_table_and_entry_point_then_check_is_clean(tmp_path, monkeypatch):
    pd = _scratch_pack(tmp_path)
    monkeypatch.syspath_prepend(str(pd / "src"))

    first = check_pack(pd)
    assert first.missing_tables == ["t1539demo/do"]
    assert first.missing_entry_points == [
        '"t1539demo/do" = "operator_t1539demo.op:DemoOp"']

    result = sync_pack(pd)
    assert result.clean, (result.missing_tables, result.drifted,
                          result.missing_entry_points, result.import_problems)

    toml_text = (pd / "pack.toml").read_text()
    assert toml_text.startswith("# narrative header comment")
    m = load_manifest(pd / "pack.toml")
    entry = m.operators["t1539demo/do"]
    assert entry.risk == "external_write" and entry.requires_approval
    assert entry.version == "0.2.0"

    raw = tomllib.loads((pd / "pyproject.toml").read_text())
    assert raw["project"]["entry-points"]["cairn.operators"]["t1539demo/do"] \
        == "operator_t1539demo.op:DemoOp"


def test_sync_rewrites_a_hand_staled_table_in_place(tmp_path, monkeypatch):
    pd = _scratch_pack(tmp_path)
    monkeypatch.syspath_prepend(str(pd / "src"))
    sync_pack(pd)

    stale = (pd / "pack.toml").read_text().replace(
        'risk      = "external_write"', 'risk      = "read_only"')
    (pd / "pack.toml").write_text(stale)
    report = check_pack(pd)
    assert "t1539demo/do" in report.drifted
    assert any("risk" in d for d in report.drifted["t1539demo/do"])

    fixed = sync_pack(pd)
    assert fixed.clean
    assert load_manifest(pd / "pack.toml").operators["t1539demo/do"].risk \
        == "external_write"


# ─── the migration prover: every SHIPPED pack is in sync ───────────────


def test_every_shipped_operator_pack_is_in_sync():
    """The acceptance line of #1539: a hand-edited stale pack.toml fails the
    check — so the shipped tree must pass it. A pack whose wheel is not
    installed is skipped (same stance as the #1105 drift check)."""
    packs = _layout.operator_packs()
    assert packs, "no operator packs found"
    failures = []
    for pd in packs:
        r = check_pack(pd)
        if r.import_problems and all("ModuleNotFound" in p for p in r.import_problems):
            continue  # pack not installed in this env — the doctor's warn case
        if not r.clean:
            failures.append((pd.name, r.missing_tables, dict(r.drifted),
                             r.missing_entry_points, r.import_problems))
    assert not failures, failures


# ── #2241 B: `implements` is a drift-gated manifest field ───────────────────

class _OptedInOp(_TrainOp):
    name = "t2241/train"
    implements = ("contract://training.sft.adapter/1.0.0", "contract://training.sft.adapter/1.1.0")


def test_implements_round_trips_through_the_rendered_table():
    block = render_operator_table(_OptedInOp)
    assert 'implements = ["contract://training.sft.adapter/1.0.0", "contract://training.sft.adapter/1.1.0"]' in block
    assert "implements" not in render_operator_table(_TrainOp)       # legacy: nothing emitted
    parsed = tomllib.loads(block)["operator"]["t2241/train"]
    assert parsed["implements"] == list(_OptedInOp.implements)
    assert any(d.startswith("implements:") for d in entry_drift(derive_entry(_OptedInOp), derive_entry(_TrainOp)))
    assert entry_drift(derive_entry(_OptedInOp), derive_entry(_OptedInOp)) == []


@pytest.mark.parametrize("value", ['["contract://a.b/1"]', '["contract://a.b/1.0.0", "contract://a.c/1.0.0"]', '"contract://a.b/1.0.0"'])
def test_manifest_refuses_a_bad_implements_declaration(tmp_path, value):
    (tmp_path / "pack.toml").write_text(textwrap.dedent(f'''
        [pack]
        name = "t2241"
        version = "0.1.0"
        description = "x"
        kind = "operator"
        [operator."t2241/op"]
        version = "0.1.0"
        implements = {value}
    '''))
    with pytest.raises(Exception, match="implements"):
        load_manifest(tmp_path / "pack.toml")
