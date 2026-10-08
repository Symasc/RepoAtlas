"""The GraphQL parser: schema coordinates, same-file type links, claims, bounds.

Every fixture here is invented. The guarantees pinned:

* a definition is named by its schema coordinate and spans exactly its own source;
* a type reference becomes an edge only when the file defines that type exactly once,
  and is otherwise a claim that says why -- never a link to a same-named type elsewhere;
* an operation or fragment is a claim carrying what it selects, whole or not at all;
* a broken, oversized or pathological file is a diagnostic in RepoAtlas's own words,
  and the parser never raises;
* GraphQL claims stay claims through the global pass and never reach the HTTP planner;
* the graphql-core pin, its lockfile hash, the notices and the installed license agree.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path

import graphql
import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.compat import tomllib
from repoatlas.config.settings import Settings
from repoatlas.discovery.languages import detect_language
from repoatlas.graph import linker as linker_module
from repoatlas.graph.linker import ReferenceLinker, plan_repository_http
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import (
    PARSE_BOUND_EXCEEDED,
    PARSE_FAILED,
    SYNTAX_ERROR,
    EntityType,
    ParsedFile,
)
from repoatlas.parsers import graphql as graphql_module
from repoatlas.parsers.graphql import (
    DUPLICATE,
    EXTERNAL,
    GRAPHQL_DOCUMENT,
    GRAPHQL_FACTS,
    GRAPHQL_TYPE,
    GraphQLParser,
)
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, reindex_from_scratch
from tests.impact_corpus import build

ROOT = Path(__file__).resolve().parent.parent
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

SCHEMA = (
    '"""Where a parcel is."""\n'  # 1
    "enum Stage {\n"  # 2
    "  PACKED\n"  # 3
    '  """Left the depot."""\n'  # 4
    '  SENT @deprecated(reason: "use MOVING")\n'  # 5
    "}\n"  # 6
    "interface Node {\n"  # 7
    "  id: ID!\n"  # 8
    "}\n"  # 9
    'type Parcel implements Node @key(fields: "id") {\n'  # 10
    "  id: ID!\n"  # 11
    "  stage: Stage!\n"  # 12
    "  depot: Depot\n"  # 13
    "  history(stage: [Stage!] = [PACKED], first: Int = 10): [Parcel!]!\n"  # 14
    "}\n"  # 15
    "extend enum Stage { LOST }\n"  # 16
    "union Found = Parcel | Depot\n"  # 17
    "input ParcelFilter {\n"  # 18
    "  stage: Stage = PACKED\n"  # 19
    "}\n"  # 20
    "directive @audit(level: Level) repeatable on FIELD_DEFINITION | OBJECT\n"  # 21
    "schema {\n"  # 22
    "  query: Query\n"  # 23
    "}\n"  # 24
    "type Query {\n"  # 25
    "  parcels(filter: ParcelFilter): [Parcel!]!\n"  # 26
    "}\n"  # 27
)

DOCUMENT = (
    "query Shipments($stage: Stage = PACKED, $f: ParcelFilter = {stage: SENT}) {\n"  # 1
    "  parcels(filter: {stage: $stage}) {\n"  # 2
    "    id\n"  # 3
    "    s: stage\n"  # 4
    "    ...Card\n"  # 5
    "    ... on Parcel { depot }\n"  # 6
    "  }\n"  # 7
    "}\n"  # 8
    "fragment Card on Parcel { stage }\n"  # 9
    "{ parcels { id } }\n"  # 10
)


def parse(content: str, name: str = "schema.graphql") -> ParsedFile:
    return GraphQLParser().parse(Path(name), content)


def _local(parsed: ParsedFile) -> set[tuple[str, str, int, int]]:
    return {
        (r.target_key, r.source_symbol or "", r.line_start, r.line_end)
        for r in parsed.references
        if PENDING_FACT not in r.meta
    }


def _claims(parsed: ParsedFile, role: str) -> list:
    return [r for r in parsed.references if r.meta.get(PENDING_FACT) == role]


# ---------------------------------------------------------------------------
# coordinates and spans
# ---------------------------------------------------------------------------


def test_every_definition_is_named_by_its_schema_coordinate() -> None:
    parsed = parse(SCHEMA)

    assert parsed.diagnostics == ()
    assert [
        (s.kind, s.qualified_name, s.line_start, s.line_end, s.meta["graphql"], s.parent)
        for s in parsed.symbols
    ] == [
        (EntityType.CLASS, "Stage", 1, 6, "enum", None),
        (EntityType.SYMBOL, "Stage.PACKED", 3, 3, "enum-value", "Stage"),
        (EntityType.SYMBOL, "Stage.SENT", 4, 5, "enum-value", "Stage"),
        (EntityType.CLASS, "Node", 7, 9, "interface", None),
        (EntityType.SYMBOL, "Node.id", 8, 8, "field", "Node"),
        (EntityType.CLASS, "Parcel", 10, 15, "object", None),
        (EntityType.SYMBOL, "Parcel.id", 11, 11, "field", "Parcel"),
        (EntityType.SYMBOL, "Parcel.stage", 12, 12, "field", "Parcel"),
        (EntityType.SYMBOL, "Parcel.depot", 13, 13, "field", "Parcel"),
        (EntityType.SYMBOL, "Parcel.history", 14, 14, "field", "Parcel"),
        (EntityType.SYMBOL, "Parcel.history(stage:)", 14, 14, "argument", "Parcel.history"),
        (EntityType.SYMBOL, "Parcel.history(first:)", 14, 14, "argument", "Parcel.history"),
        (EntityType.CLASS, "extend Stage", 16, 16, "enum", None),
        (EntityType.SYMBOL, "Stage.LOST", 16, 16, "enum-value", "extend Stage"),
        (EntityType.CLASS, "Found", 17, 17, "union", None),
        (EntityType.CLASS, "ParcelFilter", 18, 20, "input", None),
        (EntityType.SYMBOL, "ParcelFilter.stage", 19, 19, "input-field", "ParcelFilter"),
        (EntityType.SYMBOL, "@audit", 21, 21, "directive", None),
        (EntityType.SYMBOL, "@audit(level:)", 21, 21, "argument", "@audit"),
        (EntityType.SYMBOL, "schema", 22, 24, "schema", None),
        (EntityType.CLASS, "Query", 25, 27, "object", None),
        (EntityType.SYMBOL, "Query.parcels", 26, 26, "field", "Query"),
        (EntityType.SYMBOL, "Query.parcels(filter:)", 26, 26, "argument", "Query.parcels"),
    ]


def test_a_definition_records_how_it_is_typed_and_what_is_applied_to_it() -> None:
    symbols = {s.qualified_name: s for s in parse(SCHEMA).symbols}

    assert symbols["Parcel.history"].meta == {
        "graphql": "field",
        "type_ref": "[Parcel!]!",
        "named_type": "Parcel",
    }
    assert symbols["Parcel.history"].signature == (
        "history(stage: [Stage!] = [PACKED], first: Int = 10): [Parcel!]!"
    )
    assert symbols["Parcel.history(stage:)"].meta == {
        "graphql": "argument",
        "type_ref": "[Stage!]",
        "named_type": "Stage",
        "default": "[PACKED]",
    }
    assert symbols["ParcelFilter.stage"].meta["default"] == "PACKED"
    assert json.loads(symbols["Parcel"].meta["directives"]) == [
        {"name": "key", "arguments": {"fields": '"id"'}}
    ]
    assert json.loads(symbols["Stage.SENT"].meta["directives"]) == [
        {"name": "deprecated", "arguments": {"reason": '"use MOVING"'}}
    ]
    assert symbols["extend Stage"].meta == {"graphql": "enum", "extension": "true"}
    assert symbols["Parcel"].signature == "type Parcel implements Node"
    assert symbols["@audit"].meta == {
        "graphql": "directive",
        "locations": "FIELD_DEFINITION,OBJECT",
        "repeatable": "true",
    }
    assert json.loads(symbols["schema"].meta["roots"]) == {"query": "Query"}


def test_a_span_ends_where_the_definition_ends_not_where_its_last_token_starts() -> None:
    """A block string's token sits on the line it opens; the definition runs to its close."""
    parsed = parse('type Box {\n  size: Int @note(text: """\n  big\n  """)\n  weight: Int\n}\n')

    spans = {s.qualified_name: (s.line_start, s.line_end) for s in parsed.symbols}
    assert spans == {"Box": (1, 6), "Box.size": (2, 4), "Box.weight": (5, 5)}


