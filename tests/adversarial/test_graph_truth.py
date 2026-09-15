"""Graph facts asserted against the hand-written ground truth.

Every expectation here comes from `fixtures/GROUND_TRUTH.md`, which was written
from the fixture construction. The ecosystem deliberately contains name collisions,
two repositories claiming one package identity, and prose that contradicts
structure — so the interesting assertions are the **forbidden** ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace

pytestmark = pytest.mark.adversarial


# -- helpers -----------------------------------------------------------------


def edges(workspace: Workspace, edge_type: str) -> set[tuple[str, str]]:
    rows = workspace.database.execute(
        "SELECT src.key AS s, dst.key AS d FROM edges e "
        "JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id WHERE e.type = ?",
        (edge_type,),
    ).fetchall()
    return {(row["s"], row["d"]) for row in rows}


def repo_dependencies(workspace: Workspace) -> set[tuple[str, str]]:
    """Repository→repository DEPENDS_ON edges only."""
    return {
        (src.removeprefix("repo:"), dst.removeprefix("repo:"))
        for src, dst in edges(workspace, "DEPENDS_ON")
        if src.startswith("repo:") and dst.startswith("repo:")
    }


def entity_keys(workspace: Workspace) -> set[str]:
    return {row["key"] for row in workspace.database.execute("SELECT key FROM entities").fetchall()}


def unresolved(workspace: Workspace) -> set[tuple[str, str]]:
    rows = workspace.database.execute(
        "SELECT edge_type, target_key FROM unresolved_refs"
    ).fetchall()
    return {(row["edge_type"], row["target_key"]) for row in rows}


def _sync_all(workspace: Workspace, root: Path) -> None:
    sync(workspace, workspace.resolver().resolve(root))


# -- cross-repository dependencies -------------------------------------------


def test_exactly_the_three_expected_cross_repository_relationships_exist(
    adversarial: Workspace,
) -> None:
    """GROUND_TRUTH: python-service→common-lib, ts-service→common-lib, and the
    scripts→python-service execution. Nothing else."""
    assert repo_dependencies(adversarial) == {
        ("python-service", "common-lib"),
        ("ts-service", "common-lib"),
    }
    assert (
        "file:scripts:deploy.sh",
        "file:python-service:src/app.py",
    ) in edges(adversarial, "EXECUTES")


def test_two_repositories_claiming_one_package_produce_no_edge(
    adversarial: Workspace,
) -> None:
    """`shared-utils` is provided by both ambiguous providers. Picking one would be
    a guess; V1_SPEC §9 forbids it."""
    for provider in ("ambiguous-provider-a", "ambiguous-provider-b"):
        assert ("python-service", provider) not in repo_dependencies(adversarial)

    # The declaration is still recorded as a fact, and the package is still interned.
    declared = adversarial.database.execute(
        "SELECT constraint_text FROM dependencies d JOIN repositories r ON r.id = d.repo_id "
        "WHERE r.name = 'python-service' AND d.normalized_name = 'shared-utils' "
        "AND d.role = 'declared'"
    ).fetchone()
    assert declared is not None and declared["constraint_text"] == ">=3.0"
    assert "pkg:pypi:shared-utils" in entity_keys(adversarial)


def test_an_ambiguity_resolved_by_deletion_becomes_a_real_edge(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Removing one claimant leaves exactly one provider, which is now provable."""
    (adversarial_root / "ambiguous-provider-b" / "pyproject.toml").unlink()
    _sync_all(adversarial, adversarial_root)
    assert ("python-service", "ambiguous-provider-a") in repo_dependencies(adversarial)


def test_a_declared_package_nobody_provides_stays_an_unresolved_claim(
    adversarial: Workspace,
) -> None:
    assert not [
        pair
        for pair in repo_dependencies(adversarial)
        if pair[0] == "python-service" and "absent" in pair[1]
    ]
    assert "pkg:pypi:absent-from-managed-set" in entity_keys(adversarial)


# -- symbol ambiguity --------------------------------------------------------


def _load_config_targets(workspace: Workspace) -> set[str]:
    """Which files a python-service reference to load_config is wired to."""
    rows = workspace.database.execute(
        "SELECT dst.key AS d FROM edges e "
        "JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id "
        "WHERE src.key LIKE 'sym:python-service:%' OR src.key LIKE 'file:python-service:%'"
    ).fetchall()
    return {row["d"] for row in rows if "load_config" in row["d"]}


