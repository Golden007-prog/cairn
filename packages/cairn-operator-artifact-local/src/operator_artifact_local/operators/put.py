"""storage/put — write an artifact to the local FS-backed artifact store.

Per-store subdirs under ``CAIRN_ARTIFACTS_DIR`` (default
``~/.cairn/artifacts``). Returns a ``file://`` URI suitable for
flowing through template state to ``storage/get`` callers.

Key derivation: if the caller omits ``key``, we hash the content and
use the SHA-256 hex digest as the key. This makes the operator
content-addressed by default — repeated writes of the same content
land at the same URI (idempotent).

Failure modes bubble via ``ok=False``:
  - filesystem I/O errors (permission denied, disk full, etc.) →
    caught generically with ``type(e).__name__``
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
    sha256_hex,
    to_uri,
)
from operator_artifact_local.schemas.put import PutInput, PutOutput


@register_operator(
    name="storage/put",
    version="0.1.0",
    capabilities=["storage.put"],
    implements=["contract://storage.put/1.0.0"],  # #2243 V2 — cairn-contracts-storage
)
class PutOp(Operation[PutInput, PutOutput]):
    """Write ``content`` to (store, key) on the local artifact FS."""

    input_schema = PutInput
    output_schema = PutOutput
    # #1539 — governance declarations; the [operator.*] table is generated from these
    risk = 'external_write'
    supports_mock = True
    sandbox_safe = True  # #844 — cheap scoped write; live to the scratch store in the playground env
    reports = ReportSpec.NONE  # ADR-0103: one file write, nothing to report

    def run(self, inp: PutInput, ctx: OperationContext) -> PutOutput:
        body = (
            inp.content.encode("utf-8")
            if isinstance(inp.content, str)
            else inp.content
        )
        digest = sha256_hex(body)
        key = inp.key or digest

        path = artifact_path(inp.store, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        except OSError as exc:
            return PutOutput(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        return PutOutput(
            ok=True,
            uri=to_uri(path),
            key=key,
            sha256=digest,
            size_bytes=len(body),
        )

    def mock(self, inp: PutInput, ctx: OperationContext) -> PutOutput:
        """Return a derived URI + digest without touching the filesystem.

        Useful for template authors developing offline — the URI shape
        matches the real one so downstream `storage/get` calls in mock
        mode see consistent inputs.
        """
        body = (
            inp.content.encode("utf-8")
            if isinstance(inp.content, str)
            else inp.content
        )
        digest = sha256_hex(body)
        key = inp.key or digest
        # Mock URI doesn't resolve to a real path; the prefix signals it.
        return PutOutput(
            ok=True,
            uri=f"file:///mock/artifacts/{inp.store}/{key}",
            key=key,
            sha256=digest,
            size_bytes=len(body),
        )
