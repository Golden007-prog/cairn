"""In-process leg liveness: the process owner lock (ADR-0125 D2).

An in-process leg (a task leg, a template leg on the in-process runtime, a flow
leg's poll loop) runs inside one control-plane process and dies with it. Its
run names that process in ``Run.owner`` (ADR-0125 D1), and this module is how
anyone else can ask whether that process is still alive.

**The owner id** is ``"<hostname>:<boot id>"``. The boot id is random and made
once per process start, so two processes on one host — a server and a ``cairn
pipeline run`` CLI next to it, two replicas in one pod — never share an id.

**Holding a lock is being alive.** There is no heartbeat write and no clock
arithmetic, for the reasons ADR-0125 gives: a timer write would race a leg's
terminal write on Postgres, and it falls silent when a leg blocks the event
loop. A lock is held by the database or the kernel, not by the loop, so a
blocked loop keeps it.

- *Postgres store.* A session advisory lock in the two-int4 key space,
  ``pg_advisory_lock(OWNER_CLASS_ID, hashtext(owner))``, on a dedicated size-1
  pool (one per instance, the #2543 lesson), with TCP keepalives on. The
  two-int4 space never collides with the single-key locks (leadership, the
  critical sections, budget reservations); inside it, ``OWNER_CLASS_ID`` differs
  from the schema-init and run-events first keys, so a lifetime owner lock can
  never block schema setup or an event append. A re-acquire loop checks the lock
  every leadership poll interval and takes it again with a short ``lock_timeout``
  when the session was lost (a Postgres restart, a killed backend).
- *SQLite store.* An OS ``flock`` on ``<db dir>/.cairn-owners/<owner>.lock``,
  held for the process's life. SQLite is single-host, so a file lock is exact;
  the kernel releases it when the process exits, ``kill -9`` included. Lock
  files whose non-blocking ``flock`` succeeds at start-up belong to dead owners
  and are deleted. ``flock`` is not reliable on NFS-backed volumes.
- *No store, or the in-memory one.* Nothing else can see this process's runs,
  so the lock is held trivially and every probe is undetermined.

**The probe** (:meth:`OwnerLock.lock_free`) answers "is that owner's lock free?"
and never runs on the owner's own connection, because session locks are
reentrant: on Postgres it is ``pg_try_advisory_xact_lock`` in a short
transaction on the STORE pool (the probe's lock releases at commit); on SQLite a
non-blocking ``flock`` on the owner's file. The verdict built on it lives in
``runtime/reconciler.py`` (``owner_verdict``).

**A process that does not hold its lock starts nothing.** The pipeline bridge
leaves a leg it would start ``pending`` (or ``queued``) and records the pipeline
here (:meth:`OwnerLock.hold_back`); re-acquiring the lock fires the registered
callbacks, and the bridge runs one advance pass over what it held back.

**Process singleton (approved with ADR-0125, root CLAUDE.md §5).** The server
lifespan and the ``cairn pipeline run`` CLI each start one with
:func:`start_process_owner`; :func:`current` is how the bridge, the dispatcher
and the reconciler find it. A process that never starts one (tests, scripts)
writes no owner, so its runs read as legacy rows and keep the #2540 rules.

Deployment assumptions: a direct or session-mode Postgres connection
(PgBouncer transaction pooling breaks session locks), ``idle_session_timeout =
0`` (set on the owner session where the server allows it), and no proxy that
idles out a quiet connection.
"""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import os
import re
import socket
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from cairn.core.leadership import ACQUIRE_TIMEOUT_SECONDS, POLL_SECONDS, postgres_dsn

logger = logging.getLogger("cairn.liveness")

#: First key of the owner lock in Postgres's two-int4 advisory key space. A fixed
#: constant, not a hash: it must differ from ``hashtext('cairn:schema-init')``
#: (``core/pg_schema_init.py``) and ``hashtext('cairn:run_events')``
#: (``storage/postgres/runs.py``), the two other users of that space, and a test
#: asserts it does.
OWNER_CLASS_ID = 0x0CA10125

