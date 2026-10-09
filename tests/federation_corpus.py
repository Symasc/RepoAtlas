"""Neutral fixtures for federated schema copies, their provenance and what is generated from them.

Three invented repositories. ``parcel-api`` serves a subgraph: one construction loads its
schema, a Helm chart released by its Tiltfile gives it the identity ``http://parcels``, and
a compose service named ``cache`` runs an image built elsewhere; it also keeps an unloaded
draft schema. ``parcel-web`` keeps a supergraph copy a ``package.json`` script fetches,
generates TypeScript types, operation types and typed documents from it -- one enum value
mapped explicitly, one output importing the enum instead -- and uses them. ``parcel-sdk``
keeps an older copy a Python script downloads, and generates Python enums from it with
ariadne-codegen. Nothing here comes from a real repository.
"""

from __future__ import annotations

import json

PROVIDER = (
    "enum Stage {\n"  # 1
    "  PACKED\n"  # 2
    "  SENT\n"  # 3
    "  LOST\n"  # 4
    "}\n"  # 5
    "type Parcel {\n"  # 6
    "  id: ID!\n"  # 7
    "  stage: Stage!\n"  # 8
    "}\n"  # 9
    "type Shelf {\n"  # 10
    "  id: ID!\n"  # 11
    "}\n"  # 12
    "type Query {\n"  # 13
    "  parcels(stage: Stage): [Parcel!]!\n"  # 14
    "}\n"  # 15
)

_MACHINERY = (
    "schema\n"
    '  @link(url: "https://specs.apollo.dev/link/v1.0")\n'
    '  @link(url: "https://specs.apollo.dev/join/v0.3", for: EXECUTION)\n'
    "{\n"
    "  query: Query\n"
    "}\n"
    "directive @join__enumValue(graph: join__Graph!) repeatable on ENUM_VALUE\n"
    "directive @join__field(graph: join__Graph) repeatable on FIELD_DEFINITION\n"
    "directive @join__graph(name: String!, url: String!) on ENUM_VALUE\n"
    "directive @join__type(graph: join__Graph!) repeatable on OBJECT | ENUM\n"
    "directive @link(url: String, for: link__Purpose) repeatable on SCHEMA\n"
    "enum link__Purpose {\n"
    "  SECURITY\n"
    "  EXECUTION\n"
    "}\n"
)


def supergraph(*values: str) -> str:
    """A supergraph composing ``parcels`` (tied by deployment), ``routes`` (an origin nothing
    deploys) and ``cache`` (an origin whose image is built elsewhere)."""
    stage = "".join(f"  {v} @join__enumValue(graph: PARCELS)\n" for v in values)
    return (
        _MACHINERY
        + "enum join__Graph {\n"
        + '  PARCELS @join__graph(name: "parcels", url: "http://parcels/graphql")\n'
        + '  ROUTES @join__graph(name: "routes", url: "http://routes.example.invalid/graphql")\n'
        + '  CACHE @join__graph(name: "cache", url: "http://cache:4000/graphql")\n'
        + "}\n"
        + "enum Stage @join__type(graph: PARCELS) {\n"
        + stage
        + "}\n"
        + "type Parcel @join__type(graph: PARCELS) {\n"
        + "  id: ID!\n"
        + "  stage: Stage!\n"
        + "}\n"
        + "type Route @join__type(graph: ROUTES) {\n"
        + "  id: ID!\n"
        + "}\n"
        + "type Shelf @join__type(graph: CACHE) {\n"
        + "  id: ID!\n"
        + "}\n"
        + "type Query @join__type(graph: PARCELS) {\n"
        + "  parcels(stage: Stage): [Parcel!]! @join__field(graph: PARCELS)\n"
        + "}\n"
    )


