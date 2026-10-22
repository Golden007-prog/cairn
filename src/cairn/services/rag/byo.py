"""BYO corpus resolution + indexing (#516/#517, ADR-0062).

Turns a `WorkspaceCorpus` record (a governed source ref) into indexed, composable
knowledge — reusing the SAME RAG pipeline packs use. Git is the first-class
ingress: read-only, versioned, ephemeral clone (nothing tenant-persistent to
isolate beyond the workspace-scoped vector collection).

  - ``git+https://…`` / ``…​.git`` → shallow clone → index.
  - ``file://…`` → local dir (dev).

The source is caller-typed, so by default only a public ``https://`` git remote is
cloned: it must pass the connection probe's address guard
(:func:`cairn.core.connection_probe.probe_target_block_reason`), git is limited to
the https protocol and does not follow redirects, runs without this server's
system/global git config (credential helpers, extra headers, url rewrites) or a
terminal prompt, and ``--`` ends git's options before the URL. ``file://``, local paths and ssh / ``file:`` git remotes need
``allow_local=True``, which only a deployment caller passes. An option-shaped
(``-…``) or ``<transport>::`` source is never cloned.

Known limits of the address check: it resolves the host once, before git runs,
so it does not defend against a name whose DNS answer changes between the check
and the clone (DNS rebinding); and a host that does not resolve at check time
passes it (git then fails to connect, or reaches whatever the name resolves to
later). ``CAIRN_PROBE_ALLOW_PRIVATE`` relaxes the private-address part of the
check for these clones too, not only for the connection probe.
  - ``s3://`` / ``artifact://`` → resolved via the account's connection / artifact
    storage (ADR-0062; not wired here — the git slice ships first).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional
from urllib.parse import urlparse

from cairn.services.rag.embeddings import rag_collection
from cairn.services.rag.lifecycle import CorpusTarget

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 180


def is_git_source(source_uri: str) -> bool:
    s = source_uri.lower()
    return (s.startswith(("git+", "git@")) or s.endswith(".git")
            or "github.com" in s or "gitlab.com" in s or "bitbucket.org" in s)


_TRANSPORT_RE = re.compile(r"^[A-Za-z0-9+.-]+::")  # git's <transport>::<address> remote helpers


def _clone_url(url: str, *, allow_local: bool) -> str:
    """The URL git may clone, or ``ValueError``: never option-shaped or a remote
    helper; without ``allow_local`` only a public ``https://`` address."""
    clone_url = url[4:] if url.lower().startswith("git+") else url  # strip git+
    if clone_url.lstrip().startswith("-") or _TRANSPORT_RE.match(clone_url):
        raise ValueError("corpus source is not a git URL")
    if not allow_local:
        parsed = urlparse(clone_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("corpus source must be an https:// git URL")
        from cairn.core.connection_probe import probe_target_block_reason  # lazy: httpx off import path

        blocked = probe_target_block_reason(clone_url)
        if blocked:
            raise ValueError(f"corpus source is not allowed: {blocked}")
    return clone_url


def _clone(clone_url: str, ref: Optional[str], dest: str, *, allow_local: bool) -> None:
    """Shallow-clone an already-checked ``clone_url`` (see :func:`_clone_url`)."""
    env = None
    cmd = ["git"]
    if not allow_local:
        # https only; a redirect cannot walk past the address check; and none of
        # this server's own git setup (system/global config: credential helpers,
        # extra headers, url rewrites) or an interactive prompt rides along on a
        # tenant's clone.
        env = {
            **os.environ,
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        cmd += ["-c", "http.followRedirects=false", "-c", "credential.helper="]
    cmd += ["clone", "--depth", "1"]
    if ref:
        cmd += [f"--branch={ref}"]
    cmd += ["--", clone_url, dest]
    subprocess.run(cmd, check=True, capture_output=True, timeout=_GIT_TIMEOUT_S, env=env)


def resolve_corpus_source(
    source_uri: str, *, ref: Optional[str] = None, dest: Optional[str] = None, allow_local: bool = False
) -> str:
    """Resolve a governed source ref to a LOCAL directory of documents. The
    caller owns cleanup of a cloned dir (use ``index_workspace_corpus``, which
    does it). Raises for schemes not yet wired (s3/artifact = #517 follow-up).

    ``allow_local`` (deployment callers only) admits ``file://`` sources, local
    paths and ssh / ``file:`` git remotes; without it only a public ``https://``
    git URL is accepted (``ValueError`` otherwise)."""
    if source_uri.startswith("file://"):
        if not allow_local:
            raise ValueError("file:// corpus sources are not allowed for this caller")
        return source_uri[len("file://"):]
    if is_git_source(source_uri):
        clone_url = _clone_url(source_uri, allow_local=allow_local)  # refuse before making a temp dir
        if ref and ref.lstrip().startswith("-"):
            raise ValueError("corpus ref must be a branch or tag name")
        d = dest or tempfile.mkdtemp(prefix="cairn-byo-git-")
        _clone(clone_url, ref, d, allow_local=allow_local)
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


async def index_workspace_corpus(corpus, *, catalog=None, allow_local: bool = False):
    """Resolve a BYO corpus's source → run it through the RAG pipeline (parse
    → chunk → embed → upsert) into its embedder-scoped collection — the same
    ``index_corpus`` packs use. Cloned dirs are cleaned up. Returns the
    IndexResult (or a str on skip). ``allow_local`` as in
    :func:`resolve_corpus_source` (deployment callers only)."""
    from cairn.services.rag.indexer import index_corpus  # lazy: LlamaIndex ingest chain

    cloned = is_git_source(corpus.source_uri) and not corpus.source_uri.startswith("file://")
    local_dir = resolve_corpus_source(corpus.source_uri, allow_local=allow_local)
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
