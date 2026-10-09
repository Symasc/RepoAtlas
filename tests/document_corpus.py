"""Neutral fixtures for GraphQL documents and resolvers.

Three invented repositories. ``depot-api`` owns a schema and serves it: one construction
loads it anchored to its own file and binds a resolver map built from imported objects, an
aliased import, inline functions and a reference resolver; others load type definitions in
ways that cannot be followed. ``depot-web`` declares its documents in a GraphQL Code
Generator configuration whose schema is ``depot-api``'s: documents in ``.graphql`` files and
in ``gql`` templates, a fragment spliced in from another module, a document whose text only
run time decides, and documents no configuration covers. ``depot-py`` serves a schema with
Ariadne. Nothing here comes from a real repository.
"""

from __future__ import annotations

import json

SCHEMA = (
    "enum Stage {\n"  # 1
    "  PACKED\n"  # 2
    "  SENT\n"  # 3
    "  LOST\n"  # 4
    "}\n"  # 5
    "type Parcel {\n"  # 6
    "  id: ID!\n"  # 7
    "  stage: Stage!\n"  # 8
    "  label: String!\n"  # 9
    "  weight: Int\n"  # 10
    "}\n"  # 11
    "input ParcelFilter {\n"  # 12
    "  stage: Stage\n"  # 13
    "  note: String\n"  # 14
    "}\n"  # 15
    "type Query {\n"  # 16
    "  parcels(filter: ParcelFilter, stage: Stage = PACKED): [Parcel!]!\n"  # 17
    "  parcel(id: ID!): Parcel\n"  # 18
    "}\n"  # 19
    "type Mutation {\n"  # 20
    "  ship(id: ID!): Parcel\n"  # 21
    "}\n"  # 22
)

API = {
    "package.json": json.dumps({"name": "depot-api", "private": True}),
    "schema/depot.graphql": SCHEMA,
    "schema/extra.graphql": "type Crate {\n  id: ID!\n}\ntype Query {\n  crates: [Crate!]!\n}\n",
    "src/server.ts": (
        "import { readFileSync } from 'node:fs';\n"  # 1
        "import { join } from 'node:path';\n"  # 2
        "import { buildSubgraphSchema } from '@apollo/subgraph';\n"  # 3
        "import { parse } from 'graphql';\n"  # 4
        "import { resolvers } from './resolvers';\n"  # 5
        "const typeDefs = parse(readFileSync(join(__dirname, '../schema/depot.graphql'), 'utf8'));\n"  # noqa: E501 - 6
        "export const schema = buildSubgraphSchema([{ typeDefs, resolvers }]);\n"  # 7
    ),
    "src/resolvers/index.ts": (
        "import { ship as dispatch, find } from '../parcels';\n"  # 1
        "import { queryResolvers } from './query';\n"  # 2
        "export const resolvers = {\n"  # 3
        "  Query: queryResolvers,\n"  # 4
        "  Mutation: {\n"  # 5
        "    ship: dispatch,\n"  # 6
        "  },\n"  # 7
        "  Parcel: {\n"  # 8
        "    stage: (parcel: { stage: string }) => parcel.stage,\n"  # 9
        "    __resolveReference: (ref: { id: string }) => find(ref.id),\n"  # 10
        "  },\n"  # 11
        "  Ghost: {\n"  # 12
        "    boo: () => 1,\n"  # 13
        "  },\n"  # 14
        "};\n"  # 15
    ),
    "src/resolvers/query.ts": (
        "import { listParcels } from '../parcels';\n"  # 1
        "export const queryResolvers = {\n"  # 2
        "  parcels: listParcels,\n"  # 3
        "  parcel: (_parent: unknown, args: { id: string }) => ({ id: args.id }),\n"  # 4
        "};\n"  # 5
    ),
    "src/parcels.ts": (
        "export async function listParcels() {\n"  # 1
        "  return [];\n"  # 2
        "}\n"  # 3
        "export async function ship(_parent: unknown, args: { id: string }) {\n"  # 4
        "  return { id: args.id };\n"  # 5
        "}\n"  # 6
        "export function find(id: string) {\n"  # 7
        "  return { id };\n"  # 8
        "}\n"  # 9
    ),
    # Relative to whatever directory the process runs in: not something the source settles.
    "src/legacy-server.ts": (
        "import { readFileSync } from 'fs';\n"
        "import { makeExecutableSchema } from '@graphql-tools/schema';\n"
        "export const legacy = makeExecutableSchema({\n"
        "  typeDefs: readFileSync('schema/depot.graphql', 'utf8'),\n"
        "  resolvers: {},\n"
        "});\n"
    ),
    "src/inline.ts": (
        "import { createSchema } from 'graphql-yoga';\n"
        "import { gql } from 'graphql-tag';\n"
        "export const inline = createSchema({ typeDefs: gql`type Query { a: Int }`, resolvers: {} });\n"  # noqa: E501
    ),
    # A map from a package: nothing in this tree says what it resolves.
    "src/packaged.ts": (
        "import { join } from 'path';\n"
        "import { makeExecutableSchema } from '@graphql-tools/schema';\n"
        "import { loadFilesSync } from '@graphql-tools/load-files';\n"
        "import { cratesResolvers } from 'crates-kit';\n"
        "export const packaged = makeExecutableSchema({\n"
        "  typeDefs: loadFilesSync(join(__dirname, '../schema/extra.graphql')),\n"
        "  resolvers: cratesResolvers,\n"
        "});\n"
    ),
    # Named like the library's function, but this file's own: not a construction.
    "src/fake.ts": (
        "function buildSubgraphSchema(x: unknown) { return x; }\n"
        "export const fake = buildSubgraphSchema([{ typeDefs: 1, resolvers: {} }]);\n"
    ),
}

