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
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.internal_corpus import REPOS, refusals
from tests.workspace_corpus import repo_uses

FOO = "provider:src/foo.ts#InternalFoo"
PLAIN_TOOL = "plain:src/index.ts#plainTool"


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("internal-packages")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
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