def test_a_shared_symbol_name_never_resolves_to_the_wrong_definition(
    adversarial: Workspace,
) -> None:
    """`load_config` is defined in common-lib and in unrelated-repo. python-service
    imports it from common_lib.

    **V1 resolves symbols across files, not across repositories** (ARCHITECTURE §5),
    so the call stays unresolved here. What must never happen is the dangerous
    half: the definition in `unrelated-repo`, which nothing imports, becoming the
    answer because the name matches. Name equality is not evidence (V1_SPEC §2).
    """
    assert not any("unrelated-repo" in target for target in _load_config_targets(adversarial))


def test_the_intra_repository_half_of_the_rule_does_resolve(
    adversarial: Workspace,
) -> None:
    """The same rule *within* a repository is fully implemented, and this is what
    pins the difference: `app.py` imported `build_config`, so the call is wired."""
    assert (
        "sym:python-service:src/app.py:Application.__init__:7",
        "sym:python-service:src/config.py:build_config:10",
    ) in edges(adversarial, "CALLS")


def test_a_cross_repository_import_is_recorded_as_a_standing_claim(
    adversarial: Workspace,
) -> None:
    """The limitation is conservative rather than lossy: the import is a durable
    unresolved reference, so a future release can resolve it with no reparse."""
    assert ("IMPORTS", "common_lib.settings") in unresolved(adversarial)
    assert ("CALLS", "load_config") in unresolved(adversarial)


