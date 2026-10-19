"""Unit tests for assemble_serve_report python_node (#703)."""
from __future__ import annotations

import asyncio

from cairn.core.engine.python_function_registry import (
    known_functions,
    resolve_python_function,
)


def _run(state: dict) -> dict:
    fn = resolve_python_function("assemble_serve_report")
    return asyncio.run(fn(dict(state)))


def test_registered():
    assert "assemble_serve_report" in known_functions()


def test_ready_hoists_endpoint_and_bench():
    state = {
        "findings": {
            "serve_endpoint_url": "http://gpu:8000/v1",
            "serve_status": "ready",
            "serve_model_name": "qlora",
            "bench_ok": True,
            "bench_tokens_per_sec": 118.4,
            "bench_latency_p50": 41.0,
            "bench_latency_p95": 60.0,
        }
    }
    out = _run(state)
    assert out["serve_status"] == "ready"
    assert out["endpoint_url"] == "http://gpu:8000/v1"
    assert out["served_model_name"] == "qlora"
    assert out["bench_ok"] is True
    assert out["tokens_per_sec"] == 118.4
    assert "READY" in out["summary"] and "qlora" in out["summary"] and "tok/s" in out["summary"]


def test_failed_serve_summary():
    out = _run({"findings": {"serve_status": "failed", "serve_model_name": "qlora"}})
    assert out["serve_status"] == "failed"
    assert out["endpoint_url"] == ""
    assert "FAILED" in out["summary"]


def test_empty_state_is_safe():
    out = _run({})
    assert out["serve_status"] == "failed"
    assert out["bench_ok"] is False
    assert out["summary"]
