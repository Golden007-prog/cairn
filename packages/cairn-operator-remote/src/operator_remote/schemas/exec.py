"""Schemas for remote/exec — run a command on an SSH box, ship files in/out."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RemoteExecInput(BaseModel):
    """Inputs for a remote command execution over SSH.

    The compute target (host/user/key) comes from a ``compute.ssh`` connection via
    ``ctx.compute`` (bind ``execution.compute_target``), never from here.
    """

    command: str = Field(..., description="Shell command to run on the remote host (the workflow author's; run from a script file, not interpolated).")
    workdir: str = Field("", description="Remote working dir; default a fresh ~/.cairn-jobs/<id>.")
    upload: list[str] = Field(default_factory=list, description="Local file paths to SFTP into the workdir before running.")
    fetch: list[str] = Field(default_factory=list, description="Remote paths/globs (relative to workdir) to SFTP back on success.")
    # Any scalar — the runner str()s every value when exporting, so a binding can
    # map a numeric $input (e.g. max_steps) straight in without quoting.
    env: dict[str, Any] = Field(default_factory=dict, description="Env vars exported before the command.")


class RemoteExecOutput(BaseModel):
    """Result of a remote command execution."""

    status: str = "succeeded"
    exit_code: int = 0
    host: str = ""
    workdir: str = ""
    fetched: list[str] = Field(default_factory=list, description="Local paths of fetched outputs.")
    metrics: dict[str, Any] = Field(default_factory=dict, description="Parsed workdir/metrics.json, if the command wrote one.")
    error: str = ""
