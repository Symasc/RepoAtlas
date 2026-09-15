"""Stale-index torture on the adversarial ecosystem (V1_SPEC §12).

The invariant under test is stated at the level a user experiences it, not at the
level of table counts:

    After a sync, no question can be answered with evidence from repository
    content that is no longer in the working tree.

Each scenario establishes a fact, proves RepoAtlas knows it, mutates the tree, and
then proves the fact is gone — by *querying*, the way a user would. Where the
mutation is a git operation, the result is additionally compared against a clean
index of the same tree, which is invariant 2 of ARCHITECTURE §9.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.evidence import EvidenceCandidate
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.adversarial.conftest import ADVERSARIAL_ROOT, REPOSITORIES
from tests.conftest import fact_snapshot, git

pytestmark = pytest.mark.adversarial


def _sync(workspace: Workspace, root: Path, repo: str | None = None):
    return sync(workspace, workspace.resolver().resolve(root if repo is None else root / repo))


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _clean_index(settings: Settings, root: Path, tmp_path: Path, tag: str) -> dict[str, object]:
    """Index the tree as it stands now into a fresh database, and snapshot it."""
    from repoatlas.services.init_service import initialize
    from repoatlas.services.workspace import open_workspace

    fresh_settings = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(update={"database": tmp_path / tag / "atlas.db"})
        }
    )
    fresh = open_workspace(settings=fresh_settings, require_root=False)
    try:
        initialize(fresh, root, config_path=tmp_path / f"{tag}.yaml")
        return fact_snapshot(fresh)
    finally:
        fresh.close()


def _equals_clean_index(
    workspace: Workspace, settings: Settings, root: Path, tmp_path: Path, tag: str
) -> None:
    _sync(workspace, root)
    assert fact_snapshot(workspace) == _clean_index(settings, root, tmp_path, tag)


def can_find(workspace: Workspace, text: str) -> bool:
    return bool(workspace.search.search_literal(text))


def symbol_exists(workspace: Workspace, name: str) -> bool:
    return bool(workspace.search.find_entities_by_name([name], None, 10))


def file_indexed(workspace: Workspace, repository: str, rel_path: str) -> bool:
    row = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.name = ? AND f.rel_path = ?",
        (repository, rel_path),
    ).fetchone()
    return bool(row["n"])


def text_indexed(workspace: Workspace, needle: str) -> bool:
    row = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE instr(text, ?) > 0", (needle,)
    ).fetchone()
    return bool(row["n"])


# -- symbols -----------------------------------------------------------------


def test_a_deleted_function_stops_being_findable(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    assert symbol_exists(adversarial, "build_config")
    config = adversarial_root / "python-service" / "src" / "config.py"
    config.write_text('"""Emptied."""\n')
    _sync(adversarial, adversarial_root, "python-service")
    assert not symbol_exists(adversarial, "build_config")
    # CONNECT_RETRIES is read in ts-service, configured in deployment and named in
    # this repository's own legacy notes, so it must vanish from the *file* that no
    # longer contains it — which is exactly what a stale fact would be.
    hits = adversarial.search.search_literal("CONNECT_RETRIES")
    assert not [hit for hit in hits if hit.path == "src/config.py"]


def test_a_deleted_class_takes_its_methods_with_it(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    assert symbol_exists(adversarial, "Application")
    app = adversarial_root / "python-service" / "src" / "app.py"
    app.write_text("def main() -> None:\n    pass\n")
    _sync(adversarial, adversarial_root, "python-service")
    assert not symbol_exists(adversarial, "Application")


def test_a_renamed_symbol_is_not_answerable_under_its_old_name(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    config = adversarial_root / "python-service" / "src" / "config.py"
    config.write_text(config.read_text().replace("build_config", "assemble_config"))
    _sync(adversarial, adversarial_root, "python-service")
    assert not symbol_exists(adversarial, "build_config")
    assert symbol_exists(adversarial, "assemble_config")


def test_deleting_a_file_removes_every_answer_that_cited_it(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The headline invariant, asked as a question rather than as a row count."""
    context = adversarial.resolver().resolve(adversarial_root / "python-service")
    before = adversarial.retriever().retrieve("build_config", context)
    assert any(c.path == "src/config.py" for c in before.candidates)

    (adversarial_root / "python-service" / "src" / "config.py").unlink()
    _sync(adversarial, adversarial_root, "python-service")

    after = adversarial.retriever().retrieve("build_config", context)
    assert not any(c.path == "src/config.py" for c in after.candidates)


