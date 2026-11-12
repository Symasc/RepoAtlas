"""Ordinary imports and bindings resolved through configuration and package evidence.

An ``IMPORTS`` claim whose specifier is not a relative path is linked to a file only when the
resolver finds one indexed local file or a declared, unique provider. These tests pin the
positive cases next to ways that proof can fail: a duplicate provider, conflicting entry
conditions, an alias into an unindexed target or out of the repository, and a declaration naming
another source. A coincidental name is never enough.

Every name here is invented.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.ordinary_outcomes import (
    ALIAS_TARGET_NOT_INDEXED,
    AMBIGUOUS,
    CONFLICTING_PACKAGE_ENTRY,
    CROSS_REPOSITORY_PROVIDER,
    DECLARATION_NAMES_OTHER_SOURCE,
    DUPLICATE_WORKSPACE_PROVIDER,
    PACKAGE_ENTRY_NOT_FOUND,
    TARGET_NOT_FOUND,
    UNSUPPORTED,
)
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit

#: The web app of ``site``: aliases, escapes and members, in one place each.
SITE = {
    "pnpm-workspace.yaml": 'packages:\n  - "packages/*"\n  - "apps/*"\n',
    "package.json": '{"name": "site", "private": true}\n',
    "apps/web/package.json": json.dumps(
        {
            "name": "@toy/web",
            "dependencies": {"@toy/lib": "workspace:*", "@toy/renamed": "npm:@toy/other@1.0.0"},
        }
    ),
    "apps/web/tsconfig.json": json.dumps(
        {
            "compilerOptions": {
                "baseUrl": ".",
                "paths": {
                    "@/*": ["./src/*"],
                    "@gone/*": ["./nowhere/*"],
                    "@out/*": ["../../../outside/*"],
                    "@multi/*": ["./first/*", "./second/*"],
                },
            }
        }
    ),
    "apps/web/src/ok.ts": "export const ok = 1;\n",
    "apps/web/first/pick.ts": "export const picked = 'first';\n",
    "apps/web/second/pick.ts": "export const picked = 'second';\n",
    "apps/web/second/only.ts": "export const only = 'second';\n",
    "apps/web/src/main.ts": (
        'import { ok } from "@/ok";\n'  # 1 alias, one file
        'import { gone } from "@gone/x";\n'  # 2 alias into an unindexed directory
        'import { far } from "@out/x";\n'  # 3 alias out of the repository
        'import { picked } from "@multi/pick";\n'  # 4 first substitution that exists decides
        'import { only } from "@multi/only";\n'  # 5 only the second substitution holds it
        'import { lib } from "@toy/lib";\n'  # 6 workspace member
        'import { twin } from "@toy/twin";\n'  # 7 two members of one name
        'import { split } from "@toy/split";\n'  # 8 entry fields lead to different files
        'import { built } from "@toy/built";\n'  # 9 entry in build output, nothing to map it
        'import { other } from "@toy/renamed";\n'  # 10 declared as another package's alias
        'import { elsewhere } from "@toy/provided-elsewhere";\n'  # 11 only another repo provides
        'import React from "react";\n'  # 12 third party
        "export const all = [ok, gone, far, picked, only, lib, twin, split, built, other,\n"
        "  elsewhere, React];\n"
    ),
    "packages/lib/package.json": json.dumps({"name": "@toy/lib", "main": "src/index.ts"}),
    "packages/lib/src/index.ts": "export const lib = 1;\n",
    "packages/twin-a/package.json": json.dumps({"name": "@toy/twin", "main": "index.ts"}),
    "packages/twin-a/index.ts": "export const twin = 1;\n",
    "packages/twin-b/package.json": json.dumps({"name": "@toy/twin", "main": "index.ts"}),
    "packages/twin-b/index.ts": "export const twin = 2;\n",
    "packages/split/package.json": json.dumps(
        {"name": "@toy/split", "main": "./dist/a.js", "types": "./dist/b.d.ts"}
    ),
    "packages/split/tsconfig.json": json.dumps(
        {"compilerOptions": {"outDir": "dist", "rootDir": "src"}}
    ),
    "packages/split/src/a.ts": "export const split = 1;\n",
    "packages/split/src/b.ts": "export const split = 2;\n",
    "packages/built/package.json": json.dumps({"name": "@toy/built", "main": "dist/index.js"}),
    "packages/built/src/index.ts": "export const built = 1;\n",
    "packages/renamed/package.json": json.dumps({"name": "@toy/renamed", "main": "index.ts"}),
    "packages/renamed/index.ts": "export const other = 1;\n",
}

#: A file the alias ``@out/*`` would reach if it were allowed to leave its repository, and the
#: only provider of a package ``site`` imports but has no workspace for.
OUTSIDE = {
    "package.json": json.dumps({"name": "@toy/provided-elsewhere", "main": "index.ts"}),
    "index.ts": "export const elsewhere = 1;\n",
    "x.ts": "export const far = 1;\n",
}

MAIN = "apps/web/src/main.ts"


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[Workspace]:
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    build(workspace, tmp_path, {"site": SITE, "outside": OUTSIDE})
    yield workspace
    workspace.close()


def imported(workspace: Workspace, path: str = MAIN) -> dict[int, tuple[str, str]]:
    """line -> (target repository:path, basis) of the file's ordinary file-level ``IMPORTS``."""
    rows = workspace.database.execute(
        """
        SELECT e.line_start, r.slug || ':' || df.rel_path, e.resolution
        FROM edges e JOIN entities dst ON dst.id = e.dst_id
        JOIN files df ON df.id = dst.file_id JOIN repositories r ON r.id = df.repo_id
        JOIN files sf ON sf.id = e.file_id
        WHERE e.type = 'IMPORTS' AND e.parser = 'linker:global' AND sf.rel_path = ?
        """,
        (path,),
    ).fetchall()
    return {
        line: (target, json.loads(res)["basis"] if res else "relative")
        for line, target, res in rows
    }


