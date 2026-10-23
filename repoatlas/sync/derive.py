"""What a file's content yields before anything is written, and where it is computed.

Parsing, dependency extraction and chunking are a pure function of a file's path and
content, and they are almost all of what indexing costs: a fresh index of a few
thousand files spends minutes in them and seconds in the writes. They touch no
database, so they need not run where the writes do. :class:`Deriver` computes them in
a pool of worker processes once a repository pass has enough files to repay starting
one, and in this process otherwise.

The writes stay where they were. The engine consumes what is derived **in plan
order**, one file per transaction, exactly as before, so the parallel pass commits the
same rows in the same order as the serial one; only the parsing moved
(ARCHITECTURE.md §9).

Workers are started by a fork server, never by forking this process. A sync can run
on a thread of a process with others (the HTTP API indexes from one), and a fork taken
while another thread holds a lock gives the child a lock nobody will release. A worker
builds its own parser and dependency registries, and before the pool is trusted with a
single file it is asked for the facts version those registries produce: a pool whose
version differs from the engine's would record facts under a version that did not
produce them, so it is not used, and the pass parses here instead.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import select
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from repoatlas.config.settings import IndexingSettings
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.indexing.chunking import Chunk, chunk_content
from repoatlas.models.dependencies import DependencyReport
from repoatlas.models.parsing import ParsedFile, ParseDiagnostic
from repoatlas.parsers.registry import ParserRegistry

#: Files a pass must derive before a pool is started for it. Starting the fork server
#: and its workers costs about as much as parsing this many ordinary files, so below
#: it the pass is faster in this process, and a one-file sync never starts a process.
POOL_THRESHOLD = 64

#: The environment variable that sets the number of worker processes; ``1`` parses in
#: this process.
WORKERS_ENV = "REPOATLAS_SYNC_WORKERS"

#: How many files may be derived ahead of the one being written, per worker. Bounds
#: the content and results held in memory while keeping every worker busy behind a
#: slow file at the head of the queue.
_AHEAD_PER_WORKER = 8


@dataclass(frozen=True, slots=True)
class Derived:
    """Every fact one file's content yields, before any of it is written."""

    parsed: ParsedFile
    dependencies: tuple[tuple[str, DependencyReport], ...]
    dependency_diagnostics: tuple[ParseDiagnostic, ...]
    chunks: tuple[Chunk, ...]


def derive(
    registry: ParserRegistry,
    dependency_registry: DependencyRegistry,
    indexing: IndexingSettings,
    rel_path: str,
    content: str,
    language: str | None,
) -> Derived:
    """Parse, extract dependencies from and chunk one file's content. Pure.

    A manifest such as ``package.json`` is read twice on purpose, once by its language
    parser as configuration keys and once by the dependency parsers, because the two
    answer different questions.
    """
    path = Path(rel_path)
    parser = registry.for_path(path)
    if parser is not None:
        parsed = parser.parse(path, content)
    else:
        parsed = ParsedFile(language=language or "unknown", parser="none", parser_version="0")
    reports: list[tuple[str, DependencyReport]] = []
    diagnostics: list[ParseDiagnostic] = []
    for dependency_parser in dependency_registry.for_path(path):
        report = dependency_parser.parse(path, content)
        diagnostics.extend(report.diagnostics)
        reports.append((dependency_parser.name, report))
    return Derived(
        parsed=parsed,
        dependencies=tuple(reports),
        dependency_diagnostics=tuple(diagnostics),
        chunks=tuple(chunk_content(content, parsed.symbols, indexing)),
    )


class Pending:
    """One file's derivation, computed when it is asked for or by a worker meanwhile."""

    __slots__ = ("_compute", "_future")

    def __init__(
        self,
        *,
        future: Future[Derived] | None = None,
        compute: Callable[[], Derived] | None = None,
    ) -> None:
        self._future = future
        self._compute = compute

    def result(self) -> Derived:
        """The derivation, or the exception the parser raised for it."""
        if self._future is not None:
            return self._future.result()
        assert self._compute is not None
        return self._compute()


