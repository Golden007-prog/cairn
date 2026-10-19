"""Compiler integration tests for OperationBTNode (OP3 #182).

Verifies:
  - A state_machine template with an `operation` BT node compiles.
  - Missing binding file fails LOUD at compile time.
  - Unregistered operator reference fails LOUD at compile time.
  - End-to-end invocation: state flows through operator.run() and back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from cairn.core.engine.compiler import compile_template
from cairn.core.operator import (
    Operation,
    OperationBindingError,
    OperationContext,
    OperatorRegistry,
    UnknownOperatorError,
)
from cairn.core.operator import default_registry as op_registry
from cairn.core.operator.binding import BindingError


# ── Operator under test ──────────────────────────────────────────────────


class _EchoIn(BaseModel):
    message: str


class _EchoOut(BaseModel):
    echoed: str


@pytest.fixture
def echo_operator():
    """Register an echo operator into the default registry for the test,
    then deregister to keep state clean."""
    # Use the registry's clear-then-restore pattern by tracking what was there.
    snapshot = op_registry.all()

    class EchoOp(Operation[_EchoIn, _EchoOut]):
        name = "test/echo"
        version = "0.1.0"
        input_schema = _EchoIn
        output_schema = _EchoOut

        def run(self, inp, ctx: OperationContext):
            return _EchoOut(echoed=f"ECHO:{inp.message}")

    op_registry.register(EchoOp)
    try:
        yield EchoOp
    finally:
        op_registry.clear()
        for name, cls in snapshot.items():
            op_registry.register(cls)


# ── Pack fixture builder ─────────────────────────────────────────────────


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _make_demo_template(tmp_path: Path) -> Path:
    """Build a tiny pack: templates/demo.md + operations/echo.yaml.

    The output binding targets ``final_output`` because the state_machine
    state schema (:class:`IncidentMachineState`) explicitly declares it.
    LangGraph's StateGraph silently drops unknown keys, so operations
    in real packs must target state keys the template declared.
    """
    # operations/echo.yaml
    _write(
        tmp_path,
        "operations/echo.yaml",
        "operator: test/echo\n"
        "input:\n"
        "  message: $state.symptom\n"
        "output:\n"
        "  echoed: $state.final_output\n",
    )
    # templates/demo.md — minimal state_machine using ONE operation BT node
    tmpl = _write(
        tmp_path,
        "templates/demo.md",
        """---
name: demo
version: 1.0.0
description: tiny op demo
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    ref: echo
  nodes: {}
---

# Demo template

Demonstrates OP3 OperationBTNode compile + execute.
""".strip(),
    )
    return tmpl


# ── Tests ────────────────────────────────────────────────────────────────


def test_template_with_operation_node_compiles(tmp_path, echo_operator):
    tmpl = _make_demo_template(tmp_path)
    compiled = compile_template(tmpl)
    assert compiled.name == "demo"
    # state_machine graph built; one operation node landed
    # (we don't assert exact internal node names — just that compile succeeded)


def test_unknown_operator_ref_fails_at_compile_time(tmp_path, echo_operator):
    """If the binding's operator isn't registered, compile fails LOUD."""
    # operations/missing.yaml references an unregistered operator
    _write(
        tmp_path,
        "operations/missing.yaml",
        "operator: never/registered\n"
        "input:\n  x: 1\n"
        "output:\n  y: $state.y\n",
    )
    tmpl = _write(
        tmp_path,
        "templates/broken.md",
        """---
name: broken
version: 1.0.0
description: broken
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    ref: missing
  nodes: {}
---

# broken
""".strip(),
    )
    with pytest.raises(UnknownOperatorError) as ei:
        compile_template(tmpl)
    msg = str(ei.value)
    assert "never/registered" in msg
    assert "missing" in msg  # the binding ref is in the error


def test_missing_binding_file_fails_at_compile_time(tmp_path, echo_operator):
    """No operations/<ref>.yaml → compile fails loud."""
    tmpl = _write(
        tmp_path,
        "templates/orphan.md",
        """---
name: orphan
version: 1.0.0
description: orphan
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    ref: nonexistent
  nodes: {}
---

