"""Drive a coroutine to completion from synchronous code.

A stdlib-only leaf, shared by the sync facades over async stores (secrets,
schedules, policies) and the sync auth helpers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` and return its result, whether or not a loop is running.

    Off-loop → :func:`asyncio.run`. Inside a running loop (a sync facade called
    from async code, or a test under ``asyncio_mode=auto``) a nested
    ``asyncio.run`` is illegal, so the coroutine runs on a fresh loop in a
    worker thread and this call blocks on its result. That private loop is why a
    resource bound to the caller's loop (an asyncpg pool) must not be reached
    through here — await it instead (#2378).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()
