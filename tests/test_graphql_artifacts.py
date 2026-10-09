"""Federated schema copies, their provenance, and the breadth of what is generated from them.

The guarantees pinned here, on the invented fixtures in ``tests/federation_corpus.py``:

* a supergraph coordinate is tied to its provider only through the whole stated chain -- the
  subgraph's URL, a deployment identity that runs exactly one repository's code, and that
  repository's served schema; a URL nothing deploys, or a service whose image is built
  elsewhere, ties nothing, each broken link is named, and a copy whose values differ is
  ``snapshot-stale`` with every difference listed;
* a schema file that is neither served nor composed is ``composition-unproven``;
* a fetch is recorded as where a copy came from (``artifact-remote``), its URL redacted, and
  never as ownership; a download of anything but a schema is not a fetch;
* ariadne-codegen's enums bind by name and their members by value; GraphQL Code Generator's
  operation types and typed documents bind to the document they are generated from, and
  ``enumValues`` binds a member by the value it maps to; a graphql-config project binds the
  documents it includes;
* change impact crosses from a provider through every copy to the code generated from it and
  that code's users, each step naming what it rests on; incremental equals fresh; plans
  write nothing.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.graphql_artifacts import FAMILY as ARTIFACTS
from repoatlas.graph.graphql_codegen import FAMILY as CODEGEN
from repoatlas.graph.graphql_documents import FAMILY as DOCUMENTS
from repoatlas.graph.graphql_ownership import EVIDENCE, origin_of
from repoatlas.graph.graphql_ownership import FAMILY as OWNERSHIP
from repoatlas.graph.linker import GLOBAL_PARSER, ReferenceLinker
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import ENUM_MEMBER
from repoatlas.parsers.graphql_artifacts import (
    GRAPHQL_ARTIFACT,
    package_script_references,
    schema_fetch,
)
from repoatlas.parsers.graphql_codegen import GRAPHQL_CODEGEN_OUTPUT
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, reindex_from_scratch
from tests.federation_corpus import API, PROVIDER, SDK, WEB, repos, supergraph
from tests.impact_corpus import build, verify_steps

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

COPY = "src/graphql/supergraph.graphql"
OLD = "schema/supergraph.graphql"
PROVIDED = "schema/parcels.graphql"


def decisions(workspace: Workspace, family: str) -> dict[str, str]:
    return {r.subject: r.decision for r in workspace.knowledge.list_link_decisions(family=family)}


def evidence(workspace: Workspace, family: str, subject: str) -> dict[str, Any]:
    [row] = [
        r for r in workspace.knowledge.list_link_decisions(family=family) if r.subject == subject
    ]
    return json.loads(row.evidence_json)


def generates(workspace: Workspace, key: str) -> dict[tuple[str, str], dict[str, Any]]:
    """(source, target) -> the evidence under ``key`` of every GENERATES edge recording it."""
    rows = workspace.database.execute(
        """
        SELECT r.slug || ':' || sf.rel_path || '#' || s.qualified_name AS source,
               dr.slug || ':' || df.rel_path || '#' || d.qualified_name AS target,
               e.resolution AS resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        JOIN repositories r ON r.id = sf.repo_id JOIN repositories dr ON dr.id = df.repo_id
        WHERE e.type = 'GENERATES' AND e.parser = ?
        """,
        (GLOBAL_PARSER,),
    ).fetchall()
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        record = json.loads(row["resolution"] or "{}").get(key)
        if record is not None:
            found[(row["source"], row["target"])] = record
    return found


def line_of(text: str, needle: str) -> int:
    return next(n for n, line in enumerate(text.splitlines(), 1) if needle in line)


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------


def test_each_copied_type_is_decided_by_the_chain_that_ties_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    assert decisions(workspace, OWNERSHIP) == {
        f"parcel-web:{COPY}::Stage": "linked",
        f"parcel-web:{COPY}::Parcel": "linked",
        f"parcel-web:{COPY}::Query": "linked",
        # an origin no deployment states
        f"parcel-web:{COPY}::Route": "provider-unproven",
        # an origin whose service runs an image built elsewhere, though the repository
        # holding its compose file serves a schema defining the type
        f"parcel-web:{COPY}::Shelf": "provider-unproven",
        f"parcel-sdk:{OLD}::Stage": "snapshot-stale",
        f"parcel-sdk:{OLD}::Parcel": "linked",
        f"parcel-sdk:{OLD}::Query": "linked",
        f"parcel-sdk:{OLD}::Route": "provider-unproven",
        f"parcel-sdk:{OLD}::Shelf": "provider-unproven",
        # neither served nor composed: nothing states where it comes from
        "parcel-api:schema/draft.graphql::Stage": "composition-unproven",
    }
    tied = evidence(workspace, OWNERSHIP, f"parcel-web:{COPY}::Stage")
    assert tied["graphs"] == [
        {
            "graph": "PARCELS",
            "url": "http://parcels/graphql",
            "repository": "parcel-api",
            "identity": "charts/parcels/templates/service.yaml:3 via Tiltfile:2",
        }
    ]
    elsewhere = evidence(workspace, OWNERSHIP, f"parcel-web:{COPY}::Shelf")["graphs"]
    assert elsewhere == [
        {
            "graph": "CACHE",
            "url": "http://cache:4000/graphql",
            "repository": None,
            "identity": "docker-compose.yml:2",
        }
    ]


def test_every_coordinate_is_composed_from_its_provider_definition_only(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    composed = generates(workspace, EVIDENCE)
    provider = f"parcel-api:{PROVIDED}#"
    coordinates = [
        "Stage",
        "Stage.PACKED",
        "Stage.SENT",
        "Parcel",
        "Parcel.id",
        "Parcel.stage",
        "Query",
        "Query.parcels",
        "Query.parcels(stage:)",
    ]
    assert set(composed) == {
        *((f"{provider}{c}", f"parcel-web:{COPY}#{c}") for c in [*coordinates, "Stage.LOST"]),
        # the older copy: what it shares with the provider, and nothing it lacks or adds
        *((f"{provider}{c}", f"parcel-sdk:{OLD}#{c}") for c in coordinates),
    }
    record = composed[(f"{provider}Stage.SENT", f"parcel-web:{COPY}#Stage.SENT")]
    assert record["graph"] == "PARCELS"
    assert record["url"] == "http://parcels/graphql"
    assert record["identity"] == "charts/parcels/templates/service.yaml:3 via Tiltfile:2"
    assert record["provider"] == f"parcel-api/{PROVIDED}:3"
    stale = evidence(workspace, OWNERSHIP, f"parcel-sdk:{OLD}::Stage")
    assert stale["stale"] == ["missing in copy: Stage.LOST", "missing in provider: Stage.RETURNED"]


def _without(path: str) -> dict[str, dict[str, str]]:
    found = repos()
    del found["parcel-api"][path]
    return found


def _with(files: dict[str, str]) -> dict[str, dict[str, str]]:
    found = repos()
    found["parcel-api"].update(files)
    return found


_ADMIN = (
    "import { readFileSync } from 'node:fs';\n"
    "import { join } from 'node:path';\n"
    "import { createSchema } from 'graphql-yoga';\n"
    "const typeDefs = readFileSync(join(__dirname, '../schema/admin.graphql'), 'utf8');\n"
    "export const admin = createSchema({ typeDefs, resolvers: {} });\n"
)


@pytest.mark.parametrize(
    ("corpus", "decision"),
    [
        # the release that binds the chart's Service is gone: no identity
        (lambda: _without("Tiltfile"), "provider-unproven"),
        # nothing constructs a schema: the repository serves none
        (lambda: _without("src/server.ts"), "schema-unserved"),
        # the served schema no longer has the type
        (
            lambda: _with({PROVIDED: PROVIDER.replace("enum Stage", "enum Phase")}),
            "provider-missing",
        ),
        # a second construction serves a different definition of it
        (
            lambda: _with(
                {"src/admin.ts": _ADMIN, "schema/admin.graphql": "enum Stage {\n  PACKED\n}\n"}
            ),
            "provider-ambiguous",
        ),
    ],
)
def test_each_broken_link_of_the_chain_is_named_and_ties_nothing(
    workspace: Workspace, tmp_path: Path, corpus: Any, decision: str
) -> None:
    build(workspace, tmp_path, corpus())

    assert decisions(workspace, OWNERSHIP)[f"parcel-web:{COPY}::Stage"] == decision
    assert not [
        t for _, t in generates(workspace, EVIDENCE) if t.startswith(f"parcel-web:{COPY}#Stage")
    ]


def test_an_origin_is_the_scheme_host_and_port_a_url_reaches() -> None:
    assert origin_of("http://parcels/graphql") == "http://parcels:80"
    assert origin_of("https://parcels.example.invalid:8443/x?y=1") == (
        "https://parcels.example.invalid:8443"
    )
    assert origin_of("parcels") is None
    assert origin_of("http://parcels:99999/") is None


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_a_fetch_is_where_a_copy_came_from_never_who_provides_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    assert decisions(workspace, ARTIFACTS) == {
        "parcel-web:package.json::schema:fetch": "artifact-remote",
        "parcel-sdk:scripts/fetch_schema.py": "artifact-remote",
    }
    script = evidence(workspace, ARTIFACTS, "parcel-sdk:scripts/fetch_schema.py")
    assert script == {
        "tool": "requests.get",
        "url": "https://registry.example.invalid/supergraph",
        "output": "../schema/supergraph.graphql",
        "writes": "parcel-sdk/schema/supergraph.graphql",
    }
    # cited by the copy's ownership decisions, as provenance alongside the tie
    copied = evidence(workspace, OWNERSHIP, f"parcel-web:{COPY}::Parcel")
    assert copied["fetched_by"] == ["parcel-web:package.json::schema:fetch"]
    assert "secret" not in json.dumps(
        [r.evidence_json for r in workspace.knowledge.list_link_decisions()]
    )


@pytest.mark.parametrize(
    ("command", "fetch"),
    [
        (
            "hive artifact:fetch --artifact supergraph --outputFile schema/s.graphql",
            {"tool": "hive artifact:fetch", "output": "schema/s.graphql"},
        ),
        (
            "rm -rf out && pnpm exec rover subgraph fetch g@v --name parcels --output=s.graphql",
            {"tool": "rover subgraph fetch", "output": "s.graphql"},
        ),
        (
            "npx get-graphql-schema https://u:p@api.example.invalid/graphql?k=1#f > s.graphql",
            {
                "tool": "get-graphql-schema",
                "output": "s.graphql",
                "url": "https://api.example.invalid/graphql",
            },
        ),
        (
            "apollo client:download-schema --endpoint=x schema.json",
            {"tool": "apollo client:download-schema", "output": "schema.json"},
        ),
        (
            "curl -s https://api.example.invalid/schema.graphqls -o api.graphqls",
            {
                "tool": "curl",
                "output": "api.graphqls",
                "url": "https://api.example.invalid/schema.graphqls",
            },
        ),
        # JSON is a schema only when what is asked is an introspection query
        (
            'curl -d \'{"query": "{ __schema { types { name } } }"}\''
            " https://api.example.invalid/graphql -o schema.json",
            {"tool": "curl", "output": "schema.json", "url": "https://api.example.invalid/graphql"},
        ),
        ("curl -o data.json https://api.example.invalid/data.json", None),
        ("wget -O notes.txt https://api.example.invalid/notes", None),
        ("graphql-codegen --config codegen.yml", None),
        ("echo 'unbalanced", None),
    ],
)
def test_a_command_is_a_fetch_only_when_it_runs_one(
    command: str, fetch: dict[str, str] | None
) -> None:
    assert schema_fetch(command) == fetch


def test_package_scripts_are_read_at_their_own_lines() -> None:
    text = WEB["package.json"]
    documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    [claim] = package_script_references(PurePosixPath("package.json"), documents)
    assert claim.meta[PENDING_FACT] == GRAPHQL_ARTIFACT
    assert (claim.meta["script"], claim.meta["output"]) == (
        "schema:fetch",
        "src/graphql/supergraph.graphql",
    )
    assert claim.line_start == line_of(text, '"schema:fetch"')
    assert not package_script_references(PurePosixPath("other.json"), documents)


_FETCH = (
    "from pathlib import Path\n"
    "import requests\n"
    'TARGET = Path(__file__).parent / "{name}"\n'
    "{query}"
    "def main():\n"
    '    TARGET.write_text(requests.post("https://api.example.invalid/graphql").text)\n'
)


@pytest.mark.parametrize(
    ("source", "output"),
    [
        (SDK["scripts/fetch_schema.py"], "../schema/supergraph.graphql"),
        (_FETCH.format(name="api.graphql", query=""), "api.graphql"),
        (
            _FETCH.format(name="api.json", query='QUERY = "query IntrospectionQuery { x }"\n'),
            "api.json",
        ),
        # JSON that is not an introspection answer
        (_FETCH.format(name="api.json", query=""), None),
        (SDK["scripts/fetch_rates.py"], None),
        # writes a schema, requests nothing
        ('from pathlib import Path\nPath(__file__).with_name("a.graphql").write_text("")\n', None),
        # a path relative to wherever it is run from
        (
            "import requests\n"
            'open("s.graphql", "w").write(requests.get("https://x.example.invalid").text)\n',
            None,
        ),
    ],
)
def test_a_module_fetches_only_when_it_requests_and_writes_an_anchored_schema(
    source: str, output: str | None
) -> None:
    result = PythonParser().parse(Path("scripts/fetch.py"), source)
    found = [
        r.meta.get("output")
        for r in result.references
        if r.meta.get(PENDING_FACT) == GRAPHQL_ARTIFACT
    ]
    assert found == ([output] if output else [])


# ---------------------------------------------------------------------------
# generation breadth
# ---------------------------------------------------------------------------


def test_python_enum_members_are_symbols_carrying_their_values() -> None:
    source = (
        "from enum import Enum, StrEnum\n"
        "class Stage(str, Enum):\n"
        '    PACKED = "PACKED"\n'
        '    _hidden = "x"\n'
        "    COUNT = 3\n"
        "class Lane(StrEnum):\n"
        '    FAST = "fast"\n'
        "class Plain:\n"
        '    SENT = "SENT"\n'
    )
    symbols = PythonParser().parse(Path("enums.py"), source).symbols
    members = {
        s.qualified_name: (s.meta.get("value"), s.line_start)
        for s in symbols
        if s.meta.get("kind") == ENUM_MEMBER
    }
    assert members == {"Stage.PACKED": ("PACKED", 3), "Lane.FAST": ("fast", 7)}


def _claims(path: str, text: str) -> list[dict[str, Any]]:
    parser = ParserRegistry().for_path(Path(path))
    assert parser is not None
    result = parser.parse(Path(path), text)
    return [
        {k: json.loads(v) if k in {"schema", "documents"} else v for k, v in r.meta.items()}
        for r in result.references
        if r.meta.get(PENDING_FACT) == GRAPHQL_CODEGEN_OUTPUT
    ]


def test_ariadne_codegen_is_read_from_its_project_table() -> None:
    [claim] = _claims("pyproject.toml", SDK["pyproject.toml"])
    assert claim["generator"] == "ariadne-codegen"
    assert claim["output"] == "parcel_sdk/enums.py"
    assert [(i["kind"], i["value"]) for i in claim["schema"]] == [
        ("path", "schema/supergraph.graphql")
    ]
    assert [(i["kind"], i["value"]) for i in claim["documents"]] == [("tree", "queries")]

    remote = (
        "[tool.ariadne-codegen]\n"
        'remote_schema_url = "https://u:p@api.example.invalid/graphql?token=t"\n'
        'target_package_name = "client"\n'
        'enums_module_name = "kinds"\n'
    )
    [claim] = _claims("pyproject.toml", remote)
    assert claim["output"] == "client/kinds.py"
    assert [(i["kind"], i["value"]) for i in claim["schema"]] == [
        ("url", "https://api.example.invalid/graphql")
    ]
    assert not _claims("pyproject.toml", '[project]\nname = "plain"\n')


def test_ariadne_enums_bind_by_name_and_their_members_by_value(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = decisions(workspace, CODEGEN)
    output = "parcel-sdk:pyproject.toml->parcel_sdk/enums.py"
    assert {s: d for s, d in found.items() if s.startswith(f"{output}::Stage")} == {
        f"{output}::Stage": "linked",
        f"{output}::Stage.PACKED": "linked",
        f"{output}::Stage.SENT": "linked",
        # generated from the copy, which still has it
        f"{output}::Stage.RETURNED": "linked",
    }
    edges = generates(workspace, CODEGEN)
    record = edges[(f"parcel-sdk:{OLD}#Stage.SENT", "parcel-sdk:parcel_sdk/enums.py#Stage.SENT")]
    assert (record["plugin"], record["member_value"]) == ("ariadne-codegen", "SENT")
    # only enums: the copy's object types are other modules' business, not even decided
    assert not [t for _, t in edges if t.startswith("parcel-sdk:") and t.endswith("#Parcel")]
    assert f"{output}::Parcel" not in found
    # the queries directory is the documents ariadne-codegen validates
    assert decisions(workspace, DOCUMENTS)["parcel-sdk:queries/parcels.graphql::ListParcels"] == (
        "linked"
    )


def test_operation_types_and_typed_documents_bind_to_their_document(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    output = "parcel-web:codegen.yml->src/gen/types.ts"
    found = {s: d for s, d in decisions(workspace, CODEGEN).items() if " " in s.split("::")[-1]}
    assert found == {
        f"{output}::query SentParcels": "linked",
        f"{output}::fragment ParcelStage": "generated-missing",
        # the generator would rename it
        f"{output}::query lowParcels": "naming-unsupported",
        f"{output}::query Twice": "generated-ambiguous",
    }
    assert evidence(workspace, CODEGEN, f"{output}::fragment ParcelStage")["missing"] == [
        "ParcelStageFragmentDoc"
    ]
    assert evidence(workspace, CODEGEN, f"{output}::query Twice")["documents"] == [
        "parcel-web/src/queries/again.graphql:1",
        "parcel-web/src/queries/twice.graphql:1",
    ]
    queries = "parcel-web:src/queries/parcels.graphql#"
    edges = {
        pair: record
        for pair, record in generates(workspace, CODEGEN).items()
        if pair[0].startswith(queries)
    }
    assert set(edges) == {
        (f"{queries}SentParcels", "parcel-web:src/gen/types.ts#SentParcelsQuery"),
        (f"{queries}SentParcels", "parcel-web:src/gen/types.ts#SentParcelsQueryVariables"),
        (f"{queries}SentParcels", "parcel-web:src/gen/types.ts#SentParcelsDocument"),
        (f"{queries}ParcelStage", "parcel-web:src/gen/types.ts#ParcelStageFragment"),
    }
    record = edges[(f"{queries}SentParcels", "parcel-web:src/gen/types.ts#SentParcelsDocument")]
    assert record["plugin"] == "typed-document-node"
    assert record["schema"] == "parcel-web/src/queries/parcels.graphql:1"


def test_enum_values_bind_a_member_by_the_value_it_maps_to(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())

    output = "parcel-web:codegen.yml->src/gen/types.ts"
    assert decisions(workspace, CODEGEN)[f"{output}::Stage.LOST"] == "linked"
    assert evidence(workspace, CODEGEN, f"{output}::Stage.LOST")["mapped"] == "missing"
    record = generates(workspace, CODEGEN)[
        (f"parcel-web:{COPY}#Stage.LOST", "parcel-web:src/gen/types.ts#Stage.Lost")
    ]
    assert record["member_value"] == "missing"
    # an enum enumValues imports from a module is not declared by the output
    imported = "parcel-web:codegen.yml->src/gen/imported.ts"
    assert decisions(workspace, CODEGEN)[f"{imported}::Stage"] == "naming-unsupported"
    assert decisions(workspace, CODEGEN)[f"{imported}::Parcel"] == "linked"

    root = tmp_path / "repos"
    config = root / "parcel-web" / "codegen.yml"
    config.write_text(config.read_text().replace("LOST: missing", "LOST: gone"))
    sync(workspace, workspace.resolver().resolve(root / "parcel-web"))
    assert decisions(workspace, CODEGEN)[f"{output}::Stage.LOST"] == "member-stale"
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_a_graphql_config_project_binds_the_documents_it_includes(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(
        workspace,
        tmp_path,
        {
            "parcel-app": {
                "package.json": json.dumps({"name": "parcel-app", "private": True}),
                ".graphqlrc.yml": (
                    "projects:\n"
                    "  app:\n"
                    "    schema: schema.graphql\n"
                    "    documents: src/**/*.graphql\n"
                    "    exclude: src/legacy/**\n"
                ),
                "schema.graphql": PROVIDER,
                "src/sent.graphql": "query Sent {\n  parcels(stage: SENT) {\n    id\n  }\n}\n",
                "src/legacy/old.graphql": "query Old {\n  parcels {\n    id\n  }\n}\n",
            }
        },
    )

    assert decisions(workspace, CODEGEN) == {"parcel-app:.graphqlrc.yml::app": "linked"}
    assert decisions(workspace, DOCUMENTS) == {
        "parcel-app:src/sent.graphql::Sent": "linked",
        "parcel-app:src/legacy/old.graphql::Old": "schema-unknown",
    }


# ---------------------------------------------------------------------------
# change impact, freshness, planning
# ---------------------------------------------------------------------------


def _impact(workspace: Workspace, seed: str) -> Any:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=seed, presentation=Presentation.FULL)
    )
    assert response.seed.status == "resolved"
    verify_steps(workspace, response)
    return response


def test_a_provider_change_reaches_every_copy_what_is_generated_from_it_and_its_users(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    copy = supergraph("PACKED", "SENT", "LOST")

    response = _impact(workspace, f"sym:parcel-api:{PROVIDED}:Stage.SENT:3")

    by_entity = {c.entity: c for c in response.candidates}
    composed = by_entity[f"sym:parcel-web:{COPY}:Stage.SENT:{line_of(copy, '  SENT @')}"]
    assert (str(composed.step.edge), composed.step.reading) == ("GENERATES", "is composed from")
    assert composed.step.composition is not None
    assert composed.step.composition.graph == "PARCELS"
    assert composed.step.composition.provider == f"parcel-api/{PROVIDED}:3"
    assert composed.step.generation is None
    # (repository, path, qualified name), from each candidate's key
    names = {
        (c.repository, c.path, c.entity.rsplit(":", 1)[0].split(":", 3)[3])
        for c in response.candidates
    }
    assert {
        ("parcel-web", "src/gen/types.ts", "Stage.Sent"),
        ("parcel-web", "src/queries/parcels.graphql", "SentParcels"),
        ("parcel-web", "src/gen/types.ts", "SentParcelsDocument"),
        ("parcel-web", "src/pages/sent.ts", "useSent"),
        ("parcel-sdk", OLD, "Stage.SENT"),
        ("parcel-sdk", "parcel_sdk/enums.py", "Stage.SENT"),
        ("parcel-sdk", "parcel_sdk/moving.py", "is_moving"),
    } <= names
    generated = next(c for c in response.candidates if c.name == "SentParcelsDocument")
    assert generated.step.generation is not None
    assert generated.step.generation.plugin == "typed-document-node"
    # an unproven copy, and the copy's other subgraphs, are reached by nothing
    assert not [c for c in response.candidates if c.path == "schema/draft.graphql"]


def test_a_type_whose_identity_runs_elsewhere_reaches_no_copy(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    response = _impact(workspace, f"sym:parcel-api:{PROVIDED}:Shelf:10")
    assert not [c for c in response.candidates if c.repository != "parcel-api"]


def test_a_provider_change_makes_copies_stale_exactly_as_a_fresh_index_does(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    schema = root / "parcel-api" / PROVIDED
    schema.write_text(PROVIDER.replace("  LOST\n", ""))
    sync(workspace, workspace.resolver().resolve(root / "parcel-api"))

    assert decisions(workspace, OWNERSHIP)[f"parcel-web:{COPY}::Stage"] == "snapshot-stale"
    assert evidence(workspace, OWNERSHIP, f"parcel-web:{COPY}::Stage")["stale"] == [
        "missing in provider: Stage.LOST"
    ]
    assert evidence(workspace, OWNERSHIP, f"parcel-sdk:{OLD}::Stage")["stale"] == [
        "missing in provider: Stage.RETURNED"
    ]
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)

    schema.write_text(PROVIDER)
    sync(workspace, workspace.resolver().resolve(root / "parcel-api"))
    assert decisions(workspace, OWNERSHIP)[f"parcel-web:{COPY}::Stage"] == "linked"
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_removing_the_release_unties_every_copy_exactly_as_a_fresh_index_does(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    (root / "parcel-api" / "Tiltfile").unlink()
    sync(workspace, workspace.resolver().resolve(root / "parcel-api"))

    assert not generates(workspace, EVIDENCE)
    assert set(decisions(workspace, OWNERSHIP).values()) == {
        "provider-unproven",
        "composition-unproven",
    }
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_the_global_plan_is_repeatable_and_writes_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    before = fact_snapshot(workspace)

    linker = ReferenceLinker(workspace.knowledge)
    first, second = linker.prepare_global(), linker.prepare_global()

    assert first.decisions == second.decisions
    assert sorted(first.edges, key=repr) == sorted(second.edges, key=repr)
    assert {d.family for d in first.decisions} >= {OWNERSHIP, ARTIFACTS, CODEGEN, DOCUMENTS}
    assert fact_snapshot(workspace) == before


def test_api_and_sdk_fixtures_are_what_the_tests_cite() -> None:
    """The line numbers the assertions name, held to the fixtures."""
    assert line_of(PROVIDER, "  SENT") == 3
    assert line_of(PROVIDER, "type Shelf") == 10
    assert line_of(API["charts/parcels/templates/service.yaml"], "name: parcels") == 3
    assert line_of(SDK["scripts/fetch_schema.py"], "write_text") == 10
