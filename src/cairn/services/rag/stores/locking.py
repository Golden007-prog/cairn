"""One collection operation at a time on a shared vector store.

A :class:`~cairn.services.rag.stores.base.VectorStore` keeps the collection its
next operation acts on as instance state (``initialize`` selects it). The
process-wide RAG store and the episodic-memory store are each one instance used
for every workspace's collections, so "select the collection" and "run the store
operation" must happen with no other caller in between. :func:`on_collection`
does both under the store's :class:`StoreLock`.

Spans are short on purpose: a caller holds the lock for a select plus the store
calls that need it (a search, an upsert batch, a delete) — never across
embedding, parsing or answer synthesis.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from cairn.services.rag.stores.base import VectorStore

#: How long a caller waits for a busy store before :class:`VectorStoreBusy`.
LOCK_WAIT_S = 30.0


class VectorStoreBusy(RuntimeError):
    """The knowledge store stayed busy for longer than :data:`LOCK_WAIT_S`."""


class StoreLock:
    """A FIFO lock for asyncio callers on any event loop or thread.

    Waiters park on a future of their own loop (no polling, no worker thread);
    a release hands the lock to the oldest waiter. The owner is the asyncio task
    that acquired it, so a task that merely inherited a context from the owner
    does not count as holding it. A waiter that times out or is cancelled never
    ends up owning the lock."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future, asyncio.Task]] = deque()
        #: the (collection, dim) the owner last selected, for nested re-entry
        self.selected: Optional[tuple[str, int]] = None

    def owned_by_current_task(self) -> bool:
        task = asyncio.current_task()
        return task is not None and self._owner is task

    def locked(self) -> bool:
        return self._owner is not None

    async def acquire(self, timeout: Optional[float] = None) -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        wait = LOCK_WAIT_S if timeout is None else timeout
        with self._mutex:
            if self._owner is None:
                self._owner = task
                return
            fut = loop.create_future()
            entry = (loop, fut, task)
            self._waiters.append(entry)
        try:
            await asyncio.wait_for(fut, wait)
        except BaseException as exc:
            with self._mutex:
                try:
                    self._waiters.remove(entry)
                    granted = False
                except ValueError:  # a release already handed the lock to this waiter
                    granted = fut.done() and not fut.cancelled()
                    if not fut.done():
                        fut.cancel()  # the pending hand-over sees this and passes the lock on
            if granted:
                self.release()
            if isinstance(exc, TimeoutError):
                raise VectorStoreBusy("knowledge store busy, try again") from None
            raise

    def release(self) -> None:
        while True:
            with self._mutex:
                self.selected = None
                if not self._waiters:
                    self._owner = None
                    return
                loop, fut, task = self._waiters.popleft()
                self._owner = task
            try:
                loop.call_soon_threadsafe(self._hand_over, fut)
                return
            except RuntimeError:  # that waiter's loop is closed: next one
                continue

    def _hand_over(self, fut: asyncio.Future) -> None:
        if fut.done():  # the waiter gave up meanwhile
            self.release()
        else:
            fut.set_result(True)


@asynccontextmanager
async def on_collection(
    store: VectorStore, collection: str, dim: int, lock: StoreLock,
    *, timeout: Optional[float] = None,
) -> AsyncIterator[VectorStore]:
    """Select ``collection`` on the shared ``store`` and hold ``lock`` until the
    block ends; keep the block to the store calls themselves.

    Waits at most ``timeout`` (default :data:`LOCK_WAIT_S`) and then raises
    :class:`VectorStoreBusy`. Re-entrant for the task that holds the lock: a
    nested block selects its collection and, on exit, selects the outer block's
    collection again."""
    if lock.owned_by_current_task():
        previous = lock.selected
        await store.initialize(collection, dim)
        lock.selected = (collection, dim)
        try:
            yield store
        finally:
            if previous is not None and previous != (collection, dim):
                await store.initialize(*previous)
            lock.selected = previous
        return
    await lock.acquire(timeout)
    try:
        await store.initialize(collection, dim)
        lock.selected = (collection, dim)
        yield store
    finally:
        lock.release()


__all__ = ["LOCK_WAIT_S", "StoreLock", "VectorStoreBusy", "on_collection"]
