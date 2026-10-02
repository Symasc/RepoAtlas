"""Dependency provenance and multiplicity (V1_SPEC §13, ARCHITECTURE.md §3, §5).

The invariant under test, in one sentence:

    Aggregation may summarize dependency observations, but may never destroy
    provenance or silently choose one of several real declarations or resolutions.

One repository legitimately holds more than one dependency domain — a root
``package.json`` beside ``backend/package.json``, a ``pyproject.toml`` beside two
``requirements*.txt`` files — and each of them states something true. The stored
rows always kept that; the *view* over them did not. It grouped by
``(repo_id, ecosystem, normalized_name)`` and assigned ``constraint``, ``version``,
``manifest_path`` and ``lockfile_path`` in a loop, so the last row SQLite happened
to return won and every other declaration disappeared without trace.

The tests below assert both directions. Real multiplicity must survive and be
visible as multiplicity; and multiplicity must not be *invented* — several files
agreeing on one constraint is agreement, not conflict, and RepoAtlas says so
without claiming to have decided whether two different constraints are actually
incompatible (V1_SPEC §13).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.dependencies import Agreement, DependencyPackage, DependencyRole
from repoatlas.rendering.text import render_dependencies
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, init_git_repository, reindex_from_scratch

pytestmark = pytest.mark.adversarial


# --- fixtures -----------------------------------------------------------------


def package_json(name: str, dependencies: dict[str, str] | None = None) -> str:
    body = f'  "name": "{name}",\n  "version": "1.0.0"'
    if dependencies is not None:
        entries = ", ".join(f'"{k}": "{v}"' for k, v in dependencies.items())
        body += f',\n  "dependencies": {{{entries}}}'
    return "{\n" + body + "\n}\n"


def package_lock(name: str, resolved: dict[str, str]) -> str:
    entries = ",\n".join(
        f'    "node_modules/{k}": {{"version": "{v}"}}' for k, v in resolved.items()
    )
    return (
        "{\n"
        f'  "name": "{name}",\n  "lockfileVersion": 3,\n'
        '  "packages": {\n' + entries + "\n  }\n}\n"
    )


#: The fixture from the independent review, verbatim (§29).
REVIEW_TREE: dict[str, str] = {
    "package.json": package_json("@company/root", {"left-pad": "^1.0.0"}),
    "package-lock.json": package_lock("@company/root", {"left-pad": "1.3.0"}),
    "backend/package.json": package_json("@company/backend", {"left-pad": "^9.9.9"}),
    "backend/package-lock.json": package_lock("@company/backend", {"left-pad": "9.9.9"}),
    "pyproject.toml": (
        '[project]\nname = "svc"\nversion = "1.0.0"\ndependencies = ["requests>=2.0"]\n'
    ),
    "requirements.txt": "requests==2.28.0\n",
    "requirements-dev.txt": "requests==9.9.9\n",
}


def make_repo(root: Path, relative: str, files: dict[str, str]) -> Path:
    repo = root / relative
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def make_settings(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
        }
    )


def indexed(tmp_path: Path, files: dict[str, str], tag: str = "state") -> Workspace:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    make_repo(root, "svc", files)
    space = open_workspace(settings=make_settings(tmp_path, tag), require_root=False)
    initialize(space, root, config_path=tmp_path / f"{tag}-config.yaml")
    return space


def packages(space: Workspace) -> dict[tuple[str, str], DependencyPackage]:
    return {(p.ecosystem, p.normalized_name): p for p in space.dependencies.list_packages(None)}


def sync_root(space: Workspace) -> None:
    root = space.settings.root
    assert root is not None
    sync(space, space.resolver().resolve(root))


def sources(package: DependencyPackage, role: DependencyRole) -> set[tuple[str, str | None]]:
    return {(o.source_path, o.value) for o in package.observations if o.role is role}


@pytest.fixture
def review(tmp_path: Path) -> Workspace:
    return indexed(tmp_path, REVIEW_TREE)


# --- 1. the independent-review fixture, observation by observation -------------


def test_every_declaration_of_left_pad_survives(review: Workspace) -> None:
    """Two npm manifests ask for different versions. Both are real."""
    package = packages(review)[("npm", "left-pad")]

    assert sources(package, DependencyRole.DECLARED) == {
        ("package.json", "^1.0.0"),
        ("backend/package.json", "^9.9.9"),
    }


def test_every_resolution_of_left_pad_survives(review: Workspace) -> None:
    """§13: a repository may legitimately hold two resolved versions of one package."""
    package = packages(review)[("npm", "left-pad")]

    assert sources(package, DependencyRole.RESOLVED) == {
        ("package-lock.json", "1.3.0"),
        ("backend/package-lock.json", "9.9.9"),
    }


def test_every_declaration_of_requests_survives(review: Workspace) -> None:
    """Three Python files declare it; the view used to show one."""
    package = packages(review)[("pypi", "requests")]

    assert sources(package, DependencyRole.DECLARED) == {
        ("pyproject.toml", ">=2.0"),
        ("requirements.txt", "==2.28.0"),
        ("requirements-dev.txt", "==9.9.9"),
    }


def test_no_single_repository_wide_answer_is_offered(review: Workspace) -> None:
    """The whole point: there is no one `left-pad` version for this repository."""
    package = packages(review)[("npm", "left-pad")]

    assert package.declaration_agreement is Agreement.DIVERGENT
    assert package.resolution_agreement is Agreement.DIVERGENT
    assert len(package.declared_constraints) == 2
    assert len(package.resolved_versions) == 2


# --- 2. scope, and evidence-based manifest/lockfile pairing ---------------------


def test_a_manifest_pairs_only_with_the_lockfile_beside_it(review: Workspace) -> None:
    """`backend/package.json` must not be paired with the root lockfile."""
    package = packages(review)[("npm", "left-pad")]
    by_scope = {scope.scope: scope for scope in package.scopes}

    assert set(by_scope) == {".", "backend"}
    assert [o.value for o in by_scope["."].declarations] == ["^1.0.0"]
    assert [o.value for o in by_scope["."].resolutions] == ["1.3.0"]
    assert [o.value for o in by_scope["backend"].declarations] == ["^9.9.9"]
    assert [o.value for o in by_scope["backend"].resolutions] == ["9.9.9"]


def test_a_scope_with_one_declaration_and_one_resolution_is_unambiguous(
    review: Workspace,
) -> None:
    """§29: a caller that selects a scope may get one answer, because one is known."""
    package = packages(review)[("npm", "left-pad")]
    backend = next(s for s in package.scopes if s.scope == "backend")

    assert backend.declaration_agreement is Agreement.SINGLE
    assert backend.resolution_agreement is Agreement.SINGLE


def test_python_files_in_one_directory_share_a_scope_but_not_an_answer(
    review: Workspace,
) -> None:
    """Co-location is the pairing rule; it does not merge separate declarations."""
    package = packages(review)[("pypi", "requests")]
    root_scope = next(s for s in package.scopes if s.scope == ".")

    assert len(root_scope.declarations) == 3
    assert root_scope.declaration_agreement is Agreement.DIVERGENT


def test_a_nested_manifest_without_its_own_lockfile_claims_no_version(
    tmp_path: Path,
) -> None:
    """A workspace root lockfile is not evidence about a nested package's version."""
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@company/root", {"left-pad": "^1.0.0"}),
            "package-lock.json": package_lock("@company/root", {"left-pad": "1.3.0"}),
            "frontend/package.json": package_json("@company/frontend", {"left-pad": "^2.0.0"}),
        },
    )
    package = packages(space)[("npm", "left-pad")]
    frontend = next(s for s in package.scopes if s.scope == "frontend")

    assert [o.value for o in frontend.declarations] == ["^2.0.0"]
    assert frontend.resolutions == ()
    assert frontend.resolution_agreement is Agreement.NONE
    space.close()


