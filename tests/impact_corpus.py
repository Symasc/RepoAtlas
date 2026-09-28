"""Neutral change-impact fixtures, shared by the ``USES`` and impact tests.

Each corpus is a small repository written out file by file, with every relationship
it is meant to prove stated next to the source that proves it. Nothing here is taken
from a real repository or a benchmark task.

Fixture A (symbol propagation) exercises how a declaration's use of a name survives
the ways TypeScript lets a name travel: composition inside one file, a named export,
a renaming barrel, an alias import, a default, two wholesale re-exports that disagree,
a namespace import used with and without a member, a type-only import, an unrelated
importer of the same file, and every kind of shadowing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

SYMBOLS: dict[str, str] = {
    "package.json": json.dumps({"name": "symbols", "private": True}),
    # composition within one file, and a default that names a declaration
    "src/limits.ts": (
        "export const LIMIT = 10;\n"  # 1
        "const TABLE = { max: LIMIT };\n"  # 2  TABLE uses LIMIT
        "export function cap(n: number) {\n"  # 3
        "  return Math.min(n, TABLE.max);\n"  # 4  cap uses TABLE as a receiver
        "}\n"  # 5
        "export const unrelated = 1;\n"  # 6
        "export default function defaultRule() {\n"  # 7
        "  return LIMIT;\n"  # 8  defaultRule uses LIMIT
        "}\n"
    ),
    "src/helper.ts": "export const helperValue = 2;\n",
    "src/stars-a.ts": "export const shared = 1;\nexport const onlyA = 1;\n",
    "src/stars-b.ts": "export const shared = 2;\n",
    "src/expr.ts": "export default { anonymous: true };\n",
    "src/types.ts": "export interface Shape {\n  side: number;\n}\n",
    # a barrel: a renamed re-export, a default re-exported by name, a name imported
    # and exported again, and two wholesale re-exports that both define `shared`
    "src/index.ts": (
        'export { LIMIT as MAX_ITEMS } from "./limits";\n'
        'export { default as rule } from "./limits";\n'
        'import { helperValue } from "./helper";\n'
        "export { helperValue };\n"
        'export * from "./stars-a";\n'
        'export * from "./stars-b";\n'
    ),
    # through the barrel, under an alias
    "src/check.ts": (
        'import { MAX_ITEMS as ceiling, rule, helperValue, shared, onlyA } from "./index";\n'
        "export function check(n: number) {\n"  # 2
        "  return n < ceiling && rule() && helperValue + shared + onlyA;\n"  # 3
        "}\n"
    ),
    # a namespace import, with and without a member; a default that is an expression
    "src/spaces.ts": (
        'import * as L from "./limits";\n'
        'import anonymous from "./expr";\n'
        "export const withMember = L.LIMIT;\n"  # 3
        "export const whole = Object.keys(L);\n"  # 4
        "export const fromExpression = anonymous;\n"  # 5
    ),
    # a type-only import
    "src/area.ts": (
        'import type { Shape } from "./types";\n'
        "export function area(s: Shape): number {\n"  # 2
        "  return s.side * s.side;\n"
        "}\n"
    ),
    # an unrelated importer of the file that defines LIMIT
    "src/other.ts": (
        'import { unrelated } from "./limits";\nexport const other = unrelated + 1;\n'
    ),
    # a test: its callbacks are anonymous, so its uses are the file's
    "src/limits.test.ts": (
        'import { LIMIT } from "./limits";\n'
        'describe("limits", () => {\n'
        '  it("caps", () => {\n'
        "    expect(LIMIT).toBe(10);\n"
        "  });\n"
        "});\n"
    ),
    # every kind of shadowing, and one use that escapes it
    "src/shadow.ts": (
        'import { LIMIT } from "./limits";\n'
        "export function byParameter(LIMIT: number) {\n"  # 2
        "  return LIMIT;\n"
        "}\n"
        "export function byBlock() {\n"  # 5
        "  {\n"
        "    const LIMIT = 3;\n"
        "    void LIMIT;\n"
        "  }\n"
        "  return LIMIT;\n"  # 10  a use: the block's LIMIT ended with the block
        "}\n"
        "export function byVar(flag: boolean) {\n"  # 12
        "  if (flag) {\n"
        "    var LIMIT = 1;\n"  # hoisted: binds LIMIT throughout byVar
        "  }\n"
        "  return LIMIT;\n"
        "}\n"
        "export const byCallback = [1].map((LIMIT) => LIMIT);\n"  # 18
        "export function byCatch() {\n"  # 19
        "  try {\n"
        "    return 0;\n"
        "  } catch (LIMIT) {\n"
        "    return LIMIT;\n"
        "  }\n"
        "}\n"
    ),
}


def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def build(workspace: Workspace, tmp_path: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    root = tmp_path / "repos"
    for name, files in repos.items():
        make_repo(root, name, files)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace


def label(path: str | None, qualified: str | None, entity_type: str) -> str:
    """``path#qualified`` for a declaration, the bare path for a file."""
    return (path or "") if entity_type == "file" else f"{path}#{qualified}"


