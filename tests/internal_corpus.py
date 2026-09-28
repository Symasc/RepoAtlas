"""Neutral fixtures for internal packages that live in another Git repository (XW1-XW15).

A Git repository is not a package manager's boundary. These repositories declare
packages that other indexed repositories provide by path (``file:``, ``link:``,
``portal:``) and by workspace (``workspace:``), and every package states the shape it
exists to prove:

* ``mono`` is a pnpm workspace whose members are partly its own (``packages/local``,
  the ``apps/*`` consumers) and partly repositories nested inside it:
  ``mono/packages/internal`` provides ``@fixture/internal`` (a renaming barrel and an
  exported subpath), ``mono/packages/solo`` provides ``@fixture/solo``,
  ``mono/apps/nested-app`` consumes both (XW1, XW12, XW15) and ``mono/apps/inner-ws`` is
  a workspace of its own with no such member (XW1c);
* ``provider`` provides ``@fixture/internal`` too, so no name alone can pick either:
  whatever still links is pinned by a path or a workspace (XW4-XW6, XW9, XW10);
* ``wrong`` provides another package under the same export names (XW7), and ``plain``
  a package no one else provides (XW8, XW13, and the refusals that must not fall back
  to it);
* ``twins`` is a workspace whose two nested members share one name (XW9);
* ``paths`` is one package per way of declaring a package another repository provides.

Nothing here is taken from a real repository or a benchmark task.
"""

from __future__ import annotations

import json
from collections import Counter

from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses_link import use_resolver
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.services.workspace import Workspace
from tests.xrepo_corpus import manifest

INTERNAL = "@fixture/internal"
PLAIN = "@fixture/plain"

#: A consumer of ``@fixture/internal``: imports ``Foo`` under an alias and uses it as a type.
USE_FOO = (
    'import { Foo as LocalFoo } from "@fixture/internal";\n'
    "export function useFoo(x: LocalFoo) {\n"  # 2
    "  return x;\n"
    "}\n"
)
#: A consumer of ``@fixture/plain``.
USE_PLAIN = (
    'import { plainTool } from "@fixture/plain";\n'
    "export function usePlain() {\n  return plainTool();\n}\n"
)


def internal_package(tag: str) -> dict[str, str]:
    """``@fixture/internal``: ``Foo`` is ``InternalFoo`` renamed by the barrel; ``./sub`` too."""
    return {
        "package.json": manifest(
            name=INTERNAL,
            version="1.0.0",
            exports={".": "./src/index.ts", "./sub": "./src/sub.ts"},
        ),
        "src/index.ts": 'export { InternalFoo as Foo } from "./foo";\n',
        "src/foo.ts": f'export class InternalFoo {{\n  tag = "{tag}";\n}}\n',
        "src/sub.ts": f'export function subTool() {{\n  return "{tag}";\n}}\n',
    }


def consumer(name: str, dependencies: dict[str, str], **files: str) -> dict[str, str]:
    return {"package.json": manifest(name=name, dependencies=dependencies), **files}


def under(directory: str, files: dict[str, str]) -> dict[str, str]:
    return {f"{directory}/{path}": content for path, content in files.items()}


MONO: dict[str, str] = {
    "pnpm-workspace.yaml": 'packages:\n  - "apps/*"\n  - "packages/*"\n  - "!packages/excluded"\n',
    "package.json": manifest(name="mono-root", private=True),
    # XW14: a member that is mono's own
    "packages/local/package.json": manifest(
        name="@fixture/local", version="1.0.0", exports={".": "./src/index.ts"}
    ),
    "packages/local/src/index.ts": "export function localTool() {\n  return 1;\n}\n",
    # XW1, XW14
    **under(
        "apps/app",
        consumer(
            "mono-app",
            {INTERNAL: "workspace:*", "@fixture/local": "workspace:*"},
            **{
                "src/use.ts": USE_FOO,
                "src/local-use.ts": (
                    'import { localTool } from "@fixture/local";\n'
                    "export function useLocal() {\n  return localTool();\n}\n"
                ),
            },
        ),
    ),
    # XW2: a workspace alias spelling the package's own name
    **under("apps/named", consumer("mono-named", {INTERNAL: "workspace:@fixture/internal@*"})),
    "apps/named/src/use.ts": USE_FOO,
    # XW3: a workspace alias of another package
    **under("apps/other", consumer("mono-other", {INTERNAL: "workspace:@fixture/other@*"})),
    "apps/other/src/use.ts": USE_FOO,
}

INTERNAL_NESTED = internal_package("nested")
SOLO: dict[str, str] = {
    "package.json": manifest(name="@fixture/solo", version="1.0.0", main="src/index.ts"),
    "src/index.ts": "export function soloTool() {\n  return 1;\n}\n",
}
#: XW1 (a nested consumer), XW12 (the chain on to a test), XW15 (a whole-module use).
NESTED_APP: dict[str, str] = {
    "package.json": manifest(
        name="nested-app", dependencies={INTERNAL: "workspace:^", "@fixture/solo": "workspace:*"}
    ),
    "src/use.ts": USE_FOO,
    "src/downstream.ts": (
        'import { useFoo } from "./use";\n'
        "export function downstream() {\n"  # 2
        "  return useFoo(undefined as never);\n"
        "}\n"
    ),
    "test/downstream.test.ts": (
        'import { downstream } from "../src/downstream";\n'
        "export function checksDownstream() {\n"  # 2
        "  return downstream();\n"
        "}\n"
    ),
    "src/solo-use.ts": (
        'import * as solo from "@fixture/solo";\n'
        "export function pick(key: string) {\n"
        "  return solo[key as never];\n"
        "}\n"
    ),
}
#: XW1c: the innermost workspace decides, and it has no such member.
INNER_WS: dict[str, str] = {
    "pnpm-workspace.yaml": 'packages:\n  - "libs/*"\n',
    "package.json": manifest(
        name="inner-root", private=True, dependencies={INTERNAL: "workspace:*"}
    ),
    "src/use.ts": USE_FOO,
}

