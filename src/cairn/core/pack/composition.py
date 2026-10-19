"""§24.3 pack composition — load-time validation (P2.b).

After the loader has registered every installed pack, this module answers
four questions in one pass:

  1. **Who provides what?** Builds the contribution registry —
     ``capability → [pack, …]`` — from each pack's public surface.
  2. **Are required capabilities satisfied?** Every name in a pack's
     ``[requires.capabilities]`` must be provided by *some* pack. If not
     → :class:`CompositionConflict`.
  3. **Are providers unique?** If two packs both ``provides.capabilities``
     the same name, that's a conflict — unless a deployment profile picks
     a winner via :func:`load_profile`. (§24.3.5 Open call B —
     fail-by-default + profile override.)
  4. **Which optional capabilities are missing?** Recorded as warnings,
     not failures (§24.3.1 / §24.3.5). Doctor surfaces them so an operator
     knows what extra packs would light up.

Permissive default (§24.3.10) — the migration on-ramp:

  - A pack with ``provides_declared=False`` doesn't *opt into* encapsulation.
    Its ``capability_bindings`` still count as global contributions for the
    purposes of satisfying *other* packs' ``requires.capabilities`` (this is
    today's behavior), but it never *participates in a conflict*. The
    conflict check is gated on ``provides_declared=True`` on both sides.
  - This means we can land §24.3 without forcing every pack to migrate in
    lockstep — same pattern as §24.4 storage Phase 1.

Wired into the loader by :func:`cairn.core.pack.loader.load_all_packs`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

# Phase-1 §2: profile became a typed bundle (capabilities + bindings +
# mcp_servers + backends + extends). The thin dict-shaped helpers used to
# live here; they moved to ``profile.py``. Composition consumes the
# capability disambiguation slice (``Profile.capabilities``) and is
# otherwise infrastructure-agnostic.
from cairn.core.pack.profile import (
    Profile,
    ProfileError,
    load_profile,
    load_profile_from_env,
)

if TYPE_CHECKING:
    from cairn.core.pack.protocol import Pack

logger = logging.getLogger(__name__)


# ─── Exceptions + report shape ──────────────────────────────────────


class CompositionConflict(RuntimeError):
    """A load-time composition rule failed.

    Carries a list of human-readable reasons so the message can quote
    multiple conflicts in one go (we don't fail-fast on the first — an
    operator wants to see every problem at once).
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        joined = "\n  - ".join(reasons)
        super().__init__(f"pack composition failed:\n  - {joined}")


@dataclass(frozen=True)
class CompositionReport:
    """Result of one composition pass.

    Doctor's ``[packs]`` category (P2.c) reads this verbatim — the same
    structure is rendered as `[fail]` rows (conflicts) and `[warn]` rows
    (warnings).
    """

    #: Final ``capability → [pack, …]`` map after profile overrides.
    #: Includes *both* public and permissive contributions: a downstream
    #: lookup of "who provides logs.read" needs both.
    provider_map: dict[str, list[str]] = field(default_factory=dict)
    #: Hard violations. Non-empty → strict mode raises.
    conflicts: list[str] = field(default_factory=list)
    #: Soft observations (unsatisfied optionals, unused profile overrides).
    warnings: list[str] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return not self.conflicts


# ─── Profile-aware contribution registry ────────────────────────────
#
# `load_profile` / `load_profile_from_env` are re-exported from
# ``cairn.core.pack.profile`` for back-compat with callers that import
# them from this module. New code should import directly from
# ``profile``.


def _overrides_from(profile: Optional[Profile | dict[str, str]]) -> dict[str, str]:
    """Normalise the profile argument into the capability disambiguation
    dict the registry walks.

    Accepts a :class:`Profile`, a plain ``dict[str, str]`` (legacy callers
    that already had only the capability slice), or ``None``.
    """
    if profile is None:
        return {}
    if isinstance(profile, Profile):
        return dict(profile.capabilities)
    return dict(profile)


# ─── Core: build registry + validate ────────────────────────────────


def _public_capabilities(pack: "Pack") -> set[str]:
    """The set of capability names ``pack`` *publicly* provides.

    With ``provides_declared=True``: the explicit list from the manifest.
    Without it (permissive default): every CapabilityBinding the pack
    contributes — same as today's "everything visible" behavior.
    """
    m = pack.manifest
    if m is not None and m.provides_declared:
        return set(m.provides_capabilities)
    return {b.capability for b in pack.capability_bindings}


