"""deploy/light/docker-compose.yml — the cairn-light quickstart contract (#1831, epic #1828).

Light is a deployment posture, not a fork: three services, keyless, every value
defaulted so a fresh clone brings it up with no ``.env``, the ``light`` feature
preset on, and exactly four documented add-on profiles (``infisical`` joined with
the builtin secrets/infisical, hello-cairn WP2; ``proof`` with the feature-proof
harness, hello-cairn WP7). These tests pin that contract by
parsing the file — a ``${VAR:?}`` guard creeping in, a fourth default service, or an
undocumented profile would each break the 15-minute quickstart (#1833) without
failing anything else.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LIGHT = ROOT / "deploy" / "light" / "docker-compose.yml"
#: The Infisical server's own secrets: they belong to the infisical services, never the engine (#2577).
INFISICAL_SECRETS = ("INFISICAL_ENCRYPTION_KEY", "INFISICAL_AUTH_SECRET", "INFISICAL_DB_PASSWORD")


@pytest.fixture(scope="module")
def light() -> dict:
    return yaml.safe_load(LIGHT.read_text())


def _default_services(compose: dict) -> dict[str, dict]:
    return {n: s for n, s in compose["services"].items() if not s.get("profiles")}


def test_exactly_three_default_services(light):
    assert set(_default_services(light)) == {"caddy", "server", "postgres"}


def test_only_the_four_documented_add_on_profiles(light):
    profiles = {p for s in light["services"].values() for p in (s.get("profiles") or [])}
    assert profiles == {"llm-gateway", "backup", "infisical", "proof"}


def test_the_infisical_profile_is_off_by_default_and_pinned(light):
    """The Infisical server and its Postgres + Redis start only under the profile, on pinned
    (non-``latest``) tags; ENCRYPTION_KEY and AUTH_SECRET have an EMPTY default (no key baked
    in), telemetry defaults off, and the web UI is published on loopback only. The database
    password keeps a laptop default, like the app Postgres."""
    svcs = {n: s for n, s in light["services"].items() if "infisical" in (s.get("profiles") or [])}
    assert set(svcs) == {"infisical", "infisical-db", "infisical-redis"}
    for name, svc in svcs.items():
        assert not svc["image"].endswith(":latest") and ":" in svc["image"], (name, svc["image"])
    env = svcs["infisical"]["environment"]
    # the keys come from .env.infisical (#2577), mapped onto Infisical's names at start
    start = svcs["infisical"]["command"][-1]
    for key in ("ENCRYPTION_KEY", "AUTH_SECRET"):
        assert key not in env, key
        assert f'{key}="$${{INFISICAL_{key}:-}}"' in start, (key, start)
    assert start.rstrip().endswith("exec ./standalone-entrypoint.sh")
    for name in ("infisical", "infisical-db"):
        assert svcs[name]["env_file"] == [{"path": ".env.infisical", "required": False}], name
    assert env["TELEMETRY_ENABLED"] == "${INFISICAL_TELEMETRY_ENABLED:-false}"
    # never published off-box: loopback only
    assert all(str(p).startswith("127.0.0.1:") for p in svcs["infisical"].get("ports", []))


def test_no_required_variable_guards_anywhere():
    """A fresh clone must come up with NO .env. Compose interpolates EVERY
    service, profile on or off, so a `${VAR:?...}` guard even on an add-on
    refuses the bare 3-service `up` (the first fresh-clone run of #1833 died
    exactly there, on the gateway's master key)."""
    text = LIGHT.read_text()
    guarded = re.findall(r"\$\{(\w+):\?", text)
    assert guarded == [], guarded


def test_server_runs_the_light_posture(light):
    env = light["services"]["server"]["environment"]
    assert env["CAIRN_LIGHT"] == "${CAIRN_LIGHT:-${OBS_LIGHT:-on}}"
    assert env["CAIRN_RUNTIME"] == "${CAIRN_RUNTIME:-${OBS_RUNTIME:-inprocess}}"
    # the receipts: without a profile that binds audit.sink the Audit tab is
    # empty on a fresh install (#1833 run 3); secure = audit + redact, no tier
    assert env["CAIRN_PROFILE"].endswith("cairn/profiles/secure.toml}}")
    # keyless by default: an EMPTY secret, and no CAIRN_REQUIRE_AUTH (prod's guard)
    assert env["CAIRN_JWT_SECRET"] == "${CAIRN_JWT_SECRET:-${OBS_JWT_SECRET:-}}"
    assert "CAIRN_REQUIRE_AUTH" not in env and "OBS_REQUIRE_AUTH" not in env
    assert env["CAIRN_BACKEND"] == "${CAIRN_BACKEND:-${OBS_BACKEND:-mock}}"
    # keyless binds loopback by default (#1507); inside the container that hides
    # the server from caddy (502 on every request — #1833, run 3). The container
    # network is the boundary: `expose`, never `ports`, on the server.
    assert env["CAIRN_TRIGGER_HOST"] == "${CAIRN_TRIGGER_HOST:-${OBS_TRIGGER_HOST:-0.0.0.0}}"
    assert "ports" not in light["services"]["server"]
    # one Postgres carries run store + checkpointer
    assert env["CAIRN_STORAGE"].startswith("postgres://cairn:") and "@postgres:5432/cairn" in env["CAIRN_STORAGE"]
    assert env["CAIRN_CHECKPOINTER"] == "${CAIRN_CHECKPOINTER:-${OBS_CHECKPOINTER:-postgres}}"


def test_server_builds_the_engine_image(light):
    """Light builds the same image a production deployment runs, from this clone.
    The console build argument is the platform's (tests/core/test_prod_deploy_files.py)."""
    assert light["services"]["server"]["build"]["dockerfile"] == "Dockerfile.engine"


def test_env_file_is_optional(light):
    files = light["services"]["server"]["env_file"]
    assert files == [{"path": ".env", "required": False}]


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    return probe.returncode == 0


def _filled(example: Path) -> str:
    """The env file an operator writes from ``example``: every documented ``# KEY=value``
    line uncommented, the Infisical secrets given a recognisable value."""
    if not example.exists():
        return ""
    out = []
    for line in example.read_text().splitlines():
        m = re.match(r"#\s?([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            key, value = m.groups()
            out.append(f"{key}=sentinel-{key}" if key in INFISICAL_SECRETS else f"{key}={value}")
    return "\n".join(out) + "\n"


@pytest.mark.skipif(not _docker_compose_available(), reason="needs the docker compose CLI (no daemon)")
def test_infisical_secrets_never_reach_the_engine(tmp_path):
    """#2577: the server loads ALL of ``.env`` through ``env_file``, so the Infisical server's
    own secrets must not be documented there. Fill in every key the two example files document,
    render ``docker compose --profile infisical config``, and the three secrets are ABSENT from
    the engine's environment and PRESENT in the Infisical containers'. The engine still gets its
    Infisical CLIENT settings (it reads those)."""
    light = tmp_path / "deploy" / "light"
    light.mkdir(parents=True)
    shutil.copy(LIGHT, light / "docker-compose.yml")
    (light / ".env").write_text(_filled(LIGHT.parent / ".env.example"))
    (light / ".env.infisical").write_text(_filled(LIGHT.parent / ".env.infisical.example"))
    # the caller's shell must not leak into interpolation or profiles
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("INFISICAL_", "COMPOSE_", "CAIRN_", "OBS_"))}
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(light / "docker-compose.yml"), "--profile", "infisical",
         "config", "--format", "json"],
        capture_output=True, text=True, env=env, cwd=light,
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    server = services["server"]["environment"]
    for key in INFISICAL_SECRETS:
        assert key not in server, f"{key} reaches the engine container"
        assert f"sentinel-{key}" not in json.dumps(server), f"{key}'s value reaches the engine container"
        assert services["infisical"]["environment"].get(key) == f"sentinel-{key}", key
    assert services["infisical-db"]["environment"].get("INFISICAL_DB_PASSWORD") == "sentinel-INFISICAL_DB_PASSWORD"
    assert "INFISICAL_CLIENT_ID" in server and "INFISICAL_HOST" in server


