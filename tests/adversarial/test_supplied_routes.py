"""A route this investigation was shown may be named; nothing else may.

Grounding reads a token with slashes in prose as a repository path, because a
path in a sentence reads as provenance to a human and a fabricated one is the
shape the whole rule exists to catch. A route has that shape too, so an agent
that correctly reported ``GET /v1/courses/${courseId}/guardian`` had the claim
removed as a fabricated file path — a true statement, deleted, with no way to
state it.

The fix is the rule grounding already applies to files, extended to routes and
to nothing else. Prose may name a **file** the investigation supplied. It may
likewise name a **route** the investigation supplied, as an HTTP row, with its
lines. Form exempts nothing: a token is not safe because it looks like a route,
and these tests are mostly about what still goes.

What must still be removed, and is:

* a route this investigation was never shown, however route-shaped —
  ``GET /etc/shadow`` is not exempt for having a method in front of it;
* a route supplied only in a *different* investigation;
* a route named in a claim whose rows were fetched without ``include_provenance``,
  because then nothing was supplied for it;
* a literal where the supplied route has a parameter: ``/v1/courses/42/guardian``
  is not covered by ``/v1/courses/:courseId/guardian``;
* an unsupplied path sitting in the same sentence as a supplied route, which
  still condemns the claim it is in.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_inventory import HttpOperationsRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingVerdict,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace
from tests.capabilities.test_http_operations import COURSES, GUARDIANS, OTHER, REPOSITORY, _index

#: The route the client writes and nothing in this repository serves. The whole
#: original failure was that reporting it truthfully was impossible.
GUARDIAN = "GET /v1/courses/${courseId}/guardian"
CLIENT = "src/client.ts"


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    opened, _ = _index(tmp_path, {COURSES: REPOSITORY, GUARDIANS: OTHER})
    try:
        yield opened
    finally:
        opened.close()


def _context(slug: str = COURSES) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _rows_with_provenance(service: CapabilityService) -> tuple[str, str]:
    """Supply the client's rows with their lines. Returns the investigation and an id."""
    response = service.http_operations(
        HttpOperationsRequest(
            context=_context(), include_provenance=True, within_path=CLIENT, limit=200
        )
    )
    named = [row for row in response.operations if row.route.endswith("/guardian")]
    assert named and named[0].evidence_id is not None
    return response.investigation_id, named[0].evidence_id


def _lines_without_routes(service: CapabilityService) -> tuple[str, str]:
    """Supply the very same lines, with no route context at all.

    An inspection of the file the client is written in: the agent saw the text,
    so a citation to it resolves and the claim reaches the prose rule — which is
    the only thing these cases are about.
    """
    response = service.inspect(
        InspectRequest(context=_context(), target=InspectTarget(path=CLIENT))
    )
    assert response.segments
    return response.investigation_id, response.segments[0].id


def _verify(
    service: CapabilityService,
    investigation: str,
    evidence_id: str,
    statement: str,
    **answer: object,
) -> VerifyGroundingResponse:
    return service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=investigation,
            answer=AgentAnswer(
                claims=(
                    AgentClaim(
                        statement=statement,
                        citations=(AgentCitation(evidence_id=evidence_id),),
                    ),
                ),
                **answer,  # type: ignore[arg-type]
            ),
        )
    )


def _survived(response: VerifyGroundingResponse) -> bool:
    return bool(response.sanitized_answer.claims)


# -- what may now be said ------------------------------------------------------


def test_a_route_this_investigation_was_shown_may_be_named(workspace: Workspace) -> None:
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        f"The client calls {GUARDIAN}, which this repository does not serve.",
    )
    assert _survived(response)
    assert response.verdict is GroundingVerdict.GROUNDED
    kept = response.sanitized_answer.claims[0].statement
    assert GUARDIAN in kept, "the route comes back exactly as it was written"


def test_the_handlers_own_spelling_of_the_same_shape_is_the_same_route(
    workspace: Workspace,
) -> None:
    """A supplied ``${courseId}`` covers a named ``:courseId``: one shape, two spellings."""
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        "Nothing here serves GET /v1/courses/:courseId/guardian.",
    )
    assert _survived(response)


