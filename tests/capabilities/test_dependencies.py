"""The deterministic dependencies capability (V2 Slice G).

RepoAtlas reads manifests and lockfiles. It does not resolve them, install
anything, consult a registry or look at a running environment — so every
temptation this suite guards against is the same one wearing a different hat: to
turn a statement a file makes into a statement about a program.

Four of those are load-bearing, and each would produce a response that looks
entirely reasonable:

* a **declaration** is not a **resolution**, and neither is an installation;
* observations are grouped, never reduced — there is no winning version;
* a package's identity includes its ecosystem *and* its repository, because npm
  ``foo-bar`` and PyPI ``foo_bar`` are not the same dependency and two
  repositories declaring one package are making two statements;
* a name filter is exact under the ecosystem's own equality rule, because this is
  structured inventory and search is the capability for resemblance.

The read-only section at the end is what the rest rests on: a capability that
could write could make any of these assertions true after the fact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidDependencyRequestError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependencies import DependencyRole
from repoatlas.models.dependency_inventory import (
    MAX_PAGE_SIZE,
    DependenciesRequest,
    DependenciesResponse,
    DependencyPackageView,
    Ecosystem,
)
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
COMMON = "common-lib"
TS = "ts-service"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _ask(workspace: Workspace, **kwargs: object) -> DependenciesResponse:
    fields: dict[str, object] = {"context": GLOBAL, "limit": MAX_PAGE_SIZE}
    fields.update(kwargs)
    return _service(workspace).dependencies(DependenciesRequest(**fields))  # type: ignore[arg-type]


def _repo(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _one(response: DependenciesResponse, slug: str, name: str) -> DependencyPackageView:
    found = [
        package
        for package in response.packages
        if package.repository_slug == slug and package.normalized_name == name
    ]
    assert len(found) == 1, f"expected exactly one {slug}/{name}"
    return found[0]


def _values(package: DependencyPackageView, role: DependencyRole) -> list[str | None]:
    return [o.value for o in package.observations if o.role is role]


def _ids(workspace: Workspace, slug: str, rel_path: str) -> tuple[int, int]:
    row = workspace.database.execute(
        "SELECT f.id AS file_id, f.repo_id AS repo_id FROM files f "
        "JOIN repositories r ON r.id = f.repo_id WHERE r.slug = ? AND f.rel_path = ?",
        (slug, rel_path),
    ).fetchone()
    assert row is not None, f"{slug}/{rel_path}"
    return (int(row["repo_id"]), int(row["file_id"]))


def _plant(
    workspace: Workspace,
    *,
    slug: str,
    rel_path: str,
    ecosystem: str,
    name: str,
    normalized: str,
    role: str,
    constraint: str | None = None,
    version: str | None = None,
    group: str | None = None,
    line: int = 1,
    parser: str = "planted",
) -> None:
    """Add one dependency row the fixture does not naturally contain."""
    repo_id, file_id = _ids(workspace, slug, rel_path)
    workspace.database.execute(
        "INSERT INTO dependencies (repo_id, file_id, ecosystem, name, normalized_name, role, "
        "constraint_text, version, group_name, line_start, line_end, parser) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            repo_id,
            file_id,
            ecosystem,
            name,
            normalized,
            role,
            constraint,
            version,
            group,
            line,
            line,
            parser,
        ),
    )
    workspace.database.connection.commit()


# ---------------------------------------------------------------------------
# CASE 1 / CASE 2 — what a file says, and what it does not
# ---------------------------------------------------------------------------


def test_a_manifest_declaration_comes_back_exactly_as_written(
    initialized: Workspace,
) -> None:
    """CASE 1. The constraint is the parsed string, not an interpreted range."""
    package = _one(_ask(initialized, name="requests", ecosystem=Ecosystem.PYPI), COMMON, "requests")

    declarations = [o for o in package.observations if o.role is DependencyRole.DECLARED]
    assert len(declarations) == 1
    assert declarations[0].value == ">=2.31"
    assert declarations[0].declared_constraint == ">=2.31"
    assert declarations[0].provenance.path == "pyproject.toml"
    assert declarations[0].provenance.line_start == 4
    assert declarations[0].provenance.parser == "pyproject"


def test_a_lock_observation_stays_distinct_from_the_declaration(
    initialized: Workspace,
) -> None:
    """CASE 2, the concrete one.

    ``python-service`` asks for ``common-lib>=1.0`` in a manifest and its lockfile
    records ``1.4.0``. Two files, two statements, two rows. A response that showed
    one version would have had to pick, and the constraint is not a version.
    """
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )

    declared = [o for o in package.observations if o.role is DependencyRole.DECLARED]
    resolved = [o for o in package.observations if o.role is DependencyRole.RESOLVED]
    assert len(declared) == 1 and len(resolved) == 1

    assert declared[0].value == ">=1.0"
    assert declared[0].declared_constraint == ">=1.0"
    assert declared[0].locked_version is None, "a constraint was read as a version"
    assert declared[0].provenance.path == "pyproject.toml"

    assert resolved[0].value == "1.4.0"
    assert resolved[0].locked_version == "1.4.0"
    assert resolved[0].declared_constraint is None, "a version was read as a constraint"
    assert resolved[0].provenance.path == "uv.lock"


def test_no_field_claims_a_package_is_installed(initialized: Workspace) -> None:
    """RepoAtlas reads files. The wire vocabulary must not suggest otherwise."""
    from repoatlas.models.dependency_inventory import DependencyObservationView

    fields = set(DependencyObservationView.model_fields) | {
        "declared_constraint",
        "locked_version",
        "provided_version",
    }
    for banned in ("installed_version", "resolved_version", "active_version", "current_version"):
        assert banned not in fields, banned

    payload = _ask(initialized).model_dump_json()
    for banned in ("installed", "vulnerab", "latest_version", "outdated_version"):
        assert banned not in payload, banned


# ---------------------------------------------------------------------------
# CASE 3 / CASE 4 — nothing is collapsed
# ---------------------------------------------------------------------------


def test_two_constraints_for_one_package_are_two_observations(
    initialized: Workspace,
) -> None:
    """CASE 3. A second manifest asking for a different version loses nothing.

    The destructive implementation is easy to write and hard to see: group by
    package, assign ``constraint`` in a loop, and whichever row came last wins
    while the others vanish without trace.
    """
    _plant(
        initialized,
        slug=COMMON,
        rel_path="pyproject.toml",
        ecosystem="pypi",
        name="requests",
        normalized="requests",
        role="declared",
        constraint="<3",
        group="main",
        line=5,
    )
    package = _one(_ask(initialized, name="requests", ecosystem=Ecosystem.PYPI), COMMON, "requests")

    assert sorted(v or "" for v in _values(package, DependencyRole.DECLARED)) == ["<3", ">=2.31"]
    assert len([o for o in package.observations if o.role is DependencyRole.DECLARED]) == 2


def test_one_package_in_two_repositories_stays_two_packages(
    initialized: Workspace,
) -> None:
    """CASE 4. ``left-pad`` is declared in one repository and locked in another.

    Merging them would produce a repository-less package with a constraint from
    one place and a version from another — a fact about no repository at all.
    """
    response = _ask(initialized, name="left-pad", ecosystem=Ecosystem.NPM)

    slugs = {package.repository_slug for package in response.packages}
    assert slugs == {COMMON, TS}

    assert _values(_one(response, COMMON, "left-pad"), DependencyRole.DECLARED) == ["^1.3.0"]
    assert _values(_one(response, COMMON, "left-pad"), DependencyRole.RESOLVED) == []
    assert _values(_one(response, TS, "left-pad"), DependencyRole.RESOLVED) == ["1.3.0"]
    assert _values(_one(response, TS, "left-pad"), DependencyRole.DECLARED) == []


def test_a_repository_scope_returns_only_that_repository(initialized: Workspace) -> None:
    """CASE 4. Scope narrows the answer; it never merges across repositories."""
    response = _service(initialized).dependencies(
        DependenciesRequest(context=_repo(TS), name="left-pad", limit=MAX_PAGE_SIZE)
    )

    assert {package.repository_slug for package in response.packages} == {TS}
    assert response.context.repositories == (TS,)


def test_scope_does_not_follow_a_package_to_another_repository(
    initialized: Workspace,
) -> None:
    """The same package existing elsewhere is not a reason to leave the scope."""
    scoped = _service(initialized).dependencies(
        DependenciesRequest(context=_repo(COMMON), limit=MAX_PAGE_SIZE)
    )
    assert {package.repository_slug for package in scoped.packages} == {COMMON}

    everywhere = {
        package.repository_slug for package in _ask(initialized, name="left-pad").packages
    }
    assert len(everywhere) > 1, "the fixture no longer proves the boundary"


def test_there_is_no_winning_version_anywhere_in_the_model(
    initialized: Workspace,
) -> None:
    """The single field this whole design exists to not have."""
    fields = set(DependencyPackageView.model_fields)
    for banned in ("version", "constraint", "best_version", "effective_version", "chosen"):
        assert banned not in fields, banned


# ---------------------------------------------------------------------------
# CASE 5 — ecosystems are part of identity
# ---------------------------------------------------------------------------


def test_one_name_in_two_ecosystems_is_two_packages(initialized: Workspace) -> None:
    """CASE 5. An npm ``requests`` and a PyPI ``requests`` are unrelated.

    They normalize to the same string, which is exactly why the ecosystem has to
    be part of the identity rather than a label beside it.
    """
    _plant(
        initialized,
        slug=COMMON,
        rel_path="package.json",
        ecosystem="npm",
        name="requests",
        normalized="requests",
        role="declared",
        constraint="^1.0.0",
        group="main",
        line=6,
    )
    response = _ask(initialized, name="requests")

    same_repo = [p for p in response.packages if p.repository_slug == COMMON]
    assert {p.ecosystem for p in same_repo} == {"npm", "pypi"}
    assert len(same_repo) == 2

    npm = next(p for p in same_repo if p.ecosystem == "npm")
    pypi = next(p for p in same_repo if p.ecosystem == "pypi")
    assert _values(npm, DependencyRole.DECLARED) == ["^1.0.0"]
    assert _values(pypi, DependencyRole.DECLARED) == [">=2.31"]


def test_an_ecosystem_filter_selects_one_of_them(initialized: Workspace) -> None:
    _plant(
        initialized,
        slug=COMMON,
        rel_path="package.json",
        ecosystem="npm",
        name="requests",
        normalized="requests",
        role="declared",
        constraint="^1.0.0",
        line=6,
    )
    npm_only = _ask(initialized, name="requests", ecosystem=Ecosystem.NPM)
    assert {p.ecosystem for p in npm_only.packages} == {"npm"}


def test_the_ecosystem_enum_is_exactly_what_the_parsers_produce() -> None:
    """A closed set that drifts from the parsers makes a real ecosystem unaskable."""
    assert {str(member) for member in Ecosystem} == set(DependencyRegistry().ecosystems)


# ---------------------------------------------------------------------------
# Name filtering
# ---------------------------------------------------------------------------


def test_a_name_is_matched_under_the_ecosystems_own_rule(initialized: Workspace) -> None:
    """PEP 503 folds ``_``, ``-`` and ``.`` together; npm does not.

    ``common_lib`` and ``common-lib`` are one PyPI distribution, so both spellings
    must find it. Reusing the ecosystem's rule is what makes that exact rather
    than lenient.
    """
    for spelling in ("common-lib", "common_lib", "Common.Lib"):
        response = _ask(initialized, name=spelling, ecosystem=Ecosystem.PYPI)
        assert {p.normalized_name for p in response.packages} == {"common-lib"}, spelling


def test_a_name_is_never_matched_as_a_substring(initialized: Workspace) -> None:
    """This is inventory, not search. ``req`` is not ``requests``."""
    for fragment in ("req", "quests", "eft-pa", "common"):
        assert _ask(initialized, name=fragment).packages == (), fragment


def test_a_blank_name_is_malformed(initialized: Workspace) -> None:
    with pytest.raises(ValueError):
        DependenciesRequest(context=GLOBAL, name="   ")


def test_a_name_matching_nothing_is_a_successful_empty_answer(
    initialized: Workspace,
) -> None:
    """CASE 20. Absence is an answer about the inventory, not a failure."""
    response = _ask(initialized, name="no-such-package-anywhere")

    assert response.packages == ()
    assert response.evidence == ()
    assert response.has_more is False
    assert response.next_cursor is None
    assert response.investigation_id


# ---------------------------------------------------------------------------
# CASE 6 — roles, and the group they are not
# ---------------------------------------------------------------------------


def test_a_role_filter_selects_exactly_that_kind(initialized: Workspace) -> None:
    """CASE 6. Deterministic selection, and no reclassification."""
    response = _ask(initialized, roles=(DependencyRole.RESOLVED,))

    assert response.packages
    for package in response.packages:
        assert {o.role for o in package.observations} == {DependencyRole.RESOLVED}
        assert all(o.locked_version is not None for o in package.observations)


def test_role_filtering_narrows_and_never_reclassifies(initialized: Workspace) -> None:
    everything = _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI)
    declared = _ask(
        initialized,
        name="common-lib",
        ecosystem=Ecosystem.PYPI,
        roles=(DependencyRole.DECLARED,),
    )

    wide = {
        (p.repository_slug, o.role, o.value) for p in everything.packages for o in p.observations
    }
    narrow = {
        (p.repository_slug, o.role, o.value) for p in declared.packages for o in p.observations
    }
    assert narrow == {item for item in wide if item[1] is DependencyRole.DECLARED}


def test_a_package_with_nothing_left_after_filtering_is_absent(
    initialized: Workspace,
) -> None:
    """Returning it empty would claim RepoAtlas knows a package it was not asked about."""
    response = _ask(
        initialized, name="left-pad", ecosystem=Ecosystem.NPM, roles=(DependencyRole.RESOLVED,)
    )
    assert {p.repository_slug for p in response.packages} == {TS}


def test_the_manifest_group_is_not_the_role(initialized: Workspace) -> None:
    """``development,test`` is what the Gemfile says; the role is still a declaration.

    V1 records the manifest's own grouping separately from what the observation
    asserts, and conflating them would turn "asked for, in the dev group" into a
    fourth kind of statement that no file makes.
    """
    package = _one(_ask(initialized, name="rspec", ecosystem=Ecosystem.GEM), COMMON, "rspec")

    declared = next(o for o in package.observations if o.role is DependencyRole.DECLARED)
    assert declared.group == "development,test"
    assert declared.role is DependencyRole.DECLARED

    resolved = next(o for o in package.observations if o.role is DependencyRole.RESOLVED)
    assert resolved.group is None, "a lockfile row was given a group it never stated"


def test_roles_are_exactly_what_the_substrate_records() -> None:
    """Not runtime/dev/optional. V1's roles say declaration versus resolution."""
    assert {str(role) for role in DependencyRole} == {"declared", "resolved", "provided"}