def use_edges(workspace: Workspace) -> list[dict[str, Any]]:
    """Every ``USES`` edge, with both ends labelled and its resolution decoded."""
    rows = workspace.database.execute(
        """
        SELECT s.type AS s_type, sf.rel_path AS s_path, s.qualified_name AS s_name,
               d.type AS d_type, df.rel_path AS d_path, d.qualified_name AS d_name,
               e.line_start AS line, e.resolution AS resolution, e.parser AS parser
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        LEFT JOIN files sf ON sf.id = s.file_id
        LEFT JOIN files df ON df.id = d.file_id
        WHERE e.type = 'USES'
        """
    ).fetchall()
    return [
        {
            "source": label(row["s_path"], row["s_name"], row["s_type"]),
            "target": label(row["d_path"], row["d_name"], row["d_type"]),
            "line": row["line"],
            "parser": row["parser"],
            **json.loads(row["resolution"] or "{}"),
        }
        for row in rows
    ]


def uses(workspace: Workspace) -> set[tuple[str, str, str]]:
    """(user, used, kind of use) for every ``USES`` edge."""
    return {(edge["source"], edge["target"], edge["use"]) for edge in use_edges(workspace)}


# -- fixture B (the large hub) and the confinement pair ----------------------------

#: The hub fixture's shape: files across packages, and functions per file each using HUB.
HUB_PACKAGES = 5
HUB_FILES = 50
HUB_USERS_PER_FILE = 100


def hub_repo() -> dict[str, str]:
    """One constant used by 5,000 functions in 50 files across 5 packages."""
    files = {
        "package.json": json.dumps({"name": "hub", "private": True}),
        "core/hub.ts": "export const HUB = 1;\n",
    }
    for k in range(HUB_PACKAGES):
        files[f"packages/p{k}/package.json"] = json.dumps({"name": f"@hub/p{k}"})
    for i in range(HUB_FILES):
        body = "".join(
            f"export function f{i}_{j}() {{\n  return HUB + {j};\n}}\n"
            for j in range(HUB_USERS_PER_FILE)
        )
        package = f"packages/p{i % HUB_PACKAGES}"
        files[f"{package}/src/u{i:02d}.ts"] = f'import {{ HUB }} from "../../../core/hub";\n{body}'
    return files


#: Two repositories sharing an environment variable and a package. ``alpha`` sorts
#: before ``zeta`` everywhere, so a limit applied before confinement would fall on
#: alpha's rows when the walk stands in zeta.
ALPHA = {
    "package.json": json.dumps({"name": "alpha", "version": "1.0.0"}),
    "src/config.ts": "export const alphaUrl = process.env.SHARED_URL;\n",
}
ZETA = {
    "package.json": json.dumps(
        {"name": "zeta", "private": True, "dependencies": {"alpha": "^1.0.0"}}
    ),
    "src/config.ts": "export const zetaUrl = process.env.SHARED_URL;\n",
}


