"""topology/children: what one directory or file holds, and how each child is linked.

A directory's children are its immediate subdirectories and files; a file's are its
declarations. Links between two children are counted once, from the child holding
the sources, and checked against the index. Other repositories are reported only
when crossing was asked for.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from repoatlas.capabilities import topology as topology_module
from repoatlas.capabilities.errors import (
    InvalidTopologyCursorError,
    InvalidTopologyRequestError,
    PathNotFoundError,
)
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.relationships import RelationshipDirection
from repoatlas.models.topology import (
    MAX_CHILDREN_PAGE,
    MAX_EXAMPLES,
    EntryKind,
    LinkKind,
    PeerKind,
    TopologyChildrenRequest,
    TopologyChildrenResponse,
)
from repoatlas.services.workspace import Workspace
from tests.topology_corpus import GLOBAL, LINKED, STRUCTURAL_TYPES, kind_of, open_corpus, repository


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_corpus(tmp_path_factory.mktemp("topology-children"), LINKED)
    yield workspace
    workspace.close()


def children(
    workspace: Workspace, slug: str, path: str = "", **fields: object
) -> TopologyChildrenResponse:
    request = TopologyChildrenRequest(context=repository(slug), path=path, **fields)  # type: ignore[arg-type]
    return CapabilityService(workspace).topology_children(request)


def sibling_edges(workspace: Workspace, slug: str, prefix: str) -> Counter[tuple[str, str, str]]:
    """Edges between two different children of ``prefix``, by (kind, source, target) child."""
    rows = workspace.database.execute(
        """
        SELECT e.type AS type, sf.rel_path AS source, df.rel_path AS target
        FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN files sf ON sf.id = s.file_id
        JOIN entities d ON d.id = e.dst_id JOIN files df ON df.id = d.file_id
        JOIN repositories r ON r.id = sf.repo_id
        WHERE r.slug = ? AND sf.repo_id = df.repo_id AND e.type NOT IN (?, ?)
        """,
        (slug, *STRUCTURAL_TYPES),
    ).fetchall()
    found: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        source, target = row["source"], row["target"]
        if not (source.startswith(prefix + "/") and target.startswith(prefix + "/")):
            continue
        near = prefix + "/" + source[len(prefix) + 1 :].split("/")[0]
        far = prefix + "/" + target[len(prefix) + 1 :].split("/")[0]
        if near != far:
            found[(kind_of(row["type"]), near, far)] += 1
    return found


def test_a_directory_lists_subdirectories_then_files_with_their_counts(linked: Workspace) -> None:
    root = children(linked, "app")
    assert root.target is EntryKind.DIRECTORY
    assert [(e.kind, e.id) for e in root.entries] == [
        (EntryKind.DIRECTORY, "src"),
        (EntryKind.FILE, "package.json"),
    ]
    indexed = linked.database.execute(
        "SELECT COUNT(*) FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.slug = 'app' AND f.rel_path LIKE 'src/%'"
    ).fetchone()[0]
    assert root.entries[0].files == indexed
    src = children(linked, "app", "src")
    assert src.entries[0].kind is EntryKind.DIRECTORY
    policy = next(e for e in src.entries if e.id == "src/policy.ts")
    assert (policy.key, policy.declarations) == ("file:app:src/policy.ts", 2)


def test_a_file_lists_its_declarations_in_source_order(linked: Workspace) -> None:
    policy = children(linked, "app", "src/policy.ts")
    assert policy.target is EntryKind.FILE
    assert [(e.kind, e.id, e.line_start) for e in policy.entries] == [
        (EntryKind.DECLARATION, "sym:app:src/policy.ts:policy:1", 1),
        (EntryKind.DECLARATION, "sym:app:src/policy.ts:canEdit:2", 2),
    ]
    # the use of contracts is the declaration's, and the file's own import is the file's
    uses = {
        (link.child, link.peer.repository)
        for link in policy.links
        if link.peer.kind is PeerKind.REPOSITORY
    }
    assert uses == set()
    crossing = children(linked, "app", "src/policy.ts", cross_repository=True)
    assert {
        (link.child, link.peer.repository)
        for link in crossing.links
        if link.peer.kind is PeerKind.REPOSITORY
    } == {
        ("src/policy.ts", "contracts"),
        ("sym:app:src/policy.ts:canEdit:2", "contracts"),
    }


def test_a_link_between_two_children_is_counted_once_from_its_source(linked: Workspace) -> None:
    response = children(linked, "app", "src")
    reported: Counter[tuple[str, str, str]] = Counter()
    for link in response.links:
        if link.peer.kind is not PeerKind.LOCAL or link.peer.path is None:
            continue
        if not link.peer.path.startswith("src/") or "/" in link.peer.path[len("src/") :]:
            continue
        assert link.direction is RelationshipDirection.OUTGOING, link
        reported[(str(link.kind), link.child, link.peer.path)] += link.count
    expected = sibling_edges(linked, "app", "src")
    assert expected, "the corpus must hold links between children of app/src"
    assert reported == expected


def test_other_repositories_appear_only_when_crossing_is_asked_for(linked: Workspace) -> None:
    confined = children(linked, "app")
    assert not any(link.peer.kind is PeerKind.REPOSITORY for link in confined.links)
    crossing = children(linked, "app", cross_repository=True)
    peers = {
        (str(link.kind), link.child, link.peer.repository)
        for link in crossing.links
        if link.peer.kind is PeerKind.REPOSITORY
    }
    assert ("observed", "src", "contracts") in peers
    # the manifest declares it: a dependency, belonging to the file that asserts it
    assert ("dependency", "package.json", "contracts") in peers


def test_a_manifest_holds_the_dependency_edges_of_its_repository(linked: Workspace) -> None:
    response = children(linked, "app")
    manifest = [link for link in response.links if link.child == "package.json"]
    assert {(str(link.kind), link.peer.kind, link.peer.entity_type) for link in manifest} == {
        ("dependency", PeerKind.SHARED, "package")
    }
    assert sum(link.count for link in manifest) == 5


def test_examples_are_bounded_and_link_kinds_are_pure(linked: Workspace) -> None:
    response = children(linked, "app", "src", cross_repository=True)
    for link in response.links:
        assert 1 <= len(link.examples) <= min(MAX_EXAMPLES, link.count)
        assert {kind_of(str(t.type)) for t in link.types} == {str(link.kind)}
    assert any(link.kind is LinkKind.OBSERVED for link in response.links)


def test_pages_resume_where_they_stopped_and_only_for_their_own_path(linked: Workspace) -> None:
    whole = children(linked, "app", "src")
    first = children(linked, "app", "src", limit=4)
    assert first.has_more and first.next_cursor is not None
    assert first.entries_total == whole.entries_total
    rest = children(linked, "app", "src", limit=100, cursor=first.next_cursor)
    assert [e.id for e in (*first.entries, *rest.entries)] == [e.id for e in whole.entries]
    assert not rest.has_more and rest.next_cursor is None
    # the page is of entries; links always describe every child
    assert first.links == whole.links
    with pytest.raises(InvalidTopologyCursorError):
        children(linked, "app", "", cursor=first.next_cursor)
    with pytest.raises(InvalidTopologyCursorError):
        children(linked, "app", "src", cursor="tc1.not-a-cursor")
    clamped = children(linked, "app", "src", limit=MAX_CHILDREN_PAGE + 1)
    assert (clamped.limits.applied, clamped.limits.clamped) == (MAX_CHILDREN_PAGE, ("limit",))


def test_links_past_the_cap_are_counted_and_cut(
    linked: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = children(linked, "app", "src")
    monkeypatch.setattr(topology_module, "MAX_CHILD_LINKS", 2)
    cut = children(linked, "app", "src")
    assert cut.truncated and cut.links_total == whole.links_total > 2
    assert list(cut.links) == list(whole.links[:2])


def test_requests_outside_what_children_answer_are_refused(linked: Workspace) -> None:
    with pytest.raises(PathNotFoundError):
        children(linked, "app", "no/such/place")
    with pytest.raises(InvalidTopologyRequestError):
        CapabilityService(linked).topology_children(TopologyChildrenRequest(context=GLOBAL))
    for bad in ("../app", "/src", "src/../src", "src\\guard.ts"):
        with pytest.raises(ValidationError):
            TopologyChildrenRequest(context=repository("app"), path=bad)
    # a trailing slash and "." name the same places
    assert children(linked, "app", "src/").path == "src"
    assert children(linked, "app", ".").path == ""
