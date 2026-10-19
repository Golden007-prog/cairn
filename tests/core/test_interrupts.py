"""ADR-0065 / #551 — the typed-interrupt discriminator."""
from __future__ import annotations

from cairn.core.interrupts import (
    KIND_APPROVAL,
    KIND_REMOTE_JOB,
    interrupt_kind,
    is_remote_job_interrupt,
)


def test_explicit_kind_wins():
    assert interrupt_kind({"kind": "remote_job", "remote_job": True}) == KIND_REMOTE_JOB
    assert interrupt_kind({"kind": "approval", "operator": "x"}) == KIND_APPROVAL


def test_legacy_remote_job_key_infers_remote_job():
    # payloads written before ADR-0065 carry only remote_job=True
    assert interrupt_kind({"remote_job": True, "run_id": "r", "node": "n"}) == KIND_REMOTE_JOB


def test_bare_approval_request_is_approval():
    assert interrupt_kind({"operator": "model/register", "risk": "external_write"}) == KIND_APPROVAL


def test_none_and_non_dict_default_to_approval():
    assert interrupt_kind(None) == KIND_APPROVAL
    assert interrupt_kind("whatever") == KIND_APPROVAL


def test_pydantic_payload_is_serialized():
    class _P:
        def model_dump(self, mode="json"):
            return {"kind": "remote_job"}
    assert interrupt_kind(_P()) == KIND_REMOTE_JOB


def test_is_remote_job_helper():
    assert is_remote_job_interrupt({"remote_job": True}) is True
    assert is_remote_job_interrupt({"operator": "x"}) is False