# --- 3. agreement is not conflict ----------------------------------------------


def test_identical_constraints_in_two_files_are_agreement_not_divergence(
    tmp_path: Path,
) -> None:
    space = indexed(
        tmp_path,
        {
            "requirements.txt": "requests==2.28.0\n",
            "requirements-prod.txt": "requests==2.28.0\n",
        },
    )
    package = packages(space)[("pypi", "requests")]

    assert package.declaration_agreement is Agreement.AGREEING
    assert package.declared_constraints == ("==2.28.0",)
    space.close()


def test_agreeing_declarations_keep_both_source_paths(tmp_path: Path) -> None:
    """Deduplicating identical text is not deduplicating provenance."""
    space = indexed(
        tmp_path,
        {
            "requirements-a.txt": "requests==2.28.0\n",
            "requirements-b.txt": "requests==2.28.0\n",
        },
    )
    package = packages(space)[("pypi", "requests")]

    assert len(package.declarations) == 2
    assert {o.source_path for o in package.declarations} == {
        "requirements-a.txt",
        "requirements-b.txt",
    }
    space.close()


def test_one_declaration_is_reported_as_single(tmp_path: Path) -> None:
    space = indexed(tmp_path, {"requirements.txt": "requests==2.28.0\n"})
    package = packages(space)[("pypi", "requests")]

    assert package.declaration_agreement is Agreement.SINGLE
    assert package.resolution_agreement is Agreement.NONE
    space.close()


