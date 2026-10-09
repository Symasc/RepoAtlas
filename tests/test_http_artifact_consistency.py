# ruff: noqa: E501 - the sample configurations hold their lines as a CI system reads them
"""One authoritative artifact join, and reads consistent with it (DF2_PHASE_A.md §15, E7).

The join runs once, in the global pass; ``http_operations`` answers crossings from the
decisions that pass stored, never by joining again. Acceptance tests A1-A7:

* A1 only the linker imports the join;
* A2 a consumer's crossings, cross-repository, are its stored decisions exactly;
* A3 under repository scope, the same refs, statuses and reasons, with exactly what
  another repository states withheld;
* A4 after one-file syncs of every kind of input, the crossings are the stored decisions,
  and the stored state is a fresh index's;
* A5 with the join made to fail, reads still answer, from storage;
* A6 each repository's replan is its share of the global plan, and holds no CI-bound
  exposure;
* A7 an unchanged tree synced twice stores identical decisions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph import http_chain
from repoatlas.graph.linker import ReferenceLinker, plan_repository_http
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_inventory import HttpCrossing, HttpOperationsRequest, OperationRole
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.artifact_corpus import WEB_CHART, repos
from tests.conftest import fact_snapshot, init_git_repository
from tests.impact_corpus import build

ORBED = """\
version: 2.1
orbs:
  deploy: sample-org/helm-deploy@1.0.0
workflows:
  release:
    jobs:
      - deploy/release:
          chart: charts/web
          release-name: orbed
          overrides: ingress.host=orbed.test.example,image.tag=$CIRCLE_SHA1
          filters: {branches: {only: [main]}}
"""

DECLARATION = """\
repoatlas: ci-semantics/v1
orbs:
  - orb: sample-org/helm-deploy@1.0.0
    job: release
    helm-release:
      chart: {parameter: chart}
      release: {parameter: release-name}
      set: {parameter: overrides, format: comma-separated}
"""

ORBED_CLIENT = """\
import requests

ORBED_URL = "https://orbed.test.example"


def fetch_orbed(item_id: str) -> dict:
    return requests.get(f"{ORBED_URL}/v1/items/{item_id}", timeout=5).json()
