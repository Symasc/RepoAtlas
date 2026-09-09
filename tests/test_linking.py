"""Cross-file and cross-repository linking (V1_SPEC §9, §10, ARCHITECTURE.md §5).

These tests assert on the graph the global link pass produces over the fixture
repositories, which together reproduce the §21 success scenario:

    Python reads SERVICE_HOST · Salt configures SERVICE_HOST · Salt obtains the
    value from pillar · Bash invokes Python tooling · TypeScript depends on
    another local repository
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import git


def edges(workspace: Workspace, **where: object) -> list[dict[str, Any]]:
    """Every edge, joined to the names that make an assertion readable."""
    rows = workspace.database.execute(
        """
        SELECT e.type AS type, e.confidence AS confidence, e.parser AS parser,
               e.line_start AS line_start,
               src.type AS src_type, src.name AS src_name, src.key AS src_key,
               dst.type AS dst_type, dst.name AS dst_name, dst.key AS dst_key,
               r.name AS repository, f.rel_path AS path,
               dr.name AS dst_repository, df.rel_path AS dst_path
        FROM edges e
        JOIN entities src ON src.id = e.src_id
        JOIN entities dst ON dst.id = e.dst_id
        LEFT JOIN files f ON f.id = e.file_id
        LEFT JOIN repositories r ON r.id = e.repo_id
        LEFT JOIN files df ON df.id = dst.file_id
        LEFT JOIN repositories dr ON dr.id = dst.repo_id
        """
    ).fetchall()
    found = [dict(row) for row in rows]
    for key, value in where.items():
        found = [row for row in found if row[key] == value]
    return found


def one(rows: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(rows) == 1, f"expected exactly one edge, got {len(rows)}: {rows}"
    return rows[0]


# --- cross-repository relationships (V1_SPEC §9) -----------------------------


def test_local_python_package_dependency_links_the_two_repositories(
    initialized: Workspace,
) -> None:
    """``common_lib`` in a manifest matches ``common-lib`` by PEP 503, exactly."""
    edge = one(
        edges(
            initialized,
            type="DEPENDS_ON",
            src_key="repo:python-service",
            dst_key="repo:common-lib",
        )
    )
    assert edge["path"] == "pyproject.toml"
    assert edge["line_start"] == 4
    assert edge["confidence"] == 1.0


def test_local_npm_dependency_links_the_two_repositories(initialized: Workspace) -> None:
    edge = one(
        edges(initialized, type="DEPENDS_ON", src_key="repo:ts-service", dst_key="repo:common-lib")
    )
    assert edge["path"] == "package.json"


def test_a_package_no_managed_repository_provides_stays_a_package(
    initialized: Workspace,
) -> None:
    """``requests`` is real and recorded, but it is nobody's repository."""
    assert edges(initialized, dst_key="pkg:pypi:requests")
    assert not [
        row
        for row in edges(initialized, type="DEPENDS_ON")
        if row["dst_type"] == "repository" and row["dst_name"] == "requests"
    ]