def test_a_moved_file_is_cited_at_its_new_location_only(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    repo = adversarial_root / "python-service"
    (repo / "src" / "settings").mkdir()
    git(repo, "mv", "src/config.py", "src/settings/config.py")
    _commit(repo, "move config")
    _sync(adversarial, adversarial_root, "python-service")

    rows = adversarial.database.execute(
        "SELECT f.rel_path FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE e.name = 'build_config'"
    ).fetchall()
    assert [row["rel_path"] for row in rows] == ["src/settings/config.py"]


# -- imports and relationships -----------------------------------------------


def test_removing_an_import_removes_the_edge_it_justified(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The cross-file symbol edge exists *because* of the import (§5)."""

    def calls() -> set[str]:
        rows = adversarial.database.execute(
            "SELECT dst.key AS d FROM edges e JOIN entities dst ON dst.id = e.dst_id "
            "JOIN entities src ON src.id = e.src_id "
            "WHERE e.type = 'CALLS' AND src.key LIKE 'sym:python-service:src/app.py%'"
        ).fetchall()
        return {row["d"] for row in rows}

    assert any("config.py" in key for key in calls())
    app = adversarial_root / "python-service" / "src" / "app.py"
    app.write_text(app.read_text().replace("from src.config import build_config\n", ""))
    _sync(adversarial, adversarial_root, "python-service")
    assert not any("config.py" in key for key in calls())


def test_deleting_a_salt_state_removes_its_requisites(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    assert symbol_exists(adversarial, "service_package")
    init = adversarial_root / "deployment" / "service" / "init.sls"
    init.write_text("service_running:\n  service.running:\n    - name: service\n")
    _sync(adversarial, adversarial_root, "deployment")

    assert not symbol_exists(adversarial, "service_package")
    remaining = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'REQUIRES' AND dst.name = 'service_package'"
    ).fetchone()["n"]
    assert remaining == 0


def test_removing_a_configures_relationship_changes_the_answer(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The §21 chain, broken on purpose. Asking where SERVICE_HOST is configured
    must stop citing the line that configured it, and the fact must be gone.

    What the answer may still contain is exactly what the file still says. Line 4
    of init.sls is a comment naming SERVICE_HOST, and a lexical match on it is true
    evidence of a mention, not a claim of configuration. An earlier version of this
    test asserted that nothing from init.sls came back at all; that held only while
    the evidence budget happened to cut the comment chunk, and changing which other
    files are parsed structurally (OFF-01) moved it back inside the budget. The
    guarantee is about the configuring line and the relationship, so that is what is
    asserted --- independently of where the budget falls.
    """
    context = adversarial.resolver().resolve(adversarial_root / "python-service")
    init = adversarial_root / "deployment" / "service" / "init.sls"
    configuring_line = next(
        number
        for number, line in enumerate(init.read_text().splitlines(), start=1)
        if "SERVICE_HOST:" in line
    )

    def citing_the_configuring_line(
        candidates: Sequence[EvidenceCandidate],
    ) -> list[EvidenceCandidate]:
        return [
            c
            for c in candidates
            if c.repository == "deployment"
            and "init.sls" in c.path
            and c.line_start is not None
            and c.line_end is not None
            and c.line_start <= configuring_line <= c.line_end
        ]

    before = adversarial.retriever().retrieve("Where is SERVICE_HOST configured?", context)
    assert citing_the_configuring_line(before.candidates)

    init.write_text(init.read_text().replace("SERVICE_HOST:", "RENAMED_HOST:"))
    _sync(adversarial, adversarial_root)

    after = adversarial.retriever().retrieve("Where is SERVICE_HOST configured?", context)
    configuring = citing_the_configuring_line(after.candidates)
    assert configuring == [], [c.location for c in configuring]
    remaining = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key = 'env:SERVICE_HOST' AND e.type = 'CONFIGURES'"
    ).fetchone()["n"]
    assert remaining == 0


def test_renaming_an_environment_variable_everywhere_retires_the_entity(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    for (
        repo,
        relative,
    ) in (
        ("python-service", "src/config.py"),
        ("ts-service", "src/settings.ts"),
        ("common-lib", "lib/settings.rb"),
        ("common-lib", "src/index.ts"),
        ("ruby-tooling", "lib/runner.rb"),
        ("scripts", "env.sh"),
        ("scripts", "deploy.sh"),
        ("deployment", "service/init.sls"),
        ("deployment", "service/files/service.conf.jinja"),
    ):
        path = adversarial_root / repo / relative
        path.write_text(path.read_text().replace("SERVICE_HOST", "RENAMED_SERVICE_HOST"))
    _sync(adversarial, adversarial_root)

    rows = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key = 'env:SERVICE_HOST'"
    ).fetchone()["n"]
    assert rows == 0
    assert symbol_exists(adversarial, "RENAMED_SERVICE_HOST")


# -- dependencies ------------------------------------------------------------


def _dependency(workspace: Workspace, repo: str, name: str, role: str) -> dict | None:
    row = workspace.database.execute(
        "SELECT constraint_text, version FROM dependencies d "
        "JOIN repositories r ON r.id = d.repo_id "
        "WHERE r.name = ? AND d.normalized_name = ? AND d.role = ?",
        (repo, name, role),
    ).fetchone()
    return dict(row) if row else None


def test_declared_and_resolved_are_stored_separately(adversarial: Workspace) -> None:
    """GROUND_TRUTH: `@company/common` is declared ^2.1.0 and resolved 2.1.4."""
    declared = _dependency(adversarial, "ts-service", "@company/common", "declared")
    resolved = _dependency(adversarial, "ts-service", "@company/common", "resolved")
    assert declared is not None and declared["constraint_text"] == "^2.1.0"
    assert resolved is not None and resolved["version"] == "2.1.4"


def test_changing_only_the_lockfile_moves_the_resolved_version(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The V2 documentation flow depends on this being exact (§13, §22)."""
    lock = adversarial_root / "ts-service" / "package-lock.json"
    lock.write_text(lock.read_text().replace('"version": "2.1.4"', '"version": "2.4.0"'))
    _sync(adversarial, adversarial_root, "ts-service")

    resolved = _dependency(adversarial, "ts-service", "@company/common", "resolved")
    assert resolved is not None and resolved["version"] == "2.4.0"
    declared = _dependency(adversarial, "ts-service", "@company/common", "declared")
    assert declared is not None and declared["constraint_text"] == "^2.1.0"

    stale = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM dependencies d JOIN repositories r ON r.id = d.repo_id "
        "WHERE r.name = 'ts-service' AND d.normalized_name = '@company/common' "
        "AND d.version = '2.1.4'"
    ).fetchone()["n"]
    assert stale == 0, "the previous resolved version must not survive"


def test_no_duplicate_dependency_rows_accumulate_across_syncs(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    for _ in range(3):
        _sync(adversarial, adversarial_root, "ts-service")
    rows = adversarial.database.execute(
        "SELECT d.normalized_name, d.role, COUNT(*) AS n FROM dependencies d "
        "JOIN repositories r ON r.id = d.repo_id WHERE r.name = 'ts-service' "
        "GROUP BY d.normalized_name, d.role HAVING n > 1"
    ).fetchall()
    assert [dict(row) for row in rows] == []


def test_renaming_the_provider_retires_the_edge_and_creates_the_new_one(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    def depends_on(target: str) -> bool:
        return bool(
            adversarial.database.execute(
                "SELECT 1 FROM edges e JOIN entities src ON src.id = e.src_id "
                "JOIN entities dst ON dst.id = e.dst_id "
                "WHERE e.type = 'DEPENDS_ON' AND src.key = 'repo:python-service' "
                "AND dst.key = ?",
                (target,),
            ).fetchall()
        )

    assert depends_on("repo:common-lib")
    provider = adversarial_root / "common-lib" / "pyproject.toml"
    provider.write_text(provider.read_text().replace('name = "common_lib"', 'name = "renamed_lib"'))
    _sync(adversarial, adversarial_root)
    assert not depends_on("repo:common-lib")


# -- git operations ----------------------------------------------------------


def test_a_branch_switch_yields_the_same_state_as_a_clean_index(
    adversarial: Workspace,
    adversarial_settings: Settings,
    adversarial_root: Path,
    tmp_path: Path,
) -> None:
    repo = adversarial_root / "python-service"
    git(repo, "checkout", "--quiet", "-b", "experiment")
    (repo / "src" / "experiment.py").write_text("def experimental():\n    return 1\n")
    (repo / "src" / "legacy_config.py").unlink()
    _commit(repo, "experiment")
    _sync(adversarial, adversarial_root)
    assert symbol_exists(adversarial, "experimental")

    git(repo, "checkout", "--quiet", "main")
    _equals_clean_index(adversarial, adversarial_settings, adversarial_root, tmp_path, "back")
    assert not symbol_exists(adversarial, "experimental")


def test_checking_out_an_older_commit_removes_the_newer_facts(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    repo = adversarial_root / "scripts"
    original = git_rev(repo)
    (repo / "later.sh").write_text("#!/usr/bin/env bash\nlater_function() { :; }\n")
    _commit(repo, "later work")
    _sync(adversarial, adversarial_root, "scripts")
    assert symbol_exists(adversarial, "later_function")

    git(repo, "checkout", "--quiet", original)
    _sync(adversarial, adversarial_root, "scripts")
    assert not symbol_exists(adversarial, "later_function")


def git_rev(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    ).stdout.strip()


def test_a_hard_reset_removes_what_it_discarded(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    # Ruby is indexed as text, not structure (OFF-01), so what a hard reset must
    # discard here is the file and its searchable text rather than a symbol.
    repo = adversarial_root / "ruby-tooling"
    baseline = git_rev(repo)
    (repo / "lib" / "extra.rb").write_text(
        "module RubyTooling\n  class ExtraDiscarded\n  end\nend\n"
    )
    _commit(repo, "extra")
    _sync(adversarial, adversarial_root, "ruby-tooling")
    assert file_indexed(adversarial, "ruby-tooling", "lib/extra.rb")
    assert text_indexed(adversarial, "ExtraDiscarded")

    git(repo, "reset", "--hard", "--quiet", baseline)
    _sync(adversarial, adversarial_root, "ruby-tooling")
    assert not file_indexed(adversarial, "ruby-tooling", "lib/extra.rb")
    assert not text_indexed(adversarial, "ExtraDiscarded")


def test_stashing_and_restoring_tracks_the_working_tree(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    # The working tree is tracked through the Ruby file's text (Ruby is gated,
    # OFF-01), which is what stays searchable for it.
    repo = adversarial_root / "common-lib"
    settings = repo / "lib" / "settings.rb"
    settings.write_text(settings.read_text().replace("class Settings", "class StashedSettings"))
    _sync(adversarial, adversarial_root, "common-lib")
    assert text_indexed(adversarial, "StashedSettings")

    git(repo, "stash", "push", "--quiet")
    _sync(adversarial, adversarial_root, "common-lib")
    assert not text_indexed(adversarial, "StashedSettings")

    git(repo, "stash", "pop", "--quiet")
    _sync(adversarial, adversarial_root, "common-lib")
    assert text_indexed(adversarial, "StashedSettings")


def test_a_large_branch_change_equals_a_clean_index(
    adversarial: Workspace,
    adversarial_settings: Settings,
    adversarial_root: Path,
    tmp_path: Path,
) -> None:
    """Many files across many repositories, changed at once."""
    for name in ("python-service", "ts-service", "deployment", "scripts"):
        repo = adversarial_root / name
        git(repo, "checkout", "--quiet", "-b", "big-change")
        for path in sorted(repo.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".sls", ".sh"}:
                continue
            try:
                existing = path.read_text()
            except UnicodeDecodeError:
                continue  # invalid_utf8.py is deliberately undecodable
            path.write_text(f"# rewritten on the branch\n{existing}")
        (repo / "added_on_branch.txt").write_text("branch content\n")
        _commit(repo, "big change")

    _equals_clean_index(adversarial, adversarial_settings, adversarial_root, tmp_path, "big")


def test_a_repository_removed_from_disk_leaves_no_answerable_facts(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The repository row may legitimately survive as a record. What must not
    survive is evidence a question can be answered with."""
    assert can_find(adversarial, "RubyTooling")
    shutil.rmtree(adversarial_root / "ruby-tooling")
    _sync(adversarial, adversarial_root)

    context = adversarial.resolver().resolve(adversarial_root)
    evidence = adversarial.retriever().retrieve("RubyTooling", context)
    assert not any(c.repository == "ruby-tooling" for c in evidence.candidates)


def test_the_whole_ecosystem_survives_repeated_syncs_unchanged(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Idempotence at the ecosystem level: a second and third sync of an unchanged
    tree must not add, remove or renumber a single fact."""
    _sync(adversarial, adversarial_root)
    first = fact_snapshot(adversarial)
    _sync(adversarial, adversarial_root)
    _sync(adversarial, adversarial_root)
    assert fact_snapshot(adversarial) == first


def test_the_fixture_ecosystem_is_intact_on_disk() -> None:
    """Guards the whole file: a missing fixture would make every test above pass
    for the wrong reason."""
    for name in REPOSITORIES:
        assert (ADVERSARIAL_ROOT / name).is_dir(), name
