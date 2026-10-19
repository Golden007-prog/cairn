"""Deprecated alias — moved to :mod:`cairn.core.engine.triggers_presets` (#2416, ADR-0120 D6).

The webhook-receiver presets are data the trigger grammar validates against,
not HTTP, so they live in ``engine/`` beside it.
This path stays importable for one release and is removed after it: it binds
``cairn.core.triggers.presets`` to the SAME module object, so an import or an
attribute patch through either path reaches the one module. Import the new home.
"""
import sys

from cairn.core.engine import triggers_presets as _moved

sys.modules[__name__] = _moved
