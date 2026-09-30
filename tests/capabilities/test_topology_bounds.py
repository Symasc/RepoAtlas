"""Topology reads are a fixed number of statements, whatever they describe.

The same three repositories are indexed twice, once with six times the uses of the
other, and every topology answer must cost exactly the same statements in both.
Within one index, directories holding two files and fourteen must cost the same
too. A read that ran a query per child, per file or per edge would fail here
before it was slow anywhere.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.topology import (
    LinkKind,
    TopologyChildrenRequest,
    TopologyEdgeRequest,
    TopologyOverviewRequest,
)
from repoatlas.services.workspace import Workspace
from tests.topology_corpus import GLOBAL, open_corpus, repository, statements
from tests.xrepo_corpus import hub_repos


@pytest.fixture(scope="module")
def sizes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Workspace, Workspace]]:
    small = open_corpus(tmp_path_factory.mktemp("topology-small"), hub_repos(1))
    large = open_corpus(tmp_path_factory.mktemp("topology-large"), hub_repos(6))
    yield small, large
    small.close()
    large.close()


def counted(workspace: Workspace, call: Callable[[CapabilityService], object]) -> int:
    service = CapabilityService(workspace)
    with statements(workspace) as seen:
        call(service)
    return len(seen)


def overview(service: CapabilityService) -> object:
    return service.topology_overview(TopologyOverviewRequest(context=GLOBAL))


def children_of(slug: str, path: str) -> Callable[[CapabilityService], object]:
    def call(service: CapabilityService) -> object:
        return service.topology_children(
            TopologyChildrenRequest(context=repository(slug), path=path, cross_repository=True)
        )

    return call


def first_edge(service: CapabilityService) -> object:
    links = service.topology_overview(TopologyOverviewRequest(context=GLOBAL)).links
    ref = next(link.examples[0] for link in links if link.kind is LinkKind.OBSERVED)
    return service.topology_edge(TopologyEdgeRequest(context=GLOBAL, edge_ref=ref))


def test_the_corpora_differ_in_size(sizes: tuple[Workspace, Workspace]) -> None:
    small, large = sizes

    def uses(workspace: Workspace) -> int:
        return workspace.database.execute(
            "SELECT COUNT(*) FROM edges WHERE type = 'USES'"
        ).fetchone()[0]

    assert uses(large) > 3 * uses(small)


@pytest.mark.parametrize(
    "call",
    [
        overview,
        children_of("hub-a", ""),
        children_of("hub-a", "packages"),
        children_of("hub-a", "packages/u0/src"),
        children_of("hub-a", "packages/u0/src/f0.ts"),
        first_edge,
    ],
    ids=["overview", "children-root", "children-dir", "children-leaf-dir", "children-file", "edge"],
)
def test_a_larger_index_costs_the_same_statements(
    sizes: tuple[Workspace, Workspace], call: Callable[[CapabilityService], object]
) -> None:
    small, large = sizes
    assert counted(small, call) == counted(large, call)


def _modules(count: int) -> dict[str, str]:
    """``count`` sibling modules, each importing the next, under one directory."""
    files = {"pyproject.toml": '[project]\nname = "width"\nversion = "1.0"\n'}
    for i in range(count):
        files[f"src/width/m{i:02d}.py"] = (
            f"from width.m{(i + 1) % count:02d} import value{(i + 1) % count}\n\n"
            f"def value{i}():\n    return {i}\n"
        )
    return files


@pytest.fixture(scope="module")
def widths(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Workspace, Workspace]]:
    narrow = open_corpus(tmp_path_factory.mktemp("topology-narrow"), {"width": _modules(2)})
    wide = open_corpus(tmp_path_factory.mktemp("topology-wide"), {"width": _modules(30)})
    yield narrow, wide
    narrow.close()
    wide.close()


def test_a_directory_costs_the_same_however_many_children_it_holds(
    widths: tuple[Workspace, Workspace], sizes: tuple[Workspace, Workspace]
) -> None:
    narrow, wide = widths
    for path in ("", "src", "src/width"):
        assert counted(narrow, children_of("width", path)) == counted(
            wide, children_of("width", path)
        ), path

    def files(workspace: Workspace) -> int:
        return workspace.database.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    assert files(wide) > 5 * files(narrow)
    # two roots of different shapes cost the same too
    _, large = sizes
    assert counted(large, children_of("hub-a", "")) == counted(large, children_of("hub-b", ""))
