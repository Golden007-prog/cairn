"""Manifest ↔ code drift detection for operator packs (#1105, ADR-0083 D4).

Every operator is declared **twice**: once in code (``@register_operator`` + the
Operation class) and once in ``pack.toml`` as an ``[operator."<name>"]`` table.
They drift — and because the manifest is what governance reads (``risk`` decides
whether a gate opens), a drifted manifest is a **governance-relevant**
inconsistency, not cosmetics.

Two real cases, both hit in a single session:

* ``rag/parse`` — the manifest declares it, no class is registered. The loader
  logs a warning on every run that nobody acts on.
* An operator whose entry point existed but whose installed distribution metadata
  was stale until the pack was reinstalled — invisible to the registry, so its
  declared risk never applied.

**Scope.** #1105 shipped "option B": only ``version``/``capabilities``/
``schemas`` were derivable, because ``risk``, ``requires_approval``,
``supports_*`` and ``executors`` lived solely in ``pack.toml``. #1539 closed
that gap — the Operation CLASS now carries every ``[operator.*]`` field and
``cairn pack sync`` generates the tables from it — so this check also compares
the governance fields (kind ``governance_mismatch``, via
``operator.manifest_sync.governance_drift``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


#: Kinds that are a manifest↔code DISAGREEMENT (a real defect). Anything else —
#: today only ``pack_not_installed`` — is an environment fact and must NOT fail a
#: CI gate: a dev machine or a slim deployment legitimately omits some packs.
DRIFT_KINDS = frozenset({
    "declared_not_registered", "registered_not_declared",
    "version_mismatch", "capability_mismatch", "schema_mismatch",
    "governance_mismatch",  # #1539 — risk/executors/flags/mcp, class vs table
})


@dataclass(frozen=True)
class DriftFinding:
    """One manifest↔code disagreement.

    ``kind`` is a stable slug so callers can filter; ``detail`` is written for a
    human and always names the operator — a finding a reader cannot act on is
    noise.
    """

    kind: str
    operator: str
    detail: str

    @property
    def is_drift(self) -> bool:
        """True for a real manifest↔code disagreement (vs an environment fact)."""
        return self.kind in DRIFT_KINDS


def _schema_name(obj: Any) -> str:
    """The class name of a class-valued schema attribute, or ``""``."""
    return getattr(obj, "__name__", "") or ""


def check_operator_drift(
    *,
    declared: Mapping[str, Any],
    registered: Mapping[str, Any],
    installed_entry_points: set[str] | None = None,
) -> list[DriftFinding]:
    """Compare manifest entries against registered classes.

    ``declared`` maps operator name → manifest ``OperatorEntry``; ``registered``
    maps operator name → the Operation subclass. Both are passed in so this stays
    a pure function (the installed-tree wiring lives in
    :func:`check_installed_operator_drift`).
    """
    out: list[DriftFinding] = []

    for name in sorted(set(declared) - set(registered)):
        # DISTINGUISH not-installed from drift. `discover_operator_pack_manifests`
        # walks the FILESYSTEM, but registration needs the pack pip-INSTALLED — so
        # on a dev machine where some packs aren't installed, every one of their
        # operators would look like drift, and the check would be useless as a CI
        # gate. Only a pack that IS installed and still fails to register is drift.
        # `installed_entry_points` is the operator NAMES present in distribution
        # metadata — precisely what the loader registers from. In metadata but not
        # registered = real drift (the import failed, or the class is gone). Absent
        # from metadata = the pack isn't pip-installed here.
        if installed_entry_points is None or name in installed_entry_points:
            out.append(DriftFinding(
                "declared_not_registered", name,
                f"manifest declares {name!r} but no Operation class is registered under "
                f"that name — check the entry-points table for cairn.operators, and "
                f"remember entry points only register on pack REINSTALL",
            ))
        else:
            out.append(DriftFinding(
                "pack_not_installed", name,
                f"{name!r} is declared by a pack that is not installed in this "
                f"environment — install it (pip install -e <pack>) or ignore if "
                f"intentional; NOT a manifest/code disagreement",
            ))

    for name in sorted(set(registered) - set(declared)):
        out.append(DriftFinding(
            "registered_not_declared", name,
            f"{name!r} is registered in code but has no [operator] entry in any "
            f"manifest — it therefore declares NO risk, so the approval gate cannot "
            f"key on it",
        ))

    for name in sorted(set(declared) & set(registered)):
        md, cls = declared[name], registered[name]

        m_version = str(getattr(md, "version", "") or "")
        c_version = str(getattr(cls, "version", "") or "")
        if m_version and c_version and m_version != c_version:
            out.append(DriftFinding(
                "version_mismatch", name,
                f"{name}: manifest says version {m_version}, the class says {c_version}",
            ))

        m_caps = sorted(getattr(md, "provides_capabilities", None) or [])
        c_caps = sorted(getattr(cls, "capabilities", None) or [])
        if m_caps != c_caps:
            out.append(DriftFinding(
                "capability_mismatch", name,
                f"{name}: manifest provides {m_caps or '[]'}, the class declares "
                f"{c_caps or '[]'}",
            ))

        for field, cls_attr, label in (
            ("input_schema", "input_schema", "input"),
            ("output_schema", "output_schema", "output"),
        ):
            m_schema = str(getattr(md, field, "") or "")
            c_schema = _schema_name(getattr(cls, cls_attr, None))
            # An omitted manifest schema is not a contradiction — many manifests
            # simply don't name one. Only a stated disagreement is drift.
            if m_schema and c_schema and m_schema != c_schema:
                out.append(DriftFinding(
                    "schema_mismatch", name,
                    f"{name}: manifest {label}_schema is {m_schema!r}, the class "
                    f"uses {c_schema!r}",
                ))

        # #1539 — the class now carries the governance fields too; a table
        # that disagrees is stale (`cairn pack sync` regenerates it).
        from cairn.core.operator.manifest_sync import governance_drift  # noqa: PLC0415 - cycle: pack.drift → operator
        gov = governance_drift(md, cls)
        if gov:
            out.append(DriftFinding(
                "governance_mismatch", name,
                f"{name}: pack.toml is stale vs the class — run `cairn pack "
                f"sync` — " + "; ".join(gov),
            ))

    return out


def check_installed_operator_drift() -> list[DriftFinding]:
    """Run :func:`check_operator_drift` over the INSTALLED tree.

    Loads operators + packs (idempotent) and compares the registry's manifest
    metadata against the classes actually registered.
    """
    from cairn.core.operator import default_registry, load_all_operators  # noqa: PLC0415 - cycle: pack → operator
    from cairn.core.operator.loader import (  # noqa: PLC0415 - cycle: pack.drift → operator.loader
        discover_operator_pack_manifests,
    )

    load_all_operators(strict=False)

    # Read the MANIFESTS directly, not `registry.all_metadata()`: the loader drops
    # metadata for an operator whose class never registered (it only warns), so the
    # registry's map is missing exactly the orphans this check exists to find.
    declared = dict(discover_operator_pack_manifests())
    eps = _installed_entry_point_names()

    # Compare ONLY operators the distribution metadata knows about. The default
    # registry is a mutable global that anything in-process can add to (tests
    # register fakes; a notebook might register one ad hoc) — including those
    # would make the result depend on what else has run, and a drift check whose
    # answer changes with import order is worse than no check.
    registered = {
        name: cls for name, cls in _registered_classes(default_registry).items()
        if eps is None or name in eps
    }

    return check_operator_drift(
        declared=declared, registered=registered,
        installed_entry_points=eps,
    )


def _installed_entry_point_names() -> set[str] | None:
    """Operator names present in ``cairn.operators`` DISTRIBUTION metadata.

    The same source the loader registers from, so "installed" here means exactly
    what registration means. ``None`` when metadata is unreadable → the caller
    treats every declaration as installed rather than hiding a real drift.
    """
    from importlib.metadata import entry_points  # noqa: PLC0415 - stdlib, cheap

    try:
        return {e.name for e in entry_points(group="cairn.operators")}
    except Exception:  # noqa: BLE001 — unreadable metadata must not mask drift
        return None


def _registered_classes(registry: Any) -> dict[str, Any]:
    """Every registered Operation class, best-effort across registry shapes."""
    for attr in ("all", "all_operators", "items"):
        fn = getattr(registry, attr, None)
        if callable(fn):
            try:
                got = fn()
                if isinstance(got, dict):
                    return dict(got)
            except Exception:  # noqa: BLE001 — fall through to the private map
                pass
    return dict(getattr(registry, "_operators", None) or {})


__all__ = ["DRIFT_KINDS", "DriftFinding", "check_operator_drift",
           "check_installed_operator_drift"]