# --- 4. one-sided states are real states ---------------------------------------


def test_a_declared_package_with_no_lockfile_entry_survives(tmp_path: Path) -> None:
    space = indexed(tmp_path, {"package.json": package_json("@c/a", {"left-pad": "^1.0.0"})})
    package = packages(space)[("npm", "left-pad")]

    assert package.declarations
    assert package.resolutions == ()
    space.close()


def test_a_resolved_package_with_no_declaration_survives(tmp_path: Path) -> None:
    """A transitive lock entry is a fact about what is installed."""
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@c/a", {}),
            "package-lock.json": package_lock("@c/a", {"transitive-only": "4.5.6"}),
        },
    )
    package = packages(space)[("npm", "transitive-only")]

    assert package.declarations == ()
    assert [o.value for o in package.resolutions] == ["4.5.6"]
    space.close()


def test_a_provided_package_is_an_observation_with_provenance(tmp_path: Path) -> None:
    space = indexed(tmp_path, {"package.json": package_json("@company/common", {})})
    package = packages(space)[("npm", "@company/common")]

    assert [(o.source_path, o.value) for o in package.provisions] == [("package.json", "1.0.0")]
    space.close()


def test_two_scopes_providing_different_packages_both_survive(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@company/root", {}),
            "backend/package.json": package_json("@company/backend", {}),
        },
    )
    found = packages(space)

    assert found[("npm", "@company/root")].provisions[0].scope == "."
    assert found[("npm", "@company/backend")].provisions[0].scope == "backend"
    space.close()


# --- 5. identity: ecosystem and normalization ----------------------------------


def test_the_same_normalized_name_in_two_ecosystems_stays_separate(
    tmp_path: Path,
) -> None:
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@c/a", {"foo-bar": "^1.0.0"}),
            "requirements.txt": "foo_bar==2.0.0\n",
        },
    )
    found = packages(space)

    assert ("npm", "foo-bar") in found
    assert ("pypi", "foo-bar") in found
    assert found[("npm", "foo-bar")] is not found[("pypi", "foo-bar")]
    space.close()


def test_pypi_normalization_still_merges_spellings_of_one_package(
    tmp_path: Path,
) -> None:
    """PEP 503 equivalence must keep working; it must not collapse the sources."""
    space = indexed(
        tmp_path,
        {
            "requirements.txt": "Common_Lib==1.0.0\n",
            "requirements-dev.txt": "common-lib==2.0.0\n",
        },
    )
    package = packages(space)[("pypi", "common-lib")]

    assert package.declaration_agreement is Agreement.DIVERGENT
    assert {o.source_path for o in package.declarations} == {
        "requirements.txt",
        "requirements-dev.txt",
    }
    space.close()


# --- 5b. groups are part of an observation, not a reason to merge ---------------