def _is_encapsulated(pack: "Pack") -> bool:
    return pack.manifest is not None and pack.manifest.provides_declared


def _build_raw_registry(packs: list["Pack"]) -> dict[str, list[str]]:
    """Pre-override contribution map. Internal — exposed shape is the
    post-override view via :func:`build_contribution_registry`."""
    raw: dict[str, list[str]] = defaultdict(list)
    for p in packs:
        for cap in _public_capabilities(p):
            raw[cap].append(p.name)
    return dict(raw)


def build_contribution_registry(
    packs: list["Pack"],
    profile_overrides: Optional[Profile | dict[str, str]] = None,
) -> dict[str, list[str]]:
    """Map ``capability → [pack_name, …]`` across every loaded pack.

    Used both for conflict detection (multiple providers for one name)
    and dep resolution (does a required capability resolve at all).

    Profile overrides are applied *after* the raw registry is built: if
    multiple packs provide the same capability AND the profile picks one,
    the registry's list for that capability collapses to the winner.

    The override is honored even when only one pack provides the
    capability — that case is benign for routing, but
    :func:`validate_composition` still flags it as a conflict (the
    operator asked to pick X but X doesn't actually provide it, which is
    almost always a typo).
    """
    raw = _build_raw_registry(packs)
    overrides = _overrides_from(profile_overrides)
    final: dict[str, list[str]] = {}
    for cap, providers in raw.items():
        if cap in overrides and overrides[cap] in providers:
            final[cap] = [overrides[cap]]
        else:
            final[cap] = providers
    return final


def validate_composition(
    packs: list["Pack"],
    profile_overrides: Optional[Profile | dict[str, str]] = None,
) -> CompositionReport:
    """Run every §24.3 load-time check; return one report.

    Doesn't raise — the caller chooses whether to fail-hard (CLI
    commands that mutate state) or just log (doctor, dev workflow).
    """
    overrides = _overrides_from(profile_overrides)
    raw = _build_raw_registry(packs)
    registry = build_contribution_registry(packs, overrides)

    conflicts: list[str] = []
    warnings: list[str] = []

    encapsulated_names = {p.name for p in packs if _is_encapsulated(p)}
    pack_names = {p.name for p in packs}

    # ── Check 1: every profile override resolves to a real provider ──
    # Run this first so override-related conflicts surface clearly even
    # when there's no duplicate-provider situation to "rescue."
    for cap, winner in overrides.items():
        if winner not in pack_names:
            conflicts.append(
                f"profile picks {winner!r} for capability {cap!r} "
                f"but no pack named {winner!r} is installed"
            )
            continue
        providers_of_cap = raw.get(cap, [])
        if winner not in providers_of_cap:
            conflicts.append(
                f"profile picks {winner!r} for capability {cap!r} "
                f"but that pack doesn't provide it; "
                f"actual providers: {sorted(providers_of_cap) or 'none'}"
            )

    # ── Check 2: duplicate providers among encapsulated packs ──
    # Per §24.3.5: the conflict is only meaningful when both sides
    # *explicitly export* the capability. Two permissive packs sharing
    # a capability name keep today's last-writer-wins behavior — the
    # contract didn't promise otherwise.
    for cap, providers in sorted(raw.items()):
        explicit = [p for p in providers if p in encapsulated_names]
        if len(explicit) <= 1:
            continue
        # Profile resolved the duplicate?
        if cap in overrides and overrides[cap] in providers:
            continue
        conflicts.append(
            f"capability {cap!r} provided by multiple packs "
            f"{sorted(explicit)} — pick one in [profile.capabilities]"
        )

    # ── Check 3: required capabilities all resolve ──
    for p in packs:
        m = p.manifest
        if m is None:
            continue
        for cap in m.required_capabilities:
            if cap not in registry or not registry[cap]:
                conflicts.append(
                    f"pack {p.name!r} requires capability {cap!r} "
                    f"but no installed pack provides it"
                )

    # ── Check 4: optional capabilities — warn only ──
    for p in packs:
        m = p.manifest
        if m is None:
            continue
        for cap in m.optional_capabilities:
            if cap not in registry or not registry[cap]:
                warnings.append(
                    f"pack {p.name!r} optional capability {cap!r} "
                    f"unprovided; richer behavior will not activate"
                )

    # ── Phase-1 §2 loud-failure: profile-aware checks ──
    # These only run when the caller passed a typed Profile (the loader
    # path does; legacy dict callers don't). Without bindings + mcp_servers
    # the substrate has nothing to match against.
    if isinstance(profile_overrides, Profile):
        _check_profile_bindings(packs, profile_overrides, conflicts)


    return CompositionReport(
        provider_map=registry,
        conflicts=conflicts,
        warnings=warnings,
    )


