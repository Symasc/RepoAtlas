"""Persistence: Prisma and SQL schemas, queries bound to their database, value mappings.

The guarantees pinned here, on the invented fixtures in ``tests/persistence_corpus.py``:

* the Prisma and SQL readers are pure and bounded, name what the database calls each thing
  (``@map`` / ``@@map``, never a constraint's ``map:``), and never echo source;
* a query is bound only through the whole chain the source states -- a client construction,
  a database identity in the same repository, PostgreSQL, readable SQL, a mapped column --
  and each broken link is named;
* migrations are generated from the enum whose type they create and alter, and what they
  leave is compared with what the schema says;
* the generated Prisma client binds the names it exports, in its own repository;
* a typed mapping maps members to members (and, between two enums, type to type), and an
  incomplete one is decided so; a typed comparison binds the member whose value it names,
  through the field's declared type, never another enum sharing the value;
* change impact follows all of it; incremental equals fresh; the plans write nothing.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.compat import tomllib
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import GLOBAL_PARSER, ReferenceLinker
from repoatlas.graph.persistence import FAMILY as PERSISTENCE
from repoatlas.graph.prisma_schema import CLIENT, MIGRATIONS
from repoatlas.graph.value_mappings import FAMILY as MAPPINGS
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import ENUM_MEMBER, PARSE_BOUND_EXCEEDED, SYNTAX_ERROR
from repoatlas.parsers import prisma as prisma_module
from repoatlas.parsers.dashboards import dashboard_references
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.prisma import PrismaParser
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.sql import Predicate, SqlParser, sql_predicates
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, reindex_from_scratch
from tests.impact_corpus import build, verify_steps
from tests.persistence_corpus import API, INIT, OPS, SCHEMA, repos

ROOT = Path(__file__).resolve().parents[1]
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def decisions(workspace: Workspace, family: str) -> dict[str, str]:
    return {r.subject: r.decision for r in workspace.knowledge.list_link_decisions(family=family)}


def evidence(workspace: Workspace, family: str, subject: str) -> dict[str, Any]:
    [row] = [
        r for r in workspace.knowledge.list_link_decisions(family=family) if r.subject == subject
    ]
    return json.loads(row.evidence_json)


def edges(workspace: Workspace, kind: str, key: str | None = None) -> set[tuple[str, str]]:
    """(source, target) as ``repo:path#qualified`` for every global edge of a type, or only
    those whose resolution records ``key``."""
    rows = workspace.database.execute(
        """
        SELECT r.slug || ':' || sf.rel_path || '#' || s.qualified_name AS source,
               dr.slug || ':' || df.rel_path || '#' || d.qualified_name AS target,
               e.resolution AS resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        JOIN repositories r ON r.id = sf.repo_id JOIN repositories dr ON dr.id = df.repo_id
        WHERE e.type = ? AND e.parser = ?
        """,
        (kind, GLOBAL_PARSER),
    ).fetchall()
    return {
        (row["source"], row["target"])
        for row in rows
        if key is None or key in json.loads(row["resolution"] or "{}")
    }


def claims(result: Any, role: str) -> list[dict[str, Any]]:
    return [
        {"symbol": r.source_symbol, "line": r.line_start, **r.meta}
        for r in result.references
        if r.meta.get(PENDING_FACT) == role
    ]


# ---------------------------------------------------------------------------
# Prisma
# ---------------------------------------------------------------------------


def test_prisma_names_what_the_database_calls_each_thing() -> None:
    result = PrismaParser().parse(Path("prisma/schema.prisma"), SCHEMA)
    assert not result.diagnostics
    found = {s.qualified_name: (s.line_start, s.meta) for s in result.symbols}
    assert found["db"][1] == {
        "prisma": "datasource",
        "provider": "postgresql",
        "url_env": "DEPOT_DATABASE_URL",
    }
    assert found["client"][1] == {"prisma": "generator", "provider": "prisma-client-js"}
    assert found["ParcelState"] == (11, {"prisma": "enum", "db_name": "parcel_state"})
    assert found["ParcelState.lost"] == (
        14,
        {"prisma": "enum-value", "db_name": "missing", "kind": ENUM_MEMBER, "value": "lost"},
    )
    # @@index's map: names an index, never the table
    assert found["Parcel"][1] == {"prisma": "model", "db_name": "parcels"}
    assert found["Parcel.state"] == (
        21,
        {
            "prisma": "field",
            "db_name": "state_code",
            "type": "ParcelState",
            "modifier": "",
            "default": "packed",
        },
    )
    assert found["Parcel.label"][1]["modifier"] == "?"
    assert [(r.source_symbol, r.target_key, r.line_start) for r in result.references] == [
        ("Parcel.state", "ParcelState", 21)
    ]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("model A {\n  id String @id\n", 2),
        ('enum E {\n  A\n}\nmodel M {\n  x "quoted" type\n}\n', 5),
        ("datasource db {\n  url = \n}\n", 2),
        ("/* never closed\nmodel A {\n  id String\n}\n", 1),
        ("model A { id String @@id }\n", 1),
        ("model A {\n  name String # comment\n}\n", 2),
    ],
)
def test_a_broken_schema_is_refused_by_line_without_its_text(text: str, line: int) -> None:
    result = PrismaParser().parse(Path("schema.prisma"), text)
    assert not result.symbols
    [diagnostic] = result.diagnostics
    assert (diagnostic.message, diagnostic.line) == (SYNTAX_ERROR, line)


def test_a_schema_past_the_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prisma_module, "MAX_TOKENS", 10)
    result = PrismaParser().parse(Path("schema.prisma"), SCHEMA)
    assert [d.message for d in result.diagnostics] == [PARSE_BOUND_EXCEEDED]
    deep = "model A {\n  id String @default(" + "f(" * 100 + ")" * 101 + "\n}\n"
    assert PrismaParser().parse(Path("schema.prisma"), deep).diagnostics[0].message == (
        PARSE_BOUND_EXCEEDED
    )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def test_sql_defines_types_tables_and_columns_at_their_lines() -> None:
    result = SqlParser().parse(Path("migration.sql"), INIT)
    found = {(s.qualified_name, s.line_start): s.meta for s in result.symbols}
    assert found[("parcel_state", 2)] == {
        "sql": "enum-type",
        "values": '["packed", "sent"]',
    }
    assert found[("parcel_state.packed", 2)]["kind"] == ENUM_MEMBER
    assert found[("parcels", 5)] == {"sql": "table"}
    assert found[("parcels.state_code", 7)] == {
        "sql": "column",
        "type": "parcel_state",
        "default": "packed",
    }
    assert [(r.source_symbol, r.target_key) for r in result.references] == [
        ("parcels.state_code", "parcel_state")
    ]


@pytest.mark.parametrize(
    ("statement", "symbols"),
    [
        (
            "ALTER TYPE \"s\".\"t\" ADD VALUE IF NOT EXISTS 'x' BEFORE 'y';",
            {
                "s.t": {"sql": "alter-type", "action": "add-value", "value": "x", "before": "y"},
                "s.t.x": {"sql": "enum-value", "kind": ENUM_MEMBER, "value": "x", "added": "true"},
            },
        ),
        (
            "ALTER TYPE t RENAME VALUE 'a' TO 'b';",
            {
                "t": {"sql": "alter-type", "action": "rename-value", "from": "a", "to": "b"},
                "t.b": {"sql": "enum-value", "kind": ENUM_MEMBER, "value": "b", "renamed": "true"},
            },
        ),
        ("ALTER TYPE T RENAME TO u;", {"t": {"sql": "alter-type", "action": "rename", "to": "u"}}),
        ("ALTER TYPE t OWNER TO someone;", {"t": {"sql": "alter-type", "action": "other"}}),
        ('DROP TYPE "t";', {"t": {"sql": "drop-type"}}),
        (
            'ALTER TABLE "p" ADD COLUMN "c" "t" DEFAULT \'x\';',
            {
                "p": {"sql": "alter-table"},
                "p.c": {"sql": "column", "type": "t", "default": "x", "added": "true"},
            },
        ),
        # MySQL: backticks are not PostgreSQL, so nothing is read as a table
        ("CREATE TABLE `m` (`id` INT, `tier` ENUM('A'));", {}),
        ("SELECT 1; INSERT INTO p VALUES (1);", {}),
        ("CREATE TYPE broken AS ENUM ('a',;", {}),
    ],
)
def test_each_statement_form_is_read_or_skipped(
    statement: str, symbols: dict[str, dict[str, str]]
) -> None:
    result = SqlParser().parse(Path("m.sql"), statement)
    assert not result.diagnostics
    assert {s.qualified_name: s.meta for s in result.symbols} == symbols


def test_sqlglot_never_logs_the_source(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        # statements sqlglot falls back on, which it logs with their text
        SqlParser().parse(Path("m.sql"), "CREATE TABLE `m` (`private-token` INT);")
        sql_predicates("ALTER TYPE t RENAME VALUE 'private-token' TO 'b'")
        SqlParser().parse(Path("m.sql"), "ALTER TYPE t RENAME VALUE 'private-token' TO 'b';")
        sql_predicates("SELECT 'private-token' FROM x WHERE a = ((")
    assert "private-token" not in caplog.text


@pytest.mark.parametrize(
    ("text", "found"),
    [
        (
            "SELECT count(*) FROM parcels WHERE state_code = 'sent'",
            (Predicate("parcels", "state_code", "=", ("sent",)),),
        ),
        (
            "SELECT * FROM parcels p JOIN labels l ON l.id = p.label "
            "WHERE p.state_code IN ('a', 'b') AND kind <> 'x'",
            (
                Predicate("parcels", "state_code", "in", ("a", "b")),
                Predicate(None, "kind", "<>", ("x",)),
            ),
        ),
        (
            "SELECT * FROM parcels WHERE state_code NOT IN ('a') AND 'b' = state_code",
            (
                Predicate("parcels", "state_code", "<>", ("a",)),
                Predicate("parcels", "state_code", "=", ("b",)),
            ),
        ),
        (
            "UPDATE parcels SET state_code = 'lost'::parcel_state WHERE id = $1",
            (Predicate("parcels", "state_code", "set", ("lost",)),),
        ),
        ("SELECT DISTINCT state_code FROM parcels", ()),
        ("SELECT 1; SELECT 2", None),
        ("not sql ((", None),
    ],
)
def test_embedded_sql_yields_its_comparisons(
    text: str, found: tuple[Predicate, ...] | None
) -> None:
    read = sql_predicates(text)
    assert (None if read is None else set(read)) == (None if found is None else set(found))


def test_sqlglot_is_pinned_locked_noticed_and_licensed_as_one() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert [d for d in declared if d.startswith("sqlglot")] == ["sqlglot==30.19.0"]
    [locked] = [
        p
        for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        if p["name"] == "sqlglot"
    ]
    assert locked["version"] == "30.19.0" == importlib.metadata.version("sqlglot")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    [row] = [
        [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        for line in notices.splitlines()
        if "`sqlglot`" in line
    ]
    _, distribution, version, upstream, tag, commit, license_id, sdist = row
    assert (distribution, version, upstream, tag, license_id) == (
        "sqlglot",
        "30.19.0",
        "https://github.com/tobymao/sqlglot",
        "v30.19.0",
        "MIT",
    )
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert f"sha256:{sdist}" == locked["sdist"]["hash"]
    installed = importlib.metadata.distribution("sqlglot")
    [license_file] = [f for f in installed.files or () if Path(str(f)).name == "LICENSE"]
    text = Path(str(installed.locate_file(license_file))).read_text(encoding="utf-8")
    published = notices.split("### sqlglot 30.19.0", 1)[1].split("### ", 1)[0]
    for line in text.splitlines():
        if line.startswith("Copyright"):
            assert line in published


def test_the_pin_and_the_sql_parser_version_move_together() -> None:
    assert (importlib.metadata.version("sqlglot"), SqlParser.version) == ("30.19.0", "1")


# ---------------------------------------------------------------------------
# facts in code
# ---------------------------------------------------------------------------


def _ts(path: str) -> Any:
    return TypeScriptParser().parse(Path(path), API[path])


def test_typescript_clients_record_their_library_and_environment() -> None:
    found = {c["symbol"]: c for c in claims(_ts("src/db.ts"), "persistence-client")}
    assert {k: (v["library"], v.get("env")) for k, v in found.items()} == {
        "prisma": ("prisma", None),
        "db": ("knex", "DEPOT_DATABASE_URL"),
        "pool": ("pg", "DEPOT_DATABASE_URL"),
        "legacy": ("knex", "LEGACY_URL"),
    }
    assert found["prisma"]["module"] == "@prisma/client"


def test_typescript_queries_record_their_receiver_and_what_they_compare() -> None:
    found = {c["owner"]: c for c in claims(_ts("src/queries.ts"), "persistence-query")}
    assert found["packedParcels"]["binding"] == "import"
    assert json.loads(found["packedParcels"]["predicates"]) == [["state_code", "=", ["packed"]]]
    assert json.loads(found["movingParcels"]["tables"]) == [["parcels", "p"], ["labels", "l"]]
    assert json.loads(found["movingParcels"]["predicates"]) == [
        ["p.state_code", "in", ["sent", "missing"]]
    ]
    assert json.loads(found["sentCount"]["predicates"]) == [
        ["state_code", "=", ["sent"], "parcels"]
    ]
    # a substitution in a tagged $queryRaw is a bound parameter, anywhere else run time's
    assert found["packedSince"]["parameters"] == "1" and "dynamic" not in found["packedSince"]
    assert found["inState"]["dynamic"] == "true"
    assert found["shadowed"]["binding"] == "local"
    assert set(found) == {
        "packedParcels",
        "movingParcels",
        "sentCount",
        "packedSince",
        "inState",
        "legacySent",
        "shadowed",
        "byColour",
    }


def test_typescript_mappings_record_each_entry() -> None:
    found = {c["symbol"]: c for c in claims(_ts("src/mapping.ts"), "value-mapping")}
    assert (found["toState"]["key_type"], found["toState"]["value_type"]) == (
        "Stage",
        "ParcelState",
    )
    assert json.loads(found["toState"]["entries"])[0] == [
        "member",
        "Stage.Packed",
        "member",
        "ParcelState.packed",
        5,
    ]
    assert json.loads(found["badges"]["entries"]) == [
        ["member", "Stage.Packed", "literal", "boxed", 11]
    ]


def _py(path: str) -> Any:
    return PythonParser().parse(Path(path), OPS[path])


def test_python_facts() -> None:
    [query] = claims(_py("ops/report.py"), "persistence-query")
    assert (query["binding"], query["env"], query["owner"]) == (
        "inline",
        "DEPOT_DATABASE_URL",
        "sent",
    )
    comparisons = claims(_py("ops/checks.py"), "typed-comparison")
    assert [(c["symbol"], c["type"], c["field"], c["values"]) for c in comparisons] == [
        ("lost", "Parcel", "state", '["LOST"]'),
        ("first_lost", "Parcel", "state", '["LOST"]'),
    ]
    [mapping] = claims(_py("ops/labels.py"), "value-mapping")
    assert (mapping["key_type"], mapping["value_type"]) == ("Stage", "str")
    [parcel] = [s for s in _py("ops/models.py").symbols if s.qualified_name == "Parcel"]
    assert json.loads(parcel.meta["fields"]) == {"id": "str", "state": "Stage"}


@pytest.mark.parametrize(
    ("body", "dynamic"),
    [
        ('conn.execute(f"SELECT * FROM t WHERE a = {x}")', True),
        ('conn.execute("SELECT * FROM t WHERE a = %s" % x)', True),
        ('conn.execute("SELECT * FROM t WHERE a = {}".format(x))', True),
        ("conn.execute(\"SELECT * FROM t WHERE a = 'b'\")", False),
    ],
)
def test_python_sql_built_at_run_time_is_dynamic(body: str, dynamic: bool) -> None:
    source = (
        "import os\nimport psycopg\n\ndef f(x):\n"
        '    with psycopg.connect(os.environ["U"]) as conn:\n'
        f"        {body}\n"
    )
    [query] = claims(PythonParser().parse(Path("q.py"), source), "persistence-query")
    assert ("dynamic" in query) is dynamic


def test_dashboards_record_their_sql() -> None:
    document = yaml.compose(OPS["dashboards/parcels.json"], Loader=yaml.SafeLoader)
    [claim] = dashboard_references(PurePosixPath("dashboards/parcels.json"), [document])
    assert claim.meta["binding"] == "dashboard"
    assert json.loads(claim.meta["predicates"]) == [["state_code", "=", ["missing"], "parcels"]]
    variable = yaml.compose(
        json.dumps({"panels": [{"targets": [{"rawSql": "SELECT 1 WHERE a = $state"}]}]}),
        Loader=yaml.SafeLoader,
    )
    [claim] = dashboard_references(PurePosixPath("d.json"), [variable])
    assert claim.meta["dynamic"] == "true"
    plain = yaml.compose(json.dumps({"targets": [{"rawSql": "SELECT 1"}]}), Loader=yaml.SafeLoader)
    assert not dashboard_references(PurePosixPath("d.json"), [plain])  # no panels: no dashboard
    assert not dashboard_references(PurePosixPath("d.yaml"), [variable])


# ---------------------------------------------------------------------------
# the global pass
# ---------------------------------------------------------------------------

Q = "depot-api:src/queries.ts::"


def test_each_query_is_decided_by_the_first_link_its_chain_lacks(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    assert decisions(workspace, PERSISTENCE) == {
        f"{Q}packedParcels": "linked",
        f"{Q}movingParcels": "linked",
        f"{Q}sentCount": "linked",
        f"{Q}packedSince": "linked",
        f"{Q}inState": "sql-dynamic",
        # a client whose environment variable no datasource here reads
        f"{Q}legacySent": "database-identity-unproven",
        f"{Q}shadowed": "receiver-unproven",
        f"{Q}byColour": "column-unknown",
        "depot-mysql:src/db.ts::gold": "dialect-unsupported",
        # the same variable as depot-api's datasource, in another repository
        "depot-ops:ops/report.py::sent": "database-identity-unproven",
        "depot-ops:dashboards/parcels.json": "database-identity-unproven",
    }


def test_a_bound_query_references_the_field_and_the_value_its_literal_names(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    schema = "depot-api:prisma/schema.prisma#"
    found = {(s, t) for s, t in edges(workspace, "REFERENCES", "persistence")}
    assert found == {
        ("depot-api:src/queries.ts#packedParcels", f"{schema}Parcel.state"),
        ("depot-api:src/queries.ts#packedParcels", f"{schema}ParcelState.packed"),
        ("depot-api:src/queries.ts#movingParcels", f"{schema}Parcel.state"),
        ("depot-api:src/queries.ts#movingParcels", f"{schema}ParcelState.sent"),
        # 'missing' is the database's name for `lost`
        ("depot-api:src/queries.ts#movingParcels", f"{schema}ParcelState.lost"),
        ("depot-api:src/queries.ts#sentCount", f"{schema}Parcel.state"),
        ("depot-api:src/queries.ts#sentCount", f"{schema}ParcelState.sent"),
        ("depot-api:src/queries.ts#packedSince", f"{schema}Parcel.state"),
        ("depot-api:src/queries.ts#packedSince", f"{schema}ParcelState.packed"),
    }


def test_migrations_are_generated_from_the_enum_and_folded_against_it(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())

    init = "depot-api:prisma/migrations/20260101000000_init/migration.sql#"
    lost = "depot-api:prisma/migrations/20260201000000_lost/migration.sql#"
    assert edges(workspace, "GENERATES", "prisma_migrate") == {
        ("depot-api:prisma/schema.prisma#ParcelState", f"{init}parcel_state"),
        ("depot-api:prisma/schema.prisma#ParcelState", f"{lost}parcel_state"),
    }
    assert decisions(workspace, MIGRATIONS) == {
        "depot-api:prisma/schema.prisma::ParcelState": "linked",
        # nothing in a MySQL migration is read as PostgreSQL, so no enum is folded there
        "depot-mysql:prisma/migrations/20260101000000_init/migration.sql": "dialect-unsupported",
    }

    root = tmp_path / "repos"
    (root / "depot-api/prisma/migrations/20260201000000_lost/migration.sql").unlink()
    sync(workspace, workspace.resolver().resolve(root / "depot-api"))
    assert decisions(workspace, MIGRATIONS)["depot-api:prisma/schema.prisma::ParcelState"] == (
        "migration-drift"
    )
    assert evidence(workspace, MIGRATIONS, "depot-api:prisma/schema.prisma::ParcelState")[
        "missing_in_migrations"
    ] == ["missing"]
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_the_generated_client_binds_the_names_it_exports_in_its_repository(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    schema = "depot-api:prisma/schema.prisma#"
    assert edges(workspace, "REFERENCES", "prisma_client") == {
        ("depot-api:src/mapping.ts#toState", f"{schema}ParcelState"),
        ("depot-api:src/mapping.ts#isLost", f"{schema}ParcelState"),
    }
    assert decisions(workspace, CLIENT) == {"depot-api:@prisma/client": "linked"}


def test_a_typed_mapping_maps_member_to_member_and_type_to_type(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    stage = "depot-api:src/gen/types.ts#Stage"
    state = "depot-api:prisma/schema.prisma#ParcelState"
    assert edges(workspace, "MAPS_TO") == {
        (stage, state),
        (f"{stage}.Packed", f"{state}.packed"),
        (f"{stage}.Sent", f"{state}.sent"),
        (f"{stage}.Lost", f"{state}.lost"),
    }
    assert decisions(workspace, MAPPINGS) == {
        "depot-api:src/mapping.ts::toState": "linked",
        "depot-api:src/mapping.ts::badges": "mapping-incomplete",
        "depot-ops:ops/labels.py::LABELS": "mapping-incomplete",
    }
    assert evidence(workspace, MAPPINGS, "depot-api:src/mapping.ts::badges")["missing"] == [
        "Lost",
        "Sent",
    ]
    assert evidence(workspace, MAPPINGS, "depot-ops:ops/labels.py::LABELS")["missing"] == ["LOST"]


def test_a_typed_comparison_binds_the_member_of_the_field_s_declared_type(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    assert edges(workspace, "REFERENCES", "typed_comparison") == {
        ("depot-ops:ops/checks.py#lost", "depot-ops:ops/stages.py#Stage.LOST"),
        ("depot-ops:ops/checks.py#first_lost", "depot-ops:ops/stages.py#Stage.LOST"),
    }


def _impact(workspace: Workspace, seed: str) -> Any:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=seed, presentation=Presentation.FULL)
    )
    assert response.seed.status == "resolved"
    verify_steps(workspace, response)
    return response


def _names(response: Any) -> set[tuple[str, str]]:
    return {
        (c.repository, c.entity.rsplit(":", 1)[0].split(":", 3)[3]) for c in response.candidates
    }


def test_a_generated_enum_s_change_reaches_the_database_through_the_mapping(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    response = _impact(workspace, "sym:depot-api:src/gen/types.ts:Stage.Lost:4")
    found = _names(response)
    assert {
        ("depot-api", "ParcelState.lost"),
        ("depot-api", "movingParcels"),
    } <= found
    lost = next(c for c in response.candidates if c.entity.endswith(":ParcelState.lost:14"))
    assert (str(lost.step.edge), lost.step.reading) == ("MAPS_TO", "is mapped from")
    by_name = {c.name: c for c in response.candidates}
    assert by_name["movingParcels"].basis == "exact"
    # what reads another value is reached only through the enum the value belongs to
    assert {by_name[n].basis for n in ("packedParcels", "sentCount")} == {"containment"}
    # and a database nothing names, never
    assert ("depot-api", "legacySent") not in found
    assert not [c for c in response.candidates if c.repository != "depot-api"]


def test_a_schema_enum_s_change_reaches_its_queries_mappings_and_migrations(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = _names(_impact(workspace, "sym:depot-api:prisma/schema.prisma:ParcelState:11"))
    assert {
        ("depot-api", "Parcel.state"),
        ("depot-api", "packedParcels"),
        ("depot-api", "movingParcels"),
        ("depot-api", "sentCount"),
        ("depot-api", "packedSince"),
        ("depot-api", "toState"),
        ("depot-api", "isLost"),
        ("depot-api", "parcel_state"),
    } <= found
    assert not {("depot-api", n) for n in ("inState", "shadowed", "legacySent", "byColour")} & found
    assert not [r for r, _ in found if r != "depot-api"]


def test_a_comparison_s_enum_reaches_the_comparing_function_only(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())

    found = _names(_impact(workspace, "sym:depot-ops:ops/stages.py:Stage.LOST:6"))
    assert {("depot-ops", "lost"), ("depot-ops", "first_lost")} <= found
    assert ("depot-ops", "untyped") not in found
    courier = _names(_impact(workspace, "sym:depot-ops:ops/stages.py:Courier.LOST:10"))
    assert not {("depot-ops", "lost"), ("depot-ops", "first_lost")} & courier


def test_moving_the_datasource_s_variable_unbinds_the_queries_as_a_fresh_index_does(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    schema = root / "depot-api/prisma/schema.prisma"
    schema.write_text(schema.read_text().replace("DEPOT_DATABASE_URL", "OTHER_URL"))
    sync(workspace, workspace.resolver().resolve(root / "depot-api"))

    assert decisions(workspace, PERSISTENCE)[f"{Q}packedParcels"] == "database-identity-unproven"
    # a Prisma client's database is its own project's, whatever variable the datasource reads
    assert decisions(workspace, PERSISTENCE)[f"{Q}packedSince"] == "linked"
    assert {s for s, _ in edges(workspace, "REFERENCES", "persistence")} == {
        "depot-api:src/queries.ts#packedSince"
    }
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_a_model_s_new_field_type_rebinds_comparisons_as_a_fresh_index_does(
    workspace: Workspace, tmp_path: Path, settings: Settings
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"
    models = root / "depot-ops/ops/models.py"
    models.write_text(
        models.read_text()
        .replace("import Stage", "import Courier")
        .replace("Optional[Stage]", "Optional[Courier]")
    )
    sync(workspace, workspace.resolver().resolve(root / "depot-ops"))

    assert edges(workspace, "REFERENCES", "typed_comparison") == {
        ("depot-ops:ops/checks.py#lost", "depot-ops:ops/stages.py#Courier.LOST"),
        ("depot-ops:ops/checks.py#first_lost", "depot-ops:ops/stages.py#Courier.LOST"),
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
    assert {d.family for d in first.decisions} >= {PERSISTENCE, MAPPINGS, MIGRATIONS, CLIENT}
    assert fact_snapshot(workspace) == before


def test_fixture_lines_are_what_the_tests_cite() -> None:
    def line_of(text: str, needle: str) -> int:
        return next(n for n, line in enumerate(text.splitlines(), 1) if needle in line)

    assert line_of(SCHEMA, "enum ParcelState") == 11
    assert line_of(API["src/gen/types.ts"], "Lost =") == 4
    assert line_of(OPS["ops/stages.py"], '    LOST = "LOST"') == 6