def test_one_manifest_declaring_a_package_twice_keeps_both_groups(tmp_path: Path) -> None:
    """npm `dependencies` and `devDependencies` are two requests, not one."""
    space = indexed(
        tmp_path,
        {
            "package.json": "{\n"
            '  "name": "@c/a",\n  "version": "1.0.0",\n'
            '  "dependencies": {"left-pad": "^1.0.0"},\n'
            '  "devDependencies": {"left-pad": "^2.0.0"}\n}\n'
        },
    )
    package = packages(space)[("npm", "left-pad")]

    assert {(o.group, o.value) for o in package.declarations} == {
        ("main", "^1.0.0"),
        ("dev", "^2.0.0"),
    }
    assert package.declaration_agreement is Agreement.DIVERGENT
    space.close()


def test_a_python_optional_extra_is_a_separate_observation(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "svc"\nversion = "1.0.0"\n'
            'dependencies = ["requests>=2.0"]\n\n'
            "[project.optional-dependencies]\n"
            'test = ["requests==3.0.0"]\n'
        },
    )
    package = packages(space)[("pypi", "requests")]

    assert {(o.group, o.value) for o in package.declarations} == {
        ("main", ">=2.0"),
        ("optional:test", "==3.0.0"),
    }
    space.close()


def test_a_ruby_group_block_keeps_its_group_and_pairs_with_its_lockfile(
    tmp_path: Path,
) -> None:
    space = indexed(
        tmp_path,
        {
            "Gemfile": 'source "https://rubygems.org"\ngem "rake", "~> 13.0"\n\n'
            'group :development do\n  gem "rake", "~> 12.0"\nend\n',
            "Gemfile.lock": "GEM\n  remote: https://rubygems.org/\n  specs:\n"
            "    rake (13.0.6)\n\nDEPENDENCIES\n  rake (~> 13.0)\n",
        },
    )
    package = packages(space)[("gem", "rake")]
    root_scope = next(s for s in package.scopes if s.scope == ".")

    assert {o.group for o in package.declarations} >= {"main", "development"}
    assert [o.value for o in root_scope.resolutions] == ["13.0.6"]
    space.close()


# --- 6. ordering ---------------------------------------------------------------


def test_observation_order_is_deterministic_and_not_insertion_order(
    review: Workspace,
) -> None:
    """No last-writer semantics anywhere, including in what is shown first."""
    package = packages(review)[("npm", "left-pad")]
    order = [(o.scope, o.role.value, o.source_path, o.value) for o in package.observations]

    assert order == sorted(order)
    assert order[0][0] == "."


def test_repeated_calls_return_identical_output(review: Workspace) -> None:
    first = review.dependencies.list_packages(None)
    second = review.dependencies.list_packages(None)

    assert first == second


# --- 7. rendering ---------------------------------------------------------------


def test_rendering_shows_every_observation_of_a_divergent_package(
    review: Workspace,
) -> None:
    rendered = render_dependencies(
        [p for p in review.dependencies.list_packages(None) if p.normalized_name == "left-pad"]
    )

    for expected in (
        "package.json",
        "backend/package.json",
        "package-lock.json",
        "backend/package-lock.json",
        "^1.0.0",
        "^9.9.9",
        "1.3.0",
        "9.9.9",
    ):
        assert expected in rendered, f"{expected} missing from:\n{rendered}"


def test_rendering_marks_multiplicity_without_claiming_incompatibility(
    review: Workspace,
) -> None:
    """§12: RepoAtlas has no constraint solver, so it does not say "conflict"."""
    rendered = render_dependencies(review.dependencies.list_packages(None))

    assert "declarations" in rendered
    assert "conflict" not in rendered.lower()
    assert "incompatible" not in rendered.lower()


def test_rendering_stays_compact_for_an_unambiguous_package(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@c/a", {"left-pad": "^1.0.0"}),
            "package-lock.json": package_lock("@c/a", {"left-pad": "1.3.0"}),
        },
    )
    rendered = render_dependencies(
        [p for p in space.dependencies.list_packages(None) if p.normalized_name == "left-pad"]
    )

    assert "declarations" not in rendered
    assert "^1.0.0" in rendered
    assert "1.3.0" in rendered
    space.close()


# --- 8. the graph and the linker ------------------------------------------------