# orphan
""".strip(),
    )
    with pytest.raises(BindingError) as ei:
        compile_template(tmpl)
    msg = str(ei.value)
    assert "nonexistent" in msg
    assert "not found" in msg


def test_end_to_end_operation_node_runs(tmp_path, echo_operator):
    """Invoking the compiled template runs the operator and updates state."""
    import asyncio

    tmpl = _make_demo_template(tmp_path)
    compiled = compile_template(tmpl)
    final = asyncio.run(compiled.run({"symptom": "alert: payment failures"}))
    # The echo operator's output flowed into state via $state.final_output;
    # the engine post-processing surfaces it under the `output` key.
    assert final.get("output") == "ECHO:alert: payment failures"


# ── SCH4 #192: compile-time binding validation ───────────────────────────


def test_binding_missing_required_input_fails_at_compile_time(
    tmp_path, echo_operator
):
    """The echo operator declares ``message`` as required; a binding that
    omits it must fail at compile time, not at first run."""
    _write(
        tmp_path,
        "operations/badin.yaml",
        # No 'message' field bound — required field omitted
        "operator: test/echo\n"
        "input: {}\n"
        "output:\n  echoed: $state.final_output\n",
    )
    tmpl = _write(
        tmp_path,
        "templates/badin.md",
        """---
name: badin
version: 1.0.0
description: missing required input
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    ref: badin
  nodes: {}
---

# badin
""".strip(),
    )
    with pytest.raises(OperationBindingError) as ei:
        compile_template(tmpl)
    msg = str(ei.value)
    assert "message" in msg
    assert "required input field" in msg
    assert "test/echo" in msg


def test_binding_unknown_output_field_fails_at_compile_time(
    tmp_path, echo_operator
):
    """Capturing an operator output field that doesn't exist on the
    operator's output_schema must surface at compile time."""
    _write(
        tmp_path,
        "operations/badout.yaml",
        "operator: test/echo\n"
        "input:\n  message: $state.symptom\n"
        # 'never_emitted' is not on _EchoOut
        "output:\n  never_emitted: $state.final_output\n",
    )
    tmpl = _write(
        tmp_path,
        "templates/badout.md",
        """---
name: badout
version: 1.0.0
description: unknown output capture
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    ref: badout
  nodes: {}
---

# badout
""".strip(),
    )
    with pytest.raises(OperationBindingError) as ei:
        compile_template(tmpl)
    msg = str(ei.value)
    assert "never_emitted" in msg
    assert "unknown field" in msg


# ── #2241 C: the recorded contract reaches a template operation node ─────────
from cairn.core.engine.workflow_config import collect_template_operation_nodes  # noqa: E402
from cairn.core.operator.binding import BindingError  # noqa: E402, F811 — re-import keeps the section self-contained
from cairn.core.operator.effective import ContractConformanceError  # noqa: E402

_CAP = "contract://test.echo/1.0.0"


def _echo_envelope(version="0.1.0", output_required=("echoed",)):
    return {"capability_ref": _CAP, "operator_ref": "test/echo", "operator_version": version,
            "refs": {"input": "contract://t.in/1.0.0", "configuration": "contract://t.cfg/1.0.0", "output": "contract://t.out/1.0.0"},
            "schemas": {"contract://t.in/1.0.0": {"type": "object", "required": ["message"]},
                        "contract://t.cfg/1.0.0": {"type": "object"},
                        "contract://t.out/1.0.0": {"type": "object", "required": list(output_required)}}}


def _admitted(symptom, **entries):
    return {"symptom": symptom, "__contract_invocations__": {"__leg__": "L", **entries}}


def test_the_node_walk_names_each_operation_node(tmp_path, echo_operator):
    assert collect_template_operation_nodes(_make_demo_template(tmp_path)) == [("echo", "test/echo")]


async def test_a_conformance_refusal_fails_the_template_run_not_a_finding(tmp_path, echo_operator):
    """D1 — the #568 degrade-to-finding policy does not apply to a shared-contract refusal."""
    echo_operator.implements = (_CAP,)
    compiled = compile_template(_make_demo_template(tmp_path))
    with pytest.raises(ContractConformanceError, match="output"):
        await compiled.run(_admitted("hi", echo=_echo_envelope(output_required=("missing",))))
    assert (await compiled.run(_admitted("hi", echo=_echo_envelope())))["final_output"] == "ECHO:hi"


async def test_an_admitted_leg_without_the_nodes_entry_refuses_but_the_dev_loop_is_legacy(tmp_path, echo_operator):
    """D3 — a stamped channel with no entry for an opted-in node is lost provenance."""
    echo_operator.implements = (_CAP,)
    compiled = compile_template(_make_demo_template(tmp_path))
    with pytest.raises(ContractConformanceError, match="admitted with no entry"):
        await compiled.run(_admitted("hi"))
    assert (await compiled.run({"symptom": "hi"}))["final_output"] == "ECHO:hi"


