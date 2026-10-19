"""#1782 — the venv overlay stager, the one piece of the two-layer split that can
be WRONG in a way the image still boots.

The final image assembles its venv from a large stable layer (uv.lock only) and a
small overlay (cairn source). Before the split, every deploy re-exported the whole
~3.5GB venv to ship ~7MB of changed Python — 409s of a ~13 min deploy, measured on
the prod box 2026-08-31.

These build a miniature venv on disk rather than mocking the filesystem: the thing
under test is "did the right BYTES land", which a mock cannot answer.
"""
from __future__ import annotations

import json
import os

import pytest

from deploy.stage_venv_overlay import record_path, site_packages, stage


def _venv(tmp_path, dists: dict[str, list[str]], *, bin_files=()):
    """A venv holding `dists` = {name-version: [relative files]}."""
    venv = tmp_path / "venv"
    sp = venv / "lib" / "python3.13" / "site-packages"
    sp.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    for spec, files in dists.items():
        name, version = spec.rsplit("-", 1)
        di = sp / f"{name}-{version}.dist-info"
        di.mkdir()
        rows = []
        for rel in files:
            f = sp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"# {rel}\n")
            rows.append(f"{rel},sha256=x,{len(rel)}")
        (di / "RECORD").write_text("\n".join(rows) + "\n")
    for b in bin_files:
        p = venv / "bin" / b
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    return venv


def test_only_the_ADDED_distributions_files_are_staged(tmp_path):
    venv = _venv(tmp_path, {
        "torch-2.13.0": ["torch/__init__.py", "torch/_C.py"],       # in deps
        "cairn-0.1.0": ["cairn/__init__.py", "cairn/core/run.py"],  # added
    }, bin_files=["python", "cairn-server"])
    out = tmp_path / "overlay"
    n, copied, _ = stage(str(venv), str(out),
                         {"torch": "2.13.0"},
                         {"torch": "2.13.0", "cairn": "0.1.0"},
                         ["python"])
    assert n == 1
    staged = {os.path.relpath(os.path.join(r, f), out)
              for r, _d, fs in os.walk(out) for f in fs}
    assert "lib/python3.13/site-packages/cairn/__init__.py" in staged
    assert "lib/python3.13/site-packages/cairn/core/run.py" in staged
    # the 3GB half must NOT be duplicated into the small layer — the whole point
    assert not any("torch" in s for s in staged)


def test_a_new_console_script_rides_along(tmp_path):
    """cairn-server is written into .venv/bin by the sync and appears in no
    RECORD under site-packages — a missing one is a container that cannot start."""
    venv = _venv(tmp_path, {"cairn-0.1.0": ["cairn/__init__.py"]},
                 bin_files=["python", "cairn-server"])
    out = tmp_path / "overlay"
    stage(str(venv), str(out), {}, {"cairn": "0.1.0"}, ["python"])
    assert (out / "bin" / "cairn-server").exists()
    assert not (out / "bin" / "python").exists()  # already in the stable layer


def test_a_version_CHANGED_distribution_is_REFUSED(tmp_path):
    """Docker layers add and replace but never REMOVE, so laying v2 over v1
    leaves v1's orphans and the image silently runs a mixed install. This is the
    tokenizers shape that broke the #1806 build; here it must be a red build."""
    venv = _venv(tmp_path, {"tokenizers-0.23.1": ["tokenizers/__init__.py"]})
    with pytest.raises(SystemExit) as e:
        stage(str(venv), str(tmp_path / "o"),
              {"tokenizers": "0.22.2"}, {"tokenizers": "0.23.1"}, [])
    msg = str(e.value)
    assert "REFUSING to split" in msg
    assert "tokenizers: 0.22.2 -> 0.23.1" in msg


def test_a_distribution_with_no_RECORD_is_a_hard_failure(tmp_path):
    """Silently staging nothing for it would ship a venv missing that package."""
    venv = _venv(tmp_path, {})
    with pytest.raises(SystemExit, match="no RECORD"):
        stage(str(venv), str(tmp_path / "o"), {}, {"ghost": "1.0"}, [])


def test_RECORD_entries_absent_on_disk_are_counted_not_fatal(tmp_path):
    """RECORD lists entries an install may not have written (e.g. pruned .pyc);
    those are reported, never silently ignored and never fatal."""
    venv = _venv(tmp_path, {"cairn-0.1.0": ["cairn/__init__.py"]})
    sp = site_packages(str(venv))
    rec = record_path(sp, "cairn")
    with open(rec, "a", encoding="utf-8") as fh:
        fh.write("cairn/never_written.py,sha256=x,10\n")
    _n, copied, absent = stage(str(venv), str(tmp_path / "o"), {}, {"cairn": "0.1.0"}, [])
    assert absent == 1 and copied == 1


def test_dist_info_name_normalization(tmp_path):
    """`uv pip list` reports cairn-client; the dist-info dir is cairn_client-…"""
    venv = _venv(tmp_path, {"cairn_client-0.1.0": ["cairn_client/__init__.py"]})
    assert record_path(site_packages(str(venv)), "cairn-client") is not None
