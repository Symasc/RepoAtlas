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
from tests.impact_corpus import build, verify_steps
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
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    return build(workspace, root, LINKING)


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
CROSSING_CASES = {"uses": (COURSE_ROLE, CAN_EDIT, ("USES", CAN_EDIT, COURSE_ROLE))}


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
