"""hello-cairn — the example domain pack (#2423, ADR-0127 D2). Data plus glue; this
shim builds the Pack from the ``pack.toml`` beside it. The glue (``hooks.py``,
``tools/calc_server.py``) is reached through an entry point and a profile, never
imported here."""
from __future__ import annotations

from cairn.core.pack import Pack

pack = Pack.from_manifest(__file__)

__all__ = ["pack"]
