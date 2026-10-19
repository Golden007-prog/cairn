"""Tests for §24.11.3 content-addressed template identity.

The hash is the foundation for:
  - Mutable tags (§24.11.3) — tag → hash → load
  - Replay pinning (``runs.template_hash``) — every run records its version
  - Regression eval (§24.11.4) — ``--compare-against <tag-or-hash>`` resolves
    to a specific immutable hash

Properties we exercise here (all pure-logic — no LLM, no filesystem):
  1. Stable: identical (spec, body) → same hash
  2. Sensitive to body changes
  3. Sensitive to frontmatter VALUE changes
  4. INsensitive to frontmatter KEY ORDER (canonical projection)
  5. Sensitive to whitespace in the body (a re-indented prompt IS a
     different prompt to the LLM)
  6. ``short_hash`` matches the prefix convention
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine.compiled import CompiledTemplate, compute_content_hash
from cairn.core.engine.schema import TemplateSpec


def _spec(**overrides) -> TemplateSpec:
    """Minimal valid TemplateSpec for hashing tests."""
    base = {
        "name": "test-template",
        "version": "0.1.0",
        "description": "test",
        "flow": {"type": "prompt", "model": "gpt-4o-mini"},
        "tools": {"required": []},
        "output": {"schema": "free_text", "format": "text"},
    }
    base.update(overrides)
    return TemplateSpec.model_validate(base)


# ─── Stability ────────────────────────────────────────────────────────


def test_hash_is_deterministic_across_calls():
    """Same input → same hash, always."""
    spec = _spec()
    body = "# Instructions\n\nDo the thing.\n"
    h1 = compute_content_hash(spec, body)
    h2 = compute_content_hash(spec, body)
    assert h1 == h2


def test_hash_has_sha256_prefix():
    """Algorithm prefix prevents ambiguity if we ever migrate."""
    h = compute_content_hash(_spec(), "body")
    assert h.startswith("sha256:")
    # sha256 is 64 hex chars + "sha256:" = 71 total
    assert len(h) == 7 + 64


# ─── Sensitivity to changes that SHOULD invalidate ────────────────────


def test_hash_changes_when_body_changes():
    spec = _spec()
    h1 = compute_content_hash(spec, "Original prompt.")
    h2 = compute_content_hash(spec, "Edited prompt.")
    assert h1 != h2


def test_hash_changes_when_frontmatter_value_changes():
    """Changing the model is a semantic edit — must invalidate."""
    h1 = compute_content_hash(_spec(flow={"type": "prompt", "model": "gpt-4o"}), "body")
    h2 = compute_content_hash(_spec(flow={"type": "prompt", "model": "gpt-4o-mini"}), "body")
    assert h1 != h2


def test_hash_changes_when_version_field_changes():
    """The ``version`` field is part of identity."""
    h1 = compute_content_hash(_spec(version="0.1.0"), "body")
    h2 = compute_content_hash(_spec(version="0.2.0"), "body")
    assert h1 != h2


def test_hash_distinguishes_body_vs_frontmatter_boundary():
    """A template with content X in frontmatter and Y in body must hash
    differently than one with Y in frontmatter and X in body. The
    explicit ``---body---`` boundary in the hasher prevents accidental
    collisions across the seam."""
    h1 = compute_content_hash(_spec(description="foo"), "bar")
    h2 = compute_content_hash(_spec(description="bar"), "foo")
    assert h1 != h2


# ─── INsensitivity to noise ───────────────────────────────────────────


def test_hash_insensitive_to_frontmatter_key_order():
    """Canonical projection means the same content with different key
    ordering produces the same hash. YAML doesn't preserve order
    reliably; we shouldn't either."""
    # Both produce equivalent TemplateSpec objects regardless of which
    # keys came first in the source YAML.
    s1 = TemplateSpec.model_validate({
        "name": "t",
        "version": "1.0.0",
        "description": "x",
        "flow": {"type": "prompt", "model": "gpt-4o-mini"},
        "tools": {"required": []},
        "output": {"schema": "free_text", "format": "text"},
    })
    s2 = TemplateSpec.model_validate({
        "output": {"schema": "free_text", "format": "text"},
        "tools": {"required": []},
        "flow": {"type": "prompt", "model": "gpt-4o-mini"},
        "description": "x",
        "version": "1.0.0",
        "name": "t",
    })
    assert compute_content_hash(s1, "body") == compute_content_hash(s2, "body")


def test_hash_sensitive_to_body_whitespace():
    """A re-indented prompt is a different prompt — LLM sees the
    whitespace. We don't try to be clever about normalizing it."""
    h1 = compute_content_hash(_spec(), "  Indented prompt.\n")
    h2 = compute_content_hash(_spec(), "Indented prompt.\n")
    assert h1 != h2


# ─── CompiledTemplate.short_hash ──────────────────────────────────────


def test_short_hash_matches_docker_git_convention():
    """First 12 hex chars after the algorithm prefix — like Docker
    sha256 short refs and git short hashes."""
    full = "sha256:abc123def456789012345678901234567890abcdef1234567890abcdef1234"
    ct = CompiledTemplate(
        spec=_spec(),
        prose="body",
        flow_type="prompt",
        runnable=None,  # type: ignore[arg-type] — not exercised here
        content_hash=full,
    )
    assert ct.short_hash == "abc123def456"
    assert len(ct.short_hash) == 12


def test_short_hash_empty_when_unset():
    """Old code paths that don't set content_hash → empty short_hash,
    not a crash."""
    ct = CompiledTemplate(
        spec=_spec(),
        prose="body",
        flow_type="prompt",
        runnable=None,  # type: ignore[arg-type]
    )
    assert ct.content_hash == ""
    assert ct.short_hash == ""
