"""Pack-local test hermeticity.

A developer shell (or CI secret) may carry live ``OBS_ARTIFACT_S3_*`` config —
with #1465 uniform capture that would make every runner test silently upload
its workdir to a REAL bucket and pollute frozen-stats assertions (found live
2026-08-20: the checkpoint-manifest test failed only on machines with a bucket
configured). Capture tests that want the feature set the env explicitly.
"""
from __future__ import annotations

import pytest

_CAPTURE_ENV = (
    "OBS_ARTIFACT_S3_BUCKET", "OBS_ARTIFACT_S3_PREFIX",
    "OBS_ARTIFACT_S3_REGION", "OBS_ARTIFACT_S3_ENDPOINT", "CAIRN_CAPTURE",
)


@pytest.fixture(autouse=True)
def _no_ambient_artifact_store(monkeypatch):
    for var in _CAPTURE_ENV:
        monkeypatch.delenv(var, raising=False)


# ADR-0134 D2: this is an optional wheel. Where it is not installed (a bare `pytest` on an
# install without `cairndev[skypilot]`), its tests are not collected — said once in the
# header, not hidden. The public CI installs the extra and runs them (ADR-0134 D1).
import importlib.util  # noqa: E402

_INSTALLED = importlib.util.find_spec("operator_dataflow_skypilot") is not None
collect_ignore_glob = [] if _INSTALLED else ["test_*.py"]


def pytest_report_header(config):
    if not _INSTALLED:
        return ("cairn-operator-dataflow-skypilot is not installed: its tests are not collected "
                "(pip install 'cairndev[skypilot]')")
    return None