def test_crlf_and_a_byte_order_mark_keep_the_line_model() -> None:
    parsed = parse("﻿enum Mode {\r\n  ON\r\n  OFF\r\n}\r\n")

    assert [(s.qualified_name, s.line_start, s.line_end) for s in parsed.symbols] == [
        ("Mode", 1, 4),
        ("Mode.ON", 2, 2),
        ("Mode.OFF", 3, 3),
    ]


# ---------------------------------------------------------------------------
# type references: linked here, or claimed with a reason
# ---------------------------------------------------------------------------


def test_a_type_this_file_defines_once_is_linked_here() -> None:
    parsed = parse(SCHEMA)

    assert _local(parsed) == {
        ("Node", "Parcel", 10, 10),
        ("Stage", "Parcel.stage", 12, 12),
        ("Parcel", "Parcel.history", 14, 14),
        ("Stage", "Parcel.history(stage:)", 14, 14),
        ("Stage", "extend Stage", 16, 16),
        ("Parcel", "Found", 17, 17),
        ("Stage", "ParcelFilter.stage", 19, 19),
        ("Query", "schema", 23, 23),
        ("Parcel", "Query.parcels", 26, 26),
        ("ParcelFilter", "Query.parcels(filter:)", 26, 26),
    }
    # The specification's scalars are no references at all.
    assert not {r.target_key for r in parsed.references} & graphql_module.BUILTIN_SCALARS