WEB = {
    "package.json": json.dumps({"name": "depot-web", "private": True}),
    "codegen.yml": (
        "schema: ../depot-api/schema/depot.graphql\n"
        "documents:\n"
        "  - src/**/*.graphql\n"
        "  - src/**/*.ts\n"
        "  - '!src/ignored/**'\n"
        "generates:\n"
        "  src/gen.ts:\n"
        "    plugins: [typescript, typescript-operations]\n"
    ),
    "src/queries/parcels.graphql": (
        "query PackedParcels($stage: Stage = SENT) {\n"  # 1
        "  parcels(stage: PACKED, filter: { stage: LOST }) {\n"  # 2
        "    id\n"  # 3
        "    ...ParcelCard\n"  # 4
        "    ... on Parcel {\n"  # 5
        "      weight\n"  # 6
        "    }\n"  # 7
        "  }\n"  # 8
        "}\n"  # 9
        "fragment ParcelCard on Parcel {\n"  # 10
        "  label\n"  # 11
        "  stage\n"  # 12
        "}\n"  # 13
        "query Broken {\n"  # 14
        "  parcels {\n"  # 15
        "    nope\n"  # 16
        "  }\n"  # 17
        "}\n"  # 18
    ),
    "src/fragments.ts": (
        "import { gql } from '@apollo/client';\n"  # 1
        "export const CARD = gql`\n"  # 2
        "  fragment ParcelCardFields on Parcel {\n"  # 3
        "    label\n"  # 4
        "    stage\n"  # 5
        "  }\n"  # 6
        "`;\n"  # 7
    ),
    "src/hooks.ts": (
        "import { gql } from '@apollo/client';\n"  # 1
        "import { CARD } from './fragments';\n"  # 2
        "export const WITH_CARD = gql`\n"  # 3
        "  query WithCard {\n"  # 4
        '    parcel(id: "1") {\n'  # 5
        "      ...ParcelCardFields\n"  # 6
        "    }\n"  # 7
        "  }\n"  # 8
        "  ${CARD}\n"  # 9
        "`;\n"  # 10
        "export function WithCard(): number {\n"  # 11  a TS function the document's name shares
        "  return helper();\n"  # 12
        "}\n"  # 13
        "function helper(): number {\n"  # 14
        "  return 1;\n"  # 15
        "}\n"  # 16
        "export function dynamic(stage: string) {\n"  # 17
        "  return gql`query Dyn { parcels(stage: ${stage}) { id } }`;\n"  # 18
        "}\n"  # 19
        "export const BROKEN = gql`query {`;\n"  # 20
    ),
    "src/ignored/old.ts": (
        "import { gql } from '@apollo/client';\n"
        "export const OLD = gql`query Old { parcels { id } }`;\n"
    ),
    "scripts/probe.ts": (
        "import { gql } from '@apollo/client';\n"
        "export const PROBE = gql`query Probe { parcels { id stage } }`;\n"
    ),
}

PY = {
    "pyproject.toml": "[project]\nname = 'depot-py'\nversion = '0'\n",
    "depot_py/__init__.py": "",
    "depot_py/schema/main.graphql": (
        "type Crate {\n"  # 1
        "  id: ID!\n"  # 2
        "  size: Int\n"  # 3
        "}\n"  # 4
        "type Query {\n"  # 5
        "  crates(kind: String): [Crate!]!\n"  # 6
        "  crate(id: ID!): Crate\n"  # 7
        "}\n"  # 8
    ),
    "depot_py/crates.py": "def list_crates(_obj, _info, kind=None):\n    return []\n",
    "depot_py/app.py": (
        "import os\n"  # 1
        "\n"  # 2
        "from ariadne import ObjectType, QueryType, load_schema_from_path, make_executable_schema\n"
        "\n"  # 4
        "from depot_py.crates import list_crates as crates_for\n"  # 5
        "\n"  # 6
        'type_defs = load_schema_from_path(os.path.join(os.path.dirname(__file__), "schema"))\n'
        "\n"  # 8
        "query = QueryType()\n"  # 9
        'query.set_field("crates", crates_for)\n'  # 10
        'crate = ObjectType("Crate")\n'  # 11
        'crate.set_field("size", lambda obj, info: 1)\n'  # 12
        "\n"  # 13
        "\n"  # 14
        '@query.field("crate")\n'  # 15
        "def resolve_crate(_obj, _info, id):\n"  # 16
        '    return {"id": id}\n'  # 17
        "\n"  # 18
        "\n"  # 19
        "schema = make_executable_schema(type_defs, query, crate)\n"  # 20
    ),
    "depot_py/cwd.py": (
        "from ariadne import QueryType, load_schema_from_path, make_executable_schema\n"
        "query = QueryType()\n"
        'schema = make_executable_schema(load_schema_from_path("depot_py/schema"), query)\n'
    ),
}


def repos() -> dict[str, dict[str, str]]:
    return {"depot-api": dict(API), "depot-web": dict(WEB), "depot-py": dict(PY)}