def test_an_unknown_role_is_structurally_refused(initialized: Workspace) -> None:
    with pytest.raises(ValueError):
        DependenciesRequest(context=GLOBAL, roles=("runtime",))  # type: ignore[arg-type]


def test_a_repeated_role_is_refused(initialized: Workspace) -> None:
    with pytest.raises(InvalidDependencyRequestError) as raised:
        _ask(initialized, roles=(DependencyRole.DECLARED, DependencyRole.DECLARED))
    assert raised.value.field == "roles"


# ---------------------------------------------------------------------------
# CASE 13 — deterministic ordering
# ---------------------------------------------------------------------------


def test_the_order_is_the_documented_one(initialized: Workspace) -> None:
    """§13: repository slug, then ecosystem, then normalized name."""
    response = _ask(initialized)
    keys = [(p.repository_slug, p.ecosystem, p.normalized_name) for p in response.packages]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_the_answer_does_not_depend_on_insertion_order(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 13. An index built in a different file order answers identically.

    SQLite hands back rows in whatever order the table filled up. If that decided
    which packages a caller saw, two indexes built from the same managed root
    would disagree and neither would be wrong.
    """
    from repoatlas.storage.sqlite.stores import SqliteDependencyStore

    baseline = _ask(initialized).model_dump_json(
        exclude={"investigation_id", "ledger", "freshness", "evidence"}
    )
    real = SqliteDependencyStore.list_for_repos

    def reversed_rows(self: SqliteDependencyStore, repo_ids: object = None) -> object:
        return list(reversed(real(self, repo_ids)))  # type: ignore[arg-type]

    monkeypatch.setattr(SqliteDependencyStore, "list_for_repos", reversed_rows)
    assert (
        _ask(initialized).model_dump_json(
            exclude={"investigation_id", "ledger", "freshness", "evidence"}
        )
        == baseline
    )


def test_observations_within_a_package_are_ordered_deterministically(
    initialized: Workspace,
) -> None:
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )
    keys = [
        (o.scope, str(o.role), o.provenance.path, o.provenance.line_start, o.value or "")
        for o in package.observations
    ]
    assert keys == sorted(keys)


def test_a_second_identical_call_returns_an_identical_answer(
    initialized: Workspace,
) -> None:
    service = _service(initialized)

    def once() -> str:
        response = service.dependencies(DependenciesRequest(context=GLOBAL, limit=MAX_PAGE_SIZE))
        return response.model_dump_json(exclude={"investigation_id", "ledger", "freshness"})

    assert once() == once()


# ---------------------------------------------------------------------------
# CASE 19 — freshness
# ---------------------------------------------------------------------------


def test_a_stored_depth_request_runs_no_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 19. Stored freshness answers from the index alone."""
    import subprocess

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("dependencies shelled out at STORED depth")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    response = _ask(initialized, depth=FreshnessDepth.STORED)
    assert response.freshness.depth is FreshnessDepth.STORED
    assert response.packages


def test_a_stale_index_still_answers(initialized: Workspace, workspace_root: Path) -> None:
    """A declaration stays a fact about the snapshot that recorded it.

    Suppressing the inventory because the tree moved on would answer a question
    about freshness with silence about dependencies.
    """
    (workspace_root / SERVICE / "pyproject.toml").write_text(
        '[project]\nname = "python-service"\n', encoding="utf-8"
    )
    response = _ask(initialized)
    assert response.packages
    assert any(p.repository_slug == SERVICE for p in response.packages)


# ---------------------------------------------------------------------------
# §31 — public schema hygiene
# ---------------------------------------------------------------------------


def test_no_storage_identifier_reaches_the_response(initialized: Workspace) -> None:
    """Row ids are allocation order; a reindex renumbers every one of them."""
    response = _ask(initialized)
    payload = json.loads(response.model_dump_json())

    def field_names(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(str(key))
                found |= field_names(item)
        elif isinstance(value, list):
            for item in value:
                found |= field_names(item)
        return found

    names = field_names(payload)
    for banned in (
        "dependency_id",
        "file_id",
        "repo_id",
        "repository_id",
        "entity_id",
        "edge_id",
        "chunk_id",
        "rowid",
        "score",
        "distance",
    ):
        assert banned not in names, banned


def test_no_host_absolute_path_appears_anywhere(initialized: Workspace) -> None:
    response = _ask(initialized)
    payload = response.model_dump_json()
    assert str(Path(initialized.database.path).parent) not in payload
    for package in response.packages:
        for observation in package.observations:
            assert not observation.provenance.path.startswith("/")


def test_the_page_limit_is_clamped_to_the_server_ceiling(initialized: Workspace) -> None:
    response = _ask(initialized, limit=MAX_PAGE_SIZE * 10)
    assert response.limits.applied == MAX_PAGE_SIZE
    assert response.limits.clamped == ("limit",)
    assert response.limits.requested == MAX_PAGE_SIZE * 10


def test_a_non_positive_limit_is_malformed(initialized: Workspace) -> None:
    with pytest.raises(ValueError):
        DependenciesRequest(context=GLOBAL, limit=0)


# ---------------------------------------------------------------------------
# §26 — read-only proof
# ---------------------------------------------------------------------------


def test_dependencies_changes_no_indexed_fact(initialized: Workspace) -> None:
    before = fact_snapshot(initialized)

    service = _service(initialized)
    for kwargs in (
        {},
        {"name": "common-lib", "ecosystem": Ecosystem.PYPI},
        {"roles": (DependencyRole.RESOLVED,)},
        {"include_providers": False},
        {"include_provenance": False},
    ):
        service.dependencies(
            DependenciesRequest(context=GLOBAL, limit=MAX_PAGE_SIZE, **kwargs)  # type: ignore[arg-type]
        )

    assert fact_snapshot(initialized) == before


def test_dependencies_makes_no_write_at_all(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write boundary is fatal for the duration of one call."""
    from repoatlas.storage.sqlite.database import Database

    real_execute = Database.execute
    written: list[str] = []

    def watched(self: Database, sql: str, *args: object, **kwargs: object) -> object:
        head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            written.append(sql.strip()[:60])
        return real_execute(self, sql, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Database, "execute", watched)
    _ask(initialized)

    assert written == []


def test_dependencies_never_syncs_or_takes_the_writer_lock(initialized: Workspace) -> None:
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    body = source[source.index("def dependencies(") : source.index("# -- dependencies internals")]
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "writer_lock" not in code
    assert "sync_engine" not in code
    assert "sync(" not in code


def test_dependencies_runs_no_retrieval_stage(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inventory question is answered from the inventory, not from the funnel."""
    from repoatlas.retrieval.retriever import Retriever
    from repoatlas.storage.sqlite.search import SqliteSearchIndex

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("dependencies reached retrieval")

    monkeypatch.setattr(Retriever, "retrieve", forbidden)
    monkeypatch.setattr(Retriever, "retrieve_detailed", forbidden)
    stages = ("find_literal", "find_files_by_path", "find_entities_by_name", "search_entities")
    for name in stages:
        monkeypatch.setattr(SqliteSearchIndex, name, forbidden)

    assert _ask(initialized).packages


def test_dependencies_walks_no_graph(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider facts live in the dependency substrate, not in the edge table."""
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("dependencies expanded the graph")

    monkeypatch.setattr(SqliteKnowledgeStore, "expand_neighbors", forbidden)
    monkeypatch.setattr(SqliteKnowledgeStore, "neighbors", forbidden)

    response = _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI)
    assert any(package.providers for package in response.packages)


def test_dependencies_consults_nothing_outside_this_machine(initialized: Workspace) -> None:
    """§25. No registry, no package manager, no environment probing."""
    source = Path("repoatlas/capabilities/dependencies.py").read_text(encoding="utf-8")
    service = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    body = service[service.index("def dependencies(") : service.index("    # -- internals ---")]
    for banned in ("urllib", "requests.", "httpx", "socket", "subprocess", "importlib.metadata"):
        assert banned not in source, banned
        assert banned not in body, banned