def test_a_type_defined_elsewhere_is_a_claim_saying_so() -> None:
    claims = _claims(parse(SCHEMA), GRAPHQL_TYPE)

    assert sorted(
        (c.target_key, c.source_symbol, c.line_start, c.meta["reason"]) for c in claims
    ) == [
        ("Depot", "Found", 17, EXTERNAL),
        ("Depot", "Parcel.depot", 13, EXTERNAL),
        ("Level", "@audit(level:)", 21, EXTERNAL),
    ]


def test_a_name_held_twice_is_a_claim_and_never_a_link() -> None:
    """Two definitions, or a definition and an operation of the same name: which one a
    reference means is not this parser's to decide."""
    parsed = parse(
        "type Box { inner: Box }\n"
        "type Box { other: Int }\n"
        "type Crate { size: Int }\n"
        "query Crate { size }\n"
        "type Shelf { crate: Crate box: Box }\n"
    )

    assert _local(parsed) == set()
    assert sorted(
        (c.target_key, c.source_symbol, c.meta["reason"]) for c in _claims(parsed, GRAPHQL_TYPE)
    ) == [
        ("Box", "Box.inner", DUPLICATE),
        ("Box", "Shelf.box", DUPLICATE),
        ("Crate", "Shelf.crate", DUPLICATE),
    ]


_RENAMES = {"Stage": "Lifecycle", "PACKED": "OPEN", "Parcel": "Item", "stage": "status"}
_RENAMED = re.compile(r"\b(" + "|".join(_RENAMES) + r")\b")


def _rename(text: str) -> str:
    return _RENAMED.sub(lambda match: _RENAMES[match.group(1)], text)


