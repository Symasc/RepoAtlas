# ruff: noqa: E501 - fixture source lines are spelled whole, with their line numbers
"""Neutral model-field fixtures (DF3b): which fields exist, and which declarations use them.

Every relationship a test expects is stated next to the source that proves it. Nothing here
is taken from a real repository or a benchmark task. Three repositories:

* ``api`` (Python): request and response models of each initializer kind, the handlers that
  read them through typed parameters and construct them, and every shape a field-level
  relationship must not be inferred from -- a plain class, an attrs-like class, an alias, a
  subclass, a union, a rebound parameter, an attribute-qualified annotation;
* ``report`` (Python): its own model with same-named fields, read only by its own code;
* ``web`` (TypeScript): interfaces, an object type alias and a class, read through typed
  parameters, destructured, and given typed object literals; a same-named field elsewhere;
  and a function named like an interface field, which a call still resolves to.
"""

from __future__ import annotations

API: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "api"\nversion = "1.0"\n',
    "api/__init__.py": "",
    "api/models.py": (
        "from dataclasses import dataclass, field\n"  # 1
        "from typing import ClassVar\n"  # 2
        "\n"  # 3
        "from pydantic import BaseModel, Field\n"  # 4
        "\n"  # 5
        "\n"  # 6
        "class Query(BaseModel):\n"  # 7
        "    limit: int | None = None\n"  # 8   bound by keyword (pydantic)
        "    sort: str | None = None\n"  # 9   bound by keyword (pydantic)
        '    hidden: str = Field(default="", alias="h")\n'  # 10  aliased: not keyword-bound
        '    _secret: str = ""\n'  # 11  private: not keyword-bound
        "    VERSION: ClassVar[int] = 1\n"  # 12  no field
        "\n"  # 13
        "\n"  # 14
        "class Out(BaseModel):\n"  # 15
        "    observation_id: str\n"  # 16
        "    measurement: float\n"  # 17
        "\n"  # 18
        "\n"  # 19
        "@dataclass\n"  # 20
        "class Row:\n"  # 21
        "    measurement: float\n"  # 22  bound by keyword (dataclass)
        '    note: str = field(default="", init=False)\n'  # 23  init=False: not keyword-bound
        "\n"  # 24
        "\n"  # 25
        "class Plain:\n"  # 26
        "    measurement: float\n"  # 27  a field, but no initializer binds it by keyword
        "\n"  # 28
        "    def __init__(self, measurement: float) -> None:\n"  # 29
        "        self.measurement = measurement * 2\n"  # 30
        "\n"  # 31
        "\n"  # 32
        "class Child(Out):\n"  # 33
        "    extra: str\n"  # 34  a subclass's own field: not keyword-bound (zero hops)
        "\n"  # 35
        "\n"  # 36
        "Alias = Out\n"  # 37
    ),
    "api/handlers.py": (
        "import api.models\n"  # 1
        "from api.models import Alias, Child, Out, Plain, Query, Row\n"  # 2
        "\n"  # 3
        "\n"  # 4
        "def read(query: Query) -> Out:\n"  # 5
        "    limit = query.limit or 10\n"  # 6   USES Query.limit (parameter)
        "    first = query.sort\n"  # 7   USES Query.sort (parameter)
        "    return Out(observation_id=first, measurement=float(limit))\n"  # 8  both (construction)
        "\n"  # 9
        "\n"  # 10
        "def build_rows() -> list:\n"  # 11
        "    rows = [Row(measurement=1.0), Plain(measurement=2.0)]\n"  # 12 Row.measurement only
        '    Query(limit=1, h="x", _secret="y")\n'  # 13 Query.limit only
        "    Child(extra=1)\n"  # 14 nothing: Child's initializer is not its own
        "    return rows\n"  # 15
        "\n"  # 16
        "\n"  # 17
        "def through_alias(out: Alias, child: Child, either: Out | Row) -> float:\n"  # 18
        "    return out.measurement + child.measurement + either.measurement\n"  # 19 nothing
        "\n"  # 20
        "\n"  # 21
        "def rebound(query: Query) -> int | None:\n"  # 22
        "    query = Query()\n"  # 23
        "    return query.limit\n"  # 24 nothing: the parameter is bound again
        "\n"  # 25
        "\n"  # 26
        "def qualified(query: api.models.Query, default: Query | None = None) -> int | None:\n"  # 27
        "    return query.limit\n"  # 28 nothing: an attribute-qualified annotation
        "\n"  # 29
        "\n"  # 30
        "def deep(query: Query) -> int:\n"  # 31
        "    return query.sort.count\n"  # 32 Query.sort only, never what sort holds
        "\n"  # 33
        "\n"  # 34
        "class Local:\n"  # 35
        "    name: str\n"  # 36
        "\n"  # 37
        "\n"  # 38
        "def local(item: Local) -> str:\n"  # 39
        "    return item.name\n"  # 40 USES Local.name (a model this file declares)
    ),
    # entities that record no kind at all (SQL columns): never taken for fields
    "api/schema.sql": "CREATE TABLE rows (\n    id INTEGER PRIMARY KEY,\n    measurement REAL\n);\n",
    "api/nested.py": (
        "class Outer:\n"  # 1
        "    class Inner:\n"  # 2
        "        size: int\n"  # 3  Outer.Inner.size: inside Outer, never Outer's
        "\n"  # 4
        "    name: str\n"  # 5
        "\n"  # 6
        "\n"  # 7
        "def outer(o: Outer) -> object:\n"  # 8
        "    return o.size, o.name\n"  # 9  USES Outer.name only
    ),
}

