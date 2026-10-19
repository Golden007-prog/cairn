"""The contract documentation stays true to the code (docs/CLAUDE.md rule 4).

Two mechanical rules, both high-confidence:
* every ``path:line`` anchor in ``docs/contracts/*.md`` and the architecture front door
  names a file that exists and a line inside it (a moved function fails here, in the PR
  that moved it — never a reader later);
* the entry-point groups the operator/pack contract lists are exactly the groups the code
  declares (``ENTRY_POINT_GROUP`` constants under ``cairn/core``, and under ``cairn/cli``
  for the CLI's own ``cairn.cli`` group, #2421 — the CLI is a surface, ADR-0120 D6).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = sorted((ROOT / "docs" / "contracts").glob("*.md")) + [ROOT / "docs" / "architecture" / "README.md"]
ANCHOR = re.compile(
    r"`((?:src/cairn|packages|console|tests|scripts|deploy|docs|\.github)/[\w./\-]+?\.(?:py|ts|tsx|yaml|yml|toml|md|json))"
    r"[:#](\d+)(?:-(\d+))?`")


def _anchors():
    for doc in DOCS:
        for ln, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for m in ANCHOR.finditer(line):
                yield doc, ln, m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))


@pytest.mark.parametrize("doc,ln,path,start,end", list(_anchors()),
                         ids=lambda v: v.name if isinstance(v, Path) else str(v))
def test_every_contract_doc_anchor_resolves(doc, ln, path, start, end):
    target = ROOT / path
    assert target.is_file(), f"{doc.name}:{ln} cites {path}, which does not exist"
    n = sum(1 for _ in target.open(encoding="utf-8", errors="ignore"))
    assert start <= end <= n, f"{doc.name}:{ln} cites {path}:{start}-{end} but the file has {n} lines"


def test_the_docs_cite_at_least_the_core_contracts():
    cited = {p for _, _, p, _, _ in _anchors()}
    for must in ("src/cairn/core/storage/artifacts.py", "src/cairn/core/storage/protocols.py",
                 "src/cairn/core/storage/pipeline_store.py", "src/cairn/core/engine/pipeline.py",
                 "src/cairn/core/operator/protocol.py", "src/cairn/core/dataflow/contract.py",
                 "src/cairn/core/dataflow/engine.py", "src/cairn/core/policy/pipeline_gates.py"):
        assert must in cited, must


def test_the_entry_point_groups_in_the_pack_contract_match_the_code():
    declared = set()
    for py in [*(ROOT / "src" / "cairn" / "core").rglob("*.py"), *(ROOT / "src" / "cairn" / "cli").rglob("*.py")]:
        declared |= set(re.findall(r'ENTRY_POINT_GROUP = "(cairn\.[a-z_.]+)"', py.read_text(encoding="utf-8", errors="ignore")))
    doc = (ROOT / "docs" / "contracts" / "operators-and-packs.md").read_text(encoding="utf-8")
    section = doc.split("## Entry-point groups")[1].split("## ")[0]
    documented = set(re.findall(r"`(cairn\.[a-z_.]+)`", section))
    assert documented == declared, f"documented {sorted(documented)} vs code {sorted(declared)}"


def test_every_contract_doc_in_the_index_exists():
    index = (ROOT / "docs" / "contracts" / "README.md").read_text(encoding="utf-8")
    for name in re.findall(r"\[([a-z-]+\.md)\]\(([a-z-]+\.md)\)", index):
        assert (ROOT / "docs" / "contracts" / name[1]).is_file(), name[1]
