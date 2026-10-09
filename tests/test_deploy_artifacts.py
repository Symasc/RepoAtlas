"""DF2's artifact join on indexed invented repositories (DF2_PHASE_A.md §4-§7, §15).

One workspace (:mod:`tests.artifact_corpus`): a deployment repository releasing two charts
several ways, three repositories whose CI builds images, and a consumer. Every
``container_artifact`` decision, every refusal reason, the cross-repository tie and its
places, and a competing established release (E8) are asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.deploy_artifacts import CONTAINER_ARTIFACT
from repoatlas.services.workspace import Workspace
from tests.artifact_corpus import repos
from tests.impact_corpus import build


def artifacts(workspace: Workspace) -> dict[str, tuple[str, dict[str, Any]]]:
    """Each release's container decision, by release name."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for row in workspace.knowledge.list_link_decisions(family=CONTAINER_ARTIFACT):
        evidence = json.loads(row.evidence_json)
        assert evidence["release"] not in found, "one container per release here"
        found[evidence["release"]] = (row.decision, evidence)
    return found


def calls(workspace: Workspace) -> dict[str, Any]:
    """Each consumer request's stored decision, by the function making it."""
    rows = workspace.knowledge.list_http_call_decisions()
    named: dict[str, Any] = {}
    for row in rows:
        key = workspace.database.execute(
            "SELECT qualified_name FROM entities WHERE id = ?", (row.src_id,)
        ).fetchone()[0]
        named[str(key)] = row
    return named


def chain(row: Any) -> list[dict[str, Any]]:
    return json.loads(row.chain_json)


def hop(row: Any, kind: str) -> dict[str, Any]:
    return next(h for h in chain(row) if h["kind"] == kind)


def slugs(workspace: Workspace) -> dict[int, str]:
    return {repo.id: repo.slug for repo in workspace.repositories.list_all() if repo.id is not None}


@pytest.fixture
def corpus(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, repos())


def test_each_container_of_a_bound_release_has_one_artifact_decision(corpus: Workspace) -> None:
    decided = {release: decision for release, (decision, _) in artifacts(corpus).items()}
    assert decided == {
        "api": "linked",
        "other": "publisher-unresolved",
        "old": "tag-unmatched",
        "twin": "artifact-ambiguous",
        "noreg": "registry-unresolved",
        "alpha": "linked",
    }  # canary and maybe: established, never bound -- no artifact decision


def test_a_link_names_its_publisher_basis_and_an_unknown_version_across_ci_systems(
    corpus: Workspace,
) -> None:
    _, api = artifacts(corpus)["api"]
    assert api["image"] == "registry.example:5000/team/api"
    assert api["publisher"] == "api:.circleci/config.yml:8; api:.circleci/config.yml:9"
    assert api["version_relation"] == "unknown"
    assert (api["binding_basis"], api["build_basis"]) == ("source", "source")
    assert api["workload_in"] == "charts/web/templates/deployment.yaml:16"
    assert api["container"] == "web"


def test_refusals_say_what_was_found_and_never_pick(corpus: Workspace) -> None:
    found = artifacts(corpus)
    # another registry's image of the same path: no indexed publisher, whatever the path
    assert found["other"][1]["image"] == "registry-two.example:5000/team/api"
    assert "publisher" not in found["other"][1]
    # the only publisher names another literal version
    assert found["old"][1]["tag"] == "2.0.0"
    assert [u["tag"] for u in found["old"][1]["unmatched"]] == ["1.0.0"]
    # two compatible publishers: both listed, none chosen
    publishers = sorted(p["publisher"].split(":")[0] for p in found["twin"][1]["publishers"])
    assert publishers == ["api", "report"]
    assert "version_relation" not in found["twin"][1]


def test_a_request_links_across_three_repositories_with_each_place_in_its_own(
    corpus: Workspace,
) -> None:
    row = calls(corpus)["fetch_item"]
    assert row.decision == "linked"
    evidence = json.loads(row.evidence_json)
    assert {k: evidence[k] for k in ("deployed_by", "built_by", "provider", "source_rule")} == {
        "deployed_by": "deployer",
        "built_by": "api",
        "provider": "api",
        "source_rule": "image",
    }
    assert evidence["values_in"] == "env/test/api.yaml:2"
    assert evidence["source_in"] == "deploy/api.Dockerfile:4; api/app.py"
    names = slugs(corpus)
    places = {
        place["role"]: names[place["repo_id"]]
        for h in chain(row)
        for place in h.get("provenance", [])
    }
    assert places["identity_in"] == places["binding_in"] == places["workload_in"] == "deployer"
    assert places["values_in"] == places["image_in"] == "deployer"
    assert places["published_in"] == places["built_in"] == places["source_in"] == "api"
    assert all(h["status"] == "proven" for h in chain(row))


def test_artifact_refusals_fail_the_source_hop_with_their_own_reason(corpus: Workspace) -> None:
    named = calls(corpus)
    reasons = {
        name: (
            named[name].decision,
            hop(named[name], "source_binding")["status"],
            hop(named[name], "source_binding")["reason"],
        )
        for name in ("fetch_other", "fetch_old", "fetch_twin", "fetch_noreg")
    }
    source_unresolved = "http-deployment-source-unresolved"
    assert reasons == {
        "fetch_other": (source_unresolved, "unresolved", "publisher_unresolved"),
        "fetch_old": (source_unresolved, "unresolved", "tag_unmatched"),
        "fetch_twin": (source_unresolved, "ambiguous", "artifact_ambiguous"),
        "fetch_noreg": (source_unresolved, "unresolved", "registry_unresolved"),
    }


def test_an_established_release_s_own_unrendered_host_competes_with_its_chart_s(
    corpus: Workspace,
) -> None:
    # `maybe` releases charts/svc with an unread values path: `{svc}.svc.example` is its
    # own host pattern, and alpha.svc.example fits it (E8). Not suppressed for sharing
    # alpha's chart.
    assert artifacts(corpus)["alpha"][0] == "linked"
    assert calls(corpus)["fetch_alpha"].decision == "http-provider-ambiguous"


def test_a_hole_only_host_of_an_unread_release_competes_with_nothing(corpus: Workspace) -> None:
    # `canary`'s host is `{{ .Values.ingress.host }}` from an unread file: a pattern that
    # would fit every host, so it says nothing about api.test.example.
    assert calls(corpus)["fetch_item"].decision == "linked"


def test_exactly_one_handler_link_exists(corpus: Workspace) -> None:
    rows = corpus.database.execute(
        """SELECT s.qualified_name, d.qualified_name FROM edges e JOIN entities s ON s.id = e.src_id
           JOIN entities d ON d.id = e.dst_id WHERE e.type = 'CALLS_HTTP_HANDLER'"""
    ).fetchall()
    assert [tuple(r) for r in rows] == [("fetch_item", "get_item")]
