"""``repoatlas status`` and ``repoatlas repos`` (V1_SPEC §16)."""

from __future__ import annotations

from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.reports import RepositoryStatus, StatusReport
from repoatlas.models.repository import Repository, SyncStatus
from repoatlas.services.workspace import Workspace


def _index_state(repo: Repository, workspace: Workspace) -> str:
    """Report freshness honestly (V1_SPEC §16).

    Never claim the index is current when RepoAtlas can cheaply tell it is not.
    """
    if repo.last_indexed_at is None:
        return "not indexed"
    if repo.last_sync_status is not SyncStatus.OK:
        return "interrupted"

    live = workspace.git.state(repo.path)
    if not live.known:
        return "unknown"
    if live.head_sha != repo.last_indexed_head:
        return "stale (HEAD changed)"
    if repo.last_indexed_dirty is not None and live.is_dirty != repo.last_indexed_dirty:
        return "stale (working tree changed)"
    if live.is_dirty:
        return "current (dirty working tree)"
    return "current"


def status(workspace: Workspace, context: RepositoryContext) -> StatusReport:
    root = workspace.root
    assert root is not None

    per_repo: list[RepositoryStatus] = []
    for repo in context.repositories:
        repo_ids = [repo.id] if repo.id is not None else []
        per_repo.append(
            RepositoryStatus(
                name=repo.name,
                slug=repo.slug,
                path=repo.path,
                branch=repo.branch,
                head_sha=repo.head_sha,
                is_dirty=repo.is_dirty,
                languages=repo.languages,
                indexed_files=workspace.files.count(repo_ids),
                symbols=workspace.knowledge.count_entities(repo_ids),
                relationships=workspace.knowledge.count_edges(repo_ids),
                last_sync=repo.last_indexed_at,
                index_state=_index_state(repo, workspace),
            )
        )

    all_ids = list(context.repository_ids)
    last_syncs = [r.last_indexed_at for r in context.repositories if r.last_indexed_at]
    states = {s.index_state for s in per_repo}

    if not states:
        overall = "not indexed"
    elif states == {"current"}:
        overall = "current"
    elif any(s.startswith("stale") for s in states):
        overall = "stale"
    else:
        overall = sorted(states)[0]

    return StatusReport(
        root=root,
        scope=str(context.scope),
        repositories=tuple(per_repo),
        repository_count=len(per_repo)
        if context.scope is ContextScope.REPOSITORY
        else len(workspace.repositories.list_all()),
        indexed_files=workspace.files.count(all_ids),
        symbols=workspace.knowledge.count_entities(all_ids),
        relationships=workspace.knowledge.count_edges(all_ids),
        last_sync=max(last_syncs) if last_syncs else None,
        index_state=overall,
    )


def list_repositories(workspace: Workspace) -> list[Repository]:
    """``repos`` is always global (V1_SPEC §4)."""
    return workspace.repositories.list_all()