PROVIDER = internal_package("provider")
WRONG: dict[str, str] = {
    "package.json": manifest(
        name="@fixture/wrong", version="1.0.0", exports={".": "./src/index.ts"}
    ),
    "src/index.ts": "export class Foo {}\nexport function plainTool() {\n  return 0;\n}\n",
}
PLAIN_REPO: dict[str, str] = {
    "package.json": manifest(name=PLAIN, version="1.0.0", main="src/index.ts"),
    "src/index.ts": "export function plainTool() {\n  return 1;\n}\n",
}

TWINS: dict[str, str] = {
    "pnpm-workspace.yaml": 'packages:\n  - "apps/*"\n  - "packages/*"\n',
    "package.json": manifest(name="twins-root", private=True),
    "apps/app/package.json": manifest(
        name="twins-app", dependencies={"@fixture/twin": "workspace:*"}
    ),
    "apps/app/src/use.ts": (
        'import { twinTool } from "@fixture/twin";\n'
        "export function useTwin() {\n  return twinTool();\n}\n"
    ),
}


def twin(tag: str) -> dict[str, str]:
    return {
        "package.json": manifest(name="@fixture/twin", version="1.0.0", main="src/index.ts"),
        "src/index.ts": f'export function twinTool() {{\n  return "{tag}";\n}}\n',
    }


#: Each directory of ``paths`` declares a package its own way; ``use.ts`` uses it.
DECLARED: dict[str, tuple[str, str, str]] = {
    # XW4-XW6: a path into another repository pins its provider, whoever else shares the name
    "file": (INTERNAL, "file:../../provider", USE_FOO),
    "link": (INTERNAL, "link:../../provider", USE_FOO),
    "portal": (INTERNAL, "portal:../../provider", USE_FOO),
    # XW7: the path leads to a package of another name
    "wrong": (INTERNAL, "file:../../wrong", USE_FOO),
    # XW8: nothing indexed there, a directory holding no manifest, a tarball -- each for
    # a name exactly one repository provides, so a fall-back to the name would link
    "missing": (PLAIN, "file:../../nowhere", USE_PLAIN),
    "nopkg": (PLAIN, "file:../../plain/src", USE_PLAIN),
    "tarball": (PLAIN, "file:../../plain/plain-1.0.0.tgz", USE_PLAIN),
    # XW1b: a workspace range that no indexed workspace holds
    "ws-unindexed": (PLAIN, "workspace:*", USE_PLAIN),
    # XW3b: a remote source
    "git": (PLAIN, "git+https://example.invalid/plain.git", USE_PLAIN),
    # XW3: an alias of another package
    "npm-other": (INTERNAL, "npm:@fixture/other@1.0.0", USE_FOO),
    # XW9: a version of a name two repositories provide
    "semver": (INTERNAL, "^1.0.0", USE_FOO),
    # XW13: a version, and an alias spelling the package's own name
    "semver-plain": (PLAIN, "^1.0.0", USE_PLAIN),
    "npm-same": (PLAIN, "npm:@fixture/plain@1.0.0", USE_PLAIN),
}

PATHS: dict[str, str] = {"package.json": manifest(name="paths-root", private=True)}
for _name, (_package, _constraint, _use) in DECLARED.items():
    PATHS[f"{_name}/package.json"] = manifest(
        name=f"paths-{_name}", dependencies={_package: _constraint}
    )
    PATHS[f"{_name}/use.ts"] = _use
# XW10: a subpath of a package the path pins
PATHS["link/sub-use.ts"] = (
    'import { subTool } from "@fixture/internal/sub";\n'
    "export function useSub() {\n  return subTool();\n}\n"
)

REPOS: dict[str, dict[str, str]] = {
    "mono": MONO,
    "mono/packages/internal": INTERNAL_NESTED,
    "mono/packages/solo": SOLO,
    "mono/apps/nested-app": NESTED_APP,
    "mono/apps/inner-ws": INNER_WS,
    "provider": PROVIDER,
    "wrong": WRONG,
    "plain": PLAIN_REPO,
    "twins": TWINS,
    "twins/packages/a": twin("a"),
    "twins/packages/b": twin("b"),
    "paths": PATHS,
}


def refusals(workspace: Workspace, repository: str, path: str) -> dict[str, str]:
    """Why each use one file makes resolved to nothing, by the local name it uses.

    Resolved with the resolver the global pass builds, one use at a time, reading the
    reason each adds to its (measurement-only) count.
    """
    knowledge = workspace.knowledge
    index = ResolutionIndex.build(knowledge.list_file_rows(), knowledge.list_symbols())
    resolver = use_resolver(knowledge, index, workspace.dependencies.list_for_repos(None))
    [repo_id] = [i for i, slug in index.repo_slugs.items() if slug == repository]
    origin = index.file(repo_id, path)
    assert origin is not None
    found: dict[str, str] = {}
    for claim in knowledge.list_use_claims([origin.file_id]):
        if claim.edge_type is EdgeType.IMPORTS or claim.target_key == USES_EXPORT_TABLE:
            continue
        meta = json.loads(claim.meta_json or "{}")
        before = Counter(resolver.unresolved)
        if resolver.resolve(origin, meta) is None:
            [why] = (resolver.unresolved - before).elements()
            found[meta["local"]] = why
    return found
