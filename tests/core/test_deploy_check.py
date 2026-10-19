"""Tests for cairn.core.deploy_check — the `cairn deploy doctor` logic (#390).

Pure data-in/data-out: an env dict + enabled profiles → CheckResults. Proves the
env↔profile consistency rules, especially the canonical failure the brief calls
out (CAIRN_RUNTIME=temporal without the temporal profile).
"""
from __future__ import annotations

from pathlib import Path

from cairn.core.deploy_check import (
    KNOWN_PROFILES,
    PROFILE_TIERS,
    check_deploy,
    exit_code,
    parse_env_file,
    parse_profiles,
)

# A fully-valid minimal deploy: required vars set, default in-process runtime,
# no optional profiles → zero failures.
_VALID = {
    "CADDY_DOMAIN": "app.example.com",
    "POSTGRES_PASSWORD": "s3cret",
    "CAIRN_JWT_SECRET": "jwt",
    # #1853 — a genuine argon2 hash (of "s3cret"): the doctor now checks the
    # hash PARSES, so a placeholder would fail every test that expects a clean
    # deploy. Hardcoded rather than hashed at import: argon2 is deliberately slow.
    "CAIRN_API_PASSWORD_HASH": '$argon2id$v=19$m=65536,t=3,p=4$hdL7wUwAlItMM+v3pVfHxA$R1VstcN4/3bgfIjAOEmkCALYtjAqE4bjysDdB7zSit8',
    "CAIRN_PROFILE": "/app/.venv/lib/python3.13/site-packages/cairn/profiles/secure.toml",  # #2296
}


def _fails(results):
    return {r.name for r in results if r.status == "fail"}


def _by_name(results, name):
    return next(r for r in results if r.name == name)


# ── required vars ────────────────────────────────────────────────────────


def test_valid_minimal_config_has_no_failures():
    results = check_deploy(dict(_VALID), profiles=set())
    assert _fails(results) == set()
    assert exit_code(results) == 0


def test_missing_required_vars_fail():
    results = check_deploy({}, profiles=set())
    assert _fails(results) == set(
        ["CADDY_DOMAIN", "POSTGRES_PASSWORD", "CAIRN_JWT_SECRET", "CAIRN_API_PASSWORD_HASH", "CAIRN_PROFILE"]
    )
    assert exit_code(results) == 1


def test_blank_required_var_counts_as_missing():
    env = dict(_VALID, CAIRN_JWT_SECRET="   ")
    assert "CAIRN_JWT_SECRET" in _fails(check_deploy(env, set()))


# ── runtime ↔ temporal profile (the canonical brief example) ─────────────


def test_temporal_runtime_without_profile_fails():
    env = dict(_VALID, CAIRN_RUNTIME="temporal")
    results = check_deploy(env, profiles=set())
    assert "runtime" in _fails(results)
    assert "temporal' profile is off" in _by_name(results, "runtime").detail


def test_temporal_runtime_with_profile_ok():
    # A valid Temporal deploy also needs a durable checkpointer (#532) so gates
    # resume across the server↔worker split.
    env = dict(_VALID, CAIRN_RUNTIME="temporal",
               CAIRN_CHECKPOINTER="postgres", CAIRN_CHECKPOINTER_DSN="postgresql://x")
    results = check_deploy(env, profiles={"temporal"})
    assert _by_name(results, "runtime").status == "ok"
    assert exit_code(results) == 0


def test_temporal_profile_without_runtime_warns():
    results = check_deploy(dict(_VALID), profiles={"temporal"})
    r = _by_name(results, "runtime")
    assert r.status == "warn"
    assert exit_code(results) == 0  # warn doesn't fail
    assert exit_code(results, strict=True) == 1  # …unless strict


def test_default_runtime_is_inprocess_ok():
    r = _by_name(check_deploy(dict(_VALID), set()), "runtime")
    assert r.status == "ok" and "inprocess" in r.detail


# ── durable runtime ↔ checkpointer (#532 Problem 2) ──────────────────────


def test_temporal_with_memory_checkpointer_fails():
    """The #532 footgun: Temporal on + default MemorySaver → HITL/composed-subgraph
    gates can't resume across the server↔worker boundary."""
    env = {**_VALID, "CAIRN_RUNTIME": "temporal"}  # CAIRN_CHECKPOINTER unset → memory
    r = _by_name(check_deploy(env, profiles={"temporal"}), "checkpointer")
    assert r.status == "fail"
    assert "CAIRN_CHECKPOINTER=postgres" in r.detail


