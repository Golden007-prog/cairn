"""Where a watch is allowed to read from (#1657).

`monitor/threshold` resolves its signal from a path or a URL supplied by the
caller — and the caller is the console's New-watch form. Unconstrained, that
lets anyone with console access make the ENGINE open any file it can read and
GET any address it can reach, including cloud instance-metadata endpoints.

That was tolerable while a deployment was one operator running their own engine.
ADR-0094 (self-serve signup) changed the premise: a stranger's account now gets
a console on someone else's engine.

Both guards fail CLOSED and both are configurable, because "watch a metrics file
on the box" and "watch an internal endpoint" are the legitimate uses this
operator exists for — the point is that they become deliberate.

Stdlib only, keeping the pack's zero-dependency property.
"""
from __future__ import annotations
from cairn.core.operator import getenv

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

#: Colon-separated roots a watch may read files from. Defaults to the cairn home,
#: which is where a deployment's own metrics files sensibly live.
ROOTS_ENV = "CAIRN_WATCH_FILE_ROOTS"
#: Opt in to URLs that resolve to loopback / private / link-local addresses.
ALLOW_PRIVATE_ENV = "CAIRN_WATCH_ALLOW_PRIVATE_URLS"

_TRUE = {"1", "true", "yes", "on"}


class SourceRefused(ValueError):
    """A watch source is outside what this deployment permits. A ValueError so
    it surfaces as a governed operator failure with its reason, not a 500."""


def allowed_roots() -> list[Path]:
    raw = getenv(ROOTS_ENV, "").strip()
    if not raw:
        return [Path.home() / ".cairn"]
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]


def check_path(raw: str) -> Path:
    """Resolve ``raw`` and require it to sit under an allowed root.

    ``resolve()`` follows symlinks BEFORE the comparison, so a link inside an
    allowed root that points at ``/etc/shadow`` is rejected on where it lands,
    not on where it appears to be.
    """
    roots = allowed_roots()
    target = Path(raw).expanduser().resolve()
    for root in roots:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        return target
    listed = ", ".join(str(r) for r in roots)
    raise SourceRefused(
        f"watch may not read {raw!r}: it is outside this deployment's allowed "
        f"roots ({listed}). Move the file under one, or set {ROOTS_ENV}."
    )


def check_url(raw: str) -> str:
    """Require http/https to a non-private address.

    The scheme check is not a formality: ``urllib`` happily opens ``file://``,
    so without it a URL source reads local files and walks straight around
    :func:`check_path`.
    """
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise SourceRefused(
            f"watch source_url must be http(s), got {parsed.scheme or 'no'} scheme. "
            f"Use source_path for a file — it is checked against the allowed roots."
        )
    host = parsed.hostname
    if not host:
        raise SourceRefused(f"watch source_url has no host: {raw!r}")
    if getenv(ALLOW_PRIVATE_ENV, "").strip().lower() in _TRUE:
        return raw

    for ip in _resolve(host):
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SourceRefused(
                f"watch may not fetch {host!r}: it resolves to {ip}, a private or "
                f"link-local address — that reaches the engine's own network and "
                f"cloud metadata endpoints. Set {ALLOW_PRIVATE_ENV}=1 to allow it "
                f"deliberately."
            )
    return raw


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    """Every address ``host`` resolves to. A literal IP resolves to itself.

    NOTE, honestly: this checks the addresses as they resolve HERE, and urllib
    resolves again when it connects. A DNS-rebinding attacker controlling the
    zone can therefore still slip through the gap. Closing it properly means
    pinning the connection to the checked address (a custom opener) or putting
    an egress allow-list in front of the engine — tracked rather than pretended
    away. This still removes the whole class of `http://169.254.169.254/…` and
    `http://localhost:…` attempts, which is what a form field actually invites.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SourceRefused(f"watch source_url host {host!r} does not resolve: {e}") from None
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise SourceRefused(f"watch source_url host {host!r} resolved to no usable address")
    return out


__all__ = ["SourceRefused", "allowed_roots", "check_path", "check_url", "ROOTS_ENV", "ALLOW_PRIVATE_ENV"]
