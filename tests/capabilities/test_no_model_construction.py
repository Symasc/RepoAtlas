"""No deterministic path builds a model, even when one is reachable (RCA-02).

The import correction proves the legacy stack is not *loaded*. That is necessary
and it is not sufficient: a module can be imported later, and an object can be
constructed without connecting to anything. A closed port proves neither, because
``OllamaBackend(...)`` succeeds happily against a port nobody is listening on --
it only fails when something asks it a question.

So this suite forces the legacy stack in (the opposite of the import tests) and
then makes every way of *entering* it fatal: the factory, the backend and the
reasoner all raise on construction. Every capability then runs against a real
indexed workspace. A capability that reached for a model would take the
exception rather than quietly succeed against a server that happened to be down.

Together with :mod:`tests.test_import_boundaries` this closes both halves: the
deterministic path does not import the legacy stack, and would not use it if
something else had.

V1's ``locate`` is covered here too. It lives in ``query_service`` beside
``reason``, so it is the one deterministic entry point that *has* the legacy
imports in scope --- which makes it the easiest place in the codebase to start
building a model by accident, and the hardest place to notice: constructing an
Ollama backend contacts nothing, so ``locate`` would go on returning correct
evidence and no end-to-end test would change its answer.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest
import repoatlas.llm.factory as factory
import repoatlas.llm.ollama as ollama
import repoatlas.llm.reasoner as reasoner
import repoatlas.services.query_service as query_service
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest, StatusRequest
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    VerifyGroundingRequest,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"


class ModelWasBuilt(AssertionError):
    """Raised by every door into the legacy reasoning stack."""


#: Every way into the legacy reasoning stack, at the module that defines it.
DOORS: tuple[tuple[object, str], ...] = (
    (factory, "build_model"),
    (ollama, "OllamaBackend"),
    (reasoner, "Reasoner"),
)


@pytest.fixture
def no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make constructing a model fatal, without removing the ability to.

    Patching the three definition sites is not enough, and the reason is a
    property of ``import`` rather than of this codebase. ``from x import f`` binds
    ``f`` into the importer's own namespace at *its* import time; rebinding
    ``x.f`` afterwards leaves that copy alone. ``query_service`` imports
    ``build_model`` and ``Reasoner`` that way, and ``factory`` imports
    ``OllamaBackend`` that way, so a guard that patched only the definitions
    would hold when this file is the first thing to touch the legacy stack and
    quietly stop holding once anything else got there first --- which, in a full
    session, is always. A guard whose strength depends on test order is not a
    guard; it is a coin toss that usually lands green.

    So every already-imported RepoAtlas module is asked whether it is holding a
    door under any name, and each reference found is replaced. That covers the
    definition modules themselves (they are in ``sys.modules`` like everything
    else), the two consumers above, and any rebinding a future refactor
    introduces that this file has never heard of.
    """

    def refuse(*args: object, **kwargs: object) -> object:
        raise ModelWasBuilt("a capability reached for a reasoning model")

    # Held by this list for the duration, so identity stays meaningful while the
    # references that kept them alive are being replaced one by one.
    originals = [getattr(module, attribute) for module, attribute in DOORS]
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("repoatlas."):
            continue
        # Identity, not name: an importer is free to rename what it imported, and
        # a scan keyed on the name would then look straight past the reference.
        for attribute, value in list(vars(module).items()):
            if any(value is original for original in originals):
                monkeypatch.setattr(module, attribute, refuse)


@pytest.fixture
def service(initialized: Workspace, no_model: None) -> Iterator[CapabilityService]:
    svc = CapabilityService(initialized)
    yield svc
    svc.close()


def test_the_guard_itself_fires(no_model: None) -> None:
    """A guard on the guard.

    If the patches missed, every test below would pass by doing nothing --- which
    is exactly how a test suite comes to certify an invariant it never checked.

    Each door is called through a plain reference and with no arguments, because
    what is being asserted is that the refusal is installed, not that the real
    signature is satisfied --- the real callable is never reached.
    """
    reachable: list[Any] = [
        factory.build_model,
        ollama.OllamaBackend,
        reasoner.Reasoner,
        # The rebound copies, which are what ``locate`` and ``reason`` reach for
        # and what a definition-site-only patch would have left real.
        query_service.build_model,
        query_service.Reasoner,
        factory.OllamaBackend,
    ]
    for door in reachable:
        with pytest.raises(ModelWasBuilt):
            door()


