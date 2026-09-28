"""Neutral cross-repository fixtures (XR1-XR9): packages one repository provides, used in another.

Each repository is written out file by file, with the relationship it proves stated
beside the source that proves it. Nothing here is taken from a real repository or a
benchmark task. Manifests are indented, so a manifest line names one entry.

* ``contracts`` provides ``@fixture/contracts`` (XR1, XR2, XR4, XR7, XR8): a barrel over
  an enum, a renamed re-export of an import, and a default.
* ``app`` declares it and uses it: exactly (XR1), under aliases (XR2), through a
  namespace (XR4); a use that continues inside ``app`` down to a test (XR8); a relative
  import that climbs into a sibling repository on disk (N1). It also uses a package
  subpath (XR3) and a package whose entry is build output (N7).
* ``left`` and ``right`` export one name and share nothing (XR5).
* ``dup-*``, ``dup-mono`` and ``self-provider``: a name more than one manifest provides,
  or one the consumer provides itself (XR6).
* ``coarse-user`` declares ``@fixture/contracts`` and uses it only in ways no declaration
  can be named from (XR7); ``mono-kit`` and ``kit-user`` put the exact consumer behind a
  second package (XR7b).
* ``refusals`` and its providers: every other reason a package joins nothing.
* :func:`hub_repos` is XR9: one exported function used across three repositories.
"""

from __future__ import annotations

import json
from typing import Any

from repoatlas.services.workspace import Workspace


def manifest(**fields: Any) -> str:
    return json.dumps(fields, indent=2) + "\n"


CONTRACTS = {
    # name on line 2
    "package.json": manifest(name="@fixture/contracts", version="1.0.0", main="src/index.ts"),
    "src/roles.ts": (
        "export enum CourseRole {\n"  # 1
        '  Owner = "owner",\n'
        '  Editor = "editor",\n'
        "}\n"
    ),
    "src/internal.ts": 'export const InternalRole = { Owner: "owner" } as const;\n',
    # an import exported again under another name
    "src/public.ts": (
        'import { InternalRole } from "./internal";\nexport { InternalRole as Role };\n'
    ),
    "src/index.ts": (
        'export { CourseRole } from "./roles";\n'
        'export { Role } from "./public";\n'
        "export default function defaultPolicy() {\n"  # 3
        "  return 1;\n"
        "}\n"
    ),
}

APP = {
    # declarations on lines 6-10
    "package.json": manifest(
        name="xr-app",
        private=True,
        dependencies={
            "@fixture/contracts": "^1.0.0",
            "@fixture/auth-kit": "^1.0.0",
            "@fixture/plain": "^1.0.0",
            "@fixture/built": "^1.0.0",
            "@fixture/built-two": "^1.0.0",
        },
    ),
    # XR1: exact named import, used by a declaration
    "src/policy.ts": (
        'import { CourseRole } from "@fixture/contracts";\n'  # 1
        "export function canEdit(role: CourseRole) {\n"  # 2
        "  return role === CourseRole.Editor;\n"  # 3
        "}\n"
    ),
    # XR8: the change continues inside the consumer
    "src/guard.ts": (
        'import { canEdit } from "./policy";\n'
        "export function guard(role: string) {\n"  # 2
        "  return canEdit(role as never);\n"
        "}\n"
    ),
    "src/__tests__/guard.test.ts": (
        'import { guard } from "../guard";\ntest("guard", () => {\n  guard("editor");\n});\n'
    ),
    # XR2: an aliased import of a renamed re-export, and a default under another name
    "src/aliases.ts": (
        'import { Role as LocalRole } from "@fixture/contracts";\n'  # 1
        'import applyPolicy from "@fixture/contracts";\n'  # 2
        "export function isOwner(role: string) {\n"  # 3
        "  return role === LocalRole.Owner;\n"
        "}\n"
        "export const policyValue = applyPolicy();\n"  # 6
    ),
    # XR4: a namespace, with a static member and with a computed one
    "src/spaces.ts": (
        'import * as Contracts from "@fixture/contracts";\n'
        "export function staticMember() {\n"  # 2
        "  return Contracts.CourseRole.Owner;\n"
        "}\n"
        "export function computedMember(key: string) {\n"  # 5
        "  return Contracts[key as never];\n"
        "}\n"
    ),
    # XR3: a subpath the provider exports, one it does not, and a package with no exports
    "src/auth-use.ts": (
        'import { Role } from "@fixture/auth-kit/auth";\nexport const authRole = Role;\n'
    ),
    "src/internal-use.ts": (
        'import { Session } from "@fixture/auth-kit/internal";\nexport const hidden = Session;\n'
    ),
    "src/plain-use.ts": (
        'import { tool } from "@fixture/plain/src/extra/tool";\nexport const plainTool = tool;\n'
    ),
    # N1: a relative path that climbs out of this repository into a sibling one on disk
    "src/relative.ts": (
        'import { CourseRole } from "../../contracts/src/roles";\n'
        "export const climbed = CourseRole;\n"
    ),
    # N7: a package whose entry is build output
    "src/built-use.ts": (
        'import { encodeParams } from "@fixture/built";\n'
        "export function request() {\n"  # 2
        '  return encodeParams("a");\n'
        "}\n"
    ),
    "src/built-two-use.ts": (
        'import { encodeOther } from "@fixture/built-two";\nexport const other = encodeOther;\n'
    ),
}

