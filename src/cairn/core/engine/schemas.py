"""Resolve schema references in template frontmatter + subgraph + operation bindings.

A template can declare its input/output contract two ways:

  1. Legacy (Phase 2a): fully-qualified Python dotted path to a Pydantic
     model::

         input_schema:  cairn.packs.incident.code.schemas.X.Input

  2. URI form (SCH1 #189 + SCH4 #192): ``schema://<pack>/<SchemaName>``
     looked up in the process-wide :data:`SchemaRegistry`::

         input_schema:  schema://incident/RCA

Both forms coexist by design — the URI form is the §24.20.10 lock and
becomes the recommended path; the dotted-path form keeps legacy Pydantic
schemas working without a forced migration.

The compiler calls :func:`resolve_schema` for each declared reference;
SCH4 also adds :func:`validate_operation_binding` for compile-time
operation binding pinning (analogous to ``_validate_subgraph_mapping``
but for the ``OperationBTNode`` step type).
"""

from __future__ import annotations

import importlib
from typing import Any, Optional, Union

from pydantic import BaseModel

from cairn.core.schema import (
    SchemaURIError,
    UnknownSchemaError,
    default_registry,
    is_schema_uri,
)


# A resolved schema can be a Pydantic class (legacy dotted path OR a
# Pydantic-shaped artifact registered under a schema:// URI by SCH3) or
# a JSON Schema dict (loaded from YAML by SCH2).
ResolvedSchema = Union[type[BaseModel], dict]


class SchemaResolutionError(ValueError):
    """A schema reference in a template failed to resolve.

    Always carries the exact reference + reason — template authors
    should never have to guess which import broke or which URI is
    missing from the registry.
    """


def resolve_schema(ref: Optional[str]) -> Optional[ResolvedSchema]:
    """Resolve a template-level schema reference.

    Dispatch:

    - ``None`` → ``None`` (the field is optional; not declaring a schema is fine).
    - String starting with ``schema://`` → :data:`default_registry` lookup;
      raises :class:`SchemaResolutionError` if the URI isn't registered.
    - Anything else → fully-qualified Python dotted path, imported via
      :func:`importlib.import_module` and checked for ``BaseModel`` subclass.

    Returns either a Pydantic class or a JSON Schema dict, depending on
    what was registered (URI form) or imported (dotted path). Callers
    needing a uniform interface use :func:`required_fields` /
    :func:`schema_fields`, which handle both shapes.
    """
    if ref is None:
        return None
    if not isinstance(ref, str) or not ref.strip():
        raise SchemaResolutionError(
            f"schema reference must be a non-empty string, got {ref!r}"
        )

    # ── schema:// URI form (SCH1 #189 + SCH4 #192) ───────────────────
    if is_schema_uri(ref):
        try:
            return default_registry.get(ref)
        except UnknownSchemaError as e:
            raise SchemaResolutionError(
                f"schema reference {ref!r}: not registered — {e}"
            ) from e
        except SchemaURIError as e:
            raise SchemaResolutionError(
                f"schema reference {ref!r}: malformed URI — {e}"
            ) from e

    # ── Legacy dotted-path Python form ───────────────────────────────
    if "." not in ref:
        raise SchemaResolutionError(
            f"schema reference {ref!r} must be either a schema:// URI or a "
            f"fully-qualified Python dotted path (module.ClassName)"
        )

    module_path, _, attr = ref.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise SchemaResolutionError(
            f"schema reference {ref!r}: cannot import module "
            f"{module_path!r} — {e}"
        ) from e

    if not hasattr(module, attr):
        raise SchemaResolutionError(
            f"schema reference {ref!r}: module {module_path!r} has "
            f"no attribute {attr!r}"
        )

    obj = getattr(module, attr)

    if not isinstance(obj, type):
        raise SchemaResolutionError(
            f"schema reference {ref!r} resolved to "
            f"{type(obj).__name__}, not a class"
        )

    if not issubclass(obj, BaseModel):
        raise SchemaResolutionError(
            f"schema reference {ref!r} resolved to {obj.__name__}, "
            f"which is not a Pydantic BaseModel subclass"
        )

    return obj


