"""`cairn compute status` with no connection names where the names come from (#2536)."""
from __future__ import annotations

from typer.testing import CliRunner

from cairn.cli.commands import compute


def test_missing_connection_hints_at_connect_ls() -> None:
    res = CliRunner().invoke(compute.app, ["status"])
    assert res.exit_code == 2  # the usage-error code a missing argument always had
    assert "cairn connect ls" in res.output
