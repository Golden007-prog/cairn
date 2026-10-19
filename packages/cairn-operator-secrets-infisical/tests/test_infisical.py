"""Hermetic tests for secrets/infisical (Part of #408 / #389).

No network: the Infisical SDK client is fully mocked via ``monkeypatch`` on
``_infisical.authed_client``, so ``get`` / ``available_keys`` are exercised
against a fake client. The optional-dep gate (SDK absent → clear RuntimeError)
is proven by patching the SDK import to fail, mirroring the `[pii]`/`[minhash]`
gate pattern in cairn-operator-dataset-checks. ``pytest.importorskip`` guards the one
test that touches the real SDK's import surface.
"""
from __future__ import annotations

import builtins

import pytest

from cairn.core.operator import OperationContext

from operator_secrets_infisical import _infisical
from operator_secrets_infisical.operators.infisical import InfisicalSecretStoreOp
from operator_secrets_infisical.schemas.infisical import SecretCheckInput


# ─── fakes for the Infisical SDK client ──────────────────────────────────


class _FakeSecret:
    def __init__(self, key: str, value: str) -> None:
        self.secretKey = key
        self.secretValue = value


class _FakeListResp:
    def __init__(self, secrets: list[_FakeSecret]) -> None:
        self.secrets = secrets


class _FakeSecrets:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store
        self.get_calls: list[dict] = []

    def get_secret_by_name(self, *, secret_name, environment_slug, secret_path, project_id):
        self.get_calls.append(
            {
                "secret_name": secret_name,
                "environment_slug": environment_slug,
                "secret_path": secret_path,
                "project_id": project_id,
            }
        )
        if secret_name not in self._store:
            raise RuntimeError(f"secret not found: {secret_name}")
        return _FakeSecret(secret_name, self._store[secret_name])

    def list_secrets(self, *, environment_slug, secret_path, project_id):
        return _FakeListResp([_FakeSecret(k, v) for k, v in self._store.items()])


class _FakeClient:
    def __init__(self, store: dict[str, str]) -> None:
        self.secrets = _FakeSecrets(store)


_ENV = {
    "INFISICAL_CLIENT_ID": "cid",
    "INFISICAL_CLIENT_SECRET": "csecret",
    "INFISICAL_PROJECT_ID": "proj-123",
    "INFISICAL_ENVIRONMENT": "staging",
    "INFISICAL_SECRET_PATH": "/app",
    "INFISICAL_HOST": "https://vault.example.com",
}


@pytest.fixture()
def env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def fake_client(monkeypatch):
    store = {"HF_TOKEN": "hf_secret_value", "OPENAI_API_KEY": "sk-abc"}
    client = _FakeClient(store)
    monkeypatch.setattr(_infisical, "authed_client", lambda cfg: client)
    return client


def _ctx() -> OperationContext:
    return OperationContext(pipeline_id="t", node_name="secrets")


# ─── config ──────────────────────────────────────────────────────────────


def test_load_config_reads_env(env):
    cfg = _infisical.load_config()
    assert cfg.client_id == "cid"
    assert cfg.project_id == "proj-123"
    assert cfg.environment == "staging"
    assert cfg.secret_path == "/app"
    assert cfg.host == "https://vault.example.com"


def test_load_config_defaults(monkeypatch):
    monkeypatch.setenv("INFISICAL_CLIENT_ID", "cid")
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-123")
    monkeypatch.delenv("INFISICAL_ENVIRONMENT", raising=False)
    monkeypatch.delenv("INFISICAL_SECRET_PATH", raising=False)
    monkeypatch.delenv("INFISICAL_HOST", raising=False)
    cfg = _infisical.load_config()
    assert cfg.environment == "dev"
    assert cfg.secret_path == "/"
    assert cfg.host == "https://app.infisical.com"


