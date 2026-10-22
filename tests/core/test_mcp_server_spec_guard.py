"""``mcp_server_spec`` fails closed on where a kind=mcp connection may reach.

A ``stdio`` spec starts a process on the server, so it is built only when the
caller passes ``allow_stdio=True`` (the deployment). An http/sse spec goes
through the same address guard as the connection probe (no loopback,
link-local, cloud metadata or private address) unless the caller passes
``allow_private_network=True``. Hermetic: IP literals and a stubbed resolver,
no network.
"""
from __future__ import annotations

import ipaddress
import json

import pytest

from cairn.core import connection_probe
from cairn.core.clients.mcp_client import mcp_server_spec


@pytest.fixture(autouse=True)
def _no_private_override(monkeypatch):
    monkeypatch.delenv("CAIRN_PROBE_ALLOW_PRIVATE", raising=False)


STDIO = {"transport": "stdio", "command": "some-mcp-server", "args": ["--flag"]}


def test_stdio_is_refused_by_default():
    with pytest.raises(ValueError, match="stdio"):
        mcp_server_spec(STDIO)


def test_stdio_inferred_from_a_command_is_refused():
    with pytest.raises(ValueError, match="stdio"):
        mcp_server_spec({"command": "some-mcp-server"})


def test_stdio_inside_pasted_json_is_refused():
    raw = json.dumps({"mcpServers": {"x": {"command": "some-mcp-server"}}})
    with pytest.raises(ValueError, match="stdio"):
        mcp_server_spec({"transport": "json", "raw_json": raw})


def test_stdio_is_built_when_the_deployment_allows_it():
    spec = mcp_server_spec(STDIO, {"TOKEN": "t"}, allow_stdio=True)
    assert spec == {"transport": "stdio", "command": "some-mcp-server", "args": ["--flag"], "env": {"TOKEN": "t"}}


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/mcp",
    "http://169.254.169.254/latest/meta-data/",
    "http://metadata.google.internal/",
    "http://10.0.0.5/mcp",
    "http://[::1]/sse",
])
def test_http_to_an_internal_address_is_refused(url):
    with pytest.raises(ValueError, match="not allowed"):
        mcp_server_spec({"transport": "streamable_http", "url": url}, {"TOKEN": "t"})


def test_sse_to_a_name_resolving_inside_is_refused(monkeypatch):
    monkeypatch.setattr(connection_probe, "_resolved_ips", lambda host, port: ([ipaddress.ip_address("10.1.2.3")], None))
    with pytest.raises(ValueError, match="not allowed"):
        mcp_server_spec({"transport": "sse", "url": "https://mcp.internal.example/sse"})


def test_http_to_a_public_address_is_built():
    spec = mcp_server_spec({"transport": "streamable_http", "url": "https://93.184.215.14/mcp"}, {"TOKEN": "t"})
    assert spec == {"transport": "streamable_http", "url": "https://93.184.215.14/mcp",
                    "headers": {"Authorization": "Bearer t"}}


def test_the_deployment_may_reach_an_internal_server():
    spec = mcp_server_spec({"transport": "streamable_http", "url": "http://127.0.0.1:8080/mcp"},
                           allow_private_network=True)
    assert spec == {"transport": "streamable_http", "url": "http://127.0.0.1:8080/mcp"}
