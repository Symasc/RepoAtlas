"""Configured code generation: which generated declarations come from which schema definitions.

The guarantees pinned here, on the invented fixtures in ``tests/schema_corpus.py``:

* a generated declaration is linked only to definitions in the files its configuration
  loads, across a repository boundary if the configuration crosses one, and never to a
  definition that merely shares its name;
* an enum's members are linked by value, and a schema value the generated enum lacks is
  ``member-stale``;
* every other way an output can fail is a persisted decision with its own name;
* decisions and edges are planned without writing, committed together, and an incremental
  sync leaves exactly what a fresh index writes.
"""

from __future__ import annotations

import json
import random
import socket
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph import linker as linker_module
from repoatlas.graph.globs import compile_glob, match_admitted
from repoatlas.graph.graphql_codegen import FAMILY, plan_codegen
from repoatlas.graph.linker import GLOBAL_PARSER, ReferenceLinker
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.graphql_codegen import GRAPHQL_CODEGEN_OUTPUT
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import EdgeRecord
from tests.conftest import fact_snapshot, reindex_from_scratch
from tests.impact_corpus import build, verify_steps
from tests.schema_corpus import repos

API = "parcel-api:codegen.yml->src/gen/types.ts"
WEB = "parcel-web:codegen.yml->src/"


def decisions(workspace: Workspace) -> dict[str, str]:
    return {
        row.subject: row.decision for row in workspace.knowledge.list_link_decisions(family=FAMILY)
    }


def generates(workspace: Workspace) -> set[tuple[str, str, str, int, str]]:
    """(source, target, edge file, edge line, resolution) for every codegen edge."""
    rows = workspace.database.execute(
        """
        SELECT sf.rel_path || '#' || s.qualified_name AS source,
               df.rel_path || '#' || d.qualified_name AS target,
               f.rel_path AS at, e.line_start AS line, e.resolution AS resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        JOIN files f ON f.id = e.file_id
        WHERE e.type = 'GENERATES' AND e.parser = ?
        """,
        (GLOBAL_PARSER,),
    ).fetchall()
    return {(r["source"], r["target"], r["at"], r["line"], r["resolution"]) for r in rows}


def pairs(workspace: Workspace) -> set[tuple[str, str]]:
    return {(source, target) for source, target, *_ in generates(workspace)}


# ---------------------------------------------------------------------------
# what is linked, and what never is
# ---------------------------------------------------------------------------


