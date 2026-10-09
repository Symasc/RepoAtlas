"""Every schema-construction and resolver-map shape the GraphQL support claims, exercised.

One invented TypeScript repository and one invented Python repository, each written in the
forms the parsers say they read: Apollo Server, graphql-tools and graphql-yoga
constructions; ``import.meta.url``, ``${__dirname}`` and namespace ``fs`` paths; merged type
definitions and resolvers; spreads, methods, computed keys and ``{ subscribe }`` entries; and
Ariadne bindables imported from another module, ``ObjectType`` decorators and the anchored
path spellings. Nothing here comes from a real repository.
"""

from __future__ import annotations

import json
from pathlib import Path

from repoatlas.graph.graphql_resolvers import FAMILY as RESOLVERS
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build

SDL = (
    "type Item {\n"  # 1
    "  id: ID!\n"  # 2
    "  name: String\n"  # 3
    "}\n"  # 4
    "type Query {\n"  # 5
    "  items: [Item!]!\n"  # 6
    "  item(id: ID!): Item\n"  # 7
    "  count: Int\n"  # 8
    "}\n"  # 9
    "type Subscription {\n"  # 10
    "  ticks: Int\n"  # 11
    "}\n"  # 12
)

TS = {
    "package.json": json.dumps({"name": "shapes-ts", "private": True}),
    "src/schema/items.graphql": SDL,
    "src/schema/more/count.graphql": "extend type Query {\n  total: Int\n}\n",
    "src/items.ts": (
        "export function listItems() {\n"  # 1
        "  return [];\n"  # 2
        "}\n"  # 3
        "export function ticks() {\n"  # 4
        "  return null;\n"  # 5
        "}\n"  # 6
    ),
    "src/base.ts": (
        "import { listItems } from './items';\n"  # 1
        "export const base = {\n"  # 2
        "  Query: {\n"  # 3
        "    items: listItems,\n"  # 4
        "  },\n"  # 5
        "};\n"  # 6
    ),
    # graphql-yoga: a spread, a method, a computed key, a { subscribe } entry
    "src/yoga.ts": (
        "import { createSchema } from 'graphql-yoga';\n"  # 1
        "import { readFileSync } from 'node:fs';\n"  # 2
        "import { base } from './base';\n"  # 3
        "import { ticks } from './items';\n"  # 4
        "const key = 'count';\n"  # 5
        "export const yogaSchema = createSchema({\n"  # 6
        "  typeDefs: readFileSync(new URL('./schema/items.graphql', import.meta.url), 'utf8'),\n"
        "  resolvers: {\n"  # 8
        "    ...base,\n"  # 9
        "    Item: {\n"  # 10
        "      name(item: { name: string }) {\n"  # 11
        "        return item.name;\n"  # 12
        "      },\n"  # 13
        "    },\n"  # 14
        "    Query: {\n"  # 15
        "      [key]: () => 0,\n"  # 16
        "    },\n"  # 17
        "    Subscription: {\n"  # 18
        "      ticks: { subscribe: ticks },\n"  # 19
        "    },\n"  # 20
        "  },\n"  # 21
        "});\n"  # 22
    ),
    # graphql-tools: merged type definitions from a glob, merged resolver maps, namespace fs
    "src/tools.ts": (
        "import * as fs from 'fs';\n"  # 1
        "import { makeExecutableSchema } from '@graphql-tools/schema';\n"  # 2
        "import { mergeResolvers, mergeTypeDefs } from '@graphql-tools/merge';\n"  # 3
        "import { loadFilesSync } from '@graphql-tools/load-files';\n"  # 4
        "import { base } from './base';\n"  # 5
        "const extra = { Query: { item: () => null } };\n"  # 6
        "export const toolsSchema = makeExecutableSchema({\n"  # 7
        "  typeDefs: mergeTypeDefs([\n"  # 8
        "    fs.readFileSync(`${__dirname}/schema/items.graphql`, 'utf8'),\n"  # 9
        "    loadFilesSync(`${__dirname}/schema/more/*.graphql`),\n"  # 10
        "  ]),\n"  # 11
        "  resolvers: mergeResolvers([base, extra]),\n"  # 12
        "});\n"  # 13
    ),
    # Apollo Server, given a schema built elsewhere: no construction of its own here
    "src/apollo.ts": (
        "import { ApolloServer } from '@apollo/server';\n"
        "import { toolsSchema } from './tools';\n"
        "export const server = new ApolloServer({ schema: toolsSchema });\n"
    ),
    # Apollo Server given type definitions and resolvers itself
    "src/apollo-direct.ts": (
        "import { ApolloServer } from '@apollo/server';\n"  # 1
        "import { readFileSync } from 'fs';\n"  # 2
        "import { resolve } from 'path';\n"  # 3
        "export const direct = new ApolloServer({\n"  # 4
        "  typeDefs: readFileSync(resolve(__dirname, 'schema', 'items.graphql'), 'utf8'),\n"  # 5
        "  resolvers: { Query: { count: () => 1 } },\n"  # 6
        "});\n"  # 7
    ),
}