def test_a_configured_specifier_with_one_target_in_its_own_repository_is_linked(
    indexed: Workspace,
) -> None:
    assert imported(indexed) == {
        1: ("site:apps/web/src/ok.ts", "tsconfig-paths"),
        4: ("site:apps/web/first/pick.ts", "tsconfig-paths"),
        5: ("site:apps/web/second/only.ts", "tsconfig-paths"),
        6: ("site:packages/lib/src/index.ts", "workspace-package"),
    }


@pytest.mark.parametrize(
    ("line", "why"),
    [
        (2, "an alias into a directory nothing is indexed in"),
        (3, "an alias that climbs out of the repository, onto a file another repository holds"),
        (7, "two workspace members share the name"),
        (8, "the entry's fields lead to different source files"),
        (9, "an entry in build output and nothing that says where its source is"),
        (10, "the importer declares the name as another package's alias"),
        (11, "a name only another repository provides: never an import target"),
        (12, "a third-party package"),
    ],
)
def test_unproven_imports_get_no_edge(indexed: Workspace, line: int, why: str) -> None:
    assert line not in imported(indexed), why


def test_each_refusal_is_explained_by_the_evidence_that_refused_it(tmp_path: Path) -> None:
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    try:
        build(workspace, tmp_path, {"site": SITE, "outside": OUTSIDE})
        sync(workspace, workspace.resolver().resolve(tmp_path / "repos"), classify=True)
        why: dict[str, tuple[str, str]] = {}
        for row in workspace.knowledge.list_link_decisions(family="ordinary"):
            if row.path == MAIN and row.subject.startswith("IMPORTS:module:"):
                why[row.subject.rsplit(":", 1)[1]] = (
                    row.decision,
                    json.loads(row.evidence_json)["subreason"],
                )
        assert why["@gone/x"] == (TARGET_NOT_FOUND, ALIAS_TARGET_NOT_INDEXED)
        assert why["@toy/twin"] == (AMBIGUOUS, DUPLICATE_WORKSPACE_PROVIDER)
        assert why["@toy/split"] == (AMBIGUOUS, CONFLICTING_PACKAGE_ENTRY)
        assert why["@toy/built"] == (TARGET_NOT_FOUND, PACKAGE_ENTRY_NOT_FOUND)
        assert why["@toy/renamed"] == (UNSUPPORTED, DECLARATION_NAMES_OTHER_SOURCE)
        assert why["@toy/provided-elsewhere"] == (UNSUPPORTED, CROSS_REPOSITORY_PROVIDER)
        # an alias that leaves the repository reaches no file of it, and is reported as such
        assert why["@out/x"] == (TARGET_NOT_FOUND, ALIAS_TARGET_NOT_INDEXED)
    finally:
        workspace.close()


def test_imports_do_not_create_unrelated_resolved_edges(indexed: Workspace) -> None:
    kinds = indexed.database.execute(
        "SELECT DISTINCT type FROM edges WHERE parser = 'linker:global' AND resolution IS NOT NULL"
        " AND type != 'USES'"
    ).fetchall()
    assert {row[0] for row in kinds} == {"IMPORTS"}