def test_renamed_identities_parse_to_the_same_facts_renamed() -> None:
    """Nothing here depends on what anything is called."""
    original = parse(SCHEMA + DOCUMENT)
    renamed = parse(_rename(SCHEMA + DOCUMENT))

    assert _rename(original.model_dump_json()) == renamed.model_dump_json()


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def test_an_operation_or_fragment_is_a_claim_carrying_what_it_selects() -> None:
    parsed = parse(DOCUMENT, "shipments.gql")

    assert [(s.qualified_name, s.line_start, s.line_end, s.meta) for s in parsed.symbols] == [
        ("Shipments", 1, 8, {"graphql": "operation", "operation": "query"}),
        ("Card", 9, 9, {"graphql": "fragment", "on": "Parcel"}),
    ]
    assert parsed.symbols[0].signature == "query Shipments($stage: Stage, $f: ParcelFilter)"
    # A document defines no type, so it makes no type claims.
    assert _claims(parsed, GRAPHQL_TYPE) == []

    operation, fragment, anonymous = _claims(parsed, GRAPHQL_DOCUMENT)
    assert (operation.target_key, operation.source_symbol, operation.line_start) == (
        "query",
        "Shipments",
        1,
    )
    assert operation.line_end == 8
    assert {k: v for k, v in operation.meta.items() if k not in {"selections", "variables"}} == {
        PENDING_FACT: GRAPHQL_DOCUMENT,
        "document": "operation",
        "operation": "query",
        "name": "Shipments",
    }
    assert json.loads(operation.meta["variables"]) == [
        {
            "name": "stage",
            "line": 1,
            "type_ref": "Stage",
            "named_type": "Stage",
            "default": {"enum": "PACKED"},
        },
        {
            "name": "f",
            "line": 1,
            "type_ref": "ParcelFilter",
            "named_type": "ParcelFilter",
            "default": {"object": [{"name": "stage", "value": {"enum": "SENT"}}]},
        },
    ]
    assert json.loads(operation.meta["selections"]) == [
        {
            "field": "parcels",
            "line": 2,
            "arguments": [
                {
                    "name": "filter",
                    "line": 2,
                    "value": {"object": [{"name": "stage", "value": {"variable": "stage"}}]},
                }
            ],
            "selections": [
                {"field": "id", "line": 3},
                {"field": "stage", "alias": "s", "line": 4},
                {"spread": "Card", "line": 5},
                {"on": "Parcel", "line": 6, "selections": [{"field": "depot", "line": 6}]},
            ],
        }
    ]

    assert (fragment.target_key, fragment.source_symbol, fragment.line_start) == (
        "Parcel",
        "Card",
        9,
    )
    assert fragment.meta["document"] == "fragment" and fragment.meta["on"] == "Parcel"
    assert "variables" not in fragment.meta
    assert json.loads(fragment.meta["selections"]) == [{"field": "stage", "line": 9}]

    # An anonymous operation has no symbol: its claim belongs to the file.
    assert (anonymous.target_key, anonymous.source_symbol, anonymous.line_start) == (
        "query",
        None,
        10,
    )
    assert "name" not in anonymous.meta


@pytest.mark.parametrize(("nodes", "depth"), [(3, 64), (1_024, 2)], ids=["nodes", "depth"])
def test_a_document_past_its_bound_keeps_no_selections_at_all(
    monkeypatch: pytest.MonkeyPatch, nodes: int, depth: int
) -> None:
    monkeypatch.setattr(graphql_module, "MAX_SELECTION_NODES", nodes)
    monkeypatch.setattr(graphql_module, "MAX_DEPTH", depth)

    [claim, *_] = _claims(parse(DOCUMENT, "shipments.gql"), GRAPHQL_DOCUMENT)

    assert claim.meta["bound"] == "exceeded"
    assert "selections" not in claim.meta and "variables" not in claim.meta
    assert claim.meta["name"] == "Shipments"


def test_a_long_literal_is_kept_as_a_literal_without_its_text() -> None:
    long = "x" * 300
    [claim] = _claims(parse(f'{{ find(a: "{long}", b: "short") }}\n'), GRAPHQL_DOCUMENT)

    [field] = json.loads(claim.meta["selections"])
    assert [argument["value"] for argument in field["arguments"]] == [
        {"literal": None},
        {"literal": '"short"'},
    ]


# ---------------------------------------------------------------------------
# failures: RepoAtlas's own words, and never an exception
# ---------------------------------------------------------------------------


def test_a_syntax_error_is_reported_without_the_source_it_quotes() -> None:
    parsed = parse("type Unclosed_marker_value {\n  id: ID!\n")

    assert parsed.symbols == () and parsed.references == ()
    [diagnostic] = parsed.diagnostics
    assert diagnostic.message == SYNTAX_ERROR
    assert diagnostic.line == 2  # past the end of the file, clamped to its last line
    assert "Unclosed_marker_value" not in diagnostic.model_dump_json()


