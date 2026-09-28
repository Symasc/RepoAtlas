"""Neutral fixtures for incremental ``USES`` resolution: an edit, then a sync, then a fresh index.

Each case edits one tree, syncs it incrementally, and compares the result with a pristine
database indexing the same tree. Nothing here is taken from a real repository or a benchmark
task; every file states the relationship it exists to prove.

* ``chain`` (I0): fourteen files each re-exporting the next with ``export *``, the last
  declaring ``Deep``. ``shallow.ts`` imports it seven hops from the end, ``deep.ts``
  thirteen -- one past :data:`~repoatlas.graph.modules.MAX_REEXPORT_HOPS` -- so ``deep``'s
  use resolves to nothing however the pass reached it. ``deep.ts`` sorts first, so a fresh
  index resolves it before ``shallow.ts``, and re-indexing it alone reverses that order.
* ``mono`` (I1-I5, I8, I9): a workspace whose package ``@mono/kit`` is a barrel -- two
  ``export *``, a renamed re-export, a default re-exported by name and an ``export * as``
  -- and an app that uses it, a tsconfig ``paths`` alias, a relative import, and an
  import of a file that does not exist yet. ``later.ts`` uses ``Later``, which ``a.ts``
  declares below a function whose body an edit grows; ``other.ts`` uses only ``Beta``, so
  it reads ``a.ts`` and lands in ``b.ts``; ``unrelated.ts`` uses nothing.
* ``wsinc`` (I13-I18): a workspace whose package ``@inc/backend`` names every entry in
  build output that is not indexed, mapped back through the one source root its tsconfig
  ``include`` names, and an app that uses it at the root and at a wildcard subpath.
  ``lib/claims.ts`` is what the subpath maps to once ``include`` names ``lib`` instead.
* ``provider`` and ``consumer`` (I6, I7, I10): a package another repository declares and
  uses, at its root entry, through a renamed re-export, and at a ``./sub`` subpath.
  ``dup`` (I7) is a second repository that can be made to provide the same name.
* ``ipath``, ``iprov`` and ``iwrong`` (I19, I20): a package another repository declares by
  a ``file:`` path, and the repository of another name the path can be pointed at.
* ``imeta`` (I21, I22): a workspace whose members are repositories nested inside it -- a
  provider and a consumer declaring it by ``workspace:*`` -- and a second provider of the
  same name that can be nested there too.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from repoatlas.config.settings import Settings
from repoatlas.models.telemetry import UsesWork
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, init_git_repository
from tests.impact_corpus import use_edges

CHAIN_LENGTH = 14


def chain_repo() -> dict[str, str]:
    files = {
        f"chain/c{i:02d}.ts": f'export * from "./c{i + 1:02d}";\n' for i in range(CHAIN_LENGTH - 1)
    }
    files[f"chain/c{CHAIN_LENGTH - 1:02d}.ts"] = "export const Deep = 13;\n"
    files["deep.ts"] = (
        'import { Deep } from "./chain/c00";\nexport function useDeep() {\n  return Deep;\n}\n'
    )
    files["shallow.ts"] = (
        'import { Deep } from "./chain/c07";\nexport function useShallow() {\n  return Deep;\n}\n'
    )
    return files


def manifest(**fields: Any) -> str:
    """A package manifest, indented so each field sits on a line of its own."""
    return json.dumps(fields, indent=2) + "\n"


def mono_repo() -> dict[str, str]:
    return {
        "package.json": manifest(name="mono-root", private=True, workspaces=["packages/*"]),
        "tsconfig.json": json.dumps(
            {"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["apps/web/src/*"]}}}
        ),
        "packages/kit/package.json": manifest(name="@mono/kit", exports={".": "./src/index.ts"}),
        "packages/kit/src/index.ts": (
            'export * from "./a";\n'
            'export * from "./b";\n'
            'export { Gamma as Renamed } from "./c";\n'
            'export { default as Def } from "./d";\n'
            'export * as NS from "./ns";\n'
        ),
        "packages/kit/src/a.ts": (
            "export const Alpha = 1;\n"
            "export function helper() {\n"
            "  return Alpha;\n"
            "}\n"
            "export const Later = 2;\n"
        ),
        "packages/kit/src/b.ts": "export const Beta = 2;\n",
        "packages/kit/src/c.ts": "export const Gamma = 3;\n",
        "packages/kit/src/d.ts": "export default function makeDefault() {\n  return 4;\n}\n",
        "packages/kit/src/ns.ts": "export const Nested = 5;\n",
        "apps/web/src/consumer.ts": (
            'import { Alpha, Beta, Renamed, Def, NS } from "@mono/kit";\n'
            'import { util } from "@app/util";\n'
            'import { local } from "./local";\n'
            'import { Fresh } from "./fresh";\n'
            "export function useAlpha() {\n  return Alpha;\n}\n"
            "export function useBeta() {\n  return Beta;\n}\n"
            "export function useRenamed() {\n  return Renamed;\n}\n"
            "export function useDef() {\n  return Def();\n}\n"
            "export function useNamespace() {\n  return NS;\n}\n"
            "export function useUtil() {\n  return util + local;\n}\n"
            "export function useFresh() {\n  return Fresh;\n}\n"
        ),
        "apps/web/src/later.ts": (
            'import { Later } from "@mono/kit";\nexport function useLater() {\n  return Later;\n}\n'
        ),
        "apps/web/src/other.ts": (
            'import { Beta } from "@mono/kit";\nexport function useOther() {\n  return Beta;\n}\n'
        ),
        "apps/web/src/util.ts": "export const util = 6;\n",
        "apps/web/src/alt/util.ts": "export const util = 7;\n",
        "apps/web/src/local.ts": "export const local = 8;\nexport const Beta = 9;\n",
        "apps/web/src/unrelated.ts": "export function lonely() {\n  return 1;\n}\n",
    }


#: ``@inc/backend``'s exports: the root and every subpath in build output.
BACKEND_EXPORTS: dict[str, Any] = {
    ".": {"import": "./dist/index.js", "types": "./dist/index.d.ts"},
    "./*": {"import": "./dist/*.js", "types": "./dist/*.d.ts"},
}


def ws_repo() -> dict[str, str]:
    return {
        "pnpm-workspace.yaml": 'packages:\n  - "apps/*"\n  - "packages/*"\n',
        "package.json": manifest(name="wsinc-root", private=True),
        "packages/backend/package.json": manifest(
            name="@inc/backend", version="1.0.0", main="dist/index.js", exports=BACKEND_EXPORTS
        ),
        "packages/backend/tsconfig.json": json.dumps(
            {"compilerOptions": {"outDir": "./dist"}, "include": ["src/**/*"]}
        ),
        "packages/backend/src/index.ts": 'export * from "./errors";\n',
        "packages/backend/src/errors.ts": (
            "export function normalizeError(error: unknown) {\n  return error;\n}\n"
        ),
        "packages/backend/src/claims.ts": "export interface Claims {\n  id: string;\n}\n",
        "packages/backend/src/claims-v2.ts": "export interface Claims {\n  version: 2;\n}\n",
        "packages/backend/lib/claims.ts": "export interface Claims {\n  id: number;\n}\n",
        "apps/api/package.json": manifest(name="api", dependencies={"@inc/backend": "workspace:*"}),
        "apps/api/src/handler.ts": (
            'import { normalizeError } from "@inc/backend";\n'
            'import type { Claims } from "@inc/backend/claims";\n'
            "export function handle(error: unknown): Claims {\n"
            "  return normalizeError(error) as Claims;\n"
            "}\n"
        ),
        "apps/api/src/quiet.ts": "export function quiet() {\n  return 1;\n}\n",
    }


#: The provider's manifest, and the same with ``./sub`` moved to another file.
PROVIDER_MANIFEST = manifest(
    name="@fx/provider",
    version="1.0.0",
    exports={".": "./src/index.ts", "./sub": "./src/sub.ts"},
)
PROVIDER_MANIFEST_MOVED = manifest(
    name="@fx/provider",
    version="1.0.0",
    exports={".": "./src/index.ts", "./sub": "./src/sub2.ts"},
)


def provider_repo() -> dict[str, str]:
    return {
        "package.json": PROVIDER_MANIFEST,
        "src/index.ts": 'export { Pee as Pea } from "./pee";\nexport const Top = 1;\n',
        "src/pee.ts": "export const Pee = 1;\n",
        "src/pee2.ts": "export const Pee = 10;\n",
        "src/sub.ts": "export const Sub = 2;\n",
        "src/sub2.ts": "export const Sub = 3;\n",
    }


def consumer_repo() -> dict[str, str]:
    return {
        "package.json": manifest(
            name="fx-consumer", private=True, dependencies={"@fx/provider": "^1.0.0"}
        ),
        "src/use.ts": (
            'import { Pea, Top } from "@fx/provider";\n'
            'import { Sub } from "@fx/provider/sub";\n'
            "export function usePea() {\n  return Pea + Top;\n}\n"
            "export function useSub() {\n  return Sub;\n}\n"
        ),
        "src/bystander.ts": "export const Quiet = 1;\n",
    }


def dup_repo(name: str) -> dict[str, str]:
    return {
        "package.json": manifest(name=name, version="2.0.0", main="src/index.ts"),
        "src/index.ts": "export const Pea = 99;\n",
    }


def internal_provider(name: str = "@inc/internal") -> dict[str, str]:
    return {
        "package.json": manifest(name=name, version="1.0.0", main="src/index.ts"),
        "src/index.ts": "export function internalTool() {\n  return 1;\n}\n",
    }


USE_INTERNAL = (
    'import { internalTool } from "@inc/internal";\n'
    "export function useInternal() {\n  return internalTool();\n}\n"
)


def internal_consumer(constraint: str) -> dict[str, str]:
    return {
        "package.json": manifest(name="iconsumer", dependencies={"@inc/internal": constraint}),
        "src/use.ts": USE_INTERNAL,
    }


def meta_repo() -> dict[str, str]:
    """The workspace root; its members are the repositories nested inside it."""
    return {
        "pnpm-workspace.yaml": 'packages:\n  - "apps/*"\n  - "packages/*"\n',
        "package.json": manifest(name="imeta-root", private=True),
    }


def use_snapshot(workspace: Workspace) -> list[str]:
    """Every ``USES`` edge, both ends labelled and its resolution decoded, in a stable order."""
    return sorted(json.dumps(edge, sort_keys=True) for edge in use_edges(workspace))


def read_snapshot(workspace: Workspace) -> list[tuple[str, str]]:
    """``use_reads`` as (consumer, file whose export table it read), each ``repo:path``."""
    rows = workspace.database.execute(
        """
        SELECT cr.slug || ':' || c.rel_path AS consumer, rr.slug || ':' || r.rel_path AS read
        FROM use_reads u
        JOIN files c ON c.id = u.file_id JOIN repositories cr ON cr.id = c.repo_id
        JOIN files r ON r.id = u.read_file_id JOIN repositories rr ON rr.id = r.repo_id
        """
    ).fetchall()
    return sorted((row["consumer"], row["read"]) for row in rows)


def edit(root: Path, repo: str, path: str, text: str) -> None:
    """Replace one file's content."""
    target = root / repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def append(root: Path, repo: str, path: str, text: str) -> None:
    target = root / repo / path
    target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")


