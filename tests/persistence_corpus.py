"""Neutral fixtures for persistence: Prisma projects, SQL, queries, mappings and typed values.

Three invented repositories. ``depot-api`` keeps a PostgreSQL Prisma schema (an enum value and
a column renamed for the database with ``@map``), the migrations Prisma Migrate wrote from it,
and code that reaches the database every way this milestone reads -- Knex, ``pg``,
``$queryRaw`` -- and some ways it refuses: SQL built at run time, a client whose database
nothing states, a receiver a parameter shadows, a column no model has. It maps a generated
enum to the Prisma one with a typed ``Record``. ``depot-mysql`` keeps a MySQL schema.
``depot-ops`` is Python: a model whose field is typed by an enum, a comparison of that
field with a wire value, a typed mapping missing a member, a query against a database it
cannot name, and a dashboard. Nothing here comes from a real repository.
"""

from __future__ import annotations

import json

SCHEMA = (
    "datasource db {\n"  # 1
    '  provider = "postgresql"\n'  # 2
    '  url      = env("DEPOT_DATABASE_URL")\n'  # 3
    "}\n"  # 4
    "\n"  # 5
    "generator client {\n"  # 6
    '  provider = "prisma-client-js"\n'  # 7
    "}\n"  # 8
    "\n"  # 9
    "/* Where a parcel is. */\n"  # 10
    "enum ParcelState {\n"  # 11
    "  packed\n"  # 12
    "  sent\n"  # 13
    '  lost @map("missing")\n'  # 14
    "\n"  # 15
    '  @@map("parcel_state")\n'  # 16
    "}\n"  # 17
    "\n"  # 18
    "model Parcel {\n"  # 19
    "  id    String      @id\n"  # 20
    '  state ParcelState @default(packed) @map("state_code")\n'  # 21
    "  label String?     @db.Text\n"  # 22
    "\n"  # 23
    '  @@index([state], map: "parcels_state_idx")\n'  # 24
    '  @@map("parcels")\n'  # 25
    "}\n"  # 26
)

INIT = (
    "-- CreateEnum\n"  # 1
    "CREATE TYPE \"parcel_state\" AS ENUM ('packed', 'sent');\n"  # 2
    "\n"  # 3
    "-- CreateTable\n"  # 4
    'CREATE TABLE "parcels" (\n'  # 5
    '    "id" TEXT NOT NULL,\n'  # 6
    '    "state_code" "parcel_state" NOT NULL DEFAULT \'packed\',\n'  # 7
    '    "label" TEXT,\n'  # 8
    '    CONSTRAINT "parcels_pkey" PRIMARY KEY ("id")\n'  # 9
    ");\n"  # 10
)

LOST = "-- AlterEnum\nALTER TYPE \"parcel_state\" ADD VALUE 'missing';\n"

API = {
    "package.json": json.dumps({"name": "depot-api", "private": True}),
    "prisma/schema.prisma": SCHEMA,
    "prisma/migrations/20260101000000_init/migration.sql": INIT,
    "prisma/migrations/20260201000000_lost/migration.sql": LOST,
    "prisma/migrations/migration_lock.toml": 'provider = "postgresql"\n',
    "src/gen/types.ts": (
        "export enum Stage {\n"  # 1
        "  Packed = 'PACKED',\n"  # 2
        "  Sent = 'SENT',\n"  # 3
        "  Lost = 'LOST',\n"  # 4
        "}\n"  # 5
    ),
    "src/db.ts": (
        "import { PrismaClient } from '@prisma/client';\n"  # 1
        "import knex from 'knex';\n"  # 2
        "import { Pool } from 'pg';\n"  # 3
        "\n"  # 4
        "export const prisma = new PrismaClient();\n"  # 5
        "export const db = knex({ client: 'pg', connection: process.env.DEPOT_DATABASE_URL });\n"  # 6  # noqa: E501
        "export const pool = new Pool({ connectionString: process.env.DEPOT_DATABASE_URL });\n"  # 7
        "export const legacy = knex({ client: 'pg', connection: process.env.LEGACY_URL });\n"  # 8
    ),
    "src/queries.ts": (
        "import { db, pool, prisma, legacy } from './db';\n"  # 1
        "\n"  # 2
        "export function packedParcels() {\n"  # 3
        "  return db('parcels').where('state_code', 'packed');\n"  # 4
        "}\n"  # 5
        "\n"  # 6
        "export function movingParcels() {\n"  # 7
        "  return db('parcels as p')\n"  # 8
        "    .join('labels as l', 'l.id', 'p.label')\n"  # 9
        "    .whereIn('p.state_code', ['sent', 'missing']);\n"  # 10
        "}\n"  # 11
        "\n"  # 12
        "export async function sentCount() {\n"  # 13
        "  return pool.query(\"SELECT count(*) FROM parcels WHERE state_code = 'sent'\");\n"  # 14
        "}\n"  # 15
        "\n"  # 16
        "export async function packedSince(since: string) {\n"  # 17
        "  return prisma.$queryRaw`SELECT id FROM parcels WHERE state_code = 'packed' AND id > ${since}`;\n"  # noqa: E501 - 18
        "}\n"  # 19
        "\n"  # 20
        "export async function inState(state: string) {\n"  # 21
        "  return pool.query(`SELECT id FROM parcels WHERE state_code = '${state}'`);\n"  # 22
        "}\n"  # 23
        "\n"  # 24
        "export function legacySent() {\n"  # 25
        "  return legacy('parcels').where('state_code', 'sent');\n"  # 26
        "}\n"  # 27
        "\n"  # 28
        "export function shadowed(db: (t: string) => { where: (c: string, v: string) => unknown }) {\n"  # noqa: E501 - 29
        "  return db('parcels').where('state_code', 'packed');\n"  # 30
        "}\n"  # 31
        "\n"  # 32
        "export function byColour() {\n"  # 33
        "  return db('parcels').where('colour', 'red');\n"  # 34
        "}\n"  # 35
    ),
    "src/mapping.ts": (
        "import { ParcelState } from '@prisma/client';\n"  # 1
        "import { Stage } from './gen/types';\n"  # 2
        "\n"  # 3
        "export const toState: Record<Stage, ParcelState> = {\n"  # 4
        "  [Stage.Packed]: ParcelState.packed,\n"  # 5
        "  [Stage.Sent]: ParcelState.sent,\n"  # 6
        "  [Stage.Lost]: ParcelState.lost,\n"  # 7
        "};\n"  # 8
        "\n"  # 9
        "export const badges = {\n"  # 10
        "  [Stage.Packed]: 'boxed',\n"  # 11
        "} satisfies Record<Stage, string>;\n"  # 12
        "\n"  # 13
        "export function isLost(state: ParcelState): boolean {\n"  # 14
        "  return state === ParcelState.lost;\n"  # 15
        "}\n"  # 16
    ),
}

