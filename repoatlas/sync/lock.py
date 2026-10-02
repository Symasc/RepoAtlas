"""The knowledge-base writer lock (ARCHITECTURE.md §9).

One invariant, enforced here:

> At most one RepoAtlas knowledge-base writer is active for a state database.

The lock is an **OS advisory lock** (``flock``) on a file beside the database,
not a row in ``meta``. That choice is the whole design, and it was made for one
reason: a lock row records an *intention*, and nothing revokes an intention when
the process holding it dies. ``kill -9`` on a syncing container leaves a row
saying "locked" and no way to tell a live writer from a corpse — so every
row-based scheme needs a liveness heuristic (a pid, a hostname, a heartbeat, a
staleness timeout), and every such heuristic is wrong somewhere: pids are reused,
hostnames collide between containers, and a timeout either strands the lock or
breaks it under a writer that was merely slow.

The kernel already knows which processes are alive. An ``flock`` is held by an
open file description, so it is released on ``close()``, on ``exit()``, on an
uncaught exception, on ``SIGTERM`` and on ``SIGKILL``, without RepoAtlas running
any recovery code at all. There is no stale-lock state to reason about, because
there is no state — which is why the lock file's contents are diagnostic only
and are never consulted to decide whether the lock is held.

Scope is one state database. The lock file sits next to it, so two workspaces
pointed at different databases never contend, and every process sharing one
database does — including a container and a host process bind-mounting the same
state volume.

Readers do not take this lock. WAL gives them a committed snapshot, so ``locate``,
``status``, ``repos`` and ``ask`` run unimpeded during a sync (ARCHITECTURE.md §9).
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from repoatlas.errors import RepoAtlasError, SyncInProgressError

#: How long a contending writer waits before giving up. A bounded wait rather
#: than an immediate refusal, because two overlapping syncs are ordinary — a cron
#: run and a manual one, a watcher and a shell — and making the second one wait a
#: moment is friendlier than making the user rerun it. Bounded rather than
#: indefinite, because a writer that never finishes must be reported, not waited
#: on in silence (§13 of the remediation).
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Polling interval while waiting. ``flock`` has no timeout of its own, and the
#: alternatives (``SIGALRM``, a watchdog thread) buy precision nobody needs here.
_POLL_SECONDS = 0.05


def lock_path(database: Path) -> Path:
    """The lock file for a state database: ``atlas.db`` → ``atlas.db.lock``."""
    return database.with_name(database.name + ".lock")


def _flock():
    """The POSIX advisory-locking module, or a clear error explaining its absence.

    Imported lazily so that a platform without it can still *read* an index. A
    writer, though, fails loudly: a sync that cannot serialize must not run
    unserialized and silently corrupt the graph, which is exactly the failure
    this module exists to prevent.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX is the supported target
        raise RepoAtlasError(
            "RepoAtlas cannot serialize writers on this platform.",
            detail=(
                "The writer lock needs POSIX advisory locking (flock), which this "
                "Python build does not provide. Reading an existing index still "
                "works; indexing does not."
            ),
        ) from exc
    return fcntl


@contextmanager
def writer_lock(
    database: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> Iterator[Path]:
    """Hold the exclusive writer lock for ``database`` for the duration of the block.

    Waits up to ``timeout`` seconds, then raises :class:`SyncInProgressError`. A
    ``timeout`` of zero tries once and reports immediately.
    """
    fcntl = _flock()
    path = lock_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Opened read/write rather than write-only: the diagnostic owner line written
    # below is read back to name the other process in the error message, and a
    # truncating open would destroy the incumbent's line before we know whether we
    # can even take the lock.
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _in_progress(path) from None
                time.sleep(_POLL_SECONDS)

        _record_owner(handle, now=now)
        try:
            yield path
        finally:
            # Released explicitly on the way out, and by the kernel if we never
            # get here at all.
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _record_owner(handle: int, *, now: datetime | None) -> None:
    """Write who holds the lock. Diagnostic only — never read to decide ownership.

    Not ``fsync``ed: another process reads it through the shared page cache, and
    the line has no value after a crash — the kernel has already released the lock
    by then. The ``fsync`` cost about 4 ms, which is most of what acquiring an
    uncontended lock would otherwise be.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    owner = f"pid={os.getpid()} host={socket.gethostname()} since={stamp}\n"
    try:
        os.ftruncate(handle, 0)
        os.lseek(handle, 0, os.SEEK_SET)
        os.write(handle, owner.encode("utf-8"))
    except OSError:  # pragma: no cover - a read-only state directory is fatal anyway
        pass


def _read_owner(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - the holder may be rewriting it
        return ""


def _in_progress(path: Path) -> SyncInProgressError:
    """The contention error, with the remedy public and the coordinates local.

    ``detail`` is the whole of what a V2 capability caller is told, because that
    is the field the capability envelope carries to one. It says what to do and
    nothing else: the holder's pid, the holder's hostname and the absolute lock
    path describe the machine RepoAtlas is running on, and an external agent can
    neither act on them nor be trusted with them merely because it asked to sync
    (FINAL audit, F-01).

    The coordinates are not discarded --- an operator resolving a wedged writer
    needs exactly them. They travel as attributes and reach the terminal through
    :meth:`SyncInProgressError.render`, which only the CLI calls.
    """
    return SyncInProgressError(
        "Another RepoAtlas synchronization is already in progress.",
        detail="Wait for it to finish and try again.",
        owner=_read_owner(path),
        lock_path=path,
    )