API = {
    "package.json": json.dumps({"name": "parcel-api", "private": True}),
    "schema/parcels.graphql": PROVIDER,
    # A copy no construction loads and no composition states: provenance unproven.
    "schema/draft.graphql": "enum Stage {\n  PACKED\n}\n",
    "src/server.ts": (
        "import { readFileSync } from 'node:fs';\n"
        "import { join } from 'node:path';\n"
        "import { buildSubgraphSchema } from '@apollo/subgraph';\n"
        "import { parse } from 'graphql';\n"
        "const typeDefs = parse(readFileSync(join(__dirname, '../schema/parcels.graphql'), 'utf8'));\n"  # noqa: E501
        "export const schema = buildSubgraphSchema([{ typeDefs, resolvers: {} }]);\n"
    ),
    "charts/parcels/Chart.yaml": "apiVersion: v2\nname: parcels\nversion: 1.0.0\n",
    "charts/parcels/templates/service.yaml": (
        "kind: Service\nmetadata:\n  name: parcels\nspec:\n  ports:\n    - port: 80\n"
    ),
    "Tiltfile": (
        "docker_build('parcels', '.')\nk8s_yaml(helm('charts/parcels', name='parcels'))\n"
    ),
    # A service of this repository's deployment whose image is built somewhere else.
    "docker-compose.yml": (
        "services:\n"
        "  cache:\n"
        "    image: registry.example.invalid/cache:1\n"
        "    ports:\n"
        '      - "4000:4000"\n'
    ),
}

GENERATED = (
    "import type { TypedDocumentNode as DocumentNode } from '@graphql-typed-document-node/core';\n"  # 1  # noqa: E501
    "export type Maybe<T> = T | null;\n"  # 2
    "export enum Stage {\n"  # 3
    "  Lost = 'missing',\n"  # 4
    "  Packed = 'PACKED',\n"  # 5
    "  Sent = 'SENT'\n"  # 6
    "}\n"  # 7
    "export type Parcel = {\n"  # 8
    "  id: string;\n"  # 9
    "  stage: Stage;\n"  # 10
    "};\n"  # 11
    "export type Route = { id: string };\n"  # 12
    "export type Shelf = { id: string };\n"  # 13
    "export type Query = { parcels: Array<Parcel> };\n"  # 14
    "export type SentParcelsQueryVariables = { [key: string]: never };\n"  # 15
    "export type SentParcelsQuery = { parcels: Array<{ id: string }> };\n"  # 16
    "export type ParcelStageFragment = { stage: Stage };\n"  # 17
    "export const SentParcelsDocument = { kind: 'Document', definitions: [] } as unknown as DocumentNode<SentParcelsQuery, SentParcelsQueryVariables>;\n"  # 18  # noqa: E501
)

