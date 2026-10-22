"""A BYO corpus source reaches only a public ``https`` git remote by default.

``resolve_corpus_source`` / ``index_workspace_corpus`` take a source a caller
typed. By default only an ``https://`` git URL that passes the connection
probe's address guard is cloned, with ``--`` before the URL; ``file://``, ssh,
local paths and ``file:`` git remotes need ``allow_local=True`` (the
deployment), and an option-shaped (``-…``) or ``<transport>::`` source is never
cloned. Hermetic: a stub ``git`` on PATH records its argv; names resolve
through a stubbed resolver; no network.
"""
from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path

import pytest

from cairn.core import connection_probe
from cairn.core.storage.knowledge_corpora import WorkspaceCorpus
from cairn.services.rag.byo import index_workspace_corpus, resolve_corpus_source

_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$GIT_STUB_LOG"
printf '%s' "$GIT_ALLOW_PROTOCOL" > "$GIT_STUB_LOG.protocols"
for last; do :; done
mkdir -p "$last"
"""


@pytest.fixture
def git_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(_STUB)
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "git.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GIT_STUB_LOG", str(log))
    monkeypatch.delenv("CAIRN_PROBE_ALLOW_PRIVATE", raising=False)
    # every name resolves to a public address unless a test says otherwise
    monkeypatch.setattr(connection_probe, "_resolved_ips",
                        lambda host, port: ([ipaddress.ip_address("93.184.215.14")], None))
    return log


def test_a_public_https_git_url_is_cloned_after_a_double_dash(git_stub, tmp_path):
    dest = resolve_corpus_source("git+https://github.com/acme/docs.git", ref="main", dest=str(tmp_path / "d"))
    argv = git_stub.read_text().splitlines()
    assert argv[-3:] == ["--", "https://github.com/acme/docs.git", str(tmp_path / "d")]
    assert "--branch=main" in argv and "clone" in argv
    assert Path(git_stub.with_suffix(".log.protocols")).read_text() == "https"
    assert dest == str(tmp_path / "d")


@pytest.mark.parametrize("source", [
    "-oProxyCommand=sh.git",                     # option-shaped
    "--upload-pack=touch x.git",
    "ext::sh -c touch% x.git",                   # remote-helper transport
    "git+file:///srv/repos/docs.git",            # local git remote
    "file:///etc/docs.git",
    "/srv/repos/docs.git",                       # local path
    "git@github.com:acme/docs.git",              # ssh (scp form)
    "ssh://git@github.com/acme/docs.git",
    "http://github.com/acme/docs.git",           # plain http
    "https://169.254.169.254/latest.git",        # metadata
    "https://127.0.0.1/acme/docs.git",           # loopback
    "https://10.0.0.7/acme/docs.git",            # private
])
def test_a_non_public_or_non_https_git_source_is_refused(git_stub, source):
    with pytest.raises(ValueError):
        resolve_corpus_source(source)
    assert not git_stub.exists()  # git never ran


def test_a_name_resolving_to_a_private_address_is_refused(git_stub, monkeypatch):
    monkeypatch.setattr(connection_probe, "_resolved_ips",
                        lambda host, port: ([ipaddress.ip_address("10.0.0.7")], None))
    with pytest.raises(ValueError):
        resolve_corpus_source("https://git.internal.example/acme/docs.git")
    assert not git_stub.exists()


def test_a_file_source_is_refused_by_default(tmp_path):
    with pytest.raises(ValueError):
        resolve_corpus_source(f"file://{tmp_path}")


def test_the_deployment_may_use_local_sources(git_stub, tmp_path):
    assert resolve_corpus_source(f"file://{tmp_path}", allow_local=True) == str(tmp_path)
    resolve_corpus_source("git@github.com:acme/docs.git", dest=str(tmp_path / "d"), allow_local=True)
    argv = git_stub.read_text().splitlines()
    assert argv[-3:] == ["--", "git@github.com:acme/docs.git", str(tmp_path / "d")]


@pytest.mark.parametrize("source", ["-oProxyCommand=sh.git", "ext::sh -c touch% x.git"])
def test_option_and_transport_sources_are_refused_even_for_the_deployment(git_stub, source):
    with pytest.raises(ValueError):
        resolve_corpus_source(source, allow_local=True)
    assert not git_stub.exists()


@pytest.mark.asyncio
async def test_index_workspace_corpus_refuses_a_server_path_by_default(tmp_path, monkeypatch):
    calls: list[str] = []

    async def _index(corpus_id, root, **kw):
        calls.append(root)
        return "indexed"

    monkeypatch.setattr("cairn.services.rag.indexer.index_corpus", _index)
    local = tmp_path / "docs.git"  # a local dir whose name looks like a git source
    local.mkdir()
    corpus = WorkspaceCorpus(workspace_id="w", name="docs", source_uri=f"file://{local}")
    with pytest.raises(ValueError):
        await index_workspace_corpus(corpus)
    assert calls == []
    assert await index_workspace_corpus(corpus, allow_local=True) == "indexed"
    assert calls == [str(local)]
    assert local.is_dir()  # a local source is never cleaned up like a clone


def test_a_tenant_clone_runs_without_the_servers_git_setup(git_stub, tmp_path, monkeypatch):
    """No system/global git config (credential helpers, extra headers, url
    rewrites), no credential helper, no terminal prompt, no redirects."""
    import subprocess

    from cairn.services.rag import byo

    calls: list[tuple[list[str], dict]] = []

    def _run(cmd, **kw):
        calls.append((cmd, kw.get("env")))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(byo.subprocess, "run", _run)
    resolve_corpus_source("https://github.com/acme/docs.git", dest=str(tmp_path / "d"))
    (cmd, env), = calls
    assert cmd[:5] == ["git", "-c", "http.followRedirects=false", "-c", "credential.helper="]
    assert cmd.index("credential.helper=") < cmd.index("clone")
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_deployment_clone_keeps_the_servers_git_setup(git_stub, tmp_path, monkeypatch):
    import subprocess

    from cairn.services.rag import byo

    calls: list[tuple[list[str], object]] = []
    monkeypatch.setattr(byo.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw.get("env"))) or subprocess.CompletedProcess(cmd, 0))
    resolve_corpus_source("git@github.com:acme/docs.git", dest=str(tmp_path / "d"), allow_local=True)
    (cmd, env), = calls
    assert env is None and cmd[:2] == ["git", "clone"]
