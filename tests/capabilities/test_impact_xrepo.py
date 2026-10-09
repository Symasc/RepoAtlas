"""Change impact across repositories: exact, evidenced, and continued on the far side (XR).

A change to a declaration a package exports reaches the declarations that use it in
every repository that depends on that package -- through the ``USES`` edge a package
link proved, and through nothing else. Once across, the walk goes on in the consumer's
repository exactly as if it had started there. A repository scope is a boundary it
never crosses, and an edge that joins two repositories without recording the evidence
for it is never followed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.impact import FAMILIES
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactCandidate, ImpactRequest, ImpactResponse, ImpactRole
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import HTTP_API, HTTP_WEB, build, verify_steps
from tests.xrepo_corpus import LINKING

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

COURSE_ROLE = "sym:contracts:src/roles.ts:CourseRole:1"
CAN_EDIT = "sym:app:src/policy.ts:canEdit:2"
GUARD = "sym:app:src/guard.ts:guard:2"
GUARD_TEST = "file:app:src/__tests__/guard.test.ts"


def _open(root: Path) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    return build(workspace, root, {**LINKING, "http-api": HTTP_API, "http-web": HTTP_WEB})


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = _open(tmp_path_factory.mktemp("xrepo-impact"))
    yield workspace
    workspace.close()


def impact(
    workspace: Workspace, seed: str, context: CapabilityContext = GLOBAL, **fields: object
) -> ImpactResponse:
    return CapabilityService(workspace).impact(
        ImpactRequest(context=context, seed=seed, **fields)  # type: ignore[arg-type]
    )


def by_entity(response: ImpactResponse) -> dict[str, ImpactCandidate]:
    return {c.entity: c for c in (*response.candidates, *response.tests)}


def test_xr1_a_change_reaches_the_declaration_that_uses_it_in_another_repository(
    indexed: Workspace,
) -> None:
    response = impact(indexed, COURSE_ROLE)
    candidates = {c.entity: c for c in response.candidates}
    consumer = candidates[CAN_EDIT]
    assert (consumer.repository, consumer.distance, consumer.from_) == ("app", 1, 0)
    assert (consumer.step.edge, consumer.step.at, consumer.basis) == (
        "USES",
        "app/src/policy.ts",
        "exact",
    )
    # every step, the crossing included, is an indexed edge at the lines it names
    assert verify_steps(indexed, response) == len(response.candidates)
    # the other exact consumers of the export, each in its own repository
    assert {"sym:app:src/spaces.ts:staticMember:2", "sym:refusals:ok/use.ts:ok:2"} <= set(
        candidates
    )
    # a use of the whole module is not a use of this declaration
    assert not any("computedMember" in key or "coarse-user" in key for key in candidates)


def test_xr8_the_walk_goes_on_inside_the_consumer(indexed: Workspace) -> None:
    response = impact(indexed, COURSE_ROLE)
    found = by_entity(response)
    consumer, caller = found[CAN_EDIT], found[GUARD]
    assert (caller.repository, caller.distance, caller.from_) == ("app", 2, consumer.id)
    [test] = [t for t in response.tests if t.entity == GUARD_TEST]
    assert (test.distance, test.from_) == (3, caller.id)


def test_n5_a_repository_scope_is_a_boundary_the_walk_never_crosses(indexed: Workspace) -> None:
    only = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="contracts")
    response = impact(indexed, COURSE_ROLE, only)
    assert response.seed.status == "resolved"
    assert {c.repository for c in (*response.candidates, *response.tests)} <= {"contracts"}


def test_n6_an_edge_joining_repositories_without_its_evidence_is_never_followed(
    tmp_path: Path,
) -> None:
    workspace = _open(tmp_path)
    try:
        database = workspace.database
        planted = "sym:right:src/local.ts:rightUse:2"
        [(src, repo_id, file_id)] = database.execute(
            "SELECT id, repo_id, file_id FROM entities WHERE key = ?", (planted,)
        ).fetchall()
        [(dst,)] = database.execute(
            "SELECT id FROM entities WHERE key = ?", (COURSE_ROLE,)
        ).fetchall()
        # a use that names its package but not the evidence that crosses to it
        resolution = json.dumps({"basis": "package", "use": "value"}, sort_keys=True)
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO edges (src_id, dst_id, type, confidence, repo_id, file_id,"
                " line_start, line_end, parser, resolution)"
                " VALUES (?, ?, 'USES', 1.0, ?, ?, 2, 2, 'linker:global', ?)",
                (src, dst, repo_id, file_id, resolution),
            )
        response = impact(workspace, COURSE_ROLE)
        assert planted not in by_entity(response)
        assert CAN_EDIT in by_entity(response)  # the evidenced crossing still is
    finally:
        workspace.close()


# -- the crossing family, in its stored direction --------------------------------------

#: Each family that crosses on evidence: (seed, reached, the stored edge).
CROSSING_CASES = {
    "uses": (COURSE_ROLE, CAN_EDIT, ("USES", CAN_EDIT, COURSE_ROLE)),
    "http": (
        "sym:http-api:app.py:get_item:9",
        "sym:http-web:client.py:load_item:4",
        (
            "CALLS_HTTP_HANDLER",
            "sym:http-web:client.py:load_item:4",
            "sym:http-api:app.py:get_item:9",
        ),
    ),
}


def test_every_family_that_crosses_has_a_crossing_case() -> None:
    assert {f.name for f in FAMILIES if f.crosses is not None} == set(CROSSING_CASES)


@pytest.mark.parametrize("family", sorted(CROSSING_CASES))
def test_a_crossing_family_travels_in_its_stored_direction(indexed: Workspace, family: str) -> None:
    seed, reached, (edge_type, source, target) = CROSSING_CASES[family]
    [stored] = indexed.database.execute(
        """
        SELECT COUNT(*) FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        WHERE e.type = ? AND s.key = ? AND d.key = ?
        """,
        (edge_type, source, target),
    ).fetchall()
    assert stored[0] > 0
    forward = by_entity(impact(indexed, seed))
    assert forward[reached].role is ImpactRole.DEPENDENT
    # the dependent's own change does not flow back to what it uses
    assert seed not in by_entity(impact(indexed, reached))


def test_a_whole_module_use_across_repositories_follows_only_a_whole_file_change(
    indexed: Workspace,
) -> None:
    whole = {"sym:app:src/spaces.ts:computedMember:5", "sym:coarse-user:src/computed.ts:pick:2"}
    # the entry file changes whole: every use of its whole namespace depends on it
    assert whole <= set(by_entity(impact(indexed, "file:contracts:src/index.ts")))
    # one declaration it re-exports changes: a computed member names no declaration
    assert not whole & set(by_entity(impact(indexed, COURSE_ROLE)))


# -- what the answer says about a crossing -----------------------------------------------


def verify_crossing(workspace: Workspace, candidate: ImpactCandidate) -> None:
    """Every part of a crossing names a fact the index holds, at the line it gives."""
    crossing = candidate.step.crossing
    assert crossing is not None

    def dependency(role: str, spelled: str) -> None:
        located, _, line = spelled.rpartition(":")
        repository, _, manifest = located.partition("/")
        [(count,)] = workspace.database.execute(
            """
            SELECT COUNT(*) FROM dependencies d
            JOIN files f ON f.id = d.file_id JOIN repositories r ON r.id = d.repo_id
            WHERE d.role = ? AND r.slug = ? AND f.rel_path = ? AND d.line_start = ?
              AND 'npm:' || d.name = ?
            """,
            (role, repository, manifest, int(line), crossing.package),
        ).fetchall()
        assert count == 1, (role, spelled)

    dependency("provided", crossing.provided_by)
    dependency("declared", crossing.declared_by)
    repository, _, entry = crossing.entry.partition("/")
    assert (repository, crossing.to_repository) == (
        crossing.from_repository,
        candidate.repository,
    )
    [(files,)] = workspace.database.execute(
        "SELECT COUNT(*) FROM files f JOIN repositories r ON r.id = f.repo_id"
        " WHERE r.slug = ? AND f.rel_path = ?",
        (repository, entry),
    ).fetchall()
    assert files == 1
    if crossing.imported_at is not None:
        located, _, line = crossing.imported_at.rpartition(":")
        repository, _, path = located.partition("/")
        root = Path(workspace.settings.repos.root or "") / repository
        text = (root / path).read_text().splitlines()[int(line) - 1]
        assert text.startswith("import") and (crossing.local or "") in text


def test_xr1_the_answer_says_how_the_change_crossed_and_where_to_read_it(
    indexed: Workspace,
) -> None:
    response = impact(indexed, COURSE_ROLE)
    consumer = {c.entity: c for c in response.candidates}[CAN_EDIT]
    crossing = consumer.step.crossing
    assert crossing is not None
    assert crossing.model_dump(exclude_none=True) == {
        "package": "npm:@fixture/contracts",
        "from_repository": "contracts",
        "to_repository": "app",
        "provided_by": "contracts/package.json:2",
        "entry": "contracts/src/index.ts",
        "entry_fields": "main",
        "declared_by": "app/package.json:5",
        "constraint": "^1.0.0",
        "imported_at": "app/src/policy.ts:1",
        "declared_as": "CourseRole",
        "imported_as": "CourseRole",
        "local": "CourseRole",
    }
    assert consumer.step.via == ("contracts/src/index.ts",)
    assert consumer.boundaries == ("contracts:. -> app:.",)
    assert (
        consumer.chain
        == "CourseRole -> canEdit (uses, contracts -> app via npm:@fixture/contracts)"
    )
    for candidate in response.candidates:
        if candidate.step.crossing is not None:
            verify_crossing(indexed, candidate)
    # the steps after the crossing are the consumer's own, and say nothing of packages
    assert all(
        c.step.crossing is None for c in (*response.candidates, *response.tests) if c.distance > 1
    )
    assert response.summary.policy == "impact/4"


def test_xr2_every_name_on_the_way_is_in_the_answer(indexed: Workspace) -> None:
    response = impact(indexed, "sym:contracts:src/internal.ts:InternalRole:1")
    [consumer] = response.candidates
    crossing = consumer.step.crossing
    assert crossing is not None
    # declared InternalRole; exported as Role by public.ts, then the barrel; bound LocalRole
    assert (
        crossing.declared_as,
        consumer.step.via_names,
        crossing.imported_as,
        crossing.local,
    ) == (
        "InternalRole",
        ("Role", "Role"),
        "Role",
        "LocalRole",
    )
    assert consumer.step.via == ("contracts/src/index.ts", "contracts/src/public.ts")
    verify_crossing(indexed, consumer)


def test_xr3_the_subpath_is_named_on_the_step_and_the_package(indexed: Workspace) -> None:
    response = impact(indexed, "sym:auth-kit:src/auth/index.ts:Role:1")
    [consumer] = response.candidates
    assert consumer.step.crossing is not None
    assert (consumer.step.crossing.subpath, consumer.step.crossing.entry) == (
        "auth",
        "auth-kit/src/auth/index.ts",
    )
    [package] = [p for p in response.packages if p.dependent == "app:."]
    assert (package.exact_consumers, package.consumers, package.subpaths) == (
        1,
        (consumer.id,),
        ("auth",),
    )
    # the root's own Role, a different declaration, is used by nobody
    assert impact(indexed, "sym:auth-kit:src/index.ts:Role:1").candidates == ()


def test_xr7_a_dependent_whose_consumer_cannot_be_named_stays_coarse(indexed: Workspace) -> None:
    response = impact(indexed, COURSE_ROLE)
    [coarse] = [p for p in response.packages if p.dependent == "coarse-user:."]
    assert (coarse.granularity, coarse.exact_consumers, coarse.consumers) == (
        "package",
        "unresolved",
        (),
    )
    assert coarse.unresolved_consumers == 1  # pick: a computed member
    assert not any(c.repository == "coarse-user" for c in response.candidates)
    [exact] = [p for p in response.packages if p.dependent == "app:."]
    # canEdit and staticMember cross; guard is reached after them, not through the package
    assert exact.exact_consumers == 2
    assert {c.entity for c in response.candidates if c.id in exact.consumers} == {
        CAN_EDIT,
        "sym:app:src/spaces.ts:staticMember:2",
    }
    assert exact.unresolved_consumers == 1  # computedMember


def test_xr7b_an_exact_consumer_is_credited_only_to_the_package_it_crossed(
    indexed: Workspace,
) -> None:
    response = impact(indexed, "sym:mono-kit:packages/p/src/index.ts:X:1")
    g = {c.entity: c for c in response.candidates}["sym:kit-user:src/g.ts:g:2"]
    by_package = {(p.package, p.dependent): p for p in response.packages}
    through_q = by_package[("npm:@kit/q", "kit-user:.")]
    assert (through_q.exact_consumers, through_q.consumers) == (1, (g.id,))
    # the seed's own package is declared by kit-user too, but nothing crossed through it
    through_p = by_package[("npm:@kit/p", "kit-user:.")]
    assert (through_p.exact_consumers, through_p.consumers) == ("unresolved", ())
    # every consumer a package entry names has a path through that very package
    for package in response.packages:
        for consumer in package.consumers:
            [candidate] = [c for c in response.candidates if c.id == consumer]
            assert candidate.step.crossing is not None
            assert (candidate.step.crossing.package, candidate.step.crossing.declared_by) == (
                package.package,
                package.declared_at,
            )


def test_n5_a_scoped_answer_says_the_dependents_are_outside_it(indexed: Workspace) -> None:
    only = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="contracts")
    response = impact(indexed, COURSE_ROLE, only)
    across = [p for p in response.packages if p.crosses_repository]
    assert across and {p.exact_consumers for p in across} == {"outside scope"}


def test_a_dependent_that_is_not_the_provider_s_is_not_listed(indexed: Workspace) -> None:
    listed = {p.dependent for p in impact(indexed, COURSE_ROLE).packages}
    # an npm: alias and a workspace protocol declare some other package under the name
    assert not {"refusals:aliased", "refusals:workspace"} & listed
    # a repository providing the name itself: which provider it uses is not ours to pick
    shadowed = impact(indexed, "sym:shadow-source:src/index.ts:shade:1")
    assert not any(p.dependent.startswith("self-provider") for p in shadowed.packages)


def test_a_dependent_that_uses_none_of_the_change_claims_no_exact_consumer(
    indexed: Workspace,
) -> None:
    response = impact(indexed, "sym:mono-kit:packages/p/src/index.ts:Y:2")
    assert response.candidates == ()
    by_dependent = {p.dependent: p for p in response.packages}
    # a unit of the same repository declares the package, and nothing crossed from Y
    assert by_dependent["mono-kit:packages/q"].exact_consumers == "unresolved"
    # another repository declares it too: coarse, and says so
    assert by_dependent["kit-user:."].exact_consumers == "unresolved"
    assert by_dependent["kit-user:."].consumers == ()