REPORT: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "report"\nversion = "1.0"\n',
    "report/__init__.py": "",
    "report/models.py": (
        "from pydantic import BaseModel\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "class ReportRow(BaseModel):\n"  # 4
        "    observation_id: str\n"  # 5
        "    measurement: float\n"  # 6
    ),
    "report/render.py": (
        "from report.models import ReportRow\n"  # 1
        "\n"  # 2
        "\n"  # 3
        "def render(row: ReportRow) -> str:\n"  # 4
        "    return str(row.measurement)\n"  # 5 USES ReportRow.measurement, never Out's
    ),
}

WEB: dict[str, str] = {
    "package.json": '{"name": "web", "private": true}\n',
    "src/types.ts": (
        "export interface Item {\n"  # 1
        "  id: string;\n"  # 2
        "  measurement: number;\n"  # 3
        "  render(): string;\n"  # 4   a method signature: no field
        "}\n"  # 5
        "export type Opts = { limit: number; sort?: string };\n"  # 6
        "export class Box {\n"  # 7
        "  size: number = 1;\n"  # 8
        "  static count: number;\n"  # 9   static: no field
        "  open = () => true;\n"  # 10  a function: no field
        "}\n"  # 11
    ),
    "src/use.ts": (
        'import { Item, Opts, Box } from "./types";\n'  # 1
        "export function show(item: Item | null): number {\n"  # 2
        "  return item?.measurement ?? 0;\n"  # 3   USES Item.measurement (parameter)
        "}\n"  # 4
        "export function make(): Item {\n"  # 5
        '  return { id: "a", measurement: 1, render: () => "" };\n'  # 6  id, measurement
        "}\n"  # 7
        "export function sized({ limit }: Opts, box: Box): number {\n"  # 8  Opts.limit
        "  const { size } = box;\n"  # 9   Box.size
        "  return limit + size;\n"  # 10
        "}\n"  # 11
        "export const defaults: Opts = { limit: 5 };\n"  # 12  defaults USES Opts.limit
        "export function either(value: Item | Box): unknown {\n"  # 13
        "  return value.measurement;\n"  # 14  nothing: two types
        "}\n"  # 15
        "export function generic<T>(value: T): unknown {\n"  # 16
        "  return value;\n"  # 17
        "}\n"  # 18
    ),
    "src/other.ts": (
        "interface Other {\n"  # 1
        "  measurement: number;\n"  # 2
        "}\n"  # 3
        "export function local(o: Other): number {\n"  # 4
        "  return o.measurement;\n"  # 5   USES Other.measurement, never Item's or Second's
        "}\n"  # 6
        "interface Second {\n"  # 7
        "  measurement: number;\n"  # 8   the same name, in the same file: never reached
        "}\n"  # 9
    ),
    "src/ids.ts": (
        "export function id(): string {\n"  # 1
        '  return "x";\n'  # 2
        "}\n"  # 3
        "export interface WithId {\n"  # 4
        "  id: string;\n"  # 5   a field named like the function above
        "}\n"  # 6
    ),
    "src/call.ts": (
        'import { id } from "./ids";\n'  # 1
        "export function run(): string {\n"  # 2
        "  return id();\n"  # 3   CALLS id, the function: a field is no callee
        "}\n"  # 4
    ),
}


def repos() -> dict[str, dict[str, str]]:
    return {"api": API, "report": REPORT, "web": WEB}
