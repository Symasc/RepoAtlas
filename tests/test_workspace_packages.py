"""Workspace packages inside one repository: a use of a sibling package's name is exact.

A declaration that uses a name imported from another package of its own workspace uses
the declaration that package exports (:meth:`~repoatlas.graph.modules.ModuleResolver.
resolve`), even when every entry the package names is build output that is not indexed.
These tests pin the edge each fixture proves, the evidence it records, and every
arrangement that must join nothing. The fixtures are in :mod:`tests.workspace_corpus`.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses_link import use_resolver
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.workspace_corpus import REPOS, repo_uses

APP = "ws:apps/app/src"
SHARED = "ws:packages/shared/src"
BUILT = "ws:packages/built/src"


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("workspace-packages")
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


def test_w1_a_root_import_is_a_use_of_the_member_s_declaration(
    edges: list[dict[str, Any]],
) -> None:
    top = edge(edges, f"{APP}/consumer.ts#useIt", f"{SHARED}/index.ts#Top")
    assert top["basis"] == "workspace-package"
    assert top["package"] == "@fixture/shared"
    assert top["manifest"] == "packages/shared/package.json"
    assert top["entry"] == "exports"
    assert "mapped" not in top
    assert "crossing" not in top
    # the consuming declaration, never the importing file
    assert not [e for e in edges if e["source"] == "ws:apps/app/src/consumer.ts"]


def test_w3_an_alias_of_a_renamed_re_export_keeps_every_name(edges: list[dict[str, Any]]) -> None:
    used = edge(edges, f"{APP}/consumer.ts#useIt", f"{SHARED}/internal.ts#Internal")
    assert (used["imported"], used["local"], used["name"]) == ("Public", "Local", "Internal")
    assert (used["via"], used["via_names"]) == (["packages/shared/src/index.ts"], ["Public"])
    assert used["use"] == "call"


def test_w4_an_exported_subpath_names_its_own_file(edges: list[dict[str, Any]]) -> None:
    role = edge(edges, f"{APP}/consumer.ts#useRole", f"{SHARED}/auth/index.ts#Role")
    assert (role["basis"], role["specifier"]) == ("workspace-package", "@fixture/shared/auth")


def test_w5_an_entry_in_build_output_is_mapped_to_the_one_source_root(
    edges: list[dict[str, Any]],
) -> None:
    root = edge(edges, f"{APP}/built-use.ts#handle", f"{BUILT}/helpers/errors.ts#normalize")
    assert root["mapped"] == "tsconfig include"
    assert root["entry"] == "exports.import,exports.require,exports.types"
    assert root["via"] == ["packages/built/src/index.ts"]
    assert "crossing" not in root
    sub = edge(edges, f"{APP}/built-use.ts#handle", f"{BUILT}/interfaces/claims.ts#Claims")
    assert (sub["mapped"], sub["use"], sub.get("type_only")) == ("tsconfig include", "type", True)
    assert sub["specifier"] == "@fixture/built/interfaces/claims"


@pytest.mark.parametrize(
    ("declaration", "why"),
    [
        ("useAmb", "W5b: two source files answer the build output"),
        ("useRooted", "W5b: the tsconfig names two source roots"),
        ("useTwin", "W6: two members share the name"),
        ("useHidden", "W8: the subpath is not exported, though a file sits there"),
        ("useSecret", "W8b: the subpath climbs out of the package"),
        ("useLonely", "the repository provides the name, but no member of its workspace"),
    ],
)
def test_what_is_not_proven_joins_nothing(
    edges: list[dict[str, Any]], declaration: str, why: str
) -> None:
    assert from_source(edges, f"{APP}/refused.ts#{declaration}") == set(), why


def test_each_refusal_inside_the_repository_says_why(indexed: Workspace) -> None:
    assert refusals(indexed, "ws", "apps/app/src/refused.ts") == {
        "Amb": "workspace:entry-ambiguous",
        "Rooted": "workspace:entry-unresolved",
        "Twin": "workspace:provider-ambiguous",
        "Hidden": "workspace:subpath-not-exported",
        "Secret": "workspace:subpath-outside-package",
        "Lonely": "workspace:no-local-member",
    }


def test_w7_the_app_s_own_tsconfig_alias_wins_over_a_member_of_that_name(
    edges: list[dict[str, Any]],
) -> None:
    shade = edge(edges, f"{APP}/shadow-use.ts#useShade", f"{APP}/local-shadow.ts#Shade")
    assert shade["basis"] == "tsconfig-paths"
    assert from_source(edges, f"{APP}/shadow-use.ts#useShade") == {f"{APP}/local-shadow.ts#Shade"}


def test_w9_a_change_to_the_member_reaches_the_app_and_its_test(indexed: Workspace) -> None:
    response = CapabilityService(indexed).impact(
        ImpactRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            seed="sym:ws:packages/shared/src/internal.ts:Internal:1",
        )
    )
    reached = {c.entity: c for c in (*response.candidates, *response.tests)}
    use_it = reached["sym:ws:apps/app/src/consumer.ts:useIt:3"]
    assert (use_it.distance, use_it.step.edge, use_it.step.crossing) == (1, "USES", None)
    assert reached["sym:ws:apps/app/src/downstream.ts:caller:2"].distance == 2
    assert reached["sym:ws:apps/app/test/consumer.test.ts:checksCaller:2"].distance == 3
    assert {c.repository for c in reached.values()} == {"ws"}


def test_w10_names_the_member_shares_join_nothing_but_the_member(
    edges: list[dict[str, Any]],
) -> None:
    decoys = (
        "ws:packages/excluded/",
        "ws:tools/shared/",
        "ws:apps/other/",
        "elsewhere:",
    )
    assert not [e for e in edges if e["target"].startswith(decoys)]
    assert from_source(edges, f"{APP}/consumer.ts#useIt") == {
        f"{SHARED}/index.ts#Top",
        f"{SHARED}/internal.ts#Internal",
    }


def test_w11_a_package_another_repository_provides_still_crosses_on_its_own_evidence(
    edges: list[dict[str, Any]],
) -> None:
    crossing = [e for e in edges if "crossing" in e]
    assert [(e["source"], e["target"]) for e in crossing] == [
        (f"{APP}/kit-use.ts#useKit", "kit:src/tool.ts#kitTool")
    ]
    [kit] = crossing
    assert kit["basis"] == "package"
    assert kit["crossing"] == {
        "package": "npm:@fixture/kit",
        "provider": "kit",
        "provided_at": "package.json:2",
        "entry": "src/index.ts",
        "entry_fields": "exports.import,exports.types",
        "entry_mapped": {"by": "tsconfig include", "from": "dist/index.mjs", "to": "src/index.ts"},
        "declared_at": "apps/app/package.json:12",
        "constraint": "^1.0.0",
        "imported_at": 1,
    }
