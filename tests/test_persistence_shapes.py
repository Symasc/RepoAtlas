"""Every shape V1_SPEC §7.13 says persistence reads, each exercised once on invented fixtures.

``tests/test_persistence.py`` pins the guarantees; this pins the breadth: each client
construction, query sink, Knex form, mapping form, comparison form, generator output and
migration statement the specification names, read and decided the way it says.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from repoatlas.graph.persistence import FAMILY as PERSISTENCE
from repoatlas.graph.prisma_schema import CLIENT, MIGRATIONS
from repoatlas.graph.value_mappings import FAMILY as MAPPINGS
from repoatlas.parsers.dashboards import dashboard_references
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.test_persistence import decisions, edges, evidence


def _claims(result: Any, role: str) -> dict[str, dict[str, Any]]:
    return {
        str(r.meta.get("owner") or r.source_symbol): dict(r.meta)
        for r in result.references
        if r.meta.get(PENDING_FACT) == role
    }


# ---------------------------------------------------------------------------
# TypeScript
# ---------------------------------------------------------------------------

TS = (
    "const knex = require('knex');\n"  # 1
    "import * as pg from 'pg';\n"  # 2
    "export const db = knex({ connection: process.env['STORE_URL'] });\n"  # 3
    "export const pool = new pg.Pool({ connectionString: process.env.STORE_URL });\n"  # 4
    "export function fromRoot() {\n"  # 5
    "  return db.select('id').from('crates').where('kind', '<>', 'empty');\n"  # 6
    "}\n"  # 7
    "export function objectWhere() {\n"  # 8
    "  return db('crates').where({ kind: 'full' }).whereNot('size', 'big');\n"  # 9
    "}\n"  # 10
    "export function notIn() {\n"  # 11
    "  return db('crates').whereNotIn('kind', ['empty']);\n"  # 12
    "}\n"  # 13
    "export function raw() {\n"  # 14
    "  return db.raw(\"SELECT id FROM crates WHERE kind = 'full'\");\n"  # 15
    "}\n"  # 16
    "export function unsafe(kind: string) {\n"  # 17
    "  return pool.query('SELECT id FROM crates WHERE kind = ' + kind);\n"  # 18
    "}\n"  # 19
    "export function computedOp() {\n"  # 20
    "  return db('crates').where('kind', 'like', 'f%');\n"  # 21
    "}\n"  # 22
)


def test_every_typescript_client_and_query_form() -> None:
    result = TypeScriptParser().parse(Path("src/store.ts"), TS)
    clients = _claims(result, "persistence-client")
    assert {k: (v["library"], v.get("env")) for k, v in clients.items()} == {
        "db": ("knex", "STORE_URL"),
        "pool": ("pg", "STORE_URL"),
    }
    queries = _claims(result, "persistence-query")
    predicates = {k: json.loads(v.get("predicates") or "[]") for k, v in queries.items()}
    assert predicates["fromRoot"] == [["kind", "<>", ["empty"]]]
    assert json.loads(queries["fromRoot"]["tables"]) == [["crates", "crates"]]
    assert predicates["objectWhere"] == [["kind", "=", ["full"]], ["size", "<>", ["big"]]]
    assert predicates["notIn"] == [["kind", "<>", ["empty"]]]
    assert predicates["raw"] == [["kind", "=", ["full"], "crates"]]
    assert queries["unsafe"]["dynamic"] == "true"
    # an operator other than = / <> compares nothing this pass reads
    assert "computedOp" not in queries


@pytest.mark.parametrize(
    ("declaration", "entries"),
    [
        (
            "export const m: Record<Kind, string> = { full: 'F', 'empty': 'E', 3: 'x' };",
            [
                ["literal", "full", "literal", "F"],
                ["literal", "empty", "literal", "E"],
                ["literal", "3", "literal", "x"],
            ],
        ),
        (
            "export const m: Record<Kind, Other> = { [Kind.Full]: Other.A, [f()]: Other.B };",
            [["member", "Kind.Full", "member", "Other.A"]],
        ),
        ("export const m: Partial<Record<Kind, string>> = { full: 'F' };", None),
        ("export const m: Record<Kind, string> = build();", None),
    ],
)
def test_typescript_mapping_forms(declaration: str, entries: list[list[str]] | None) -> None:
    found = _claims(TypeScriptParser().parse(Path("m.ts"), declaration), "value-mapping")
    if entries is None:
        assert not found
        return
    assert [e[:4] for e in json.loads(found["m"]["entries"])] == entries


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

PY = (
    "import os\n"  # 1
    "from collections.abc import Sequence\n"  # 2
    "\n"  # 3
    "import psycopg\n"  # 4
    "from sqlalchemy import create_engine, text\n"  # 5
    "\n"  # 6
    "from crates.kinds import Kind\n"  # 7
    "from crates.models import Crate\n"  # 8
    "\n"  # 9
    'CONN = psycopg.connect(os.getenv("STORE_URL"))\n'  # 10
    'ENGINE = create_engine(os.environ.get("STORE_URL"))\n'  # 11
    "\n"  # 12
    "\n"  # 13
    "def full() -> int:\n"  # 14
    "    return CONN.execute(\"SELECT count(*) FROM crates WHERE kind = 'full'\").fetchone()[0]\n"
    "\n"  # 16
    "\n"  # 17
    "def through_engine():\n"  # 18
    "    return text(\"SELECT id FROM crates WHERE kind = 'empty'\")\n"  # 19
    "\n"  # 20
    "\n"  # 21
    "def tupled(crates: tuple[Crate, ...]) -> list[Crate]:\n"  # 22
    "    return [c for c in crates if c.kind != 'EMPTY']\n"  # 23
    "\n"  # 24
    "\n"  # 25
    "def sequenced(crates: Sequence[Crate]) -> bool:\n"  # 26
    "    return any('FULL' == c.kind for c in crates)\n"  # 27
    "\n"  # 28
    "\n"  # 29
    "def single(crate: Crate) -> bool:\n"  # 30
    "    return crate.kind in ['FULL', 'EMPTY']\n"  # 31
    "\n"  # 32
    "\n"  # 33
    "def mixed(crate: Crate) -> bool:\n"  # 34
    "    return crate.kind == other()\n"  # 35
    "\n"  # 36
    "\n"  # 37
    "TO_CODE: dict[Kind, Kind] = {Kind.FULL: Kind.EMPTY, 'EMPTY': Kind.FULL}\n"  # 38
)


def test_every_python_client_query_and_comparison_form() -> None:
    result = PythonParser().parse(Path("crates/store.py"), PY)
    clients = _claims(result, "persistence-client")
    assert {k: (v["library"], v.get("env")) for k, v in clients.items()} == {
        "CONN": ("psycopg", "STORE_URL"),
        "ENGINE": ("sqlalchemy", "STORE_URL"),
    }
    queries = _claims(result, "persistence-query")
    assert (queries["full"]["binding"], queries["full"]["receiver"]) == ("module", "CONN")
    assert (queries["through_engine"]["library"], queries["through_engine"]["binding"]) == (
        "sqlalchemy",
        "unbound",
    )
    comparisons = [
        (r.source_symbol, r.meta["values"])
        for r in result.references
        if r.meta.get(PENDING_FACT) == "typed-comparison"
    ]
    assert comparisons == [
        ("tupled", '["EMPTY"]'),
        ("sequenced", '["FULL"]'),
        ("single", '["FULL", "EMPTY"]'),
    ]
    [mapping] = _claims(result, "value-mapping").values()
    assert json.loads(mapping["entries"]) == [
        ["member", "Kind.FULL", "member", "Kind.EMPTY", 38],
        ["literal", "EMPTY", "member", "Kind.FULL", 38],
    ]


@pytest.mark.parametrize(
    "annotation", ["Optional[Kind]", "Kind | None", "'Kind'", "typing.Optional[Kind]"]
)
def test_a_field_annotation_names_one_type(annotation: str) -> None:
    source = f"import typing\nfrom typing import Optional\nclass Crate:\n    kind: {annotation}\n"
    [crate] = [s for s in PythonParser().parse(Path("m.py"), source).symbols if s.name == "Crate"]
    assert json.loads(crate.meta["fields"]) == {"kind": "Kind"}


# ---------------------------------------------------------------------------
# dashboards
# ---------------------------------------------------------------------------


def test_dashboards_read_row_panels_and_variables() -> None:
    dashboard = {
        "panels": [
            {"type": "row", "panels": [{"targets": [{"rawSql": "SELECT 1 FROM t WHERE a = 'b'"}]}]},
            "not a panel",
            {"targets": ["not a target", {"rawSql": "not sql (("}]},
        ],
        "templating": {"list": [{"query": "SELECT a FROM t"}, {"query": {"structured": 1}}]},
    }
    document = yaml.compose(json.dumps(dashboard), Loader=yaml.SafeLoader)
    found = dashboard_references(PurePosixPath("d.json"), [document])
    assert {
        (c.meta["source"], c.meta.get("unsupported"), c.meta.get("predicates")) for c in found
    } == {
        ("panel", None, '[["a","=",["b"],"t"]]'),
        ("panel", "true", None),
        ("variable", None, "[]"),
    }


# ---------------------------------------------------------------------------
# the global pass: generators, migrations, identities
# ---------------------------------------------------------------------------


def _schema(env: str, *, output: str | None = None, provider: str | None = "postgresql") -> str:
    generator = '  provider = "prisma-client-js"\n' + (f'  output = "{output}"\n' if output else "")
    source = f'  provider = "{provider}"\n' if provider else ""
    return (
        f'datasource db {{\n{source}  url = env("{env}")\n}}\n'
        f"generator client {{\n{generator}}}\n"
        'enum Kind {\n  FULL\n  EMPTY\n  @@map("crate_kind")\n}\n'
        'model Crate {\n  id String @id\n  kind Kind\n  @@map("crates")\n}\n'
    )


SHAPES = {
    "store": {
        "package.json": json.dumps({"name": "store", "private": True}),
        # an output under node_modules names the package code imports
        "a/prisma/schema.prisma": _schema("A_URL", output="../../node_modules/@prisma/a-client"),
        "a/prisma/migrations/1_init/migration.sql": (
            "CREATE TYPE \"crate_kind\" AS ENUM ('FULL', 'GONE');\n"
            "CREATE TYPE \"old_kind\" AS ENUM ('X');\n"
        ),
        "a/prisma/migrations/2_rename/migration.sql": (
            "ALTER TYPE \"crate_kind\" RENAME VALUE 'GONE' TO 'EMPTY';\n"
            'ALTER TYPE "old_kind" RENAME TO "older_kind";\n'
            'DROP TYPE "older_kind";\n'
        ),
        # two schemas generating the same default client
        "b/prisma/schema.prisma": _schema("B_URL"),
        "c/prisma/schema.prisma": _schema("C_URL"),
        # a schema with migrations but no provider stated, and one with no migration of its enum
        "d/prisma/schema.prisma": _schema("D_URL", provider=None),
        "d/prisma/migrations/1_init/migration.sql": "CREATE TABLE t (id int);\n",
        "e/prisma/schema.prisma": _schema("E_URL"),
        "e/prisma/migrations/1_init/migration.sql": "CREATE TABLE t (id int);\n",
        "src/a.ts": (
            "import { Kind, Crate } from '@prisma/a-client';\n"
            "export function full(crate: Crate): boolean {\n"
            "  return crate.kind === Kind.FULL;\n"
            "}\n"
        ),
        "src/b.ts": (
            "import { Kind } from '@prisma/client';\nexport const ANY: Kind[] = [Kind.FULL];\n"
        ),
        "src/clients.ts": (
            "import { PrismaClient } from '@prisma/a-client';\n"
            "import knex from 'knex';\n"
            "export const a = new PrismaClient();\n"
            "export const b = knex({ connection: process.env.B_URL });\n"
            "export const unnamed = knex({ connection: 'postgres://literal' });\n"
        ),
        "src/queries.ts": (
            "import { a, b, unnamed } from './clients';\n"
            "export function prismaRaw() {\n"
            "  return a.$queryRawUnsafe(\"SELECT id FROM crates WHERE kind = 'EMPTY'\");\n"
            "}\n"
            "export function unreadable() {\n"
            "  return a.$queryRawUnsafe('SELECT FROM WHERE ((');\n"
            "}\n"
            "export function viaB() {\n"
            "  return b('crates').where('kind', 'FULL');\n"
            "}\n"
            "export function noVariable() {\n"
            "  return unnamed('crates').where('kind', 'FULL');\n"
            "}\n"
            "export function notAClient() {\n"
            "  return other('crates').where('kind', 'FULL');\n"
            "}\n"
        ),
    }
}


def test_generators_migrations_and_identities_of_every_shape(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, SHAPES)

    assert decisions(workspace, CLIENT) == {
        "store:@prisma/a-client": "linked",
        "store:@prisma/client": "client-ambiguous",
    }
    assert edges(workspace, "REFERENCES", "prisma_client") == {
        ("store:src/a.ts#full", "store:a/prisma/schema.prisma#Crate"),
        ("store:src/a.ts#full", "store:a/prisma/schema.prisma#Kind"),
    }
    assert decisions(workspace, MIGRATIONS) == {
        # created with GONE, renamed to EMPTY: what the migrations leave is the enum
        "store:a/prisma/schema.prisma::Kind": "linked",
        "store:d/prisma/migrations/1_init/migration.sql": "dialect-unknown",
        "store:e/prisma/schema.prisma::Kind": "migration-missing",
    }
    assert {t.split("#")[0] for _, t in edges(workspace, "GENERATES", "prisma_migrate")} == {
        "store:a/prisma/migrations/1_init/migration.sql",
        "store:a/prisma/migrations/2_rename/migration.sql",
    }
    q = "store:src/queries.ts::"
    assert decisions(workspace, PERSISTENCE) == {
        f"{q}prismaRaw": "linked",
        f"{q}unreadable": "sql-unsupported",
        f"{q}viaB": "linked",
        f"{q}noVariable": "database-identity-unproven",
        f"{q}notAClient": "receiver-unproven",
    }
    assert evidence(workspace, PERSISTENCE, f"{q}viaB")["queries"][0]["schema"] == (
        "store/b/prisma/schema.prisma"
    )


def test_a_mapping_between_members_of_one_enum_and_literal_keys(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(
        workspace,
        tmp_path,
        {
            "crates": {
                "pyproject.toml": '[project]\nname = "crates"\nversion = "0.1.0"\n',
                "crates/__init__.py": "",
                "crates/kinds.py": (
                    "from enum import Enum\n\n\nclass Kind(str, Enum):\n"
                    '    FULL = "FULL"\n    EMPTY = "EMPTY"\n'
                ),
                "crates/swap.py": (
                    "from crates.kinds import Kind\n\n"
                    "SWAP: dict[Kind, Kind] = {Kind.FULL: Kind.EMPTY, 'EMPTY': Kind.FULL}\n"
                ),
            }
        },
    )

    assert decisions(workspace, MAPPINGS) == {"crates:crates/swap.py::SWAP": "linked"}
    kind = "crates:crates/kinds.py#Kind"
    assert edges(workspace, "MAPS_TO") == {
        (kind, kind),
        (f"{kind}.FULL", f"{kind}.EMPTY"),
        (f"{kind}.EMPTY", f"{kind}.FULL"),
    }