# ---------------------------------------------------------------------------
# All eight capabilities
# ---------------------------------------------------------------------------


def test_repositories(service: CapabilityService) -> None:
    assert service.repositories(RepositoriesRequest(context=GLOBAL)).repositories


def test_status(service: CapabilityService) -> None:
    response = service.status(StatusRequest(context=GLOBAL))
    assert response.root_configured is True
    assert response.freshness is not None


def test_search(service: CapabilityService) -> None:
    response = service.search(SearchRequest(context=GLOBAL, question="SERVICE_HOST"))
    assert response.evidence


def test_search_with_the_semantic_stage_engaged(service: CapabilityService) -> None:
    """The one capability with any embedding in its path.

    Embeddings are not reasoning --- they are a deterministic vector backend ---
    but this is the stage where a model-shaped thing is nearest, so it is worth
    naming rather than leaving to the general case above.
    """
    response = service.search(
        SearchRequest(context=GLOBAL, question="how does the service read its database settings")
    )
    assert response.freshness is not None


def test_inspect(service: CapabilityService) -> None:
    response = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path="src/config.py"),
        )
    )
    assert response.segments


def test_relationships(service: CapabilityService) -> None:
    response = service.relationships(
        RelationshipsRequest(context=GLOBAL, target="env:SERVICE_HOST", max_neighbors=50)
    )
    assert response.edges


def test_dependencies(service: CapabilityService) -> None:
    assert service.dependencies(DependenciesRequest(context=GLOBAL)).packages


def test_verify_grounding(service: CapabilityService) -> None:
    found = service.search(SearchRequest(context=GLOBAL, question="SERVICE_HOST"))
    item = found.evidence[0]
    verdict = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(
                        statement="SERVICE_HOST is configured here",
                        citations=(AgentCitation(evidence_id=item.id),),
                    ),
                ),
            ),
        )
    )
    assert verdict.verdict in {"grounded", "partially_grounded", "ungrounded"}


def test_sync(service: CapabilityService) -> None:
    """The write path, which is where a model would be least expected and worst."""
    assert service.sync(SyncRequest(context=GLOBAL)).status is not None


# ---------------------------------------------------------------------------
# V1's deterministic retrieval, which shares a module with the reasoning half
# ---------------------------------------------------------------------------


def test_v1_locate_builds_no_model(initialized: Workspace, no_model: None) -> None:
    """CASE 8. ``locate`` is retrieval, and requires no model to perform it.

    The failure this catches is silent by construction. ``build_model`` returns a
    backend without contacting anything, so a ``locate`` that started building one
    would return the same evidence, at the same ranks, with the same digests --- and
    would only reveal itself as a model process somebody did not ask for.
    """
    from repoatlas.models.context import ContextScope, RepositoryContext
    from repoatlas.services.retrieval_service import locate

    context = RepositoryContext(
        scope=ContextScope.GLOBAL,
        repositories=tuple(initialized.repositories.list_all()),
        primary=None,
        reason="global",
    )
    evidence = locate(initialized, "SERVICE_HOST", context)
    assert evidence.candidates


def test_the_diagnostic_retrieval_mode_builds_no_model(
    initialized: Workspace, no_model: None
) -> None:
    """The same for the widened mode, which retrieves more and still models nothing."""
    from repoatlas.models.context import ContextScope, RepositoryContext
    from repoatlas.services.retrieval_service import locate_diagnostic

    context = RepositoryContext(
        scope=ContextScope.GLOBAL,
        repositories=tuple(initialized.repositories.list_all()),
        primary=None,
        reason="global",
    )
    evidence = locate_diagnostic(initialized, "SERVICE_HOST", context)
    assert evidence.candidates


def test_v1_reason_does_build_a_model(initialized: Workspace, no_model: None) -> None:
    """The other side of the rule, so the two tests above cannot both be vacuous.

    ``reason`` is the legacy reasoning entry point and is *supposed* to reach the
    model. If the refusal fixture were somehow not installed, this test would fail
    and say so, rather than letting the pair above pass by checking nothing.
    """
    from repoatlas.models.context import ContextScope, RepositoryContext
    from repoatlas.services.query_service import QueryIntent, reason

    context = RepositoryContext(
        scope=ContextScope.GLOBAL,
        repositories=tuple(initialized.repositories.list_all()),
        primary=None,
        reason="global",
    )
    with pytest.raises(ModelWasBuilt):
        reason(initialized, "SERVICE_HOST", context, QueryIntent.ASK)
