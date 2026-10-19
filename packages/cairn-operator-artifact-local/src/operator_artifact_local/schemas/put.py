"""Pydantic schemas for storage/put."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PutInput(BaseModel):
    """Inputs for writing an artifact.

    ``store`` is the named artifact kind from a pack's
    ``[requires.artifacts.<name>]`` declaration. ``content`` is the
    payload to write — str is encoded as UTF-8, bytes are written verbatim.

    ``key`` is the addressable identifier within the store. If omitted,
    the operator derives one from the SHA-256 of ``content`` (giving
    content-addressed storage for callers that don't care about names).

    ``content_type`` is recorded for downstream consumers but doesn't
    affect on-disk format — local FS has no MIME registry.
    """

    store: str = Field(
        ...,
        description="Named store from [requires.artifacts.<name>].",
    )
    content: str | bytes = Field(
        ...,
        description="Payload to write. str → UTF-8; bytes verbatim.",
    )
    key: str = Field(
        "",
        description="Optional addressable key; derived from content sha256 if omitted.",
    )
    content_type: str = Field(
        "",
        description="MIME type recorded with the artifact (free-form).",
    )


class PutOutput(BaseModel):
    """Result of an artifact write.

    ``size_bytes`` (not ``bytes``) for consistency with
    :class:`GetOutput`, which can't use ``bytes`` because the field
    name shadows the builtin used in ``content: str | bytes``.
    """

    ok: bool
    uri: str = ""
    key: str = ""
    sha256: str = ""
    size_bytes: int = 0
    error: str = ""
