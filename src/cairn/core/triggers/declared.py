"""Deprecated alias — moved to :mod:`cairn.core.engine.triggers_grammar` (#2416, ADR-0120 D6).

The flow-declared trigger grammar (``triggers:`` in front matter) is a
compile-time grammar, not HTTP, so it lives in ``engine/``.
This path stays importable for one release and is removed after it: it binds
``cairn.core.triggers.declared`` to the SAME module object, so an import or an
attribute patch through either path reaches the one module. Import the new home.
"""
import sys

from cairn.core.engine import triggers_grammar as _moved

sys.modules[__name__] = _moved
