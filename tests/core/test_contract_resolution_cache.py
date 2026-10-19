"""The installed contract resolution is kept for the process (maintainer 2026-10-11, "1a").

A full discovery lists every file of every installed distribution and validates every body
(about 2 s); the launch door, every leg and every opted-in stage asked for it again. The kept
result is reused only while nothing that decides it changed: no distribution installed,
upgraded or removed, no contract file edited in place. A refusal is never kept; an explicit
``distributions`` always resolves afresh; a body handed to a caller is a copy.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from cairn.core.pack import contracts
from cairn.core.pack import registry as pack_registry
from cairn.core.pack.contracts import ContractDiscoveryError, resolve_contracts
from tests.core.test_contract_discovery import alpha, beta


@pytest.fixture
def counted(monkeypatch):
    """Count full resolutions; start from an empty memo."""
    calls = []
    real = contracts._resolve_contracts

    def _spy(distributions):
        calls.append(distributions)
        return real(distributions)
    monkeypatch.setattr(contracts, "_resolve_contracts", _spy)
    monkeypatch.setattr(contracts, "_INSTALLED", None)
    return calls


def test_an_unchanged_installation_resolves_once(tmp_path, monkeypatch, counted):
    a, b = alpha(tmp_path), beta(tmp_path)
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [b, a])
    first = resolve_contracts()
    assert resolve_contracts() is first and pack_registry._closure_bodies() is pack_registry._closure_bodies()
    assert len(counted) == 1


def test_an_install_or_removal_resolves_again(tmp_path, monkeypatch, counted):
    a, b = alpha(tmp_path), beta(tmp_path)
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [a])
    assert [d.name for d in resolve_contracts()] == ["alpha-contracts"]
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [b, a])
    assert [d.name for d in resolve_contracts()] == ["alpha-contracts", "beta-contracts"]
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [b])
    with pytest.raises(ContractDiscoveryError):   # beta without its dependency refuses, every time
        resolve_contracts()
    with pytest.raises(ContractDiscoveryError):
        resolve_contracts()
    assert len(counted) == 4, "a refusal is never kept"


def test_an_in_place_edit_of_an_installed_definition_resolves_again(tmp_path, monkeypatch, counted):
    a, b = alpha(tmp_path), beta(tmp_path)
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [b, a])
    before = {x.id: x.digest for d in resolve_contracts() for x in d.definitions}
    data = tmp_path / "alpha-contracts-1.0.0" / "cairn_contracts" / "alpha-contracts" / "data.json"
    data.write_text(data.read_text().replace('"integer"', '"number" '))
    os.utime(data, ns=(1, 1))                      # a different stamp even on a coarse clock
    after = {x.id: x.digest for d in resolve_contracts() for x in d.definitions}
    assert len(counted) == 2 and after["alpha.data"] != before["alpha.data"]


def test_explicit_distributions_always_resolve_afresh(tmp_path, monkeypatch, counted):
    a = alpha(tmp_path)
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [a])
    resolve_contracts()
    resolve_contracts(distributions=[a])
    resolve_contracts(distributions=[a])
    assert len(counted) == 3


def test_a_caller_never_mutates_the_kept_bodies(tmp_path, monkeypatch, counted):
    a, b = alpha(tmp_path), beta(tmp_path)
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [b, a])
    defs = pack_registry.invocation_definitions("contract://beta.run/1.0.0")["definitions"]
    pairs = [(f"contract://{d['id']}/{d['revision']}", d["digest"]) for d in defs]
    closure = [(f"contract://{d['id']}/{d['revision']}", d["digest"])
               for d in pack_registry.contract_resolution_snapshot()["definitions"]]
    envelope = pack_registry.invocation_contract(pairs, closure)
    for schema in envelope["schemas"].values():
        schema.clear()                             # a careless consumer
    again = pack_registry.invocation_contract(pairs, closure)
    assert all(again["schemas"].values()) and len(counted) == 1


def test_an_upgrade_a_partial_removal_and_a_same_version_reinstall_resolve_again(tmp_path, monkeypatch, counted):
    a, b = alpha(tmp_path), beta(tmp_path)
    a2 = alpha(tmp_path / "v2", version="1.0.1")
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [a])
    resolve_contracts()
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [a2])          # upgrade
    assert [d.version for d in resolve_contracts()] == ["1.0.1"]
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [b, a])
    resolve_contracts()
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [a])           # removal, still valid
    assert [d.name for d in resolve_contracts()] == ["alpha-contracts"]
    record = next(pathlib.Path(a._path).glob("RECORD"))
    os.utime(record, ns=(1, 1))                                                       # reinstalled in place
    resolve_contracts()
    assert len(counted) == 5


def test_a_file_edited_while_it_was_being_resolved_is_not_kept(tmp_path, monkeypatch, counted):
    a = alpha(tmp_path)
    monkeypatch.setattr(contracts, "installed_distributions", lambda: [a])
    data = tmp_path / "alpha-contracts-1.0.0" / "cairn_contracts" / "alpha-contracts" / "data.json"
    real = contracts._read_bodies

    def _read_then_edit(entries):
        bodies = real(entries)
        data.write_text(data.read_text().replace('"integer"', '"number" '))   # lands mid-resolution
        return bodies
    monkeypatch.setattr(contracts, "_read_bodies", _read_then_edit)
    resolve_contracts()
    monkeypatch.setattr(contracts, "_read_bodies", real)
    resolve_contracts()
    assert len(counted) == 2, "a result whose files changed under it must not be kept"