def test_a_file_past_the_token_bound_is_refused_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graphql_module, "MAX_TOKENS", 20)

    parsed = parse(SCHEMA)

    assert parsed.symbols == () and parsed.references == ()
    [diagnostic] = parsed.diagnostics
    assert diagnostic.message == PARSE_BOUND_EXCEEDED
    assert diagnostic.line is not None and 1 <= diagnostic.line <= 27


def test_the_token_bound_admits_the_densest_schema_a_sync_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One mebibyte, the most a sync reads, of fields with one-letter names and types: the
    densest real schema measured, at about a third of a token per byte. Only the bound is
    under test, so the tree is parsed and then not read."""
    monkeypatch.setattr(graphql_module._Reader, "walk", lambda self, document: None)
    fields = []
    size = len("type T {}\n")
    for i in range(1_000_000):
        field = f" a{i}:I"
        if size + len(field) > 1_048_576:
            break
        fields.append(field)
        size += len(field)
    body = "type T {" + "".join(fields) + "}\n"
    assert len(body.encode()) <= 1_048_576

    parsed = parse(body)

    assert parsed.diagnostics == ()


def test_a_mebibyte_of_one_character_tokens_is_refused() -> None:
    body = "union U = A" + "|A" * 500_000 + "\n"
    assert len(body.encode()) <= 1_048_576

    parsed = parse(body)

    assert [d.message for d in parsed.diagnostics] == [PARSE_BOUND_EXCEEDED]
    assert parsed.symbols == ()


def test_nesting_deeper_than_the_library_reads_is_a_parse_failure() -> None:
    parsed = parse("query {" + " a {" * 5_000 + " b" + " }" * 5_000 + " }\n")

    assert [d.message for d in parsed.diagnostics] == [PARSE_FAILED]
    assert parsed.symbols == () and parsed.references == ()


def test_a_failure_while_reading_the_tree_is_a_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(self: object, document: object) -> None:
        raise RuntimeError("Unclosed_marker_value")

    monkeypatch.setattr(graphql_module._Reader, "walk", broken)

    parsed = parse(SCHEMA)

    assert [d.message for d in parsed.diagnostics] == [PARSE_FAILED]
    assert "Unclosed_marker_value" not in parsed.model_dump_json()


@pytest.mark.parametrize("content", ["", "\n\n", "# only a comment\n", ",,,\n", "﻿"], ids=repr)
def test_a_file_with_no_tokens_is_empty_not_broken(content: str) -> None:
    parsed = parse(content)

    assert (parsed.symbols, parsed.references, parsed.diagnostics) == ((), (), ())


# ---------------------------------------------------------------------------
# registration and provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a.graphql", "b.gql", "c.graphqls", "D.GRAPHQL"])
def test_graphql_files_are_dispatched_to_the_graphql_parser(name: str) -> None:
    assert isinstance(ParserRegistry().for_path(Path(name)), GraphQLParser)
    assert detect_language(name.lower()) == "GraphQL"


def test_graphql_core_is_pinned_locked_noticed_and_licensed_as_one() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    pins = [spec.replace(" ", "") for spec in declared if spec.startswith("graphql-core")]
    assert pins == ["graphql-core==3.2.6"], "a different version is a different parser"
    pinned = pins[0].split("==")[1]

    [locked] = [
        p
        for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        if p["name"] == "graphql-core"
    ]
    assert locked["version"] == pinned
    assert importlib.metadata.version("graphql-core") == pinned == graphql.__version__

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    rows = [
        [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        for line in notices.splitlines()
        if "`graphql-core`" in line
    ]
    assert len(rows) == 1
    _, distribution, version, upstream, tag, commit, license_id, sdist = rows[0]
    assert (distribution, version, tag, license_id) == ("graphql-core", pinned, f"v{pinned}", "MIT")
    assert upstream == "https://github.com/graphql-python/graphql-core"
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert f"sha256:{sdist}" == locked["sdist"]["hash"]

    installed = importlib.metadata.distribution("graphql-core")
    [license_file] = [f for f in installed.files or () if Path(str(f)).name == "LICENSE"]
    text = Path(str(installed.locate_file(license_file))).read_text(encoding="utf-8")
    section = notices.split(f"### graphql-core {pinned}", 1)
    assert len(section) == 2, "no license section for graphql-core"
    published = section[1].split("### ", 1)[0]
    assert "MIT License" in text and "MIT License" in published
    for line in text.splitlines():
        if line.startswith("Copyright"):
            assert line in published, line


def test_the_pin_and_the_parser_version_move_together() -> None:
    """Moving the pin changes what is parsed: re-characterize, then bump the version."""
    assert (graphql.__version__, GraphQLParser.version) == ("3.2.6", "1")


# ---------------------------------------------------------------------------
# indexed: links, claims, impact, incremental equals fresh
# ---------------------------------------------------------------------------

REPO = "depot-api"
FILES = {
    "schema/stage.graphql": SCHEMA,
    # Stage is defined in another file: a claim, never a link to it.
    "schema/crate.graphqls": "type Crate {\n  stage: Stage\n}\n",
    # Mode is defined twice in two files: neither is the one a reference means.
    "schema/mode_a.graphql": "enum Mode { ON }\n",
    "schema/mode_b.graphql": "enum Mode { OFF }\n",
    "schema/switch.gql": "type Switch {\n  mode: Mode\n}\n",
    "ops/shipments.gql": DOCUMENT,
}


def _edges(workspace: Workspace, source: str) -> set[tuple[str, str, str, int | None]]:
    rows = workspace.database.execute(
        """
        SELECT d.qualified_name AS target, f.rel_path AS path, e.parser, e.line_start
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        LEFT JOIN files f ON f.id = d.file_id
        WHERE e.type = 'REFERENCES' AND s.qualified_name = ?
        """,
        (source,),
    ).fetchall()
    return {(r["target"], r["path"], r["parser"], r["line_start"]) for r in rows}


def _stored_claims(workspace: Workspace) -> list[dict[str, object]]:
    rows = workspace.database.execute(
        """
        SELECT f.rel_path AS path, u.target_key, u.meta_json, s.qualified_name AS source
        FROM unresolved_refs u JOIN files f ON f.id = u.file_id
        JOIN entities s ON s.id = u.src_id
        """
    ).fetchall()
    return [
        {
            "path": r["path"],
            "target": r["target_key"],
            "source": r["source"],
            **json.loads(r["meta_json"]),
        }
        for r in rows
        if json.loads(r["meta_json"] or "{}").get(PENDING_FACT) in GRAPHQL_FACTS
    ]


def test_an_indexed_schema_keeps_its_coordinates_links_and_claims(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {REPO: FILES})

    stored = workspace.database.execute(
        "SELECT meta_json, line_start FROM entities WHERE qualified_name = 'Parcel.stage'"
    ).fetchone()
    assert json.loads(stored["meta_json"]) == {
        "graphql": "field",
        "named_type": "Stage",
        "type_ref": "Stage!",
    }
    assert stored["line_start"] == 12
    language = workspace.database.execute(
        "SELECT language FROM files WHERE rel_path = 'schema/stage.graphql'"
    ).fetchone()
    assert language["language"] == "GraphQL"

    assert _edges(workspace, "Parcel.stage") == {("Stage", "schema/stage.graphql", "graphql", 12)}
    assert _edges(workspace, "Query.parcels(filter:)") == {
        ("ParcelFilter", "schema/stage.graphql", "graphql", 26)
    }
    claims = {
        (c["path"], c["source"], c["target"], c[PENDING_FACT], c.get("reason"))
        for c in _stored_claims(workspace)
    }
    assert ("schema/crate.graphqls", "Crate.stage", "Stage", GRAPHQL_TYPE, EXTERNAL) in claims
    assert ("schema/switch.gql", "Switch.mode", "Mode", GRAPHQL_TYPE, EXTERNAL) in claims
    assert ("ops/shipments.gql", "Shipments", "query", GRAPHQL_DOCUMENT, None) in claims
    assert ("ops/shipments.gql", "Card", "Parcel", GRAPHQL_DOCUMENT, None) in claims


def test_a_claim_is_never_linked_by_name_to_a_type_in_another_file(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Stage is defined exactly once in the repository and Mode twice; neither makes a
    claim a link. Which file a schema is assembled from is not a name's to decide."""
    build(workspace, tmp_path, {REPO: FILES})

    assert _edges(workspace, "Crate.stage") == set()
    assert _edges(workspace, "Switch.mode") == set()
    assert _edges(workspace, "Shipments") == set()
    assert _edges(workspace, "Card") == set()

    plan = ReferenceLinker(workspace.knowledge).prepare_global()
    graphql_claims = len(_stored_claims(workspace))
    assert graphql_claims >= 7
    assert plan.unresolved >= graphql_claims
    assert not [
        e for e in plan.edges if e.file_id in _file_ids(workspace, ".graphql", ".graphqls", ".gql")
    ]


