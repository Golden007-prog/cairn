"""PostgresBus (Tier-2, #477) — structural tests + OBS_BUS routing.

Without a live Postgres we still verify (mirrors test_postgres_run_store.py):
  1. lazy asyncpg (no import on construction);
  2. the lazy pool refuses loudly when no DSN;
  3. `OBS_BUS=postgres://` routes get_bus() to PostgresBus with the DSN case
     preserved (credentials must not be lowercased);
  4. the DDL/DML SQL constants compile to plausible shapes.

The publish → subscribe → replay roundtrip is gated on `PGBUS_DSN` + the
`storage-postgres` dep (skipped by default).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.bus import InProcessBus, get_bus, reset_bus
from cairn.core.bus.postgres import (
    PostgresBus,
    _CREATE_BUS_MESSAGES_SQL,
    _INSERT_BUS_MESSAGE_SQL,
    _SELECT_BUS_MESSAGE_SQL,
    _SELECT_BUS_REPLAY_SQL,
)


# ─── instantiation + lazy pool ─────────────────────────────────────────


def test_postgresbus_instantiates_without_dep_installed():
    bus = PostgresBus(dsn="postgres://nowhere/db")
    assert bus._pool is None
    assert bus._schema_ready is False
    assert bus._listen_conn is None


@pytest.mark.asyncio
async def test_get_pool_raises_when_no_dsn():
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        bus = PostgresBus()
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await bus._get_pool()
        return
    bus = PostgresBus()
    with pytest.raises(RuntimeError, match="CAIRN_BUS=postgres"):
        await bus._get_pool()


# ─── OBS_BUS routing ───────────────────────────────────────────────────


def test_get_bus_routes_postgres_scheme_to_postgresbus(monkeypatch):
    monkeypatch.setenv("OBS_BUS", "postgres://User:Pass@Host:5432/DB")
    reset_bus()
    try:
        bus = get_bus()
        assert isinstance(bus, PostgresBus)
        # DSN case preserved — creds/host must not be lowercased
        assert bus._dsn == "postgres://User:Pass@Host:5432/DB"
    finally:
        reset_bus()


def test_get_bus_default_is_inprocess(monkeypatch):
    monkeypatch.delenv("OBS_BUS", raising=False)
    reset_bus()
    try:
        assert isinstance(get_bus(), InProcessBus)
    finally:
        reset_bus()


def test_get_bus_redis_is_refused(monkeypatch):
    # ADR-0127 D5 — no Redis bus exists; selecting one refuses (was: degrade).
    monkeypatch.setenv("OBS_BUS", "redis://localhost:6379")
    reset_bus()
    try:
        with pytest.raises(ValueError, match="postgres://"):
            get_bus()
    finally:
        reset_bus()


# ─── SQL constant shapes ───────────────────────────────────────────────


def test_sql_constants_shape():
    assert "bus_messages" in _CREATE_BUS_MESSAGES_SQL
    assert "message_id" in _CREATE_BUS_MESSAGES_SQL and "topic" in _CREATE_BUS_MESSAGES_SQL
    assert _INSERT_BUS_MESSAGE_SQL.count("$") == 4  # message_id, topic, correlation_id, data
    assert "WHERE message_id = $1" in _SELECT_BUS_MESSAGE_SQL
    assert "created_at >= $1" in _SELECT_BUS_REPLAY_SQL and "ORDER BY created_at" in _SELECT_BUS_REPLAY_SQL


# ─── DSN-gated e2e roundtrip (skipped without a live PG) ────────────────


_PGBUS_DSN = os.environ.get("PGBUS_DSN", "").strip()


@pytest.mark.skipif(not _PGBUS_DSN, reason="set PGBUS_DSN to a live Postgres to run the bus roundtrip")
@pytest.mark.asyncio
async def test_publish_subscribe_replay_roundtrip():
    import asyncio
    from datetime import datetime, timezone

    bus = PostgresBus(dsn=_PGBUS_DSN)
    got: list = []

    async def handler(msg):
        got.append(msg)

    since = datetime.now(timezone.utc)
    sub = await bus.subscribe("run.*", handler)
    try:
        await bus.publish("run.done", {"x": 1}, correlation_id="run-e2e")
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.02)
        assert got and got[0].topic == "run.done" and got[0].correlation_id == "run-e2e"

        # replay reads the durable log
        replayed = [m async for m in bus.replay("run.*", since)]
        assert any(m.correlation_id == "run-e2e" for m in replayed)
    finally:
        sub.unsubscribe()
        await bus.close()
