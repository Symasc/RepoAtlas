"""GraphQL documents and resolvers bound to the schema their configuration or construction loads.

The guarantees pinned here, on the invented fixtures in ``tests/document_corpus.py``:

* a document is bound only through a configuration's ``documents:`` inputs, to that
  configuration's schema, across a repository boundary if the schema lies across one; every
  selection, argument, enum literal, input field, variable, inline fragment and spread it
  makes is a reference at its own line, and a selection the schema lacks is reported;
* a fragment spliced in with ``${X}`` is followed to the module that declares it; text only
  run time decides is ``document-dynamic`` and states nothing;
* a resolver map is followed from its construction, through imported objects and aliased
  imports, to the declaration implementing each field; every object field is ``resolved``
  or ``default-resolver``, or ``resolver-unknown`` when a map cannot be followed;
* type definitions are followed only when anchored to the file; a path relative to the
  working directory, inline SDL and a library function's namesake are not constructions;
* change impact reaches the documents and resolvers from the schema they are bound to;
* plans write nothing and do not depend on order; incremental equals fresh.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import IndexingSettings, Settings
from repoatlas.graph.graphql_codegen import plan_codegen
from repoatlas.graph.graphql_documents import FAMILY as DOCUMENTS
from repoatlas.graph.graphql_documents import plan_documents
from repoatlas.graph.graphql_resolvers import FAMILY as RESOLVERS
from repoatlas.graph.graphql_resolvers import plan_resolvers
from repoatlas.graph.graphql_schema import symbols_by_file
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import EMBEDDED_DOCUMENT
from repoatlas.parsers.graphql import read_embedded
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, reindex_from_scratch
from tests.document_corpus import API, PY, WEB, repos
from tests.impact_corpus import build, verify_steps

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def decisions(workspace: Workspace, family: str) -> dict[str, str]:
    return {r.subject: r.decision for r in workspace.knowledge.list_link_decisions(family=family)}


def references(workspace: Workspace) -> set[tuple[str, str, str, int]]:
    """(source, target, file asserting it, line) for every reference the global pass made."""
    rows = workspace.database.execute(
        """
        SELECT sf.rel_path || '#' || s.qualified_name AS source,
               df.rel_path || '#' || d.qualified_name AS target,
               f.rel_path AS at, e.line_start AS line
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        JOIN files f ON f.id = e.file_id
        WHERE e.type = 'REFERENCES' AND e.parser = ?
        """,
        (GLOBAL_PARSER,),
    ).fetchall()
    return {(r["source"], r["target"], r["at"], r["line"]) for r in rows}


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

Q = "src/queries/parcels.graphql"
S = "schema/depot.graphql"


def test_each_document_is_decided_by_the_configuration_that_covers_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    assert decisions(workspace, DOCUMENTS) == {
        f"depot-web:{Q}::PackedParcels": "linked",
        f"depot-web:{Q}::ParcelCard": "linked",
        f"depot-web:{Q}::Broken": "document-unresolved",
        "depot-web:src/fragments.ts::ParcelCardFields": "linked",
        "depot-web:src/hooks.ts::WithCard": "linked",
        "depot-web:src/hooks.ts::dynamic": "document-dynamic",
        "depot-web:src/hooks.ts::BROKEN": "document-invalid",
        # excluded by the configuration's negation, and covered by no configuration
        "depot-web:src/ignored/old.ts::Old": "schema-unknown",
        "depot-web:src/lib/shared.ts::SharedFields": "schema-unknown",
        "depot-web:scripts/probe.ts::Probe": "schema-unknown",
        "depot-web:scripts/probe.ts::ProbeFields": "schema-unknown",
        # its fragment is in a covered module only through the substitution that splices it
        "depot-web:src/spliced.ts::WithShared": "linked",
        "depot-web:src/spliced.ts::partial": "document-dynamic",
        # a spread of a fragment no configured document defines
        "depot-web:src/queries/orphan.graphql::UsesProbe": "document-unresolved",
    }
    broken = next(
        r
        for r in workspace.knowledge.list_link_decisions(family=DOCUMENTS)
        if r.subject.endswith("::Broken")
    )
    assert json.loads(broken.evidence_json)["unresolved"] == ["Parcel.nope"]


def test_every_selection_is_a_reference_at_its_own_line(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = {(s, t, line) for s, t, at, line in references(workspace) if at == Q}
    packed = f"{Q}#PackedParcels"
    assert {
        (packed, f"{S}#Stage", 1),  # the variable's type
        (packed, f"{S}#Stage.SENT", 1),  # its default
        (packed, f"{S}#Query.parcels", 2),
        (packed, f"{S}#Query.parcels(stage:)", 2),
        (packed, f"{S}#Stage.PACKED", 2),  # an enum literal argument
        (packed, f"{S}#Query.parcels(filter:)", 2),
        (packed, f"{S}#ParcelFilter.stage", 2),  # an input object's field
        (packed, f"{S}#Stage.LOST", 2),  # ...and the literal it is given
        (packed, f"{S}#Parcel.id", 3),
        (packed, f"{Q}#ParcelCard", 4),  # the fragment spread
        (packed, f"{S}#Parcel", 5),  # the inline fragment's condition
        (packed, f"{S}#Parcel.weight", 6),
        (f"{Q}#ParcelCard", f"{S}#Parcel", 10),
        (f"{Q}#ParcelCard", f"{S}#Parcel.label", 11),
        (f"{Q}#ParcelCard", f"{S}#Parcel.stage", 12),
        (f"{Q}#Broken", f"{S}#Query.parcels", 15),
    } == found


def test_an_embedded_document_cites_its_own_lines_and_follows_a_spliced_fragment(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = {(s, t, at, line) for s, t, at, line in references(workspace)}
    assert (
        "src/hooks.ts#WithCard",
        "src/fragments.ts#ParcelCardFields",
        "src/hooks.ts",
        6,
    ) in found
    assert ("src/hooks.ts#WithCard", f"{S}#Query.parcel(id:)", "src/hooks.ts", 5) in found
    assert (
        "src/fragments.ts#ParcelCardFields",
        f"{S}#Parcel.stage",
        "src/fragments.ts",
        5,
    ) in found
    assert (
        "src/spliced.ts#WithShared",
        "src/lib/shared.ts#SharedFields",
        "src/spliced.ts",
        4,
    ) in found
    # Text only run time decides states nothing, not even its operation's name.
    unbound = ("src/hooks.ts#Dyn", "src/spliced.ts#Partial", "scripts/", "src/ignored/")
    assert not [r for r in found if r[0].startswith(unbound)]
    names = {
        r["qualified_name"]
        for r in workspace.database.execute(
            "SELECT e.qualified_name FROM entities e JOIN files f ON f.id = e.file_id "
            "WHERE f.rel_path = 'src/hooks.ts'"
        ).fetchall()
    }
    assert "Dyn" not in names
    spliced = {
        r["qualified_name"]
        for r in workspace.database.execute(
            "SELECT e.qualified_name FROM entities e JOIN files f ON f.id = e.file_id "
            "WHERE f.rel_path = 'src/spliced.ts'"
        ).fetchall()
    }
    assert "Partial" not in spliced


def test_a_document_named_like_a_declaration_takes_nothing_from_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    """``WithCard`` is a query and a function in one file: the function keeps its calls,
    and nothing of the host language resolves to the query."""
    build(workspace, tmp_path, repos())

    rows = workspace.database.execute(
        """
        SELECT s.qualified_name AS source, s.meta_json AS source_meta,
               d.qualified_name AS target, e.type
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = s.file_id
        WHERE f.rel_path = 'src/hooks.ts' AND e.type IN ('CALLS', 'USES', 'REFERENCES')
        """
    ).fetchall()
    calls = {
        (r["source"], json.loads(r["source_meta"] or "{}").get("kind"), r["target"], r["type"])
        for r in rows
    }
    assert ("WithCard", "function", "helper", "CALLS") in calls
    assert not [c for c in calls if c[1] == EMBEDDED_DOCUMENT and c[3] != "REFERENCES"]


def test_embedded_documents_cut_no_chunk() -> None:
    parsed = TypeScriptParser().parse(Path("src/hooks.ts"), WEB["src/hooks.ts"])
    embedded = [s for s in parsed.symbols if s.meta.get("kind") == EMBEDDED_DOCUMENT]
    assert [(s.qualified_name, s.line_start, s.line_end, s.parent) for s in embedded] == [
        ("WithCard", 4, 8, "WITH_CARD")
    ]
    without = tuple(s for s in parsed.symbols if s.meta.get("kind") != EMBEDDED_DOCUMENT)
    settings = IndexingSettings()

    assert chunk_content(WEB["src/hooks.ts"], parsed.symbols, settings) == chunk_content(
        WEB["src/hooks.ts"], without, settings
    )


def test_an_embedded_reader_maps_lines_and_tells_sdl_from_a_broken_document() -> None:
    read = read_embedded("\n  query Q { a }\n", first_line=10, owner="Q_DOC")
    assert read is not None
    assert [(s.qualified_name, s.line_start, s.parent) for s in read.symbols] == [
        ("Q", 11, "Q_DOC")
    ]
    sdl = read_embedded("type T { a: Int }", first_line=1, owner="")
    assert sdl is not None and sdl.type_system and sdl.claims == ()
    assert read_embedded("query {", first_line=1, owner="") is None


# ---------------------------------------------------------------------------
# resolvers
# ---------------------------------------------------------------------------


def test_every_object_field_is_resolved_defaulted_or_unknown(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    assert decisions(workspace, RESOLVERS) == {
        "depot-api:src/server.ts::schema": "linked",
        "depot-api:src/server.ts::schema::Ghost.boo": "resolver-orphan",
        f"depot-api:{S}::Query.parcels": "resolved",
        f"depot-api:{S}::Query.parcel": "resolved",
        f"depot-api:{S}::Mutation.ship": "resolved",
        f"depot-api:{S}::Parcel.stage": "resolved",
        f"depot-api:{S}::Parcel.id": "default-resolver",
        f"depot-api:{S}::Parcel.label": "default-resolver",
        f"depot-api:{S}::Parcel.weight": "default-resolver",
        "depot-api:src/legacy-server.ts::legacy": "schema-unsupported",
        "depot-api:src/inline.ts::inline": "schema-unsupported",
        "depot-api:src/packaged.ts::packaged": "resolvers-unresolved",
        "depot-api:schema/extra.graphql::Crate.id": "resolver-unknown",
        "depot-api:schema/extra.graphql::Query.crates": "resolver-unknown",
        "depot-py:depot_py/app.py::schema": "linked",
        "depot-py:depot_py/schema/main.graphql::Query.crates": "resolved",
        "depot-py:depot_py/schema/main.graphql::Query.crate": "resolved",
        "depot-py:depot_py/schema/main.graphql::Crate.size": "resolved",
        "depot-py:depot_py/schema/main.graphql::Crate.id": "default-resolver",
        "depot-py:depot_py/cwd.py::schema": "schema-unsupported",
    }


def test_a_resolver_is_the_declaration_that_implements_the_field(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = {
        (s, t, at, line)
        for s, t, at, line in references(workspace)
        if not at.startswith(("src/queries", "src/hooks", "src/fragments", "src/spliced"))
    }
    assert found == {
        # an imported function, through an imported object
        ("src/parcels.ts#listParcels", f"{S}#Query.parcels", "src/resolvers/query.ts", 3),
        # an inline function: the object holding it
        ("src/resolvers/query.ts#queryResolvers", f"{S}#Query.parcel", "src/resolvers/query.ts", 4),
        # an aliased import (`ship as dispatch`)
        ("src/parcels.ts#ship", f"{S}#Mutation.ship", "src/resolvers/index.ts", 6),
        ("src/resolvers/index.ts#resolvers", f"{S}#Parcel.stage", "src/resolvers/index.ts", 9),
        # a reference resolver is the type's
        ("src/resolvers/index.ts#resolvers", f"{S}#Parcel", "src/resolvers/index.ts", 10),
        # Ariadne: an aliased import, a decorated function, a lambda on an ObjectType
        (
            "depot_py/crates.py#list_crates",
            "depot_py/schema/main.graphql#Query.crates",
            "depot_py/app.py",
            10,
        ),
        (
            "depot_py/app.py#resolve_crate",
            "depot_py/schema/main.graphql#Query.crate",
            "depot_py/app.py",
            15,
        ),
        ("depot_py/app.py#crate", "depot_py/schema/main.graphql#Crate.size", "depot_py/app.py", 12),
    }


def test_construction_facts_are_read_only_from_the_library_and_anchored_paths() -> None:
    def facts(path: str, content: str, parser: Any) -> list[dict[str, str]]:
        parsed = parser.parse(Path(path), content)
        return [
            dict(r.meta)
            for r in parsed.references
            if r.meta.get(PENDING_FACT) == "graphql-schema-construction"
        ]

    server = facts("src/server.ts", API["src/server.ts"], TypeScriptParser())
    assert [json.loads(f["type_defs"]) for f in server] == [[{"file": "../schema/depot.graphql"}]]
    assert json.loads(server[0]["resolvers"]) == [
        {"ref": {"module": "./resolvers", "name": "resolvers"}}
    ]
    assert facts("src/fake.ts", API["src/fake.ts"], TypeScriptParser()) == []
    [legacy] = facts("src/legacy-server.ts", API["src/legacy-server.ts"], TypeScriptParser())
    assert json.loads(legacy["type_defs"]) == [{"unsupported": "cwd-relative"}]
    [app] = facts("depot_py/app.py", PY["depot_py/app.py"], PythonParser())
    assert json.loads(app["type_defs"]) == [{"tree": "schema"}]
    assert json.loads(app["bindables"]) == [{"local": "query"}, {"local": "crate"}]


# ---------------------------------------------------------------------------
# change impact
# ---------------------------------------------------------------------------


def test_a_schema_change_reaches_the_documents_and_resolvers_bound_to_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    response = CapabilityService(workspace).impact(
        ImpactRequest(
            context=GLOBAL,
            seed=f"sym:depot-api:{S}:Parcel.stage:8",
            presentation=Presentation.FULL,
        )
    )
    verify_steps(workspace, response)

    reached = {c.entity for c in response.candidates}
    assert {
        f"sym:depot-web:{Q}:ParcelCard:10",
        f"sym:depot-web:{Q}:PackedParcels:1",  # through the fragment it spreads
        "sym:depot-web:src/fragments.ts:ParcelCardFields:3",
        "sym:depot-web:src/hooks.ts:WithCard:4",  # through the fragment it splices in
        "sym:depot-api:src/resolvers/index.ts:resolvers:3",
    } <= reached
    # Never a document no configuration binds, or one only run time decides.
    assert not [e for e in reached if ":scripts/" in e or ":src/ignored/" in e]
    assert not [e for e in reached if e.endswith((":dynamic:17", ":Dyn:18"))]


def test_a_resolver_seed_reaches_its_users_and_a_field_reaches_its_resolver(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=f"sym:depot-api:{S}:Query.parcels:17")
    )
    verify_steps(workspace, response)

    reached = {c.entity for c in response.candidates}
    assert "sym:depot-api:src/parcels.ts:listParcels:1" in reached
    assert "sym:depot-api:src/resolvers/query.ts:queryResolvers:2" in reached
    assert f"sym:depot-web:{Q}:PackedParcels:1" in reached


# ---------------------------------------------------------------------------
# incremental equals fresh; plans are pure and ordered
# ---------------------------------------------------------------------------


def test_a_renamed_field_unbinds_what_selected_it_exactly_as_a_fresh_index_does(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    schema = root / "depot-api" / S
    schema.write_text(schema.read_text().replace("  label: String!\n", "  title: String!\n"))
    sync(workspace, workspace.resolver().resolve(root / "depot-api"))

    found = decisions(workspace, DOCUMENTS)
    assert found[f"depot-web:{Q}::ParcelCard"] == "document-unresolved"
    assert found["depot-web:src/fragments.ts::ParcelCardFields"] == "document-unresolved"
    assert decisions(workspace, RESOLVERS)[f"depot-api:{S}::Parcel.title"] == "default-resolver"
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_removing_the_documents_input_leaves_every_document_unbound(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    config = root / "depot-web" / "codegen.yml"
    config.write_text(config.read_text().replace("  - src/**/*.ts\n", ""))
    sync(workspace, workspace.resolver().resolve(root / "depot-web"))

    found = decisions(workspace, DOCUMENTS)
    assert found["depot-web:src/hooks.ts::WithCard"] == "schema-unknown"
    assert found[f"depot-web:{Q}::PackedParcels"] == "linked"
    assert not [r for r in references(workspace) if r[2] == "src/hooks.ts"]
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_the_plans_are_the_same_in_any_order_and_write_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    knowledge = workspace.knowledge
    files = knowledge.list_file_rows()
    symbols = knowledge.list_symbols()
    index = ResolutionIndex.build(files, symbols)
    by_id = {f.file_id: f for f in files}
    claims = knowledge.list_unresolved_refs(exclude_uses=True)

    def role(claim: Any) -> str:
        return str(json.loads(claim.meta_json or "{}").get(PENDING_FACT, ""))

    before = fact_snapshot(workspace)

    def plan(order: list[Any], ordered_symbols: list[Any]) -> tuple[Any, Any]:
        codegen = plan_codegen(
            [c for c in order if role(c) == "graphql-codegen-output"],
            index,
            by_id,
            ordered_symbols,
            producer=GLOBAL_PARSER,
        )
        by_file = symbols_by_file(ordered_symbols)
        return (
            plan_documents(
                [c for c in order if role(c) == "graphql-document"],
                codegen.projects,
                index,
                by_id,
                by_file,
                producer=GLOBAL_PARSER,
            ),
            plan_resolvers(
                [
                    c
                    for c in order
                    if role(c)
                    in {"graphql-schema-construction", "ts-object-shape", "graphql-bindable"}
                ],
                index,
                by_id,
                by_file,
                producer=GLOBAL_PARSER,
            ),
        )

    first = plan(list(claims), list(symbols))
    shuffler = random.Random(11)  # noqa: S311 - an order to try, not a secret
    for _ in range(3):
        order, ordered_symbols = list(claims), list(symbols)
        shuffler.shuffle(order)
        shuffler.shuffle(ordered_symbols)
        assert plan(order, ordered_symbols) == first

    assert fact_snapshot(workspace) == before
    assert first[0].edges and first[1].edges


@pytest.mark.parametrize("content", ["gql`query {`", "gql`query Q { a(x: ${y}) }`"])
def test_a_broken_or_dynamic_template_never_raises(content: str) -> None:
    parsed = TypeScriptParser().parse(Path("a.ts"), f"const y = 1;\nexport const D = {content};\n")
    kinds = {
        r.meta.get("document")
        for r in parsed.references
        if r.meta.get(PENDING_FACT) == "graphql-document"
    }
    assert kinds <= {"unparsed", "dynamic"} and kinds
