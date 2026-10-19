"""secrets/env — env-var SecretStore provider (capability secrets.store, ADR-0033).

The zero-dep default secret backend: reads values from ``os.environ``. Dual
interface, like cairn-operator-audit-store:

  - ``get(name)`` / ``available_keys()`` — the SecretStore path. The runtime's
    ``build_scoped_secrets`` resolves the ``secrets.store`` binding to this
    operator and wraps it in a ScopedSecretStore (the binding's declared keys),
    which it injects as ``ctx.secrets``.
  - ``run(inp, ctx)`` — the node path: a PRESENCE check only. It never returns
    the value (that would leak the secret into state / the run trace).

Heavier backings (sops+age, OpenBao) ship as their own ``operator-secrets-*``
and swap via the binding — no core change.
"""
from __future__ import annotations

import os

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_secrets_env.schemas.env import SecretCheckInput, SecretCheckOutput


@register_operator(
    name="secrets/env",
    version="0.1.0",
    capabilities=["secrets.store"],
    implements=["contract://secrets.store/1.0.0"],  # #2243 V2 — cairn-contracts-secrets
)
class EnvSecretStoreOp(Operation[SecretCheckInput, SecretCheckOutput]):
    """Environment-variable SecretStore (capability secrets.store)."""

    input_schema = SecretCheckInput
    output_schema = SecretCheckOutput
    supports_mock = True
    side_effect_free = True  # #836 — secret read; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one presence check, nothing to report

    # ── SecretStore interface (used via build_scoped_secrets → ctx.secrets) ──
    def get(self, name: str) -> str:
        try:
            return os.environ[name]
        except KeyError:
            raise KeyError(f"secret {name!r} is not set in the environment") from None

    def available_keys(self) -> list[str]:
        return sorted(os.environ.keys())

    # ── Node path: presence check ONLY — never returns the value ─────────────
    async def run(self, inp: SecretCheckInput, ctx: OperationContext) -> SecretCheckOutput:
        return SecretCheckOutput(present=inp.name in os.environ)

    def mock(self, inp: SecretCheckInput, ctx: OperationContext) -> SecretCheckOutput:
        return SecretCheckOutput(present=False)