# ─── Phase-2.8: trigger provider seam ───────────────────────────────


_KIND_TO_CAPABILITIES: dict[str, tuple[str, ...]] = {
    # Memory stores are read (similarity_search / exact_lookup) and
    # written (write). Both bindings must be present even if a particular
    # pack only writes — the substrate may surface the unused one later
    # when other packs share the store.
    "memory":    ("memory.search", "memory.write"),
    # RAG corpora are searched + occasionally fetched verbatim by id.
    "rag":       ("rag.search", "rag.fetch"),
    # Artifacts are written; reads pull through storage.get.
    "artifacts": ("storage.put", "storage.get"),
}


def _check_profile_bindings(
    packs: list["Pack"],
    profile: Profile,
    conflicts: list[str],
) -> None:
    """Verify every pack-side semantic requirement has a profile-side wiring.

    Appends one ``conflicts`` line per mismatch with a ``fix:`` hint
    naming what the env owner must do. Doctor renders these verbatim
    under the ``[packs]`` category as ``composition_conflict`` fail rows.

    An *empty* profile (no bindings AND no mcp_servers) is treated as
    "no profile configured" — the loud-failure pass skips entirely. This
    is the loader's default when ``CAIRN_PROFILE`` is unset and matches
    the pre-Phase-1 behavior where doctor passed on a default install.
    Once a profile carries any binding the pass kicks in for every pack.
    """
    bindings = profile.bindings
    mcp_servers = set(profile.mcp_servers.keys())
    if not bindings and not mcp_servers:
        return

    for pack in packs:
        m = pack.manifest
        if m is None:
            continue

        # ── memory / rag / artifacts: every store needs its kind's capabilities bound
        for kind, store_dict in (
            ("memory",    m.required_memory),
            ("rag",       m.required_rag),
            ("artifacts", m.required_artifacts),
        ):
            if not store_dict:
                continue
            for cap in _KIND_TO_CAPABILITIES[kind]:
                if cap not in bindings:
                    conflicts.append(
                        f"pack {pack.name!r} requires [{kind}.{next(iter(store_dict))!r}] "
                        f"but profile has no binding for {cap!r}; "
                        f"fix: add `\"{cap}\" = \"<operator-pack-ref>\"` "
                        f"under [profile.bindings] (e.g. {kind}/sqlite-vector "
                        f"for local-dev) or install an operator pack that "
                        f"provides it"
                    )

        # ── mcp: declared servers must be bound; declared tools must
        # reference declared servers; mcp.call must be bound if the pack
        # touches MCP at all.
        mcp_req = m.required_mcp
        if mcp_req is None:
            continue
        for server in mcp_req.servers:
            if server not in mcp_servers:
                conflicts.append(
                    f"pack {pack.name!r} requires MCP server {server!r} "
                    f"but profile has no [profile.mcp_servers.{server}] entry; "
                    f"fix: add the server to the profile (transport + url/command), "
                    f"or alias an existing one via env overrides"
                )
        # tool entries look like "<server>:<tool>" — the prefix must be
        # in the pack's own declared servers (consistency check on the
        # pack itself, surfaced here because it's load-time).
        declared = set(mcp_req.servers)
        for tool_ref in mcp_req.tools:
            if ":" not in tool_ref:
                conflicts.append(
                    f"pack {pack.name!r} [requires.mcp].tools entry "
                    f"{tool_ref!r} must be '<server>:<tool>'"
                )
                continue
            server_prefix = tool_ref.split(":", 1)[0]
            if server_prefix not in declared:
                conflicts.append(
                    f"pack {pack.name!r} [requires.mcp].tools entry "
                    f"{tool_ref!r} references server {server_prefix!r} "
                    f"which isn't in [requires.mcp].servers; "
                    f"fix: add {server_prefix!r} to the servers list or "
                    f"remove the tool entry"
                )
        # mcp.call binding is what the substrate dispatches all mcp_call
        # primitives through. A pack that touches MCP needs this bound.
        if mcp_req.servers and "mcp.call" not in bindings:
            conflicts.append(
                f"pack {pack.name!r} declares MCP servers but profile has "
                f"no binding for 'mcp.call'; "
                f"fix: add `\"mcp.call\" = \"mcp/call\"` under "
                f"[profile.bindings] (the substrate ships mcp/call as a "
                f"reference operator)"
            )