def test_dbos_with_memory_checkpointer_fails():
    """#1847: DBOS recovers PENDING workflows on restart, but a MemorySaver died
    with the process — so recovery replays the whole graph (re-charging every
    operator and LLM call) while still calling itself a durable runtime."""
    env = {**_VALID, "CAIRN_RUNTIME": "dbos"}  # CAIRN_CHECKPOINTER unset → memory
    r = _by_name(check_deploy(env, set()), "checkpointer")
    assert r.status == "fail"
    assert "re-runs the whole graph" in r.detail
    # the remedy must name the single-node tier: DBOS has no server↔worker split,
    # so Temporal's postgres-only wording would be wrong advice here.
    assert "CAIRN_CHECKPOINTER=sqlite" in r.detail


def test_dbos_with_sqlite_checkpointer_ok():
    env = {**_VALID, "CAIRN_RUNTIME": "dbos", "CAIRN_CHECKPOINTER": "sqlite"}
    r = _by_name(check_deploy(env, set()), "checkpointer")
    assert r.status == "ok"


def test_temporal_with_postgres_checkpointer_ok():
    env = {**_VALID, "CAIRN_RUNTIME": "temporal",
           "CAIRN_CHECKPOINTER": "postgres", "CAIRN_CHECKPOINTER_DSN": "postgresql://x"}
    r = _by_name(check_deploy(env, profiles={"temporal"}), "checkpointer")
    assert r.status == "ok"


def test_postgres_checkpointer_without_dsn_fails():
    env = {**_VALID, "CAIRN_CHECKPOINTER": "postgres"}  # no DSN
    r = _by_name(check_deploy(env, set()), "checkpointer")
    assert r.status == "fail" and "DSN" in r.detail


def test_memory_checkpointer_without_temporal_is_ok():
    # In-process runtime → memory checkpointer is fine (same-process resume).
    r = _by_name(check_deploy(dict(_VALID), set()), "checkpointer")
    assert r.status == "ok"


def test_remote_job_durable_with_memory_checkpointer_fails():
    # #551 seam 6: durable remote jobs need a shared, durable checkpointer even
    # under in_process — a restart during the job would orphan the paused run.
    env = {**_VALID, "CAIRN_REMOTE_JOB_DURABLE": "1"}  # CAIRN_CHECKPOINTER unset → memory
    r = _by_name(check_deploy(env, set()), "checkpointer")
    assert r.status == "fail"
    assert "CAIRN_REMOTE_JOB_DURABLE=1" in r.detail
    assert "CAIRN_CHECKPOINTER=postgres" in r.detail


def test_remote_job_durable_with_postgres_checkpointer_ok():
    env = {**_VALID, "CAIRN_REMOTE_JOB_DURABLE": "1",
           "CAIRN_CHECKPOINTER": "postgres", "CAIRN_CHECKPOINTER_DSN": "postgresql://x"}
    r = _by_name(check_deploy(env, set()), "checkpointer")
    assert r.status == "ok"


# ── durable remote-job ↔ shared job store (#564) ─────────────────────────
def _durable_ok(**over):
    # A durable-remote-job config that passes the checkpointer check, so the
    # remote_job_store check is what's under test.
    return {**_VALID, "CAIRN_REMOTE_JOB_DURABLE": "1",
            "CAIRN_CHECKPOINTER": "postgres", "CAIRN_CHECKPOINTER_DSN": "postgresql://x", **over}


def test_remote_job_durable_with_memory_store_fails():
    # #564: the RemoteJobRecord must live in a store shared across the node
    # (Temporal worker) and the control loop (server). In-memory is per-process.
    env = _durable_ok(CAIRN_JOB_STORE="memory")
    r = _by_name(check_deploy(env, set()), "remote_job_store")
    assert r.status == "fail"
    assert "#564" in r.detail


def test_remote_job_durable_with_sqlite_store_ok():
    # Default (CAIRN_JOB_STORE unset) → sqlite, a durable shared file.
    r = _by_name(check_deploy(_durable_ok(), set()), "remote_job_store")
    assert r.status == "ok"


