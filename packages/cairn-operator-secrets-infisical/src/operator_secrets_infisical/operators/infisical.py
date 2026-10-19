"""secrets/infisical — Infisical SecretStore provider (capability secrets.store, ADR-0033).

The production swap-in for cairn-operator-secrets-env: reads secret values from an
Infisical project via the Infisical Python SDK (universal / machine-identity
auth). Dual interface, like cairn-operator-secrets-env:

  - ``get(name)`` / ``available_keys()`` — the SecretStore path. The runtime's
    ``build_scoped_secrets`` resolves the ``secrets.store`` binding to this
    operator and wraps it in a ScopedSecretStore (the binding's declared keys),
    which it injects as ``ctx.secrets``.
  - ``run(inp, ctx)`` — the node path: a PRESENCE check only. It never returns
    the value (that would leak the secret into state / the run trace).

Config + auth (env) live in :mod:`operator_secrets_infisical._infisical`. The
Infisical SDK is lazy-imported there (the ``[infisical]`` extra), so importing
this pack without the SDK never breaks — only a real ``get``/``available_keys``
call needs it.
"""
from __future__ import annotations

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_secrets_infisical import _infisical
from operator_secrets_infisical.schemas.infisical import (
    SecretCheckInput,
    SecretCheckOutput,
)


@register_operator(
    name="secrets/infisical",
    version="0.1.0",
    capabilities=["secrets.store"],
    implements=["contract://secrets.store/1.0.0"],  # #2243 V2 / ADR-0127 D3 — cairn-contracts-secrets
)
class InfisicalSecretStoreOp(Operation[SecretCheckInput, SecretCheckOutput]):
    """Infisical SecretStore (capability secrets.store)."""

    input_schema = SecretCheckInput
    output_schema = SecretCheckOutput
    supports_mock = True
    side_effect_free = True  # #836 — secret read; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one presence check, nothing to report

    # ── SecretStore interface (used via build_scoped_secrets → ctx.secrets) ──
    def get(self, name: str) -> str:
        cfg = _infisical.load_config()
        client = _infisical.authed_client(cfg)
        try:
            secret = client.secrets.get_secret_by_name(
                secret_name=name,
                environment_slug=cfg.environment,
                secret_path=cfg.secret_path,
                project_id=cfg.project_id,
            )
        except Exception as exc:  # noqa: BLE001 — any SDK/network failure → KeyError
            raise KeyError(
                f"secret {name!r} could not be read from Infisical "
                f"(project {cfg.project_id}, env {cfg.environment}): {exc}"
            ) from exc
        return secret.secretValue

    def available_keys(self) -> list[str]:
        cfg = _infisical.load_config()
        client = _infisical.authed_client(cfg)
        resp = client.secrets.list_secrets(
            environment_slug=cfg.environment,
            secret_path=cfg.secret_path,
            project_id=cfg.project_id,
        )
        return sorted(s.secretKey for s in resp.secrets)

    # ── Node path: presence check ONLY — never returns the value ─────────────
    async def run(
        self, inp: SecretCheckInput, ctx: OperationContext
    ) -> SecretCheckOutput:
        try:
            present = inp.name in self.available_keys()
        except Exception:  # noqa: BLE001 — unreachable backend → not present
            present = False
        return SecretCheckOutput(present=present)

    def mock(
        self, inp: SecretCheckInput, ctx: OperationContext
    ) -> SecretCheckOutput:
        return SecretCheckOutput(present=False)
