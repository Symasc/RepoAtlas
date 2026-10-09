# ruff: noqa: E501 - the sample configurations hold their lines as a CI system or Helm reads them
"""What the global pass decides about CI configurations, on indexed invented repositories
(DF1_PHASE_A.md §6, §9, §10, §12): the four decision families, the operator declarations
source, the decisions listing, and an incremental sync equal to a fresh index."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from repoatlas.config.settings import DECLARATIONS_SLUG, Settings
from repoatlas.discovery.reconcile import declarations_candidate, declarations_ignored
from repoatlas.graph.ci_facts import CI_BUILD, CI_RELEASE, CI_SEMANTICS_FAMILY, CI_STEP
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest
from repoatlas.services.status_service import list_decisions, list_repositories
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, init_git_repository, reindex_from_scratch
from tests.impact_corpus import build

DEPLOY_WORKFLOW = """
name: deploy
on:
  push:
    branches: [main]
jobs:
  ship:
    steps:
      - uses: actions/checkout@v4
      - run: helm upgrade --install api charts/api -f charts/api/values.yaml -f environments/prod.yaml -n prod --set image.tag=${{ github.sha }}
      - run: helm upgrade --install other remote/chart
      - run: helm upgrade --install canary charts/api -f environments/${{ vars.ENV }}/x.yaml
      - run: helm upgrade --install escape charts/api -f ../outside.yaml
      - uses: example-org/notify@v1
"""

CHART = {
    "charts/api/Chart.yaml": "apiVersion: v2\nname: api\nversion: 1.0.0\n",
    "charts/api/values.yaml": "image:\n  tag: ''\n",
    "environments/prod.yaml": "ingress:\n  host: api.example\n",
    "environments/values-staging.yaml": "ingress:\n  host: api.staging.example\n",
}

BUILDER_CIRCLE = """
version: 2.1
orbs:
  deploy: sample-org/helm-deploy@1.0.0
jobs:
  build:
    steps:
      - checkout
      - run: docker build -t registry.example:5000/team/api:$CIRCLE_SHA1 .
      - run: docker push registry.example:5000/team/api:$CIRCLE_SHA1
workflows:
  release:
    jobs:
      - build
      - deploy/release:
          requires: [build]
          chart: charts/api
          release-name: $CIRCLE_PROJECT_REPONAME
          overrides: stage=$CIRCLE_BRANCH,image.tag=$CIRCLE_SHA1
          filters: {branches: {only: [main]}}
"""

DECLARATION = """
repoatlas: ci-semantics/v1
orbs:
  - orb: sample-org/helm-deploy@1.0.0
    job: release
    helm-release:
      chart: {parameter: chart}
      release: {parameter: release-name}
      set: {parameter: overrides, format: comma-separated}
projects:
  builder: {circleci-project: builder}