def test_remote_job_durable_with_postgres_storage_ok():
    # CAIRN_STORAGE=postgres:// wins the selector → shared postgres store, even if
    # CAIRN_JOB_STORE=memory is set.
    env = _durable_ok(CAIRN_STORAGE="postgresql://x", CAIRN_JOB_STORE="memory")
    r = _by_name(check_deploy(env, set()), "remote_job_store")
    assert r.status == "ok"


def test_no_remote_job_store_check_when_not_durable():
    # The check only applies when durable remote jobs are on.
    names = {r.name for r in check_deploy(dict(_VALID), set())}
    assert "remote_job_store" not in names


# ── nats ↔ durable trigger inbox ─────────────────────────────────────────


def test_nats_inbox_without_profile_fails():
    env = dict(_VALID, CAIRN_TRIGGER_INBOX="nats://nats:4222", CAIRN_TRIGGER_DURABLE="1")
    assert "trigger_inbox" in _fails(check_deploy(env, profiles=set()))


def test_nats_durable_flag_alone_triggers_the_check():
    env = dict(_VALID, CAIRN_TRIGGER_DURABLE="true")
    assert "trigger_inbox" in _fails(check_deploy(env, profiles=set()))


def test_nats_profile_with_inbox_ok():
    env = dict(_VALID, CAIRN_TRIGGER_INBOX="nats://nats:4222", CAIRN_TRIGGER_DURABLE="1")
    results = check_deploy(env, profiles={"nats"})
    assert _by_name(results, "trigger_inbox").status == "ok"


def test_nats_profile_without_inbox_warns():
    r = _by_name(check_deploy(dict(_VALID), profiles={"nats"}), "trigger_inbox")
    assert r.status == "warn"


# ── dedicated temporal db ────────────────────────────────────────────────


def test_temporal_db_seeds_without_profile_fails():
    env = dict(_VALID, TEMPORAL_DB_SEEDS="temporal-db")
    assert "temporal_db" in _fails(check_deploy(env, profiles=set()))


def test_dedicated_profile_without_seeds_warns():
    results = check_deploy(dict(_VALID), profiles={"dedicated-temporal-db"})
    assert _by_name(results, "temporal_db").status == "warn"


# ── otel ─────────────────────────────────────────────────────────────────


