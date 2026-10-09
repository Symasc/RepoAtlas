"""``repoatlas status`` and ``repoatlas repos`` (V1_SPEC §16)."""

from __future__ import annotations

from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.discovery.reconcile import declarations_ignored
from repoatlas.errors import ContextResolutionError
from repoatlas.freshness import legacy_index_state
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.models.reports import (
    DecisionCount,
    DecisionEntry,
    DecisionsReport,
    RepositoryStatus,
    StatusReport,
)
from repoatlas.models.repository import Repository
from repoatlas.services.freshness_service import (
    current_facts_version,
    repository_freshness_for,
    repository_set_drift,
)
from repoatlas.services.workspace import Workspace


def _index_state(repo: Repository, workspace: Workspace, facts: str | None = None) -> str:
    """Report freshness honestly (V1_SPEC §16).

    Never claim the index is current when RepoAtlas can cheaply tell it is not.

    The decision moved to :mod:`repoatlas.freshness`, which the capability layer
    needs and may not reach through a service. What is left here is the V1
    rendering: ``index_state`` is a public string, printed in the status table and
    emitted in ``--json``, and ``status`` derives its aggregate verdict by reading
    these very strings. It is pinned character for character by
    ``tests/capabilities/test_v1_status_compatibility.py``.

    ``status`` always resolves at ``LIVE`` depth, exactly as before: V1 asks git
    about every repository it reports on.
    """
    freshness = repository_freshness_for(workspace, repo, depth=FreshnessDepth.LIVE, facts=facts)
    return legacy_index_state(freshness)


def status(workspace: Workspace, context: RepositoryContext) -> StatusReport:
    root = workspace.root
    assert root is not None

    # Detected at every scope, unlike reconciliation: detection writes nothing, so a
    # repository-scoped status can say "this repository is current, and the
    # workspace is missing two" without overstepping.
    drift = repository_set_drift(workspace)

    # Computed once for the whole report: it is derived from the parser and
    # dependency registries, so it cannot differ between two repositories of one
    # run, and re-deriving it per repository was pure repetition.
    facts = current_facts_version(workspace)

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
                index_state=_index_state(repo, workspace, facts),
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

    # Repository-set drift outranks every per-repository verdict. Each registered
    # repository can be perfectly current and the workspace still not be: a
    # repository the managed root holds and the index does not is exactly the case
    # where "current" was a false statement, and it was the *only* thing status
    # used to report (V1_SPEC §12).
    if drift is not None and drift.drifted:
        overall = "stale"

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
        new_repositories=tuple(c.slug for c in drift.added) if drift else (),
        missing_repositories=tuple(r.slug or r.name for r in drift.missing) if drift else (),
        conflicting_repositories=tuple(c.candidate.slug for c in drift.conflicts) if drift else (),
        unreadable_paths=drift.unreadable if drift else (),
        declarations_ignored=declarations_ignored(),
        index_state=overall,
    )


def list_repositories(workspace: Workspace) -> list[Repository]:
    """``repos`` is always global (V1_SPEC §4).

    The **registered** set, deliberately. ``repos`` lists what RepoAtlas manages —
    what has an identity, a slug and facts that evidence can be attributed to — and
    a discovered-but-unregistered directory has none of those yet. Listing it here
    would offer a name that ``--repo`` cannot select and ``locate`` cannot answer
    about. Drift is reported where it is actionable: ``status`` names the
    unregistered repositories, and a global ``sync`` registers them.
    """
    # The operator declarations source is the workspace's configuration, not one of its
    # repositories: `status` shows it, a listing does not (DF1, D1-1).
    return [repo for repo in workspace.repositories.list_all() if repo.slug != DECLARATIONS_SLUG]


#: The most decisions one ``repoatlas decisions`` page lists.
MAX_DECISIONS_PAGE = 500


def list_decisions(
    workspace: Workspace,
    *,
    family: str | None = None,
    decision: str | None = None,
    repository: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DecisionsReport:
    """The stored link decisions the filters select: their total, their counts by family
    and outcome, and one page of them in a fixed order (repository, path, line, family,
    subject, ref). Reads only; every decision stays countable however many there are."""
    slugs = {
        repo.id: repo.slug for repo in workspace.repositories.list_all() if repo.id is not None
    }
    repo_id = None
    if repository is not None:
        repo_id = next((rid for rid, slug in slugs.items() if slug == repository), None)
        if repo_id is None:
            raise ContextResolutionError(
                f"no managed repository is {repository!r}",
                detail="`repoatlas repos` lists the managed repositories",
            )
    rows = [
        row
        for row in workspace.knowledge.list_link_decisions(family=family, repo_id=repo_id)
        if decision is None or row.decision == decision
    ]
    rows.sort(
        key=lambda r: (
            slugs.get(r.repo_id, ""),
            r.path or "",
            r.line_start or 0,
            r.family,
            r.subject,
            r.decision_ref,
        )
    )
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        counts[(row.family, row.decision)] = counts.get((row.family, row.decision), 0) + 1
    limit = max(1, min(limit, MAX_DECISIONS_PAGE))
    offset = max(0, offset)
    page = rows[offset : offset + limit]
    return DecisionsReport(
        family=family,
        decision=decision,
        repository=repository,
        total=len(rows),
        counts=tuple(
            DecisionCount(family=f, decision=d, count=n) for (f, d), n in sorted(counts.items())
        ),
        offset=offset,
        limit=limit,
        decisions=tuple(
            DecisionEntry(
                repository=slugs.get(row.repo_id, ""),
                path=row.path or "",
                line=row.line_start,
                family=row.family,
                decision=row.decision,
                subject=row.subject,
                decision_ref=row.decision_ref,
            )
            for row in page
        ),
        remaining=max(0, len(rows) - offset - len(page)),
    )