#: How often the owner lock is checked and, when lost, taken again. Leadership's
#: poll interval: the reconciler calls an owner DEAD only after two probes more
#: than one interval apart, so a lock lost to a Postgres restart is back before
#: the second probe.
REACQUIRE_SECONDS = POLL_SECONDS

#: ``lock_timeout`` for taking the lock: long enough that a probe's momentary
#: transaction lock on the same key cannot make the owner skip an interval,
#: short enough that a real holder (an id collision) does not hang the loop.
_LOCK_TIMEOUT_MS = 2000

#: Bound on one probe. A probe that takes longer is undetermined, never "free".
PROBE_TIMEOUT_SECONDS = 5.0

_LOCK_SQL = "SELECT pg_advisory_lock($1::int4, hashtext($2))"
_TRY_XACT_SQL = "SELECT pg_try_advisory_xact_lock($1::int4, hashtext($2))"
#: Is the lock still ours? A two-int4 advisory lock shows in ``pg_locks`` with
#: ``classid`` = key 1, ``objid`` = key 2 and ``objsubid = 2``.
_HELD_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()
          AND classid = ($1::int4)::oid AND objid = (hashtext($2))::oid AND objsubid = 2)
"""
#: Server-side keepalives for the owner session (no-ops on a unix socket), and no
#: idle-session timeout where the server lets a session set it.
_SESSION_SETTINGS = (
    f"SET lock_timeout = '{_LOCK_TIMEOUT_MS}ms'",
    "SET tcp_keepalives_idle = 30",
    "SET tcp_keepalives_interval = 10",
    "SET tcp_keepalives_count = 3",
    "SET idle_session_timeout = 0",
)

#: The lock files' directory, next to the SQLite database.
OWNERS_DIRNAME = ".cairn-owners"

Callback = Callable[[], Union[Awaitable[None], None]]


def new_boot_id() -> str:
    """A fresh random boot id."""
    return uuid.uuid4().hex[:16]


def _safe_name(owner: str) -> str:
    """The owner id as a file name."""
    return re.sub(r"[^A-Za-z0-9._:-]", "_", owner)


def _enable_tcp_keepalives(conn: Any) -> None:
    """Client-side TCP keepalives on the owner connection (asyncpg sets none)."""
    try:
        transport = getattr(conn, "_transport", None)  # asyncpg exposes no public socket accessor
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is None or sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        for opt, value in ((idle, 30), (getattr(socket, "TCP_KEEPINTVL", None), 10),
                           (getattr(socket, "TCP_KEEPCNT", None), 3)):
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except Exception:  # noqa: BLE001 — a private asyncpg accessor; keepalives are best-effort
        logger.debug("liveness: could not set TCP keepalives", exc_info=True)


class OwnerLock:
    """This process's owner id and the lock that proves it is alive.

    ``dsn`` selects the Postgres lock, ``lock_dir`` the SQLite file lock; with
    neither the lock is held trivially (no shared store). Use
    :func:`start_process_owner` in a process; construct directly in tests,
    where two instances stand for two processes."""

    def __init__(self, *, dsn: str = "", lock_dir: str = "",
                 poll_seconds: float = REACQUIRE_SECONDS,
                 hostname: Optional[str] = None) -> None:
        self.dsn = dsn
        self.lock_dir = Path(lock_dir) if lock_dir else None
        self.poll_seconds = poll_seconds
        self.hostname = hostname or socket.gethostname() or "localhost"
        self.boot_id = new_boot_id()
        self._held = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list[Callback] = []
        self._held_back: dict[str, set[str]] = {}
        # Postgres: one size-1 pool per INSTANCE (#2543), a unique purpose.
        self._pool: Any = None
        self._conn: Any = None
        self._pool_purpose = f"owner-lock:{uuid.uuid4().hex[:12]}"
        # SQLite: the open file whose flock is the lock.
        self._fd: Optional[int] = None

    # ── identity ─────────────────────────────────────────────────────

    @property
    def owner(self) -> str:
        return f"{self.hostname}:{self.boot_id}"

    @property
    def held(self) -> bool:
        return self._held

    @property
    def backend(self) -> str:
        if self.dsn:
            return "postgres"
        return "sqlite" if self.lock_dir is not None else "memory"

    # ── callbacks and held-back pipelines ────────────────────────────

    def on_reacquired(self, cb: Callback) -> Callable[[], None]:
        """Run ``cb`` each time the lock is taken again after being lost (or
        after a failed start). Returns the function that unregisters it."""
        self._callbacks.append(cb)

        def _remove() -> None:
            if cb in self._callbacks:
                self._callbacks.remove(cb)
        return _remove

    def hold_back(self, pipeline_id: str, step_id: str = "", *, released: bool = False) -> None:
        """Record a pipeline whose leg this process did not start because it
        does not hold its lock. ``released``: a person had already released the
        step (approve/advance), so the pass that re-acquiring runs submits it
        without asking again."""
        steps = self._held_back.setdefault(pipeline_id, set())
        if released and step_id:
            steps.add(step_id)

    def take_held_back(self) -> dict[str, set[str]]:
        """The held-back pipelines (pipeline id → released step ids), cleared."""
        held, self._held_back = self._held_back, {}
        return held

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Take the lock now, then keep it in the background. A failed FIRST
        attempt regenerates the boot id (nothing references it yet), so a
        ``hashtext`` collision with a live owner never locks a process out."""
        got = await self._acquire()
        if not got:
            old = self.owner
            self.boot_id = new_boot_id()
            logger.warning("liveness: could not take owner lock %s at start — retrying "
                           "as %s", old, self.owner)
            got = await self._acquire()
        self._held = got
        if got:
            logger.info("liveness: holding owner lock %s (%s)", self.owner, self.backend)
        else:
            logger.warning("liveness: owner lock %s not held — this process starts and "
                           "adopts no in-process leg until it is (retry every %.0fs)",
                           self.owner, self.poll_seconds)
        self._task = asyncio.create_task(self._loop(), name="liveness:owner-lock")

    async def stop(self) -> None:
        """Stop the loop and release the lock."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown
                pass
        self._held = False
        await self._release()
        await self._close_pool()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll_seconds)
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the owner loop must never die
                logger.exception("liveness: owner-lock loop error")

    async def check(self) -> bool:
        """One pass of the re-acquire loop: confirm the lock, take it again when
        it was lost, and fire the callbacks on a re-acquire. Returns ``held``."""
        if self._held and await self._still_held():
            return True
        if self._held:
            self._held = False
            logger.warning("liveness: LOST owner lock %s — starting and adopting no "
                           "in-process leg until it is taken again", self.owner)
            await self._release()
        if await self._acquire():
            self._held = True
            logger.info("liveness: owner lock %s taken again", self.owner)
            await self._fire_callbacks()
        return self._held

    async def _fire_callbacks(self) -> None:
        for cb in list(self._callbacks):
            try:
                out = cb()
                if inspect.isawaitable(out):
                    await out
            except Exception:  # noqa: BLE001 — one bad callback must not cost the others
                logger.exception("liveness: re-acquire callback failed")

    # ── the lock, per backend ────────────────────────────────────────

    async def _acquire(self) -> bool:
        if self.backend == "postgres":
            return await self._pg_acquire()
        if self.backend == "sqlite":
            return self._file_acquire()
        return True

    async def _still_held(self) -> bool:
        if self.backend == "postgres":
            try:
                return bool(await asyncio.wait_for(
                    self._conn.fetchval(_HELD_SQL, OWNER_CLASS_ID, self.owner),
                    timeout=ACQUIRE_TIMEOUT_SECONDS))
            except Exception:  # noqa: BLE001 — a dead session is a lost lock
                logger.debug("liveness: owner session check failed", exc_info=True)
                return False
        if self.backend == "sqlite":
            return self._file_still_held()
        return True

    async def _release(self) -> None:
        if self.backend == "postgres":
            await self._pg_drop_conn()
        elif self.backend == "sqlite":
            self._file_release()

    # Postgres

    async def _pg_acquire(self) -> bool:
        try:
            if self._pool is None:
                from cairn.core.storage.postgres.pool import (  # noqa: PLC0415 — optional dep (storage-postgres)
                    shared_pool,
                )

                # Its own pool: this connection is pinned for the process
                # lifetime, so it never comes from the store pool. Size 1, one
                # pool per instance (#2543).
                self._pool = await shared_pool(
                    self.dsn, purpose=self._pool_purpose, min_size=0, max_size=1)
            if self._conn is None:
                self._conn = await self._pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS)
                _enable_tcp_keepalives(self._conn)
                for stmt in _SESSION_SETTINGS:
                    try:
                        await self._conn.execute(stmt)
                    except Exception:  # noqa: BLE001 — an older server lacks a setting
                        logger.debug("liveness: %r refused", stmt, exc_info=True)
            await self._conn.fetchval(_LOCK_SQL, OWNER_CLASS_ID, self.owner)
            return True
        except Exception:  # noqa: BLE001 — unreachable or held elsewhere: not held
            logger.warning("liveness: could not take owner lock %s", self.owner,
                           exc_info=True)
            await self._pg_drop_conn()
            return False

    async def _pg_drop_conn(self) -> None:
        """Release the owner connection. Dropping it ends the session, which
        releases the lock."""
        conn, self._conn = self._conn, None
        if conn is None or self._pool is None:
            return
        try:
            await self._pool.release(conn)
        except Exception:  # noqa: BLE001 — a dead connection is already released
            try:
                conn.terminate()
            except Exception:  # noqa: BLE001
                pass

    async def _close_pool(self) -> None:
        pool, self._pool = self._pool, None
        self._pool_purpose = f"owner-lock:{uuid.uuid4().hex[:12]}"
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=ACQUIRE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — shutdown; never block standing down
            pool.terminate()

    # SQLite

    def _lock_path(self, owner: str) -> Path:
        assert self.lock_dir is not None
        return self.lock_dir / f"{_safe_name(owner)}.lock"

    def _file_acquire(self) -> bool:
        """Lock a fresh file under a temporary name, then rename it into place,
        so a file at an owner's path is always already locked (a start-up GC
        that runs meanwhile never deletes a live owner's file)."""
        try:
            self.lock_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            if self._fd is None:
                gc_dead_owner_files(self.lock_dir)  # type: ignore[arg-type]
            path = self._lock_path(self.owner)
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
            fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.replace(tmp, path)
            except OSError:
                os.close(fd)
                tmp.unlink(missing_ok=True)
                raise
            self._file_release()
            self._fd = fd
            return True
        except OSError:
            logger.warning("liveness: could not take owner lock file for %s", self.owner,
                           exc_info=True)
            return False

    def _file_still_held(self) -> bool:
        """Ours while the path still names the file we hold locked."""
        if self._fd is None:
            return False
        try:
            return os.stat(self._lock_path(self.owner)).st_ino == os.fstat(self._fd).st_ino
        except OSError:
            return False

    def _file_release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            path = self._lock_path(self.owner)
            if os.stat(path).st_ino == os.fstat(fd).st_ino:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            os.close(fd)  # closing the descriptor releases the flock
        except OSError:
            pass

    # ── the probe ────────────────────────────────────────────────────

    async def lock_free(self, owner: str) -> Optional[bool]:
        """Is ``owner``'s lock free? ``True`` free, ``False`` held, ``None``
        when this backend cannot tell. Raises on a store error; the verdict
        treats that, and a timeout, as undetermined."""
        if self.backend == "postgres":
            from cairn.core.storage.postgres.pool import shared_pool  # noqa: PLC0415 — optional dep (storage-postgres)

            # The STORE pool, never the owner's connection: session locks are
            # reentrant, so probing on it would always find a lock we hold free.
            pool = await shared_pool(self.dsn)
            async with pool.acquire(timeout=PROBE_TIMEOUT_SECONDS) as conn:
                async with conn.transaction():
                    return bool(await conn.fetchval(_TRY_XACT_SQL, OWNER_CLASS_ID, owner))
        if self.backend == "sqlite":
            return probe_owner_file(self._lock_path(owner))
        return None


