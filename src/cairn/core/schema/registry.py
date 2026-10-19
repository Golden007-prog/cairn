"""Schema registry (SCH1 #189).

Maps a ``schema://<pack>/<name>`` URI to whatever artifact represents
that schema. SCH1 stores values as opaque ``Any``: SCH2 #190 will
populate it with JSON Schema dicts loaded from ``schemas/*.yaml`` files
in domain packs; SCH3 #191 will populate it with Pydantic classes
exported from operator packs at build time.

The split is intentional. Domain authors write JSON Schema YAML;
operator authors write Pydantic — both flow into the same registry
keyed by the canonical ``<pack>/<name>`` identity. SCH4 #192 will
introduce a uniform validation wrapper at the consumer (compiler) side
so callers don't have to branch on the registered type.

This module mirrors :class:`cairn.core.operator.registry.OperatorRegistry`
shape on purpose: register / get / has / list / clear, plus a
process-wide :data:`default_registry`. Keeping the surfaces parallel
makes the substrate predictable for pack authors and pack tooling.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from cairn.core.schema.uri import (
    SchemaURI,
    SchemaURIError,
    is_schema_uri,
    parse_schema_uri,
)


class SchemaRegistryError(ValueError):
    """Base class for registry-level failures."""


class UnknownSchemaError(SchemaRegistryError, KeyError):
    """Raised when a URI is not registered.

    Inherits from :class:`KeyError` so ``except KeyError:`` patterns at
    callers keep working — that's the historical Python idiom for
    "not in registry".
    """


class DuplicateSchemaError(SchemaRegistryError):
    """Raised when a different artifact is already registered under the
    same identity. Re-registering the exact same object is idempotent
    (no error) so loader retries and reload-on-edit flows stay clean."""


class SchemaRegistry:
    """In-memory map of ``schema://<pack>/<name>`` → artifact.

    Artifact type is intentionally unconstrained at SCH1. SCH2 / SCH3
    will register dicts and Pydantic classes respectively; SCH4 will
    add a wrapper to give consumers a uniform validation interface.

    Idempotency rule: registering the SAME object under an existing
    key is a no-op; registering a DIFFERENT object under the same key
    raises :class:`DuplicateSchemaError`. This mirrors the operator
    registry's behavior and is what the SCH2 YAML loader relies on for
    its "fail loud on conflict, silent on retry" semantics.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Any] = {}

    # ── Register ─────────────────────────────────────────────────────

    def register(
        self,
        uri_or_pack: str,
        artifact: Any,
        *,
        name: str | None = None,
    ) -> SchemaURI:
        """Register an artifact under a ``schema://<pack>/<name>`` URI.

        Two call shapes:

        - ``register("schema://incident/RCA", obj)`` — URI form
        - ``register("incident", obj, name="RCA")`` — pack/name form

        Returns the parsed :class:`SchemaURI` for caller convenience
        (the SCH2 loader uses it to log the canonical form).

        :raises SchemaURIError: malformed URI.
        :raises DuplicateSchemaError: a different artifact is already
            registered under this key.
        """
        if name is not None:
            # pack/name form
            parsed = parse_schema_uri(f"schema://{uri_or_pack}/{name}")
        elif is_schema_uri(uri_or_pack):
            parsed = parse_schema_uri(uri_or_pack)
        else:
            raise SchemaURIError(
                f"register() needs either a schema:// URI or pack+name; "
                f"got {uri_or_pack!r}"
            )

        existing = self._by_key.get(parsed.key)
        if existing is not None and existing is not artifact:
            # Identity miss is fine if the values are equal — the YAML
            # loader produces a fresh dict on every re-read, so packs
            # that re-register at idempotent ensure_loaded() points are
            # not real conflicts. Only structurally different artifacts
            # (or non-dict types) are the duplicate the lock guards.
            try:
                same_value = existing == artifact
            except Exception:
                same_value = False
            if not same_value:
                raise DuplicateSchemaError(
                    f"schema {parsed.uri!r}: a different artifact is already "
                    f"registered (existing={type(existing).__name__}, "
                    f"new={type(artifact).__name__})"
                )
        self._by_key[parsed.key] = artifact
        return parsed

    # ── Lookup ───────────────────────────────────────────────────────

    def get(self, uri: str) -> Any:
        """Look up an artifact by ``schema://`` URI.

        :raises UnknownSchemaError: URI not registered. Error message
            lists the registered keys to make typos obvious.
        :raises SchemaURIError: URI is malformed.
        """
        parsed = parse_schema_uri(uri)
        if parsed.key not in self._by_key:
            raise UnknownSchemaError(
                f"schema {uri!r} not registered (known: "
                f"{sorted(self._by_key.keys())})"
            )
        return self._by_key[parsed.key]

    def has(self, uri: str) -> bool:
        """``True`` if the URI is registered. Returns ``False`` for
        malformed URIs rather than raising — convenience for callers
        doing "is it present?" style checks."""
        try:
            parsed = parse_schema_uri(uri)
        except SchemaURIError:
            return False
        return parsed.key in self._by_key

    # ── Introspection ────────────────────────────────────────────────

    def list_uris(self) -> list[str]:
        """All registered URIs in canonical ``schema://...`` form,
        sorted lexicographically — stable for snapshot tests."""
        return sorted(f"schema://{k}" for k in self._by_key)

    def all(self) -> dict[str, Any]:
        """Shallow copy of the URI → artifact map. Callers can mutate
        the returned dict without affecting the registry."""
        return {f"schema://{k}": v for k, v in self._by_key.items()}

    # ── Lifecycle ────────────────────────────────────────────────────

    def clear(self) -> None:
        """Drop all registrations. Used by tests and by reload flows."""
        self._by_key.clear()

    def update(self, mapping: Mapping[str, Any]) -> None:
        """Bulk-register from a mapping of URI → artifact. Each entry
        goes through :meth:`register`, so conflicts still raise."""
        for uri, artifact in mapping.items():
            self.register(uri, artifact)

    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, uri: object) -> bool:
        if not isinstance(uri, str):
            return False
        return self.has(uri)

    def __iter__(self) -> Iterable[str]:  # pragma: no cover - trivial
        return iter(self.list_uris())


# ─── Process-wide default ────────────────────────────────────────────────


default_registry = SchemaRegistry()
"""Module-global registry. SCH2's YAML loader registers into this on
``load_pack_schemas``; SCH3's Pydantic exporter registers into this
during operator-pack builds. SCH4's compiler resolves URIs against it.

Tests that need isolation create a local :class:`SchemaRegistry` and
pass it in explicitly (the SCH2 loader will take a ``registry=``
keyword)."""


def register_schema(
    uri_or_pack: str,
    artifact: Any,
    *,
    name: str | None = None,
    registry: SchemaRegistry | None = None,
) -> SchemaURI:
    """Module-level convenience wrapper around
    :meth:`SchemaRegistry.register` that defaults to
    :data:`default_registry`."""
    target = registry if registry is not None else default_registry
    return target.register(uri_or_pack, artifact, name=name)


__all__ = [
    "SchemaRegistry",
    "SchemaRegistryError",
    "UnknownSchemaError",
    "DuplicateSchemaError",
    "default_registry",
    "register_schema",
]
