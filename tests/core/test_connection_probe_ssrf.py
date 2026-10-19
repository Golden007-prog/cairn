"""The connection test-probe cannot be aimed at internal infrastructure (#1506).

The probe sends an authenticated request to a URL the USER supplies — a custom
connection's ``base_url`` lands in ``resolved.config`` — from a host carrying an
IAM instance role. Unguarded that is a credential-read primitive: point a
connection at ``http://169.254.169.254/latest/meta-data/iam/…`` and the probe
hands you back the reply.

The rule is deliberately not uniform across probe kinds, and these tests pin
that asymmetry so nobody "simplifies" it later:

* **HTTP** talks to third-party vendor APIs — loopback/link-local is never
  legitimate, and private ranges are blocked by default but re-openable for
  self-hosters via ``CAIRN_PROBE_ALLOW_PRIVATE``.
* **SSH/DSN** reaching a private host is the whole point (your GPU box, your
  Postgres), so only the metadata endpoints are blocked there.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.connection_probe import probe_target_block_reason


# ─── the crown jewel: instance metadata, blocked for every probe kind ───

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "https://169.254.169.254/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata.goog/",
    "http://[fd00:ec2::254]/latest/meta-data/",
])
def test_metadata_endpoints_are_blocked(url):
    assert probe_target_block_reason(url, allow_private=True) is not None


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "//metadata.google.internal",
])
def test_metadata_is_blocked_even_under_the_loose_ssh_dsn_rule(url):
    """`metadata_only=True` relaxes private-range blocking — it must NOT relax
    this. SSH and Postgres have no business talking to an IMDS."""
    assert probe_target_block_reason(url, metadata_only=True) is not None


# ─── the rest of the internal surface (HTTP rule) ──────────────────────

@pytest.mark.parametrize("url,why", [
    ("http://127.0.0.1:8000/admin", "loopback"),
    ("http://localhost:5432/", "loopback by name"),
    ("http://[::1]/", "IPv6 loopback"),
    ("http://169.254.1.1/", "link-local"),
    ("http://10.0.0.5/internal", "RFC-1918 private"),
    ("http://192.168.1.1/", "RFC-1918 private"),
    ("http://172.16.0.1/", "RFC-1918 private"),
    ("http://0.0.0.0/", "unspecified"),
])
def test_internal_targets_are_blocked_by_default(url, why):
    assert probe_target_block_reason(url, allow_private=False) is not None, why


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:11211/",
    "ftp://internal/",
])
def test_non_http_schemes_are_refused(url):
    assert probe_target_block_reason(url) is not None


# ─── what must KEEP working ────────────────────────────────────────────

def test_private_ranges_are_reopenable_for_self_hosters():
    """A self-hoster with an internal gateway is a real case — blocked by
    default, explicitly re-openable, and loopback stays blocked regardless."""
    assert probe_target_block_reason("http://10.0.0.5/v1", allow_private=True) is None
    assert probe_target_block_reason("http://127.0.0.1/v1", allow_private=True) is not None


def test_the_env_switch_is_what_reopens_it(monkeypatch):
    monkeypatch.delenv("CAIRN_PROBE_ALLOW_PRIVATE", raising=False)
    assert probe_target_block_reason("http://10.0.0.5/v1") is not None
    monkeypatch.setenv("CAIRN_PROBE_ALLOW_PRIVATE", "1")
    assert probe_target_block_reason("http://10.0.0.5/v1") is None


def test_a_public_vendor_url_is_allowed():
    # 8.8.8.8 is a stable public literal — no DNS needed, so this is not a
    # network-dependent test.
    assert probe_target_block_reason("https://8.8.8.8/v1/models") is None


def test_an_unresolvable_host_is_not_blocked():
    """No address to connect to means no SSRF. Blocking here would make the
    guard fail closed offline and hide the real DNS error from the operator."""
    assert probe_target_block_reason("https://nx.invalid.example/v1") is None


def test_a_missing_host_is_refused():
    assert probe_target_block_reason("http:///just-a-path") is not None


# ─── the wiring: the probe actually consults the guard ─────────────────

def test_the_http_probe_refuses_a_metadata_target():
    """A guard nothing calls is the #1402 failure mode. Drive the real probe."""
    import asyncio

    from cairn.core.connection_probe import _http_probe
    from cairn.core.connections import ProbeSpec, ProviderSpec, ResolvedConnection

    provider = ProviderSpec(
        provider="evil", kind="model_api", description="a connection aimed at the IMDS",
        probe=ProbeSpec(method="GET", path="/latest/meta-data/",
                        base_url="http://169.254.169.254", ok_status=[200]),
    )
    resolved = ResolvedConnection(name="evil", kind="model_api", config={}, secrets={})
    result = asyncio.run(_http_probe(provider, resolved))
    assert result.ok is False
    assert "blocked" in (result.detail or "")
    assert "metadata" in (result.detail or "")


def test_redirect_following_is_off():
    """The guard only ever sees the FIRST url; a 302 to the IMDS would walk
    straight past it. Pin that the owned client does not chase redirects."""
    import inspect

    from cairn.core import connection_probe

    src = inspect.getsource(connection_probe._http_probe)
    assert "follow_redirects=False" in src, "the probe's own client must not follow redirects"
    assert "follow_redirects=True" not in src
