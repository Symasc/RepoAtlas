"""Gathering the facts index freshness is decided from (V1_SPEC §12, §16).

:mod:`repoatlas.freshness` decides; this module pays. The split exists because
the decision is pure and cheap to test while the gathering is neither, and
because what it costs depends entirely on how hard the caller asked RepoAtlas to
look.

Three tiers, and the difference between them is the whole point:

============  ===========================================================
``STORED``    one indexed database read per repository. **No git
              invocation, no filesystem walk.** Safe to call on every
              request.
``LIVE``      the above plus ``git rev-parse`` and a dirty check per
              repository in scope.
drift         plus a full discovery walk of the managed root. Opt-in
              separately from depth, because it is the only part whose
              cost grows with the tree rather than with the scope.
============  ===========================================================

A read that quietly invoked git per repository would make the cheap question and
the expensive one indistinguishable from the outside, which is how a capability
that looks free ends up running a command per call.
"""

from __future__ import annotations

from collections.abc import Sequence

from repoatlas.discovery.reconcile import plan_repository_set
from repoatlas.freshness import index_freshness, repository_freshness
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness, RepositoryFreshness
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version


def current_facts_version(workspace: Workspace) -> str:
    """The facts version stored rows are compared against.

    A free function rather than a sync-engine method so that a read-only question
    never constructs an engine — which would build the embedding backend, and
    possibly download a model, to answer it (V1_SPEC §12).
    """
    return facts_version(workspace.registry, workspace.dependency_registry)


def repository_freshness_for(
    workspace: Workspace,
    repo: Repository,
    *,
    depth: FreshnessDepth,
    facts: str | None = None,
) -> RepositoryFreshness:
    """Freshness of one repository at the requested depth.

    ``facts`` is threaded through so a caller looping over repositories computes
    the facts version once. It is derived from the parser and dependency
    registries and cannot differ between two repositories in the same process.
    """
    version = facts if facts is not None else current_facts_version(workspace)
    outdated = (
        workspace.files.count_outdated(repo.id, version)
        if repo.id is not None
        # No row id means no indexed files to be outdated. Counting would query
        # for a repository that does not exist.
        else 0
    )
    live = workspace.git.state(repo.path) if depth is FreshnessDepth.LIVE else None
    return repository_freshness(repo, outdated_files=outdated, live=live)


def index_freshness_for(
    workspace: Workspace,
    repositories: Sequence[Repository],
    *,
    depth: FreshnessDepth,
    include_repository_set_drift: bool = False,
) -> IndexFreshness:
    """Freshness across a scope, at one depth, with drift detection opt-in.

    Drift is a claim about the managed root rather than about any repository in
    it: a directory the root holds and the index does not is exactly the case
    where "every repository is current" is true and "the index is current" is not
    (V1_SPEC §12). It costs a discovery walk, so it is asked for rather than
    assumed, and ``repository_set_drifted`` stays ``None`` when nobody asked.
    """
    version = current_facts_version(workspace)
    per_repo = [
        repository_freshness_for(workspace, repo, depth=depth, facts=version)
        for repo in repositories
    ]

    drifted: bool | None = None
    if include_repository_set_drift and workspace.root is not None:
        plan = plan_repository_set(
            workspace.root,
            workspace.settings.discovery,
            workspace.repositories.list_all(),
        )
        drifted = plan.drifted

    return index_freshness(per_repo, depth=depth, repository_set_drifted=drifted)
