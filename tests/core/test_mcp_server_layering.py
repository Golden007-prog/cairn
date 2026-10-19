"""C-6: services never import packs (root ``CLAUDE.md`` §3, ``docs/adr/README.md`` C-6).

Every module under ``cairn/services/`` is parsed with ``ast`` and each import statement
(module top or function-local, absolute or relative) is checked: none may reach a domain
pack (``cairn.packs.*``) or an operator pack (a top-level ``operator_*`` package). A
service that needs a pack-provided capability resolves it by ref through a core registry,
never by naming the pack at author time.

The file keeps its historical name: it is the named C-6 evidence. It used to guard only
``services/mcp/server.py``, which was retired in #2425; it now covers every services
module.

``RECORDED_EXCEPTIONS`` is the only way past the rule. The owner approved at most one
entry, ``cairn/services/rag/inject.py`` (risk M4, #2425), but that file no longer imports
an operator pack: since #2415 it resolves ``rag/search`` by ref through the operator
registry, which this rule allows. So the list is empty, and it must never grow beyond that
one approved entry. ``test_c6_rule_can_fail`` shows the gate rejects offending source.
"""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

from tests import _layout

SERVICES = _layout.ENGINE / "services"

#: (services file, imported module prefix) -> issue / risk reference. Never widen: the
#: only entry the owner approved (#2425) is ``cairn/services/rag/inject.py`` for risk M4,
#: and that site is gone (#2415), so nothing is recorded.
RECORDED_EXCEPTIONS: dict[tuple[str, str], str] = {}
_APPROVED_EXCEPTION_FILES = {"cairn/services/rag/inject.py"}  # #2425, risk M4


def _imported_modules(src: str, rel: str) -> list[tuple[str, int]]:
    """Every module an import statement in ``src`` reaches, with its line number."""
    package = ".".join(Path(rel).with_suffix("").parts[:-1])
    out: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            if node.module:
                out.append((module, node.lineno))
            # `from cairn import packs` must not hide the pack behind its parent package.
            out.extend((f"{module}.{a.name}", node.lineno) for a in node.names if a.name != "*")
    return out


def _forbidden(module: str) -> bool:
    return (module == "cairn.packs" or module.startswith("cairn.packs.")
            or module.split(".")[0].startswith("operator_"))


def _violations(src: str, rel: str) -> list[str]:
    bad = []
    for module, line in _imported_modules(src, rel):
        if not _forbidden(module):
            continue
        if any(rel == f and (module == m or module.startswith(m + ".")) for f, m in RECORDED_EXCEPTIONS):
            continue
        bad.append(f"{rel}:{line} imports {module}")
    return bad


def test_no_services_module_imports_a_pack():
    files = sorted(SERVICES.rglob("*.py"))
    assert len(files) > 20, "the scan found too few services modules to mean anything"
    bad: list[str] = []
    for path in files:
        bad.extend(_violations(path.read_text(encoding="utf-8"), _layout.rel(path)))
    assert not bad, (
        "services must not import a domain or operator pack (C-6); resolve the capability "
        "by ref through a core registry instead:\n" + "\n".join(f"  {b}" for b in bad))


def test_recorded_exceptions_never_widen():
    assert {f for f, _ in RECORDED_EXCEPTIONS} <= _APPROVED_EXCEPTION_FILES, (
        "C-6 exceptions are owner-approved; only cairn/services/rag/inject.py (#2425, M4) was")
    assert len(RECORDED_EXCEPTIONS) <= 1


@pytest.mark.parametrize("rel, src", [
    ("cairn/services/llm/new.py", "from cairn.packs.x import y\n"),
    ("cairn/services/llm/new.py", "import cairn.packs.x\n"),
    ("cairn/services/llm/new.py", "def f():\n    from cairn.packs.x import y\n"),
    ("cairn/services/llm/new.py", "from cairn import packs\n"),
    ("cairn/services/llm/new.py", "from ...packs import x\n"),
    ("cairn/services/rag/new.py", "def f():\n    import operator_rag_local.operators.search\n"),
    ("cairn/services/rag/inject.py", "def f():\n    from operator_rag_local import search\n"),
])
def test_c6_rule_can_fail(rel, src):
    """Synthetic offending source: the rule must flag it (a gate that cannot fail is not a gate)."""
    assert _violations(src, rel), f"C-6 did not flag {rel}: {src!r}"


def test_c6_rule_allows_core_and_services_imports():
    src = "from cairn.core.operator.registry import default_registry\nfrom . import roles\n"
    assert _violations(src, "cairn/services/rag/new.py") == []
