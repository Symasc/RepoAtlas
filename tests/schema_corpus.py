"""Neutral fixtures for configured GraphQL code generation.

Two invented repositories. ``parcel-api`` owns a schema, generates TypeScript from it,
and uses the generated enum; it also holds an unloaded duplicate schema and a hand-written
enum of the same name, neither of which any configuration names. ``parcel-web`` generates
from ``parcel-api``'s schema across the repository boundary, and declares one output for
every way an output can be refused. Nothing here comes from a real repository.
"""

from __future__ import annotations

import json

SCHEMA = (
    '"""Where a parcel is."""\n'  # 1
    "enum Stage {\n"  # 2
    "  PACKED\n"  # 3
    "  SENT\n"  # 4
    "  LOST\n"  # 5
    "}\n"  # 6
    "type Parcel {\n"  # 7
    "  id: ID!\n"  # 8
    "  stage: Stage!\n"  # 9
    "}\n"  # 10
    "input ParcelFilter {\n"  # 11
    "  stage: Stage\n"  # 12
    "}\n"  # 13
    "type Query {\n"  # 14
    "  parcels(filter: ParcelFilter): [Parcel!]!\n"  # 15
    "}\n"  # 16
    "scalar Weight\n"  # 17
)

GENERATED = (
    "export type Maybe<T> = T | null;\n"  # 1
    "export enum Stage {\n"  # 2
    "  Lost = 'LOST',\n"  # 3
    "  Packed = 'PACKED',\n"  # 4
    "  Sent = 'SENT'\n"  # 5
    "}\n"  # 6
    "export type Parcel = {\n"  # 7
    "  id: string;\n"  # 8
    "  stage: Stage;\n"  # 9
    "};\n"  # 10
    "export type ParcelFilter = {\n"  # 11
    "  stage?: Maybe<Stage>;\n"  # 12
    "};\n"  # 13
    "export type Query = {\n"  # 14
    "  parcels: Array<Parcel>;\n"  # 15
    "};\n"  # 16
)

API = {
    "package.json": json.dumps({"name": "parcel-api", "private": True}),
    "codegen.yml": (
        "schema: schema/parcels.graphql\n"  # 1
        "generates:\n"  # 2
        "  src/gen/types.ts:\n"  # 3
        "    plugins:\n"  # 4
        "      - typescript\n"  # 5
    ),
    "schema/parcels.graphql": SCHEMA,
    # The same enum, in a file no configuration loads: never a source of anything.
    "schema/legacy.graphql": "enum Stage {\n  PACKED\n  SENT\n}\n",
    "src/gen/types.ts": GENERATED,
    "src/ship.ts": (
        'import { Stage, Parcel } from "./gen/types";\n'  # 1
        "export function ship(parcel: Parcel): boolean {\n"  # 2
        "  return parcel.stage === Stage.Sent;\n"  # 3
        "}\n"  # 4
    ),
    # A hand-written enum of the same name and values, outside every output.
    "src/ui/stage.ts": "export enum Stage {\n  Packed = 'PACKED',\n  Sent = 'SENT',\n}\n",
    "src/ui/badge.ts": (
        'import { Stage } from "./stage";\n'
        "export function badge(stage: Stage): string {\n"
        "  return stage === Stage.Packed ? 'boxed' : 'moving';\n"
        "}\n"
    ),
}

WEB = {
    "package.json": json.dumps({"name": "parcel-web", "private": True}),
    "codegen.yml": (
        "schema: ../parcel-api/schema/parcels.graphql\n"  # 1
        "generates:\n"  # 2
        "  src/gen.ts:\n"  # 3   linked across the boundary; LOST is stale
        "    plugins: [typescript]\n"  # 4
        "  src/remote.ts:\n"  # 5
        "    schema: https://user:pw@schema.example.invalid/graphql?key=k\n"  # 6
        "    plugins: [typescript]\n"  # 7
        "  src/coded.ts:\n"  # 8
        "    schema: ./build-schema.ts\n"  # 9
        "    plugins: [typescript]\n"  # 10
        "  src/absent.ts:\n"  # 11
        "    schema: schema/nowhere.graphql\n"  # 12
        "    plugins: [typescript]\n"  # 13
        "  src/client/:\n"  # 14
        "    preset: client\n"  # 15
        "  src/never-written.ts:\n"  # 16
        "    plugins: [typescript]\n"  # 17
        "  src/operations.ts:\n"  # 18
        "    plugins: [typescript-operations]\n"  # 19
        "  src/prefixed.ts:\n"  # 20
        "    config:\n"  # 21
        "      typesPrefix: I\n"  # 22
        "    plugins: [typescript]\n"  # 23
        "  src/literal.ts:\n"  # 24
        "    config:\n"  # 25
        "      enumsAsTypes: true\n"  # 26
        "    plugins: [typescript]\n"  # 27
        "  src/clash.ts:\n"  # 28
        "    schema: schema/clash.graphql\n"  # 29
        "    plugins: [typescript]\n"  # 30
        "  ${OUT_DIR}/types.ts:\n"  # 31
        "    plugins: [typescript]\n"  # 32
    ),
    "build-schema.ts": "export const schema = 1;\n",
    "schema/clash.graphql": "type Stage {\n  id: ID!\n}\nenum shipping_lane {\n  FAST\n}\n",
    "src/gen.ts": (
        "export enum Stage {\n"  # 1
        "  Packed = 'PACKED',\n"  # 2
        "  Sent = 'SENT'\n"  # 3
        "}\n"  # 4
        "export type Parcel = {\n"  # 5
        "  stage: Stage;\n"  # 6
        "};\n"  # 7
        "export type ParcelFilter = { stage?: Stage };\n"  # 8
        "export type Query = { parcels: Parcel[] };\n"  # 9
        "export type Query = { other: number };\n"  # 10  generated twice: ambiguous
    ),
    "src/literal.ts": (
        "export type Stage = 'LOST' | 'PACKED' | 'SENT';\n"
        "export type Parcel = { stage: Stage };\n"
        "export type ParcelFilter = { stage?: Stage };\n"
        "export type Query = { parcels: Parcel[] };\n"
    ),
    "src/clash.ts": "export type Stage = { id: string };\n",
    "src/prefixed.ts": "export enum IStage { Packed = 'PACKED' }\n",
    "src/operations.ts": "export type ParcelsQuery = { parcels: number };\n",
    "src/track.ts": (
        'import { Stage } from "./gen";\nexport const LOST_STAGES = [Stage.Packed];\n'
    ),
}


def repos() -> dict[str, dict[str, str]]:
    return {"parcel-api": dict(API), "parcel-web": dict(WEB)}