# -- fixture F (seed resolution) ------------------------------------------------------

SEEDS = {
    "package.json": json.dumps({"name": "seeds", "private": True}),
    # one name, two declarations
    "src/format.ts": "export function formatDate(d: Date) {\n  return d.toISOString();\n}\n",
    "src/legacy/format.ts": "export function formatDate(d: Date) {\n  return String(d);\n}\n",
    "src/legacy/old.ts": "export const old = 1;\n",
    # the known line-1 collision: the file's module symbol and its first declaration
    # carry one key
    "src/limits.ts": "export function limits() {\n  return 1;\n}\n",
    # one name, two wholesale re-exports that each define it
    "src/stars-a.ts": "export const shared = 1;\n",
    "src/stars-b.ts": "export const shared = 2;\n",
    "src/stars.ts": 'export * from "./stars-a";\nexport * from "./stars-b";\n',
    # an environment variable and a constant spelled alike
    "src/db.ts": "export const DATABASE_URL = process.env.DATABASE_URL;\n",
    # a class and its members
    "src/store.ts": (
        "export class Store {\n  get() {\n    return 1;\n  }\n  set() {\n    return 2;\n  }\n}\n"
    ),
    # a configuration key with members
    ".github/workflows/ci.yml": (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo build\n"
    ),
}
ELSEWHERE = {
    "package.json": json.dumps({"name": "elsewhere", "private": True}),
    "src/only.ts": "export const onlyThere = 1;\n",
}


# -- fixture G (semantic direction: one minimal form per admitted family) --------------

DIRECTION = {
    "package.json": json.dumps({"name": "direction", "private": True}),
    # uses, and the file-level import a whole-file change follows
    "src/a.ts": "export const A = 1;\n",
    "src/b.ts": 'import { A } from "./a";\nexport function useA() {\n  return A;\n}\n',
    # references: a heritage clause
    "src/base.ts": "export class Base {\n  size() {\n    return 1;\n  }\n}\n",
    "src/sub.ts": 'import { Base } from "./base";\nexport class Sub extends Base {}\n',
    # calls
    "src/calls.ts": (
        "export function callee() {\n  return 1;\n}\n"
        "export function caller() {\n  return callee();\n}\n"
    ),
    # reads
    "src/env.ts": "export function readsEnv() {\n  return process.env.G_KEY;\n}\n",
    # member-of: a member uses A, so its container, and the container's users, change
    "src/holder.ts": (
        'import { A } from "./a";\n'
        "export class Holder {\n  run() {\n    return A;\n  }\n}\n"
        "export const held = new Holder();\n"
    ),
    # configures (and its setter): a Salt state's context sets the variable
    "salt/app.sls": (
        "app_config:\n"
        "  file.managed:\n"
        "    - name: /etc/app.conf\n"
        "    - context:\n"
        "        G_KEY: '1'\n"
    ),
    # a Dockerfile's declaration (and its setter)
    "Dockerfile": "FROM node:22 AS app\nARG G_ARG\n",
    # serves, served-by and requests
    "src/server.ts": (
        'import express from "express";\n'
        "const app = express();\n"
        "export function listItems(req: unknown, res: { json(v: unknown): void }) {\n"
        "  res.json([]);\n"
        "}\n"
        'app.get("/v1/items", listItems);\n'
    ),
    "src/client.ts": 'export function loadItems() {\n  return fetch("/v1/items");\n}\n',
}


# -- fixture D (a directory) and fixture E (repositories) ------------------------------

