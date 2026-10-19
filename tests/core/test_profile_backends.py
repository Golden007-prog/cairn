"""Self-describing backend schema + resolver (ADR-0052 §1, epic #301 / #302).

Pins the two contracts the rest of the configurator hangs off:
  - the schema is well-formed and JSON-serializable (the form renders from it);
  - `effective_backends` resolves profile → env → default, honestly (never raises,
    zero-config defaults hold, url-kind envs map to a kind).
"""
from __future__ import annotations

import json

from cairn.core.profile_backends import (
    BACKENDS,
    GROUPS,
    backends_schema,
    backends_schema_grouped,
    effective_backends,
    get_field,
)


def test_schema_is_well_formed():
    keys = [f.key for f in BACKENDS]
    assert len(keys) == len(set(keys)), "backend keys must be unique"
    for f in BACKENDS:
        assert f.group in GROUPS, f"{f.key}: group {f.group!r} not in GROUPS"
        assert f.description, f"{f.key}: needs help text"
        if f.control == "select":
            assert f.options, f"{f.key}: select needs options"
            assert any(o.value == str(f.default) for o in f.options), (
                f"{f.key}: default {f.default!r} not among its options"
            )
        if f.control == "toggle":
            assert isinstance(f.default, bool), f"{f.key}: toggle default must be bool"


def test_schema_is_json_serializable_and_grouped():
    payload = backends_schema()
    json.dumps(payload)  # must not raise
    assert len(payload) == len(BACKENDS)
    grouped = backends_schema_grouped()
    assert [g["group"] for g in grouped] == GROUPS
    # every field lands in exactly one group bucket
    assert sum(len(g["fields"]) for g in grouped) == len(BACKENDS)


def test_defaults_when_empty_profile_and_env():
    eff = effective_backends({}, env={})
    # zero-config safe defaults hold
    assert eff["storage"] == "sqlite"
    assert eff["rag_vector_store"] == "sqlite_vec"
    assert eff["durable_runtime"] == "in_process"
    assert eff["event_bus"] == "inprocess"
    assert eff["rag_autoindex"] is False
    assert eff["memory"] is False
    assert eff["remote_execution"] is False


def test_profile_overrides_env_and_default():
    eff = effective_backends(
        {"rag_vector_store": "qdrant", "memory": "true"},
        env={"RAG_VECTOR_STORE": "pgvector"},   # profile must win over env
    )
    assert eff["rag_vector_store"] == "qdrant"
    assert eff["memory"] is True                 # string "true" coerced to bool


def test_env_fallback_when_profile_silent():
    eff = effective_backends({}, env={"RAG_VECTOR_STORE": "qdrant", "OBS_RAG_AUTOINDEX": "1"})
    assert eff["rag_vector_store"] == "qdrant"
    assert eff["rag_autoindex"] is True


def test_url_kind_env_maps_to_backend_kind():
    eff = effective_backends({}, env={"OBS_STORAGE": "postgres://user@host:5432/db"})
    assert eff["storage"] == "postgres"
    eff2 = effective_backends({}, env={"OBS_STORAGE": "sqlite:///tmp/x.db"})
    assert eff2["storage"] == "sqlite"
    eff3 = effective_backends({}, env={"OBS_JOB_CHANNEL": "nats://localhost:4222"})
    assert eff3["job_channel"] == "nats"


def test_toggle_truthiness_from_env():
    for raw, want in [("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("nope", False)]:
        eff = effective_backends({}, env={"OBS_REMOTE_JOB_DURABLE": raw})
        assert eff["remote_execution"] is want, f"{raw!r} -> {want}"


def test_blank_profile_value_falls_through():
    # an empty string in the profile is treated as "unset" -> env/default, not ""
    eff = effective_backends({"rag_vector_store": ""}, env={"RAG_VECTOR_STORE": "qdrant"})
    assert eff["rag_vector_store"] == "qdrant"


def test_every_field_resolves_and_get_field():
    eff = effective_backends({}, env={})
    assert set(eff) == {f.key for f in BACKENDS}
    assert get_field("storage").label == "Run / event store"
    assert get_field("nope") is None