AUTH_KIT = {
    "package.json": manifest(
        name="@fixture/auth-kit",
        version="1.0.0",
        exports={".": "./src/index.ts", "./auth": "./src/auth/index.ts"},
    ),
    "src/index.ts": 'export const Role = "root";\n',
    "src/auth/index.ts": 'export const Role = "auth";\nexport const Session = 1;\n',
    "src/internal/index.ts": "export const Session = 2;\n",
}

PLAIN = {
    "package.json": manifest(name="@fixture/plain", version="1.0.0", main="src/index.ts"),
    "src/index.ts": "export const root = 1;\n",
    "src/extra/tool.ts": "export const tool = 1;\n",
}

#: Entries in build output that is not in the repository; the tsconfig names one root.
BUILT = {
    "package.json": manifest(
        name="@fixture/built",
        version="1.0.0",
        main="./dist/index.cjs",
        types="./dist/index.d.ts",
        exports={
            ".": {
                "types": "./dist/index.d.ts",
                "import": "./dist/index.mjs",
                "require": "./dist/index.cjs",
            }
        },
    ),
    "tsconfig.json": json.dumps({"compilerOptions": {"strict": True}, "include": ["src"]}),
    "src/index.ts": 'export { encodeParams } from "./params";\n',
    "src/params.ts": "export function encodeParams(value: string) {\n  return value;\n}\n",
}

#: The same shape with two source roots: which one the output mirrors is unknown.
BUILT_TWO = {
    "package.json": manifest(name="@fixture/built-two", version="1.0.0", main="./dist/index.js"),
    "tsconfig.json": json.dumps({"include": ["src", "scripts"]}),
    "src/index.ts": "export const encodeOther = 1;\n",
    "scripts/build.ts": "export const build = 1;\n",
}

# -- XR5: one name, two repositories, no relationship --------------------------------

LEFT = {
    "package.json": manifest(name="@left/roles", version="1.0.0", main="src/index.ts"),
    "src/index.ts": 'export const CourseRole = "left";\n',
    "src/local.ts": 'import { CourseRole } from "./index";\nexport const leftUse = CourseRole;\n',
}
RIGHT = {
    "package.json": manifest(name="@right/roles", version="1.0.0", main="src/index.ts"),
    "src/index.ts": 'export const CourseRole = "right";\n',
    "src/local.ts": 'import { CourseRole } from "./index";\nexport const rightUse = CourseRole;\n',
    # a package nothing provides
    "src/stray.ts": (
        'import { CourseRole } from "@nowhere/roles";\nexport const stray = CourseRole;\n'
    ),
    # a package another repository provides, but this one never declares
    "src/undeclared.ts": (
        'import { CourseRole } from "@left/roles";\nexport const undeclared = CourseRole;\n'
    ),
}

# -- XR6: more than one provider, or the consumer's own -------------------------------