class Deriver:
    """Derives files for one sync run, in worker processes when that pays.

    The pool is started on the first pass that needs it and kept for the rest of the
    run, so a workspace of many repositories pays for it once. :meth:`close` stops it,
    and the engine calls it when the run ends however it ends.
    """

    def __init__(
        self,
        registry: ParserRegistry,
        dependency_registry: DependencyRegistry,
        indexing: IndexingSettings,
        facts_version: str,
    ) -> None:
        self._registry = registry
        self._dependency_registry = dependency_registry
        self._indexing = indexing
        self._facts_version = facts_version
        self._workers = configured_workers()
        self._pool: ProcessPoolExecutor | None = None
        #: Whether a pool was refused for this run, so it is not started again.
        self._refused = False

    @property
    def ahead(self) -> int:
        """How many files a pass may derive ahead of the one it is writing."""
        return self._workers * _AHEAD_PER_WORKER if self._pool is not None else 0

    def prepare(self, files: int) -> None:
        """Start the pool if a pass about to derive ``files`` files repays it."""
        if self._pool is not None or self._refused:
            return
        if self._workers <= 1 or files < POOL_THRESHOLD:
            return
        self._pool = self._start()
        if self._pool is None:
            self._refused = True

    def submit(self, rel_path: str, content: str, language: str | None) -> Pending:
        """Derive one file: queued to a worker when a pool runs, else on demand."""
        if self._pool is not None:
            return Pending(future=self._pool.submit(_derive_in_worker, rel_path, content, language))
        return Pending(
            compute=lambda: derive(
                self._registry,
                self._dependency_registry,
                self._indexing,
                rel_path,
                content,
                language,
            )
        )

    def close(self) -> None:
        """Stop the pool, abandoning whatever it has not finished."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def _start(self) -> ProcessPoolExecutor | None:
        if "forkserver" in multiprocessing.get_all_start_methods():
            context = multiprocessing.get_context("forkserver")
            # The fork server imports these once and forks every worker from itself,
            # so a worker starts with the parsers loaded. It would otherwise import
            # the parent's `__main__`, which re-runs a script that has no main guard
            # --- a whole sync, inside the fork server.
            context.set_forkserver_preload([__name__])
        else:
            context = multiprocessing.get_context("spawn")
        pool = ProcessPoolExecutor(
            max_workers=self._workers,
            mp_context=context,
            initializer=_start_worker,
            initargs=(self._indexing, os.getpid()),
        )
        try:
            version = pool.submit(_worker_facts_version).result()
        except Exception:
            # A pool that cannot start --- no semaphores in a sandbox, a process limit
            # --- costs the run its parallelism and nothing else.
            pool.shutdown(wait=True, cancel_futures=True)
            return None
        if version != self._facts_version:
            pool.shutdown(wait=True, cancel_futures=True)
            return None
        return pool


def configured_workers() -> int:
    """Worker processes a pass may use: ``$REPOATLAS_SYNC_WORKERS``, else one per CPU.

    An unparseable or non-positive value is ignored rather than fatal, as the other
    environment overrides are: it must not stop indexing, only lose the override.
    """
    configured = os.environ.get(WORKERS_ENV)
    if configured:
        with contextlib.suppress(ValueError):
            workers = int(configured)
            if workers >= 1:
                return workers
    return _available_cpus()


def _available_cpus() -> int:
    counter = getattr(os, "process_cpu_count", None)
    if counter is not None:
        return counter() or 1
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


# -- the worker side ---------------------------------------------------------

#: A worker's registries and chunking settings, set once when it starts.
_worker: tuple[ParserRegistry, DependencyRegistry, IndexingSettings] | None = None


def _start_worker(indexing: IndexingSettings, parent: int) -> None:
    """Make a worker process ready to derive files for ``parent``.

    An interrupt is the parent's to handle: it stops the pool, and a worker that took
    the same Ctrl-C would only print a second traceback. A worker also ends with the
    process it works for (:func:`_end_with`).
    """
    global _worker
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _end_with(parent)
    _worker = (ParserRegistry(), DependencyRegistry(), indexing)


#: How often a worker that cannot be told of its sync's exit checks for it, in seconds.
_PARENT_POLL_SECONDS = 1.0


def _end_with(parent: int) -> None:
    """Exit this worker as soon as ``parent`` has, however it ended.

    A worker waits on its task queue, and nothing writes to that queue once the sync is
    gone: a ``SIGKILL`` runs no cleanup to say so. The fork server, this worker's own
    parent, exits with the sync, but that does nothing to its children. So a thread of
    the worker watches the sync itself --- through a process descriptor where the kernel
    offers one, which becomes readable when the process exits and cannot be confused
    with a later process given the same number, and by polling elsewhere.
    """
    try:
        handle = os.pidfd_open(parent)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        handle = None
    if handle is None and not _alive(parent):
        os._exit(0)

    def watch() -> None:
        if handle is not None:
            select.select([handle], [], [])
        else:
            while _alive(parent):
                time.sleep(_PARENT_POLL_SECONDS)
        os._exit(0)

    threading.Thread(target=watch, name="repoatlas-parent-watch", daemon=True).start()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_facts_version() -> str:
    from repoatlas.sync.planner import facts_version

    assert _worker is not None
    registry, dependency_registry, _ = _worker
    return facts_version(registry, dependency_registry)


def _derive_in_worker(rel_path: str, content: str, language: str | None) -> Derived:
    assert _worker is not None
    registry, dependency_registry, indexing = _worker
    return derive(registry, dependency_registry, indexing, rel_path, content, language)