def test_a_changed_configuration_changes_the_edges_and_equals_a_fresh_index(
    indexed: Workspace, tmp_path: Path
) -> None:
    """Incremental and fresh agree across the edits that decide a specifier: an alias, a member's
    entry, a duplicate provider removed and a manifest that stops declaring another source."""
    root = tmp_path / "repos"
    settings = indexed.settings
    before = imported(indexed)
    edit(root, "site", "apps/web/tsconfig.json", json.dumps({"compilerOptions": {"paths": {}}}))
    sync(indexed, indexed.resolver().resolve(root))
    assert imported(indexed) == {6: ("site:packages/lib/src/index.ts", "workspace-package")}
    assert_equals_fresh(indexed, settings, root, tmp_path, "alias-removed")

    (root / "site" / "packages" / "twin-b" / "package.json").unlink()
    sync(indexed, indexed.resolver().resolve(root))
    assert imported(indexed)[7] == ("site:packages/twin-a/index.ts", "workspace-package")
    assert_equals_fresh(indexed, settings, root, tmp_path, "provider-removed")

    edit(
        root,
        "site",
        "apps/web/package.json",
        json.dumps({"name": "@toy/web", "dependencies": {"@toy/renamed": "workspace:*"}}),
    )
    sync(indexed, indexed.resolver().resolve(root))
    assert imported(indexed)[10] == ("site:packages/renamed/index.ts", "workspace-package")
    assert_equals_fresh(indexed, settings, root, tmp_path, "declaration-fixed")
    assert before != imported(indexed)


def test_a_target_in_another_repository_is_refused_even_if_a_resolver_returned_one(
    indexed: Workspace,
) -> None:
    """The resolver is repository-local by construction; the link pass does not rely on that.
    A resolver that answered with another repository's file would still produce no edge."""
    from repoatlas.graph.linker import ReferenceLinker
    from repoatlas.graph.modules import ModuleTarget
    from repoatlas.storage.protocols import UnresolvedRefRecord

    rows = indexed.knowledge.list_file_rows()
    origin = next(r for r in rows if r.rel_path == MAIN and r.repo_slug == "site")
    foreign = next(r for r in rows if r.repo_slug == "outside" and r.rel_path == "x.ts")

    class Stub:
        def resolve(self, _origin: object, _specifier: str) -> ModuleTarget:
            return ModuleTarget(foreign, "tsconfig-paths", ())

    claim = UnresolvedRefRecord(
        repo_id=origin.repo_id,
        file_id=origin.file_id,
        src_id=origin.file_entity_id,
        edge_type="IMPORTS",  # type: ignore[arg-type]
        target_kind="module",  # type: ignore[arg-type]
        target_key="@out/x",
        line_start=3,
    )
    linker = ReferenceLinker(indexed.knowledge, indexed.dependencies)
    assert linker._configured_import(claim, origin, Stub()) is None  # type: ignore[arg-type]


def test_configured_aliases_and_workspace_packages_resolve_calls_and_exports(
    workspace: Workspace, tmp_path: Path
) -> None:
    site = {
        **SITE,
        "apps/web/src/logic.ts": (
            "export function compute() {}\nexport const value = { run() {} };\n"
        ),
        "packages/lib/src/index.ts": "export function run() {}\n",
        "apps/web/src/dynamic.ts": (
            'const { compute } = await import("@/logic");\nexport { compute };\n'
        ),
        MAIN: (
            'import { compute, value } from "@/logic";\n'  # 1
            'import { run } from "@toy/lib";\n'  # 2
            'import * as utils from "@/logic";\n'  # 3
            "export { compute as publicCompute };\n"  # 4
            "export function start() {\n"  # 5
            "  compute();\n"  # 6
            "  compute();\n"  # 7
            "  run();\n"  # 8
            "  utils.compute();\n"  # 9
            "  value.run();\n"  # 10: a method of a value, not the imported run
            "}\n"
            "export function shadow(compute: () => void) {\n"
            "  compute();\n"  # 13: the parameter, not the import
            "}\n"
        ),
    }
    indexed = build(workspace, tmp_path, {"site": site, "outside": OUTSIDE})
    rows = indexed.database.execute(
        """
        SELECT e.type, e.line_start, df.rel_path, d.qualified_name, e.resolution
        FROM edges e JOIN entities d ON d.id = e.dst_id
        JOIN files df ON df.id = d.file_id JOIN files sf ON sf.id = e.file_id
        WHERE sf.rel_path = ? AND e.parser = 'linker:global'
          AND e.type IN ('CALLS', 'EXPORTS')
        """,
        (MAIN,),
    ).fetchall()
    found = {(row[0], row[1], row[2], row[3]) for row in rows}
    assert found == {
        ("EXPORTS", 4, "apps/web/src/logic.ts", "compute"),
        ("CALLS", 6, "apps/web/src/logic.ts", "compute"),
        ("CALLS", 7, "apps/web/src/logic.ts", "compute"),
        ("CALLS", 8, "packages/lib/src/index.ts", "run"),
        ("CALLS", 9, "apps/web/src/logic.ts", "compute"),
    }
    assert {json.loads(row[4])["basis"] for row in rows} == {
        "tsconfig-paths",
        "workspace-package",
    }
    dynamic_export = indexed.database.execute(
        """
        SELECT COUNT(*) FROM edges e JOIN files sf ON sf.id = e.file_id
        JOIN entities d ON d.id = e.dst_id JOIN files df ON df.id = d.file_id
        WHERE sf.rel_path = 'apps/web/src/dynamic.ts' AND e.type = 'EXPORTS'
          AND df.rel_path = 'apps/web/src/logic.ts'
        """
    ).fetchone()[0]
    assert dynamic_export == 0
    sync(indexed, indexed.resolver().resolve(tmp_path / "repos"), classify=True)
    [decision] = [
        row
        for row in indexed.knowledge.list_link_decisions(family="ordinary")
        if row.path == MAIN and row.line_start == 10 and row.subject == "CALLS:symbol:value.run"
    ]
    assert json.loads(decision.evidence_json)["subreason"] == "receiver-member"


