"""Neutral fixtures for workspace packages resolved inside one repository (W1-W11).

``ws`` is one pnpm workspace. Its app imports sibling packages by name, and every
package states the shape it exists to prove:

* ``@fixture/shared``: source entries in ``exports``, a renaming barrel, an exported
  subpath, and a file at the package root that ``exports`` does not list (W1, W3, W4, W8);
  one app per way of declaring it, each using its ``Top``: the declarations that name the
  member or a version of it (W2), and those that name something else (W2b);
* ``@fixture/built``: every entry in build output that is not indexed, mapped back through
  the one source root its tsconfig ``include`` names -- the shape of a package whose
  ``tsc`` has not run (W5);
* ``@fixture/built-amb`` and ``@fixture/built-roots``: build output two source files, or
  two source roots, would answer (W5b);
* two members named ``@fixture/twin`` (W6); a member named ``@fixture/shadow`` that the
  app's tsconfig ``paths`` maps elsewhere (W7); a package without ``exports`` imported
  through a subpath that climbs out of it (W8b);
* a chain from the shared package's declaration to the app's test (W9), and decoys that
  share its names without being its provider (W10): an excluded package and one outside
  the workspace globs claiming ``@fixture/shared``, a same-named function in another app,
  and repository ``elsewhere`` providing ``@fixture/shared`` too.

``kit`` is another repository, providing ``@fixture/kit`` from build output that the app
declares by version: the package link that crosses repositories on its own evidence,
which a change to workspace resolution must leave exactly as it was (W11).

Nothing here is taken from a real repository or a benchmark task.
"""

from __future__ import annotations

import json
from typing import Any

from repoatlas.services.workspace import Workspace
from tests.xrepo_corpus import manifest

#: Every entry of the package in unindexed build output, as ``tsc`` would emit it.
BUILD_OUTPUT_EXPORTS = {
    ".": {"import": "./dist/index.js", "require": "./dist/index.js", "types": "./dist/index.d.ts"},
    "./*": {"import": "./dist/*.js", "require": "./dist/*.js", "types": "./dist/*.d.ts"},
}


def built_package(name: str) -> str:
    return manifest(
        name=name,
        version="1.0.0",
        main="dist/index.js",
        types="dist/index.d.ts",
        exports=BUILD_OUTPUT_EXPORTS,
    )


def tsconfig(include: list[str], **options: Any) -> str:
    return json.dumps({"compilerOptions": {"outDir": "./dist", **options}, "include": include})


