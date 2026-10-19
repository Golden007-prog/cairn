"""#2417: SSH dispatch without the [ssh] extra is a clear EngineError naming the
extra, not an ImportError traceback — and only the extra's own absence is
translated, so a genuine fault inside an installed pack still surfaces."""
from __future__ import annotations

import asyncio
import sys

import pytest

from cairn.core.dataflow import DataflowSpec, EngineError, EngineJobHandle, StageRef

from operator_dataflow import engines_ssh
from operator_dataflow.engines_ssh import SSH_EXTRA_MISSING, BuiltinSshEngine


@pytest.fixture
def no_operator_remote(monkeypatch):
    """Make ``operator_remote`` unimportable, as on an install without [ssh]."""
    for name in [m for m in sys.modules if m == "operator_remote" or m.startswith("operator_remote.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "operator_remote", None)


def test_submit_without_the_extra_names_it(no_operator_remote):
    eng = BuiltinSshEngine("fixbox")
    spec = DataflowSpec(stages=[StageRef(stage="noop")])
    ctx = type("C", (), {"pipeline_id": "run-1", "node_name": "clean"})()
    with pytest.raises(EngineError) as ei:
        asyncio.run(eng.submit(spec, ctx))
    assert str(ei.value) == SSH_EXTRA_MISSING
    assert "cairndev[ssh]" in str(ei.value) and "cairn-operator-" not in str(ei.value)  # ADR-0137 D5
    assert isinstance(ei.value.__cause__, ModuleNotFoundError)


def test_poll_of_an_ssh_handle_without_the_extra_names_it(no_operator_remote):
    handle = EngineJobHandle(engine="builtin-ssh", job_id="box:/jobs/x", meta={})
    with pytest.raises(EngineError, match=r"cairndev\[ssh\]"):
        asyncio.run(BuiltinSshEngine("fixbox").poll(handle))


def test_an_unrelated_missing_module_is_not_relabelled():
    engines_ssh._raise_if_ssh_extra_missing(ModuleNotFoundError("x", name="numpy"))
    with pytest.raises(EngineError):
        engines_ssh._raise_if_ssh_extra_missing(
            ModuleNotFoundError("x", name="operator_remote.operators"))
    with pytest.raises(EngineError):
        engines_ssh._raise_if_ssh_extra_missing(ModuleNotFoundError("x", name="asyncssh"))
