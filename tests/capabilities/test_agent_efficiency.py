"""Workflow regressions: useful first evidence, truthful previews, bounded discovery."""

from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp.server import dispatch
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.http_inventory import DiscoveryBasis, HttpOperationsRequest
from repoatlas.models.search import SearchRequest
from tests.test_http_structural_matching import CLIENT, SERVER, _index


@pytest.fixture
def symbols(tmp_path: Path):
    source = """import { remove } from "./relationships";
export class RosterService {
  remove(id: string) {
    if (!id) throw new Error("missing id");
    return remove(id);
  }
}
export class OtherService {
  remove(id: string) { return id; }
}
"""
    ws, _ = _index(
        tmp_path,
        {
            name: {
                "service.ts": source,
                "relationships.ts": "export function remove(id: string) { return id; }",
            }
            for name in ("one", "two")
        },
    )
    service = CapabilityService(ws)
    yield service, source
    service.close()


def test_qualified_lookup_leads_with_its_definition_and_keeps_repository_alternatives(symbols):
    service, _ = symbols
    result = service.search(
        SearchRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            question="RosterService.remove",
            budget=EvidenceBudget(max_items=2),
        )
    )
    assert len(result.evidence) == 2
    assert {item.ref.repository_slug for item in result.evidence} == {"one", "two"}
    assert all(
        item.entity and ":RosterService.remove:" in item.entity.key for item in result.evidence
    )
    assert all(item.declaration for item in result.evidence)
    assert result.has_more


def test_one_call_preview_supplies_complete_small_definition_with_exact_source_lines(symbols):
    service, source = symbols
    result = service.search(
        SearchRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="one"),
            question="RosterService.remove",
            source_preview=True,
            budget=EvidenceBudget(max_items=1),
        )
    )
    item = result.evidence[0]
    assert not item.declaration
    assert item.ref.line_start is not None and item.ref.line_end is not None
    assert item.text == "\n".join(source.splitlines()[item.ref.line_start - 1 : item.ref.line_end])
    assert "throw new Error" in item.text and "return remove(id)" in item.text
    assert item.preview and not item.preview.omitted_spans


def test_preview_finds_late_match_and_declares_omitted_lines(tmp_path: Path):
    source = (
        "export class Reader {\n"
        + "".join(f"  field{i} = {i};\n" for i in range(70))
        + '  secret = "RARE_BEHAVIOR";\n}\n'
    )
    ws, _ = _index(tmp_path, {"app": {"reader.ts": source}})
    with closing(CapabilityService(ws)) as service:
        result = service.search(
            SearchRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="app"),
                question='"RARE_BEHAVIOR"',
                source_preview=True,
                budget=EvidenceBudget(max_items=1, max_item_chars=120),
            )
        )
        item = result.evidence[0]
        assert "RARE_BEHAVIOR" in item.text
        assert item.preview and item.preview.omitted_spans
        assert item.truncated and len(item.text) <= 120
        assert item.ref.line_start and item.ref.line_end
        assert item.text == "\n".join(
            source.splitlines()[item.ref.line_start - 1 : item.ref.line_end]
        )


def test_unprovable_split_line_is_not_promoted_to_source(tmp_path: Path):
    source = 'export const enormous = "' + ("x" * 20000) + '";\n'
    ws, _ = _index(tmp_path, {"app": {"large.ts": source}})
    with closing(CapabilityService(ws)) as service:
        result = service.search(
            SearchRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="app"),
                question="enormous",
                source_preview=True,
            )
        )
        assert all(item.ref.line_start is None for item in result.evidence)


def test_preview_uses_fitting_match_when_an_earlier_match_exceeds_the_budget(tmp_path: Path):
    source = (
        'export class Reader {\n  large = "RARE_BEHAVIOR'
        + "x" * 400
        + '";\n  small = "RARE_BEHAVIOR";\n}\n'
    )
    ws, _ = _index(tmp_path, {"app": {"reader.ts": source}})
    with closing(CapabilityService(ws)) as service:
        result = service.search(
            SearchRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="app"),
                question='"RARE_BEHAVIOR"',
                source_preview=True,
                budget=EvidenceBudget(max_items=1, max_item_chars=120),
            )
        )
        item = result.evidence[0]
        assert 'small = "RARE_BEHAVIOR"' in item.text
        assert item.ref.line_start == 3
        assert item.preview and item.preview.omitted_spans == ((1, 2),)