WS: dict[str, str] = {
    "pnpm-workspace.yaml": 'packages:\n  - "apps/*"\n  - "packages/*"\n  - "!packages/excluded"\n',
    "package.json": manifest(name="ws-root", private=True),
    # W1, W3, W4, W8
    "packages/shared/package.json": manifest(
        name="@fixture/shared",
        version="1.0.0",
        exports={".": "./src/index.ts", "./auth": "./src/auth/index.ts"},
    ),
    "packages/shared/src/index.ts": (
        'export { Internal as Public } from "./internal";\nexport const Top = 1;\n'
    ),
    "packages/shared/src/internal.ts": "export function Internal() {\n  return 1;\n}\n",
    "packages/shared/src/auth/index.ts": 'export const Role = "role";\n',
    # a file where a subpath not in `exports` would land if it fell back to the package
    "packages/shared/hidden.ts": "export const Hidden = 1;\n",
    # W5: the entry and every subpath in build output, one `include` root
    "packages/built/package.json": built_package("@fixture/built"),
    "packages/built/tsconfig.json": tsconfig(["src/**/*"], baseUrl="./"),
    "packages/built/src/index.ts": 'export * from "./helpers/errors";\n',
    "packages/built/src/helpers/errors.ts": (
        "export function normalize(error: unknown) {\n  return error;\n}\n"
    ),
    "packages/built/src/interfaces/claims.ts": "export interface Claims {\n  id: string;\n}\n",
    # W5b: two source files answer the entry; two source roots
    "packages/built-amb/package.json": built_package("@fixture/built-amb"),
    "packages/built-amb/tsconfig.json": tsconfig(["src"]),
    "packages/built-amb/src/index.ts": "export const Amb = 1;\n",
    "packages/built-amb/src/index/index.ts": "export const Amb = 2;\n",
    "packages/built-roots/package.json": built_package("@fixture/built-roots"),
    "packages/built-roots/tsconfig.json": tsconfig(["src", "lib"]),
    "packages/built-roots/src/index.ts": "export const Rooted = 1;\n",
    "packages/built-roots/lib/index.ts": "export const Rooted = 2;\n",
    # W6: two members share a name
    "packages/twin-a/package.json": manifest(name="@fixture/twin", main="index.ts"),
    "packages/twin-a/index.ts": 'export const Twin = "a";\n',
    "packages/twin-b/package.json": manifest(name="@fixture/twin", main="index.ts"),
    "packages/twin-b/index.ts": 'export const Twin = "b";\n',
    # W7: a member the app's own tsconfig maps elsewhere
    "packages/shadow/package.json": manifest(name="@fixture/shadow", main="index.ts"),
    "packages/shadow/index.ts": 'export const Shade = "member";\n',
    # W8b: no `exports`, so a subpath is a path inside the package
    "packages/plain/package.json": manifest(name="@fixture/plain", main="index.ts"),
    "packages/plain/index.ts": "export const Plain = 1;\n",
    # W10: excluded from the workspace, and outside its globs
    "packages/excluded/package.json": manifest(name="@fixture/shared", main="index.ts"),
    "packages/excluded/index.ts": "export function Internal() {\n  return 3;\n}\n",
    "tools/shared/package.json": manifest(name="@fixture/shared", main="index.ts"),
    "tools/shared/index.ts": "export function Internal() {\n  return 4;\n}\n",
    # provided in the repository, but by no member of its workspace
    "tools/lonely/package.json": manifest(name="@fixture/lonely", version="1.0.0", main="index.ts"),
    "tools/lonely/index.ts": "export const Lonely = 1;\n",
    "apps/other/package.json": manifest(name="other"),
    "apps/other/src/internal.ts": "export function Internal() {\n  return 5;\n}\n",
    # the app
    "apps/app/package.json": manifest(
        name="app",
        dependencies={
            "@fixture/shared": "workspace:*",
            "@fixture/built": "workspace:^",
            "@fixture/built-amb": "workspace:*",
            "@fixture/built-roots": "workspace:*",
            "@fixture/twin": "workspace:*",
            "@fixture/shadow": "workspace:*",
            "@fixture/plain": "workspace:*",
            "@fixture/lonely": "^1.0.0",
            "@fixture/kit": "^1.0.0",
        },
    ),
    "apps/app/tsconfig.json": json.dumps(
        {"compilerOptions": {"paths": {"@fixture/shadow": ["./src/local-shadow.ts"]}}}
    ),
    "apps/app/src/consumer.ts": (
        'import { Public as Local, Top } from "@fixture/shared";\n'
        'import { Role } from "@fixture/shared/auth";\n'
        "export function useIt() {\n"  # 3
        "  return Local() + Top;\n"
        "}\n"
        "export function useRole() {\n"  # 6
        "  return Role;\n"
        "}\n"
    ),
    "apps/app/src/downstream.ts": (
        'import { useIt } from "./consumer";\nexport function caller() {\n  return useIt();\n}\n'
    ),
    "apps/app/test/consumer.test.ts": (
        'import { caller } from "../src/downstream";\n'
        "export function checksCaller() {\n  return caller();\n}\n"
    ),
    "apps/app/src/built-use.ts": (
        'import { normalize } from "@fixture/built";\n'
        'import type { Claims } from "@fixture/built/interfaces/claims";\n'
        "export function handle(error: unknown): Claims {\n"
        "  return normalize(error) as Claims;\n"
        "}\n"
    ),
    "apps/app/src/refused.ts": (
        'import { Amb } from "@fixture/built-amb";\n'
        'import { Rooted } from "@fixture/built-roots";\n'
        'import { Twin } from "@fixture/twin";\n'
        'import { Hidden } from "@fixture/shared/hidden";\n'
        'import { Secret } from "@fixture/plain/../../apps/app/src/secret";\n'
        'import { Lonely } from "@fixture/lonely";\n'
        "export function useAmb() {\n  return Amb;\n}\n"
        "export function useRooted() {\n  return Rooted;\n}\n"
        "export function useTwin() {\n  return Twin;\n}\n"
        "export function useHidden() {\n  return Hidden;\n}\n"
        "export function useSecret() {\n  return Secret;\n}\n"
        "export function useLonely() {\n  return Lonely;\n}\n"
    ),
    "apps/app/src/secret.ts": "export const Secret = 1;\n",
    "apps/app/src/local-shadow.ts": 'export const Shade = "local";\n',
    "apps/app/src/shadow-use.ts": (
        'import { Shade } from "@fixture/shadow";\n'
        "export function useShade() {\n  return Shade;\n}\n"
    ),
    # W11: a package another repository provides, declared by version
    "apps/app/src/kit-use.ts": (
        'import { kitTool } from "@fixture/kit";\n'
        "export function useKit() {\n  return kitTool();\n}\n"
    ),
}

SHARED = "@fixture/shared"

#: W2: consumers whose declaration names the member itself, or only a version of it.
ACCEPTED: dict[str, dict[str, dict[str, str]]] = {
    "star": {"dependencies": {SHARED: "workspace:*"}},
    "caret": {"dependencies": {SHARED: "workspace:^"}},
    "tilde": {"dependencies": {SHARED: "workspace:~"}},
    "exact": {"dependencies": {SHARED: "workspace:1.0.0"}},
    "workspace-alias": {"dependencies": {SHARED: f"workspace:{SHARED}@*"}},
    "npm-same": {"dependencies": {SHARED: f"npm:{SHARED}@1.0.0"}},
    "workspace-path": {"dependencies": {SHARED: "workspace:../../packages/shared"}},
    "link": {"dependencies": {SHARED: "link:../../packages/shared"}},
    "file": {"dependencies": {SHARED: "file:../../packages/shared/"}},
    "portal": {"dependencies": {SHARED: "portal:../../packages/shared"}},
    "semver": {"dependencies": {SHARED: "^1.0.0"}},
    "catalog": {"dependencies": {SHARED: "catalog:"}},
    "undeclared": {},
    "peer-and-dev": {"peerDependencies": {SHARED: "*"}, "devDependencies": {SHARED: "workspace:*"}},
}