MYSQL = {
    "package.json": json.dumps({"name": "depot-mysql", "private": True}),
    "prisma/schema.prisma": (
        'datasource db {\n  provider = "mysql"\n  url      = env("MYSQL_URL")\n}\n'
        "enum Tier {\n  GOLD\n  SILVER\n}\n"
        'model Member {\n  id   String @id\n  tier Tier\n  @@map("members")\n}\n'
    ),
    "prisma/migrations/20260101000000_init/migration.sql": (
        "CREATE TABLE `members` (\n"
        "    `id` VARCHAR(191) NOT NULL,\n"
        "    `tier` ENUM('GOLD', 'SILVER') NOT NULL,\n"
        "    PRIMARY KEY (`id`)\n"
        ");\n"
    ),
    "src/db.ts": (
        "import knex from 'knex';\n"
        "export const db = knex({ client: 'mysql2', connection: process.env.MYSQL_URL });\n"
        "export function gold() {\n"
        "  return db('members').where('tier', 'GOLD');\n"
        "}\n"
    ),
    # The same specifier, in a repository none of whose schemas generates it: bound to nothing.
    "src/state.ts": (
        "import { ParcelState } from '@prisma/client';\n"
        "export function isPacked(state: ParcelState): boolean {\n"
        "  return state === ParcelState.packed;\n"
        "}\n"
    ),
}

OPS = {
    "pyproject.toml": '[project]\nname = "depot-ops"\nversion = "0.1.0"\n',
    "ops/__init__.py": "",
    "ops/stages.py": (
        "from enum import Enum\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "class Stage(str, Enum):\n"  # 4
        '    PACKED = "PACKED"\n'  # 5
        '    LOST = "LOST"\n'  # 6
        "\n"  # 7
        "\n"  # 8
        "class Courier(str, Enum):\n"  # 9
        '    LOST = "LOST"\n'  # 10
    ),
    "ops/models.py": (
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "\n"
        "from ops.stages import Stage\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Parcel:\n"
        "    id: str\n"
        "    state: Optional[Stage]\n"
    ),
    "ops/checks.py": (
        "from ops.models import Parcel\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "def lost(parcels: list[Parcel]) -> list[Parcel]:\n"  # 4
        '    return [p for p in parcels if p.state == "LOST"]\n'  # 5
        "\n"  # 6
        "\n"  # 7
        "def first_lost(parcels: list[Parcel]) -> Parcel | None:\n"  # 8
        "    for parcel in parcels:\n"  # 9
        '        if parcel.state in ("LOST",):\n'  # 10
        "            return parcel\n"  # 11
        "    return None\n"  # 12
        "\n"  # 13
        "\n"  # 14
        "def untyped(parcel) -> bool:\n"  # 15
        '    return parcel.state == "LOST"\n'  # 16
    ),
    "ops/labels.py": (
        'from ops.stages import Stage\n\nLABELS: dict[Stage, str] = {Stage.PACKED: "Boxed"}\n'
    ),
    "ops/report.py": (
        "import os\n"  # 1
        "\n"  # 2
        "import psycopg\n"  # 3
        "\n"  # 4
        "\n"  # 5
        "def sent() -> int:\n"  # 6
        '    with psycopg.connect(os.environ["DEPOT_DATABASE_URL"]) as conn:\n'  # 7
        "        row = conn.execute(\"SELECT count(*) FROM parcels WHERE state_code = 'sent'\").fetchone()\n"  # noqa: E501 - 8
        "    return row[0]\n"  # 9
    ),
    "dashboards/parcels.json": json.dumps(
        {
            "title": "Parcels",
            "panels": [
                {
                    "title": "Lost",
                    "targets": [
                        {"rawSql": "SELECT count(*) FROM parcels WHERE state_code = 'missing'"}
                    ],
                }
            ],
        },
        indent=2,
    ),
}


def repos() -> dict[str, dict[str, str]]:
    return {"depot-api": dict(API), "depot-mysql": dict(MYSQL), "depot-ops": dict(OPS)}
