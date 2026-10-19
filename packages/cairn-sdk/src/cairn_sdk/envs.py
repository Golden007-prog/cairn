"""The SITE vocabulary — ``Environment`` · ``Connection`` · ``Policy`` ·
``Budget`` (#1254, ADR-0087 P2; file 1 of the vocabulary brief).

Facts about *where it runs and what's allowed*, never imported by a pipeline
file — the pipeline says ``env="prod"``, a reference. Each word projects onto
the record that already governs the runtime:

- ``Policy.approval`` / ``Policy.require_gate`` → ``policies/<name>.yaml``
  (the engine's ``PolicySpec`` — ``require_gate`` rides the #1252 P0b
  ``pipeline_legs`` plan-time injection).
- ``Environment`` → a profile TOML (``[profile]`` + ``[profile.backends]`` +
  the adopted policy names) — the record ``load_profile`` already reads.
- ``Connection.ref`` → the string form backends use to point at the
  connection catalog.
- ``Budget`` and ``models=`` have NO environment-record home today (budgets
  are runtime records — pipeline ``governance.budget`` and the series ledger;
  model roles live in the #352 registry). They compile to honest build NOTES
  pointing at the real wiring — captured, never silently dropped (the same
  contract P1 gives pipeline-level ``on=[cron(...)]``).
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

import yaml

from .builders import CompileScopeError


class Connection:
    """A NAME in the connection catalog — the environment points, it never
    defines credentials (those live in the catalog / secret store)."""

    @staticmethod
    def ref(name: str) -> str:
        return f"connection:{name}"


class Budget:
    """Spend caps. No env-record home yet — compiles to build notes naming
    the real wiring (pipeline ``governance.budget`` / ``cairn series start
    --budget``); kept in the vocabulary so the author states intent once."""

    def __init__(self, *, per_run_usd: float = 0.0, per_series_usd: float = 0.0,
                 on_breach: str = "gate") -> None:
        self.per_run_usd = float(per_run_usd)
        self.per_series_usd = float(per_series_usd)
        self.on_breach = on_breach


class Policy:
    """A governance record: ``policies/<name>.yaml`` in the engine's
    ``PolicySpec`` shape. Build with the classmethods, not the constructor."""

    def __init__(self, name: str, record: dict) -> None:
        self.name = name
        self.record = record

    @classmethod
    def approval(cls, *, on_risk: str, approvers: Sequence[str] = (),
                 name: str = "", timeout: Optional[str] = None) -> "Policy":
        """Human approval for every action of a risk class.

        ``approvers``: role names become ``approval_role`` (first entry);
        ``slack:...``/``email:...`` entries become notification channels.
        """
        roles = [a for a in approvers if ":" not in a]
        channels = [a for a in approvers if ":" in a]
        pname = name or f"approval-on-{on_risk.replace('_', '-')}"
        rule: dict[str, Any] = {
            "effect": "require_hitl",
            "reason": f"risk class {on_risk} requires human approval",
        }
        if roles:
            rule["approval_role"] = roles[0]
        if channels:
            rule["channels"] = channels
        if timeout:
            rule["timeout"] = timeout
        return cls(pname, {
            "name": pname,
            "applies_to": {"risks": [on_risk]},
            "rules": [rule],
        })

    @classmethod
    def require_gate(cls, *, verdict: str, before: str,
                     name: str = "") -> "Policy":
        """A STRUCTURAL gate before every pipeline leg matching ``before``
        (glob over ``pack/name``) — injected at PLAN time by #1252 P0b, not
        removable by the pipeline author."""
        pname = name or f"require-{verdict}-before-" + \
            "".join(c if c.isalnum() else "-" for c in before).strip("-")
        return cls(pname, {
            "name": pname,
            "applies_to": {"pipeline_legs": [before]},
            "rules": [{
                "effect": "require_hitl",
                "reason": f"require a {verdict} verdict before {before}",
            }],
        })

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.record, sort_keys=False, allow_unicode=True)


class CompiledEnv:
    """The build product of one Environment: a profile TOML + policy records
    + honest notes for vocabulary with no record home yet."""

    def __init__(self, name: str, profile_toml: str, policies: dict[str, str],
                 notes: list[str]) -> None:
        self.name = name
        self.profile_toml = profile_toml
        self.policies = policies    # policy name → policies/<name>.yaml text
        self.notes = notes

    def write(self, out_dir) -> None:
        """Materialize ``profiles/<env>.toml`` + ``policies/*.yaml``."""
        from pathlib import Path  # stdlib; local to keep module import-light

        base = Path(out_dir)
        (base / "profiles").mkdir(parents=True, exist_ok=True)
        (base / "profiles" / f"{self.name}.toml").write_text(
            self.profile_toml, encoding="utf-8")
        for pname, text in self.policies.items():
            (base / "policies").mkdir(parents=True, exist_ok=True)
            (base / "policies" / f"{pname}.yaml").write_text(
                text, encoding="utf-8")


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Environment:
    """WHERE it runs and what's allowed — compiles to the profile record."""

    def __init__(self, name: str, *, backends: Optional[dict] = None,
                 models: Optional[dict] = None,
                 policies: Sequence[Policy] = (),
                 budget: Optional[Budget] = None) -> None:
        if not name:
            raise CompileScopeError("Environment needs a name.")
        self.name = name
        self.backends = dict(backends or {})
        self.models = dict(models or {})
        self.policies = list(policies)
        self.budget = budget

    def compile(self) -> CompiledEnv:
        notes: list[str] = []
        lines = [
            f"# GENERATED by the cairn SDK for environment '{self.name}' "
            "(#1254, ADR-0087 P2).",
            "[profile]",
        ]
        if self.policies:
            names = ", ".join(f'"{p.name}"' for p in self.policies)
            lines.append(f"policies = [{names}]")
        if self.backends:
            lines.append("")
            lines.append("[profile.backends]")
            for k, v in self.backends.items():
                lines.append(f"{k} = {_toml_scalar(v)}")
        if self.models:
            notes.append(
                f"models={self.models!r} — model ROLES live in the role "
                "registry (#352), not the profile record; wire via the "
                "console Models page or the roles API.")
        if self.budget is not None:
            b = self.budget
            if b.per_run_usd:
                notes.append(
                    f"budget per_run_usd={b.per_run_usd} — per-run budgets "
                    "live on the pipeline record "
                    "(@pipeline(budget_usd=...) → governance.budget).")
            if b.per_series_usd:
                notes.append(
                    f"budget per_series_usd={b.per_series_usd} — series "
                    "budgets are ledger records: "
                    f"`cairn series start <name> --budget {b.per_series_usd}`.")
            notes.append(
                f"budget on_breach='{b.on_breach}' — breach behavior is the "
                "series ledger's (#993/#999); not a profile field.")
        return CompiledEnv(
            self.name, "\n".join(lines) + "\n",
            {p.name: p.to_yaml() for p in self.policies}, notes)


__all__ = ["Budget", "CompiledEnv", "Connection", "Environment", "Policy"]