WEB = {
    "package.json": json.dumps(
        {
            "name": "parcel-web",
            "private": True,
            "scripts": {
                "schema:fetch": (
                    "npx rover supergraph fetch parcels@current"
                    " --output src/graphql/supergraph.graphql"
                ),
                "data:fetch": "curl -o fixtures/data.json https://data.example.invalid/x.json",
                "codegen": "graphql-codegen --config codegen.yml",
            },
        },
        indent=2,
    ),
    "codegen.yml": (
        "schema: src/graphql/supergraph.graphql\n"  # 1
        "documents:\n"  # 2
        "  - src/**/*.graphql\n"  # 3
        "generates:\n"  # 4
        "  src/gen/types.ts:\n"  # 5
        "    plugins:\n"  # 6
        "      - typescript\n"  # 7
        "      - typescript-operations\n"  # 8
        "      - typed-document-node\n"  # 9
        "    config:\n"  # 10
        "      enumValues:\n"  # 11
        "        Stage:\n"  # 12
        "          LOST: missing\n"  # 13
        "  src/gen/imported.ts:\n"  # 14
        "    plugins:\n"  # 15
        "      - typescript\n"  # 16
        "    config:\n"  # 17
        "      enumValues:\n"  # 18
        "        Stage: ./stage#Stage\n"  # 19
    ),
    "src/graphql/supergraph.graphql": supergraph("PACKED", "SENT", "LOST"),
    "src/gen/types.ts": GENERATED,
    "src/gen/imported.ts": (
        "import { Stage } from './stage';\n"
        "export type Parcel = {\n"
        "  id: string;\n"
        "  stage: Stage;\n"
        "};\n"
    ),
    "src/gen/stage.ts": "export enum Stage {\n  Packed = 'PACKED',\n}\n",
    "src/queries/parcels.graphql": (
        "query SentParcels {\n"  # 1
        "  parcels(stage: SENT) {\n"  # 2
        "    id\n"  # 3
        "  }\n"  # 4
        "}\n"  # 5
        "fragment ParcelStage on Parcel {\n"  # 6
        "  stage\n"  # 7
        "}\n"  # 8
        "query lowParcels {\n"  # 9
        "  parcels {\n"  # 10
        "    id\n"  # 11
        "  }\n"  # 12
        "}\n"  # 13
    ),
    # The same operation name twice in one document set: the generator refuses it.
    "src/queries/twice.graphql": "query Twice {\n  parcels {\n    id\n  }\n}\n",
    "src/queries/again.graphql": "query Twice {\n  parcels {\n    stage\n  }\n}\n",
    "src/pages/sent.ts": (
        "import { useQuery } from '@apollo/client';\n"
        "import { SentParcelsDocument } from '../gen/types';\n"
        "export function useSent() {\n"
        "  return useQuery(SentParcelsDocument);\n"
        "}\n"
    ),
}

SDK = {
    "pyproject.toml": (
        "[project]\n"
        'name = "parcel-sdk"\n'
        'version = "0.1.0"\n'
        "\n"
        "[tool.ariadne-codegen]\n"
        'schema_path = "schema/supergraph.graphql"\n'
        'queries_path = "queries"\n'
        'target_package_name = "parcel_sdk"\n'
    ),
    # An older copy: it lacks LOST, and still has a value the provider has dropped.
    "schema/supergraph.graphql": supergraph("PACKED", "SENT", "RETURNED"),
    "queries/parcels.graphql": (
        "query ListParcels($stage: Stage) {\n  parcels(stage: $stage) {\n    id\n  }\n}\n"
    ),
    "parcel_sdk/__init__.py": "",
    "parcel_sdk/enums.py": (
        "from enum import Enum\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "class Stage(str, Enum):\n"  # 4
        '    PACKED = "PACKED"\n'  # 5
        '    RETURNED = "RETURNED"\n'  # 6
        '    SENT = "SENT"\n'  # 7
    ),
    "parcel_sdk/moving.py": (
        "from parcel_sdk.enums import Stage\n"
        "\n"
        "\n"
        "def is_moving(stage: Stage) -> bool:\n"
        "    return stage == Stage.SENT\n"
    ),
    "scripts/fetch_schema.py": (
        "from pathlib import Path\n"  # 1
        "\n"  # 2
        "import requests\n"  # 3
        "\n"  # 4
        'URL = "https://user:secret@registry.example.invalid/supergraph?key=abc"\n'  # 5
        'TARGET = Path(__file__).resolve().parent.parent / "schema" / "supergraph.graphql"\n'  # 6
        "\n"  # 7
        "\n"  # 8
        "def main() -> None:\n"  # 9
        "    TARGET.write_text(requests.get(URL, timeout=30).text)\n"  # 10
    ),
    # Downloads JSON that is not a schema: no fetch.
    "scripts/fetch_rates.py": (
        "from pathlib import Path\n"
        "\n"
        "import requests\n"
        "\n"
        'TARGET = Path(__file__).parent / "rates.json"\n'
        "\n"
        "\n"
        "def main() -> None:\n"
        '    TARGET.write_text(requests.get("https://rates.example.invalid").text)\n'
    ),
}


def repos() -> dict[str, dict[str, str]]:
    return {"parcel-api": dict(API), "parcel-web": dict(WEB), "parcel-sdk": dict(SDK)}
