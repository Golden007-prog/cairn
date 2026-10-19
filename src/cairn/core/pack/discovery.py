"""Template discovery — the server-side implementation of ``GET /packs``.

Moved here from ``cairn.ask.resolve`` (#1196, ADR-0085 D7): core owns the
read's implementation, the route exposes it, and ask CONSUMES the route via
cairn-client. Before the move the dependency was bidirectional (server.py
imported cairn.ask.resolve for this) — that inversion is what the move fixes.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter

from cairn.core.pack.curation import card_blurb, visibility_of


def candidate_templates() -> list[dict[str, str]]:
    """``[{ref, category, description, visibility}]`` for every registered
    template — the same source ``cairn ls`` reads (pack ``templates_dir`` +
    frontmatter).

    Every template is returned, ANNOTATED with its curation tier (#1566) rather
    than filtered. Dropping rows here would make the API lie to an operator
    asking what this deployment has; curating is the caller's job, and each
    human surface does it with :mod:`cairn.core.pack.curation`.
    """
    from cairn.core.pack.registry import flow_dirs_of, get_packs  # noqa: PLC0415 — registry↔pack cycle

    out: list[dict[str, str]] = []
    for p in get_packs():
        # #1163 — `flows/` and the `templates/` alias both hold agentic flows.
        for f in sorted({f for td in flow_dirs_of(p) for f in Path(td).glob("*.md")}):
            try:
                meta = frontmatter.load(f).metadata
            except Exception:
                continue
            desc = " ".join(str(meta.get("description") or "").split())
            out.append({
                "ref": f"{p.name}/{f.stem}",
                "category": str(meta.get("module") or ""),
                "description": desc[:300],
                # #1566 — the "Available to enable" cards render THIS, and the
                # raw 300 chars put "(ADR-0081 P2c, #1053)" in front of a user
                # deciding whether to enable a workflow. Same trimming the
                # installed cards get, so a template reads the same on either
                # shelf. `description` stays for callers that want the full
                # text (`cairn ask` resolution matches against it).
                "blurb": card_blurb(desc),
                "visibility": visibility_of(meta),
            })
    return out