#: W2b: consumers whose declaration names something else, and why the member is refused.
REFUSED: dict[str, tuple[dict[str, dict[str, str]], str]] = {
    "npm-other": ({"dependencies": {SHARED: "npm:@fixture/other@1.0.0"}}, "dependency-aliased"),
    "workspace-other": (
        {"dependencies": {SHARED: "workspace:@fixture/other@*"}},
        "dependency-aliased",
    ),
    "link-other": ({"dependencies": {SHARED: "link:../../packages/built"}}, "dependency-elsewhere"),
    "file-outside": (
        {"dependencies": {SHARED: "file:../../../outside/shared"}},
        "dependency-elsewhere",
    ),
    "tarball": ({"dependencies": {SHARED: "file:../../vendor/shared.tgz"}}, "dependency-elsewhere"),
    "git": (
        {"dependencies": {SHARED: "git+https://example.invalid/shared.git"}},
        "dependency-elsewhere",
    ),
    "peer-and-npm": (
        {"peerDependencies": {SHARED: "*"}, "devDependencies": {SHARED: "npm:@fixture/other@1"}},
        "dependency-aliased",
    ),
    "unreadable": ({"dependencies": {SHARED: "workspace:some/form"}}, "dependency-unreadable"),
}

USE_TOP = 'import { Top } from "@fixture/shared";\nexport function useTop() {\n  return Top;\n}\n'


def declaring_consumers() -> dict[str, str]:
    """One app per declaration, each using the shared package's ``Top`` in ``src/use.ts``.

    ``nested`` declares the member by workspace, and ``nested/sub`` -- no member, but in
    the workspace -- aliases it away: the nearest manifest decides, never a farther one.
    """
    files: dict[str, str] = {}
    declarations = {**ACCEPTED, **{name: fields for name, (fields, _) in REFUSED.items()}}
    for name, fields in declarations.items():
        files[f"apps/{name}/package.json"] = manifest(name=f"decl-{name}", **fields)
        files[f"apps/{name}/src/use.ts"] = USE_TOP
    files["apps/nested/package.json"] = manifest(
        name="decl-nested", dependencies={SHARED: "workspace:*"}
    )
    files["apps/nested/src/use.ts"] = USE_TOP
    files["apps/nested/sub/package.json"] = manifest(
        name="decl-nested-sub", dependencies={SHARED: "npm:@fixture/other@1.0.0"}
    )
    files["apps/nested/sub/src/use.ts"] = USE_TOP
    return files


WS.update(declaring_consumers())

#: W10: another repository providing the same name as the workspace member.
ELSEWHERE: dict[str, str] = {
    "package.json": manifest(name="@fixture/shared", version="1.0.0", main="src/index.ts"),
    "src/index.ts": "export function Internal() {\n  return 6;\n}\nexport const Public = 7;\n",
}

#: W11: a package in another repository whose entry is build output.
KIT: dict[str, str] = {
    "package.json": manifest(
        name="@fixture/kit",
        version="1.0.0",
        main="./dist/index.js",
        exports={".": {"types": "./dist/index.d.ts", "import": "./dist/index.mjs"}},
    ),
    "tsconfig.json": json.dumps({"compilerOptions": {"strict": True}, "include": ["src"]}),
    "src/index.ts": 'export { kitTool } from "./tool";\n',
    "src/tool.ts": "export function kitTool() {\n  return 1;\n}\n",
}

REPOS: dict[str, dict[str, str]] = {"ws": WS, "elsewhere": ELSEWHERE, "kit": KIT}


def repo_uses(workspace: Workspace) -> list[dict[str, Any]]:
    """Every ``USES`` edge, both ends labelled ``repo:path#name``, its resolution decoded."""
    rows = workspace.database.execute(
        """
        SELECT sr.slug AS s_repo, sf.rel_path AS s_path, s.qualified_name AS s_name,
               s.type AS s_type, dr.slug AS d_repo, df.rel_path AS d_path,
               d.qualified_name AS d_name, d.type AS d_type, e.resolution AS resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN repositories sr ON sr.id = s.repo_id
        JOIN repositories dr ON dr.id = d.repo_id
        LEFT JOIN files sf ON sf.id = s.file_id
        LEFT JOIN files df ON df.id = d.file_id
        WHERE e.type = 'USES'
        """
    ).fetchall()
    return [
        {
            "source": _label(row["s_repo"], row["s_path"], row["s_name"], row["s_type"]),
            "target": _label(row["d_repo"], row["d_path"], row["d_name"], row["d_type"]),
            **json.loads(row["resolution"] or "{}"),
        }
        for row in rows
    ]


def _label(repo: str, path: str | None, name: str | None, entity_type: str) -> str:
    return f"{repo}:{path}" if entity_type == "file" else f"{repo}:{path}#{name}"