async def test_a_legacy_operator_in_an_admitted_leg_is_unchanged(tmp_path, echo_operator):
    compiled = compile_template(_make_demo_template(tmp_path))
    assert (await compiled.run(_admitted("hi")))["final_output"] == "ECHO:hi"


def _make_v1_template(tmp_path):
    return _write(tmp_path, "templates/demo_v1.md", """---
name: demo_v1
version: 1.0.0
description: v1 inline capability form
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    id: echo
    capability: contract://test.echo/1.0.0
    input:
      message: $state.symptom
    output:
      echoed: $state.final_output
  nodes: {}
---
v1 form.
""".strip())


async def test_a_capability_node_resolves_through_the_profile_and_equals_the_operator_form(tmp_path, echo_operator, monkeypatch):
    """D5 — same operator, `operator:` file vs `capability:` node → the same patch."""
    import cairn.core.pack.profile as PF
    from cairn.core.pack.profile import Profile

    echo_operator.implements = (_CAP,)
    monkeypatch.setattr(PF, "load_profile_from_env", lambda: Profile(bindings={"test.echo": "test/echo"}))
    legacy = compile_template(_make_demo_template(tmp_path))
    v1 = compile_template(_make_v1_template(tmp_path))
    seeded = _admitted("hi", echo=_echo_envelope())
    assert (await v1.run(seeded))["final_output"] == (await legacy.run(seeded))["final_output"] == "ECHO:hi"
    assert collect_template_operation_nodes(tmp_path / "templates/demo_v1.md") == [("echo", "test/echo")]
    # the bound operator must implement the exact revision; nothing falls back
    echo_operator.implements = ("contract://test.echo/2.0.0",)
    with pytest.raises(BindingError, match="not 'contract://test.echo/1.0.0'"):
        compile_template(_make_v1_template(tmp_path))
    monkeypatch.setattr(PF, "load_profile_from_env", lambda: Profile())
    with pytest.raises(BindingError, match="no \\[profile.bindings\\] entry"):
        compile_template(_make_v1_template(tmp_path))


async def test_the_entry_reaches_an_operation_node_inside_a_subgraph(tmp_path, echo_operator, monkeypatch):
    """D2 — the parent's channel, re-keyed under the subgraph node, seeds the child; with
    the entry present the child node runs under its contract (and refuses when it fails)."""
    from cairn.core.engine import skill_engine

    echo_operator.implements = (_CAP,)
    child = _make_demo_template(tmp_path)                       # templates/demo.md, root node ref "echo"
    parent = _write(tmp_path, "templates/parent.md", """---
name: parent
version: 1.0.0
description: parent with a subgraph
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: subgraph_node
    template: demo
  nodes: {}
---
parent.
""".strip())
    monkeypatch.setattr(skill_engine, "resolve_template_path", lambda name, *, from_pack=None: child if name == "demo" else tmp_path / f"templates/{name}.md")
    compiled = compile_template(parent)
    with pytest.raises(ContractConformanceError, match="output"):
        await compiled.run(_admitted("hi", **{"demo/echo": _echo_envelope(output_required=("missing",))}))
    with pytest.raises(ContractConformanceError, match="admitted with no entry"):
        await compiled.run(_admitted("hi"))                     # stamped parent, entry lost → refuse, never legacy
    assert collect_template_operation_nodes(parent) == [("demo/echo", "test/echo")]


# ── review round 2: the three propagation paths, configuration, inline elaboration ──
from cairn.core.engine.compiler import _node_contract  # noqa: E402
from cairn.core.operator.effective import ContractEnvelope, effective_parameters  # noqa: E402


def test_lookups_are_exact_so_a_legacy_node_never_inherits_a_childs_entry(echo_operator):
    """Review R1 — a parent's legacy node `echo` beside a child's opted-in `demo/echo`:
    the legacy node gets None (runs as before); the opted-in child reads its own key; a
    malformed recorded entry is a refusal, never a finding."""
    channel = {"__leg__": "L", "demo/echo": _echo_envelope()}
    assert _node_contract({"contract_invocations": channel}, "echo", echo_operator) is None       # legacy, exact miss
    assert _node_contract({"contract_invocations": channel}, "demo/echo", echo_operator).capability_ref == _CAP
    echo_operator.implements = (_CAP,)
    with pytest.raises(ContractConformanceError, match="admitted with no entry"):
        _node_contract({"contract_invocations": channel}, "echo", echo_operator)                  # opted-in, lost entry
    with pytest.raises(ContractConformanceError, match="malformed"):
        _node_contract({"contract_invocations": {"__leg__": "L", "echo": {"capability_ref": _CAP}}}, "echo", echo_operator)


