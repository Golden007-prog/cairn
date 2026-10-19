# cairn-operator-secrets-infisical

The **Infisical SecretStore** — the production swap-in for
`cairn-operator-secrets-env` (the builtin env-var store) for the **`secrets.store`**
capability (ADR-0033). One operator:

- `secrets/infisical` — reads secret values from an [Infisical](https://infisical.com)
  project via the Infisical Python SDK.

## How secrets reach an operator

Core owns the `SecretStore` interface + a resolver/scoper (`core/operator/secrets.py`);
no secret backend lives in core. At invocation time the runtime builds
`ctx.secrets` = a **scoped** view (only the keys the operation binding declares
in `secrets:`) backed by the bound provider:

```toml
[profile.bindings]
"secrets.store" = "secrets/infisical"
```

```yaml
# operations/run-something.yaml
operator: dataset/pull
secrets: [HF_TOKEN]        # only HF_TOKEN is readable; anything else raises
```

Unbound → `NullSecretStore` (reads raise). Swapping `secrets/env` →
`secrets/infisical` in the binding is the only change — no template edit.

## Install

The Infisical SDK is an optional extra (the base pack imports without it):

```bash
pip install "cairn-operator-secrets-infisical[infisical]"
```

## Configuration (env)

Auth is Infisical **universal auth** (machine identity) — a `client_id` /
`client_secret` pair minted for a machine identity, from env, never hard-coded:

| Env var                   | Required | Default                      | Meaning                                   |
| ------------------------- | -------- | ---------------------------- | ----------------------------------------- |
| `INFISICAL_CLIENT_ID`     | yes      | —                            | universal-auth machine identity id        |
| `INFISICAL_CLIENT_SECRET` | yes      | —                            | universal-auth machine identity secret    |
| `INFISICAL_PROJECT_ID`    | yes      | —                            | target project id                         |
| `INFISICAL_ENVIRONMENT`   | no       | `dev`                        | environment slug                          |
| `INFISICAL_SECRET_PATH`   | no       | `/`                          | secret folder path                        |
| `INFISICAL_HOST`          | no       | `https://app.infisical.com`  | site URL (set for a self-hosted instance) |

A missing required var fails **loud** at read time (`InfisicalConfigError`),
never silently.

## SDK surface used

```python
from infisical_sdk import InfisicalSDKClient

client = InfisicalSDKClient(host=INFISICAL_HOST)
client.auth.universal_auth.login(client_id=..., client_secret=...)

# get(name):
client.secrets.get_secret_by_name(
    secret_name=name, environment_slug=..., secret_path=..., project_id=...,
).secretValue

# available_keys():
[s.secretKey for s in client.secrets.list_secrets(
    environment_slug=..., secret_path=..., project_id=...,
).secrets]
```

## Safety

- Scoping is enforced by core before this backend is consulted, so a typo can't
  leak an unrelated secret.
- The node path (`run`) is a **presence check only** — it never returns a value
  (which would leak it into state / the run trace). Operators read real values
  via `ctx.secrets`.
