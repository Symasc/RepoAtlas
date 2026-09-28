"""Cross-repository ``USES``: which package imports join two repositories, on what evidence.

A use of a name imported from a package another repository provides is a use of the
declaration that package exports (:mod:`repoatlas.graph.packages`). These tests pin the
whole set of such edges over the XR corpus -- so every false join is a failure, not only
the ones a test thought to look for -- then the evidence each one carries, and the
reason every refused import joins nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.graph.modules import ModuleResolver
from repoatlas.graph.packages import PackageLink, PackageLinks
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.xrepo_corpus import LINKING, cross_repository_edges, cross_uses


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("xrepo-linking")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    build(workspace, root, LINKING)
    yield workspace
    workspace.close()


def edge(workspace: Workspace, source: str, target: str, use: str) -> dict[str, Any]:
    [found] = [
        e
        for e in cross_uses(workspace)
        if (e["source"], e["target"], e["use"]) == (source, target, use)
    ]
    return found


#: Every use that crosses a repository in the XR corpus, and nothing else.
EXPECTED = {
    # XR1: an exact named import, used as a type and as a value
    ("app:src/policy.ts#canEdit", "contracts:src/roles.ts#CourseRole", "type"),
    ("app:src/policy.ts#canEdit", "contracts:src/roles.ts#CourseRole", "value"),
    # XR2: an alias of a renamed re-export of an import, and a default under another name
    ("app:src/aliases.ts#isOwner", "contracts:src/internal.ts#InternalRole", "value"),
    ("app:src/aliases.ts#policyValue", "contracts:src/index.ts#defaultPolicy", "call"),
    # XR4: a namespace's static member is the declaration; a computed one, the module
    ("app:src/spaces.ts#staticMember", "contracts:src/roles.ts#CourseRole", "value"),
    ("app:src/spaces.ts#computedMember", "contracts:src/index.ts", "value"),
    # XR3: an exported subpath, and a file inside a package that has no exports
    ("app:src/auth-use.ts#authRole", "auth-kit:src/auth/index.ts#Role", "value"),
    ("app:src/plain-use.ts#plainTool", "plain:src/extra/tool.ts#tool", "value"),
    # N7: an entry in build output, mapped to the one source root
    ("app:src/built-use.ts#request", "built:src/params.ts#encodeParams", "call"),
    # XR7: a computed member is the whole module; `require` names nothing
    ("coarse-user:src/computed.ts#pick", "contracts:src/index.ts", "value"),
    # XR7b: the consumer reaches the provider's second package only
    ("kit-user:src/g.ts#g", "mono-kit:packages/q/src/index.ts#F", "call"),
    # the one refusal case that is not refused
    ("refusals:ok/use.ts#ok", "contracts:src/roles.ts#CourseRole", "value"),
}


def test_every_cross_repository_use_is_exactly_the_expected_set(indexed: Workspace) -> None:
    found = {(e["source"], e["target"], e["use"]) for e in cross_uses(indexed)}
    assert found == EXPECTED


def test_xr1_the_crossing_records_every_piece_of_its_evidence(indexed: Workspace) -> None:
    found = edge(indexed, "app:src/policy.ts#canEdit", "contracts:src/roles.ts#CourseRole", "type")
    assert found["at"] == "src/policy.ts"  # asserted by the consumer's file, at the use
    assert found["line"] == 2
    assert {k: found[k] for k in ("basis", "specifier", "local", "imported", "name")} == {
        "basis": "package",
        "specifier": "@fixture/contracts",
        "local": "CourseRole",
        "imported": "CourseRole",
        "name": "CourseRole",
    }
    assert (found["defined_at"], found["via"], found["via_names"]) == (
        "src/roles.ts",
        ["src/index.ts"],
        ["CourseRole"],
    )
    assert found["crossing"] == {
        "package": "npm:@fixture/contracts",
        "provider": "contracts",
        "provided_at": "package.json:2",
        "entry": "src/index.ts",
        "entry_fields": "main",
        "declared_at": "package.json:5",
        "constraint": "^1.0.0",
        "imported_at": 1,
    }


def test_xr2_each_name_the_use_passes_through_is_kept(indexed: Workspace) -> None:
    renamed = edge(
        indexed, "app:src/aliases.ts#isOwner", "contracts:src/internal.ts#InternalRole", "value"
    )
    # declared InternalRole, exported as Role by public.ts and the barrel, bound as LocalRole
    assert (renamed["name"], renamed["via"], renamed["via_names"]) == (
        "InternalRole",
        ["src/index.ts", "src/public.ts"],
        ["Role", "Role"],
    )
    assert (renamed["imported"], renamed["local"]) == ("Role", "LocalRole")
    assert renamed["crossing"]["imported_at"] == 1
    default = edge(
        indexed, "app:src/aliases.ts#policyValue", "contracts:src/index.ts#defaultPolicy", "call"
    )
    assert (default["imported"], default["local"], default["name"]) == (
        "default",
        "applyPolicy",
        "defaultPolicy",
    )
    assert default["crossing"]["imported_at"] == 2


def test_xr3_a_subpath_names_its_own_entry(indexed: Workspace) -> None:
    auth = edge(indexed, "app:src/auth-use.ts#authRole", "auth-kit:src/auth/index.ts#Role", "value")
    assert auth["crossing"]["subpath"] == "auth"
    assert (auth["crossing"]["entry"], auth["crossing"]["entry_fields"]) == (
        "src/auth/index.ts",
        "exports",
    )
    plain = edge(indexed, "app:src/plain-use.ts#plainTool", "plain:src/extra/tool.ts#tool", "value")
    assert (plain["crossing"]["subpath"], plain["crossing"]["entry_fields"]) == (
        "src/extra/tool",
        "subpath",
    )


def test_xr4_a_computed_member_is_a_use_of_the_whole_module_only(indexed: Workspace) -> None:
    whole = edge(indexed, "app:src/spaces.ts#computedMember", "contracts:src/index.ts", "value")
    assert whole["namespace"] is True
    assert whole["crossing"]["package"] == "npm:@fixture/contracts"


def test_n7_an_entry_in_build_output_is_mapped_to_its_one_source_root(indexed: Workspace) -> None:
    built = edge(
        indexed, "app:src/built-use.ts#request", "built:src/params.ts#encodeParams", "call"
    )
    assert built["crossing"]["entry"] == "src/index.ts"
    # every condition of the entry maps to the one file; the first, by name, is recorded
    assert built["crossing"]["entry_fields"] == "exports.import,exports.require,exports.types"
    assert built["crossing"]["entry_mapped"] == {
        "by": "tsconfig include",
        "from": "dist/index.mjs",
        "to": "src/index.ts",
    }


def test_n7_inside_one_repository_the_mapping_serves_a_workspace_member_without_crossing(
    indexed: Workspace,
) -> None:
    rows = indexed.database.execute(
        """
        SELECT e.resolution FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        WHERE e.type = 'USES' AND s.key LIKE 'sym:ws-built:apps/web/%' AND d.name = 'lib'
          AND d.repo_id = s.repo_id
        """
    ).fetchall()
    [resolution] = [json.loads(row[0]) for row in rows]
    assert (resolution["basis"], resolution["mapped"]) == ("workspace-package", "tsconfig include")
    assert "crossing" not in resolution


def test_xr5_repositories_that_share_only_a_name_share_no_edge(indexed: Workspace) -> None:
    assert cross_repository_edges(indexed, "right") == []
    assert cross_repository_edges(indexed, "left") == []


def _links(workspace: Workspace) -> tuple[PackageLinks, ResolutionIndex]:
    knowledge = workspace.knowledge
    index = ResolutionIndex.build(knowledge.list_file_rows(), knowledge.list_symbols())
    modules = ModuleResolver(
        index,
        knowledge.list_unresolved_refs(),
        knowledge.list_exported_names(exclude_parser=GLOBAL_PARSER),
    )
    return PackageLinks(modules, index, workspace.dependencies.list_for_repos(None)), index


@pytest.mark.parametrize(
    ("repository", "path", "specifier", "expected"),
    [
        ("refusals", "ok/use.ts", "@fixture/contracts", "link"),
        ("refusals", "aliased/use.ts", "@fixture/contracts", "dependency-aliased"),
        ("refusals", "workspace/use.ts", "@fixture/contracts", "dependency-protocol"),
        ("refusals", "shadowed/use.ts", "@fixture/contracts", "local-alias"),
        ("refusals", "undeclared/use.ts", "@fixture/contracts", "dependency-absent"),
        ("refusals", "unprovided/use.ts", "@nowhere/pkg", "provider-absent"),
        ("refusals", "indexed-dist/use.ts", "@fixture/indexed-dist", "entry-unresolved"),
        ("refusals", "split/use.ts", "@fixture/split", "entry-ambiguous"),
        ("dup-user", "src/use.ts", "@fixture/dup", "provider-ambiguous"),
        ("dup-user", "src/use.ts", "@fixture/dup-mono", "provider-ambiguous"),
        ("self-provider", "src/use.ts", "@fixture/shadowed", "provided-locally"),
        ("app", "src/internal-use.ts", "@fixture/auth-kit/internal", "subpath-not-exported"),
        ("app", "src/built-two-use.ts", "@fixture/built-two", "entry-unresolved"),
        ("app", "src/relative.ts", "../../contracts/src/roles", "relative"),
        ("right", "src/undeclared.ts", "@left/roles", "dependency-absent"),
        ("right", "src/stray.ts", "@nowhere/roles", "dependency-absent"),
    ],
)
def test_each_refusal_names_its_reason(
    indexed: Workspace, repository: str, path: str, specifier: str, expected: str
) -> None:
    links, index = _links(indexed)
    [repo_id] = [i for i, slug in index.repo_slugs.items() if slug == repository]
    origin = index.file(repo_id, path)
    assert origin is not None
    found = links.resolve(origin, specifier)
    assert (found if isinstance(found, str) else "link") == expected
    if isinstance(found, PackageLink):
        assert index.repo_slugs[found.file.repo_id] == "contracts"
