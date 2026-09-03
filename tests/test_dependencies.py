"""Dependency detection and version resolution (V1_SPEC §13, §19)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.dependencies.base import DependencyParser
from repoatlas.dependencies.node import (
    PackageJsonParser,
    PackageLockParser,
    PnpmLockParser,
    YarnLockParser,
)
from repoatlas.dependencies.normalize import GEM, NPM, PYPI, normalize, python_requirement
from repoatlas.dependencies.python import (
    PyProjectParser,
    RequirementsParser,
    SetupCfgParser,
    SetupPyParser,
    UvLockParser,
)
from repoatlas.dependencies.registry import DependencyRegistry, default_dependency_parsers
from repoatlas.dependencies.ruby import GemfileLockParser, GemfileParser, GemspecParser
from repoatlas.models.dependencies import DependencyPackage
from repoatlas.services.workspace import Workspace

# --- normalization -----------------------------------------------------------


def test_pep503_normalization_makes_separators_equivalent() -> None:
    assert normalize(PYPI, "Common_Lib") == "common-lib"
    assert normalize(PYPI, "common.lib") == "common-lib"
    assert normalize(PYPI, "COMMON--LIB") == "common-lib"


def test_npm_normalization_preserves_the_scope() -> None:
    """``@company/common`` and ``common`` are different packages."""
    assert normalize(NPM, "@Company/Common") == "@company/common"
    assert normalize(NPM, "common") != normalize(NPM, "@company/common")


def test_gem_normalization_is_case_insensitive_only() -> None:
    assert normalize(GEM, "Rack") == "rack"
    assert normalize(GEM, "rails-api") == "rails-api"


def test_python_requirement_splits_name_from_constraint() -> None:
    assert python_requirement("flask>=2.0") == ("flask", ">=2.0")
    assert python_requirement("requests[security]~=2.31 ; python_version>'3'") == (
        "requests",
        "~=2.31",
    )
    assert python_requirement("# comment") is None


# --- registry ----------------------------------------------------------------


def test_every_parser_satisfies_the_protocol() -> None:
    assert all(isinstance(p, DependencyParser) for p in default_dependency_parsers())


def test_registry_dispatches_each_manifest_and_lockfile() -> None:
    registry = DependencyRegistry()
    expected = {
        "pyproject.toml": "pyproject",
        "requirements.txt": "requirements-txt",
        "requirements-dev.txt": "requirements-txt",
        "setup.py": "setup-py",
        "setup.cfg": "setup-cfg",
        "uv.lock": "python-lock",
        "poetry.lock": "python-lock",
        "package.json": "package-json",
        "package-lock.json": "package-lock",
        "pnpm-lock.yaml": "pnpm-lock",
        "yarn.lock": "yarn-lock",
        "Gemfile": "gemfile",
        "Gemfile.lock": "gemfile-lock",
        "common.gemspec": "gemspec",
    }
    for filename, parser_name in expected.items():
        assert [p.name for p in registry.for_path(Path(filename))] == [parser_name], filename
    assert registry.for_path(Path("notes.txt")) == []


def test_registry_version_changes_when_a_parser_changes() -> None:
    base = DependencyRegistry()
    trimmed = DependencyRegistry(default_dependency_parsers()[:-1])
    assert base.version != trimmed.version


# --- Python ------------------------------------------------------------------


def test_pyproject_records_the_package_it_provides_and_what_it_declares() -> None:
    report = PyProjectParser().parse(
        Path("pyproject.toml"),
        '[project]\nname = "python-service"\nversion = "0.1.0"\n'
        'dependencies = ["common-lib>=1.0"]\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n',
    )
    assert [(p.normalized_name, p.version) for p in report.provides] == [
        ("python-service", "0.1.0")
    ]
    groups = {d.name: (d.constraint, d.group) for d in report.declared}
    assert groups == {"common-lib": (">=1.0", "main"), "pytest": (">=8", "optional:dev")}


def test_poetry_table_is_read_as_well_as_pep621() -> None:
    report = PyProjectParser().parse(
        Path("pyproject.toml"),
        '[tool.poetry]\nname = "svc"\nversion = "2.0"\n\n'
        '[tool.poetry.dependencies]\npython = "^3.13"\nflask = "^3.0"\n',
    )
    assert [p.name for p in report.provides] == ["svc"]
    assert [(d.name, d.constraint) for d in report.declared] == [("flask", "^3.0")]


def test_requirements_variants_become_groups() -> None:
    report = RequirementsParser().parse(
        Path("requirements-dev.txt"), "# comment\n-r base.txt\npytest>=8\nruff\n"
    )
    assert [(d.name, d.group) for d in report.declared] == [
        ("pytest", "dev"),
        ("ruff", "dev"),
    ]


def test_setup_py_is_inspected_statically_never_executed() -> None:
    report = SetupPyParser().parse(
        Path("setup.py"),
        "from setuptools import setup\n"
        'setup(name="my_pkg", install_requires=["foo>=1", "Bar_Baz"],\n'
        '      extras_require={"dev": ["pytest"]})\n',
    )
    assert [p.normalized_name for p in report.provides] == ["my-pkg"]
    assert {d.normalized_name for d in report.declared} == {"foo", "bar-baz", "pytest"}


def test_setup_py_computed_requirements_produce_a_diagnostic_not_a_guess() -> None:
    report = SetupPyParser().parse(
        Path("setup.py"),
        "from setuptools import setup\nimport pathlib\n"
        'setup(name="x", install_requires=pathlib.Path("r.txt").read_text().split())\n',
    )
    assert report.declared == ()
    assert report.diagnostics
    assert "not knowable without executing" in report.diagnostics[0].message


def test_setup_cfg_metadata_and_install_requires() -> None:
    report = SetupCfgParser().parse(
        Path("setup.cfg"),
        "[metadata]\nname = legacy-pkg\nversion = 1.2\n\n"
        "[options]\ninstall_requires =\n    six\n    urllib3>=2\n",
    )
    assert [p.normalized_name for p in report.provides] == ["legacy-pkg"]
    assert {d.name for d in report.declared} == {"six", "urllib3"}


def test_python_lockfiles_record_resolved_versions() -> None:
    report = UvLockParser().parse(
        Path("uv.lock"),
        'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.32.3"\n',
    )
    assert [(d.name, d.version) for d in report.resolved] == [("requests", "2.32.3")]
    assert report.kind == "lockfile"


# --- Node --------------------------------------------------------------------


def test_package_json_records_every_dependency_group() -> None:
    report = PackageJsonParser().parse(
        Path("package.json"),
        '{"name": "@company/ts-service", "version": "1.0.0",\n'
        ' "dependencies": {"@company/common": "^2.1.0"},\n'
        ' "devDependencies": {"typescript": "^5"}}\n',
    )
    assert [p.normalized_name for p in report.provides] == ["@company/ts-service"]
    assert {(d.name, d.group) for d in report.declared} == {
        ("@company/common", "main"),
        ("typescript", "dev"),
    }


def test_package_lock_v3_reads_node_modules_keys() -> None:
    report = PackageLockParser().parse(
        Path("package-lock.json"),
        '{"lockfileVersion": 3, "packages": {'
        '"": {"name": "x"},'
        '"node_modules/@company/common": {"version": "2.1.4"},'
        '"node_modules/left-pad": {"version": "1.3.0"}}}',
    )
    assert {(d.name, d.version) for d in report.resolved} == {
        ("@company/common", "2.1.4"),
        ("left-pad", "1.3.0"),
    }


def test_package_lock_v1_nested_dependencies_are_flattened() -> None:
    report = PackageLockParser().parse(
        Path("package-lock.json"),
        '{"lockfileVersion": 1, "dependencies": {'
        '"a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}}}}',
    )
    assert {(d.name, d.version) for d in report.resolved} == {("a", "1.0.0"), ("b", "2.0.0")}


def test_yarn_classic_lock_shares_one_version_across_its_header_specifiers() -> None:
    report = YarnLockParser().parse(
        Path("yarn.lock"),
        "# yarn lockfile v1\n\n"
        '"@company/common@^2.1.0":\n  version "2.1.4"\n\n'
        'left-pad@^1.3.0, left-pad@~1.3:\n  version "1.3.0"\n',
    )
    assert {(d.name, d.version) for d in report.resolved} == {
        ("@company/common", "2.1.4"),
        ("left-pad", "1.3.0"),
    }


def test_pnpm_lock_keys_carry_the_version() -> None:
    report = PnpmLockParser().parse(
        Path("pnpm-lock.yaml"),
        'lockfileVersion: "9.0"\npackages:\n'
        "  /@company/common@2.1.4:\n    resolution: {integrity: x}\n"
        "  /left-pad@1.3.0:\n    resolution: {integrity: y}\n",
    )
    assert {(d.name, d.version) for d in report.resolved} == {
        ("@company/common", "2.1.4"),
        ("left-pad", "1.3.0"),
    }


# --- Ruby --------------------------------------------------------------------


def test_gemfile_records_groups() -> None:
    report = GemfileParser().parse(
        Path("Gemfile"),
        'source "https://rubygems.org"\n\ngem "rack", "~> 3.0"\n\n'
        'group :development, :test do\n  gem "rspec"\nend\n\ngem "puma"\n',
    )
    assert [(d.name, d.constraint, d.group) for d in report.declared] == [
        ("rack", "~> 3.0", "main"),
        ("rspec", None, "development,test"),
        ("puma", None, "main"),
    ]


def test_gemspec_records_the_gem_the_repository_provides() -> None:
    report = GemspecParser().parse(
        Path("common.gemspec"),
        "Gem::Specification.new do |spec|\n"
        '  spec.name = "common-lib"\n  spec.version = "1.2.3"\n'
        '  spec.add_dependency "rack", ">= 2"\n'
        '  spec.add_development_dependency "rspec"\nend\n',
    )
    assert [(p.normalized_name, p.version) for p in report.provides] == [("common-lib", "1.2.3")]
    assert {(d.name, d.group) for d in report.declared} == {("rack", "main"), ("rspec", "dev")}


def test_gemfile_lock_reads_only_top_level_specs() -> None:
    report = GemfileLockParser().parse(
        Path("Gemfile.lock"),
        "GEM\n  remote: https://rubygems.org/\n  specs:\n"
        "    rack (3.0.9)\n    rspec (3.12.0)\n      rspec-core (~> 3.12.0)\n\n"
        "PLATFORMS\n  ruby\n",
    )
    assert {(d.name, d.version) for d in report.resolved} == {
        ("rack", "3.0.9"),
        ("rspec", "3.12.0"),
    }


# --- persistence and the declared/resolved join ------------------------------


def test_declared_and_resolved_versions_are_both_stored(initialized: Workspace) -> None:
    """V1_SPEC §13: store both. The resolved half is what V2 needs."""
    view = {p.normalized_name: p for p in initialized.dependencies.list_packages(None)}
    common = view["common-lib"]
    assert common.repository == "python-service"
    assert [(o.source_path, o.value) for o in common.declarations] == [("pyproject.toml", ">=1.0")]
    assert [(o.source_path, o.value) for o in common.resolutions] == [("uv.lock", "1.4.0")]


def test_a_locked_transitive_package_survives_without_a_declaration(
    initialized: Workspace,
) -> None:
    view = {
        (p.repository, p.normalized_name): p for p in initialized.dependencies.list_packages(None)
    }
    transitive = view[("ts-service", "left-pad")]
    assert transitive.declarations == ()
    assert [o.value for o in transitive.resolutions] == ["1.3.0"]


def test_providers_are_matched_on_the_exact_normalized_name(initialized: Workspace) -> None:
    store = initialized.dependencies
    assert [name for _, name in store.providers(PYPI, "common-lib")] == ["common-lib"]
    assert [name for _, name in store.providers(NPM, "@company/common")] == ["common-lib"]
    # The unscoped name is a different package and must not match.
    assert store.providers(NPM, "common") == []


def test_dependency_facts_are_removed_with_their_manifest(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Provenance on the row means a dependency cannot outlive its manifest."""
    from repoatlas.services.sync_service import sync

    def python_service_common() -> DependencyPackage | None:
        return next(
            (
                p
                for p in initialized.dependencies.list_packages(None)
                if p.repository == "python-service" and p.normalized_name == "common-lib"
            ),
            None,
        )

    before = python_service_common()
    assert before is not None
    assert [o.value for o in before.declarations] == [">=1.0"]

    (workspace_root / "python-service" / "pyproject.toml").unlink()
    context = initialized.resolver().resolve(workspace_root / "python-service")
    sync(initialized, context)

    after = python_service_common()
    assert after is not None, "the lockfile still resolves it"
    assert after.declarations == (), "the declaration is gone with its manifest"
    assert [o.value for o in after.resolutions] == ["1.4.0"]


def test_deleting_a_manifest_drops_the_cross_repository_edge(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The edge is provenanced by the manifest that asserted it (ARCHITECTURE §3)."""
    from repoatlas.services.sync_service import sync

    def cross_repository_edges() -> int:
        row = initialized.database.execute(
            """
            SELECT COUNT(*) AS n FROM edges e
            JOIN entities src ON src.id = e.src_id
            JOIN entities dst ON dst.id = e.dst_id
            WHERE e.type = 'DEPENDS_ON'
              AND src.type = 'repository' AND dst.type = 'repository'
            """
        ).fetchone()
        return int(row["n"])

    assert cross_repository_edges() == 2

    (workspace_root / "ts-service" / "package.json").unlink()
    sync(initialized, initialized.resolver().resolve(workspace_root))
    assert cross_repository_edges() == 1
