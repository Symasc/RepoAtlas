"""Bounded, charged neighbour retrieval: how the change-impact walk reads the graph.

A traversal that fetches every neighbour and then cuts does work in proportion to
the largest hub it touches, and one constant imported by five thousand files makes
one question five thousand rows before anything is decided. Here no call can do
that. A node's size is learned from an exact count; its neighbours arrive in
keyset pages no larger than what the budget still allows; how they spread over
files is a bounded grouping; and chosen representatives are fetched by name. Every
query, and every row one returns, is charged to :class:`Work`, whose limits are
the most one question may cost. When a limit is reached nothing more is read, and
the walk reports what it did not get to rather than silently stopping.

Confinement (:class:`~repoatlas.storage.protocols.Confinement`) is applied inside
each query, so what is counted or paged is exactly what may be followed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import (
    Confinement,
    EntityIdentity,
    KnowledgeStore,
    NeighborCount,
    NeighborCursor,
    NeighborGroup,
    NeighborRow,
    NeighborRule,
)


@dataclass(frozen=True, slots=True)
class WorkLimits:
    """The most one question may cost."""

    expansions: int = 2_000
    rows: int = 20_000
    queries: int = 4_000


@dataclass(slots=True)
class Work:
    """What one question has cost so far, against its limits."""

    limits: WorkLimits = WorkLimits()
    expanded: int = 0
    rows: int = 0
    queries: int = 0
    #: The first limit reached, if any: ``expansions``, ``rows`` or ``queries``.
    exhausted: str | None = None

    @property
    def rows_left(self) -> int:
        return max(0, self.limits.rows - self.rows)

    def _spent(self, limit: str) -> bool:
        if self.exhausted is None:
            self.exhausted = limit
        return False

    def query(self) -> bool:
        """Charge one query, if the budget allows another."""
        if self.queries >= self.limits.queries:
            return self._spent("queries")
        self.queries += 1
        return True

    def expand(self) -> bool:
        """Charge one node expansion, if the budget allows another."""
        if self.expanded >= self.limits.expansions:
            return self._spent("expansions")
        self.expanded += 1
        return True

    def charge_rows(self, count: int) -> None:
        self.rows += count
        if self.rows >= self.limits.rows:
            self._spent("rows")


class Neighbourhood:
    """The bounded queries of :class:`~repoatlas.storage.protocols.KnowledgeStore`, charged."""

    def __init__(self, knowledge: KnowledgeStore, work: Work) -> None:
        self._knowledge = knowledge
        self.work = work

    def counts(
        self, near_ids: Sequence[int], rule: NeighborRule, confinement: Confinement
    ) -> dict[int, NeighborCount] | None:
        """Exact counts per node; ``None`` when the budget allows no further query."""
        if not near_ids:
            return {}
        if not self.work.query():
            return None
        found = self._knowledge.impact_counts(near_ids, rule, confinement)
        self.work.charge_rows(len(found))
        return found

    def page(
        self,
        near_id: int,
        rule: NeighborRule,
        confinement: Confinement,
        *,
        after: NeighborCursor | None = None,
        limit: int,
    ) -> list[NeighborRow]:
        """At most ``limit`` rows after ``after``, and never more than the budget has left."""
        allowed = min(limit, self.work.rows_left)
        if allowed <= 0:
            self.work.charge_rows(0)
            return []
        if not self.work.query():
            return []
        rows = self._knowledge.impact_page(near_id, rule, confinement, after=after, limit=allowed)
        self.work.charge_rows(len(rows))
        return rows

    def distribution(
        self, near_id: int, rule: NeighborRule, confinement: Confinement, *, max_groups: int
    ) -> tuple[list[NeighborGroup], bool]:
        """Far entities per file, largest first; ``True`` when more groups exist than shown."""
        allowed = min(max_groups, self.work.rows_left)
        if allowed <= 0 or not self.work.query():
            return [], True
        groups, partial = self._knowledge.impact_distribution(
            near_id, rule, confinement, max_groups=allowed
        )
        self.work.charge_rows(len(groups))
        return groups, partial

    def rows_for(
        self,
        near_id: int,
        rule: NeighborRule,
        confinement: Confinement,
        *,
        groups: Sequence[tuple[int | None, str | None]],
        per_group: int,
    ) -> list[NeighborRow]:
        """The first ``per_group`` rows reaching each named file, within what the budget allows."""
        if per_group <= 0 or not groups:
            return []
        fitting = list(groups[: self.work.rows_left // per_group])
        if not fitting or not self.work.query():
            self.work.charge_rows(0)
            return []
        rows = self._knowledge.impact_rows_for(
            near_id, rule, confinement, groups=fitting, per_group=per_group
        )
        self.work.charge_rows(len(rows))
        return rows

    def members(
        self,
        prefix: str,
        *,
        exclude_types: Sequence[EntityType] = (),
        exclude_kinds: Sequence[str] = (),
        limit: int,
    ) -> list[EntityIdentity]:
        """Entities whose key extends ``prefix``: a declaration's or a file's members."""
        allowed = min(limit, self.work.rows_left)
        if allowed <= 0 or not self.work.query():
            self.work.charge_rows(0)
            return []
        found = self._knowledge.entities_with_key_prefix(
            prefix, exclude_types=exclude_types, exclude_kinds=exclude_kinds, limit=allowed
        )
        self.work.charge_rows(len(found))
        return found
