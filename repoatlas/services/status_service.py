"""``repoatlas status`` and ``repoatlas repos`` (V1_SPEC §16)."""

from __future__ import annotations

from repoatlas.discovery.reconcile import RepositorySetPlan, plan_repository_set
from repoatlas.freshness import legacy_index_state
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.models.reports import RepositoryStatus, StatusReport
from repoatlas.models.repository import Repository
from repoatlas.services.freshness_service import current_facts_version, repository_freshness_for
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


def _drift(workspace: Workspace, context: RepositoryContext) -> RepositorySetPlan | None:
    """Re-derive what the managed root contains. **Reads only.**

    ``status`` is a read command, so it detects and never reconciles: it says the
    workspace is not current and names why, and ``sync`` is what changes anything.
    Registering a repository here would make "check whether anything changed" a
    mutating operation, indistinguishable in a terminal from having run a sync
    (V1_SPEC §4).

    Run at **every** scope, unlike reconciliation. ``sync`` only reconciles
    membership globally, because registering a repository during
    ``repoatlas sync --repo foo`` would touch a row outside the requested scope
    and leave it registered without facts. Detection has no such constraint: it
    writes nothing, so a repository-scoped status can say "this repository is
    current, and the workspace is missing two" without overstepping. Reporting it
    only from the root would leave anyone who works inside their repositories
    exactly as uninformed as before.
    """
    if workspace.root is None:
        return None
    return plan_repository_set(
        workspace.root, workspace.settings.discovery, workspace.repositories.list_all()
    )


def status(workspace: Workspace, context: RepositoryContext) -> StatusReport:
    root = workspace.root
    assert root is not None

    drift = _drift(workspace, context)

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
    return workspace.repositories.list_all()
