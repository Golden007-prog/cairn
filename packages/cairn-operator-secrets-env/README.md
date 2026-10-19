# cairn-operator-secrets-env

The zero-dep **env-var SecretStore** — the builtin default for the
**`secrets.store`** capability (ADR-0033). One operator:

- `secrets/env` — reads secret values from `os.environ`.

## How secrets reach an operator

Core owns the `SecretStore` interface + a resolver/scoper (`core/operator/secrets.py`);
no secret backend lives in core. At invocation time the runtime builds
`ctx.secrets` = a **scoped** view (only the keys the operation binding declares
in `secrets:`) backed by the bound provider:

```toml
[profile.bindings]
"secrets.store" = "secrets/env"
```

```yaml
# operations/run-something.yaml
operator: dataset/pull
secrets: [HF_TOKEN]        # only HF_TOKEN is readable; anything else raises
```

Unbound → `NullSecretStore` (reads raise). Heavier backings — **sops+age**
(single-binary, GitOps) or **OpenBao** (dynamic secrets / leases) — ship as
their own `operator-secrets-*` and swap via the binding, no core change.

## Safety

- Scoping is enforced before the backend is consulted, so a typo can't leak an
  unrelated secret.
- The node path (`run`) is a **presence check only** — it never returns a value
  (which would leak it into state / the run trace). Operators read real values
  via `ctx.secrets`.
