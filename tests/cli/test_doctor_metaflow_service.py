"""#1365 — doctor probes the Metaflow metadata service when one is configured.

The service is the box-affinity fix (#1349/#1358): configured-but-unreachable
means cross-box resume and detached-run resolution silently degrade to
CWD-scoped local metadata (``MetaflowNotFound`` from any foreign process,
live-verified on the ADR-0089 acceptance run). Doctor's job is to make that a
named preflight failure instead of a run-time mystery.
"""
from __future__ import annotations

import asyncio

import pytest

from cairn.cli.commands.doctor import check_metaflow_service


@pytest.fixture()
def no_service(monkeypatch):
    monkeypatch.delenv("OBS_METAFLOW_SERVICE_URL", raising=False)
    monkeypatch.delenv("OBS_PROFILE", raising=False)


def test_unconfigured_is_silent(no_service):
    """No URL anywhere → no check line; a dev box must not grow a new warning."""
    assert asyncio.run(check_metaflow_service()) is None


def test_unreachable_service_is_a_fail_not_a_warn(monkeypatch, no_service):
    """A dead/wrong URL breaks everything the deployment configured it FOR —
    same silent failure shape as no service at all (#1358)."""
    # unroutable per RFC 5737 (TEST-NET) + tight probe timeout keeps this fast
    monkeypatch.setenv("OBS_METAFLOW_SERVICE_URL", "http://192.0.2.1:8080")
    monkeypatch.setattr("cairn.cli.commands.doctor._PROBE_TIMEOUT_SECONDS", 0.2)
    res = asyncio.run(check_metaflow_service())
    assert res is not None
    assert res.status == "fail"
    assert "#1358" in res.detail, "the message must name the constraint it protects"


def test_reachable_service_reports_ok(monkeypatch, no_service):
    """Any HTTP response counts (the probe checks reachability, not schema) —
    served here by a real local listener, not a mock."""
    import http.server
    import threading

    class _Ping(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802 — stdlib handler naming
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # keep pytest output clean
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Ping)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv(
            "OBS_METAFLOW_SERVICE_URL", f"http://127.0.0.1:{srv.server_port}")
        res = asyncio.run(check_metaflow_service())
        assert res is not None
        assert res.status == "ok"
        assert res.name == "metaflow_service"
    finally:
        srv.shutdown()


def test_probe_hits_the_ping_endpoint(monkeypatch, no_service):
    """The service's health contract is /ping — probing / would pass on any
    random listener bound to the same port."""
    seen: list[str] = []

    async def _fake_probe(url: str):
        seen.append(url)
        return True, "HTTP 200"

    monkeypatch.setenv("OBS_METAFLOW_SERVICE_URL", "http://mf:8080/")
    monkeypatch.setattr("cairn.cli.commands.doctor._probe_url", _fake_probe)
    asyncio.run(check_metaflow_service())
    assert seen == ["http://mf:8080/ping"]
