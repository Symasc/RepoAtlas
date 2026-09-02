"""``repoatlas init`` (V1_SPEC §5)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import save_settings
from repoatlas.discovery.finder import discover_repositories
from repoatlas.errors import RepoAtlasError
from repoatlas.models.reports import SyncReport
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace


def initialize(
    workspace: Workspace, root: Path, *, config_path: Path | None = None
) -> tuple[SyncReport, int]:
    """Discover every repository beneath ``root``, record it, and index it.

    The user never registers repositories by hand: discovery is the only path by
    which a repository becomes managed (V1_SPEC §5).
    """
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise RepoAtlasError(f"Not a directory: {root}")

    candidates = discover_repositories(resolved, workspace.settings.discovery)

    settings = workspace.settings.model_copy(
        update={"repos": workspace.settings.repos.model_copy(update={"root": resolved})}
    )
    workspace.settings = settings
    save_settings(settings, config_path)

    engine = workspace.sync_engine()
    reports = []
    for candidate in candidates:
        workspace.repositories.upsert(Repository(name=candidate.name, path=candidate.path))
        stored = workspace.repositories.get_by_path(candidate.path)
        assert stored is not None
        engine.ensure_repository_entity(stored)
        reports.append(engine.sync_repository(stored))

    return SyncReport(repositories=tuple(reports), scope="global"), len(candidates)
