"""BYO corpus resolution + indexing (#516/#517, ADR-0062).

Turns a `WorkspaceCorpus` record (a governed source ref) into indexed, composable
knowledge — reusing the SAME RAG pipeline packs use. Git is the first-class
ingress: read-only, versioned, ephemeral clone (nothing tenant-persistent to
isolate beyond the workspace-scoped vector collection).

  - ``git+https://…`` / ``…​.git`` → shallow clone → index.
  - ``file://…`` → local dir (dev).
  - ``s3://`` / ``artifact://`` → resolved via the account's connection / artifact
    storage (ADR-0062; not wired here — the git slice ships first).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from cairn.services.rag.embeddings import rag_collection
from cairn.services.rag.lifecycle import CorpusTarget

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 180


def is_git_source(source_uri: str) -> bool:
    s = source_uri.lower()
    return (s.startswith(("git+", "git@")) or s.endswith(".git")
            or "github.com" in s or "gitlab.com" in s or "bitbucket.org" in s)


def _clone(url: str, ref: Optional[str], dest: str) -> None:
    clone_url = url[4:] if url.lower().startswith("git+") else url  # strip git+
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [clone_url, dest]
    subprocess.run(cmd, check=True, capture_output=True, timeout=_GIT_TIMEOUT_S)


def resolve_corpus_source(source_uri: str, *, ref: Optional[str] = None, dest: Optional[str] = None) -> str:
    """Resolve a governed source ref to a LOCAL directory of documents. The
    caller owns cleanup of a cloned dir (use ``index_workspace_corpus``, which
    does it). Raises for schemes not yet wired (s3/artifact = #517 follow-up)."""
    if source_uri.startswith("file://"):
        return source_uri[len("file://"):]
    if is_git_source(source_uri):
        d = dest or tempfile.mkdtemp(prefix="cairn-byo-git-")
        _clone(source_uri, ref, d)
        return d
    if source_uri.startswith(("s3://", "artifact://")):
        raise NotImplementedError(
            f"{source_uri.split(':')[0]}:// ingress is specified in ADR-0062 but not "
            "wired yet (#517); git+https:// and file:// work today."
        )
    raise ValueError(f"unrecognized corpus source: {source_uri!r}")


def workspace_corpus_targets(corpora) -> list[CorpusTarget]:
    """Map BYO records → ``CorpusTarget``s for COMPOSITION (list_roles). ``root``
    carries the source ref, unresolved — resolution happens only at index time,
    so listing/composing roles never clones anything."""
    out: list[CorpusTarget] = []
    for c in corpora:
        if not c.name or not c.role:
            continue
        out.append(CorpusTarget(
            pack=f"workspace:{c.workspace_id}",
            name=c.name,
            corpus_id=f"ws:{c.workspace_id}/{c.name}",
            root=c.source_uri,
            collection=rag_collection(c.name),
            freshness=None,
            role=c.role,
        ))
    return out


def workspace_corpus_targets_sync(workspace=None) -> list[CorpusTarget]:
    """BYO targets for the (sync) role-composition path — reads the store
    synchronously; empty on any hiccup so composition never breaks."""
    try:
        from cairn.core.storage.knowledge_corpora import get_workspace_corpus_store

        rows = get_workspace_corpus_store().list_sync(workspace)
    except Exception:  # noqa: BLE001
        return []
    return workspace_corpus_targets(rows)


async def index_workspace_corpus(corpus, *, catalog=None):
    """Resolve a BYO corpus's source → run it through the RAG pipeline (parse
    → chunk → embed → upsert) into its embedder-scoped collection — the same
    ``index_corpus`` packs use. Cloned dirs are cleaned up. Returns the
    IndexResult (or a str on skip)."""
    from cairn.services.rag.indexer import index_corpus  # lazy: LlamaIndex ingest chain

    cloned = is_git_source(corpus.source_uri)
    local_dir = resolve_corpus_source(corpus.source_uri)
    corpus_id = f"ws:{corpus.workspace_id}/{corpus.name}"
    collection = rag_collection(corpus.name)
    try:
        logger.info("[byo] indexing %s from %s → %s", corpus_id, corpus.source_uri, collection)
        return await index_corpus(corpus_id, local_dir, catalog=catalog, collection=collection)
    finally:
        if cloned and os.path.isdir(local_dir):
            shutil.rmtree(local_dir, ignore_errors=True)


__all__ = [
    "is_git_source", "resolve_corpus_source",
    "workspace_corpus_targets", "index_workspace_corpus",
]
