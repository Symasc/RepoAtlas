"""topology/references: what each line of one file declares and refers to.

A reference is a located, non-containment edge the file asserts, with the entity it
resolved to. Checked against the index read directly: every such edge appears once,
on its own lines, and one resolved into another repository only when crossing was
asked for.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.errors import InvalidTopologyRequestError, PathNotFoundError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.topology import (
    EntryKind,
    TopologyChildrenRequest,
    TopologyReferencesRequest,
    TopologyReferencesResponse,
)
from repoatlas.services.workspace import Workspace
from tests.topology_corpus import GLOBAL, LINKED, STRUCTURAL_TYPES, open_corpus, repository

COURSE_ROLE = "sym:contracts:src/roles.ts:CourseRole:1"
CAN_EDIT = "sym:app:src/policy.ts:canEdit:2"

#: One function called twice on one line: the parser records the call twice.
TWICE = {
    "package.json": '{"name": "twice", "version": "1.0.0"}\n',
    "src/local.ts": (
        "function one() {\n"
        "  return 1;\n"
        "}\n"
        "export function both() {\n"
        "  return one() + one();\n"  # 5
        "}\n"
    ),
}


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_corpus(
        tmp_path_factory.mktemp("topology-references"), {**LINKED, "twice": TWICE}
    )
    yield workspace
    workspace.close()


def references(
    workspace: Workspace, slug: str, path: str, **fields: object
) -> TopologyReferencesResponse:
    request = TopologyReferencesRequest(context=repository(slug), path=path, **fields)  # type: ignore[arg-type]
    return CapabilityService(workspace).topology_references(request)


def asserted(
    workspace: Workspace, slug: str, path: str
) -> set[tuple[int, int, str, str, str, str]]:
    """Located non-containment edges the file asserts, with their target's repository."""
    rows = workspace.database.execute(
        """
        SELECT e.line_start, COALESCE(e.line_end, e.line_start), e.type, s.key, d.key,
               COALESCE(rd.slug, '')
        FROM edges e
        JOIN files f ON f.id = e.file_id JOIN repositories r ON r.id = f.repo_id
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        LEFT JOIN repositories rd ON rd.id = d.repo_id
        WHERE r.slug = ? AND f.rel_path = ? AND e.line_start IS NOT NULL
          AND e.type NOT IN (?, ?)
        """,
        (slug, path, *STRUCTURAL_TYPES),
    ).fetchall()
    return {tuple(row) for row in rows}  # type: ignore[misc]


def listed(answer: TopologyReferencesResponse) -> list[tuple[int, int, str, str, str, str]]:
    return [
        (
            r.line_start,
            r.line_end,
            str(r.type),
            r.source,
            r.target.key,
            r.target.repository_slug or "",
        )
        for r in answer.references
    ]


@pytest.mark.parametrize(
    ("slug", "path"),
    [
        ("app", "src/policy.ts"),
        ("app", "src/guard.ts"),
        ("app", "package.json"),
        ("contracts", "src/index.ts"),
        ("twice", "src/local.ts"),
    ],
)
def test_every_located_reference_appears_once_in_source_order(
    linked: Workspace, slug: str, path: str
) -> None:
    answer = references(linked, slug, path, cross_repository=True)
    rows = listed(answer)
    assert len(rows) == len(set(rows)), "a fact recorded twice is listed once"
    assert set(rows) == asserted(linked, slug, path)
    assert rows == sorted(rows, key=lambda r: (r[0], r[1], r[2], r[4], r[3]))
    assert answer.references_total == len(rows) > 0
    assert answer.other_repositories == 0
    assert answer.complete and not answer.truncated