def _file_ids(workspace: Workspace, *suffixes: str) -> set[int]:
    rows = workspace.database.execute("SELECT id, rel_path FROM files").fetchall()
    return {r["id"] for r in rows if r["rel_path"].endswith(suffixes)}


def test_no_graphql_claim_reaches_the_http_planner(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = linker_module.plan_http_claims

    def spy(claims, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.extend(json.loads(c.meta_json or "{}").get(PENDING_FACT, "") for c in claims)
        return original(claims, *args, **kwargs)

    monkeypatch.setattr(linker_module, "plan_http_claims", spy)
    build(workspace, tmp_path, {REPO: FILES})
    repo_id = workspace.database.execute(
        "SELECT id FROM repositories WHERE name = ?", (REPO,)
    ).fetchone()["id"]
    planned = plan_repository_http(workspace.knowledge, repo_id)

    assert not set(seen) & GRAPHQL_FACTS
    assert not [c for c in planned.claims if PENDING_FACT in (c.meta_json or "")]


def test_impact_from_an_enum_reaches_the_definitions_it_types(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {REPO: FILES})

    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=f"sym:{REPO}:schema/stage.graphql:Stage:1")
    )

    by_edge = {
        c.entity: (str(c.step.edge), c.step.at, c.step.lines)
        for c in response.candidates
        if c.step.edge is not None
    }
    at = f"{REPO}/schema/stage.graphql"
    for coordinate, line in [
        ("Parcel.stage", 12),
        ("Parcel.history(stage:)", 14),
        ("ParcelFilter.stage", 19),
        ("extend Stage", 16),
    ]:
        key = f"sym:{REPO}:schema/stage.graphql:{coordinate}:{line}"
        assert by_edge.get(key) == ("REFERENCES", at, (line, line)), coordinate
    # Nothing in another file is reached by sharing a name.
    reached = {c.entity for c in response.candidates}
    assert not [key for key in reached if ":schema/crate.graphqls:" in key]
    assert not [key for key in reached if ":ops/" in key]


