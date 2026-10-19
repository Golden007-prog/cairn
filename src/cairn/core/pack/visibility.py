"""§24.3 P2.d — encapsulation at the lookup seam.

The composition contract (P2.b) declared *what* each pack exports; this
module enforces *who can see what* at runtime.

Truth table (§24.3.10 with the encapsulation toggle from
:attr:`PackManifest.provides_declared`):

  ┌──────────┬─────────────┬──────────────┬───────────────────────┬────┐
  │ from     │ owning      │ encapsulated │ name in provides_*    │ ok │
  ├──────────┼─────────────┼──────────────┼───────────────────────┼────┤
  │ X        │ X           │ —            │ —                     │ ✓  │  within-pack
  │ X        │ Y           │ no (legacy)  │ —                     │ ✓  │  permissive default
  │ X        │ Y           │ yes          │ yes                   │ ✓  │  explicit export
  │ X        │ Y           │ yes          │ no                    │ ✗  │  private to Y
  └──────────┴─────────────┴──────────────┴───────────────────────┴────┘

What lands in P2.d:

  - :func:`is_capability_visible` — the predicate.
  - :func:`visible_capability_bindings` — filtered registry walk.
  - :func:`owning_pack_for_capability` — answers "which pack does this
    capability live in?" for the *first* matching binding (callers
    asking this are already in a single-provider regime; multi-provider
    is a composition conflict, handled in P2.b).

What does NOT land in P2.d:

  - Template lookup encapsulation — P2.e covers cross-pack subgraph
    refs and resolves at the template loader.
  - Policy capability attachment — P2.f extends PolicySpec.applies_to.
  - Teaching an MCP surface its caller pack (the pack-tools server,
    ``services/mcp/server.py``, was retired in #2425). MCP has no
    per-call calling-pack context today; that's
    its own design problem and will be revisited if/when the bus
    (§24.10) gives us a sender identity.

The existing :func:`all_capability_bindings` (permissive global view)
stays — pre-P2 consumers don't break. New code that *does* know its
calling pack uses the filtered API here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cairn.core.pack.protocol import CapabilityBinding, Pack

logger = logging.getLogger(__name__)


def _is_encapsulated(pack: "Pack") -> bool:
    m = pack.manifest
    return m is not None and m.provides_declared


def _provides_capabilities(pack: "Pack") -> set[str]:
    m = pack.manifest
    if m is None or not m.provides_declared:
        return set()
    return set(m.provides_capabilities)


def is_capability_visible(
    capability: str,
    *,
    owning_pack: "Pack",
    from_pack: Optional[str],
) -> bool:
    """Apply the §24.3 visibility rule to a single (cap, owner, caller) tuple.

    ``from_pack=None`` represents a "global" caller — the engine itself,
    a CLI command, the MCP gateway — and gets today's everything-visible
    semantics. This preserves backward compat for callers that don't yet
    carry a calling-pack identity.

    Use this primitive when you're iterating a flat list of bindings and
    need to drop the ones a specific caller shouldn't see. Reach for
    :func:`visible_capability_bindings` instead when you have the
    registry-level view.
    """
    # Within-pack: everything visible regardless of provides_*.
    if from_pack is not None and from_pack == owning_pack.name:
        return True
    # Permissive owner: today's everything-visible default.
    if not _is_encapsulated(owning_pack):
        return True
    # Global caller (no identity) — preserve today's behavior for
    # consumers that haven't migrated to calling-pack-aware lookup yet.
    if from_pack is None:
        return True
    # Encapsulated owner + cross-pack caller → must be explicitly exported.
    return capability in _provides_capabilities(owning_pack)


def visible_capability_bindings(
    *,
    from_pack: Optional[str] = None,
    packs: Optional[list["Pack"]] = None,
) -> list["CapabilityBinding"]:
    """Filtered version of :func:`all_capability_bindings`.

    ``from_pack=None`` returns the same flat list ``all_capability_bindings``
    does (today's permissive view). Specifying a calling pack name enforces
    the §24.3 visibility rule per binding.

    ``packs`` is injectable for testing; defaults to the live registry.
    """
    if packs is None:
        from cairn.core.pack.registry import get_packs
        packs = get_packs()

    out: list[CapabilityBinding] = []
    for p in packs:
        for b in p.capability_bindings:
            if is_capability_visible(
                b.capability, owning_pack=p, from_pack=from_pack,
            ):
                out.append(b)
    return out


def _provides_templates(pack: "Pack") -> set[str]:
    m = pack.manifest
    if m is None or not m.provides_declared:
        return set()
    return set(m.provides_templates)


def is_template_visible(
    template_name: str,
    *,
    owning_pack: "Pack",
    from_pack: Optional[str],
) -> bool:
    """Apply §24.3 visibility to a (template, owner, caller) tuple.

    Same shape as :func:`is_capability_visible` — the rule's symmetric
    over artifact kinds; only the field name (``provides_capabilities``
    vs ``provides_templates``) changes.

    ``template_name`` is the *local* name (no ``<pack>/`` prefix) —
    what would appear in ``[provides.templates]`` and what
    ``resolve_template_path`` ultimately matches against a ``.md`` file.
    """
    if from_pack is not None and from_pack == owning_pack.name:
        return True
    if not _is_encapsulated(owning_pack):
        return True
    if from_pack is None:
        return True
    return template_name in _provides_templates(owning_pack)


def owning_pack_for_capability(
    capability: str,
    *,
    packs: Optional[list["Pack"]] = None,
) -> Optional[str]:
    """First pack name whose bindings include ``capability``.

    Returns ``None`` when no pack provides it. This is the
    "where does this come from?" lookup the runtime needs when it has a
    capability call in hand and wants to attribute provenance.

    When two encapsulated packs both provide the same capability that's
    already a :class:`CompositionConflict` (caught in P2.b), so the
    first-match semantics here aren't ambiguous in a valid deployment.
    """
    if packs is None:
        from cairn.core.pack.registry import get_packs
        packs = get_packs()
    for p in packs:
        for b in p.capability_bindings:
            if b.capability == capability:
                return p.name
    return None