def test_a_fact_recorded_twice_is_one_reference(linked: Workspace) -> None:
    rows = linked.database.execute(
        """
        SELECT COUNT(*) FROM edges e
        JOIN files f ON f.id = e.file_id JOIN repositories r ON r.id = f.repo_id
        WHERE r.slug = 'twice' AND f.rel_path = 'src/local.ts' AND e.type = 'CALLS'
        """
    ).fetchone()[0]
    assert rows >= 2, "the corpus records the call once per occurrence"
    calls = [r for r in references(linked, "twice", "src/local.ts").references if r.type == "CALLS"]
    assert [(r.line_start, r.target.name) for r in calls] == [(5, "one")]


def test_containment_is_never_a_reference_even_when_located(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    workspace = open_corpus(
        tmp_path_factory.mktemp("topology-references-located"), {"twice": TWICE}
    )
    try:
        with workspace.database.transaction() as connection:
            placed = connection.execute(
                """
                INSERT INTO edges (src_id, dst_id, type, confidence, repo_id, file_id,
                                   line_start, line_end, parser, resolution)
                SELECT e.src_id, e.dst_id, e.type, e.confidence, e.repo_id, f.id, 4, 6,
                       e.parser, e.resolution
                FROM edges e JOIN entities d ON d.id = e.dst_id JOIN files f ON f.id = d.file_id
                WHERE e.type IN ('DEFINES', 'CONTAINS') AND f.rel_path = 'src/local.ts'
                """
            ).rowcount
        assert placed > 0
        answer = references(workspace, "twice", "src/local.ts")
        assert not {r.type for r in answer.references} & {"DEFINES", "CONTAINS"}
    finally:
        workspace.close()


def test_a_reference_names_where_its_target_is_declared(linked: Workspace) -> None:
    answer = references(linked, "app", "src/policy.ts", cross_repository=True)
    uses = [r for r in answer.references if r.target.key == COURSE_ROLE]
    assert [(r.line_start, r.source) for r in uses] == [(2, CAN_EDIT), (3, CAN_EDIT)]
    target = uses[0].target
    assert (target.repository_slug, target.path, target.name, target.line_start) == (
        "contracts",
        "src/roles.ts",
        "CourseRole",
        1,
    )


def test_other_repositories_are_counted_unless_crossing_is_asked_for(linked: Workspace) -> None:
    confined = references(linked, "app", "src/policy.ts")
    crossing = references(linked, "app", "src/policy.ts", cross_repository=True)
    elsewhere = [r for r in crossing.references if r.target.repository_slug not in (None, "app")]
    assert elsewhere, "the corpus crosses into contracts here"
    assert confined.other_repositories == len(elsewhere)
    assert listed(confined) == [row for row in listed(crossing) if row[5] in ("", "app")]


def test_declarations_are_the_files_children(linked: Workspace) -> None:
    answer = references(linked, "app", "src/policy.ts")
    children = CapabilityService(linked).topology_children(
        TopologyChildrenRequest(context=repository("app"), path="src/policy.ts")
    )
    assert answer.declarations == children.entries
    assert answer.declarations
    assert all(d.kind is EntryKind.DECLARATION for d in answer.declarations)


def test_reading_references_opens_no_investigation(linked: Workspace) -> None:
    service = CapabilityService(linked)
    before = len(service.sessions)
    service.topology_references(
        TopologyReferencesRequest(context=repository("app"), path="src/policy.ts")
    )
    assert len(service.sessions) == before


def test_requests_outside_what_references_answer_are_refused(linked: Workspace) -> None:
    service = CapabilityService(linked)
    with pytest.raises(InvalidTopologyRequestError):
        service.topology_references(TopologyReferencesRequest(context=GLOBAL, path="src/policy.ts"))
    with pytest.raises(InvalidTopologyRequestError):
        references(linked, "app", "src")
    with pytest.raises(PathNotFoundError):
        references(linked, "app", "src/missing.ts")
    for path in ("", ".", "/src/policy.ts", "src/../policy.ts"):
        with pytest.raises(ValidationError):
            TopologyReferencesRequest(context=repository("app"), path=path)