def test_an_edited_schema_syncs_exactly_as_a_fresh_index_writes_it(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, {REPO: FILES})
    root = tmp_path / "repos"
    schema = root / REPO / "schema/stage.graphql"
    schema.write_text(
        SCHEMA.replace("  PACKED\n", "  BOXED\n").replace("  depot: Depot\n", ""),
        encoding="utf-8",
    )
    (root / REPO / "schema/crate.graphqls").write_text("type Crate {\n  size: Int\n}\n")

    sync(workspace, workspace.resolver().resolve(root / REPO))

    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)
    names = {
        r["qualified_name"]
        for r in workspace.database.execute("SELECT qualified_name FROM entities").fetchall()
    }
    assert "Stage.BOXED" in names and "Stage.PACKED" not in names
    assert "Parcel.depot" not in names


def test_a_deleted_schema_leaves_no_definition_link_or_claim_behind(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, {REPO: FILES})
    root = tmp_path / "repos"
    (root / REPO / "schema/stage.graphql").unlink()
    (root / REPO / "ops/shipments.gql").unlink()

    sync(workspace, workspace.resolver().resolve(root / REPO))

    gone = ("Stage", "Parcel.stage", "Shipments")
    left = workspace.database.execute(
        "SELECT COUNT(*) FROM entities WHERE qualified_name IN (?, ?, ?)", gone
    ).fetchone()[0]
    assert left == 0
    assert {c["path"] for c in _stored_claims(workspace)} == {
        "schema/crate.graphqls",
        "schema/switch.gql",
    }
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)
