# ruff: noqa: E501 - the sample configurations hold their lines as a CI system reads them
"""Which releases a CI configuration binds, in how many variants (DF2_PHASE_A.md §2.3,
§15 E4): one per value of a literal finite branch domain, at most 16; nothing else --
a condition, a filter pattern, another trigger -- ever adds or narrows one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.graph.deploy_artifacts import CONTAINER_ARTIFACT, MAX_VARIANTS
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.test_artifact_versions import PLAIN, chart

STAGE_SET = "--set image.repository=reg.example/b/{name} --set image.tag=1.0.0 --set stage={symbol}"


def circle(name: str, filters: str, environment: str = "") -> str:
    helm = f"helm upgrade --install {name} charts/plain " + STAGE_SET.format(
        name=name, symbol="$CIRCLE_BRANCH"
    )
    return (
        "version: 2.1\njobs:\n  deploy:\n    docker:\n      - image: cimg/base:2024.01\n"
        + environment
        + f"    steps:\n      - run: {helm}\n"
        + f"workflows:\n  release:\n    jobs:\n      - deploy:\n          filters:\n            branches:\n{filters}"
    )


def github(name: str, on: str, condition: str = "") -> str:
    helm = f"helm upgrade --install {name} charts/plain " + STAGE_SET.format(
        name=name, symbol="${{ github.ref_name }}"
    )
    return (
        f"name: {name}\non:\n{on}jobs:\n  deploy:\n    runs-on: ubuntu-latest\n{condition}"
        f"    steps:\n      - run: {helm}\n"
    )


MANY = [f"b{index:02d}" for index in range(MAX_VARIANTS + 1)]


def repos() -> dict[str, dict[str, str]]:
    base = {**chart("plain", PLAIN)}
    return {
        "two": {
            **base,
            ".circleci/config.yml": circle("two", "              only: [main, release]\n"),
        },
        "capped": {
            **base,
            ".circleci/config.yml": circle("capped", f"              only: [{', '.join(MANY)}]\n"),
        },
        "pattern": {
            **base,
            ".circleci/config.yml": circle("pattern", "              only: /release-.*/\n"),
        },
        "settable": {
            **base,
            ".circleci/config.yml": circle(
                "settable",
                "              only: [main, release]\n",
                "    environment:\n      CIRCLE_BRANCH: $OTHER\n",
            ),
        },
        "gh": {
            **base,
            ".github/workflows/a.yml": github("pushed", "  push:\n    branches: [main, next]\n"),
            ".github/workflows/b.yml": github(
                "mixed", "  push:\n    branches: [main]\n  pull_request:\n"
            ),
            ".github/workflows/c.yml": github(
                "guarded",
                "  push:\n    branches: [main]\n",
                "    if: github.ref == 'refs/heads/release'\n",
            ),
        },
    }


@pytest.fixture
def variants(workspace: Workspace, tmp_path: Path) -> dict[str, list[str]]:
    build(workspace, tmp_path, repos())
    found: dict[str, list[str]] = {}
    for row in workspace.knowledge.list_link_decisions(family=CONTAINER_ARTIFACT):
        evidence = json.loads(row.evidence_json)
        found.setdefault(evidence["release"], []).append(evidence["variant"])
    return {release: sorted(listed) for release, listed in found.items()}


def test_a_literal_branch_domain_gives_one_variant_per_branch(
    variants: dict[str, list[str]],
) -> None:
    assert variants["two"] == ["branch=main", "branch=release"]
    assert variants["pushed"] == ["branch=main", "branch=next"]


def test_past_the_cap_a_release_renders_nothing(variants: dict[str, list[str]]) -> None:
    assert "capped" not in variants


@pytest.mark.parametrize(
    ("release", "why"),
    [
        ("pattern", "a regex filter gives no literal domain"),
        ("mixed", "another trigger than push gives no literal domain"),
        ("settable", "a job scope may set CIRCLE_BRANCH"),
    ],
)
def test_without_a_literal_domain_the_symbol_stays_unexpanded(
    variants: dict[str, list[str]], release: str, why: str
) -> None:
    assert variants[release] == [""], why


def test_a_condition_neither_adds_nor_narrows_a_variant(variants: dict[str, list[str]]) -> None:
    # `if: github.ref == 'refs/heads/release'` is not read as a branch: the domain stays [main]
    assert variants["guarded"] == ["branch=main"]
