"""``schema://`` URI parser (§24.20.10 + SCH1 #189).

The §24.20.10 lock specifies a schema's identity as ``<pack>/<SchemaName>``
(its ``$id`` field for JSON Schema YAML files; the equivalent registered
name for Pydantic-class operator schemas).

The ``schema://`` URI is the in-template handle for that identity:

.. code-block:: yaml

    input_schema:  schema://incident/AlertContext
    output_schema: schema://incident/RCA

This module owns the URI ↔ (pack, name) bidirectional mapping. The
registry (see :mod:`cairn.core.schema.registry`) does lookup; this
module does parsing only. Keeping the two split makes the URI grammar
the one place doctors and the YAML loader can reach for syntactic
validation.
"""

from __future__ import annotations

import re
from typing import NamedTuple


SCHEME = "schema"
PREFIX = f"{SCHEME}://"

# §24.20.10: pack names and schema names follow the same casing convention as
# Python identifiers — letters, digits, underscore, hyphen for packs (kebab
# allowed); SchemaNames are PascalCase by convention but we accept any
# non-empty identifier-style token here. The doctor enforces PascalCase
# separately (SCH5 #193).
_PACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchemaURIError(ValueError):
    """Raised when a string is not a well-formed ``schema://`` URI."""


class SchemaURI(NamedTuple):
    """Parsed ``schema://<pack>/<name>`` URI.

    The tuple form is intentional: schema URIs are immutable identity
    tokens, and ``NamedTuple`` gives the registry a hashable key plus a
    cheap repr without dragging dataclass machinery in.
    """

    pack: str
    name: str

    @property
    def key(self) -> str:
        """Registry key form: ``<pack>/<name>``.

        This matches the ``$id`` field of JSON Schema YAML files exactly,
        so loaders can use the schema's own ``$id`` to derive the URI
        without re-parsing.
        """
        return f"{self.pack}/{self.name}"

    @property
    def uri(self) -> str:
        """Reconstruct the canonical URI string."""
        return f"{PREFIX}{self.key}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.uri


def is_schema_uri(value: object) -> bool:
    """Cheap predicate — does ``value`` look like a ``schema://`` URI?

    Used by callers that may receive either a legacy dotted-path Python
    reference or a new ``schema://`` URI (the compiler integration in
    SCH4 #192 is the main caller). Doesn't validate well-formedness;
    that's :func:`parse_schema_uri`'s job.
    """
    return isinstance(value, str) and value.startswith(PREFIX)


def parse_schema_uri(value: str) -> SchemaURI:
    """Parse a ``schema://<pack>/<name>`` URI into its components.

    :raises SchemaURIError: ``value`` is not a string, missing the
        ``schema://`` prefix, missing a pack or name segment, contains
        more than one ``/`` (the URI is intentionally flat — no nested
        namespaces), or has characters outside the allowed identifier
        sets.
    """
    if not isinstance(value, str):
        raise SchemaURIError(
            f"schema URI must be a string, got {type(value).__name__}"
        )
    if not value.startswith(PREFIX):
        raise SchemaURIError(
            f"schema URI {value!r} must start with {PREFIX!r}"
        )
    body = value[len(PREFIX):]
    if not body:
        raise SchemaURIError(
            f"schema URI {value!r} is missing the <pack>/<name> body"
        )
    parts = body.split("/")
    if len(parts) != 2:
        raise SchemaURIError(
            f"schema URI {value!r} must have exactly one '/' separating "
            f"pack and name (got {len(parts)} segments)"
        )
    pack, name = parts
    if not pack:
        raise SchemaURIError(
            f"schema URI {value!r} is missing the pack segment"
        )
    if not name:
        raise SchemaURIError(
            f"schema URI {value!r} is missing the name segment"
        )
    if not _PACK_RE.match(pack):
        raise SchemaURIError(
            f"schema URI {value!r}: pack {pack!r} must match "
            f"[A-Za-z][A-Za-z0-9_-]*"
        )
    if not _NAME_RE.match(name):
        raise SchemaURIError(
            f"schema URI {value!r}: name {name!r} must match "
            f"[A-Za-z_][A-Za-z0-9_]*"
        )
    return SchemaURI(pack=pack, name=name)


def build_schema_uri(pack: str, name: str) -> str:
    """Build a canonical ``schema://<pack>/<name>`` URI from components.

    Validates the components by routing through :func:`parse_schema_uri`,
    so callers never accidentally construct a malformed URI.
    """
    return parse_schema_uri(f"{PREFIX}{pack}/{name}").uri


__all__ = [
    "SCHEME",
    "PREFIX",
    "SchemaURI",
    "SchemaURIError",
    "is_schema_uri",
    "parse_schema_uri",
    "build_schema_uri",
]
