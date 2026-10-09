# ruff: noqa: E501 - the sample configurations hold their lines as a CI system reads them
"""DF2's version relation and publishers (DF2_PHASE_A.md §4.4-§4.5, §15 E3).

``symbolic`` needs the same execution-stable CI built-in on both sides, one CI system and
workflow, the deploy job requiring the build job, nothing able to set the variable, and no
``default`` around the deploy side's tag. Each workflow of ``mono`` below builds one image
and releases it, varying one of those conditions; ``circle`` does the same in CircleCI.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from repoatlas.graph.deploy_artifacts import CONTAINER_ARTIFACT
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build

PLAIN = 'image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"'
DEFAULTED = (
    'image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"'
)


def chart(name: str, image: str) -> dict[str, str]:
    return {
        f"charts/{name}/Chart.yaml": f'apiVersion: v2\nname: {name}\nversion: 1.0.0\nappVersion: "1.0.0"\n',
        f"charts/{name}/values.yaml": "image:\n  repository: none\n  tag: ''\n",
        f"charts/{name}/templates/deployment.yaml": dedent(
            f"""\
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: {name}
            spec:
              template:
                spec:
                  containers:
                    - name: app
                      {image}
            """
        ),
    }


def workflow(
    name: str,
    build: str,
    deploy: str,
    *,
    needs: bool = True,
    deploy_env: str = "",
    on: str = "push:\n    branches: [main]",
) -> str:
    """A workflow of two jobs: ``build`` (its steps as given) and ``deploy`` (one run)."""
    text = f"name: {name}\non:\n  {on}\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{build}"
    text += "  deploy:\n    runs-on: ubuntu-latest\n"
    if needs:
        text += "    needs: [build]\n"
    if deploy_env:
        text += "    env:\n" + deploy_env
    return text + f"    steps:\n      - run: {deploy}\n"


def raw(image: str, tag: str, extra: str = "") -> str:
    lines = f"      - run: docker build -t {image}:{tag} . && docker push {image}:{tag}\n"
    return lines + extra


def helm(release: str, chart_name: str, image: str, tag: str) -> str:
    return f"helm upgrade --install {release} charts/{chart_name} --set image.repository={image} --set image.tag={tag}"


SHA = "${{ github.sha }}"


def mono() -> dict[str, str]:
    files = {**chart("plain", PLAIN), **chart("dflt", DEFAULTED), "Dockerfile": "FROM scratch\n"}
    cases = {
        "sym": dict(
            build=raw("reg.example/m/sym", SHA),
            deploy=helm("sym", "plain", "reg.example/m/sym", SHA),
        ),
        "noneeds": dict(
            build=raw("reg.example/m/noneeds", SHA),
            deploy=helm("noneeds", "plain", "reg.example/m/noneeds", SHA),
            needs=False,
        ),
        "jobenv": dict(
            build=raw("reg.example/m/jobenv", "$GITHUB_SHA"),
            deploy=helm("jobenv", "plain", "reg.example/m/jobenv", "$GITHUB_SHA"),
            deploy_env="      GITHUB_SHA: ${{ inputs.sha }}\n",
        ),
        "envfile": dict(
            build=raw("reg.example/m/envfile", SHA, '      - run: echo "TAG=x" >> $GITHUB_ENV\n'),
            deploy=helm("envfile", "plain", "reg.example/m/envfile", SHA),
        ),
        "attempt": dict(
            build=raw("reg.example/m/attempt", "${{ github.run_attempt }}"),
            deploy=helm("attempt", "plain", "reg.example/m/attempt", "${{ github.run_attempt }}"),
        ),
        "userdef": dict(
            build=raw("reg.example/m/userdef", "$TAG"),
            deploy=helm("userdef", "plain", "reg.example/m/userdef", "$TAG"),
        ),
        "dflt": dict(
            build=raw("reg.example/m/dflt", SHA),
            deploy=helm("dflt", "dflt", "reg.example/m/dflt", SHA),
        ),
        "exact": dict(
            build=raw("reg.example/m/exact", "1.2.3"),
            deploy=helm("exact", "plain", "reg.example/m/exact", "1.2.3"),
            needs=False,
        ),
    }
    for name, case in cases.items():
        files[f".github/workflows/{name}.yml"] = workflow(name, **case)  # type: ignore[arg-type]
    # A publisher building only on tag pushes, and a release naming the literal `latest`:
    # compatible, so linked, and the version is not established (R6).
    files[".github/workflows/tagged.yml"] = workflow(
        "tagged",
        raw("reg.example/m/tagged", "${{ github.ref_name }}"),
        "echo nothing",
        on="push:\n    tags: ['v*']",
    )
    files[".github/workflows/latest.yml"] = workflow(
        "latest",
        "      - run: echo nothing\n",
        helm("latest", "plain", "reg.example/m/tagged", "latest"),
        needs=False,
    )
    # Builtin semantics: build-push-action pushing, and one not configured to push.
    files[".github/workflows/builtin.yml"] = workflow(
        "builtin",
        "      - uses: docker/build-push-action@v6\n        with:\n          push: true\n          tags: reg.example/m/bi:${{ github.sha }}\n"
        "      - uses: docker/build-push-action@v6\n        with:\n          push: false\n          tags: reg.example/m/np:${{ github.sha }}\n",
        helm("bi", "plain", "reg.example/m/bi", SHA)
        + " && "
        + helm("np", "plain", "reg.example/m/np", SHA),
    )
    return files


CIRCLE = """\
version: 2.1
jobs:
  build:
    docker:
      - image: cimg/base:2024.01
    steps:
      - checkout
      - run: docker build -t reg.example/c/eq:<< pipeline.git.revision >> . && docker push reg.example/c/eq:<< pipeline.git.revision >>
      - run: docker build -t reg.example/c/bn:$CIRCLE_BUILD_NUM . && docker push reg.example/c/bn:$CIRCLE_BUILD_NUM
  deploy:
    docker:
      - image: cimg/base:2024.01
    steps:
      - checkout
      - run: helm upgrade --install eq charts/plain --set image.repository=reg.example/c/eq --set image.tag=$CIRCLE_SHA1
      - run: helm upgrade --install bn charts/plain --set image.repository=reg.example/c/bn --set image.tag=$CIRCLE_BUILD_NUM