def test_environment_default_is_dev_wherever_it_is_stated():
    """#2578: ONE default for INFISICAL_ENVIRONMENT. The operator's configuration default is
    ``dev``, and every place that documents it says the same — the pack manifest, its README
    and the prod-infisical profile that binds this operator (it once said ``prod``)."""
    import re
    from pathlib import Path

    import cairn.profiles

    assert _infisical._DEFAULT_ENVIRONMENT == "dev"
    pack = Path(__file__).resolve().parents[1]
    profile = Path(cairn.profiles.__file__).parent / "prod-infisical.toml"
    stated = {
        "pack.toml": re.findall(r'INFISICAL_ENVIRONMENT\s+\(default "(\w+)"\)', (pack / "pack.toml").read_text()),
        "README.md": re.findall(r"`INFISICAL_ENVIRONMENT`\s*\|[^|]*\|\s*`(\w+)`", (pack / "README.md").read_text()),
        "prod-infisical.toml": re.findall(r"INFISICAL_ENVIRONMENT \(default: (\w+)", profile.read_text()),
    }
    assert stated == {name: ["dev"] for name in stated}, stated


def test_load_config_missing_required_raises(monkeypatch):
    monkeypatch.delenv("INFISICAL_CLIENT_ID", raising=False)
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-123")
    with pytest.raises(_infisical.InfisicalConfigError, match="INFISICAL_CLIENT_ID"):
        _infisical.load_config()


# ─── SecretStore interface (get / available_keys) ────────────────────────


def test_get_returns_secret_value(env, fake_client):
    op = InfisicalSecretStoreOp()
    assert op.get("HF_TOKEN") == "hf_secret_value"
    # get() is scoped to the configured env + path + project
    call = fake_client.secrets.get_calls[0]
    assert call["environment_slug"] == "staging"
    assert call["secret_path"] == "/app"
    assert call["project_id"] == "proj-123"


def test_get_missing_raises_keyerror(env, fake_client):
    op = InfisicalSecretStoreOp()
    with pytest.raises(KeyError, match="NOPE"):
        op.get("NOPE")


def test_available_keys_lists_names_sorted(env, fake_client):
    op = InfisicalSecretStoreOp()
    assert op.available_keys() == ["HF_TOKEN", "OPENAI_API_KEY"]


def test_satisfies_secretstore_protocol(env, fake_client):
    # get_secret_provider() checks the instance has get + available_keys.
    op = InfisicalSecretStoreOp()
    assert hasattr(op, "get") and hasattr(op, "available_keys")


# ─── node path: presence check only (never leaks the value) ──────────────


@pytest.mark.asyncio
async def test_run_presence_check_true(env, fake_client):
    op = InfisicalSecretStoreOp()
    out = await op.run(SecretCheckInput(name="HF_TOKEN"), _ctx())
    assert out.present is True


@pytest.mark.asyncio
async def test_run_presence_check_false(env, fake_client):
    op = InfisicalSecretStoreOp()
    out = await op.run(SecretCheckInput(name="NOPE"), _ctx())
    assert out.present is False


def test_mock_never_present():
    op = InfisicalSecretStoreOp()
    out = op.mock(SecretCheckInput(name="HF_TOKEN"), _ctx())
    assert out.present is False


# ─── optional-dependency gate: SDK absent → clear RuntimeError ───────────


def test_missing_sdk_raises_clear_error(env, monkeypatch):
    """With the Infisical SDK hidden, a real call raises a clear install hint.

    Mirrors the `[minhash]`/`[fastpii]`/`[pii]` gate in cairn-operator-dataset-checks:
    importing the pack stays fine; only a backend call surfaces the missing dep.
    """
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "infisical_sdk" or name.startswith("infisical_sdk."):
            raise ImportError("infisical_sdk hidden for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(RuntimeError, match=r"requires the Infisical SDK"):
        _infisical.authed_client(_infisical.load_config())


# ─── real SDK import surface (skips hermetically when the SDK is absent) ──


def test_real_sdk_import_surface():
    pytest.importorskip("infisical_sdk")
    from infisical_sdk import InfisicalSDKClient  # noqa: PLC0415

    # The client constructs from host alone and exposes the auth + secrets
    # surfaces this operator relies on — guards against an SDK API drift.
    client = InfisicalSDKClient(host="https://app.infisical.com")
    assert hasattr(client.auth.universal_auth, "login")
    assert hasattr(client.secrets, "get_secret_by_name")
    assert hasattr(client.secrets, "list_secrets")
