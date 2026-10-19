"""Deprecated alias — moved to :mod:`cairn.core.engine.triggers_spec` (#2416, ADR-0120 D6).

The pack trigger-YAML spec and its loader are a grammar, not HTTP, so they live
in ``engine/`` beside the declared-trigger grammar that uses them.
This path stays importable for one release and is removed after it: it binds
``cairn.core.triggers.spec`` to the SAME module object, so an import or an
attribute patch through either path reaches the one module. Import the new home.
"""
import sys

from cairn.core.engine import triggers_spec as _moved

sys.modules[__name__] = _moved
