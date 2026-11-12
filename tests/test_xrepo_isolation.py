"""Package-proven crossings reach search only on request.

Package links also resolve ordinary imports and references into other repositories.
Those edges are facts, and ``relationships`` and change impact use them, but plain search
must answer as it did before they existed: it follows a package-proven import or reference
only with ``cross_repository`` in global scope, and otherwise says how many it left out
(``withheld_crossings``). The unlinked index here is the same corpus with package links
switched off, so nothing in it is a package-proven crossing; every other cross-repository
fact (manifest dependencies, path imports) is in both. An explicit request for ``USES``,
even one that asks to cross repositories, is confined to what ``CROSS_REPOSITORY_EDGES`` lets
cross -- which ``USES`` is not.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.packages import PackageLinks
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import RelationshipDirection, RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.xrepo_corpus import LINKING, cross_uses

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

QUESTIONS = (
    "CourseRole",
    "canEdit",
    "where is the role enum defined",
    "Role",
    "encodeParams",
    "@fixture/contracts",
    "what uses CourseRole",
    "src/policy.ts",
)

#: The one top-level key a search answer gains when package links exist.
HINT = "withheld_crossings"


def _never_linked(self: PackageLinks, origin: object, specifier: str) -> str:
    """A package link that joins nothing: every specifier is refused."""
    return "provider-absent"


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[tuple[Workspace, Workspace]]:
    """The same graph with package links on, and with them off."""
    opened = []

    def index(tag: str) -> Workspace:
        settings = Settings.model_validate(
            {
                "storage": {"database": str(tmp_path / tag / "atlas.db")},
            }
        )
        workspace = open_workspace(settings=settings, require_root=False)
        opened.append(workspace)
        return build(workspace, tmp_path / tag, LINKING)

    linked = index("linked")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("repoatlas.graph.packages.PackageLinks.resolve", _never_linked)
        unlinked = index("unlinked")
    yield linked, unlinked
    for workspace in opened:
        workspace.close()


_WHEN = frozenset({"investigation_id", "last_indexed_at"})


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _WHEN}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _answer(payload: dict[str, Any]) -> str:
    return json.dumps(_stable(payload), sort_keys=True)


def _without_hint(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != HINT}


def _search(workspace: Workspace, request: SearchRequest) -> dict[str, Any]:
    return present(request, CapabilityService(workspace).search(request))


def _contexts(workspace: Workspace) -> list[CapabilityContext]:
    """Global scope, and every repository's own scope."""
    return [
        GLOBAL,
        *(
            CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=repo.slug)
            for repo in workspace.repositories.list_all()
        ),
    ]


def _places(payload: dict[str, Any]) -> set[tuple[str, str, int | None]]:
    return {
        (item["ref"]["repository_slug"], item["ref"]["path"], item["ref"]["line_start"])
        for item in payload["evidence"]
    }


def test_package_links_were_actually_indexed(pair: tuple[Workspace, Workspace]) -> None:
    linked, unlinked = pair
    assert cross_uses(linked) and not cross_uses(unlinked)
    crossing = (
        "SELECT COUNT(*) FROM edges e JOIN entities s ON s.id = e.src_id "
        "JOIN entities d ON d.id = e.dst_id "
        "WHERE e.type IN ('IMPORTS', 'REFERENCES') AND s.repo_id != d.repo_id "
        "AND json_extract(e.resolution, '$.crossing.package') IS NOT NULL"
    )
    assert linked.database.execute(crossing).fetchone()[0] > 0
    assert unlinked.database.execute(crossing).fetchone()[0] == 0


def test_search_answers_byte_for_byte_the_same_but_for_the_hint(
    pair: tuple[Workspace, Workspace],
) -> None:
    """Default search in every scope: linked and unlinked differ in the hint alone."""
    linked, unlinked = pair
    hinted = 0
    for context in _contexts(linked):
        for question in QUESTIONS:
            request = SearchRequest(question=question, context=context)
            first, second = _search(linked, request), _search(unlinked, request)
            label = (question, context.repository)
            # (a) nothing is withheld where nothing package-proven exists
            assert second[HINT] is None, label
            # (b) the answers are the same, the hint aside
            assert _answer(_without_hint(first)) == _answer(_without_hint(second)), label
            # (c) the hint is absent or a real count
            assert first[HINT] is None or (isinstance(first[HINT], int) and first[HINT] >= 1), label
            hinted += first[HINT] is not None
    assert hinted, "no default search reported a withheld crossing"


def test_search_reports_what_it_withheld_and_follows_it_on_request(
    pair: tuple[Workspace, Workspace],
) -> None:
    linked, unlinked = pair
    default = SearchRequest(question="canEdit", context=GLOBAL)
    asked = SearchRequest(question="canEdit", context=GLOBAL, cross_repository=True)
    withheld = _search(linked, default)
    followed = _search(linked, asked)
    assert withheld[HINT] is not None and withheld[HINT] >= 1
    # Asking for the crossings is not being told about the ones left out.
    assert followed[HINT] is None
    assert _answer(_without_hint(withheld)) != _answer(_without_hint(followed))
    gained = _places(followed) - _places(withheld)
    assert gained
    assert {slug for slug, _, _ in gained} == {"contracts"}
    # Control: with nothing package-proven in the index, the flag changes nothing.
    assert _answer(_search(unlinked, asked)) == _answer(_search(unlinked, default))


def test_compact_search_carries_the_hint_only_when_there_is_one(
    pair: tuple[Workspace, Workspace],
) -> None:
    linked, unlinked = pair
    request = SearchRequest(question="canEdit", context=GLOBAL, presentation=Presentation.COMPACT)
    hinted = _search(linked, request)
    assert isinstance(hinted[HINT], int) and hinted[HINT] >= 1
    assert HINT not in _search(unlinked, request)


def test_cross_repository_needs_global_scope() -> None:
    repository = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="app")
    with pytest.raises(ValidationError, match="requires scope: global"):
        SearchRequest(question="canEdit", context=repository, cross_repository=True)
    # Global scope takes it, and the default is off in either.
    assert SearchRequest(question="canEdit", context=GLOBAL, cross_repository=True)
    assert SearchRequest(question="canEdit", context=repository).cross_repository is False


def test_explicit_relationships_cross_on_package_imports(
    pair: tuple[Workspace, Workspace],
) -> None:
    linked, _ = pair

    def into_contracts(crossing: bool) -> list[str]:
        request = RelationshipsRequest(
            context=GLOBAL,
            target="file:app:src/policy.ts",
            edge_types=(EdgeType.IMPORTS,),
            direction=RelationshipDirection.OUTGOING,
            cross_repository=crossing,
        )
        response = CapabilityService(linked).relationships(request)
        return [e.target.key for e in response.edges if e.target.repository_slug == "contracts"]

    assert into_contracts(True)
    assert into_contracts(False) == []


def test_an_explicit_request_for_uses_never_crosses(pair: tuple[Workspace, Workspace]) -> None:
    linked, _ = pair
    for crossing in (False, True):
        request = RelationshipsRequest(
            context=GLOBAL,
            target="sym:contracts:src/roles.ts:CourseRole:1",
            edge_types=(EdgeType.USES,),
            direction=RelationshipDirection.INCOMING,
            cross_repository=crossing,
        )
        text = json.dumps(present(request, CapabilityService(linked).relationships(request)))
        assert "canEdit" not in text and "staticMember" not in text