"""


def decided(workspace: Workspace, family: str) -> list[tuple[str, str]]:
    return sorted(
        (row.path or "", row.decision)
        for row in workspace.knowledge.list_link_decisions(family=family)
    )


def evidence(workspace: Workspace, family: str, decision: str) -> list[dict[str, Any]]:
    return [
        json.loads(row.evidence_json)
        for row in workspace.knowledge.list_link_decisions(family=family)
        if row.decision == decision
    ]


@pytest.fixture
def declarations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "declarations"
    monkeypatch.setenv("REPOATLAS_DECLARATIONS", str(directory))
    return directory


def write_declarations(directory: Path, files: dict[str, str], *, git: bool = True) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(dedent(text), encoding="utf-8")
    if git:
        init_git_repository(directory)


def repos() -> dict[str, dict[str, str]]:
    return {
        "deployer": {**CHART, ".github/workflows/deploy.yml": dedent(DEPLOY_WORKFLOW)},
        "builder": {
            **{k.replace("api", "api"): v for k, v in CHART.items() if k.startswith("charts/")},
            ".circleci/config.yml": dedent(BUILDER_CIRCLE),
            "Dockerfile": "FROM python:3.12-slim\n",
        },
    }


def test_releases_are_decided_by_chart_and_values_files(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    build(workspace, tmp_path, repos())
    assert decided(workspace, CI_RELEASE) == [
        (".github/workflows/deploy.yml", "bound"),
        (".github/workflows/deploy.yml", "chart-external"),
        (".github/workflows/deploy.yml", "values-unresolved"),
        (".github/workflows/deploy.yml", "values-unresolved"),
    ]
    [bound] = evidence(workspace, CI_RELEASE, "bound")
    # an overlay outside the values* naming is recorded as referenced, for DF2 to evaluate
    assert bound["values"] == [
        ["charts/api/values.yaml", "discovered"],
        ["environments/prod.yaml", "referenced"],
    ]
    assert bound["basis"] == "source" and bound["domains"] == {"branches": ["main"]}


def test_without_a_declaration_a_private_orb_is_unsupported_and_binds_nothing(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    build(workspace, tmp_path, repos())
    steps = decided(workspace, CI_STEP)
    assert (".circleci/config.yml", "unsupported-action") in steps
    assert (".github/workflows/deploy.yml", "unsupported-action") in steps
    assert not [
        r
        for r in workspace.knowledge.list_link_decisions(family=CI_RELEASE)
        if r.path == ".circleci/config.yml"
    ]
    assert decided(workspace, CI_BUILD) == [(".circleci/config.yml", "publish-configured")]


def test_an_accepted_declaration_binds_the_orb_job_as_declared(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    write_declarations(declarations, {"orbs.yml": DECLARATION})
    build(workspace, tmp_path, repos())
    [release] = [
        json.loads(r.evidence_json)
        for r in workspace.knowledge.list_link_decisions(family=CI_RELEASE)
        if r.path == ".circleci/config.yml"
    ]
    assert release["basis"] == "declared"
    assert release["declared_in"].startswith(f"{DECLARATIONS_SLUG}/orbs.yml:")
    assert release["release"] == "builder" and release["release_resolved"]
    assert ["stage", "<var:CIRCLE_BRANCH>"] in release["set"]
    assert decided(workspace, CI_SEMANTICS_FAMILY) == [
        ("orbs.yml", "accepted"),
        ("orbs.yml", "accepted"),
    ]
    assert not [
        r
        for r in workspace.knowledge.list_link_decisions(family=CI_STEP)
        if r.path == ".circleci/config.yml"
    ]


def test_conflicting_invalid_and_unused_declarations_bind_nothing(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    other = DECLARATION.replace("projects:\n  builder: {circleci-project: builder}\n", "")
    write_declarations(
        declarations,
        {
            "orbs.yml": other,
            "again.yml": other,
            "bad.yml": "repoatlas: ci-semantics/v1\norbs:\n  - orb: sample-org/helm-deploy@1\n    job: x\n    helm-release: {chart: {parameter: c}}\n",
            "spare.yml": "repoatlas: ci-semantics/v1\nprojects:\n  nobody: {circleci-project: nobody}\n",
        },
    )
    build(workspace, tmp_path, repos())
    assert decided(workspace, CI_SEMANTICS_FAMILY) == [
        ("again.yml", "conflict"),
        ("bad.yml", "invalid"),
        ("orbs.yml", "conflict"),
        ("spare.yml", "unused"),
    ]
    assert (".circleci/config.yml", "unsupported-action") in decided(workspace, CI_STEP)


def test_a_declaration_inside_an_ordinary_repository_is_ignored(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    trees = repos()
    trees["builder"][".repoatlas/ci-semantics.yml"] = dedent(DECLARATION)
    build(workspace, tmp_path, trees)
    assert decided(workspace, CI_SEMANTICS_FAMILY) == [
        (".repoatlas/ci-semantics.yml", "outside-declarations"),
        (".repoatlas/ci-semantics.yml", "outside-declarations"),
    ]
    assert (".circleci/config.yml", "unsupported-action") in decided(workspace, CI_STEP)


def test_the_declarations_source_is_reserved_listed_only_by_status_and_never_initialised(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    write_declarations(declarations, {"orbs.yml": DECLARATION}, git=False)
    assert declarations_candidate() is None and declarations_ignored() == declarations
    assert not (declarations / ".git").exists()
    init_git_repository(declarations)
    candidate = declarations_candidate()
    assert candidate is not None and candidate.slug == DECLARATIONS_SLUG
    build(workspace, tmp_path, repos())
    registered = {repo.slug for repo in workspace.repositories.list_all()}
    assert DECLARATIONS_SLUG in registered
    assert DECLARATIONS_SLUG not in {repo.slug for repo in list_repositories(workspace)}
    from repoatlas.capabilities.service import CapabilityService

    listed = CapabilityService(workspace).repositories(
        RepositoriesRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL))
    )
    assert DECLARATIONS_SLUG not in {view.slug for view in listed.repositories}


def test_the_decisions_listing_counts_everything_and_pages_deterministically(
    workspace: Workspace, tmp_path: Path, declarations: Path
) -> None:
    build(workspace, tmp_path, repos())
    whole = list_decisions(workspace)
    first = list_decisions(workspace, limit=2)
    second = list_decisions(workspace, limit=2, offset=2)
    assert whole.total == first.total == second.total == len(whole.decisions)
    assert [d.decision_ref for d in (*first.decisions, *second.decisions)] == [
        d.decision_ref for d in whole.decisions[:4]
    ]
    assert first.remaining == whole.total - 2
    steps = list_decisions(
        workspace, family=CI_STEP, decision="unsupported-action", repository="deployer"
    )
    assert steps.total == 1 and steps.decisions[0].path == ".github/workflows/deploy.yml"
    assert sum(count.count for count in whole.counts) == whole.total


def test_an_incremental_sync_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path, declarations: Path
) -> None:
    write_declarations(declarations, {"orbs.yml": DECLARATION})
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    workflow = root / "deployer/.github/workflows/deploy.yml"
    # edit a workflow, delete an overlay, rename a values file, edit a declaration
    workflow.write_text(
        workflow.read_text().replace("example-org/notify@v1", "example-org/notify@v2")
    )
    (root / "deployer/environments/prod.yaml").unlink()
    shutil.move(
        root / "deployer/environments/values-staging.yaml",
        root / "deployer/environments/values-qa.yaml",
    )
    (declarations / "orbs.yml").write_text(
        dedent(DECLARATION).replace("job: release", "job: other")
    )
    for repo in (root / "deployer", declarations):
        sync(workspace, workspace.resolver().resolve(repo))
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)
    # the declaration no longer matches: the orb job is unsupported again
    assert (".circleci/config.yml", "unsupported-action") in decided(workspace, CI_STEP)
    assert ("orbs.yml", "unused") in decided(workspace, CI_SEMANTICS_FAMILY)