def probe_owner_file(path: Path) -> bool:
    """``True`` when nobody holds ``path``'s flock (or the file is gone)."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)  # releases the probe's own flock, if it took one
    return True


def gc_dead_owner_files(lock_dir: Path) -> int:
    """Delete the lock files of dead owners: those whose non-blocking flock
    succeeds. Returns how many were deleted."""
    n = 0
    for path in lock_dir.glob("*.lock"):
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        try:
            path.unlink(missing_ok=True)
            n += 1
        finally:
            os.close(fd)
    return n


# ── the process singleton ────────────────────────────────────────────

_CURRENT: Optional[OwnerLock] = None
_REFS = 0


def current() -> Optional[OwnerLock]:
    """This process's owner lock, or ``None`` when it never started one."""
    return _CURRENT


def self_owner() -> str:
    """The owner id to stamp on an in-process leg; ``""`` when this process
    holds no owner lock at all (the run then reads as a legacy row)."""
    lv = _CURRENT
    return lv.owner if lv is not None else ""


def may_start_legs() -> bool:
    """False only while this process runs an owner lock it does not hold."""
    lv = _CURRENT
    return lv is None or lv.held


async def lock_free(owner: str) -> Optional[bool]:
    """The probe the reconciler and the bridge are given (``LockFreeFn``)."""
    lv = _CURRENT
    if lv is None:
        return None
    return await lv.lock_free(owner)