def test_generated_declarations_link_to_the_definitions_their_configuration_loads(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    linked = pairs(workspace)
    assert {
        ("schema/parcels.graphql#Stage", "src/gen/types.ts#Stage"),
        ("schema/parcels.graphql#Stage.PACKED", "src/gen/types.ts#Stage.Packed"),
        ("schema/parcels.graphql#Stage.SENT", "src/gen/types.ts#Stage.Sent"),
        ("schema/parcels.graphql#Stage.LOST", "src/gen/types.ts#Stage.Lost"),
        ("schema/parcels.graphql#Parcel", "src/gen/types.ts#Parcel"),
        ("schema/parcels.graphql#ParcelFilter", "src/gen/types.ts#ParcelFilter"),
        ("schema/parcels.graphql#Query", "src/gen/types.ts#Query"),
    } <= linked
    # Across the repository boundary, from the same definitions.
    assert ("schema/parcels.graphql#Stage", "src/gen.ts#Stage") in linked
    assert ("schema/parcels.graphql#Stage.PACKED", "src/gen.ts#Stage.Packed") in linked
    # The unloaded duplicate and the hand-written enum of the same name: never.
    assert not [p for p in linked if p[0].startswith("schema/legacy.graphql")]
    assert not [p for p in linked if p[1].startswith("src/ui/")]
    # A scalar is declared as an entry of a map, not as a declaration.
    assert not [p for p in linked if p[0].endswith("#Weight")]


def test_an_edge_cites_the_generated_lines_and_records_what_it_rests_on(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = {
        (source, target): (at, line, json.loads(resolution)[FAMILY])
        for source, target, at, line, resolution in generates(workspace)
    }
    at, line, record = found[("schema/parcels.graphql#Stage.SENT", "src/gen/types.ts#Stage.Sent")]
    assert (at, line) == ("src/gen/types.ts", 5)
    assert record == {
        "config": "parcel-api/codegen.yml:3",
        "schema": "parcel-api/schema/parcels.graphql:4",
        "output": "parcel-api/src/gen/types.ts",
        "plugin": "typescript",
        "decision_ref": record["decision_ref"],
        "member_value": "SENT",
    }
    crossing = found[("schema/parcels.graphql#Stage", "src/gen.ts#Stage")]
    assert crossing[0] == "src/gen.ts"
    assert crossing[2]["config"] == "parcel-web/codegen.yml:3"
    assert crossing[2]["schema"] == "parcel-api/schema/parcels.graphql:1"


def test_every_decision_is_persisted_under_its_own_name(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = decisions(workspace)
    expected = {
        f"{API}::Stage": "linked",
        f"{API}::Stage.LOST": "linked",
        f"{API}::Parcel": "linked",
        f"{WEB}gen.ts::Stage": "linked",
        f"{WEB}gen.ts::Stage.PACKED": "linked",
        f"{WEB}gen.ts::Stage.LOST": "member-stale",
        f"{WEB}gen.ts::Query": "generated-ambiguous",
        f"{WEB}remote.ts": "schema-remote",
        f"{WEB}coded.ts": "schema-unsupported",
        f"{WEB}absent.ts": "schema-missing",
        f"{WEB}client/": "output-unsupported",
        f"{WEB}never-written.ts": "output-missing",
        f"{WEB}operations.ts": "plugin-unsupported",
        f"{WEB}prefixed.ts": "naming-unsupported",
        f"{WEB}literal.ts::Stage": "linked",
        f"{WEB}literal.ts::Stage.LOST": "member-undeclared",
        f"{WEB}clash.ts::Stage": "schema-conflict",
        f"{WEB}clash.ts::shipping_lane": "naming-unsupported",
        "parcel-web:codegen.yml->${OUT_DIR}/types.ts": "config-unsupported",
    }
    assert {subject: found.get(subject) for subject in expected} == expected
    # The URL's credentials and query are nowhere in what was stored.
    stored = json.dumps([r.model_dump() for r in workspace.knowledge.list_link_decisions()])
    assert "user:pw" not in stored and "key=k" not in stored
    remote = next(
        r for r in workspace.knowledge.list_link_decisions() if r.subject == f"{WEB}remote.ts"
    )
    assert json.loads(remote.evidence_json)["remote"] == ["https://schema.example.invalid/graphql"]


def test_a_renamed_value_is_stale_until_the_output_is_generated_again(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    schema = root / "parcel-api" / "schema" / "parcels.graphql"
    schema.write_text(schema.read_text().replace("  SENT\n", "  SHIPPED\n"))
    sync(workspace, workspace.resolver().resolve(root / "parcel-api"))

    assert decisions(workspace)[f"{API}::Stage.SHIPPED"] == "member-stale"
    assert f"{API}::Stage.SENT" not in decisions(workspace)
    assert ("schema/parcels.graphql#Stage.SENT", "src/gen/types.ts#Stage.Sent") not in pairs(
        workspace
    )
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)

    generated = root / "parcel-api" / "src" / "gen" / "types.ts"
    generated.write_text(generated.read_text().replace("Sent = 'SENT'", "Shipped = 'SHIPPED'"))
    sync(workspace, workspace.resolver().resolve(root / "parcel-api"))

    assert decisions(workspace)[f"{API}::Stage.SHIPPED"] == "linked"
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path, "again")


@pytest.mark.parametrize(
    ("removed", "decision"),
    [
        ("codegen.yml", None),
        ("src/gen/types.ts", "output-missing"),
        ("schema/parcels.graphql", "schema-missing"),
    ],
)
def test_removing_a_link_s_evidence_removes_the_link(
    workspace: Workspace,
    tmp_path: Path,
    settings: Settings,
    removed: str,
    decision: str | None,
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    (root / "parcel-api" / removed).unlink()
    sync(workspace, workspace.resolver().resolve(root / "parcel-api"))

    assert not [p for p in pairs(workspace) if p[1].startswith("src/gen/types.ts")]
    assert decisions(workspace).get(API) == decision
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


# ---------------------------------------------------------------------------
# planning: pure, ordered, and committed whole
# ---------------------------------------------------------------------------


def _inputs(workspace: Workspace) -> tuple[list[Any], ResolutionIndex, dict[int, Any], list[Any]]:
    knowledge = workspace.knowledge
    files = knowledge.list_file_rows()
    symbols = knowledge.list_symbols()
    claims = [
        claim
        for claim in knowledge.list_unresolved_refs(exclude_uses=True)
        if json.loads(claim.meta_json or "{}").get("pending_fact") == GRAPHQL_CODEGEN_OUTPUT
    ]
    return claims, ResolutionIndex.build(files, symbols), {f.file_id: f for f in files}, symbols


def test_the_plan_is_the_same_in_any_order_and_writes_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    claims, index, files, symbols = _inputs(workspace)
    before = fact_snapshot(workspace)
    first = plan_codegen(claims, index, files, symbols, producer=GLOBAL_PARSER)

    shuffler = random.Random(7)  # noqa: S311 - an order to try, not a secret
    for _ in range(3):
        shuffled_claims, shuffled_symbols = list(claims), list(symbols)
        shuffler.shuffle(shuffled_claims)
        shuffler.shuffle(shuffled_symbols)
        again = plan_codegen(
            shuffled_claims, index, files, shuffled_symbols, producer=GLOBAL_PARSER
        )
        assert again == first

    assert fact_snapshot(workspace) == before
    assert first.decisions and first.edges


def test_a_repeated_sync_changes_nothing(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, repos())
    before = fact_snapshot(workspace)

    sync(workspace, workspace.resolver().resolve(tmp_path / "repos" / "parcel-web"), full=True)

    assert fact_snapshot(workspace) == before


def test_a_failed_commit_leaves_the_previous_decisions_and_edges(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, repos())
    before = fact_snapshot(workspace)
    linker = ReferenceLinker(workspace.knowledge)
    plan = linker.prepare_global()

    def broken(rows: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(workspace.knowledge, "replace_link_decisions", broken)
    with pytest.raises(RuntimeError, match="injected"):
        linker.commit_global(plan)

    assert fact_snapshot(workspace) == before


def test_planning_reaches_for_no_network(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, repos())

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    plan = ReferenceLinker(workspace.knowledge).prepare_global()

    assert any(d.decision == "schema-remote" for d in plan.decisions)


def test_only_codegen_claims_reach_the_codegen_planner(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = linker_module.plan_codegen

    def spy(claims, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.extend(json.loads(c.meta_json or "{}").get("pending_fact") for c in claims)
        return original(claims, *args, **kwargs)

    monkeypatch.setattr(linker_module, "plan_codegen", spy)
    build(workspace, tmp_path, repos())

    assert seen and set(seen) == {GRAPHQL_CODEGEN_OUTPUT}


# ---------------------------------------------------------------------------
# globs over admitted files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "matches"),
    [
        ("**/*.graphql", ["a.graphql", "x/a.graphql", "x/y/a.graphql"]),
        ("*.{ts,tsx}", ["a.ts", "a.tsx"]),
        ("schema/**", ["schema/a", "schema/x/y"]),
        ("a?[bc].gql", ["axb.gql", "axc.gql"]),
    ],
)
def test_glob_semantics(pattern: str, matches: list[str]) -> None:
    names = [
        "a.graphql", "x/a.graphql", "x/y/a.graphql", ".h/a.graphql", "x/.a.graphql",
        "a.ts", "a.tsx", "a.js", "x/a.ts", "schema/a", "schema/x/y", "axb.gql", "axc.gql",
        "axd.gql",
    ]  # fmt: skip
    regex = compile_glob(pattern)
    assert regex is not None
    assert [n for n in names if regex.fullmatch(n)] == matches


@pytest.mark.parametrize("pattern", ["@(a|b).gql", "{a,{b,c}}.gql"])
def test_untranslated_glob_syntax_is_declined(pattern: str) -> None:
    assert compile_glob(pattern) is None


def test_a_glob_matches_admitted_files_only_and_may_climb_into_another_repository(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    # On disk, but never admitted: a glob does not list the filesystem.
    (root / "parcel-api" / "schema" / "untracked.graphql").write_text("enum X { A }\n")
    _, index, files, _ = _inputs(workspace)
    origin = next(
        f for f in files.values() if f.repo_slug == "parcel-web" and f.rel_path == "codegen.yml"
    )

    found = match_admitted(index, origin, "../parcel-api/schema/*.graphql")

    assert found is not None
    assert [(f.repo_slug, f.rel_path) for f in found] == [
        ("parcel-api", "schema/legacy.graphql"),
        ("parcel-api", "schema/parcels.graphql"),
    ]
    assert match_admitted(index, origin, "../../outside/*.graphql") == []


# ---------------------------------------------------------------------------
# change impact: definition -> generated declaration -> its users
# ---------------------------------------------------------------------------

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
STAGE = "sym:parcel-api:schema/parcels.graphql:Stage:1"


def _impact(workspace: Workspace, seed: str) -> Any:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=seed, presentation=Presentation.FULL)
    )
    verify_steps(workspace, response)
    return response


def test_a_definition_reaches_what_was_generated_from_it_and_that_code_s_users(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    response = _impact(workspace, STAGE)

    by_entity = {c.entity: c for c in response.candidates}
    generated = by_entity["sym:parcel-api:src/gen/types.ts:Stage:2"]
    assert (str(generated.step.edge), generated.distance) == ("GENERATES", 1)
    assert generated.step.reading == "is generated from"
    assert generated.step.at == "parcel-api/src/gen/types.ts"
    assert generated.step.lines == (2, 6)
    assert generated.step.generation is not None
    assert generated.step.generation.config == "parcel-api/codegen.yml:3"
    assert generated.step.generation.definition == "parcel-api/schema/parcels.graphql:1"
    assert generated.step.generation.member_value is None
    assert "by parcel-api/codegen.yml:3" in generated.chain
    user = by_entity["sym:parcel-api:src/ship.ts:ship:2"]
    assert str(user.step.edge) == "USES"
    assert user.from_ == generated.id
    # Across the repository boundary, on the configuration's evidence.
    crossing = by_entity["sym:parcel-web:src/gen.ts:Stage:1"]
    assert str(crossing.step.edge) == "GENERATES"
    assert crossing.step.generation is not None
    assert crossing.step.generation.config == "parcel-web/codegen.yml:3"
    assert "sym:parcel-web:src/track.ts:LOST_STAGES:2" in by_entity
    # A same-named enum no configuration generates, and its users: never.
    assert not [e for e in by_entity if ":src/ui/" in e or ":schema/legacy.graphql:" in e]


def test_a_value_reaches_the_member_generated_with_its_value(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    response = _impact(workspace, "sym:parcel-api:schema/parcels.graphql:Stage.SENT:4")

    by_entity = {c.entity: c for c in response.candidates}
    member = by_entity["sym:parcel-api:src/gen/types.ts:Stage.Sent:5"]
    assert str(member.step.edge) == "GENERATES"
    assert member.step.generation is not None
    assert member.step.generation.member_value == "SENT"
    assert "sym:parcel-web:src/gen.ts:Stage.Sent:3" in by_entity


def test_an_unloaded_duplicate_reaches_nothing_generated(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    response = _impact(workspace, "sym:parcel-api:schema/legacy.graphql:Stage:1")

    assert not [c for c in response.candidates if str(c.step.edge) == "GENERATES"]


def test_a_generates_edge_without_codegen_evidence_is_never_followed(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Salt records GENERATES too (a rendered file from its template); that is not this
    family's evidence, and the walk must not take it for codegen."""
    build(workspace, tmp_path, repos())
    keys = {
        row["key"]: row["id"]
        for row in workspace.database.execute("SELECT id, key FROM entities").fetchall()
    }
    badge = "sym:parcel-api:src/ui/badge.ts:badge:2"
    workspace.knowledge.add_edges(
        [
            EdgeRecord(
                src_id=keys[STAGE],
                dst_id=keys[badge],
                type=EdgeType.GENERATES,
                parser=GLOBAL_PARSER,
                resolution=json.dumps({"salt": {"template": "x.j2"}}),
            )
        ]
    )

    response = _impact(workspace, STAGE)

    assert badge not in {c.entity for c in response.candidates}
    assert "sym:parcel-api:src/gen/types.ts:Stage:2" in {c.entity for c in response.candidates}
