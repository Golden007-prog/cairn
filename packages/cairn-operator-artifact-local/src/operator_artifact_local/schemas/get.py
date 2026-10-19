"""Pydantic schemas for storage/get."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GetInput(BaseModel):
    """Inputs for reading an artifact.

    Callers can address by URI (``file://...`` as returned by
    :class:`PutOutput`) OR by (store, key) — both forms resolve to the
    same path. URI form is preferred when the artifact id has been
    flowing through state; (store, key) is more readable in static
    operation bindings.

    ``decode`` controls whether the operator returns ``str`` (UTF-8
    decode) or ``bytes`` — defaults to True since most artifacts are
    markdown / JSON / text.
    """

    uri: str = Field(
        "",
        description="file:// URI (as returned by storage.put).",
    )
    store: str = Field(
        "",
        description="Named store — required if uri is empty.",
    )
    key: str = Field(
        "",
        description="Artifact key within store — required if uri is empty.",
    )
    decode: bool = Field(
        True,
        description="True → return str (UTF-8); False → return bytes (as base64-able dict).",
    )


class GetOutput(BaseModel):
    """Result of an artifact read.

    ``size_bytes`` is named to avoid shadowing the builtin ``bytes``
    type — Pydantic v2 resolves field annotations in the class body
    namespace, so a field literally named ``bytes`` breaks the
    ``content: str | bytes`` annotation.
    """

    ok: bool
    content: str | bytes = ""
    sha256: str = ""
    size_bytes: int = 0
    error: str = ""
