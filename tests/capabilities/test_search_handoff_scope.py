"""Every handle search hands back is one relationships takes under the same context.

``RelationshipsRequest.target`` is documented as "the handle ``search`` hands back,
usable verbatim". The LVL AuthZ audit found a repository-scoped search publishing
``sym:RepoAtlas:…`` handles that ``relationships`` then refused under the identical
context — with an error that blamed a moved key and advised searching again, which
returned the same refused key. The only test of the handoff ran in global scope,
the one place it cannot fail.

The rule is stated once (``_resolvable_in``) and used on both sides: a repository-
owned entity must belong to a repository the call named; an interned one — an
environment variable, a command, an HTTP operation — belongs to none and is in
every scope. Inspectability is not part of the promise: an interned entity has no
file to inspect.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _scoped(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _search(workspace: Workspace, question: str, context: CapabilityContext) -> SearchResponse:
    return CapabilityService(workspace).search(
        SearchRequest(question=question, context=context, budget=EvidenceBudget(max_items=18))
    )


def _handles(response: SearchResponse) -> set[str]:
    published = {handle.key for handle in response.entities}
    published |= {item.entity.key for item in response.evidence if item.entity is not None}
    return published


#: Each asks inside one repository about names defined in others, which is how a
#: foreign handle reached the caller: the name lookup that seeds expansion ran a
#: second pass over every repository.
CASES = [
    pytest.param("ts-service", "Where is load_config defined?", id="python-name-from-ts"),
    pytest.param("ts-service", "load_config servicePort getConfig", id="three-repositories"),
    pytest.param("python-service", "servicePort getConfig", id="ts-names-from-python"),
    pytest.param("common-lib", "Where is SERVICE_HOST configured?", id="hub-from-common-lib"),
    pytest.param("python-service", "src/config.py load_config", id="own-names"),
]


@pytest.mark.parametrize(("slug", "question"), CASES)
def test_every_published_handle_resolves_under_the_same_context(
    initialized: Workspace, slug: str, question: str
) -> None:
    context = _scoped(slug)
    response = _search(initialized, question, context)
    service = CapabilityService(initialized)
    for key in sorted(_handles(response)):
        service.relationships(RelationshipsRequest(context=context, target=key))


def test_a_name_defined_elsewhere_is_not_offered_as_a_handle(initialized: Workspace) -> None:
    response = _search(initialized, "Where is load_config defined?", _scoped("ts-service"))
    assert not any(key.startswith("sym:python-service:") for key in _handles(response))


def test_an_interned_hub_is_still_offered_under_repository_scope(initialized: Workspace) -> None:
    """A hub belongs to no repository, so no repository scope excludes it."""
    response = _search(initialized, "Where is SERVICE_HOST configured?", _scoped("common-lib"))
    assert "env:SERVICE_HOST" in {handle.key for handle in response.entities}


def test_global_scope_still_offers_every_repository(initialized: Workspace) -> None:
    """The filter is the context's, not a narrowing of its own."""
    response = _search(initialized, "load_config servicePort getConfig", GLOBAL)
    owners = {key.split(":")[1] for key in _handles(response) if key.startswith("sym:")}
    assert {"python-service", "ts-service", "common-lib"} <= owners