PY = {
    "pyproject.toml": "[project]\nname = 'shapes-py'\nversion = '0'\n",
    "shapes_py/__init__.py": "",
    "shapes_py/schema.graphql": SDL,
    "shapes_py/resolvers.py": (
        "from ariadne import ObjectType, QueryType\n"  # 1
        "\n"  # 2
        "query = QueryType()\n"  # 3
        'item = ObjectType("Item")\n'  # 4
        "\n"  # 5
        "\n"  # 6
        '@query.field("items")\n'  # 7
        "def resolve_items(_obj, _info):\n"  # 8
        "    return []\n"  # 9
        "\n"  # 10
        "\n"  # 11
        '@item.field("name")\n'  # 12
        "def resolve_name(obj, _info):\n"  # 13
        '    return obj["name"]\n'  # 14
    ),
    "shapes_py/app.py": (
        "import os\n"  # 1
        "from pathlib import Path\n"  # 2
        "\n"  # 3
        "import ariadne\n"  # 4
        "from ariadne import load_schema_from_path\n"  # 5
        "\n"  # 6
        "from .resolvers import item, query\n"  # 7
        "\n"  # 8
        'HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.graphql")\n'  # 9
        "schema = ariadne.make_executable_schema(load_schema_from_path(HERE), [query, item])\n"
        "other = ariadne.make_executable_schema(\n"  # 11
        '    load_schema_from_path(str(Path(__file__).resolve().parent / "schema.graphql")), query\n'  # noqa: E501 - 12
        ")\n"  # 13
    ),
}


def _decisions(workspace: Workspace) -> dict[str, str]:
    return {
        r.subject: r.decision for r in workspace.knowledge.list_link_decisions(family=RESOLVERS)
    }


def _references(workspace: Workspace) -> set[tuple[str, str, int]]:
    rows = workspace.database.execute(
        """
        SELECT sf.rel_path || '#' || s.qualified_name AS source,
               d.qualified_name AS target, e.line_start AS line
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id
        WHERE e.type = 'REFERENCES' AND e.parser = ? AND e.resolution LIKE '%graphql_resolvers%'
        """,
        (GLOBAL_PARSER,),
    ).fetchall()
    return {(r["source"], r["target"], r["line"]) for r in rows}


def test_every_typescript_construction_and_map_shape_is_followed(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"shapes-ts": TS})

    decided = _decisions(workspace)
    assert {
        k: v for k, v in decided.items() if "::" in k and k.count("::") == 1 and ".ts::" in k
    } == {
        # the computed key cannot be read: that one entry is unresolved, the rest is bound
        "shapes-ts:src/yoga.ts::yogaSchema": "resolvers-unresolved",
        "shapes-ts:src/tools.ts::toolsSchema": "linked",
        "shapes-ts:src/apollo-direct.ts::direct": "linked",
    }
    assert _references(workspace) == {
        # a spread of an imported map: its imported function
        ("src/items.ts#listItems", "Query.items", 4),
        # a method: the object holding it
        ("src/yoga.ts#yogaSchema", "Item.name", 11),
        # { subscribe }: the function it names
        ("src/items.ts#ticks", "Subscription.ticks", 19),
        # merged maps: an inline function's holder is the module constant holding it
        ("src/tools.ts#extra", "Query.item", 6),
        ("src/apollo-direct.ts#direct", "Query.count", 6),
    }
    # The glob loaded the extension too: its field has a decision of its own.
    assert decided["shapes-ts:src/schema/more/count.graphql::Query.total"] == "default-resolver"
    # Every schema file was reached through an anchored path.
    assert not [v for v in decided.values() if v in {"schema-unsupported", "schema-missing"}]


def test_every_ariadne_shape_is_followed(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, {"shapes-py": PY})

    decided = _decisions(workspace)
    assert decided["shapes-py:shapes_py/app.py::schema"] == "linked"
    assert decided["shapes-py:shapes_py/app.py::other"] == "linked"
    assert decided["shapes-py:shapes_py/schema.graphql::Query.items"] == "resolved"
    assert decided["shapes-py:shapes_py/schema.graphql::Item.name"] == "resolved"
    assert decided["shapes-py:shapes_py/schema.graphql::Query.count"] == "default-resolver"
    assert _references(workspace) == {
        ("shapes_py/resolvers.py#resolve_items", "Query.items", 7),
        ("shapes_py/resolvers.py#resolve_name", "Item.name", 12),
    }