def test_local_provider_linking_still_works(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "common-lib", {"package.json": package_json("@company/common", {})})
    make_repo(
        root, "svc", {"package.json": package_json("@company/svc", {"@company/common": "^1.0.0"})}
    )
    space = open_workspace(settings=make_settings(tmp_path), require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    edges = [
        (str(row[0]), str(row[1]))
        for row in space.database.execute(
            "SELECT src.key, dst.key FROM edges e "
            "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
            "WHERE src.type = 'repository' AND dst.type = 'repository'"
        ).fetchall()
    ]

    assert edges == [("repo:svc", "repo:common-lib")]
    space.close()


def test_two_providers_of_one_package_still_produce_no_edge(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "a", {"package.json": package_json("@company/common", {})})
    make_repo(root, "b", {"package.json": package_json("@company/common", {})})
    make_repo(
        root, "svc", {"package.json": package_json("@company/svc", {"@company/common": "^1.0.0"})}
    )
    space = open_workspace(settings=make_settings(tmp_path), require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    edges = space.database.execute(
        "SELECT src.key FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE src.type = 'repository' AND dst.type = 'repository'"
    ).fetchall()

    assert edges == []
    space.close()


def test_two_declarations_of_one_package_keep_both_provenances_and_one_relationship(
    tmp_path: Path,
) -> None:
    """§15: provenance must not be lost, and the relationship must not be doubled."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "common-lib", {"package.json": package_json("@company/common", {})})
    make_repo(
        root,
        "svc",
        {
            "package.json": package_json("@company/svc", {"@company/common": "^1.0.0"}),
            "backend/package.json": package_json(
                "@company/svc-backend", {"@company/common": "^2.0.0"}
            ),
        },
    )
    space = open_workspace(settings=make_settings(tmp_path), require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    rows = space.database.execute(
        "SELECT f.rel_path FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "JOIN files f ON f.id = e.file_id "
        "WHERE src.type = 'repository' AND dst.type = 'repository'"
    ).fetchall()
    paths = sorted(str(row[0]) for row in rows)
    package = next(
        p
        for p in space.dependencies.list_packages(None)
        if p.normalized_name == "@company/common" and p.declarations
    )

    assert paths == ["backend/package.json", "package.json"], "each declaration keeps its edge"
    assert len(package.declarations) == 2
    space.close()


def test_repeated_declarations_report_one_cross_repository_relationship(
    tmp_path: Path,
) -> None:
    """Two manifests stating one repository-to-repository fact is one relationship.

    Each keeps its own provenanced edge, because provenance lives on the edge
    (ARCHITECTURE.md §3); the *count* describes the graph, not the manifests.
    """
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "common-lib", {"package.json": package_json("@company/common", {})})
    make_repo(
        root,
        "svc",
        {
            "package.json": package_json("@company/svc", {"@company/common": "^1.0.0"}),
            "backend/package.json": package_json(
                "@company/svc-backend", {"@company/common": "^2.0.0"}
            ),
        },
    )
    space = open_workspace(settings=make_settings(tmp_path), require_root=False)
    report = initialize(space, root, config_path=tmp_path / "config.yaml")[0]

    assert report.cross_repository_edges == 1
    space.close()


def test_the_linker_does_not_consume_the_aggregated_view(tmp_path: Path) -> None:
    """Graph correctness must not depend on presentation aggregation."""
    import inspect

    from repoatlas.graph import linker

    source = inspect.getsource(linker)

    assert "list_packages" not in source
    assert "join_view" not in source


# --- 9. the model the LLM is given ----------------------------------------------


def test_no_single_dependency_version_is_handed_to_the_model(review: Workspace) -> None:
    """A package entity is interned, so it can never be cited as a located fact.

    The model only ever sees manifest and lockfile *text*, verbatim and with its own
    provenance. There is therefore no path by which the deterministic layer hands it
    `left-pad = 1.3.0` as the repository's answer while knowing two exist.
    """
    ids = [
        int(row[0])
        for row in review.database.execute(
            "SELECT id FROM entities WHERE key LIKE 'pkg:%'"
        ).fetchall()
    ]

    assert ids, "the fixture must actually produce package entities"
    assert review.knowledge.describe_entities(ids) == []


# --- 10. incremental sync -------------------------------------------------------


def test_deleting_one_manifest_removes_only_its_observation(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {"requirements.txt": "requests==2.28.0\n", "requirements-dev.txt": "requests==9.9.9\n"},
    )
    root = space.settings.root
    assert root is not None
    assert packages(space)[("pypi", "requests")].declaration_agreement is Agreement.DIVERGENT

    (root / "svc" / "requirements-dev.txt").unlink()
    sync_root(space)

    package = packages(space)[("pypi", "requests")]
    assert sources(package, DependencyRole.DECLARED) == {("requirements.txt", "==2.28.0")}
    assert package.declaration_agreement is Agreement.SINGLE
    space.close()


def test_changing_one_constraint_changes_only_that_observation(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {"requirements.txt": "requests==2.28.0\n", "requirements-dev.txt": "requests==9.9.9\n"},
    )
    root = space.settings.root
    assert root is not None

    (root / "svc" / "requirements-dev.txt").write_text("requests==9.9.10\n", encoding="utf-8")
    sync_root(space)

    assert sources(packages(space)[("pypi", "requests")], DependencyRole.DECLARED) == {
        ("requirements.txt", "==2.28.0"),
        ("requirements-dev.txt", "==9.9.10"),
    }
    space.close()


def test_a_lockfile_only_change_leaves_the_declaration_alone(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@c/a", {"left-pad": "^1.0.0"}),
            "package-lock.json": package_lock("@c/a", {"left-pad": "1.3.0"}),
        },
    )
    root = space.settings.root
    assert root is not None

    (root / "svc" / "package-lock.json").write_text(
        package_lock("@c/a", {"left-pad": "1.4.0"}), encoding="utf-8"
    )
    sync_root(space)

    package = packages(space)[("npm", "left-pad")]
    assert sources(package, DependencyRole.DECLARED) == {("package.json", "^1.0.0")}
    assert sources(package, DependencyRole.RESOLVED) == {("package-lock.json", "1.4.0")}
    space.close()


def test_adding_a_second_scope_keeps_both_visible(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@c/a", {"left-pad": "^1.0.0"}),
            "package-lock.json": package_lock("@c/a", {"left-pad": "1.3.0"}),
        },
    )
    root = space.settings.root
    assert root is not None
    backend = root / "svc" / "backend"
    backend.mkdir()
    (backend / "package.json").write_text(
        package_json("@c/b", {"left-pad": "^9.9.9"}), encoding="utf-8"
    )
    (backend / "package-lock.json").write_text(
        package_lock("@c/b", {"left-pad": "9.9.9"}), encoding="utf-8"
    )
    sync_root(space)

    package = packages(space)[("npm", "left-pad")]
    assert {s.scope for s in package.scopes} == {".", "backend"}
    assert package.resolution_agreement is Agreement.DIVERGENT
    space.close()


def test_moving_a_manifest_leaves_no_stale_observation(tmp_path: Path) -> None:
    space = indexed(
        tmp_path,
        {
            "package.json": package_json("@c/a", {}),
            "backend/package.json": package_json("@c/b", {"left-pad": "^9.9.9"}),
        },
    )
    root = space.settings.root
    assert root is not None
    moved = root / "svc" / "services" / "backend"
    moved.mkdir(parents=True)
    (root / "svc" / "backend" / "package.json").rename(moved / "package.json")
    (root / "svc" / "backend").rmdir()
    sync_root(space)

    package = packages(space)[("npm", "left-pad")]
    assert sources(package, DependencyRole.DECLARED) == {
        ("services/backend/package.json", "^9.9.9")
    }
    assert {s.scope for s in package.scopes} == {"services/backend"}
    space.close()


def test_incremental_state_equals_a_full_reindex(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "svc", REVIEW_TREE)
    settings = make_settings(tmp_path)
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    (root / "svc" / "requirements-dev.txt").unlink()
    (root / "svc" / "backend" / "package-lock.json").write_text(
        package_lock("@company/backend", {"left-pad": "9.9.10"}), encoding="utf-8"
    )
    sync_root(space)

    assert fact_snapshot(space) == reindex_from_scratch(settings, root, tmp_path, "fresh")
    space.close()