def sync_all(workspace: Workspace, root: Path) -> UsesWork:
    """One ordinary sync of everything under the managed root; what its USES stage did."""
    report = sync(workspace, workspace.resolver().resolve(root))
    assert report.link_trace is not None and report.link_trace.uses is not None
    return report.link_trace.uses


def use_edge_rows(workspace: Workspace, path: str) -> list[tuple[int, int, int]]:
    """(row id, source, target) of the global pass's ``USES`` edges asserted by one file."""
    rows = workspace.database.execute(
        """
        SELECT e.id, e.src_id, e.dst_id FROM edges e JOIN files f ON f.id = e.file_id
        WHERE e.type = 'USES' AND e.parser = 'linker:global' AND f.rel_path = ?
        ORDER BY e.id
        """,
        (path,),
    ).fetchall()
    return [(row["id"], row["src_id"], row["dst_id"]) for row in rows]


@contextmanager
def fresh_index(settings: Settings, root: Path, tmp_path: Path, tag: str) -> Iterator[Workspace]:
    """A pristine database indexing the tree as it is now: what an incremental sync must equal."""
    fresh = open_workspace(
        settings=settings.model_copy(
            update={
                "storage": settings.storage.model_copy(
                    update={"database": tmp_path / tag / "atlas.db"}
                )
            }
        ),
        require_root=False,
    )
    try:
        initialize(fresh, root, config_path=tmp_path / f"{tag}-config.yaml")
        yield fresh
    finally:
        fresh.close()


def add_repo(root: Path, name: str, files: dict[str, str]) -> None:
    """A new repository under the managed root, found by the next sync."""
    for relative, content in files.items():
        edit(root, name, relative, content)
    init_git_repository(root / name)


def remove_repo(root: Path, name: str) -> None:
    shutil.rmtree(root / name)


def assert_equals_fresh(
    workspace: Workspace, settings: Settings, root: Path, tmp_path: Path, tag: str
) -> None:
    """The invariant: this incrementally synced state is what a fresh index writes."""
    with fresh_index(settings, root, tmp_path, tag) as fresh:
        assert fact_snapshot(workspace) == fact_snapshot(fresh)
        assert read_snapshot(workspace) == read_snapshot(fresh)
