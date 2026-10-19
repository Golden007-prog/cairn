"""Schema substrate — §24.20.10 + SCH1–SCH5 (#189–#193).

Public surface:

  - URI parsing: :func:`parse_schema_uri`, :func:`build_schema_uri`,
    :func:`is_schema_uri`, :class:`SchemaURI`.
  - Registry: :class:`SchemaRegistry`, process-wide
    :data:`default_registry`, decorator-style
    :func:`register_schema`.
  - Errors: :class:`SchemaURIError`, :class:`SchemaRegistryError`,
    :class:`UnknownSchemaError`, :class:`DuplicateSchemaError`.

What SCH1 owns: URI grammar + in-memory registry. Pure data plumbing.

What lands in subsequent tasks:

  - SCH2 #190 — ``yaml_loader`` for JSON Schema YAML files in
    ``<pack>/schemas/*.yaml``; reads ``$id`` and registers.
  - SCH3 #191 — Pydantic→JSON Schema exporter for operator packs
    (build-time only; the operator-pack ``schemas/`` dir is generated).
  - SCH4 #192 — compiler dispatch: ``resolve_schema`` learns to route
    ``schema://`` URIs to this registry while keeping the legacy
    dotted-path Python form working.
  - SCH5 #193 — doctor ``[schemas]`` category for pack-level checks
    (collision detection, PascalCase, no inline JSON Schema in
    templates, etc.).
"""

from __future__ import annotations

from cairn.core.schema.registry import (
    DuplicateSchemaError,
    SchemaRegistry,
    SchemaRegistryError,
    UnknownSchemaError,
    default_registry,
    register_schema,
)
from cairn.core.schema.uri import (
    PREFIX,
    SCHEME,
    SchemaURI,
    SchemaURIError,
    build_schema_uri,
    is_schema_uri,
    parse_schema_uri,
)
from cairn.core.schema.yaml_loader import (
    DRAFT_2020_12_URI,
    SchemaYamlError,
    discover_schema_files,
    load_pack_schemas,
    load_schema_yaml,
)

__all__ = [
    # URI
    "SCHEME",
    "PREFIX",
    "SchemaURI",
    "SchemaURIError",
    "is_schema_uri",
    "parse_schema_uri",
    "build_schema_uri",
    # Registry
    "SchemaRegistry",
    "default_registry",
    "register_schema",
    # YAML loader (SCH2)
    "DRAFT_2020_12_URI",
    "SchemaYamlError",
    "load_schema_yaml",
    "discover_schema_files",
    "load_pack_schemas",
    # Errors
    "SchemaRegistryError",
    "UnknownSchemaError",
    "DuplicateSchemaError",
]
