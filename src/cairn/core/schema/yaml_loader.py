"""JSON Schema YAML loader (§24.20.10 + SCH2 #190).

Reads ``<pack>/schemas/*.yaml`` files declared in JSON Schema draft
2020-12 form, validates them, and registers each one into a
:class:`SchemaRegistry` under the URI form
``schema://<pack>/<SchemaName>`` derived from the schema's own ``$id``.

The §24.20.10 lock specifies the file shape exactly:

.. code-block:: yaml

    $id:     incident/RCA                          # required
    $schema: https://json-schema.org/draft/2020-12/schema
    type:    object
    required: [summary]
    properties:
      summary:
        type: string
        description: One-paragraph synthesis.

Standard JSON Schema only — no custom keywords. Anything ``jsonschema``'s
``Draft202012Validator.check_schema`` rejects, the loader rejects.

What this module deliberately does NOT do:

  - Run-time data validation. Loading produces dicts; SCH4 #192 wires
    validation at the compiler / runtime seam.
  - Inline JSON Schema in template frontmatter. §24.21 explicitly rules
    that out — schemas live in files, the template references them by
    URI. The doctor enforces this (SCH5 #193).
  - Pydantic export. That's SCH3 #191's job for operator packs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError

from cairn.core.schema.registry import (
    DuplicateSchemaError,
    SchemaRegistry,
    default_registry,
)
from cairn.core.schema.uri import (
    PREFIX,
    SchemaURI,
    SchemaURIError,
    parse_schema_uri,
)


# ─── Errors ──────────────────────────────────────────────────────────────


class SchemaYamlError(ValueError):
    """A schema YAML file is malformed, missing required fields, or
    fails JSON Schema meta-validation. Error messages always carry the
    file path so authors can jump straight to the offending line."""


# ─── Constants ───────────────────────────────────────────────────────────


DRAFT_2020_12_URI = "https://json-schema.org/draft/2020-12/schema"
"""§24.20.10 mandates draft 2020-12. Other drafts surface a clear error
rather than being silently re-interpreted."""


# ─── Single-file loader ──────────────────────────────────────────────────


def load_schema_yaml(
    path: Path,
    *,
    expected_pack: Optional[str] = None,
) -> tuple[SchemaURI, dict[str, Any]]:
    """Load + validate one JSON Schema YAML file.

    Returns the parsed URI (derived from ``$id``) and the schema dict.
    This is the unit operation; :func:`load_pack_schemas` does directory
    discovery on top.

    :param path: file to load. Must exist and be readable.
    :param expected_pack: if provided, the ``$id`` field's pack segment
        must match. Catches typos like ``$id: blast-radius/X`` inside a
        file living under ``incident/schemas/``.

    :raises SchemaYamlError: file missing, unreadable, not a YAML
        mapping, missing/malformed ``$id``, wrong ``$schema`` draft, or
        fails JSON Schema meta-validation.
    """
    if not path.exists():
        raise SchemaYamlError(f"schema YAML file not found: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # #1854 — UnicodeDecodeError is a ValueError, not an OSError, so it
        # used to escape as a raw exception: the pack loader only catches
        # SchemaYamlError, and the traceback named a byte offset but not the
        # file. Every read failure is now the loader's own error type, with the
        # path in it, so one bad file degrades ONE pack instead of killing boot.
        raise SchemaYamlError(
            f"could not read schema YAML {path}: {e}"
        ) from e

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise SchemaYamlError(
            f"YAML parse error in {path}: {e}"
        ) from e

    if data is None:
        raise SchemaYamlError(f"schema YAML is empty: {path}")
    if not isinstance(data, dict):
        raise SchemaYamlError(
            f"schema YAML {path} must contain a YAML mapping at top level "
            f"(got {type(data).__name__})"
        )

    # ── $id is mandatory and carries the canonical identity ─────────
    raw_id = data.get("$id")
    if raw_id is None:
        raise SchemaYamlError(
            f"schema YAML {path}: missing required field '$id' (§24.20.10 — "
            f"must be '<pack>/<SchemaName>')"
        )
    if not isinstance(raw_id, str):
        raise SchemaYamlError(
            f"schema YAML {path}: '$id' must be a string, got "
            f"{type(raw_id).__name__}"
        )
    # $id is in <pack>/<Name> form (no scheme); promote to a full URI for
    # the parser. This means the registry key derived from $id is exactly
    # what callers will use in templates as schema://<pack>/<Name>.
    try:
        uri = parse_schema_uri(f"{PREFIX}{raw_id}")
    except SchemaURIError as e:
        raise SchemaYamlError(
            f"schema YAML {path}: '$id' {raw_id!r} is not a valid "
            f"<pack>/<SchemaName> identifier — {e}"
        ) from e

    if expected_pack is not None and uri.pack != expected_pack:
        raise SchemaYamlError(
            f"schema YAML {path}: '$id' declares pack {uri.pack!r} but "
            f"the file lives under pack {expected_pack!r}. Move the file "
            f"or fix the $id."
        )

    # ── $schema draft ────────────────────────────────────────────────
    declared_draft = data.get("$schema")
    if declared_draft is not None and declared_draft != DRAFT_2020_12_URI:
        raise SchemaYamlError(
            f"schema YAML {path}: '$schema' must be {DRAFT_2020_12_URI!r} "
            f"(§24.20.10 — draft 2020-12 only), got {declared_draft!r}"
        )

    # ── Meta-validate against the JSON Schema spec itself ───────────
    # check_schema raises jsonschema.SchemaError on any structural issue:
    # unknown keywords (when in strict mode — Draft202012Validator validates
    # against the meta-schema, which doesn't include custom keywords),
    # wrong-typed values, etc.
    try:
        Draft202012Validator.check_schema(data)
    except JsonSchemaError as e:
        raise SchemaYamlError(
            f"schema YAML {path}: JSON Schema meta-validation failed — {e.message}"
        ) from e

    return uri, data


# ─── Pack-level discovery + bulk register ────────────────────────────────


def discover_schema_files(schemas_dir: Path) -> list[Path]:
    """Return every ``*.yaml`` / ``*.yml`` file under ``schemas_dir``,
    sorted for deterministic iteration. Empty list when the dir
    doesn't exist — domain packs that ship no schemas are valid."""
    if not schemas_dir.exists():
        return []
    if not schemas_dir.is_dir():
        raise SchemaYamlError(
            f"expected a schemas directory, got non-directory: {schemas_dir}"
        )
    files: list[Path] = []
    for ext in ("*.yaml", "*.yml"):
        # #1854 — skip dotfiles. They are never pack content, and one of them
        # took prod down: macOS `tar` without COPYFILE_DISABLE=1 emits a binary
        # AppleDouble sibling (`._Foo.schema.yaml`) for every file with xattrs;
        # 2,449 of them matched this glob and the first one crash-looped the
        # server on UnicodeDecodeError. Same for .DS_Store and editor swap files.
        files.extend(f for f in schemas_dir.glob(ext) if not f.name.startswith("."))
    return sorted(files)


