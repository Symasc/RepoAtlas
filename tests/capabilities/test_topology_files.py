"""topology/files: which indexed files a name points at, for the explorer's search box.

Every term must occur in the path, ignoring ASCII case; the file's own name decides
the order. Checked against the index read directly, with this file's own statement
of the ranking, so a change to the capability's rule cannot also change the oracle.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext
from repoatlas.models.topology import (
    MAX_FILE_MATCHES,
    MAX_QUERY_TERMS,
    TopologyFilesRequest,
    TopologyFilesResponse,
)
from repoatlas.services.workspace import Workspace
from tests.topology_corpus import GLOBAL, LINKED, open_corpus, repository

#: A file whose name has the characters ``LIKE`` would read as wildcards, and
#: names that make each rank matter: a file named exactly ``roles`` deep down, one
#: starting with it, one containing it, and one with it only in a directory, each
#: on a shorter path than the one before.
WILD = {
    "package.json": '{"name": "wild", "version": "1.0.0"}\n',
    "src/100%_done.ts": "export const a = 1;\n",
    "deeply/nested/folder/for/roles": "roles\n",
    "deeply/nested/folder/roles_list.ts": "export const b = 2;\n",
    "lib/myroles.ts": "export const c = 3;\n",
    "roles/x.ts": "export const d = 4;\n",
}


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_corpus(tmp_path_factory.mktemp("topology-files"), {**LINKED, "wild": WILD})
    yield workspace
    workspace.close()


def find(
    workspace: Workspace, query: str, context: CapabilityContext = GLOBAL, **fields: object
) -> TopologyFilesResponse:
    request = TopologyFilesRequest(context=context, query=query, **fields)  # type: ignore[arg-type]
    return CapabilityService(workspace).topology_files(request)


def indexed(workspace: Workspace, slug: str | None = None) -> list[tuple[str, str]]:
    rows = workspace.database.execute(
        "SELECT r.slug, f.rel_path FROM files f JOIN repositories r ON r.id = f.repo_id"
    ).fetchall()
    return [(row[0], row[1]) for row in rows if slug is None or row[0] == slug]


def rank(path: str, last: str) -> int:
    name = path.rsplit("/", 1)[-1].lower()
    if name == last:
        return 0
    if name.startswith(last):
        return 1
    return 2 if last in name else 3


def oracle(files: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    terms = [t.lower() for t in query.split()]
    found = [f for f in files if all(t in f[1].lower() for t in terms)]
    return sorted(found, key=lambda f: (rank(f[1], terms[-1]), len(f[1]), f[0], f[1]))


@pytest.mark.parametrize(
    "query", ["policy", "SRC", "index.ts", "src index", "roles", "package.json"]
)
def test_every_match_holds_every_term_and_the_best_named_come_first(
    linked: Workspace, query: str
) -> None:
    expected = oracle(indexed(linked), query)
    found = find(linked, query, limit=MAX_FILE_MATCHES)
    assert found.total == len(expected) > 0
    assert [(f.repository, f.path) for f in found.files] == expected[:MAX_FILE_MATCHES]
    assert all(f.name == f.path.rsplit("/", 1)[-1] for f in found.files)


def test_the_files_own_name_ranks_before_a_shorter_path(linked: Workspace) -> None:
    found = find(linked, "roles", repository("wild"))
    assert [f.path for f in found.files] == [
        "deeply/nested/folder/for/roles",
        "deeply/nested/folder/roles_list.ts",
        "lib/myroles.ts",
        "roles/x.ts",
    ]


def test_a_repository_scope_finds_files_only_there(linked: Workspace) -> None:
    found = find(linked, "src", repository("app"), limit=MAX_FILE_MATCHES)
    assert {f.repository for f in found.files} == {"app"}
    assert found.total == len(oracle(indexed(linked, "app"), "src"))


def test_percent_and_underscore_are_characters_not_wildcards(linked: Workspace) -> None:
    assert [(f.repository, f.path) for f in find(linked, "%").files] == [
        ("wild", "src/100%_done.ts")
    ]
    underscored = find(linked, "_", limit=MAX_FILE_MATCHES)
    assert [(f.repository, f.path) for f in underscored.files] == oracle(indexed(linked), "_")[
        :MAX_FILE_MATCHES
    ]
    assert find(linked, "no-such-file-anywhere").total == 0


def test_the_limit_lists_fewer_and_says_how_many_match(linked: Workspace) -> None:
    found = find(linked, "src", limit=2)
    assert len(found.files) == 2
    assert found.total > 2
    assert found.truncated


def test_a_search_opens_no_investigation(linked: Workspace) -> None:
    service = CapabilityService(linked)
    before = len(service.sessions)
    service.topology_files(TopologyFilesRequest(context=GLOBAL, query="policy"))
    assert len(service.sessions) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"query": ""},
        {"query": "   "},
        {"query": " ".join(["a"] * (MAX_QUERY_TERMS + 1))},
        {"query": "a", "limit": 0},
        {"query": "a", "limit": MAX_FILE_MATCHES + 1},
    ],
)
def test_requests_outside_what_files_answer_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TopologyFilesRequest(context=GLOBAL, **fields)  # type: ignore[arg-type]