def test_an_ambiguous_provider_creates_no_edge(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Two repositories providing one name is unknown, not a coin flip (§9)."""
    impostor = workspace_root / "impostor"
    impostor.mkdir()
    (impostor / "pyproject.toml").write_text('[project]\nname = "common-lib"\nversion = "9.9"\n')
    git(impostor, "init", "--quiet", "--initial-branch=main")
    git(impostor, "add", "-A")
    git(impostor, "commit", "--quiet", "-m", "impostor")

    from repoatlas.models.repository import Repository

    initialized.repositories.upsert(Repository(name="impostor", path=impostor))
    sync(initialized, initialized.resolver().resolve(workspace_root))

    assert not edges(
        initialized, type="DEPENDS_ON", src_key="repo:python-service", dst_key="repo:common-lib"
    )
    assert not edges(
        initialized, type="DEPENDS_ON", src_key="repo:python-service", dst_key="repo:impostor"
    )


def test_a_shell_script_referencing_another_repository_is_linked(
    initialized: Workspace,
) -> None:
    """``python3 ../python-service/src/app.py`` crosses a repository boundary."""
    edge = one(
        [
            row
            for row in edges(initialized, type="EXECUTES", repository="scripts")
            if row["dst_path"] == "src/app.py"
        ]
    )
    assert edge["dst_repository"] == "python-service"
    assert edge["path"] == "deploy.sh"
    assert edge["parser"] == GLOBAL_PARSER


def test_an_unresolved_reference_is_retained_and_linked_when_the_target_arrives(
    workspace: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    """Adding a repository later creates the edge without reparsing the other one."""
    from repoatlas.models.repository import Repository
    from repoatlas.services.init_service import initialize

    common = workspace_root / "common-lib"
    stashed = common.parent / ".stashed-common-lib"
    common.rename(stashed)

    initialize(workspace, workspace_root, config_path=tmp_path / "config.yaml")
    assert not edges(workspace, type="DEPENDS_ON", dst_key="repo:common-lib")

    stashed.rename(common)
    workspace.repositories.upsert(Repository(name="common-lib", path=common))
    sync(workspace, workspace.resolver().resolve(workspace_root))

    assert edges(
        workspace, type="DEPENDS_ON", src_key="repo:python-service", dst_key="repo:common-lib"
    )


# --- the import graph (ARCHITECTURE.md §5, phase 12) -------------------------


def test_a_relative_python_import_resolves_to_the_file(initialized: Workspace) -> None:
    edge = one(
        [
            row
            for row in edges(initialized, type="IMPORTS", repository="python-service")
            if row["dst_path"] == "src/config.py"
        ]
    )
    assert edge["path"] == "src/app.py"


def test_a_call_resolves_across_files_only_through_an_import(
    initialized: Workspace,
) -> None:
    """The Slice A deferral closed: ``load_config`` is wired to its definition."""
    edge = one(
        [
            row
            for row in edges(initialized, type="CALLS", repository="python-service")
            if row["dst_name"] == "load_config" and row["parser"] == GLOBAL_PARSER
        ]
    )
    assert edge["path"] == "src/app.py"
    assert edge["dst_path"] == "src/config.py"


def test_a_call_to_a_same_named_symbol_that_was_never_imported_is_not_wired(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Name equality alone is not evidence (V1_SPEC §2)."""
    target = workspace_root / "python-service" / "src" / "unrelated.py"
    target.write_text("def load_config():\n    return None\n\n\ndef caller():\n    load_config()\n")
    sync(initialized, initialized.resolver().resolve(workspace_root / "python-service"))

    crossing = [
        row
        for row in edges(initialized, type="CALLS", parser=GLOBAL_PARSER)
        if row["path"] == "src/unrelated.py"
    ]
    assert crossing == []


def test_a_relative_typescript_import_resolves_to_the_file(initialized: Workspace) -> None:
    edge = one(
        [
            row
            for row in edges(initialized, type="IMPORTS", repository="ts-service")
            if row["dst_path"] == "src/settings.ts"
        ]
    )
    assert edge["path"] == "src/index.ts"


def test_require_relative_resolves_in_ruby(initialized: Workspace) -> None:
    edge = one(
        [
            row
            for row in edges(initialized, type="IMPORTS", repository="common-lib")
            if row["dst_path"] == "lib/logging.rb"
        ]
    )
    assert edge["path"] == "lib/settings.rb"


def test_a_bash_source_resolves_to_the_sourced_script(initialized: Workspace) -> None:
    edge = one(edges(initialized, type="SOURCES"))
    assert (edge["path"], edge["dst_path"]) == ("deploy.sh", "env.sh")


# --- Salt (V1_SPEC §7.5, §9) -------------------------------------------------


def test_a_salt_include_resolves_to_the_included_sls(initialized: Workspace) -> None:
    edge = one(edges(initialized, type="INCLUDES", repository="deployment"))
    assert (edge["path"], edge["dst_path"]) == ("service/init.sls", "service/pillar.sls")


def test_a_salt_requisite_resolves_to_the_state_it_names(initialized: Workspace) -> None:
    edge = one(edges(initialized, type="REQUIRES", repository="deployment"))
    assert edge["dst_name"] == "service_package"
    assert edge["dst_path"] == "service/init.sls"


def test_a_salt_source_url_resolves_to_the_template(initialized: Workspace) -> None:
    """``salt://`` is resolved through file_roots RepoAtlas cannot read, so the
    match is on a unique path and carries reduced confidence."""
    edge = one(
        [
            row
            for row in edges(initialized, type="REFERENCES", repository="deployment")
            if row["dst_path"] == "service/files/service.conf.jinja"
        ]
    )
    assert edge["confidence"] < 1.0


def test_a_pillar_reference_resolves_to_the_key_that_defines_it(
    initialized: Workspace,
) -> None:
    """Completes the chain: Salt obtains the value from the pillar file."""
    edge = one(
        [
            row
            for row in edges(initialized, type="READS", repository="deployment")
            if row["dst_name"] == "host"
        ]
    )
    assert edge["path"] == "service/init.sls"
    assert edge["dst_path"] == "service/pillar.sls"


# --- environment variables as first-class entities (V1_SPEC §10) -------------


def test_service_host_connects_every_language_that_touches_it(
    initialized: Workspace,
) -> None:
    rows = edges(initialized, dst_key="env:SERVICE_HOST")
    reached = {(row["repository"], row["path"], row["type"]) for row in rows}
    assert ("python-service", "src/config.py", "READS") in reached
    assert ("deployment", "service/init.sls", "CONFIGURES") in reached
    assert ("scripts", "deploy.sh", "EXPORTS") in reached
    assert ("ts-service", "src/index.ts", "READS") in reached
    assert ("common-lib", "lib/settings.rb", "READS") in reached
    assert ("python-service", "Dockerfile", "EXPORTS") in reached


# --- idempotence and staleness ------------------------------------------------


def test_the_global_pass_is_idempotent(initialized: Workspace, workspace_root: Path) -> None:
    """Running it twice must give the same graph as running it once (V1_SPEC §12)."""
    before = sorted(
        (row["type"], row["src_key"], row["dst_key"], row["line_start"])
        for row in edges(initialized, parser=GLOBAL_PARSER)
    )
    initialized.sync_engine().link_global()
    after = sorted(
        (row["type"], row["src_key"], row["dst_key"], row["line_start"])
        for row in edges(initialized, parser=GLOBAL_PARSER)
    )
    assert before == after
    assert before


def test_a_deleted_file_removes_the_global_edges_it_asserted(
    initialized: Workspace, workspace_root: Path
) -> None:
    (workspace_root / "scripts" / "deploy.sh").unlink()
    sync(initialized, initialized.resolver().resolve(workspace_root))
    assert not edges(initialized, repository="scripts", type="EXECUTES")
    assert not edges(initialized, repository="scripts", type="SOURCES")


def test_removing_a_repository_removes_the_edges_pointing_into_it(
    initialized: Workspace, workspace_root: Path
) -> None:
    common = initialized.repositories.get_by_name("common-lib")
    assert common is not None and common.id is not None
    initialized.repositories.delete(common.id)
    sync(initialized, initialized.resolver().resolve(workspace_root / "python-service"))
    assert not edges(initialized, dst_key="repo:common-lib")


# --- the resolution rules themselves -----------------------------------------


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [
        ("src.config", "src/config.py"),
        ("config", "src/config.py"),
        (".config", "src/config.py"),
        ("missing.module", None),
        ("..escapes", None),
    ],
)
def test_python_module_resolution_rules(
    initialized: Workspace, specifier: str, expected: str | None
) -> None:
    index = ResolutionIndex.build(
        initialized.knowledge.list_file_rows(), initialized.knowledge.list_symbols()
    )
    origin = next(
        row
        for row in initialized.knowledge.list_file_rows()
        if row.repo_name == "python-service" and row.rel_path == "src/app.py"
    )
    resolved = index.resolve_module(origin, specifier, "python")
    assert (resolved.rel_path if resolved else None) == expected


def test_a_computed_specifier_resolves_to_nothing(initialized: Workspace) -> None:
    index = ResolutionIndex.build(
        initialized.knowledge.list_file_rows(), initialized.knowledge.list_symbols()
    )
    origin = next(
        row
        for row in initialized.knowledge.list_file_rows()
        if row.rel_path == "src/app.py" and row.repo_name == "python-service"
    )
    assert index.resolve_module(origin, "${NAME}", "python") is None
    assert index.resolve_repo_relative_path(origin, "$TARGET/x.py") is None