def test_the_proof_profile_adds_only_loopback_key_only_pinned_services(light):
    """ADR-0127 D1 (hello-cairn WP7): the feature-proof services. The infisical profile's
    three services are REUSED by carrying the profile (never duplicated); `sshd` is a pinned
    `linuxserver/openssh-server` with password login off and the public key read from the
    seeded file; `pg-proxy` is a pinned relay to the app Postgres. Every published port binds
    loopback, and no value here is a secret (the keys come from .proof/, gitignored)."""
    svcs = {n: s for n, s in light["services"].items() if "proof" in (s.get("profiles") or [])}
    assert set(svcs) == {"infisical", "infisical-db", "infisical-redis", "sshd", "pg-proxy"}
    for name in ("infisical", "infisical-db", "infisical-redis"):
        assert "infisical" in svcs[name]["profiles"], name  # the same service, both profiles
    for name, svc in svcs.items():
        assert not svc["image"].endswith(":latest") and ":" in svc["image"], (name, svc["image"])
        assert all(str(p).startswith("127.0.0.1:") for p in svc.get("ports", [])), (name, svc.get("ports"))
    sshd = svcs["sshd"]
    assert sshd["image"].startswith("linuxserver/openssh-server:")
    env = sshd["environment"]
    assert env["PASSWORD_ACCESS"] == "false" and env["SUDO_ACCESS"] == "false"
    assert env["PUBLIC_KEY_FILE"] == "/keys/proof_ed25519.pub"
    assert any(v.startswith("./.proof/ssh:") and v.endswith(":ro") for v in sshd["volumes"])
    assert "PASSWORD" not in " ".join(k for k in env if k != "PASSWORD_ACCESS")
    proxy = svcs["pg-proxy"]
    assert proxy["image"].startswith("alpine/socat:")
    assert proxy["command"][-1] == "tcp-connect:postgres:5432"
    assert proxy["depends_on"]["postgres"]["condition"] == "service_healthy"


