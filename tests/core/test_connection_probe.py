"""Authenticated connection health probe (connector-probe).

Hermetic: every HTTP probe is driven through ``httpx.MockTransport`` so no live
provider is contacted. Asserts that each auth_type builds the *right* request
(header / query / basic per the doc-verified table), that a 200 → ok and a 401 →
not-ok, that the structural / SDK-only providers degrade cleanly (never a false
"connected"), and that the 9 new catalog entries parse with a valid auth_type.
"""
from __future__ import annotations

import base64
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import httpx
import pytest

from cairn.core.connection_probe import ProbeResult, probe_connection
from cairn.core.connection_probe import test_connection as _test_connection  # not a test
from cairn.core.connections import (
    AUTH_TYPES,
    Connection,
    InMemoryConnectionStore,
    PROVIDER_CATALOG,
    connection_from_provider,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point the default connection store at a tmp sqlite + env-only secrets, so
    probe_connection()'s internal resolve_connection() finds our rows."""
    monkeypatch.setenv("OBS_CONNECTIONS_DB", str(tmp_path / "conns.db"))
    monkeypatch.delenv("OBS_PROFILE", raising=False)  # env-fallback secret resolution
    yield


def _capture():
    """A MockTransport that records the last request and replies per the status
    baked into a mutable holder — lets a test assert the request shape."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(seen.get("status", 200), json={"ok": True})

    return seen, httpx.MockTransport(handler)


def _client(transport):
    return httpx.AsyncClient(transport=transport, timeout=5.0)


# ── request-shape per auth_type (the doc-verified table) ───────────────────


async def test_openai_bearer_header(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    seen, tr = _capture()
    async with _client(tr) as cl:
        r = await probe_connection(connection_from_provider("openai"), client=cl)
    req = seen["request"]
    assert req.method == "GET"
    assert str(req.url).endswith("/v1/models")
    assert req.headers["Authorization"] == "Bearer sk-live"
    assert r.ok is True and r.status == 200


async def test_anthropic_x_api_key_and_version(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("anthropic"), client=cl)
    req = seen["request"]
    assert req.headers["x-api-key"] == "sk-ant"
    assert req.headers["anthropic-version"] == "2023-06-01"   # pinned, not bearer
    assert "Authorization" not in req.headers


async def test_gemini_key_query_param(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("gemini"), client=cl)
    req = seen["request"]
    assert "/v1beta/models" in str(req.url)
    assert req.url.params.get("key") == "g-key"               # query param, not header


async def test_runpod_graphql_key_query_param(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("runpod"), client=cl)
    req = seen["request"]
    assert req.method == "POST"
    assert str(req.url).startswith("https://api.runpod.io/graphql")
    assert req.url.params.get("api_key") == "rp-key"          # query param
    assert b"myself" in req.content


async def test_langfuse_basic_public_secret(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-2")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("langfuse"), client=cl)
    req = seen["request"]
    assert str(req.url).endswith("/api/public/projects")      # NOT /health (unauth)
    expect = base64.b64encode(b"pk-1:sk-2").decode()
    assert req.headers["Authorization"] == f"Basic {expect}"


async def test_lambda_labs_basic_key_as_user(monkeypatch):
    monkeypatch.setenv("LAMBDA_API_KEY", "lam-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("lambda_labs"), client=cl)
    req = seen["request"]
    assert str(req.url).endswith("/api/v1/instance-types")
    expect = base64.b64encode(b"lam-key:").decode()           # key-as-user, empty pass
    assert req.headers["Authorization"] == f"Basic {expect}"


async def test_e2b_x_api_key_header(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("e2b"), client=cl)
    req = seen["request"]
    assert str(req.url) == "https://api.e2b.app/sandboxes"
    assert req.headers["X-API-Key"] == "e2b-key"


async def test_langsmith_x_api_key_header(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("langsmith"), client=cl)
    req = seen["request"]
    assert "/api/v1/sessions" in str(req.url)
    assert req.headers["x-api-key"] == "ls-key"


async def test_supabase_apikey_header(monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "sb-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(
            connection_from_provider("supabase", config={"url": "https://ref.supabase.co"}),
            client=cl,
        )
    req = seen["request"]
    assert str(req.url).endswith("/rest/v1/")
    assert req.headers["apikey"] == "sb-key"


async def test_pinecone_api_key_header(monkeypatch):
    monkeypatch.setenv("PINECONE_API_KEY", "pc-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("pinecone"), client=cl)
    req = seen["request"]
    assert str(req.url) == "https://api.pinecone.io/indexes"
    assert req.headers["Api-Key"] == "pc-key"


async def test_grafana_bearer_on_api_org(monkeypatch):
    monkeypatch.setenv("GRAFANA_API_TOKEN", "graf-key")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(
            connection_from_provider("grafana", config={"url": "https://g.example"}),
            client=cl,
        )
    req = seen["request"]
    assert str(req.url) == "https://g.example/api/org"        # NOT /api/health|/api/user
    assert req.headers["Authorization"] == "Bearer graf-key"


async def test_datadog_dd_api_key_header(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "dd-key")
    monkeypatch.setenv("DD_APP_KEY", "dd-app")
    seen, tr = _capture()
    async with _client(tr) as cl:
        await probe_connection(connection_from_provider("datadog"), client=cl)
    req = seen["request"]
    assert str(req.url).endswith("/api/v1/validate")
    assert req.headers["DD-API-KEY"] == "dd-key"


# ── status → ok mapping ────────────────────────────────────────────────────


async def test_200_is_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    seen, tr = _capture()
    seen["status"] = 200
    async with _client(tr) as cl:
        r = await probe_connection(connection_from_provider("openai"), client=cl)
    assert r.ok is True and r.status == 200 and r.detail == "authenticated"
    assert r.latency_ms is not None


async def test_401_is_not_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bad")
    seen, tr = _capture()
    seen["status"] = 401
    async with _client(tr) as cl:
        r = await probe_connection(connection_from_provider("openai"), client=cl)
    assert r.ok is False and r.status == 401
    assert "rejected" in r.detail


async def test_500_is_not_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    seen, tr = _capture()
    seen["status"] = 500
    async with _client(tr) as cl:
        r = await probe_connection(connection_from_provider("openai"), client=cl)
    assert r.ok is False and r.status == 500


async def test_missing_secret_is_not_ok(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _, tr = _capture()
    async with _client(tr) as cl:
        r = await probe_connection(connection_from_provider("openai"), client=cl)
    assert r.ok is False and r.status == 0
    assert "unset" in r.detail


async def test_missing_base_url_is_not_ok(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "m-key")
    _, tr = _capture()
    async with _client(tr) as cl:
        # litellm base_url defaults to "" — no host to probe.
        r = await probe_connection(connection_from_provider("litellm"), client=cl)
    assert r.ok is False and "base URL" in r.detail


async def test_network_error_degrades_not_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")

    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    async with _client(httpx.MockTransport(boom)) as cl:
        r = await probe_connection(connection_from_provider("openai"), client=cl)
    assert r.ok is False and r.status == 0
    assert "ConnectError" in r.detail


# ── structural / SDK-only providers degrade cleanly (never false OK) ───────


@pytest.mark.parametrize("provider", ["modal", "jarvislabs", "ray", "opentelemetry", "wandb"])
async def test_sdk_only_providers_are_unsupported(provider):
    r = await probe_connection(connection_from_provider(provider))
    assert r.ok is None                       # degraded, NOT False and NOT True
    assert "unsupported" in r.detail.lower()


async def test_ssh_without_asyncssh_or_host_degrades():
    # no host configured → clean failure, never raises.
    r = await probe_connection(connection_from_provider("ssh"))
    assert r.ok is False and "host" in r.detail.lower()


async def test_mtls_kube_degrades():
    r = await probe_connection(connection_from_provider("kube"))
    assert r.ok is None and "unsupported" in r.detail.lower()


async def test_public_private_key_s3_degrades():
    r = await probe_connection(
        connection_from_provider("s3", config={"bucket": "b", "region": "us-east-1"})
    )
    assert r.ok is None                       # SigV4 is SDK territory


async def test_oauth_without_token_is_not_ok(monkeypatch):
    # github is kind=oauth; with no broker creds the token won't resolve.
    monkeypatch.setenv("OBS_OAUTH_BROKER", "does-not-exist")
    conn = Connection(name="github", kind="oauth", config={"provider": "github"})
    store = InMemoryConnectionStore()
    await store.create(conn)
    # resolve_connection for oauth would hit the broker; assert we don't raise.
    r = await probe_connection(conn)
    assert isinstance(r, ProbeResult)
    assert r.ok is not True                    # no real token → not connected


# ── the 9 new providers parse with a valid auth_type + probe ───────────────


NEW_PROVIDERS = [
    "deepseek", "langfuse", "langsmith", "arize_phoenix",
    "milvus", "supabase", "lambda_labs", "jarvislabs", "e2b",
]


@pytest.mark.parametrize("provider", NEW_PROVIDERS)
def test_new_provider_parses(provider):
    assert provider in PROVIDER_CATALOG
    spec = PROVIDER_CATALOG[provider]
    assert spec.auth_type in AUTH_TYPES
    # a Connection builds from it without error
    conn = connection_from_provider(provider)
    assert conn.name == provider


def test_every_catalog_entry_has_valid_auth_type():
    for key, spec in PROVIDER_CATALOG.items():
        assert spec.auth_type in AUTH_TYPES, f"{key}: {spec.auth_type}"


# ── test_connection service persists health ────────────────────────────────


async def test_service_persists_health(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    store = InMemoryConnectionStore()
    conn = connection_from_provider("openai")
    await store.create(conn)
    seen, tr = _capture()
    async with _client(tr) as cl:
        updated, result = await _test_connection(conn.id, store=store, client=cl)
    assert result.ok is True
    assert updated.health is not None
    assert updated.health.ok is True and updated.health.status == 200
    assert updated.health.checked_at is not None
    # persisted on the row
    reloaded = await store.get(conn.id)
    assert reloaded.health.ok is True


async def test_service_unknown_id_returns_none(monkeypatch):
    store = InMemoryConnectionStore()
    conn, result = await _test_connection("ghost", store=store)
    assert conn is None and result.ok is False
