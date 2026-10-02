"""Sync planning (V1_SPEC §12, ARCHITECTURE.md §9).

**Correctness dominates the optimization.** The content hash is the sole authority
on whether a file changed. The stat check only ever permits *skipping* the hash,
and that shortcut is disabled entirely whenever anything makes stat untrustworthy.

Stated positively: we may skip work only when we can prove nothing changed.
Whenever proof is unavailable, we do the work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.gitstate.client import GitRepositoryState
from repoatlas.indexing.chunking import CHUNKING_VERSION
from repoatlas.models.repository import FileState, Repository, SyncStatus
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.sync.scanner import ScannedFile


def facts_version(registry: ParserRegistry, dependency_registry: DependencyRegistry) -> str:
    """Version of everything that turns a file into facts (V1_SPEC §12).

    The schema version, both registries and the chunking rules contribute, because
    a change to any of them makes stored facts stale even though the file on disk
    is untouched. It is stored per file, so a version change reparses the affected
    files directly instead of hashing the whole tree to learn that nothing moved.

    A free function rather than a method on the engine so that ``status`` can
    compare an index against it without constructing a sync engine.
    """
    return (
        f"schema:{SCHEMA_VERSION}"
        f"||{registry.version}"
        f"||deps:{dependency_registry.version}"
        f"||chunks:{CHUNKING_VERSION}"
    )


@dataclass(frozen=True, slots=True)
class RehashDecision:
    """Whether the stat shortcut may be used, and why not when it may not."""

    forced: bool
    reason: str | None = None


@dataclass(slots=True)
class SyncPlan:
    """What a sync run must do."""

    unchanged: list[str] = field(default_factory=list)
    changed: list[ScannedFile] = field(default_factory=list)
    new: list[ScannedFile] = field(default_factory=list)
    deleted_file_ids: list[int] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)
    rehash: RehashDecision = field(default_factory=lambda: RehashDecision(forced=False))

    @property
    def touched(self) -> bool:
        return bool(self.changed or self.new or self.deleted_file_ids)


def decide_rehash(
    repo: Repository,
    git_state: GitRepositoryState,
    *,
    force: bool = False,
) -> RehashDecision:
    """Decide whether every file must be hashed (V1_SPEC §12).

    Each trigger corresponds to a way stat can lie about content having changed.

    A parser-version change is deliberately *not* a trigger here: it is handled
    per file in :func:`plan_sync`, which reparses the affected files directly
    rather than hashing every file to discover that none of them changed.
    """
    if force:
        return RehashDecision(forced=True, reason="--full requested")

    if repo.last_sync_status is SyncStatus.NEVER or repo.last_indexed_at is None:
        return RehashDecision(forced=True, reason="first index")

    if repo.last_sync_status is not SyncStatus.OK:
        return RehashDecision(
            forced=True, reason=f"previous sync did not complete ({repo.last_sync_status})"
        )

    # checkout / switch / pull / rebase / reset rewrite files with arbitrary mtimes
    # and can restore a file to a previous size, so stat proves nothing here.
    if git_state.known and git_state.head_sha != repo.last_indexed_head:
        return RehashDecision(forced=True, reason="HEAD changed since last index")

    if repo.last_indexed_dirty is None or git_state.is_dirty != repo.last_indexed_dirty:
        return RehashDecision(forced=True, reason="working-tree dirty state changed")

    if not git_state.known:
        return RehashDecision(forced=True, reason="git state unknown")

    return RehashDecision(forced=False)


def plan_sync(
    scanned: list[ScannedFile],
    known: dict[str, FileState],
    *,
    rehash: RehashDecision,
    parser_version: str,
    hash_of: Callable[[str], str | None],
) -> SyncPlan:
    """Classify every file as unchanged, changed, new or deleted.

    ``hash_of`` is injected so planning stays testable without a filesystem.
    """
    plan = SyncPlan(rehash=rehash)
    seen: set[str] = set()

    for scanned_file in scanned:
        seen.add(scanned_file.rel_path)
        previous = known.get(scanned_file.rel_path)

        if previous is None:
            plan.new.append(scanned_file)
            continue

        # A parser-version change makes stored facts stale by definition, even
        # though the file itself is untouched.
        if previous.parser_version != parser_version:
            plan.changed.append(scanned_file)
            continue

        stat_matches = (
            previous.size == scanned_file.size and previous.mtime_ns == scanned_file.mtime_ns
        )
        if stat_matches and not rehash.forced:
            plan.unchanged.append(scanned_file.rel_path)
            continue

        # The hash decides. A coarse-mtime filesystem costs a rehash here, never a
        # missed update. An unreadable file hashes to None, which never equals a
        # stored hash, so it is treated as changed and the indexer reports why.
        digest = hash_of(scanned_file.rel_path)
        if digest is not None and digest == previous.content_hash:
            plan.unchanged.append(scanned_file.rel_path)
        else:
            plan.changed.append(scanned_file)

    for rel_path, state in known.items():
        if rel_path not in seen:
            plan.deleted_file_ids.append(state.file_id)
            plan.deleted_paths.append(rel_path)

    return plan
