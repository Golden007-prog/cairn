"""The job-channel door (#2025, ADR-0099 §1 — derive, refuse, never auto-start).

When the process job channel is NATS (``CAIRN_JOB_CHANNEL=nats://…``), every leg
that hands a job to a remote box (an operator implementing ``submit``) needs
that NATS reachable at launch: the worker will publish its live events there
and nothing else can receive them. ADR-0099 says a requirement that is already
implied by configuration is **derived**, not declared, and is enforced at the
launch door **before any record exists**, with the remedy named — and that the
control plane refuses rather than starts the service.

So this module answers one question — *does this launch need NATS, and is it
there?* — for the preflight (dry run) and the dispatcher (the real door) from
the same code, the way the licence and intake doors already do.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import os
import socket
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit

JOB_CHANNEL_ENV = "CAIRN_JOB_CHANNEL"
SERVICE = "nats"
COMPOSE_PROFILE = "nats"
_DEFAULT_PORT = 4222


@dataclass
class ServiceVerdict:
    """``ok`` unless a required service is missing; ``reasons`` name each one."""

    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)


def job_channel_url() -> str:
    return getenv(JOB_CHANNEL_ENV, "").strip()


def channel_is_nats(url: Optional[str] = None) -> bool:
    return (url if url is not None else job_channel_url()).startswith("nats://")


def is_remote_job_operator(op_cls: object) -> bool:
    """A remote-job operator hands work to another box: it implements ``submit``."""
    return callable(getattr(op_cls, "submit", None))


def remote_job_operators(names: Iterable[str]) -> list[str]:
    """The subset of ``names`` that resolve, here, to remote-job operators.
    An uninstalled operator is not a remote job — unknown never means required."""
    from cairn.core.operator.registry import default_registry  # noqa: PLC0415 — cycle: door ↔ operator registry at import time

    out: list[str] = []
    for name in names:
        try:
            if default_registry.has(name) and is_remote_job_operator(default_registry.get(name)):
                out.append(name)
        except Exception:  # noqa: BLE001 — an unresolvable operator is unchecked, never a launch failure
            continue
    return out


def nats_endpoints(url: str) -> list[tuple[str, int]]:
    """``nats://a:4222,nats://b`` → ``[("a", 4222), ("b", 4222)]``."""
    out: list[tuple[str, int]] = []
    for part in (url or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "://" not in part:
            part = "nats://" + part
        u = urlsplit(part)
        if u.hostname:
            out.append((u.hostname, u.port or _DEFAULT_PORT))
    return out


def nats_reachable(url: str, *, timeout_s: float = 1.5) -> bool:
    """A TCP connect to any endpoint of ``url`` succeeds. Cheap, dependency-free —
    this is a door, not a client; the worker's real connection comes later."""
    for host, port in nats_endpoints(url):
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def check_job_channel(operator_names: Iterable[str], *, url: Optional[str] = None,
                      reachable=nats_reachable) -> ServiceVerdict:
    """The verdict for a launch invoking ``operator_names``.

    Requires NATS only when the channel is NATS **and** some leg is a remote
    job; then refuses unless an endpoint answers. ``reachable`` is injectable
    for tests (the door must be provable without a broker)."""
    chan = url if url is not None else job_channel_url()
    if not channel_is_nats(chan):
        return ServiceVerdict()
    remote = remote_job_operators(operator_names)
    if not remote:
        return ServiceVerdict()
    v = ServiceVerdict(required=[SERVICE])
    if reachable(chan):
        return v
    v.ok = False
    v.reasons.append(
        f"{', '.join(remote)} stream{'s' if len(remote) == 1 else ''} live run events over the "
        f"job channel, and {JOB_CHANNEL_ENV}={chan} is not reachable — start the "
        f"'{COMPOSE_PROFILE}' service (COMPOSE_PROFILES={COMPOSE_PROFILE}) or set "
        f"{JOB_CHANNEL_ENV}=http to stream over the control plane's own HTTPS"
    )
    return v


__all__ = [
    "COMPOSE_PROFILE", "JOB_CHANNEL_ENV", "SERVICE", "ServiceVerdict", "channel_is_nats",
    "check_job_channel", "is_remote_job_operator", "job_channel_url", "nats_endpoints",
    "nats_reachable", "remote_job_operators",
]
