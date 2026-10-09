"""How far a sync run has got, told to whoever is watching (ARCHITECTURE.md §9).

A run reports what it is doing as it does it: which repository it is scanning,
how many of the files it must reindex are written, which stage of the global
link pass it is in. The command line draws that as
a progress bar; every other caller passes nothing and hears nothing.

Reporting is an observation of the run and never a part of it. No event carries
repository content, nothing a listener does can change what is indexed, and a run
with no listener does exactly the work a run with one does. That is why the
protocol takes plain numbers and names: a listener that wanted more would be a
second consumer of the index, and there is already one of those.
"""

from __future__ import annotations

from typing import Protocol

#: The global link pass's stages, in the order a run reaches them
#: (:meth:`repoatlas.graph.linker.ReferenceLinker.prepare_global`, then the swap).
LINK_STAGES: tuple[str, ...] = ("index", "resolve", "uses", "persistence", "dependencies", "swap")


class SyncProgress(Protocol):
    """Receives a run's progress. Every method returns quickly and never raises."""

    def discovering(self) -> None:
        """The managed root is being searched for repositories."""
        ...

    def scanning(self, position: int, total: int, repository: str, hashed: int) -> None:
        """Repository ``position`` of ``total`` is being enumerated and compared.

        ``hashed`` counts the files whose content had to be hashed so far, the
        part of a scan that grows with the repository.
        """
        ...

    def indexing(self, position: int, total: int, repository: str, done: int, files: int) -> None:
        """``done`` of the ``files`` this repository needs reindexed are written."""
        ...

    def linking(self, stage: str, position: int, total: int) -> None:
        """The global link pass entered ``stage``, one of :data:`LINK_STAGES`."""
        ...


class _Silent:
    """The listener a run has when nobody asked to watch it."""

    def discovering(self) -> None:
        pass

    def scanning(self, position: int, total: int, repository: str, hashed: int) -> None:
        pass

    def indexing(self, position: int, total: int, repository: str, done: int, files: int) -> None:
        pass

    def linking(self, stage: str, position: int, total: int) -> None:
        pass


SILENT: SyncProgress = _Silent()
