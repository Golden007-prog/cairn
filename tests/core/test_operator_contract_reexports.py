"""ADR-0120 D7 / #2453 (group A): the public operator contract re-exports the helpers and
record types an operator pack legitimately needs, so a pack imports only
``cairn.core.operator``. Each re-export is the SAME object as its source (lazy, PEP 562,
because the sources import the operator package), and the dataflow contract is the only
other root an operator pack may import (group D)."""
from __future__ import annotations

import importlib

import pytest

import cairn.core.operator as op


@pytest.mark.parametrize("name, source", sorted(op._CONTRACT_REEXPORTS.items()))
def test_each_reexport_is_its_source_object(name, source):
    module, attr = source
    assert getattr(op, name) is getattr(importlib.import_module(module), attr)
    assert name in op.__all__ and name in dir(op)


def test_an_unknown_name_is_still_an_attribute_error():
    with pytest.raises(AttributeError):
        op.no_such_contract_name  # noqa: B018 — the access is the test


def test_the_artifact_registry_is_not_the_context_artifact_store():
    """Two protocols share a source name; the contract keeps them apart."""
    assert op.ArtifactRegistry is not op.ArtifactStore
    assert hasattr(op.ArtifactRegistry, "register") and hasattr(op.ArtifactStore, "put")