workflows:
  release:
    jobs:
      - build
      - deploy:
          requires: [build]
"""


def relations(workspace: Workspace) -> dict[str, tuple[str, str | None, dict[str, Any]]]:
    found: dict[str, tuple[str, str | None, dict[str, Any]]] = {}
    for row in workspace.knowledge.list_link_decisions(family=CONTAINER_ARTIFACT):
        evidence = json.loads(row.evidence_json)
        found[evidence["release"]] = (row.decision, evidence.get("version_relation"), evidence)
    return found


@pytest.fixture
def decided(
    workspace: Workspace, tmp_path: Path
) -> dict[str, tuple[str, str | None, dict[str, Any]]]:
    build(
        workspace,
        tmp_path,
        {
            "mono": mono(),
            "circle": {
                **chart("plain", PLAIN),
                ".circleci/config.yml": CIRCLE,
                "Dockerfile": "FROM scratch\n",
            },
        },
    )
    return relations(workspace)


def test_symbolic_needs_one_stable_built_in_one_workflow_and_needs(decided: dict[str, Any]) -> None:
    assert decided["sym"][:2] == ("linked", "symbolic")
    assert decided["eq"][:2] == ("linked", "symbolic")  # pipeline.git.revision is CIRCLE_SHA1


@pytest.mark.parametrize(
    ("release", "why"),
    [
        ("noneeds", "the deploy job does not need the build job"),
        ("jobenv", "a job scope may set the variable"),
        ("envfile", "a step writes $GITHUB_ENV"),
        ("attempt", "github.run_attempt differs between a job and its rerun"),
        ("userdef", "a user-defined variable is never execution-stable"),
        ("dflt", "an empty tag would select AppVersion"),
        ("bn", "CIRCLE_BUILD_NUM differs per job"),
    ],
)
def test_the_same_spelling_alone_is_never_symbolic(
    decided: dict[str, Any], release: str, why: str
) -> None:
    assert decided[release][:2] == ("linked", "unknown"), why


def test_the_same_literal_tag_is_exact_and_no_more(decided: dict[str, Any]) -> None:
    assert decided["exact"][:2] == ("linked", "exact")


def test_a_tag_push_publisher_and_a_latest_release_link_with_an_unknown_version(
    decided: dict[str, Any],
) -> None:
    assert decided["latest"][:2] == ("linked", "unknown")


def test_only_a_build_configured_to_publish_is_a_publisher(decided: dict[str, Any]) -> None:
    decision, relation, evidence = decided["bi"]
    assert (decision, relation, evidence["build_basis"]) == ("linked", "symbolic", "builtin")
    decision, _, evidence = decided["np"]
    assert decision == "publisher-unresolved"
    assert [entry["decision"] for entry in evidence["not_publishing"]] == ["built-not-published"]