#: A directory whose module is imported from outside it: a directory seed changes
#: everything beneath it, and reaches the importer through what it declares.
TREE = {
    "package.json": json.dumps({"name": "tree", "private": True}),
    "src/gen/api.ts": 'export const endpoints = { list: "/items" };\n',
    "src/client.ts": (
        'import { endpoints } from "./gen/api";\n'
        "export function listPath() {\n  return endpoints.list;\n}\n"
    ),
}


#: Two repositories that read one environment variable and each declare a function
#: named alike. Neither fact joins them.
APA = {
    "package.json": json.dumps({"name": "apa", "private": True}),
    "Dockerfile": "FROM node:22 AS api\nENV DATABASE_URL=postgres://db\n",
    "src/db.ts": "export function connect() {\n  return process.env.DATABASE_URL;\n}\n",
    "src/format.ts": "export function formatDate(d: Date) {\n  return d.toISOString();\n}\n",
    "src/report.ts": (
        'import { formatDate } from "./format";\n'
        "export function report() {\n  return formatDate(new Date());\n}\n"
    ),
}
BET = {
    "package.json": json.dumps({"name": "bet", "private": True}),
    "src/db.ts": "export function migrate() {\n  return process.env.DATABASE_URL;\n}\n",
    "src/format.ts": "export function formatDate(d: Date) {\n  return String(d);\n}\n",
    "src/view.ts": (
        'import { formatDate } from "./format";\n'
        "export function view() {\n  return formatDate(new Date());\n}\n"
    ),
}


def verify_steps(workspace: Workspace, response: Any) -> int:
    """Check every step of an impact answer against the index; return how many were checked.

    An edge step must be an indexed edge of its type between the candidate and its
    predecessor, asserted by the file and lines the step names. A derived step must hold
    by keys and spans: a member's key extends what it is a member of, and a container's
    qualified name is its member's prefix, in the same file.
    """
    by_id = {c.id: c for c in response.candidates}
    seed = response.seed
    checked = 0
    for candidate in response.candidates:
        before = by_id.get(candidate.from_)
        near = before.entity if before is not None else seed.entity
        step = candidate.step
        if step.edge is not None:
            rows = workspace.database.execute(
                """
                SELECT f.rel_path, e.line_start, e.line_end, r.slug
                FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
                LEFT JOIN files f ON f.id = e.file_id LEFT JOIN repositories r ON r.id = f.repo_id
                WHERE e.type = ? AND ((s.key = ? AND d.key = ?) OR (s.key = ? AND d.key = ?))
                """,
                (str(step.edge), near, candidate.entity, candidate.entity, near),
            ).fetchall()
            located = {(f"{r[3]}/{r[0]}", (r[1], r[2])) for r in rows}
            assert (step.at, step.lines) in located, (candidate.entity, step, located)
        elif step.derived == "member":
            if (
                before is None
                and seed.granularity is not None
                and seed.granularity.value == "subtree"
            ):
                assert candidate.path is not None and seed.path is not None
                assert candidate.path.startswith(seed.path.rstrip("/") + "/")
            else:
                parent_path = before.path if before is not None else seed.path
                assert candidate.path == parent_path
                parent_lines = before.lines if before is not None else seed.lines
                if (
                    parent_lines is not None
                    and seed.granularity is not None
                    and (before is not None or seed.granularity.value != "file")
                ):
                    assert candidate.lines is not None
                    assert (
                        parent_lines[0]
                        <= candidate.lines[0]
                        <= candidate.lines[1]
                        <= parent_lines[1]
                    )
        elif step.derived == "member-of":
            assert before is not None and before.path == candidate.path
            member_key = before.entity.split(":")
            container = candidate.entity.split(":")
            assert member_key[-2].startswith(container[-2] + ".")
            assert candidate.lines and before.lines
            assert candidate.lines[0] <= before.lines[0] <= before.lines[1] <= candidate.lines[1]
        else:
            raise AssertionError(f"a step that is neither an edge nor derived: {step}")
        checked += 1
    return checked


