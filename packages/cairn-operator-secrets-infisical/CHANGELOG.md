# Changelog

## Unreleased

Promoted from the official (private) tier to the builtin (public) tier: the
pack ships with the public core beside `cairn-operator-secrets-env` (owner
decision 2026-10-14, hello-cairn WP2). Distribution, import name, operator ref and
entry point are unchanged; the SDK stays the optional `[infisical]` extra.
`secrets/infisical` implements `contract://secrets.store/1.0.0` (cairn-contracts-secrets,
#2243 V2), as `secrets/env` does, on the manifest and the class.

## 0.1.0

Initial release (Part of #408 / #389). Infisical-backed `SecretStore` for the
`secrets.store` capability — the production swap-in for `operator-secrets-env`.
Universal-auth (machine identity) via env; Infisical Python SDK is an optional
`[infisical]` extra, lazy-imported so the base pack imports without it.