"""


def trees() -> dict[str, dict[str, str]]:
    found = repos()
    found["orbed"] = {**WEB_CHART, ".circleci/config.yml": ORBED}
    found["client"] = {**found["client"], "client/orbed.py": ORBED_CLIENT}
    return found


@pytest.fixture
def declarations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "declarations"
    directory.mkdir()
    (directory / "orbs.yml").write_text(DECLARATION)
    init_git_repository(directory)
    monkeypatch.setenv("REPOATLAS_DECLARATIONS", str(directory))
    return directory


@pytest.fixture
def corpus(workspace: Workspace, tmp_path: Path, declarations: Path) -> Workspace:
    return build(workspace, tmp_path, trees())


def slugs(workspace: Workspace) -> dict[int, str]:
    return {repo.id: repo.slug for repo in workspace.repositories.list_all() if repo.id is not None}


def repo_id(workspace: Workspace, slug: str) -> int:
    return next(k for k, v in slugs(workspace).items() if v == slug)


def crossings(workspace: Workspace, *, shown: bool) -> dict[str, HttpCrossing]:
    service = CapabilityService(workspace)
    found: dict[str, HttpCrossing] = {}
    cursor = None
    while True:
        response = service.http_operations(
            HttpOperationsRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="client"),
                role=OperationRole.CALLER,
                cross_repository=shown,
                limit=200,
                cursor=cursor,
            )
        )
        for row in response.operations:
            for crossing in row.crossings:
                assert crossing.decision_ref not in found, "each stored decision is listed once"
                found[crossing.decision_ref] = crossing
        if not response.has_more:
            return found
        cursor = response.next_cursor


def stored(workspace: Workspace) -> dict[str, Any]:
    return {
        row.decision_ref: row
        for row in workspace.knowledge.list_http_call_decisions(repo_id(workspace, "client"))
    }


def stored_chain(row: Any, names: dict[int, str]) -> list[tuple[object, ...]]:
    return [
        (
            hop.kind,
            hop.status,
            hop.reason,
            hop.detail,
            tuple(
                (names[p.repo_id], p.path, p.line_start, p.line_end, p.role, p.basis or None)
                for p in hop.provenance
            ),
        )
        for hop in http_chain.chain_from_json(row.chain_json)
    ]


def shown_chain(crossing: HttpCrossing) -> list[tuple[object, ...]]:
    return [
        (
            hop.kind,
            hop.status,
            hop.reason,
            hop.detail,
            tuple(
                (p.repository, p.path, p.line_start, p.line_end, p.role, p.basis)
                for p in hop.provenance
            ),
        )
        for hop in crossing.chain
    ]


def assert_consistent(workspace: Workspace) -> None:
    """A2: every stored decision, and nothing else, exactly as stored."""
    rows = stored(workspace)
    shown = crossings(workspace, shown=True)
    assert set(shown) == set(rows)
    names = slugs(workspace)
    for ref, row in rows.items():
        crossing = shown[ref]
        assert (crossing.status, crossing.method, crossing.url, crossing.variant) == (
            row.decision,
            row.method,
            row.url,
            row.variant,
        )
        assert shown_chain(crossing) == stored_chain(row, names)
        evidence = json.loads(row.evidence_json)
        if "binding_basis" in evidence:
            assert crossing.artifact is not None
            for key, value in crossing.artifact.model_dump(exclude={"withheld"}).items():
                assert value == evidence.get(key), key
        else:
            assert crossing.artifact is None


def by_function(workspace: Workspace) -> dict[str, Any]:
    named = {}
    for row in stored(workspace).values():
        name = workspace.database.execute(
            "SELECT qualified_name FROM entities WHERE id = ?", (row.src_id,)
        ).fetchone()[0]
        named[str(name)] = row
    return named


def test_a1_only_the_linker_imports_the_join() -> None:
    root = Path(__file__).resolve().parent.parent / "repoatlas"
    importers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(
            r"^\s*(from|import) repoatlas\.graph\.deploy_artifacts\b",
            path.read_text(),
            re.MULTILINE,
        )
    )
    assert importers == ["graph/linker.py"]


def test_a2_cross_repository_crossings_are_the_stored_decisions_exactly(corpus: Workspace) -> None:
    named = by_function(corpus)
    assert named["fetch_item"].decision == "linked"
    assert named["fetch_orbed"].decision == "linked"  # a declared release
    orbed = json.loads(named["fetch_orbed"].evidence_json)
    assert (orbed["binding_basis"], orbed["build_basis"]) == ("declared", "source")
    assert_consistent(corpus)


def test_a3_repository_scope_withholds_exactly_what_another_repository_states(
    corpus: Workspace,
) -> None:
    rows = stored(corpus)
    full = crossings(corpus, shown=True)
    scoped = crossings(corpus, shown=False)
    assert set(scoped) == set(rows)
    for ref, crossing in scoped.items():
        assert crossing.status == full[ref].status
        assert [(h.kind, h.status, h.reason) for h in crossing.chain] == [
            (h.kind, h.status, h.reason) for h in full[ref].chain
        ]
        assert all(p.repository == "client" for h in crossing.chain for p in h.provenance)
        if full[ref].artifact is not None:
            artifact = crossing.artifact
            assert artifact is not None and artifact.withheld
            assert (
                artifact.deployed_by,
                artifact.built_by,
                artifact.release,
                artifact.variant,
                artifact.image,
            ) == (None,) * 5
            kept = ("binding_basis", "build_basis", "version_relation", "source_rule")
            assert {k: getattr(artifact, k) for k in kept} == {
                k: getattr(full[ref].artifact, k) for k in kept
            }
    declared = next(
        c for c in scoped.values() if c.artifact and c.artifact.binding_basis == "declared"
    )
    assert [h.status for h in declared.chain][3] == "declared"


def full_state(workspace: Workspace) -> tuple[object, ...]:
    def table(sql: str) -> list[tuple[object, ...]]:
        return [tuple(r) for r in workspace.database.execute(sql).fetchall()]

    return (
        fact_snapshot(workspace),
        table(
            "SELECT decision_ref, decision, chain_json, evidence_json FROM http_call_decisions ORDER BY decision_ref"
        ),
        table(
            "SELECT family, subject, decision, evidence_json FROM link_decisions ORDER BY family, subject"
        ),
        table(
            "SELECT f.rel_path, u.meta_json FROM unresolved_refs u JOIN files f ON f.id = u.file_id WHERE u.target_key = 'helm-values:referenced' ORDER BY 1"
        ),
    )


def test_a4_one_file_syncs_keep_reads_stored_and_stored_fresh(
    corpus: Workspace, settings: Settings, tmp_path: Path, declarations: Path
) -> None:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    steps = [
        (
            "overlay host",
            "deployer",
            "env/test/api.yaml",
            lambda t: t.replace("api.test.example", "api9.test.example"),
        ),
        (
            "publisher tag",
            "api",
            ".circleci/config.yml",
            lambda t: t.replace("team/api:$CIRCLE_SHA1", "team/api-moved:$CIRCLE_SHA1"),
        ),
        (
            "referenced path",
            "deployer",
            ".github/workflows/deploy.yml",
            lambda t: t.replace("-f env/test/api.yaml", "-f env/test/values-other.yaml"),
        ),
        ("declaration", None, "orbs.yml", lambda t: t.replace("job: release", "job: other")),
    ]
    for index, (label, repo, path, change) in enumerate(steps):
        target = (declarations if repo is None else root / repo) / path
        target.write_text(change(target.read_text()))
        sync(corpus, corpus.resolver().resolve(declarations if repo is None else root / repo))
        assert_consistent(corpus)
        other = open_workspace(
            settings=settings.model_copy(
                update={
                    "storage": settings.storage.model_copy(
                        update={"database": tmp_path / f"fresh{index}" / "atlas.db"}
                    )
                }
            ),
            require_root=False,
        )
        try:
            initialize(other, root, config_path=tmp_path / f"fresh{index}.yaml")
            assert full_state(corpus) == full_state(other), label
        finally:
            other.close()
    named = by_function(corpus)
    assert named["fetch_item"].decision != "linked"
    assert named["fetch_orbed"].decision != "linked"  # the declaration no longer matches


def test_a5_reads_never_join_again(corpus: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    import repoatlas.graph.deploy_artifacts as artifacts
    import repoatlas.graph.linker as linker

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a read joined again")

    monkeypatch.setattr(artifacts, "plan_deployment_artifacts", refuse)
    monkeypatch.setattr(linker, "plan_deployment_artifacts", refuse)
    assert_consistent(corpus)
    handler = corpus.database.execute(
        "SELECT key FROM entities WHERE qualified_name = 'get_item'"
    ).fetchone()[0]
    answer = CapabilityService(corpus).impact(
        ImpactRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL), seed=handler, max_candidates=50
        )
    )
    reached = [
        c
        for c in answer.candidates
        if c.entity.endswith(":fetch_item:12") or "fetch_item" in c.entity
    ]
    assert reached and reached[0].step.http is not None
    assert reached[0].step.http.deployment_repository == "deployer"
    assert reached[0].step.http.builder_repository == "api"


def test_a6_each_replan_is_its_share_and_holds_no_ci_bound_exposure(corpus: Workspace) -> None:
    whole = ReferenceLinker(corpus.knowledge).prepare_global().http
    plans = [plan_repository_http(corpus.knowledge, rid).plan for rid in slugs(corpus)]

    def http(plan: Any) -> list[tuple[object, ...]]:
        return sorted(
            (e.src_id, str(e.type), e.target.key, e.repo_id, e.file_id, e.line_start, e.line_end)
            for e in plan.http
        )

    assert sorted(row for plan in plans for row in http(plan)) == http(whole)
    for name in ("routes", "roots", "requests", "calls", "promoted", "declared"):
        merged: dict[int, object] = {}
        for plan in plans:
            merged.update(getattr(plan, name))
        assert merged == getattr(whole, name), name
    for plan in (*plans, whole):
        for claim in (*plan.exposed, *plan.identities):
            assert "binding_basis" not in json.loads(claim.meta_json or "{}")


def test_a7_an_unchanged_tree_synced_twice_stores_identical_decisions(
    corpus: Workspace, tmp_path: Path
) -> None:
    before = full_state(corpus)
    for repo in sorted((tmp_path / "repos").iterdir()):
        sync(corpus, corpus.resolver().resolve(repo))
    assert full_state(corpus) == before


def test_the_declared_link_reads_as_declared_in_the_explorer_s_terms(corpus: Workspace) -> None:
    crossing = next(
        c
        for c in crossings(corpus, shown=True).values()
        if c.artifact and c.artifact.binding_basis == "declared"
    )
    binding = crossing.chain[3]
    assert binding.status == "declared"
    assert {p.basis for p in binding.provenance} == {"declared"}
    assert {p.repository for p in binding.provenance} == {"orbed", "@declarations"}
    assert crossing.artifact is not None and crossing.artifact.source_rule == "image"


def test_a_citation_of_a_declared_place_says_it_rests_on_a_declaration(corpus: Workspace) -> None:
    from repoatlas.models.verification import (
        AgentAnswer,
        AgentCitation,
        AgentClaim,
        VerifyGroundingRequest,
    )

    service = CapabilityService(corpus)
    ref = by_function(corpus)["fetch_orbed"].decision_ref
    response = service.http_operations(
        HttpOperationsRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            decision_ref=ref,
            include_provenance=True,
        )
    )
    [crossing] = [c for row in response.operations for c in row.crossings if c.decision_ref == ref]
    declared = next(p for p in crossing.chain[3].provenance if p.repository == "@declarations")
    called = crossing.chain[0].provenance[0]
    assert declared.evidence_id and called.evidence_id

    def claim(evidence_id: str) -> AgentClaim:
        return AgentClaim(
            statement="the request is configured to reach this handler",
            claimed_confidence=0.9,
            citations=(AgentCitation(evidence_id=evidence_id),),
        )

    verdict = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=response.investigation_id,
            answer=AgentAnswer(
                summary="configured",
                claims=(claim(declared.evidence_id), claim(called.evidence_id)),
            ),
        )
    )
    assert [c.basis for c in verdict.citations] == ["declared", None]