DUP_ONE = {
    "package.json": manifest(name="@fixture/dup", version="1.0.0", main="src/index.ts"),
    "src/index.ts": "export const one = 1;\n",
}
DUP_TWO = {
    "package.json": manifest(name="@fixture/dup", version="2.0.0", main="src/index.ts"),
    "src/index.ts": "export const one = 2;\n",
}
DUP_MONO = {
    "package.json": manifest(name="dup-mono", private=True),
    "packages/a/package.json": manifest(name="@fixture/dup-mono", main="src/index.ts"),
    "packages/a/src/index.ts": "export const mono = 1;\n",
    "packages/b/package.json": manifest(name="@fixture/dup-mono", main="src/index.ts"),
    "packages/b/src/index.ts": "export const mono = 2;\n",
}
DUP_USER = {
    "package.json": manifest(
        name="dup-user",
        dependencies={"@fixture/dup": "^1.0.0", "@fixture/dup-mono": "^1.0.0"},
    ),
    "src/use.ts": (
        'import { one } from "@fixture/dup";\n'
        'import { mono } from "@fixture/dup-mono";\n'
        "export const both = one + mono;\n"
    ),
}
#: Another repository provides ``@fixture/shadowed``; so does the one importing it.
SHADOW_SOURCE = {
    "package.json": manifest(name="@fixture/shadowed", version="1.0.0", main="src/index.ts"),
    "src/index.ts": 'export const shade = "source";\n',
}
SELF_PROVIDER = {
    "package.json": manifest(name="self-provider", dependencies={"@fixture/shadowed": "^1.0.0"}),
    "packages/shadow/package.json": manifest(name="@fixture/shadowed", main="src/index.ts"),
    "packages/shadow/src/index.ts": 'export const shade = "self";\n',
    "src/use.ts": 'import { shade } from "@fixture/shadowed";\nexport const selfUse = shade;\n',
}

# -- XR7: a declared package whose consumers cannot be named ---------------------------

COARSE_USER = {
    "package.json": manifest(name="coarse-user", dependencies={"@fixture/contracts": "^1.0.0"}),
    "src/computed.ts": (
        'import * as C from "@fixture/contracts";\n'
        "export function pick(key: string) {\n"  # 2
        "  return C[key as never];\n"
        "}\n"
    ),
    "src/required.ts": (
        'const contracts = require("@fixture/contracts");\n'
        "export const viaRequire = contracts.CourseRole;\n"
    ),
}
#: XR7b: ``X`` lives in package P; P's sibling Q uses it; the consumer uses only Q.
MONO_KIT = {
    "package.json": manifest(name="mono-kit", private=True, workspaces=["packages/*"]),
    "packages/p/package.json": manifest(name="@kit/p", version="1.0.0", main="src/index.ts"),
    # Y: a declaration nobody uses, in a package its sibling declares
    "packages/p/src/index.ts": "export const X = 1;\nexport const Y = 2;\n",
    "packages/q/package.json": manifest(
        name="@kit/q", version="1.0.0", main="src/index.ts", dependencies={"@kit/p": "*"}
    ),
    "packages/q/src/index.ts": (
        'import { X } from "@kit/p";\nexport function F() {\n  return X;\n}\n'
    ),
}
KIT_USER = {
    "package.json": manifest(
        name="kit-user", dependencies={"@kit/p": "^1.0.0", "@kit/q": "^1.0.0"}
    ),
    "src/g.ts": 'import { F } from "@kit/q";\nexport function g() {\n  return F();\n}\n',
}

# -- every other refusal ---------------------------------------------------------------

