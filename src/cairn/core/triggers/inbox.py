"""Deprecated alias — moved to :mod:`cairn.core.storage.trigger_inbox` (#2416, ADR-0120 D6).

The durable trigger inbox is persistence, not HTTP, so it lives in ``storage/``. This
path stays importable for one release and is removed after it: it binds
``cairn.core.triggers.inbox`` to the SAME module object, so an import or an
attribute patch through either path reaches the one store. Import the new home.
"""
import sys

from cairn.core.storage import trigger_inbox as _moved

sys.modules[__name__] = _moved
