"""From a declaration a search found to the source that defines it.

An agent searching LVL for ``EMAIL_LINKS`` got exactly the right entity — and its
evidence was ``const EMAIL_LINKS``: the declaration RepoAtlas recorded, supplied
without a line range because it is not a line of the file. The agent then read
the file some other way, outside the evidence path, to see the object's keys.

Nothing was missing. The entity carries its declarator's real span, and
``inspect`` on its key returns that span with context as citable lines. What was
missing was the pointer: the only sentence saying so lived in a response field
description that the MCP server never publishes. These tests pin the workflow
end to end on a file shaped like LVL's ``links.ts``, and pin the pointer where an
agent reads it — the tool descriptions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp.server import INSTRUCTIONS
from repoatlas.mcp.tools import INSPECT, SEARCH
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingVerdict,
    VerifyGroundingRequest,
)
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

REPO = "contracts"
LINKS = "src/links.ts"
CONTEXT = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=REPO)

LINKS_SOURCE = """/** Every destination an email can point at. */
export type LinkDefinition = {
  path: string;
  param?: string;
};

export const EMAIL_LINKS = {
  invite: { path: "/", param: "invite" },
  passwordReset: { path: "/reset-password", param: "token" },
  passwordForgot: { path: "/forgot-password" },
} as const satisfies Record<string, LinkDefinition>;

export type EmailLinkName = keyof typeof EMAIL_LINKS;

export function webLinkFor(origin: string, name: "passwordForgot"): string;
export function webLinkFor(origin: string, name: EmailLinkName, value: string): string;
export function webLinkFor(origin: string, name: EmailLinkName, value?: string): string {
  const link = EMAIL_LINKS[name] as LinkDefinition;
  return `${origin}${link.path}${value ?? ""}`;
}
"""

#: Lines of ``EMAIL_LINKS`` in the file above, and of the key the question is about.
EMAIL_LINKS_LINES = (7, 11)
PASSWORD_RESET_LINE = 9

LARGE_ENTRIES = 400


def _large_source() -> str:
    entries = "".join(f'  route{n:03d}: {{ path: "/r/{n:03d}" }},\n' for n in range(LARGE_ENTRIES))
    return f"export const ROUTE_TABLE = {{\n{entries}}} as const;\n"


@pytest.fixture
def contracts(workspace: Workspace, tmp_path: Path) -> Iterator[Workspace]:
    root = tmp_path / "repos"
    repo = root / REPO / "src"
    repo.mkdir(parents=True)
    (repo / "links.ts").write_text(LINKS_SOURCE, encoding="utf-8")
    (repo / "routes.ts").write_text(_large_source(), encoding="utf-8")
    init_git_repository(root / REPO)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    yield workspace


def _declared(response, name: str):
    return next(item for item in response.evidence if item.why == f"defines {name}")


def test_the_tool_descriptions_name_the_handoff() -> None:
    """The sentence an agent reads before choosing a tool, not a field it never sees."""
    assert "declaration true" in SEARCH.description
    assert "target.entity_key" in SEARCH.description
    assert "entity key" in INSPECT.description.lower()
    assert "docs/ARCHITECTURE.md" in INSPECT.description
    assert "inspect its entity key" in INSTRUCTIONS


def test_a_declaration_is_not_source_and_claims_no_line(contracts: Workspace) -> None:
    service = CapabilityService(contracts)
    found = service.search(SearchRequest(question="EMAIL_LINKS", context=CONTEXT))
    declared = _declared(found, "EMAIL_LINKS")
    assert declared.declaration is True
    assert declared.text == "const EMAIL_LINKS"
    assert declared.ref.line_start is None and declared.ref.line_end is None
    assert declared.entity is not None


def test_inspecting_the_declarations_key_supplies_the_whole_definition(
    contracts: Workspace,
) -> None:
    service = CapabilityService(contracts)
    found = service.search(SearchRequest(question="EMAIL_LINKS", context=CONTEXT))
    handle = _declared(found, "EMAIL_LINKS").entity
    assert handle is not None

    inspected = service.inspect(
        InspectRequest(
            context=CONTEXT,
            target=InspectTarget(entity_key=handle.key),
            investigation_id=found.investigation_id,
            context_lines=0,
        )
    )
    start, end = EMAIL_LINKS_LINES
    assert [(r.start, r.end) for r in inspected.coverage.returned] == [(start, end)]
    text = "".join(segment.text for segment in inspected.segments)
    assert 'passwordReset: { path: "/reset-password", param: "token" }' in text
    assert "as const satisfies" in text

    # And the lines are citable: a claim about the key grounds on its own line.
    segment = inspected.segments[0]
    verdict = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="EMAIL_LINKS",
                claims=(
                    AgentClaim(
                        statement="passwordReset links to /reset-password with a token.",
                        citations=(
                            AgentCitation(
                                evidence_id=segment.id,
                                line_start=PASSWORD_RESET_LINE,
                                line_end=PASSWORD_RESET_LINE,
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    assert verdict.verdict is GroundingVerdict.GROUNDED


def test_each_overload_is_its_own_declaration_with_its_own_source(contracts: Workspace) -> None:
    service = CapabilityService(contracts)
    found = service.search(SearchRequest(question="webLinkFor", context=CONTEXT))
    keys = sorted(
        {
            item.entity.key
            for item in found.evidence
            if item.entity and "webLinkFor" in item.entity.key
        }
        | {handle.key for handle in found.entities if "webLinkFor" in handle.key}
    )
    assert len(keys) == 3, keys

    starts = []
    for key in keys:
        inspected = service.inspect(
            InspectRequest(context=CONTEXT, target=InspectTarget(entity_key=key), context_lines=0)
        )
        returned = inspected.coverage.returned
        assert len(returned) == 1
        starts.append(returned[0].start)
        assert "webLinkFor" in inspected.segments[0].text
    assert len(set(starts)) == 3


def test_a_large_definition_is_bounded_and_says_what_it_left_out(contracts: Workspace) -> None:
    service = CapabilityService(contracts)
    found = service.search(SearchRequest(question="ROUTE_TABLE", context=CONTEXT))
    # The declaration's span covers the first chunk's lines, so deduplication keeps
    # the chunk; the exact handle still comes back among the seeds.
    handle = next(h for h in found.entities if h.key.endswith(":ROUTE_TABLE:1"))

    inspected = service.inspect(
        InspectRequest(
            context=CONTEXT,
            target=InspectTarget(entity_key=handle.key),
            budget=EvidenceBudget(max_total_chars=2000),
            context_lines=0,
        )
    )
    assert inspected.coverage.budget_omitted, "the rest of the definition is named, not dropped"
    assert sum(len(segment.text) for segment in inspected.segments) <= 2000
    assert all(segment.ref.line_start is not None for segment in inspected.segments)
