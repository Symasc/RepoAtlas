"""Internal packages in another Git repository: a declaration is read as the target it names.

A package another indexed repository provides is linked by the path or the workspace a
declaration names, not only by a version of a name exactly one repository provides; and
a declaration that names something else links nothing, never falling back to the name.
These tests pin the edge each fixture proves, the evidence it records, and every
arrangement that must join nothing. The fixtures are in :mod:`tests.internal_corpus`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.internal_corpus import REPOS, refusals
from tests.workspace_corpus import repo_uses

FOO = "provider:src/foo.ts#InternalFoo"
NESTED_FOO = "mono/packages/internal:src/foo.ts#InternalFoo"
LOCAL_TOOL = "mono:packages/local/src/index.ts#localTool"
PLAIN_TOOL = "plain:src/index.ts#plainTool"


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("internal-packages")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    build(workspace, root, REPOS)
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def edges(indexed: Workspace) -> list[dict[str, Any]]:
    return repo_uses(indexed)


def edge(edges: list[dict[str, Any]], source: str, target: str) -> dict[str, Any]:
    [found] = [e for e in edges if (e["source"], e["target"]) == (source, target)]
    return found


def from_source(edges: list[dict[str, Any]], source: str) -> set[str]:
    return {e["target"] for e in edges if e["source"] == source}


# -- paths -----------------------------------------------------------------------------


def test_xw4_a_file_path_into_another_repository_pins_its_provider(
    edges: list[dict[str, Any]],
) -> None:
    # `provider` and `mono/packages/internal` both provide the name: the path decides
    use = edge(edges, "paths:file/use.ts#useFoo", FOO)
    assert use["basis"] == "package"
    assert use["crossing"] == {
        "package": "npm:@fixture/internal",
        "provider": "provider",
        "provided_at": "package.json:2",
        "entry": "src/index.ts",
        "entry_fields": "exports",
        "declared_at": "file/package.json:4",
        "constraint": "file:../../provider",
        "imported_at": 1,
        "resolved_by": "path",
    }


@pytest.mark.parametrize("protocol", ["link", "portal"])
def test_xw5_xw6_link_and_portal_paths_pin_it_the_same_way(
    edges: list[dict[str, Any]], protocol: str
) -> None:
    crossing = edge(edges, f"paths:{protocol}/use.ts#useFoo", FOO)["crossing"]
    assert (crossing["provider"], crossing["constraint"], crossing["resolved_by"]) == (
        "provider",
        f"{protocol}:../../provider",
        "path",
    )


def test_xw10_a_subpath_of_a_pinned_package_names_its_own_file(
    edges: list[dict[str, Any]],
) -> None:
    crossing = edge(edges, "paths:link/sub-use.ts#useSub", "provider:src/sub.ts#subTool")[
        "crossing"
    ]
    assert (crossing["subpath"], crossing["entry"], crossing["resolved_by"]) == (
        "sub",
        "src/sub.ts",
        "path",
    )


def test_xw11_every_name_survives_the_rename_and_the_alias(edges: list[dict[str, Any]]) -> None:
    use = edge(edges, "paths:file/use.ts#useFoo", FOO)
    assert (use["imported"], use["local"], use["name"], use["via"], use["via_names"]) == (
        "Foo",
        "LocalFoo",
        "InternalFoo",
        ["src/index.ts"],
        ["Foo"],
    )


# -- workspaces ------------------------------------------------------------------------


def test_xw1_a_workspace_range_links_the_member_nested_in_another_repository(
    edges: list[dict[str, Any]],
) -> None:
    # `mono` indexes none of the nested repository's files: the member is its own repository
    use = edge(edges, "mono:apps/app/src/use.ts#useFoo", NESTED_FOO)
    assert use["basis"] == "package"
    assert use["crossing"] == {
        "package": "npm:@fixture/internal",
        "provider": "mono/packages/internal",
        "provided_at": "package.json:2",
        "entry": "src/index.ts",
        "entry_fields": "exports",
        "declared_at": "apps/app/package.json:4",
        "constraint": "workspace:*",
        "imported_at": 1,
        "resolved_by": "workspace",
        "workspace": "mono/pnpm-workspace.yaml",
    }
    # a consumer that is itself a nested repository is a member of the same workspace
    nested = edge(edges, "mono/apps/nested-app:src/use.ts#useFoo", NESTED_FOO)["crossing"]
    assert (nested["constraint"], nested["workspace"]) == (
        "workspace:^",
        "mono/pnpm-workspace.yaml",
    )


def test_xw2_a_workspace_alias_of_the_package_s_own_name_links_it_too(
    edges: list[dict[str, Any]],
) -> None:
    crossing = edge(edges, "mono:apps/named/src/use.ts#useFoo", NESTED_FOO)["crossing"]
    assert (crossing["constraint"], crossing["resolved_by"]) == (
        "workspace:@fixture/internal@*",
        "workspace",
    )


def test_xw14_a_member_of_the_workspace_s_own_repository_resolves_as_before(
    edges: list[dict[str, Any]],
) -> None:
    local = edge(edges, "mono:apps/app/src/local-use.ts#useLocal", LOCAL_TOOL)
    assert local["basis"] == "workspace-package"
    assert "crossing" not in local


WORKSPACE_REFUSED = {
    # XW1b: no indexed file declares a workspace holding the declaring manifest
    ("paths", "ws-unindexed/use.ts"): ("plainTool", "workspace-unindexed"),
    # XW1c: the innermost workspace decides, and it has no such member
    ("mono/apps/inner-ws", "src/use.ts"): ("LocalFoo", "workspace-member-absent"),
    # XW3: a workspace alias of another package
    ("mono", "apps/other/src/use.ts"): ("LocalFoo", "dependency-aliased"),
    # XW9: two nested members share the name
    ("twins", "apps/app/src/use.ts"): ("twinTool", "provider-ambiguous"),
}


@pytest.mark.parametrize(("where", "why"), list(WORKSPACE_REFUSED.items()))
def test_each_workspace_declaration_naming_no_one_member_joins_nothing(
    indexed: Workspace, edges: list[dict[str, Any]], where: tuple[str, str], why: tuple[str, str]
) -> None:
    repository, path = where
    assert not [e for e in edges if e["source"].startswith(f"{repository}:{path}#")]
    local, reason = why
    assert refusals(indexed, repository, path) == {local: f"package:{reason}"}


def test_xw12_a_change_to_the_member_reaches_the_consumer_and_on_to_its_test(
    indexed: Workspace,
) -> None:
    response = CapabilityService(indexed).impact(
        ImpactRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            seed="sym:mono/packages/internal:src/foo.ts:InternalFoo:1",
        )
    )
    reached = {c.entity: c for c in (*response.candidates, *response.tests)}
    use = reached["sym:mono/apps/nested-app:src/use.ts:useFoo:2"]
    assert (use.distance, use.step.edge) == (1, "USES")
    assert use.step.crossing is not None
    assert use.step.crossing.model_dump(exclude_none=True) == {
        "package": "npm:@fixture/internal",
        "from_repository": "mono/packages/internal",
        "to_repository": "mono/apps/nested-app",
        "provided_by": "mono/packages/internal/package.json:2",
        "entry": "mono/packages/internal/src/index.ts",
        "entry_fields": "exports",
        "declared_by": "mono/apps/nested-app/package.json:4",
        "constraint": "workspace:^",
        "imported_at": "mono/apps/nested-app/src/use.ts:1",
        "declared_as": "InternalFoo",
        "imported_as": "Foo",
        "local": "LocalFoo",
    }
    assert reached["sym:mono/apps/nested-app:src/downstream.ts:downstream:2"].distance == 2
    test = reached["sym:mono/apps/nested-app:test/downstream.test.ts:checksDownstream:2"]
    assert test.distance == 3
    # the pinned package is credited to each dependent that crossed through it
    by_dependent = {p.dependent: p for p in response.packages}
    assert (by_dependent["mono/apps/nested-app:."].exact_consumers,) == (1,)
    # `provider` shares the name but is no member: nothing crossed from it
    assert not any(c.repository == "provider" for c in reached.values())


# -- versions --------------------------------------------------------------------------


def test_xw13_a_version_still_links_by_the_one_provider_of_the_name(
    edges: list[dict[str, Any]],
) -> None:
    use = edge(edges, "paths:semver-plain/use.ts#usePlain", PLAIN_TOOL)
    # the record is exactly what a version always recorded: nothing says it was pinned
    assert use["crossing"] == {
        "package": "npm:@fixture/plain",
        "provider": "plain",
        "provided_at": "package.json:2",
        "entry": "src/index.ts",
        "entry_fields": "main",
        "declared_at": "semver-plain/package.json:4",
        "constraint": "^1.0.0",
        "imported_at": 1,
    }
    # an npm alias spelling the package's own name is a version of it
    same = edge(edges, "paths:npm-same/use.ts#usePlain", PLAIN_TOOL)["crossing"]
    assert same["constraint"] == "npm:@fixture/plain@1.0.0"
    assert "resolved_by" not in same


# -- refusals --------------------------------------------------------------------------

REFUSED = {
    # XW7: the path leads to a package of another name
    "wrong": ("LocalFoo", "dependency-elsewhere"),
    # XW8: nothing indexed there; a directory with no manifest; a tarball
    "missing": ("plainTool", "path-outside-index"),
    "nopkg": ("plainTool", "path-no-package"),
    "tarball": ("plainTool", "path-no-package"),
    # XW3b: a remote source is not the indexed package, whoever provides the name
    "git": ("plainTool", "dependency-elsewhere"),
    # XW3: an alias of another package
    "npm-other": ("LocalFoo", "dependency-aliased"),
    # XW9: a version of a name two repositories provide
    "semver": ("LocalFoo", "provider-ambiguous"),
}


@pytest.mark.parametrize(("directory", "why"), list(REFUSED.items()))
def test_each_declaration_naming_no_indexed_provider_joins_nothing(
    indexed: Workspace, edges: list[dict[str, Any]], directory: str, why: tuple[str, str]
) -> None:
    # never a fall-back to the one repository providing the name
    assert not [e for e in edges if e["source"].startswith(f"paths:{directory}/")]
    local, reason = why
    assert refusals(indexed, "paths", f"{directory}/use.ts") == {local: f"package:{reason}"}


# -- package-level dependents ----------------------------------------------------------


def impact_packages(workspace: Workspace, seed: str) -> dict[str, Any]:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL), seed=seed)
    )
    return {p.dependent: p for p in response.packages}


def test_xw15_a_member_used_only_as_a_whole_stays_a_coarse_dependent(indexed: Workspace) -> None:
    packages = impact_packages(indexed, "sym:mono/packages/solo:src/index.ts:soloTool:1")
    solo = packages["mono/apps/nested-app:."]
    assert (solo.package, solo.crosses_repository, solo.exact_consumers, solo.consumers) == (
        "npm:@fixture/solo",
        True,
        "unresolved",
        (),
    )
    assert solo.declared_at == "mono/apps/nested-app/package.json:5"
    assert solo.unresolved_consumers == 1  # pick: a computed member


def test_a_dependent_is_listed_only_when_its_declaration_names_the_provider(
    indexed: Workspace,
) -> None:
    # every `paths` package declares @fixture/plain, which only `plain` provides: the ones
    # whose declaration names a remote source, a workspace no file declares, or a path to
    # anything but `plain` itself are not its dependents
    listed = set(impact_packages(indexed, "sym:plain:src/index.ts:plainTool:1"))
    assert listed == {"paths:semver-plain", "paths:npm-same"}
