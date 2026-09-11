"""Guarantees that hold inside one slice and must survive between two.

Every slice arrived with its own mutation suite, and each one passed. The RC
audit found a trust defect anyway, in the place no slice owned: Slice 0 put
``split_total`` on :class:`~repoatlas.storage.protocols.ChunkHit` so that line
coverage could be proven, Slice E consumed it in
:func:`~repoatlas.capabilities.indexed_source.indexed_line_map`, and the
retrieval model between them dropped it. Both halves were correct in isolation.
The composition was not, and no test crossed the join.

So these tests are organized by *seam* rather than by capability. Each walks one
fact from the bytes on disk to what a caller may finally assert about it, and
checks the property that must hold the whole way down:

    RepoAtlas never licenses a citation to source text it did not supply.

A. a cut span, from indexing through search and the ledger to ``verify_grounding``;
B. a span the index cannot reconstruct, through relationship provenance;
C. a source the index cannot decode, through dependency observations.

B and C pin behaviour that is already correct. They are here because the join is
unowned, not because it is broken --- which is exactly what was true of A before
the audit.

The adversarial ecosystem is the corpus because it is the one that holds the
hostile shapes: a 200 KB single line, an undecodable file, a binary file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.indexed_source import indexed_line_map
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.dependency_inventory import MAX_PAGE_SIZE, DependenciesRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    VerifyGroundingRequest,
)
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"

#: ``python-service/longline.py`` is a single ~200 KB line. The chunker cannot
#: bound it by lines, so it is cut into pieces that each carry the line range
#: ``1-1`` --- the shape a minified bundle or a generated one-liner has in any
#: real repository.
LONG_LINE_FILE = "longline.py"
LONG_LINE_MARKER = "LONG_LINE_MARKER"


@pytest.fixture
def service(adversarial: Workspace) -> Iterator[CapabilityService]:
    svc = CapabilityService(adversarial)
    yield svc
    svc.close()


def _long_line_evidence(service: CapabilityService):
    response = service.search(SearchRequest(context=GLOBAL, question=LONG_LINE_MARKER))
    item = next((e for e in response.evidence if e.ref.path == LONG_LINE_FILE), None)
    assert item is not None, "the long-line fixture must be retrievable at all"
    return response.investigation_id, item


# ---------------------------------------------------------------------------
# Seam A --- a cut span: indexing -> retrieval -> shaping -> ledger -> grounding
# ---------------------------------------------------------------------------


def test_the_fixture_really_is_a_cut_span(adversarial: Workspace) -> None:
    """Otherwise the rest of seam A proves nothing.

    Asserted against the store rather than against the file, because what matters
    is the shape the index gave it.
    """
    rows = adversarial.database.execute(
        "SELECT c.split_total, COUNT(*) AS pieces FROM chunks c "
        "JOIN files f ON f.id = c.file_id WHERE f.rel_path = ? GROUP BY c.split_total",
        (LONG_LINE_FILE,),
    ).fetchall()
    assert rows, "the long-line fixture must be indexed"
    assert all(row["split_total"] > 1 for row in rows)
    assert all(row["pieces"] > 1 for row in rows)


def test_a_cut_span_supplies_its_text_and_claims_no_line(service: CapabilityService) -> None:
    """The fragment is still evidence. It is simply not evidence about a line."""
    _, item = _long_line_evidence(service)
    assert item.text, "the fragment is supplied --- withholding it would lose real evidence"
    assert item.ref.line_start is None
    assert item.ref.line_end is None


def test_the_supplied_fragment_is_a_fraction_of_the_line_it_came_from(
    service: CapabilityService, adversarial_root: Path
) -> None:
    """The quantity that makes the old claim a fabrication rather than a rounding."""
    _, item = _long_line_evidence(service)
    source = (adversarial_root / SERVICE / LONG_LINE_FILE).read_text(encoding="utf-8")
    whole_line = source.split("\n")[0]
    assert len(item.text) < len(whole_line) / 2
    assert whole_line[-40:] not in item.text, "the tail of the line was never supplied"


def test_the_location_string_cannot_advertise_a_line_either(service: CapabilityService) -> None:
    """``location`` is derived from ``ref``, so the correction must reach it.

    A caller reading ``python-service/longline.py:1`` has been told a line was
    supplied, whatever the structured fields say.
    """
    _, item = _long_line_evidence(service)
    assert item.location.endswith(LONG_LINE_FILE)
    assert ":" not in item.location.rsplit("/", 1)[-1]


def test_a_citation_about_the_unsupplied_tail_cannot_ground(service: CapabilityService) -> None:
    """The whole seam, and the reason it exists.

    Before the correction this verdict was ``grounded``: the evidence claimed
    line 1, the citation named line 1, and the overlap test had no way to know
    that only a fraction of line 1 was ever handed over.
    """
    investigation, item = _long_line_evidence(service)
    answer = AgentAnswer(
        summary="bundle contents",
        claims=(
            AgentClaim(
                statement=f"{LONG_LINE_FILE} line 1 ends with a production credential",
                claimed_confidence=0.9,
                citations=(AgentCitation(evidence_id=item.id, line_start=1, line_end=1),),
            ),
        ),
    )
    verdict = service.verify_grounding(
        VerifyGroundingRequest(investigation_id=investigation, answer=answer)
    )
    assert verdict.verdict == "ungrounded"
    assert verdict.dropped_claims == 1
    assert verdict.dropped_citations == 1


def test_citing_the_file_without_naming_a_line_still_works(service: CapabilityService) -> None:
    """The correction narrows the claim, not the evidence.

    The agent genuinely was shown text from this file, so a statement about the
    file that cites it is grounded. Refusing that too would have traded one false
    answer for another.
    """
    investigation, item = _long_line_evidence(service)
    answer = AgentAnswer(
        summary="bundle contents",
        claims=(
            AgentClaim(
                statement=f"{LONG_LINE_FILE} contains a single very long line",
                claimed_confidence=0.6,
                citations=(AgentCitation(evidence_id=item.id),),
            ),
        ),
    )
    verdict = service.verify_grounding(
        VerifyGroundingRequest(investigation_id=investigation, answer=answer)
    )
    assert verdict.verdict == "grounded"
    assert verdict.dropped_claims == 0


def test_re_supplying_the_same_fragment_reuses_one_evidence_id(
    service: CapabilityService,
) -> None:
    """Ledger identity is over the corrected reference, and is stable under it."""
    investigation, first = _long_line_evidence(service)
    second = service.search(
        SearchRequest(
            context=GLOBAL,
            question=LONG_LINE_MARKER,
            investigation_id=investigation,
        )
    )
    again = next(e for e in second.evidence if e.ref.path == LONG_LINE_FILE)
    assert again.id == first.id
    assert again.ref.span_digest == first.ref.span_digest


def test_the_digest_covers_the_returned_fragment_and_nothing_more(
    service: CapabilityService, adversarial_root: Path
) -> None:
    """A fragment with no line span is still immutable evidence with an exact digest.

    Losing the line claim must not make the reference vaguer: the digest still
    names these bytes and only these, so a later revalidation compares the same
    thing it always did.
    """
    from repoatlas.indexing.chunking import chunk_hash

    _, item = _long_line_evidence(service)
    assert item.ref.span_digest == chunk_hash(item.text)

    source = (adversarial_root / SERVICE / LONG_LINE_FILE).read_text(encoding="utf-8")
    assert item.ref.span_digest != chunk_hash(source)
    assert item.ref.span_digest != chunk_hash(source.split("\n")[0])


def test_search_and_inspect_agree_about_what_was_proven(service: CapabilityService) -> None:
    """Two capabilities, one completeness rule.

    They need not return the same shape --- inspect selects by coordinates and
    search selects by relevance --- but neither may claim a line of this file,
    because neither can prove one.
    """
    _, item = _long_line_evidence(service)
    assert item.ref.line_start is None

    inspected = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path=LONG_LINE_FILE),
            line_start=1,
            line_end=1,
            context_lines=0,
            allow_worktree=False,
        )
    )
    assert inspected.segments == ()
    assert inspected.coverage.returned == ()


@pytest.mark.parametrize("item_chars", [500, 2_000, 8_000])
def test_the_budget_cannot_restore_a_line_claim(
    service: CapabilityService, item_chars: int
) -> None:
    """Truncation narrows a span; it can never establish one that was not proven."""
    response = service.search(
        SearchRequest(
            context=GLOBAL,
            question=LONG_LINE_MARKER,
            budget=EvidenceBudget(max_item_chars=item_chars, max_total_chars=32_000),
        )
    )
    for item in response.evidence:
        if item.ref.path != LONG_LINE_FILE:
            continue
        assert item.ref.line_start is None
        assert item.ref.line_end is None


#: A second repository holding a byte-identical copy of the long line at the same
#: relative path. Identity must survive that: the digest is over the text and is
#: therefore equal, so the repository is the only thing keeping the two apart.
DUPLICATE_REPOSITORY = "unrelated-repo"


@pytest.fixture
def duplicated_long_line_service(
    adversarial_root: Path, adversarial_settings: Settings, tmp_path: Path
) -> Iterator[CapabilityService]:
    from repoatlas.services.init_service import initialize

    source = adversarial_root / SERVICE / LONG_LINE_FILE
    (adversarial_root / DUPLICATE_REPOSITORY / LONG_LINE_FILE).write_bytes(source.read_bytes())
    init_git_repository(adversarial_root / DUPLICATE_REPOSITORY)
    workspace = open_workspace(settings=adversarial_settings, require_root=False)
    initialize(workspace, adversarial_root, config_path=tmp_path / "dup-config.yaml")
    svc = CapabilityService(workspace)
    yield svc
    svc.close()
    workspace.close()


def test_the_same_fragment_in_two_repositories_stays_two_pieces_of_evidence(
    duplicated_long_line_service: CapabilityService,
) -> None:
    """Losing the line claim must not blur the one identity that remains.

    With no line range, an evidence reference is a slug, a path and a digest. The
    two copies are byte-identical, so the digest matches and the path matches —
    and the slug is the whole of what distinguishes them. Collapsing them would
    let a citation about one repository resolve against the other.
    """
    response = duplicated_long_line_service.search(
        SearchRequest(context=GLOBAL, question=LONG_LINE_MARKER)
    )
    items = [e for e in response.evidence if e.ref.path == LONG_LINE_FILE]
    slugs = {e.ref.repository_slug for e in items}
    assert len(slugs) == 2, f"expected both copies, saw {slugs}"
    assert len({e.id for e in items}) == 2
    assert len({e.ref.span_digest for e in items}) == 1, "the bytes really are identical"
    for item in items:
        assert item.ref.line_start is None


def test_v1_locate_json_is_not_told_about_any_of_this(adversarial: Workspace) -> None:
    """The V1 payload predates the correction and must not notice it.

    V1's candidate keeps the line range the chunk carries — that is V1's own
    granularity and changing it would be a V1 behaviour change. What must not
    happen is the internal cut provenance appearing in the payload, which is why
    the field is excluded from serialization rather than merely undocumented.
    """
    from repoatlas import services
    from repoatlas.models.context import ContextScope, RepositoryContext

    context = RepositoryContext(
        scope=ContextScope.GLOBAL,
        repositories=tuple(adversarial.repositories.list_all()),
        primary=None,
        reason="global",
    )
    evidence = services.locate(adversarial, LONG_LINE_MARKER, context)
    payload = evidence.model_dump_json()
    assert "split_total" not in payload
    assert "split_index" not in payload

    candidates = [c for c in evidence.candidates if c.path == LONG_LINE_FILE]
    assert candidates, "the fixture must still be locatable from V1"
    # V1 reports the chunk's own range, exactly as it always has.
    assert candidates[0].line_start == 1
    assert candidates[0].line_end == 1
    # And the provenance is present on the object, just not on the wire.
    assert candidates[0].split_total is not None
    assert candidates[0].split_total > 1


# ---------------------------------------------------------------------------
# Seam B --- a span the index cannot reconstruct, through edge provenance
# ---------------------------------------------------------------------------


#: An interned entity the adversarial ecosystem is built around, reached by the
#: handle ``search`` hands back. Named rather than discovered: a helper that
#: swallowed a lookup failure would turn "the corpus stopped producing edges"
#: into a silently vacuous test, which is the failure mode this whole file
#: exists to catch.
PROVENANCE_TARGET = "env:SERVICE_HOST"


def _edges_with_provenance(service: CapabilityService):
    response = service.relationships(
        RelationshipsRequest(context=GLOBAL, target=PROVENANCE_TARGET, max_neighbors=200)
    )
    return [edge for edge in response.edges if edge.provenance is not None]


def test_citable_edge_provenance_is_covered_by_indexed_lines(
    service: CapabilityService, adversarial: Workspace
) -> None:
    """``citable`` implies the chunks prove every line the edge cites.

    Relationship provenance reaches source through ``indexed_line_map``, which
    applies both completeness tests. This asserts the property at the join rather
    than trusting the helper: an edge marked citable must have every one of its
    asserting lines present in the map its own file produces.
    """
    edges = _edges_with_provenance(service)
    assert edges, "the corpus must produce at least one edge with provenance"
    for edge in edges:
        provenance = edge.provenance
        assert provenance is not None
        if not provenance.citable:
            continue
        assert provenance.path is not None
        assert provenance.line_start is not None
        assert provenance.line_end is not None
        row = adversarial.database.execute(
            "SELECT f.id FROM files f JOIN repositories r ON r.id = f.repo_id "
            "WHERE f.rel_path = ? AND r.slug = ?",
            (provenance.path, provenance.repository_slug),
        ).fetchone()
        assert row is not None
        available = indexed_line_map(
            adversarial.knowledge.chunks_for_file(
                row["id"], provenance.line_start, provenance.line_end
            )
        )
        missing = [
            number
            for number in range(provenance.line_start, provenance.line_end + 1)
            if number not in available
        ]
        assert not missing, f"{provenance.path} is citable but cannot prove lines {missing}"


def test_edge_provenance_never_leaks_a_host_path(service: CapabilityService) -> None:
    """Coordinates stay repository-relative whether or not they are citable."""
    for edge in _edges_with_provenance(service):
        provenance = edge.provenance
        assert provenance is not None
        if provenance.path is not None:
            assert not provenance.path.startswith("/")


# ---------------------------------------------------------------------------
# Seam C --- a source the index cannot decode, through dependency observations
# ---------------------------------------------------------------------------


#: A manifest whose stating lines sit inside a span the character cap must cut.
#: Written rather than stored, because the padding that forces the cut is 60 KB
#: of nothing and does not belong in a fixture tree anyone reads.
SPLIT_MANIFEST = (
    "[project]\n"
    'name = "split-manifest"\n'
    "#" + "p" * 60_000 + "\n"
    'dependencies = ["common-lib>=1.0"]\n'
)


@pytest.fixture
def split_manifest_service(
    adversarial_root: Path, adversarial_settings: Settings, tmp_path: Path
) -> Iterator[CapabilityService]:
    """The ecosystem with one manifest the index can parse but cannot quote.

    Built here rather than by mutating the shared fixture: the plant has to land
    before indexing, and a test that reindexed afterwards would be asserting
    against a different seam than the one it names.
    """
    from repoatlas.services.init_service import initialize

    (adversarial_root / "unrelated-repo" / "pyproject.toml").write_text(
        SPLIT_MANIFEST, encoding="utf-8"
    )
    init_git_repository(adversarial_root / "unrelated-repo")
    workspace = open_workspace(settings=adversarial_settings, require_root=False)
    initialize(workspace, adversarial_root, config_path=tmp_path / "split-config.yaml")
    svc = CapabilityService(workspace)
    yield svc
    svc.close()
    workspace.close()


def _observations(service: CapabilityService):
    response = service.dependencies(
        DependenciesRequest(context=GLOBAL, limit=MAX_PAGE_SIZE, include_provenance=True)
    )
    ledgered = {item.id for item in response.evidence}
    pairs = [
        (observation, ledgered)
        for package in response.packages
        for observation in package.observations
    ]
    return pairs


def test_a_dependency_stated_inside_a_cut_span_is_reported_but_not_quotable(
    split_manifest_service: CapabilityService,
) -> None:
    """Seam C, with the branch that the standard corpus never reaches.

    The parser reads the manifest and the dependency fact is real, so it is
    returned with its coordinates: a caller learns that ``unrelated-repo`` asks
    for ``common-lib>=1.0`` and which line said so. What it does not get is an
    evidence id, because the chunks covering that line are pieces of a cut span
    and cannot prove they hold it.

    Dropping the fact would lose something true; offering an id would hand back a
    reference verification cannot resolve. Reporting it uncitable is the third
    answer, and it is the correct one.
    """
    uncitable = [
        observation
        for observation, _ in _observations(split_manifest_service)
        if not observation.provenance.citable
    ]
    assert uncitable, "the planted manifest must produce an observation that cannot be quoted"
    for observation in uncitable:
        assert observation.evidence_ids == ()
        # The structured fact survives in full.
        assert observation.provenance.path
        assert observation.provenance.line_start >= 1


def test_dependency_observations_never_cite_what_was_not_supplied(
    service: CapabilityService,
) -> None:
    """An observation carries an evidence id only when the text was ledgered.

    The general property is asserted rather than the current file list, so a
    future parser that learns to read one more manifest cannot quietly start
    fabricating references to it.
    """
    response = service.dependencies(
        DependenciesRequest(context=GLOBAL, limit=MAX_PAGE_SIZE, include_provenance=True)
    )
    ledgered = {item.id for item in response.evidence}
    citable = 0
    uncitable = 0
    for package in response.packages:
        for observation in package.observations:
            provenance = observation.provenance
            if provenance.citable:
                # Citable means ledgered, and the ids must resolve in the very
                # response that offered them.
                assert observation.evidence_ids
                assert set(observation.evidence_ids) <= ledgered
                citable += 1
            else:
                # Not citable is truthful and frequent. What it may never do is
                # hand back an id, because a caller cannot tell an unresolvable
                # reference from a fabricated one.
                assert observation.evidence_ids == ()
                uncitable += 1
    assert citable > 0, "the corpus must contain at least one citable declaration"
    assert citable + uncitable > 0


def test_undecodable_and_binary_sources_contribute_no_chunks(adversarial: Workspace) -> None:
    """The precondition seam C rests on, asserted rather than assumed.

    If either file ever started producing chunks, the test above would keep
    passing while the property it guards quietly changed meaning.
    """
    for path in ("binary.bin", "invalid_utf8.py", "image.png"):
        row = adversarial.database.execute(
            "SELECT COUNT(*) AS n FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE f.rel_path = ?",
            (path,),
        ).fetchone()
        assert row["n"] == 0, f"{path} produced chunks"
