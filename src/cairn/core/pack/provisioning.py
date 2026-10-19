"""Pack stateful-requires vs environment provisioning (#304, ADR-0052 §4).

One mapping, two consumers: doctor's ``check_provisioned_backends`` (surfaced
validation) and the runtime fail-closed gate in the dispatcher (a run that
reaches an unprovisioned capability fails with a remediation, never a silent
skip). Kept tiny and import-light — the profile/backends read is lazy.
"""
from __future__ import annotations

#: Which environment backend field must be provisioned for each typed
#: ``[requires.<kind>]`` a pack declares. A backend counts as provisioned when
#: ``effective_backends`` resolves it truthy — every capability has a
#: zero-config default (sqlite/sqlite_vec/inprocess) EXCEPT memory (off by
#: default), so memory is the usual fail-closed case (pairs with #306).
REQUIRES_BACKEND: dict[str, str] = {
    "memory": "memory",           # [requires.memory.*] → the memory toggle
    "rag": "rag_vector_store",    # [requires.rag.*]    → a vector store must resolve
}


def unprovisioned_requires(pack_name: str) -> list[str]:
    """The stateful kinds ``pack_name`` requires that the active environment
    does NOT provision. Empty when the pack is unknown, declares nothing, or
    everything it needs is provisioned. Never raises — a broken profile reads
    as "nothing provisioned beyond defaults" (memory off)."""
    # lazy: registry + profile pull the pack/env stacks; keep import cheap
    from cairn.core.pack.registry import get_pack
    from cairn.core.pack.profile import load_profile_from_env
    from cairn.core.profile_backends import effective_backends

    pack = get_pack(pack_name)
    manifest = getattr(pack, "manifest", None) if pack is not None else None
    if manifest is None:
        return []
    try:
        backends = effective_backends(load_profile_from_env().backends)
    except Exception:  # noqa: BLE001 — a malformed profile must not crash the gate
        backends = effective_backends({})

    missing: list[str] = []
    for kind, field in REQUIRES_BACKEND.items():
        if getattr(manifest, f"required_{kind}", None) and not backends.get(field):
            missing.append(kind)
    return missing


def provisioning_remediation(pack_name: str, missing: list[str]) -> str:
    """The fail-closed message (ADR-0046 'backed by / enable one' state)."""
    kinds = " + ".join(missing)
    return (
        f"pack '{pack_name}' requires {kinds} but no {kinds} backend is enabled "
        "in this environment — enable it in Environments → Backends (or the "
        "profile's [profile.backends]) and re-run"
    )
