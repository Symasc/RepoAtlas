"""The GraphQL contract as first written for master (d8d7a39), held by the merged parser.

These are that commit's tests, one for one, each pinning the same guarantee in the
spelling the merged implementation uses: an argument is ``Type.field(arg:)``, the schema
definition is ``schema``, and a claim is a ``pending_fact`` (``graphql-type`` with its
reason, ``graphql-document`` with what it selects) rather than a ``graphql-fact`` key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.discovery.languages import detect_language
from repoatlas.models.parsing import PARSE_BOUND_EXCEEDED, SYNTAX_ERROR
from repoatlas.parsers.graphql import GRAPHQL_DOCUMENT, GRAPHQL_TYPE, GraphQLParser
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository


@pytest.mark.parametrize("extension", ["graphql", "gql", "graphqls"])
def test_graphql_dispatch(extension: str) -> None:
    path = Path(f"contract.{extension}")
    assert isinstance(ParserRegistry().for_path(path), GraphQLParser)
    assert detect_language(str(path)) == "GraphQL"


@pytest.mark.parametrize(("enum", "member"), [("Phase", "ENABLED"), ("Condition", "AVAILABLE")])
def test_schema_coordinates_and_type_references_are_generalized(enum: str, member: str) -> None:
    source = (
        f"enum {enum} {{ {member} DISABLED }}\n"
        f"input Filter {{ phases: [{enum}!] }}\n"
        f"type Item {{ phase: {enum}! }}\n"
        "type Query { items(filter: Filter): [Item!]! }\n"
    )
    result = GraphQLParser().parse(Path("contract.graphql"), source)
    assert not result.diagnostics
    names = {symbol.qualified_name for symbol in result.symbols}
    assert {
        enum,
        f"{enum}.{member}",
        "Filter.phases",
        "Item.phase",
        "Query.items(filter:)",
    } <= names
    references = {(ref.source_symbol, ref.target_key) for ref in result.references}
    assert ("Filter.phases", enum) in references
    assert ("Item.phase", enum) in references
    assert ("Query.items(filter:)", "Filter") in references
    field = next(symbol for symbol in result.symbols if symbol.qualified_name == "Item.phase")
    assert field.line_start == field.line_end == 3


def test_documents_remain_unresolved_without_schema_binding() -> None:
    result = GraphQLParser().parse(
        Path("operation.graphql"),
        "query Find($filter: Filter) { alias: items(filter: $filter) { ...Details } }\n"
        "fragment Details on Item { phase }",
    )
    assert len(result.references) == 2
    assert all(ref.meta[PENDING_FACT] == GRAPHQL_DOCUMENT for ref in result.references)
    operation = result.references[0]
    assert [v["name"] for v in json.loads(operation.meta["variables"])] == ["filter"]
    [items] = json.loads(operation.meta["selections"])
    assert (items["field"], items["alias"]) == ("items", "alias")
    assert items["selections"] == [{"spread": "Details", "line": 1}]


def test_external_types_remain_unresolved_without_name_guessing() -> None:
    result = GraphQLParser().parse(Path("part.graphql"), "extend type Item { phase: Phase }")
    phase = next(ref for ref in result.references if ref.target_key == "Phase")
    assert phase.meta == {PENDING_FACT: GRAPHQL_TYPE, "reason": "external"}
    # Nothing in the file is linked here: neither the extended type nor the field's type.
    assert all(PENDING_FACT in ref.meta for ref in result.references)


def test_duplicate_types_do_not_choose_the_last_declaration() -> None:
    result = GraphQLParser().parse(
        Path("contract.graphql"),
        "enum Phase { ENABLED }\nenum Phase { OTHER }\ntype Item { phase: Phase }",
    )
    reference = next(ref for ref in result.references if ref.source_symbol == "Item.phase")
    assert reference.meta == {PENDING_FACT: GRAPHQL_TYPE, "reason": "duplicate"}


def test_directive_names_do_not_collide_with_schema_types() -> None:
    result = GraphQLParser().parse(
        Path("contract.graphql"),
        "directive @Phase(condition: Phase) on FIELD\n"
        "enum Phase { ENABLED }\nschema { query: Root }\ntype Root { phase: Phase }",
    )
    assert {"@Phase", "@Phase(condition:)", "schema"} <= {
        symbol.qualified_name for symbol in result.symbols
    }
    assert any(
        ref.source_symbol == "schema" and ref.target_key == "Root" for ref in result.references
    )
    assert all(not ref.meta for ref in result.references)


def test_parse_error_does_not_echo_source() -> None:
    result = GraphQLParser().parse(Path("broken.graphql"), "type PrivateName { secret: }")
    assert not result.symbols
    assert result.diagnostics[0].message == SYNTAX_ERROR
    assert result.diagnostics[0].line == 1
    assert "PrivateName" not in result.model_dump_json()


def test_token_limit_is_bounded_and_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("repoatlas.parsers.graphql.MAX_TOKENS", 2)
    result = GraphQLParser().parse(Path("contract.graphql"), "enum Phase { ENABLED }")
    assert not result.symbols
    assert result.diagnostics[0].message == PARSE_BOUND_EXCEEDED


def test_parser_is_deterministic() -> None:
    parser = GraphQLParser()
    source = "enum Phase { ENABLED } type Item { phase: Phase }"
    assert parser.parse(Path("one.graphql"), source) == parser.parse(Path("other.graphql"), source)


def test_indexed_schema_impact_and_deletion(
    workspace: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    from repoatlas.capabilities.service import CapabilityService
    from repoatlas.models.impact import ImpactRequest
    from repoatlas.services.init_service import initialize
    from repoatlas.services.sync_service import sync
    from tests.test_linking import edges

    provider = workspace_root / "schema-provider"
    provider.mkdir()
    schema = provider / "contract.graphql"
    schema.write_text("enum Phase { ENABLED DISABLED }\ntype Item { phase: Phase! }\n")
    (provider / "operation.graphql").write_text("query Read { item { phase } }\n")
    (provider / "unrelated.ts").write_text("export enum Phase { ENABLED = 'ENABLED' }\n")
    init_git_repository(provider)
    initialize(workspace, workspace_root, config_path=tmp_path / "config.yaml")

    found = edges(workspace, repository="schema-provider", type="REFERENCES")
    assert any(row["src_name"] == "phase" and row["dst_name"] == "Phase" for row in found)
    assert all(row["dst_path"] == "contract.graphql" for row in found)
    claims = workspace.knowledge.list_unresolved_refs()
    assert any(
        json.loads(claim.meta_json or "{}").get(PENDING_FACT) == GRAPHQL_DOCUMENT
        for claim in claims
    )

    request = ImpactRequest.model_validate(
        {
            "context": {"scope": "global"},
            "seed": "sym:schema-provider:contract.graphql:Phase:1",
        }
    )
    impact = CapabilityService(workspace).impact(request)
    assert impact.seed.status == "resolved"
    assert any(candidate.name == "phase" for candidate in impact.candidates)
    assert not any(candidate.path == "unrelated.ts" for candidate in impact.candidates)
    # No configuration binds the operation, so the schema reaches nothing in it.
    assert not any(candidate.path == "operation.graphql" for candidate in impact.candidates)

    schema.unlink()
    sync(workspace, workspace.resolver().resolve(workspace_root))
    assert not edges(workspace, repository="schema-provider", type="REFERENCES")
