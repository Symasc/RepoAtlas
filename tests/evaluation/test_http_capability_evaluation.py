"""The HTTP linkage evaluation corpus (brief §71).

Scored the way the graph corpus is scored, because a link is a graph answer: it
exists or it does not, and an extra one is not a near miss. RepoAtlas says two
repositories statically meet on an HTTP operation; if they do not, that sentence
is a fabrication an agent will repeat.

The metric the brief sets is **forbidden false links = 0**, and it is measured
here as a closed-set comparison over every shared HTTP node in the corpus:

* every link RepoAtlas produces must appear in ``http_cases.yaml``;
* every link listed there must be produced;
* every individually named forbidden link must be absent.

Links are read through the public ``relationships`` capability on each node, so
what is scored is what an agent would see.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.services.workspace import Workspace
from tests.http_corpus import end, http_nodes, index_corpus, relationships

CASES_PATH = Path(__file__).parent / "http_cases.yaml"

PROVIDER_EDGES = frozenset({"SERVES_HTTP_ROUTE", "EXPOSES_HTTP_ENDPOINT"})
CALLER_EDGES = frozenset({"CALLS_HTTP_ROUTE", "CALLS_HTTP_ENDPOINT"})

Link = tuple[str, str, str]


def _cases() -> dict[str, Any]:
    return yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))


def _expected_links() -> set[Link]:
    links: set[Link] = set()
    for case in _cases()["links"]:
        for provider in case["providers"]:
            for caller in case["callers"]:
                links.add((case["node"], provider, caller))
    return links


@pytest.fixture(scope="module")
def produced(tmp_path_factory: pytest.TempPathFactory) -> Iterator[set[Link]]:
    workspace: Workspace = index_corpus(tmp_path_factory.mktemp("http-eval"))
    try:
        links: set[Link] = set()
        for node in sorted(http_nodes(workspace)):
            response = relationships(workspace, node)
            providers = {end(e.source) for e in response.edges if e.type.value in PROVIDER_EDGES}
            callers = {end(e.source) for e in response.edges if e.type.value in CALLER_EDGES}
            links.update((node, provider, caller) for provider in providers for caller in callers)
        yield links
    finally:
        workspace.close()


def test_the_corpus_ground_truth_is_well_formed() -> None:
    cases = _cases()
    nodes = [case["node"] for case in cases["links"]]
    assert len(nodes) == len(set(nodes)), "each node is listed once"
    for case in cases["links"]:
        assert case["providers"] and case["callers"], case["node"]
    assert {entry["scenario"] for entry in cases["forbidden"]} >= {
        "C",
        "D",
        "E",
        "F",
        "G",
        "I",
        "J",
    }


def test_forbidden_false_links_are_zero(produced: set[Link]) -> None:
    false_links = produced - _expected_links()
    assert false_links == set(), sorted(false_links)


def test_every_expected_link_is_produced(produced: set[Link]) -> None:
    missed = _expected_links() - produced
    assert missed == set(), sorted(missed)


@pytest.mark.parametrize(
    "entry", _cases()["forbidden"], ids=lambda entry: f"{entry['scenario']}-{entry['node']}"
)
def test_each_named_false_link_is_absent(produced: set[Link], entry: dict[str, str]) -> None:
    assert (entry["node"], entry["provider"], entry["caller"]) not in produced