@pytest.mark.parametrize(
    ("declared", "duplicate", "linked"),
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_cross_repository_imports_and_calls_require_a_unique_declared_provider(
    workspace: Workspace, tmp_path: Path, declared: bool, duplicate: bool, linked: bool
) -> None:
    site = {
        **SITE,
        "apps/web/package.json": json.dumps(
            {"dependencies": {"@toy/provided-elsewhere": "^1.0.0"} if declared else {}}
        ),
        MAIN: (
            'import { elsewhere } from "@toy/provided-elsewhere";\n'
            "export function run() { elsewhere(); }\n"
        ),
    }
    provider = {**OUTSIDE, "index.ts": "export function elsewhere() {}\n"}
    repos = {"site": site, "outside": provider}
    if duplicate:
        repos["duplicate"] = provider
    indexed = build(workspace, tmp_path, repos)
    rows = indexed.database.execute(
        """
        SELECT e.type, e.line_start, df.rel_path, e.resolution FROM edges e
        JOIN files sf ON sf.id = e.file_id JOIN repositories sr ON sr.id = sf.repo_id
        JOIN entities d ON d.id = e.dst_id JOIN files df ON df.id = d.file_id
        JOIN repositories dr ON dr.id = df.repo_id
        WHERE sr.slug = 'site' AND sf.rel_path = ? AND dr.slug != 'site'
          AND e.parser = 'linker:global' AND e.type IN ('IMPORTS', 'CALLS')
        """,
        (MAIN,),
    ).fetchall()
    if not linked:
        assert not rows
        return
    assert {(row[0], row[1], row[2]) for row in rows} == {
        ("IMPORTS", 1, "index.ts"),
        ("CALLS", 2, "index.ts"),
    }
    for row in rows:
        crossing = json.loads(row[3])["crossing"]
        assert crossing["package"] == "npm:@toy/provided-elsewhere"
        assert crossing["provider"] == "outside"
        assert crossing["imported_at"] == 1


def test_alias_call_tracks_configuration_changes_on_incremental_sync(
    workspace: Workspace, tmp_path: Path
) -> None:
    site = {
        **SITE,
        "apps/web/src/logic.ts": "export function compute() {}\n",
        MAIN: 'import { compute } from "@/logic";\nexport function run() { compute(); }\n',
    }
    indexed = build(workspace, tmp_path, {"site": site, "outside": OUTSIDE})

    def calls() -> list[str]:
        return [
            row[0]
            for row in indexed.database.execute(
                """
                SELECT df.rel_path FROM edges e JOIN entities d ON d.id = e.dst_id
                JOIN files df ON df.id = d.file_id JOIN files sf ON sf.id = e.file_id
                WHERE sf.rel_path = ? AND e.type = 'CALLS' AND e.parser = 'linker:global'
                """,
                (MAIN,),
            ).fetchall()
        ]

    assert calls() == ["apps/web/src/logic.ts"]
    root = tmp_path / "repos"
    edit(root, "site", "apps/web/tsconfig.json", '{"compilerOptions":{"paths":{}}}\n')
    sync(indexed, indexed.resolver().resolve(root))
    assert calls() == []
    assert_equals_fresh(indexed, indexed.settings, root, tmp_path, "alias-call-removed")
