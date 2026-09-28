"""Cross-repository ``USES`` edges are the impact walk's alone: no other tool sees them.

A package link adds facts beside the graph, not into it. Search never expands through
``USES``, so it answers byte for byte as it did before links existed; and an explicit
``relationships`` request for ``USES``, even one that asks to cross repositories, is
confined to what ``CROSS_REPOSITORY_EDGES`` lets cross -- which ``USES`` is not.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
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


def _never_linked(*_: object) -> str:
    return "provider-absent"


@pytest.fixture
def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Workspace, Workspace]]:
    """The XR repositories indexed twice: as shipped, and with package links switched off."""
    opened = []

    def index(tag: str) -> Workspace:
        settings = Settings.model_validate(
            {
                "storage": {"database": str(tmp_path / tag / "atlas.db")},
                "embeddings": {"provider": "hashing", "dim": 128},
            }
        )
        workspace = open_workspace(settings=settings, require_root=False)
        opened.append(workspace)
        return build(workspace, tmp_path / tag, LINKING)

    linked = index("linked")
    monkeypatch.setattr("repoatlas.graph.packages.PackageLinks.resolve", _never_linked)
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


def test_search_answers_byte_for_byte_the_same(pair: tuple[Workspace, Workspace]) -> None:
    linked, unlinked = pair
    assert cross_uses(linked) and not cross_uses(unlinked)
    services = [CapabilityService(workspace) for workspace in pair]
    for question in QUESTIONS:
        request = SearchRequest(question=question, context=GLOBAL)
        first, second = (present(request, service.search(request)) for service in services)
        assert json.dumps(_stable(first), sort_keys=True) == json.dumps(
            _stable(second), sort_keys=True
        ), question


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