@pytest.mark.parametrize(
    "arguments,field",
    [
        ({"context": {"scope": "global"}, "package": "shared-utils"}, "package"),
        ({"context": {"scope": "global", "repositories": ["one"]}}, "context.repositories"),
    ],
)
def test_unknown_filters_refused_before_any_capability_call(arguments, field):
    class Uncalled:
        def dependencies(self, request):
            pytest.fail("invalid filters must never reach retrieval")

    result = dispatch(cast(CapabilityService, Uncalled()), "repoatlas_dependencies", arguments)
    assert result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["error"]["code"] == "INVALID_REQUEST"
    assert field in {
        item["field"] for item in result.structuredContent["error"]["context"]["fields"]
    }


def test_split_repository_discovery_preserves_all_candidates_and_provenance(tmp_path: Path):
    ws, _ = _index(
        tmp_path,
        {
            "frontend": {"client.ts": CLIENT},
            "backend-a": {"server.ts": SERVER},
            "backend-b": {"server.ts": SERVER},
        },
    )
    with closing(CapabilityService(ws)) as service:
        args = dict(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            discover=True,
            method="GET",
            route_prefix="/v1/courses/:id/roster",
            limit=1,
            include_provenance=True,
        )
        rows = []
        evidence = {}
        cursor = None
        investigation_id = None
        while True:
            page = service.http_operations(
                HttpOperationsRequest.model_validate(
                    {**args, "cursor": cursor, "investigation_id": investigation_id}
                )
            )
            investigation_id = page.investigation_id
            assert isinstance(page.basis, DiscoveryBasis)
            assert page.basis.status_scope == "source_repository"
            assert "runtime_dependency" in page.basis.does_not_establish
            assert page.basis.repositories_examined == 3
            rows.extend(page.operations)
            evidence.update({item.id: item for item in page.evidence})
            cursor = page.next_cursor
            if not page.has_more:
                break
        assert len(rows) == 4
        assert {row.repository for row in rows} == {"frontend", "backend-a", "backend-b"}
        for row in rows:
            assert row.evidence_id
            assert evidence[row.evidence_id].ref.repository_slug == row.repository
        caller = next(
            row for row in rows if row.role == "caller" and row.status == "no_indexed_handler"
        )
        assert caller.status == "no_indexed_handler" and not caller.routes
        assert len({row.evidence_id for row in rows}) == 4


@pytest.mark.parametrize("route", [None, "/", "///"])
def test_workspace_discovery_requires_a_focused_route(route):
    with pytest.raises(ValidationError, match="non-root route_prefix"):
        HttpOperationsRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            discover=True,
            route_prefix=route,
        )


def test_preview_grounding_refuses_omitted_lines(tmp_path: Path):
    from repoatlas.models.verification import (
        AgentAnswer,
        AgentCitation,
        AgentClaim,
        VerifyGroundingRequest,
    )

    source = (
        "export class Reader {\n"
        + "".join(f"  field{i} = {i};\n" for i in range(70))
        + '  secret = "RARE_BEHAVIOR";\n}\n'
    )
    ws, _ = _index(tmp_path, {"app": {"reader.ts": source}})
    with closing(CapabilityService(ws)) as service:
        result = service.search(
            SearchRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="app"),
                question='"RARE_BEHAVIOR"',
                source_preview=True,
                budget=EvidenceBudget(max_items=1, max_item_chars=120),
            )
        )
        item = result.evidence[0]
        assert item.preview and item.preview.omitted_spans
        omitted = item.preview.omitted_spans[0][0]

        def verify(line):
            return service.verify_grounding(
                VerifyGroundingRequest(
                    investigation_id=result.investigation_id,
                    answer=AgentAnswer(
                        claims=(
                            AgentClaim(
                                statement="The supplied source contains the behavior.",
                                citations=(
                                    AgentCitation(
                                        evidence_id=item.id, line_start=line, line_end=line
                                    ),
                                ),
                            ),
                        )
                    ),
                )
            )

        assert verify(item.ref.line_start).verdict == "grounded"
        assert verify(omitted).verdict == "ungrounded"


def test_preview_uses_indexed_source_after_worktree_edit(tmp_path: Path):
    ws, root = _index(
        tmp_path, {"app": {"source.ts": 'export function doWork() { return "indexed"; }\n'}}
    )
    (root / "app/source.ts").write_text(
        'export function doWork() { return "unindexed private text"; }\n'
    )
    with closing(CapabilityService(ws)) as service:
        result = service.search(
            SearchRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="app"),
                question="doWork",
                source_preview=True,
            )
        )
        assert any("indexed" in item.text for item in result.evidence)
        assert all("unindexed private text" not in item.text for item in result.evidence)


def test_all_advertised_request_objects_reject_unknown_fields():
    from repoatlas.mcp.tools import TOOLS

    for tool in TOOLS:
        schema = tool.request.model_json_schema()
        for node in [schema, *schema.get("$defs", {}).values()]:
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, (tool.name, node.get("title"))