#: Each directory is its own package, declaring ``@fixture/contracts`` its own way.
REFUSALS = {
    "package.json": manifest(name="refusals", private=True),
    "ok/package.json": manifest(name="ok", dependencies={"@fixture/contracts": "^1.0.0"}),
    "ok/use.ts": (
        'import { CourseRole } from "@fixture/contracts";\nexport const ok = CourseRole;\n'
    ),
    "aliased/package.json": manifest(
        name="aliased", dependencies={"@fixture/contracts": "npm:@other/contracts@^1.0.0"}
    ),
    "aliased/use.ts": (
        'import { CourseRole } from "@fixture/contracts";\nexport const aliased = CourseRole;\n'
    ),
    "workspace/package.json": manifest(
        name="workspace", dependencies={"@fixture/contracts": "workspace:*"}
    ),
    "workspace/use.ts": (
        'import { CourseRole } from "@fixture/contracts";\nexport const inWorkspace = CourseRole;\n'
    ),
    "shadowed/package.json": manifest(
        name="shadowed", dependencies={"@fixture/contracts": "^1.0.0"}
    ),
    "shadowed/tsconfig.json": json.dumps(
        {"compilerOptions": {"baseUrl": ".", "paths": {"@fixture/*": ["./vendor/*"]}}}
    ),
    "shadowed/use.ts": (
        'import { CourseRole } from "@fixture/contracts";\nexport const shadowed = CourseRole;\n'
    ),
    "undeclared/use.ts": (
        'import { CourseRole } from "@fixture/contracts";\nexport const undeclared = CourseRole;\n'
    ),
    "unprovided/package.json": manifest(name="unprovided", dependencies={"@nowhere/pkg": "^1.0.0"}),
    "unprovided/use.ts": 'import { thing } from "@nowhere/pkg";\nexport const nothing = thing;\n',
    "indexed-dist/package.json": manifest(
        name="indexed-dist", dependencies={"@fixture/indexed-dist": "^1.0.0"}
    ),
    "indexed-dist/use.ts": (
        'import { kept } from "@fixture/indexed-dist";\nexport const keptUse = kept;\n'
    ),
    "split/package.json": manifest(name="split", dependencies={"@fixture/split": "^1.0.0"}),
    "split/use.ts": 'import { side } from "@fixture/split";\nexport const sideUse = side;\n',
}
#: Output that is indexed is not build output: no mapping. (``dist`` and ``build`` are
#: never indexed at all, so an indexed output directory has another name.)
INDEXED_DIST = {
    "package.json": manifest(name="@fixture/indexed-dist", version="1.0.0", main="./lib/index.js"),
    "tsconfig.json": json.dumps({"include": ["src"]}),
    "lib/keep.json": "{}\n",
    "src/index.ts": "export const kept = 1;\n",
}
#: Two conditions that map to two different source files.
SPLIT = {
    "package.json": manifest(
        name="@fixture/split",
        version="1.0.0",
        exports={".": {"browser": "./dist/browser.mjs", "node": "./dist/node.mjs"}},
    ),
    "tsconfig.json": json.dumps({"include": ["src"]}),
    "src/browser.ts": "export const side = 1;\n",
    "src/node.ts": "export const side = 2;\n",
}

#: A workspace package whose entry is build output, imported inside its own repository:
#: the same mapping resolves it, as a member of the workspace and never as a crossing.
WS_BUILT = {
    "package.json": manifest(name="ws-built", private=True, workspaces=["packages/*", "apps/*"]),
    "packages/lib/package.json": manifest(name="@ws/lib", main="./dist/index.js"),
    "packages/lib/tsconfig.json": json.dumps({"include": ["src"]}),
    "packages/lib/src/index.ts": "export const lib = 1;\n",
    "apps/web/package.json": manifest(name="@ws/web", dependencies={"@ws/lib": "*"}),
    "apps/web/src/use.ts": 'import { lib } from "@ws/lib";\nexport const webUse = lib;\n',
}

#: Every linking repository, as one index.
LINKING: dict[str, dict[str, str]] = {
    "contracts": CONTRACTS,
    "app": APP,
    "auth-kit": AUTH_KIT,
    "plain": PLAIN,
    "built": BUILT,
    "built-two": BUILT_TWO,
    "left": LEFT,
    "right": RIGHT,
    "dup-one": DUP_ONE,
    "dup-two": DUP_TWO,
    "dup-mono": DUP_MONO,
    "dup-user": DUP_USER,
    "shadow-source": SHADOW_SOURCE,
    "self-provider": SELF_PROVIDER,
    "coarse-user": COARSE_USER,
    "mono-kit": MONO_KIT,
    "kit-user": KIT_USER,
    "refusals": REFUSALS,
    "indexed-dist": INDEXED_DIST,
    "split": SPLIT,
    "ws-built": WS_BUILT,
}

# -- XR9: one exported function used across three repositories ------------------------

HUB_UNITS_A = 7
HUB_FILES_PER_UNIT = 2


