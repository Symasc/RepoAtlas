"""topology/names: which declared names a name points at, for the explorer's search box.

Every term must occur in the qualified name, ignoring ASCII case; the entity's own
name decides the order. Checked against the index read directly, with this file's
own statement of which types are searched and how they rank, so a change to the
capability's rule cannot also change the oracle.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext
from repoatlas.models.topology import (
    MAX_NAME_MATCHES,
    MAX_QUERY_TERMS,
    TopologyNamesRequest,
    TopologyNamesResponse,
)
from repoatlas.services.workspace import Workspace
from tests.topology_corpus import GLOBAL, LINKED, open_corpus, repository

#: The oracle's own list of what is searched, written out rather than imported.
SEARCHED = ("symbol", "function", "class", "method", "env_var")

#: Names that make each rank matter: ``roles`` declared twice (a constant and a
#: method), one starting with it, one containing it, a member found by its
#: class, an underscore ``LIKE`` would read as a wildcard, and an environment
#: variable read with a default.
NAMED = {
    "package.json": '{"name": "named", "version": "1.0.0"}\n',
    "src/roles.ts": (
        "export const roles = 1;\n"
        "export const rolesList = [roles];\n"
        "export function myRoles() {\n  return roles;\n}\n"
        "export class Store {\n  roles() {\n    return 1;\n  }\n}\n"
        "export const snake_case_value = 2;\n"
        "export const snakeXcaseXvalue = 3;\n"
    ),
    "src/env.ts": "export const apiUrl = process.env.API_URL || 'http://localhost:8000';\n",
}


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_corpus(tmp_path_factory.mktemp("topology-names"), {**LINKED, "named": NAMED})
    yield workspace
    workspace.close()


def find(
    workspace: Workspace, query: str, context: CapabilityContext = GLOBAL, **fields: object
) -> TopologyNamesResponse:
    request = TopologyNamesRequest(context=context, query=query, **fields)  # type: ignore[arg-type]
    return CapabilityService(workspace).topology_names(request)


Indexed = tuple[str, str, str, str, str | None, str | None, int | None]


def indexed(workspace: Workspace, slug: str | None = None) -> list[Indexed]:
    """``(key, type, name, qualified, slug, path, line)`` for every searched entity."""
    rows = workspace.database.execute(
        "SELECT e.key, e.type, e.name, COALESCE(e.qualified_name, e.name), r.slug, f.rel_path, "
        "e.line_start FROM entities e LEFT JOIN repositories r ON r.id = e.repo_id "
        "LEFT JOIN files f ON f.id = e.file_id"
    ).fetchall()
    return [
        tuple(row)  # type: ignore[misc]
        for row in rows
        if row[1] in SEARCHED and (slug is None or row[4] in (slug, None))
    ]


def rank(name: str, last: str) -> int:
    name = name.lower()
    if name == last:
        return 0
    if name.startswith(last):
        return 1
    return 2 if last in name else 3


def oracle(entities: list[Indexed], query: str) -> list[str]:
    terms = [t.lower() for t in query.split()]
    found = [e for e in entities if all(t in e[3].lower() for t in terms)]
    found.sort(
        key=lambda e: (rank(e[2], terms[-1]), len(e[2]), e[4] or "", e[5] or "", e[6] or 0, e[0])
    )
    return [e[0] for e in found]


@pytest.mark.parametrize("query", ["roles", "ROLE", "store roles", "api", "_", "can", "Url"])
def test_every_match_holds_every_term_and_the_best_named_come_first(
    linked: Workspace, query: str
) -> None:
    expected = oracle(indexed(linked), query)
    found = find(linked, query, limit=MAX_NAME_MATCHES)
    assert found.total == len(expected) > 0
    assert [n.entity.key for n in found.names] == expected[:MAX_NAME_MATCHES]
    assert {str(n.entity.type) for n in found.names} <= set(SEARCHED)


def test_a_name_ranks_by_how_its_own_name_holds_the_term(linked: Workspace) -> None:
    found = find(linked, "roles", repository("named"))
    listed = [(n.qualified_name, str(n.entity.type)) for n in found.names]
    # named exactly, the shorter path first; then starting with it; then holding it
    assert listed[:2] == [("roles", "symbol"), ("Store.roles", "method")]
    assert listed.index(("rolesList", "symbol")) < listed.index(("myRoles", "function"))


def test_a_member_is_found_by_its_class_and_says_where_it_is(linked: Workspace) -> None:
    [member] = [n for n in find(linked, "store roles").names if str(n.entity.type) == "method"]
    assert member.qualified_name == "Store.roles"
    assert member.entity.name == "roles"
    assert (member.entity.repository_slug, member.entity.path) == ("named", "src/roles.ts")
    assert member.entity.line_start is not None


def test_an_environment_variable_belongs_to_no_repository_and_every_scope(
    linked: Workspace,
) -> None:
    def variables(found: TopologyNamesResponse) -> set[tuple[str, str | None, str | None]]:
        return {
            (n.entity.key, n.entity.repository_slug, n.entity.path)
            for n in found.names
            if str(n.entity.type) == "env_var"
        }

    assert variables(find(linked, "API_URL")) == {("env:API_URL", None, None)}
    assert variables(find(linked, "API_URL", repository("app"))) == {("env:API_URL", None, None)}


def test_a_repository_scope_finds_names_only_there(linked: Workspace) -> None:
    found = find(linked, "a", repository("app"), limit=MAX_NAME_MATCHES)
    assert {n.entity.repository_slug for n in found.names} <= {"app", None}
    assert found.total == len(oracle(indexed(linked, "app"), "a"))


def test_underscore_is_a_character_not_a_wildcard(linked: Workspace) -> None:
    names = {n.qualified_name for n in find(linked, "snake_case", limit=MAX_NAME_MATCHES).names}
    assert names == {"snake_case_value"}


def test_configuration_keys_are_not_names(linked: Workspace) -> None:
    configured = linked.database.execute(
        "SELECT COUNT(*) FROM entities WHERE type = 'config' AND lower(name) = 'version'"
    ).fetchone()[0]
    assert configured > 0
    assert all(str(n.entity.type) != "config" for n in find(linked, "version").names)


def test_the_limit_lists_fewer_and_says_how_many_match(linked: Workspace) -> None:
    found = find(linked, "roles", limit=2)
    assert len(found.names) == 2
    assert found.total > 2
    assert found.truncated


def test_a_search_opens_no_investigation(linked: Workspace) -> None:
    service = CapabilityService(linked)
    before = len(service.sessions)
    service.topology_names(TopologyNamesRequest(context=GLOBAL, query="roles"))
    assert len(service.sessions) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"query": ""},
        {"query": "   "},
        {"query": " ".join(["a"] * (MAX_QUERY_TERMS + 1))},
        {"query": "a", "limit": 0},
        {"query": "a", "limit": MAX_NAME_MATCHES + 1},
    ],
)
def test_requests_outside_what_names_answer_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TopologyNamesRequest(context=GLOBAL, **fields)  # type: ignore[arg-type]