# -- fixture B (a hub across units) and E-package (package-level dependents) ---------

HUB_UNITS = ("a", "b", "c", "d")
HUB_CONSUMERS = 15  # per unit
HUB_TESTS = 12


def hub_units_repo() -> dict[str, str]:
    """``request`` is used by 60 consumers across four units, a dozen tests, and a barrel."""
    files = {
        "package.json": json.dumps({"name": "units", "private": True}),
        "packages/core/package.json": json.dumps({"name": "@u/core"}),
        "packages/core/src/config.ts": "export const BASE = 1;\n",
        "packages/core/src/request.ts": (
            'import { BASE } from "./config";\nexport function request() {\n  return BASE;\n}\n'
        ),
        # a barrel: imports the name only to pass it on
        "packages/core/src/index.ts": 'export { request } from "./request";\n',
    }
    for unit in HUB_UNITS:
        files[f"apps/{unit}/package.json"] = json.dumps({"name": f"@u/{unit}"})
        for i in range(HUB_CONSUMERS):
            files[f"apps/{unit}/src/c{i:02d}.ts"] = (
                'import { request } from "../../../packages/core/src/request";\n'
                f"export function use_{unit}{i:02d}() {{\n  return request();\n}}\n"
            )
    for i in range(HUB_TESTS):
        files[f"apps/a/src/__tests__/r{i:02d}.test.ts"] = (
            'import { request } from "../../../../packages/core/src/request";\n'
            f'test("r{i:02d}", () => {{\n  request();\n}});\n'
        )
    return files


#: One monorepo: a package its app declares and imports, and a name two packages provide.
MONO = {
    "package.json": json.dumps(
        {"name": "mono", "private": True, "workspaces": ["packages/*", "apps/*"]}
    ),
    "packages/core/package.json": json.dumps({"name": "@m/core", "main": "src/index.ts"}),
    "packages/core/src/index.ts": "export const CORE = 1;\n",
    "packages/dup-one/package.json": json.dumps({"name": "@m/dup", "main": "src/index.ts"}),
    "packages/dup-one/src/index.ts": "export const ONE = 1;\n",
    "packages/dup-two/package.json": json.dumps({"name": "@m/dup", "main": "src/index.ts"}),
    "packages/dup-two/src/index.ts": "export const TWO = 2;\n",
    "apps/web/package.json": json.dumps(
        {"name": "@m/web", "dependencies": {"@m/core": "*", "@m/dup": "*"}}
    ),
    "apps/web/src/use.ts": 'import { CORE } from "@m/core";\nexport const shown = CORE + 1;\n',
}
#: A package in one repository, declared and imported by another.
LIB = {
    "package.json": json.dumps({"name": "@acme/lib", "version": "1.0.0", "main": "src/index.ts"}),
    "src/index.ts": "export function formatDate(d: Date) {\n  return d.toISOString();\n}\n",
}
APP = {
    "package.json": json.dumps(
        {"name": "app", "private": True, "dependencies": {"@acme/lib": "^1.0.0"}}
    ),
    "src/view.ts": (
        'import { formatDate } from "@acme/lib";\n'
        "export function view() {\n  return formatDate(new Date());\n}\n"
    ),
}
#: One name, two providers: a third repository's declaration joins neither.
TWIN_ONE = {
    "package.json": json.dumps({"name": "@acme/twin", "version": "1.0.0"}),
    "src/index.ts": "export const one = 1;\n",
}
TWIN_TWO = {
    "package.json": json.dumps({"name": "@acme/twin", "version": "2.0.0"}),
    "src/index.ts": "export const two = 2;\n",
}
TWIN_USER = {
    "package.json": json.dumps({"name": "twin-user", "dependencies": {"@acme/twin": "^1.0.0"}}),
    "src/index.ts": 'import { one } from "@acme/twin";\nexport const used = one;\n',
}