def test_otel_collector_endpoint_without_profile_fails():
    env = dict(_VALID, OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318")
    assert "otel" in _fails(check_deploy(env, profiles=set()))


def test_managed_otlp_endpoint_needs_no_profile():
    env = dict(_VALID, OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp-gateway.grafana.net/otlp")
    assert "otel" not in _fails(check_deploy(env, profiles=set()))


# ── mlflow profile ↔ tracking URI (#408) ──────────────────────────────────


def test_self_hosted_mlflow_uri_without_profile_fails():
    env = dict(_VALID, MLFLOW_TRACKING_URI="http://mlflow:5000")
    assert "mlflow" in _fails(check_deploy(env, profiles=set()))


def test_managed_mlflow_uri_needs_no_profile():
    env = dict(_VALID, MLFLOW_TRACKING_URI="https://my-managed-mlflow.example.com")
    assert "mlflow" not in _fails(check_deploy(env, profiles=set()))


def test_mlflow_profile_with_uri_ok():
    env = dict(_VALID, MLFLOW_TRACKING_URI="http://mlflow:5000")
    results = check_deploy(env, profiles={"mlflow"})
    assert _by_name(results, "mlflow").status == "ok"


def test_mlflow_profile_without_uri_warns():
    r = _by_name(check_deploy(dict(_VALID), profiles={"mlflow"}), "mlflow")
    assert r.status == "warn"


# ── observability profile ↔ otel ──────────────────────────────────────────


def test_observability_without_otel_warns():
    results = check_deploy(dict(_VALID), profiles={"observability"})
    r = _by_name(results, "observability")
    assert r.status == "warn"
    assert "otel" in r.detail


def test_observability_with_otel_no_warning():
    env = dict(_VALID, OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318")
    results = check_deploy(env, profiles={"observability", "otel"})
    assert not any(r.name == "observability" for r in results)


# ── llm backend ──────────────────────────────────────────────────────────


def test_openai_backend_without_key_fails():
    env = dict(_VALID, CAIRN_BACKEND="openai")
    assert "llm_backend" in _fails(check_deploy(env, profiles=set()))


def test_openai_backend_with_key_ok():
    env = dict(_VALID, CAIRN_BACKEND="openai", OPENAI_API_KEY="sk-x")
    assert "llm_backend" not in _fails(check_deploy(env, profiles=set()))


# ── profiles parsing + typo guard ────────────────────────────────────────


def test_parse_profiles_splits_and_trims():
    assert parse_profiles("temporal, nats ,otel") == {"temporal", "nats", "otel"}
    assert parse_profiles("") == set()
    assert parse_profiles(None) == set()


def test_unknown_profile_warns():
    results = check_deploy(dict(_VALID), profiles={"tempora1"})  # typo
    assert _by_name(results, "profile:tempora1").status == "warn"


# ── deployment tiers (#408) ───────────────────────────────────────────────


def test_new_saas_profiles_are_known():
    # infisical/backup/mlflow/observability were added for #408 — must not
    # trip the unknown-profile typo guard.
    for p in ("infisical", "mlflow", "observability", "backup"):
        assert p in KNOWN_PROFILES
        results = check_deploy(dict(_VALID), profiles={p})
        assert f"profile:{p}" not in {r.name for r in results}


def test_every_known_profile_has_a_tier():
    assert set(PROFILE_TIERS) == set(KNOWN_PROFILES)
    assert set(PROFILE_TIERS.values()) <= {"core", "saas"}


def test_core_vs_saas_tier_split():
    # The dividing rule from #408: can't run/be governed without it → core;
    # watches/scores/records → saas.
    assert PROFILE_TIERS["temporal"] == "core"
    assert PROFILE_TIERS["nats"] == "core"
    assert PROFILE_TIERS["infisical"] == "core"
    assert PROFILE_TIERS["backup"] == "core"
    assert PROFILE_TIERS["otel"] == "saas"
    assert PROFILE_TIERS["observability"] == "saas"
    assert PROFILE_TIERS["mlflow"] == "saas"


def test_tier_summary_reports_enabled_profiles_by_tier():
    results = check_deploy(dict(_VALID), profiles={"temporal", "otel"})
    tiers = _by_name(results, "tiers")
    assert tiers.status == "ok"
    assert "temporal" in tiers.detail
    assert "otel" in tiers.detail


def test_tier_summary_with_no_profiles_says_none_enabled():
    tiers = _by_name(check_deploy(dict(_VALID), profiles=set()), "tiers")
    assert "(none enabled)" in tiers.detail


# ── infisical profile ↔ real secrets ──────────────────────────────────────


def test_infisical_profile_without_secrets_fails():
    results = check_deploy(dict(_VALID), profiles={"infisical"})
    assert "infisical" in _fails(results)


def test_infisical_profile_with_changeme_defaults_fails():
    env = dict(
        _VALID,
        INFISICAL_ENCRYPTION_KEY="change-me-openssl-rand-hex-16",
        INFISICAL_AUTH_SECRET="change-me-openssl-rand-hex-32",
        INFISICAL_DB_PASSWORD="change-me",
    )
    assert "infisical" in _fails(check_deploy(env, profiles={"infisical"}))


def test_infisical_profile_with_real_secrets_ok():
    env = dict(
        _VALID,
        INFISICAL_ENCRYPTION_KEY="abc123",
        INFISICAL_AUTH_SECRET="def456",
        INFISICAL_DB_PASSWORD="ghi789",
    )
    results = check_deploy(env, profiles={"infisical"})
    assert "infisical" not in _fails(results)
    assert _by_name(results, "infisical").status == "ok"


def test_infisical_check_absent_when_profile_off():
    results = check_deploy(dict(_VALID), profiles=set())
    assert "infisical" not in {r.name for r in results}


# ── infisical service ↔ engine secret-provider binding (#408) ──────────────

_INFISICAL_REAL = dict(
    _VALID,
    INFISICAL_ENCRYPTION_KEY="abc123",
    INFISICAL_AUTH_SECRET="def456",
    INFISICAL_DB_PASSWORD="ghi789",
)


def test_infisical_on_without_obs_profile_warns_binding():
    # Service configured but the engine isn't pointed at it → silent mismatch.
    env = {k: v for k, v in _INFISICAL_REAL.items() if k != "CAIRN_PROFILE"}
    r = _by_name(check_deploy(env, profiles={"infisical"}),
                 "secrets_binding")
    assert r.status == "warn"
    assert "CAIRN_PROFILE" in r.detail


def test_infisical_on_with_prod_profile_ok_binding():
    # CAIRN_PROFILE points at the shipped prod-infisical profile (binds Infisical).
    import cairn.profiles as _p
    prof = Path(_p.__file__).parent / "prod-infisical.toml"
    env = dict(_INFISICAL_REAL, CAIRN_PROFILE=str(prof))
    r = _by_name(check_deploy(env, profiles={"infisical"}), "secrets_binding")
    assert r.status == "ok"


def test_infisical_on_with_non_infisical_profile_warns(tmp_path):
    # CAIRN_PROFILE resolves to a real file that does NOT bind Infisical.
    other = tmp_path / "other.toml"
    other.write_text('[profile]\nextends = "local-dev.toml"\n')
    env = dict(_INFISICAL_REAL, CAIRN_PROFILE=str(other))
    r = _by_name(check_deploy(env, profiles={"infisical"}), "secrets_binding")
    assert r.status == "warn"
    assert "plaintext env" in r.detail


def test_secrets_binding_check_absent_when_infisical_off():
    names = {r.name for r in check_deploy(dict(_VALID), profiles=set())}
    assert "secrets_binding" not in names


# ── env file parsing + the shipped example ───────────────────────────────


def test_parse_env_file_reads_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text("CADDY_DOMAIN=x.com\n# comment\nCAIRN_JWT_SECRET=\nCOMPOSE_PROFILES=temporal,nats\n")
    env = parse_env_file(p)
    assert env["CADDY_DOMAIN"] == "x.com"
    assert env["CAIRN_JWT_SECRET"] == ""
    assert parse_profiles(env["COMPOSE_PROFILES"]) == {"temporal", "nats"}


def test_parse_env_file_missing_is_empty(tmp_path):
    assert parse_env_file(tmp_path / "nope.env") == {}


# ── #1565: a duplicate site block takes the WHOLE ingress down ───────────────
#
# Prod crash-looped on every boot for days: the Caddyfile had two
# `mlflow.cairndev.sh` blocks, Caddy exited with "ambiguous site definition",
# and app.cairndev.sh — not just the duplicated vhost — was unreachable. The
# box copy is rsync-excluded (deploy gotcha #10), so it is hand-edited and
# nothing preflighted it. These pin the preflight that now does.

import textwrap

from cairn.core.deploy_check import check_caddyfile

_TWO_SITES = textwrap.dedent("""\
    {$CADDY_DOMAIN} {
    \treverse_proxy server:9090
    }

    mlflow.cairndev.sh {
    \tbasic_auth {
    \t\tmlflow HASH
    \t}
    \treverse_proxy mlflow:5000
    }
    """)


def test_duplicate_site_fails_and_names_both_lines(tmp_path):
    f = tmp_path / "Caddyfile"
    f.write_text(_TWO_SITES + "\nmlflow.cairndev.sh {\n\treverse_proxy mlflow:5000\n}\n")
    r = check_caddyfile(f)[0]
    assert r.status == "fail"
    assert "mlflow.cairndev.sh" in r.detail
    assert "ambiguous site definition" in r.detail, "name the error Caddy will print"
    assert "ENTIRE ingress" in r.detail, "the blast radius is the point"


def test_a_clean_caddyfile_passes(tmp_path):
    f = tmp_path / "Caddyfile"
    f.write_text(_TWO_SITES)
    r = check_caddyfile(f)[0]
    assert r.status == "ok" and "no duplicates" in r.detail


def test_directive_blocks_are_not_mistaken_for_sites(tmp_path):
    """`basic_auth {`, `handle {` etc. open blocks too — counting them as site
    addresses would fire on every real Caddyfile. Only COMPARABLE addresses are
    counted: the `{$CADDY_DOMAIN}` block above is skipped as env-substituted,
    so two literal sites with nested directives must count as exactly 2."""
    f = tmp_path / "Caddyfile"
    f.write_text(_TWO_SITES + textwrap.dedent("""
        temporal.cairndev.sh {
        \tbasic_auth {
        \t\tadmin HASH
        \t}
        \treverse_proxy temporal-ui:8080
        }
        """))
    r = check_caddyfile(f)[0]
    assert r.status == "ok", r.detail
    assert "2 site block" in r.detail, (
        f"the two literal sites, not their nested directive blocks: {r.detail}")


def test_env_substituted_addresses_are_not_compared(tmp_path):
    """`{$CADDY_DOMAIN}` resolves at load time; two of them are not knowably
    duplicate here, and guessing would block a legitimate deploy."""
    f = tmp_path / "Caddyfile"
    f.write_text("{$CADDY_DOMAIN} {\n\treverse_proxy a:1\n}\n\n"
                 "{$OTHER_DOMAIN} {\n\treverse_proxy b:2\n}\n")
    assert check_caddyfile(f)[0].status == "ok"


def test_a_missing_caddyfile_warns_rather_than_fails(tmp_path):
    """Not every deploy terminates TLS with Caddy — absence is not misconfig."""
    assert check_caddyfile(tmp_path / "nope").status if False else True
    r = check_caddyfile(tmp_path / "nope")[0]
    assert r.status == "warn"


# ── #774: Caddy buffers SSE unless told not to ──────────────────────────────
#
# The console's live-run graph reads /runs/{id}/events and /runs/{id}/stream.
# Caddy buffers reverse-proxy responses by default, so on the box the page sat
# on "Connected — waiting for events…" while the run completed invisibly —
# direct to the engine the same page streamed fine, which is why this read as
# an application bug for months. It is a proxy setting.


def test_reverse_proxy_without_flush_interval_warns(tmp_path):
    f = tmp_path / "Caddyfile"
    f.write_text("{$CADDY_DOMAIN} {\n\treverse_proxy server:9090\n}\n")
    sse = [r for r in check_caddyfile(f) if r.name == "caddy_sse"][0]
    assert sse.status == "warn"
    assert "/runs/{id}/events" in sse.detail, "name the routes that break"
    assert "flush_interval -1" in sse.detail, "and the fix"


def test_flush_interval_present_is_ok(tmp_path):
    f = tmp_path / "Caddyfile"
    f.write_text(
        "{$CADDY_DOMAIN} {\n"
        "\t@sse path_regexp ^/runs/[^/]+/(events|stream)$\n"
        "\treverse_proxy @sse server:9090 {\n\t\tflush_interval -1\n\t}\n"
        "\treverse_proxy server:9090\n}\n")
    sse = [r for r in check_caddyfile(f) if r.name == "caddy_sse"][0]
    assert sse.status == "ok"


def test_a_caddyfile_that_proxies_nothing_is_not_warned(tmp_path):
    """An ingress fronting no reverse_proxy has no SSE to buffer — warning
    there would be noise, and noise is how a warn stops being read."""
    f = tmp_path / "Caddyfile"
    f.write_text("{$CADDY_DOMAIN} {\n\trespond \"ok\"\n}\n")
    assert not [r for r in check_caddyfile(f) if r.name == "caddy_sse"]


# ── password hash must parse (#1853) ─────────────────────────────────────


def test_a_corrupted_hash_fails_the_doctor_and_names_the_cause():
    """The prod shape: compose interpolated every `$` segment out of the argon2
    hash, so `$argon2id$v=19$m=65536,…` arrived as `=19=65536,…`. "set" passed;
    every login then failed as 'incorrect password'. The doctor must fail it
    and say what to do."""
    env = {**_VALID, "CAIRN_API_PASSWORD_HASH": "=19=65536,t=3,p=4/8HHdinpyNtId"}
    r = _by_name(check_deploy(env, set()), "password_hash")
    assert r.status == "fail"
    assert "$$" in r.detail and "#1853" in r.detail


def test_a_doubled_dollar_hash_read_literally_also_fails():
    """`$$argon2id$$…` is the correct thing to WRITE in .env (compose un-escapes
    it) — but if it ever reaches the process literally, it is just as unusable,
    and the doctor must not bless it."""
    env = {**_VALID, "CAIRN_API_PASSWORD_HASH": _VALID["CAIRN_API_PASSWORD_HASH"].replace("$", "$$")}
    assert _by_name(check_deploy(env, set()), "password_hash").status == "fail"


def test_a_real_hash_passes_the_doctor():
    assert _by_name(check_deploy(dict(_VALID), set()), "password_hash").status == "ok"


def test_no_hash_emits_no_password_hash_check():
    """Unset is the REQUIRED rule's business (it fails there); the parse rule
    only speaks when there is something to parse."""
    env = {**_VALID, "CAIRN_API_PASSWORD_HASH": ""}
    assert not [r for r in check_deploy(env, set()) if r.name == "password_hash"]