async def test_a_map_body_subgraph_receives_the_entries_keyed_by_its_template(tmp_path, echo_operator, monkeypatch):
    from cairn.core.engine.python_function_registry import python_function
    from cairn.core.engine import skill_engine

    @python_function("_c_seed")
    def _c_seed(state):
        return {"findings": {"items": ["a"]}}

    echo_operator.implements = (_CAP,)
    child = _make_demo_template(tmp_path)
    parent = _write(tmp_path, "templates/mapper.md", """---
name: mapper
version: 1.0.0
description: map over a subgraph
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: sequence
    children:
      - {type: python_node, function: _c_seed}
      - type: map
        over: items
        as: item
        body:
          type: subgraph_node
          template: demo
  nodes: {}
---
mapper.
""".strip())
    monkeypatch.setattr(skill_engine, "resolve_template_path", lambda name, *, from_pack=None: child if name == "demo" else tmp_path / f"templates/{name}.md")
    compiled = compile_template(parent)
    with pytest.raises(ContractConformanceError, match="output"):
        await compiled.run(_admitted("hi", **{"demo/echo": _echo_envelope(output_required=("missing",))}))


def test_the_walk_records_a_child_reached_under_two_prefixes(tmp_path, echo_operator, monkeypatch):
    """A diamond/repeat: C under the parent and C under A are two sets of entries."""
    from cairn.core.engine import skill_engine

    child = _make_demo_template(tmp_path)                       # "demo", node "echo"
    def tmpl(name, root):
        return _write(tmp_path, f"templates/{name}.md", f"""---
name: {name}
version: 1.0.0
description: x
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
{root}
  nodes: {{}}
---
x.
""".strip())
    a = tmpl("a", "    type: subgraph_node\n    template: demo")
    parent = tmpl("parent", "    type: sequence\n    children:\n      - {type: subgraph_node, template: demo}\n      - {type: subgraph_node, template: a}")
    files = {"demo": child, "a": a, "parent": parent}
    monkeypatch.setattr(skill_engine, "resolve_template_path", lambda name, *, from_pack=None: files[name])
    assert collect_template_operation_nodes(parent) == [("demo/echo", "test/echo"), ("a/demo/echo", "test/echo")]


def test_v1_configuration_is_validated_against_the_configuration_schema(echo_operator):
    """The configuration the OPERATOR sees — layered config overlaid by config fields the
    input supplies (where a v1 `configuration:` lands) — is what the shared schema judges."""
    from pydantic import BaseModel

    class _Cfg(BaseModel):
        epochs: int = 1

    class _In(BaseModel):
        message: str
        epochs: int = 1

    echo_operator.input_schema = _In
    echo_operator.config_schema = _Cfg
    env = _echo_envelope()
    env["schemas"]["contract://t.cfg/1.0.0"] = {"type": "object", "properties": {"epochs": {"maximum": 10}}}
    contract = ContractEnvelope.from_dict(env)
    effective_parameters(echo_operator, {"message": "m", "epochs": 3}, contract=contract)
    with pytest.raises(ContractConformanceError, match="configuration/epochs"):
        effective_parameters(echo_operator, {"message": "m", "epochs": 99}, contract=contract)


async def test_an_inline_v1_node_without_output_keeps_its_producer_named_channel(tmp_path, echo_operator, monkeypatch):
    import cairn.core.pack.profile as PF
    from cairn.core.pack.profile import Profile

    echo_operator.implements = (_CAP,)
    monkeypatch.setattr(PF, "load_profile_from_env", lambda: Profile(bindings={"test.echo": "test/echo"}))
    legacy = _write(tmp_path, "templates/inl.md", _make_v1_template(tmp_path).read_text()
                    .replace("capability: contract://test.echo/1.0.0", "operator: test/echo").replace("name: demo_v1", "name: inl"))
    v1 = _make_v1_template(tmp_path)
    for path in (legacy, v1):
        text = path.read_text().replace("    output:\n      echoed: $state.final_output\n", "")
        path.write_text(text)
    out_legacy = await compile_template(legacy).run({"symptom": "hi"})
    out_v1 = await compile_template(v1).run({"symptom": "hi"})
    assert out_v1.get("echo") == out_legacy.get("echo") and out_legacy.get("echo"), out_legacy