def test_removing_the_import_makes_the_reference_unresolved_not_reassigned(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The critical case: with no import, the other same-named definition must not
    become authoritative. The relationship becomes unresolved."""
    config = adversarial_root / "python-service" / "src" / "config.py"
    config.write_text(
        config.read_text().replace("from common_lib.settings import load_config\n", "")
    )
    _sync_all(adversarial, adversarial_root)

    assert _load_config_targets(adversarial) == set()
    assert not any(
        "unrelated-repo" in dst
        for src, dst in edges(adversarial, "CALLS")
        if src.startswith(("sym:python-service", "file:python-service"))
    )


def test_no_edge_ever_crosses_into_the_unrelated_repository(
    adversarial: Workspace,
) -> None:
    """Nothing in the ecosystem references unrelated-repo, by construction."""
    crossing = {
        (src, dst)
        for edge_type in ("DEPENDS_ON", "IMPORTS", "CALLS", "REFERENCES", "EXECUTES", "SOURCES")
        for src, dst in edges(adversarial, edge_type)
        if "unrelated-repo" in dst and "unrelated-repo" not in src
    }
    assert crossing == set()


# -- environment variables ---------------------------------------------------


def _env_edges(workspace: Workspace, variable: str, edge_type: str) -> set[str]:
    """Provenance of every edge to an interned variable, as ``repo/path``.

    Asserted on provenance rather than on the edge's source entity, because the
    source is a symbol when the access sits inside a function and the file when it
    does not — an implementation detail. The citation is what a user sees.
    """
    rows = workspace.database.execute(
        "SELECT r.name AS repo, f.rel_path AS path FROM edges e "
        "JOIN entities dst ON dst.id = e.dst_id "
        "JOIN repositories r ON r.id = e.repo_id "
        "JOIN files f ON f.id = e.file_id "
        "WHERE dst.key = ? AND e.type = ?",
        (f"env:{variable}", edge_type),
    ).fetchall()
    return {f"{row['repo']}/{row['path']}" for row in rows}


def test_service_host_is_read_in_every_language_that_reads_it(
    adversarial: Workspace,
) -> None:
    """GROUND_TRUTH lists five reads across Python, TypeScript and Ruby.

    The two Ruby reads are text only while Ruby structural parsing is gated
    (OFF-01): the lines still say ``ENV['SERVICE_HOST']``, and nothing extracts
    them, so neither may appear as a fact.
    """
    readers = _env_edges(adversarial, "SERVICE_HOST", "READS")
    assert readers >= {
        "python-service/src/config.py",
        "ts-service/src/settings.ts",
        "common-lib/src/index.ts",
    }, readers
    assert readers.isdisjoint({"common-lib/lib/settings.rb", "ruby-tooling/lib/runner.rb"}), readers


def test_service_host_is_exported_by_the_shell_script(adversarial: Workspace) -> None:
    assert "scripts/env.sh" in _env_edges(adversarial, "SERVICE_HOST", "EXPORTS")


def test_service_host_is_configured_by_salt(adversarial: Workspace) -> None:
    assert "deployment/service/init.sls" in _env_edges(adversarial, "SERVICE_HOST", "CONFIGURES")


def test_a_variable_local_to_one_script_is_not_a_global_entity(
    adversarial: Workspace,
) -> None:
    """`LOCAL_ONLY` is assigned and read inside env.sh and never exported. Interning
    it would claim a relationship the ecosystem does not have."""
    assert "env:LOCAL_ONLY" not in entity_keys(adversarial)


def test_prose_mentioning_a_variable_creates_no_relationship(
    adversarial: Workspace,
) -> None:
    """READMEs and legacy notes mention SERVICE_HOST. Mentioning is not reading."""
    for edge_type in ("READS", "EXPORTS", "CONFIGURES"):
        sources = _env_edges(adversarial, "SERVICE_HOST", edge_type)
        assert not any(".md" in source for source in sources), (edge_type, sources)


# -- Bash: static resolves, computed does not --------------------------------


def test_static_bash_targets_resolve(adversarial: Workspace) -> None:
    executes = edges(adversarial, "EXECUTES")
    sources = edges(adversarial, "SOURCES")
    assert ("file:scripts:deploy.sh", "file:scripts:env.sh") in sources
    assert ("file:scripts:deploy.sh", "file:python-service:src/app.py") in executes
    assert ("file:scripts:deploy.sh", "file:scripts:notify.sh") in executes


def test_no_computed_bash_target_ever_resolves(adversarial: Workspace) -> None:
    """dynamic.sh contains only computed targets. Any resolved edge out of it is a
    guess about a value that requires executing the script to know."""
    resolved = {
        (src, dst)
        for edge_type in ("EXECUTES", "SOURCES", "REFERENCES", "CALLS")
        for src, dst in edges(adversarial, edge_type)
        if src == "file:scripts:dynamic.sh" and dst.startswith("file:")
    }
    assert resolved == set(), resolved


def test_a_variable_only_read_by_a_dynamic_script_is_still_a_read(
    adversarial: Workspace,
) -> None:
    """Refusing to resolve the *target* must not also lose the variable read that
    is plainly written in the text."""
    keys = entity_keys(adversarial)
    assert "env:CONFIGURED_PATH" in keys
    assert "env:PYTHON_TARGET" in keys


# -- Salt --------------------------------------------------------------------


def test_every_salt_state_in_the_main_file_is_found(adversarial: Workspace) -> None:
    names = {
        row["name"]
        for row in adversarial.database.execute(
            "SELECT e.name FROM entities e JOIN files f ON f.id = e.file_id "
            "JOIN repositories r ON r.id = e.repo_id "
            "WHERE r.name = 'deployment' AND f.rel_path = 'service/init.sls' "
            "AND e.type = 'salt_state'"
        ).fetchall()
    }
    assert names == {
        "service_config",
        "service_package",
        "service_running",
        "injection_marker_state",
    }


def test_pillar_data_is_configuration_not_states(adversarial: Workspace) -> None:
    """A pillar file declares no `module.function`, so its keys are config data."""
    rows = adversarial.database.execute(
        "SELECT e.type, e.qualified_name FROM entities e JOIN files f ON f.id = e.file_id "
        "JOIN repositories r ON r.id = e.repo_id "
        "WHERE r.name = 'deployment' AND f.rel_path = 'service/pillar.sls'"
    ).fetchall()
    kinds = {row["type"] for row in rows}
    assert "salt_state" not in kinds
    assert {row["qualified_name"] for row in rows} >= {"service.host", "service.port"}


def test_a_templated_state_id_declares_nothing(adversarial: Workspace) -> None:
    """`deploy_{{ host }}` names nothing another file could reference."""
    names = {
        row["name"]
        for row in adversarial.database.execute(
            "SELECT e.name FROM entities e JOIN files f ON f.id = e.file_id "
            "WHERE f.rel_path = 'service/broken.sls'"
        ).fetchall()
    }
    assert not any("{{" in name or "deploy_" in name for name in names), names


def test_a_salt_source_naming_nothing_managed_does_not_resolve(
    adversarial: Workspace,
) -> None:
    resolved = {
        dst
        for src, dst in edges(adversarial, "SOURCES")
        if "missing_source" in src or "does_not_exist" in dst
    }
    assert resolved == set()
    assert (
        "REFERENCES",
        "salt://service/files/does_not_exist.jinja",
    ) in unresolved(adversarial), "the claim must survive as a durable unresolved reference"


def test_a_dynamic_template_name_does_not_resolve(adversarial: Workspace) -> None:
    """The source in dynamic.sls is built from a grain at render time."""
    for src, dst in edges(adversarial, "SOURCES"):
        assert "dynamic.sls" not in src or "{{" not in dst
        if "dynamic.sls" in src:
            assert "does_not" not in dst