def hub_repos(users_per_file: int) -> dict[str, dict[str, str]]:
    """``formatRole`` used by ``users_per_file`` declarations in each consumer file.

    ``hub-a`` spreads its files over seven units, so a choice made by units alone would
    fill every representative from it. The file layout is the same at every size, so
    the work a bounded read does cannot depend on how many users there are.
    """

    def users(prefix: str) -> str:
        lines = ['import { formatRole } from "@fixture/labels";']
        for i in range(users_per_file):
            lines += [f"export function {prefix}{i:03d}() {{", '  return formatRole("owner");', "}"]
        return "\n".join(lines) + "\n"

    labels = {
        "package.json": manifest(name="@fixture/labels", version="1.0.0", main="src/index.ts"),
        "src/labels.ts": (
            'export const ROLE_LABELS = { owner: "Owner" };\n'
            "export function formatRole(role: string) {\n"  # 2
            '  return ROLE_LABELS[role as "owner"];\n'
            "}\n"
        ),
        "src/index.ts": 'export { formatRole, ROLE_LABELS } from "./labels";\n',
    }
    declares = {"@fixture/labels": "^1.0.0"}
    hub_a: dict[str, str] = {"package.json": manifest(name="hub-a", dependencies=declares)}
    for unit in range(HUB_UNITS_A):
        hub_a[f"packages/u{unit}/package.json"] = manifest(name=f"@hub-a/u{unit}")
        for f in range(HUB_FILES_PER_UNIT):
            hub_a[f"packages/u{unit}/src/f{f}.ts"] = users(f"a{unit}f{f}_")
    hub_b = {"package.json": manifest(name="hub-b", dependencies=declares)}
    hub_c = {"package.json": manifest(name="hub-c", dependencies=declares)}
    for f in range(HUB_FILES_PER_UNIT):
        hub_b[f"src/f{f}.ts"] = users(f"b{f}_")
        hub_c[f"src/f{f}.ts"] = users(f"c{f}_")
    return {"labels": labels, "hub-a": hub_a, "hub-b": hub_b, "hub-c": hub_c}


# -- reading the index -----------------------------------------------------------------


def cross_uses(workspace: Workspace) -> list[dict[str, Any]]:
    """Every ``USES`` edge joining two repositories: both ends labelled, resolution decoded."""
    rows = workspace.database.execute(
        """
        SELECT sr.slug AS s_repo, sf.rel_path AS s_path, s.qualified_name AS s_name,
               s.type AS s_type, dr.slug AS d_repo, df.rel_path AS d_path,
               d.qualified_name AS d_name, d.type AS d_type, e.line_start AS line,
               ef.rel_path AS at, e.resolution AS resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN repositories sr ON sr.id = s.repo_id
        JOIN repositories dr ON dr.id = d.repo_id
        LEFT JOIN files sf ON sf.id = s.file_id
        LEFT JOIN files df ON df.id = d.file_id
        LEFT JOIN files ef ON ef.id = e.file_id
        WHERE e.type = 'USES' AND s.repo_id != d.repo_id
        ORDER BY sr.slug, sf.rel_path, s.qualified_name, dr.slug, df.rel_path, d.qualified_name
        """
    ).fetchall()
    return [
        {
            "source": _label(row["s_repo"], row["s_path"], row["s_name"], row["s_type"]),
            "target": _label(row["d_repo"], row["d_path"], row["d_name"], row["d_type"]),
            "line": row["line"],
            "at": row["at"],
            **json.loads(row["resolution"] or "{}"),
        }
        for row in rows
    ]


def cross_repository_edges(workspace: Workspace, repository: str) -> list[tuple[str, str, str]]:
    """Every edge of any type from ``repository`` into another repository."""
    rows = workspace.database.execute(
        """
        SELECT e.type, s.key, d.key
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN repositories sr ON sr.id = s.repo_id
        WHERE sr.slug = ? AND d.repo_id IS NOT NULL AND s.repo_id != d.repo_id
        ORDER BY e.type, s.key, d.key
        """,
        (repository,),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _label(repo: str, path: str | None, name: str | None, entity_type: str) -> str:
    return f"{repo}:{path}" if entity_type == "file" else f"{repo}:{path}#{name}"