def _backend_for(store: Any) -> dict[str, str]:
    """Postgres when ``CAIRN_STORAGE`` names it (leadership's selector); the
    SQLite file lock next to a file-backed run store; else none."""
    dsn = postgres_dsn()
    if dsn:
        return {"dsn": dsn}
    path = str(getattr(store, "path", "") or "")
    if path and path != ":memory:":
        return {"lock_dir": str(Path(path).expanduser().resolve().parent / OWNERS_DIRNAME)}
    return {}


async def start_process_owner(store: Any = None, **kwargs: Any) -> OwnerLock:
    """Start this process's owner lock (or return the one already running).
    ``store`` is the run store, which decides the backend."""
    global _CURRENT, _REFS
    if _CURRENT is None:
        lv = OwnerLock(**{**_backend_for(store), **kwargs})
        _CURRENT = lv
        await lv.start()
    _REFS += 1
    return _CURRENT


async def stop_process_owner() -> None:
    """Release one reference to the process owner lock; the last one stops it."""
    global _CURRENT, _REFS
    _REFS = max(0, _REFS - 1)
    if _REFS == 0 and _CURRENT is not None:
        lv, _CURRENT = _CURRENT, None
        await lv.stop()


__all__ = [
    "OWNER_CLASS_ID",
    "OWNERS_DIRNAME",
    "PROBE_TIMEOUT_SECONDS",
    "REACQUIRE_SECONDS",
    "OwnerLock",
    "current",
    "gc_dead_owner_files",
    "lock_free",
    "may_start_legs",
    "new_boot_id",
    "probe_owner_file",
    "self_owner",
    "start_process_owner",
    "stop_process_owner",
]
