"""Handler links in relationships (deployment HTTP roadmap, M5a; review decision C7).

``CALLS_HTTP_HANDLER`` is in the relationships default family and may be named. It
joins two repositories directly, so it leaves one only under ``cross_repository``; a
link inside one repository is walked like any other edge. Search's expansion set and
the change-impact walk's crossing rule are unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.traversal import (
    CROSS_REPOSITORY_EDGES,
    EXPANSION_EDGES,
    RELATIONSHIP_CROSSING_EDGES,
    RELATIONSHIP_DEFAULT_EDGES,
    RELATIONSHIP_EDGES,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import (
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import ACCOUNTS, WORKSPACE
from tests.impact_corpus import build

HANDLER = EdgeType.CALLS_HTTP_HANDLER


def _key(workspace: Workspace, repository: str, name: str) -> str:
    row = workspace.database.execute(
        """
        SELECT e.key FROM entities e JOIN repositories r ON r.id = e.repo_id
        WHERE r.slug = ? AND e.qualified_name = ?
        """,
        (repository, name),
    ).fetchone()
    return str(row["key"])


def _walk(
    workspace: Workspace, repository: str | None, target: str, **kwargs: object
) -> RelationshipsResponse:
    context = (
        CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=repository)
        if repository
        else CapabilityContext(scope=CapabilityScope.GLOBAL)
    )
    request = RelationshipsRequest(context=context, target=target, **kwargs)  # type: ignore[arg-type]
    return CapabilityService(workspace).relationships(request)


def _links(response: RelationshipsResponse) -> list[tuple[str, str, int, bool]]:
    return sorted(
        (edge.source.key, edge.target.key, edge.hop, edge.cross_repository)
        for edge in response.edges
        if edge.type is HANDLER
    )


@pytest.fixture
def joined(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


def test_a_handler_link_leaves_the_repository_only_when_asked(joined: Workspace) -> None:
    caller = _key(joined, "portal", "load_user")
    handler = _key(joined, "accounts", "get_user")
    assert _links(_walk(joined, "portal", caller)) == []
    assert _links(_walk(joined, "portal", caller, cross_repository=True)) == [
        (caller, handler, 1, True)
    ]


def test_a_handler_is_walked_back_to_its_callers_in_every_direction_asked(
    joined: Workspace,
) -> None:
    handler = _key(joined, "accounts", "get_user")
    callers = {_key(joined, "portal", name) for name in ("load_user", "load_literal")}
    incoming = _walk(
        joined, "accounts", handler, cross_repository=True, direction=RelationshipDirection.INCOMING
    )
    assert {source for source, *_ in _links(incoming)} == callers
    outgoing = _walk(
        joined, "accounts", handler, cross_repository=True, direction=RelationshipDirection.OUTGOING
    )
    assert _links(outgoing) == []


def test_a_handler_link_can_be_named_alone(joined: Workspace) -> None:
    caller = _key(joined, "portal", "load_user")
    response = _walk(joined, "portal", caller, edge_types=(HANDLER,), cross_repository=True)
    assert {edge.type for edge in response.edges} == {HANDLER}
    assert len(response.edges) == 1


SELF_CALLING = {
    **ACCOUNTS,
    "client.py": (
        "import requests\n\n\n"
        "def fetch(user_id):\n"
        '    return requests.get(f"https://accounts.example/api/users/{user_id}")\n'
    ),
}


def test_a_link_inside_one_repository_is_walked_by_default(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"accounts": SELF_CALLING})
    caller = _key(built, "accounts", "fetch")
    handler = _key(built, "accounts", "get_user")
    assert _links(_walk(built, "accounts", caller)) == [(caller, handler, 1, False)]


def test_search_and_impact_keep_their_own_edge_sets() -> None:
    """Search expands, and the change-impact walk crosses, exactly as before: impact
    crosses a handler link by its own inward rule on the link's evidence."""
    assert HANDLER not in EXPANSION_EDGES
    assert HANDLER not in CROSS_REPOSITORY_EDGES
    assert HANDLER in RELATIONSHIP_DEFAULT_EDGES
    assert HANDLER in RELATIONSHIP_EDGES
    assert CROSS_REPOSITORY_EDGES | {HANDLER} == RELATIONSHIP_CROSSING_EDGES
    assert set(RELATIONSHIP_DEFAULT_EDGES) >= RELATIONSHIP_CROSSING_EDGES
