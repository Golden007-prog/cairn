"""Uniform run capture (#1465) — auto-persist every stage's workdir products.

MLflow's model is "everything a run produced is the run's artifacts"; cairn's
was "what the author remembered to export" — and twice in one campaign the
next round's training input survived only in a container's ``/tmp``
(2026-08-20, the closedbook answers files). This module closes that gap at the
ONE chokepoint every flow kind and every engine passes through: the runner's
stage loop. After a stage executes, the files it created or changed are
mirrored to the artifact store and described in the stage's stats, so the
descriptors ride the existing stats path into the run's ``result_payloads`` —
no schema change, no cooperation from stage authors.

Contract (mirrors ``checkpoint_sync``'s discipline):
- **No bucket, no capture**: without ``CAIRN_ARTIFACT_S3_BUCKET`` every helper
  no-ops — local-only deployments are unchanged.
- **Best-effort**: capture failure logs and returns; it must never fail the
  stage it is evidence for.
- **Bounded**: per-file size cap (``CAIRN_CAPTURE_MAX_BYTES``, default 64MB)
  and per-stage file-count cap (``CAIRN_CAPTURE_MAX_FILES``, default 200) —
  silently skipped files are COUNTED in the stats (no silent truncation).
- **Opt-out**: ``CAIRN_CAPTURE=off`` disables globally.

The runner is stdlib-only (D5), so boto3 is imported lazily inside the upload
path — the same convention as the export kernels' s3/put contract, and the
same ``CAIRN_ARTIFACT_S3_*`` env resolves bucket/prefix/region/endpoint.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import hashlib
import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("cairn.dataflow.capture")

#: Runner-internal files that are bookkeeping, not stage products.
_INTERNAL_PREFIXES = ("stage-", "map-", "checkpoint-")
_INTERNAL_NAMES = {"metrics.json", "result.json", "in.json", "spec.json"}

_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_FILES = 200


def enabled() -> bool:
    """Capture is ON whenever an artifact bucket is configured; ``CAIRN_CAPTURE=off``
    (or ``0``/``false``) turns it off without touching the bucket config."""
    if os.environ.get("CAIRN_CAPTURE", "").strip().lower() in ("off", "0", "false", "no"):
        return False
    return bool(getenv("CAIRN_ARTIFACT_S3_BUCKET", "").strip())


def _is_internal(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if name in _INTERNAL_NAMES or name.startswith("."):
        return True
    # top-level runner bookkeeping (stage-00-x.done, map-01/, checkpoint-…);
    # nested map item WORKDIRS are products and stay capturable, but their own
    # bookkeeping files are filtered by name above.
    return any(parts[0].startswith(p) for p in _INTERNAL_PREFIXES) and rel.endswith((".done", ".json"))


def snapshot_workdir(workdir: Path) -> dict[str, tuple[float, int]]:
    """``{relpath: (mtime, size)}`` for every file under ``workdir`` — the
    "before" picture a stage's diff is computed against. Cheap (stat only)."""
    if not enabled():
        return {}
    snap: dict[str, tuple[float, int]] = {}
    try:
        for p in workdir.rglob("*"):
            if p.is_file():
                st = p.stat()
                snap[str(p.relative_to(workdir))] = (st.st_mtime, st.st_size)
    except OSError:
        _log.warning("capture: snapshot of %s failed", workdir, exc_info=True)
    return snap


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def s3_client():
    """s3 client via the same env contract as the export kernels. Lazy boto3 —
    the runner import path stays stdlib-only (D5).

    Public since #1395: the artifact-bytes relay
    (:mod:`cairn.core.storage.artifact_bytes`) resolves bucket/prefix/region/
    endpoint from exactly these vars, and a SECOND client wrapper is how two
    surfaces end up disagreeing about which endpoint MinIO is on."""
    import boto3  # noqa: PLC0415 - optional dependency: only paid when a bucket is configured

    return boto3.client(
        "s3",
        region_name=(getenv("CAIRN_ARTIFACT_S3_REGION")
                     or os.environ.get("AWS_DEFAULT_REGION") or None),
        endpoint_url=getenv("CAIRN_ARTIFACT_S3_ENDPOINT") or None,
    )


def split_s3_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/key/…`` → ``(bucket, key)``. Public alongside
    :func:`s3_client` (#1395)."""
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _upload(src: Path, uri: str) -> None:
    bucket, key = split_s3_uri(uri)
    s3_client().upload_file(str(src), bucket, key)


def _upload_bytes(data: bytes, uri: str) -> None:
    bucket, key = split_s3_uri(uri)
    s3_client().put_object(Bucket=bucket, Key=key, Body=data)


def _capture_root(workdir: Path, index: int, stage: str) -> str:
    bucket = getenv("CAIRN_ARTIFACT_S3_BUCKET", "").strip()
    prefix = getenv("CAIRN_ARTIFACT_S3_PREFIX", "").strip("/")
    base = f"s3://{bucket}" + (f"/{prefix}" if prefix else "")
    # Same identity the export kernels use (`dataflow/<workdir-id>/…`) so a
    # run's captures and its exports live side by side under one key.
    return f"{base}/dataflow/{workdir.name}/capture/{index:02d}-{stage}"


def capture_stage(
    workdir: Path, before: dict[str, tuple[float, int]], index: int, stage: str,
) -> dict | None:
    """Mirror the files ``stage`` created or changed to the artifact store.

    Returns FLAT SCALARS to merge into the stage's stats — D3 (ADR-0081) bans
    rich values at the dataflow boundary, so the full descriptor list
    (``[{name, uri, bytes, content_hash}…]``, the #1442 export shape) is
    itself uploaded as ``captured.json`` and referenced by URI::

        {"artifacts_count": n, "artifacts_bytes": total,
         "artifacts_index": "s3://…/captured.json"[, "artifacts_skipped": k]}

    ``None`` when capture is off or the stage touched nothing.
    """
    if not enabled():
        return None
    try:
        max_bytes = int(os.environ.get("CAIRN_CAPTURE_MAX_BYTES", _DEFAULT_MAX_BYTES))
        max_files = int(os.environ.get("CAIRN_CAPTURE_MAX_FILES", _DEFAULT_MAX_FILES))
        root = _capture_root(workdir, index, stage)
        captured: list[dict] = []
        skipped = 0
        for p in sorted(workdir.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(workdir))
            if _is_internal(rel):
                continue
            st = p.stat()
            if before.get(rel) == (st.st_mtime, st.st_size):
                continue  # untouched by this stage
            if st.st_size > max_bytes or len(captured) >= max_files:
                skipped += 1
                continue
            uri = f"{root}/{rel}"
            _upload(p, uri)
            captured.append({"name": rel, "uri": uri, "bytes": st.st_size,
                             "content_hash": _sha256(p)})
        if not captured and not skipped:
            return None
        index_uri = f"{root}/captured.json"
        _upload_bytes(json.dumps({"stage": stage, "index": index,
                                  "artifacts": captured}, indent=2).encode("utf-8"), index_uri)
        out: dict = {"artifacts_count": len(captured),
                     "artifacts_bytes": sum(a["bytes"] for a in captured),
                     "artifacts_index": index_uri}
        if skipped:
            out["artifacts_skipped"] = skipped  # counted, never silent (#1465)
        return out
    except Exception:  # noqa: BLE001 — evidence capture must never fail the stage
        _log.warning("capture: stage %s failed", stage, exc_info=True)
        return None