# ─── Polymorphic field accessors ─────────────────────────────────────────
#
# Both Pydantic classes and JSON Schema dicts encode the same two
# concepts (required field names, all field names). These helpers
# branch on shape so the compiler's subgraph + operation validators
# can stay agnostic.


def required_fields(schema: ResolvedSchema) -> set[str]:
    """Names of fields that have no default and must be provided.

    - Pydantic class: ``FieldInfo.is_required()``.
    - JSON Schema dict: the ``required: [...]`` array (defaults to empty
      when omitted — vanilla JSON Schema semantics).
    """
    if isinstance(schema, dict):
        req = schema.get("required", [])
        if not isinstance(req, list):
            # JSON Schema spec says required is an array; the YAML
            # loader's meta-validator rejects non-arrays, so this is
            # defensive only.
            return set()
        return {str(name) for name in req}
    return {
        name for name, field in schema.model_fields.items()
        if field.is_required()
    }


def schema_fields(schema: ResolvedSchema) -> set[str]:
    """Every declared field on ``schema``, required or optional.

    - Pydantic class: ``model_fields`` keys.
    - JSON Schema dict: ``properties: {...}`` keys.
    """
    if isinstance(schema, dict):
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            return set()
        return set(props.keys())
    return set(schema.model_fields.keys())


# ─── Field-level types (#1540) ───────────────────────────────────────────
#
# The composition boundary is typed with the SAME polymorphic stance as the
# accessors above: both schema shapes reduce to JSON-Schema type names per
# field, and "unknown" (a $ref, an unconstrained field, an unparseable spec)
# is an EMPTY set — never a guess. The wire check treats empty as "not
# enough information to object" (the artifacts.py adoption philosophy,
# applied per field instead of per artifact kind).


def _json_types(spec: Any) -> frozenset[str]:
    """JSON-Schema type names one property spec admits; empty = unknown.

    ``anyOf`` unions its branches, but ONE unknown branch makes the whole
    field unknown — a partial answer would manufacture false mismatches.
    ``null`` is dropped after an ``anyOf`` union: ``Optional[str]`` wires
    like ``str`` (the optional-wire question belongs to `consumes: X?`,
    #1251, not to the type)."""
    if not isinstance(spec, dict):
        return frozenset()
    t = spec.get("type")
    if isinstance(t, str):
        return frozenset({t})
    if isinstance(t, list):
        return frozenset(str(x) for x in t)
    any_of = spec.get("anyOf")
    if isinstance(any_of, list) and any_of:
        out: set[str] = set()
        for sub in any_of:
            sub_t = _json_types(sub)
            if not sub_t:
                return frozenset()
            out |= sub_t
        return frozenset(out - {"null"})
    return frozenset()


def field_types(schema: ResolvedSchema) -> dict[str, frozenset[str]]:
    """Per-field admissible JSON-Schema types; an empty set = unknown.

    - Pydantic class: via ``model_json_schema()`` properties.
    - JSON Schema dict: its ``properties`` directly.
    """
    if isinstance(schema, dict):
        props = schema.get("properties", {})
    else:
        try:
            props = schema.model_json_schema().get("properties", {})
        except Exception:  # noqa: BLE001 — an unschematizable model is "unknown", not an error
            return {}
    if not isinstance(props, dict):
        return {}
    return {str(k): _json_types(v) for k, v in props.items()}


def types_assignable(produced: frozenset[str], consumed: frozenset[str]) -> bool:
    """May a value of some ``produced`` type land in a ``consumed`` slot?

    Unknown on either side passes (incremental adoption — same one-sidedness
    as ``artifacts.compatible``, one level deeper). Otherwise the sets must
    OVERLAP after int→number widening: only a provably disjoint pair — the
    `adapter_uri: str` wired into `metrics: dict` class — is rejected."""
    if not produced or not consumed:
        return True
    widened = set(produced)
    if "integer" in widened:
        widened.add("number")
    return bool(widened & set(consumed))


__all__ = [
    "ResolvedSchema",
    "SchemaResolutionError",
    "field_types",
    "resolve_schema",
    "required_fields",
    "schema_fields",
    "types_assignable",
]
