# Connections

A **connection** is one reusable, typed binding to external infra — a model API,
a compute target, an MCP server, or an OAuth-brokered SaaS. Connections are
managed with `cairn connect` and consumed at runtime via `resolve_connection`.

Two principles hold for every kind:

- **Secrets are references, never values.** A connection stores a SecretStore
  reference (e.g. `OPENAI_API_KEY`), resolved at use time. `cairn connect` never
  reads, prints, or persists a raw secret value.
- **Durable by default.** Connections live in sqlite at `~/.cairn/connections.db`
  (override with `CAIRN_CONNECTIONS_DB`; `=:memory:` opts into a volatile store).
  The CLI, the admin panel, and `resolve_connection` all agree on this location.

## The CLI

```bash
cairn connect providers              # what you can connect (the catalog)
cairn connect add openai             # zero-config: refs OPENAI_API_KEY
cairn connect add docker --set host=tcp://host:2375
cairn connect add anthropic --name claude-prod --secret API_KEY=MY_ANTHROPIC_KEY
cairn connect ls                     # names + kinds + secret KEYS (never values)
cairn connect test <name>            # resolves refs; reports presence only
cairn connect rm <name>
```

`add` builds from the **provider catalog** — each entry knows the connection
`kind` and the secret refs a provider needs, so `cairn connect add openai`
produces a working `api_key` connection pointed at `OPENAI_API_KEY` with no
flags. Aliases: `hf`→huggingface, `claude`→anthropic, `google`→gemini, `k8s`→kube.

## OAuth connections (ADR-0037)

Providers that use OAuth 2.0 (GitHub, Slack, Jira, Notion, Google) are brokered.
cairn depends on a **replaceable `OAuthBroker` seam**, not a vendor SDK. The
broker runs the auth-code dance and owns token refresh; cairn fetches a **fresh
access token** at resolve time and **makes the actual API call itself** — so the
request/response never leave your infra and every use still passes the
`invoke_operator` chokepoint (cost / policy / redaction / audit / HITL).

```bash
cairn connect add github --via oauth          # persists a kind=oauth connection,
                                              # starts the flow, prints a redirect URL
cairn connect authorize github                # re-run the flow (retry / re-consent)
cairn connect test github                     # ✓ once the token exists at the broker
```

Flags: `--broker <name>` (pins the broker per-connection; otherwise the global
`CAIRN_OAUTH_BROKER` default applies at resolve time), `--connection-id <id>`
(default: the connection name), `--scope <s>` (repeatable), `--name <name>` (for
multiple accounts of the same provider).

Downstream, an OAuth connection resolves to `secrets={"ACCESS_TOKEN": <fresh>}` —
operators consume it identically to an `api_key` connection. The broker is
invisible below `resolve_connection`.

### Why a broker (and why Nango, not Composio)

- **Nango is auth-only**: it hands cairn a token; **cairn makes the call**
  (`cairn → target`). Tokens live in Nango's own Postgres, on your infra.
- **Composio executes tools on its cloud** (`cairn → Composio → target`) with a
  closed, cloud-only token vault — the call runs where cairn can't govern it.

The deciding factor is token custody + execution locus, not the license. See
ADR-0037. (OAuth *providers* — Hydra/Authentik/Zitadel/Dex — are the inbound
"SSO into cairn-server" role and are unrelated to this.)

## Running the Nango sidecar (default broker)

Nango is impl #1. "Just auth" needs Postgres + Redis + the Nango server (logs/ES
and S3 are omitted). It's wired as the `nango` profile in `docker-compose.dev.yml`.

### 1. Set the encryption key and start it

The image (`nangohq/nango-server:hosted`, Nango's OSS self-host) is pinned in the
compose. You only need a base64 32-byte encryption key (it's `linux/amd64`, so on
Apple Silicon it runs under emulation — fine, just slower to boot):

```bash
export NANGO_ENCRYPTION_KEY=$(openssl rand -base64 32)
# optional: NANGO_DASHBOARD_USERNAME / NANGO_DASHBOARD_PASSWORD (default admin/admin)
docker compose -f docker-compose.dev.yml --profile nango up -d
# server REST API → http://localhost:3003   ·   Connect UI → http://localhost:3009
curl -s http://localhost:3003/health      # → {"result":"ok"} once migrations finish (~15s)
```

### 2. Get the environment Secret Key and point cairn at it

Either copy it from the dashboard (`http://localhost:3003` → Environment Settings →
Secret Key), or read it from Nango's DB (self-host stores it on the `dev`
environment):

```bash
export CAIRN_NANGO_HOST=http://localhost:3003
export NANGO_SECRET_KEY=$(docker exec cairn-nango-db-1 \
  psql -U nango -d nango -tA -c "SELECT secret_key FROM _nango_environments WHERE name='dev';")
# cairn resolves NANGO_SECRET_KEY via the SecretStore (ref CAIRN_NANGO_SECRET_REF), env fallback
```

### 3. Configure the GitHub integration in Nango

This is Nango's job, not cairn's, and it needs *your* GitHub OAuth app:

1. GitHub → Settings → Developer settings → **OAuth Apps** → New OAuth App.
   Authorization callback URL: `http://localhost:3003/oauth/callback`.
2. In the Nango dashboard → Integrations → add **GitHub**, paste the OAuth app's
   Client ID + Client Secret, set the scopes you need.

## Live verification checklist

This cannot be claimed from cairn's hermetic tests — run it against the real
sidecar (steps 1–3 above first):

1. `cairn connect add github --via oauth` → prints a redirect URL.
2. Open the URL, complete GitHub consent → Nango stores the connection.
3. `cairn connect test github` → **✓ usable** (cairn fetched a fresh token).
4. `cairn connect rm github` to clean up.

> Verified so far against Nango v0.70.8: the sidecar boots from this compose, the
> secret key authenticates, and cairn's `--via oauth` reaches `POST /connect/sessions`
> correctly (it 400s only until the GitHub integration in step 3 exists). The
> browser-consent round-trip is the part this checklist covers.

## Swapping the broker later (replaceability)

The seam is the point. To replace Nango:

1. Implement `OAuthBroker` (`src/cairn/core/oauth/broker.py`: `start_authorization` /
   `get_access_token` / `revoke`) and `register_broker("yourbroker", YourBroker)`.
2. Select it: `CAIRN_OAUTH_BROKER=yourbroker` (global) or `--broker yourbroker`
   (per-connection, stored in `config["broker"]`).

No call site changes — cairn only ever touches the Protocol and
`Connection.config["broker"]`. A future Authlib-based broker that keeps tokens in
cairn's own store drops in the same way.