def test_a_supplied_route_survives_in_the_summary_and_the_unknowns(
    workspace: Workspace,
) -> None:
    """The free-text fields are read by the same rule, and restored by the same one."""
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        f"The client calls {GUARDIAN}.",
        summary=f"{GUARDIAN} has no handler here.",
        unknowns=(f"Whether another service serves {GUARDIAN}.",),
        next_check=f"Search the other repositories for {GUARDIAN}.",
    )
    sanitized = response.sanitized_answer
    assert GUARDIAN in sanitized.summary
    assert any(GUARDIAN in unknown for unknown in sanitized.unknowns)
    assert sanitized.next_check is not None and GUARDIAN in sanitized.next_check


def test_text_that_already_holds_the_hidden_character_is_left_exactly_as_written(
    workspace: Workspace,
) -> None:
    """The transform is injective or it does not run.

    Hiding a route's separators means writing them back afterwards. A text that
    already contains the hiding character has no unambiguous way back, so it is
    handed to V1 untouched and read exactly as it would have been before any of
    this existed. What must never happen is a slash appearing where the agent
    wrote something else.
    """
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        f"The client calls {GUARDIAN}.",
        summary=f"{GUARDIAN} is called\x1chere.",
    )
    assert "called/here" not in response.sanitized_answer.summary


# -- what is still removed -----------------------------------------------------


def test_a_route_shaped_token_that_was_never_supplied_is_still_removed(
    workspace: Workspace,
) -> None:
    """The exemption is anchored to what was supplied, never to the shape of a token."""
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    for statement in (
        "The service reads GET /etc/shadow at startup.",
        "The service reads /etc/shadow at startup.",
        "It loads GET /srv/{env}/secrets.yaml before booting.",
        "It loads GET infra/secrets.yaml before booting.",
    ):
        response = _verify(service, investigation, evidence_id, statement)
        assert not _survived(response), statement


def test_a_method_this_investigation_never_saw_is_not_covered_by_another(
    workspace: Workspace,
) -> None:
    """A supplied row is one operation, not a licence over its path.

    ``GET`` on the guardian path was supplied; ``PUT`` on it was not, and inventing
    a second operation from the first is exactly the fabrication this is here to
    prevent — the more so because writes are what an over-claim would be about.
    """
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        "The client calls PUT /v1/courses/${courseId}/guardian.",
    )
    assert not _survived(response)


def test_an_unsupplied_path_beside_a_supplied_route_still_condemns_the_claim(
    workspace: Workspace,
) -> None:
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        f"The client calls {GUARDIAN}, configured from infra/secrets.yaml.",
    )
    assert not _survived(response)


def test_a_literal_where_the_supplied_route_has_a_parameter_is_removed(
    workspace: Workspace,
) -> None:
    """A parameter is not a wildcard over prose: ``42`` is a different route."""
    service = CapabilityService(workspace)
    investigation, evidence_id = _rows_with_provenance(service)
    response = _verify(
        service,
        investigation,
        evidence_id,
        "The client calls GET /v1/courses/42/guardian.",
    )
    assert not _survived(response)


def test_a_route_whose_rows_were_fetched_without_provenance_is_not_supplied(
    workspace: Workspace,
) -> None:
    """Reading a row is not being shown its lines. Only the lines are supplied."""
    service = CapabilityService(workspace)
    listed = service.http_operations(
        HttpOperationsRequest(context=_context(), within_path=CLIENT, limit=200)
    )
    assert all(row.evidence_id is None for row in listed.operations)
    inspected = service.inspect(
        InspectRequest(
            context=_context(),
            target=InspectTarget(path=CLIENT),
            investigation_id=listed.investigation_id,
        )
    )
    response = _verify(
        service,
        listed.investigation_id,
        inspected.segments[0].id,
        f"The client calls {GUARDIAN}.",
    )
    assert not _survived(response)


def test_a_route_supplied_in_another_investigation_is_not_supplied_here(
    workspace: Workspace,
) -> None:
    """Evidence is investigation-scoped, and so is everything recorded beside it."""
    service = CapabilityService(workspace)
    _rows_with_provenance(service)  # another investigation, which saw the route
    other, evidence_id = _lines_without_routes(service)
    response = _verify(service, other, evidence_id, f"The client calls {GUARDIAN}.")
    assert not _survived(response)
