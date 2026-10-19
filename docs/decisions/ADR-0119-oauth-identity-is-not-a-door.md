# ADR-0119 — An OAuth identity is not a sign-up door

**Status:** accepted-pending-owner (2026-10-07, #2336). Amends ADR-0094 (D5 three sign-up
modes, D6 first-run bootstrap, D7 invite links). The design was approved on the issue; this
records it and the choices the issue left open.

## Context
Sign-in is `POST /token` (username + password) and sign-up is `POST /auth/signup` (company,
email, password). `OBS_SIGNUP` (D5) decides who may create an account; the first person on an
empty deployment becomes the owner (D6). People expect "Continue with Google / GitHub".
`cairn/core/oauth/` already runs Authlib, but for *outbound* connections (operators acting on
GitHub/Slack), with its own token store and catalog; nothing in it logs a person in.

## Decision
**D1 — An identity method, not a door.** Google and GitHub sign-in run the authorization-code
flow in cairn's own process (`cairn/core/oauth_login.py`, routes
`GET /auth/oauth/{google|github}/start` and `/callback`). A person nobody knows goes through
the same `signup()` as a password sign-up, with an `Identity` instead of a password hash, so
`OBS_SIGNUP` and the bootstrap decide exactly as for a password. Refusals reuse the sign-up
reasons (`signup_closed`, `domain_not_registered`, …) and add `email_not_verified`,
`oauth_state`, `oauth_denied`, `oauth_failed`, `oauth_unavailable`, `account_disabled`.

**D2 — Verified email only; link only on an exact match.** The email is the one the provider
verified: Google's `email_verified` claim from the ID token, GitHub's primary address from
`/user/emails` when `verified` (a verified secondary address is not used). An unverified
email is refused before anything else. The order is: a linked `(provider, subject)` → that
user; else a user whose login is that verified email → link the identity and sign in; else
the sign-up door. `User` gains `identities: list[Identity]` (`provider`, `subject`, `email`,
`linked_at`) in the JSON blob every store already persists: no migration, no table. Each
store gains `get_user_by_identity` as a query over that blob (in-memory filter, SQLite
`json_each`, Postgres JSONB `@>`). A user created this way has no password; `POST /token`
refuses them through its existing empty-hash check.

**D3 — The session reaches the console in a URL fragment.** The callback redirects to
`/console/oauth/callback#access_token=…&workspace=…&new_account=0|1`, carrying the same JWT
`POST /token` issues (`sub` = user id, `account_id`). A fragment is never sent to a server, so
it never reaches an access log, a proxy or a `Referer`. The landing page stores the token the
way Login does and removes the fragment from the address bar and history before navigating.
The alternative, a one-time code redeemed by `POST /auth/oauth/exchange`, is single-use only
if the code is stored, which needs a new store. A self-contained signed code would be a bearer
credential in a query string, which is the thing to avoid. The fragment is the simpler of the
two safe options.

**D4 — State bound to the browser, no store.** `start` sets a signed, HttpOnly, SameSite=Lax
cookie (path `/auth/oauth/`, 10 minutes) and sends a signed `state` carrying the same nonce.
`callback` requires both, matching, and in date (refusal `oauth_state`). Both are short JWTs
under a key derived from `OBS_JWT_SECRET` (HMAC with a fixed label), so neither verifies as an
access token. Google adds PKCE; the verifier rides in the cookie, never in the provider URL.
Google's ID token comes straight from its token endpoint over TLS, so its signature is not
re-checked (OIDC Core 3.1.3.7). Issuer, audience and expiry are checked.

**D5 — Config.** `OBS_OAUTH_GOOGLE_CLIENT_ID` / `_SECRET`, `OBS_OAUTH_GITHUB_CLIENT_ID` /
`_SECRET`, and `OBS_PUBLIC_URL` (callback base, `<base>/auth/oauth/<provider>/callback`; unset
means the request's own URL, which is wrong behind a proxy). A provider is offered only when
its client id is set, auth is on (`OBS_JWT_SECRET`), and Authlib is installed.
`GET /auth/signup` reports these as `providers`, and the console shows only those buttons.
`authlib` stays the optional `[oauth]` extra. The routes are always registered, so the
OpenAPI contract does not depend on the install. Without Authlib, no provider is offered.
Scopes are sign-in only (`openid email profile`; `read:user user:email`), deliberately not
the outbound catalog's.

**D6 — Not done, on purpose.** No account is created, and no user is linked, from an
unverified email. There is no OAuth-only bypass of the bootstrap or the mode: the first-run
owner is whoever completes sign-up first, by any method, exactly as D6 says. No provider
token is stored, and sign-in grants no outbound connection.

## Consequences
- **Env-name overlap with the outbound broker.** `AuthlibBroker._client_creds` reads the same
  `OBS_OAUTH_<P>_CLIENT_ID/SECRET` names for outbound connections. A deployment that set
  GitHub credentials for connectors now also offers "Continue with GitHub". That app's
  registered callback (`/oauth/callback`) does not cover `/auth/oauth/github/callback`, so
  GitHub refuses the redirect until the URI is registered. The issue fixes these names.
  Separate names for sign-in would need an owner decision.
- Sign-up with no company name (always the case through a provider) names the account after
  the email domain, so two people from one domain would collide. Account and workspace ids
  now take the first free slug (`acme-com`, `acme-com-2`) instead of overwriting: `put` is an
  upsert. This also fixes the same latent collision for two password sign-ups named alike.
- Not addressed here: with `OBS_SIGNUP=domain`, a founder on a public mail domain
  (`gmail.com`) claims that domain for their account. This predates this ADR and applies to
  passwords equally.

## Verification
`tests/core/test_oauth_login.py`: the provider is mocked at the HTTP layer. It covers
first-run owner, `invite` refusal, unverified email, the GitHub primary-only rule, linking
without duplicating, tampered, expired and cookie-less state, PKCE, no password login, the
configured-provider list, and same-domain tenants. The Postgres lookup has a live test,
skipped without a local server. Console: `src/auth/oauth.test.tsx`.