@pytest.mark.skipif(not _docker_compose_available(), reason="needs the docker compose CLI (no daemon)")
def test_the_proof_profile_keeps_infisical_secrets_off_the_engine(tmp_path):
    """WP7 + #2577: render ``--profile proof --env-file .proof/proof.env`` as the proof script
    does, with a proof.env that (like an older seed's) carries the three Infisical secrets and
    a seeded .env.infisical: the engine's environment has none of them, the Infisical
    services do, and the proof services publish loopback only."""
    light = tmp_path / "deploy" / "light"
    (light / ".proof" / "ssh").mkdir(parents=True)
    shutil.copy(LIGHT, light / "docker-compose.yml")
    (light / ".env.infisical").write_text(_filled(LIGHT.parent / ".env.infisical.example"))
    (light / ".proof" / "proof.env").write_text(
        "COMPOSE_PROFILES=proof\nPROOF_SSH_PORT=2232\nPROOF_PG_PORT=55432\n"
        + "".join(f"{k}=leaked-{k}\n" for k in INFISICAL_SECRETS))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("INFISICAL_", "COMPOSE_", "CAIRN_", "OBS_", "PROOF_"))}
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(light / "docker-compose.yml"), "--env-file",
         str(light / ".proof" / "proof.env"), "--profile", "proof", "config", "--format", "json"],
        capture_output=True, text=True, env=env, cwd=light,
    )
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    server = json.dumps(services["server"]["environment"])
    for key in INFISICAL_SECRETS:
        assert key not in server and f"leaked-{key}" not in server, key
        assert services["infisical"]["environment"].get(key) == f"sentinel-{key}", key
    assert "sshd" in services and "pg-proxy" in services
    for name in ("sshd", "pg-proxy", "infisical"):
        assert all(p["host_ip"] == "127.0.0.1" for p in services[name]["ports"]), name
    assert services["sshd"]["ports"][0]["published"] == "2232"


def _proof_seed(tmp_path: Path, monkeypatch):
    """scripts/oss/proof_seed.py, its paths pointed at a scratch deploy/light."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("proof_seed_under_test", ROOT / "scripts" / "oss" / "proof_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    light = tmp_path / "deploy" / "light"
    light.mkdir(parents=True)
    paths = {"ROOT": tmp_path, "LIGHT": light, "PROOF": light / ".proof", "SSH_DIR": light / ".proof" / "ssh",
             "KEY": light / ".proof" / "ssh" / "proof_ed25519", "ENV": light / ".proof" / "proof.env",
             "INFISICAL_ENV": light / ".env.infisical"}
    for name, value in paths.items():
        monkeypatch.setattr(mod, name, value)
    monkeypatch.setattr(mod, "seed_ssh_key", lambda: False)   # ssh-keygen is not what this pins
    return mod, paths


def test_proof_seed_drops_infisical_secrets_from_an_older_proof_env(tmp_path, monkeypatch):
    """WP7 + #2577: an older seed wrote the three Infisical secrets into proof.env. A re-seed
    drops them from proof.env and never carries their values over: .env.infisical (absent
    here) is written with FRESH values; `--check` names a proof.env that still carries one."""
    mod, p = _proof_seed(tmp_path, monkeypatch)
    p["PROOF"].mkdir()
    p["ENV"].write_text("PROOF_SSH_PORT=2232\n" + "".join(f"{k}=old-{k}\n" for k in INFISICAL_SECRETS))
    assert mod.seed(check=True) == 1
    assert mod.seed() == 0
    env = p["ENV"].read_text()
    assert "PROOF_SSH_PORT=2232" in env
    for key in INFISICAL_SECRETS:
        assert key not in env, key
    secrets = mod.read_env(p["INFISICAL_ENV"])
    assert set(secrets) == set(INFISICAL_SECRETS)
    assert all(v and not v.startswith("old-") for v in secrets.values()), "an older proof.env value was carried over"


def test_proof_seed_never_touches_an_existing_env_infisical(tmp_path, monkeypatch):
    """.env.infisical is written only when absent: an operator's own file is kept byte for byte."""
    mod, p = _proof_seed(tmp_path, monkeypatch)
    mine = "".join(f"{k}=mine-{k}\n" for k in INFISICAL_SECRETS)
    p["INFISICAL_ENV"].write_text(mine)
    assert mod.seed() == 0
    assert p["INFISICAL_ENV"].read_text() == mine
    assert not any(k in p["ENV"].read_text() for k in INFISICAL_SECRETS)