def load_pack_schemas(
    pack_root: Path,
    *,
    pack_name: Optional[str] = None,
    schemas_subdir: str = "schemas",
    registry: Optional[SchemaRegistry] = None,
) -> list[SchemaURI]:
    """Load every JSON Schema YAML under ``<pack_root>/<schemas_subdir>/``
    and register each into ``registry`` (default: process-wide).

    :param pack_root: the pack's filesystem root (the directory holding
        ``pack.toml``).
    :param pack_name: optional — if provided, every loaded schema's
        ``$id`` pack segment must match. The pack loader has this
        already from the manifest, so it threads it through.
    :param schemas_subdir: defaults to ``schemas`` (§24.20.21). Some
        legacy domain packs may use ``code/schemas`` for Pydantic models;
        the JSON Schema YAML loader looks at ``schemas`` by convention.
    :param registry: target registry; defaults to
        :data:`default_registry`.

    :returns: list of registered URIs in the order they were loaded.

    :raises SchemaYamlError: any single file fails to load. The loader
        is all-or-nothing: a partial registration on failure would
        leave the registry in a half-loaded state.
    :raises DuplicateSchemaError: two different schemas claim the same
        ``$id``.
    """
    schemas_dir = pack_root / schemas_subdir
    files = discover_schema_files(schemas_dir)
    if not files:
        return []

    target = registry if registry is not None else default_registry

    # Two-phase: load all first (so a failure leaves the registry
    # untouched), then register in a second pass.
    loaded: list[tuple[SchemaURI, dict[str, Any]]] = []
    for f in files:
        uri, schema = load_schema_yaml(f, expected_pack=pack_name)
        loaded.append((uri, schema))

    registered: list[SchemaURI] = []
    for uri, schema in loaded:
        try:
            target.register(uri.uri, schema)
        except DuplicateSchemaError:
            # Re-raise with file path context — the registry doesn't
            # know which file the conflict came from.
            raise DuplicateSchemaError(
                f"schema {uri.uri!r} is registered with a different "
                f"artifact (loading {schemas_dir})"
            )
        registered.append(uri)

    return registered


__all__ = [
    "DRAFT_2020_12_URI",
    "SchemaYamlError",
    "load_schema_yaml",
    "discover_schema_files",
    "load_pack_schemas",
]
