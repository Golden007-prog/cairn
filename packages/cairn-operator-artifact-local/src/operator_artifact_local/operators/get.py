"""storage/get — read an artifact from the local FS-backed artifact store.

Accepts either a ``file://`` URI (typically flowed in from a previous
``storage/put`` output) OR a (store, key) tuple. Both forms resolve to
the same on-disk path via :func:`operator_artifact_local._fs.from_uri`
or :func:`operator_artifact_local._fs.artifact_path`.

Returns the content as ``str`` (UTF-8 decoded) by default; pass
``decode=False`` to get raw bytes (Pydantic carries the bytes verbatim).

Lineage check: the SHA-256 of the read content is returned so callers
can compare against the digest recorded at write time.

Failure modes bubble via ``ok=False``:
  - missing URI + (store, key) → ``error="must supply uri or (store, key)"``
  - file not found → ``error="<path>: file not found"``
  - decode failure (``decode=True`` against binary content) → caught
    generically with ``type(e).__name__``
"""

from __future__ import annotations

from cairn.core.operator import (
    Operation,
    OperationContext,
    ReportSpec,
    register_operator,
)

from operator_artifact_local._fs import (
    artifact_path,
    from_uri,
    sha256_hex,
)
from operator_artifact_local.schemas.get import GetInput, GetOutput


@register_operator(
    name="storage/get",
    version="0.1.0",
    capabilities=["storage.get"],
    implements=["contract://storage.get/1.0.0"],  # #2243 V2 — cairn-contracts-storage
)
class GetOp(Operation[GetInput, GetOutput]):
    """Read an artifact from the local store."""

    input_schema = GetInput
    output_schema = GetOutput
    supports_mock = True
    side_effect_free = True  # #836 — read only; runs live in a sandbox run
    reports = ReportSpec.NONE  # ADR-0103: one file read, nothing to report

    def run(self, inp: GetInput, ctx: OperationContext) -> GetOutput:
        # Resolve to a concrete path. URI wins when both are provided
        # (it's the more specific identifier).
        if inp.uri:
            path = from_uri(inp.uri)
        elif inp.store and inp.key:
            path = artifact_path(inp.store, inp.key)
        else:
            return GetOutput(
                ok=False,
                error="must supply uri or (store, key)",
            )

        if not path.is_file():
            return GetOutput(
                ok=False,
                error=f"{path}: file not found",
            )

        try:
            body = path.read_bytes()
        except OSError as exc:
            return GetOutput(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        content: str | bytes
        if inp.decode:
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                return GetOutput(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
        else:
            content = body

        return GetOutput(
            ok=True,
            content=content,
            sha256=sha256_hex(body),
            size_bytes=len(body),
        )

    def mock(self, inp: GetInput, ctx: OperationContext) -> GetOutput:
        """Return a fixed mock payload — useful for offline development."""
        body = b"# mock artifact\n\nstorage/get mock mode.\n"
        return GetOutput(
            ok=True,
            content=body.decode("utf-8") if inp.decode else body,
            sha256=sha256_hex(body),
            size_bytes=len(body),
        )
