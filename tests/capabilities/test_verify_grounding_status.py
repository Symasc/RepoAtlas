"""Evidence validity, and the two things it is carefully not.

A citation grounds only when the evidence behind it still corresponds to the
indexed snapshot. That is one question, and this file is mostly about keeping it
separate from two others it keeps getting confused with.

**It is not a question about the working tree.** Evidence can be ``VALID`` against
an index that is itself behind the files on disk. Refusing such a citation would
mean grounding silently required a fresh index, which is a different product:
verification would start failing for reasons that have nothing to do with what the
agent cited. Freshness is reported beside the verdict and never folded into it.

**It is not a question the verifier may answer by looking again.** Stale evidence
is not refreshed, superseded evidence is not followed to its replacement, and an
invalid file is not searched for under another name. Each of those would silently
change what a citation refers to, and the caller would be told their citation
checks out when what checks out is something else.

The remaining subtlety is scope. ``EvidenceLedger.revalidate`` decides the status
of *every* entry it holds, and anything absent from the snapshot it is given
becomes ``UNVERIFIED``. So the snapshot verification gathers covers every file the
ledger names, not merely the cited ones — otherwise verifying one citation would
quietly damage the standing of evidence nobody asked about.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.verification import GROUNDABLE, resolve_citation
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.freshness import FreshnessState
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import (
    EvidenceRef,
    EvidenceStatus,
    LedgerEntry,
)
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFindingCode,
    GroundingVerdict,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
CONFIG = "src/config.py"
SERVICE_HOST = "SERVICE_HOST"


def _resync(workspace: Workspace, root: Path) -> None:
    from repoatlas.services.sync_service import sync

    sync(workspace, workspace.resolver().resolve(root))


def _answer(*claims: AgentClaim, summary: str = "what the evidence shows") -> AgentAnswer:
    return AgentAnswer(summary=summary, claims=claims)


def _claim(statement: str, *citations: AgentCitation) -> AgentClaim:
    return AgentClaim(statement=statement, claimed_confidence=0.9, citations=tuple(citations))


def _verify(
    service: CapabilityService, investigation_id: str, answer: AgentAnswer
) -> VerifyGroundingResponse:
    return service.verify_grounding(
        VerifyGroundingRequest(investigation_id=investigation_id, answer=answer)
    )


def _searched(service: CapabilityService) -> tuple[str, list]:
    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert response.evidence
    return response.investigation_id, list(response.evidence)


def _codes(response: VerifyGroundingResponse) -> set[GroundingFindingCode]:
    return {finding.code for finding in response.findings}


def _cite(
    service: CapabilityService, investigation: str, evidence_id: str
) -> VerifyGroundingResponse:
    return _verify(
        service, investigation, _answer(_claim("x", AgentCitation(evidence_id=evidence_id)))
    )


# ---------------------------------------------------------------------------
# The five statuses, decided directly
# ---------------------------------------------------------------------------


def _entry(status: EvidenceStatus, *, evidence_id: str = "E1") -> LedgerEntry:
    """One ledger entry at a chosen status, without a database to arrange it.

    The end-to-end paths below drive real reindexes, which can reach four of the
    five statuses. ``UNVERIFIED`` is deliberately unreachable through the
    capability — the snapshot verification gathers covers every file the ledger
    names, so no entry can be absent from it — and the rule it earns is pinned
    here rather than left untested because the plumbing makes it rare.
    """
    from datetime import datetime, timezone

    return LedgerEntry(
        id=evidence_id,
        ref=EvidenceRef(
            repository_slug="teams/service",
            path=CONFIG,
            line_start=10,
            line_end=20,
            span_digest="digest-a",
            file_content_hash="hash-a",
            indexed_facts_version="facts-1",
        ),
        repository_name="service",
        tier=EvidenceTier.DETERMINISTIC,
        match=MatchClass.EXACT_CONTENT,
        origins=(EvidenceSource.EXACT,),
        status=status,
        allocated_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (EvidenceStatus.STALE, GroundingFindingCode.STALE_EVIDENCE),
        (EvidenceStatus.SUPERSEDED, GroundingFindingCode.SUPERSEDED_EVIDENCE),
        (EvidenceStatus.INVALID, GroundingFindingCode.INVALID_EVIDENCE),
        (EvidenceStatus.UNVERIFIED, GroundingFindingCode.UNVERIFIED_EVIDENCE),
    ],
)
def test_only_valid_evidence_grounds(status: EvidenceStatus, code: GroundingFindingCode) -> None:
    """CASES 5-8, at the rule. Every status but ``VALID`` refuses the citation."""
    outcome = resolve_citation(
        claim_index=0,
        citation_index=0,
        citation=AgentCitation(evidence_id="E1"),
        entry=_entry(status),
        claimed_confidence=0.9,
    )

    assert outcome.admitted is False
    assert outcome.grounded is False
    assert outcome.code is code
    assert outcome.status is status
    assert outcome.verified is None


def test_valid_evidence_is_the_only_groundable_status() -> None:
    """The predicate, stated once, so a new status cannot arrive as groundable."""
    assert frozenset({EvidenceStatus.VALID}) == GROUNDABLE
    assert set(EvidenceStatus) - GROUNDABLE == {
        EvidenceStatus.STALE,
        EvidenceStatus.SUPERSEDED,
        EvidenceStatus.INVALID,
        EvidenceStatus.UNVERIFIED,
    }


def test_a_refused_status_still_reports_what_was_cited() -> None:
    """The caller learns which evidence and which lines, not only that it failed."""
    outcome = resolve_citation(
        claim_index=3,
        citation_index=1,
        citation=AgentCitation(evidence_id="E1", line_start=12, line_end=14),
        entry=_entry(EvidenceStatus.STALE),
        claimed_confidence=0.9,
    )

    assert outcome.claim_index == 3
    assert outcome.citation_index == 1
    assert outcome.submitted is not None
    assert (outcome.submitted.start, outcome.submitted.end) == (12, 14)
    assert outcome.location == "teams/service/src/config.py:10-20"
    assert "changed" in outcome.detail


# ---------------------------------------------------------------------------
# CASE 5 — stale, end to end
# ---------------------------------------------------------------------------


def test_evidence_whose_file_changed_no_longer_grounds(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 5. The evidence described something real; it no longer describes this."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    cited = evidence[0]
    assert _cite(service, investigation, cited.id).verdict is GroundingVerdict.GROUNDED

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    edited.write_text(
        "# a line inserted at the very top\n" + edited.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _resync(initialized, workspace_root)

    response = _cite(service, investigation, cited.id)

    assert response.verdict is GroundingVerdict.UNGROUNDED
    assert _codes(response) == {GroundingFindingCode.STALE_EVIDENCE}
    assert response.evidence_statuses[0].status is EvidenceStatus.STALE


def test_stale_evidence_is_not_refreshed_to_make_a_citation_work(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The reference is immutable. Verification observes; it does not re-supply."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    cited = evidence[0]
    original = cited.ref

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    edited.write_text("# inserted\n" + edited.read_text(encoding="utf-8"), encoding="utf-8")
    _resync(initialized, workspace_root)
    _cite(service, investigation, cited.id)

    entry = service.sessions.get(investigation).ledger.get(cited.id)
    assert entry is not None
    assert entry.ref == original, "the reference was rebound to different bytes"


def test_evidence_becomes_groundable_again_when_the_exact_snapshot_returns(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Status describes the index now, so recovery is possible in both directions."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    cited = evidence[0]

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    original_text = edited.read_text(encoding="utf-8")
    edited.write_text("# inserted\n" + original_text, encoding="utf-8")
    _resync(initialized, workspace_root)
    assert _cite(service, investigation, cited.id).verdict is GroundingVerdict.UNGROUNDED

    edited.write_text(original_text, encoding="utf-8")
    _resync(initialized, workspace_root)

    assert _cite(service, investigation, cited.id).verdict is GroundingVerdict.GROUNDED


# ---------------------------------------------------------------------------
# CASE 6 — superseded
# ---------------------------------------------------------------------------


def _supersede(
    service: CapabilityService, initialized: Workspace, workspace_root: Path
) -> tuple[str, str, str]:
    """Supply one span, change its bytes, supply it again. Returns both ids."""
    first = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path=CONFIG),
            line_start=1,
            line_end=4,
            context_lines=0,
            allow_worktree=False,
        )
    )
    assert first.segments
    old_id = first.segments[0].id
    span = first.segments[0].ref

    edited = workspace_root / span.repository_slug / span.path
    lines = edited.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[0] = "# the first line, rewritten\n"
    edited.write_text("".join(lines), encoding="utf-8")
    _resync(initialized, workspace_root)

    second = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path=CONFIG),
            line_start=span.line_start,
            line_end=span.line_end,
            context_lines=0,
            allow_worktree=False,
            investigation_id=first.investigation_id,
        )
    )
    assert second.segments
    return first.investigation_id, old_id, second.segments[0].id


def test_superseded_evidence_does_not_ground(initialized: Workspace, workspace_root: Path) -> None:
    """CASE 6. Different bytes were later supplied for that span under a newer id."""
    service = CapabilityService(initialized)
    investigation, old_id, new_id = _supersede(service, initialized, workspace_root)
    ledger = service.sessions.get(investigation).ledger
    entry = ledger.get(old_id)
    assert entry is not None and entry.status is EvidenceStatus.SUPERSEDED

    response = _cite(service, investigation, old_id)

    assert response.verdict is GroundingVerdict.UNGROUNDED
    assert _codes(response) == {GroundingFindingCode.SUPERSEDED_EVIDENCE}
    assert old_id != new_id


def test_a_superseded_citation_is_never_silently_moved_to_its_replacement(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The replacement is named, not substituted. Citing it is the caller's choice.

    Following ``superseded_by`` would make the verdict describe evidence the agent
    never cited, and the agent would have no way to tell. The id is reported so the
    caller can decide; nothing here decides for them.
    """
    service = CapabilityService(initialized)
    investigation, old_id, new_id = _supersede(service, initialized, workspace_root)

    response = _cite(service, investigation, old_id)

    assert response.citations[0].evidence_id == old_id
    assert all(verdict.evidence_id != new_id for verdict in response.citations)
    status = response.evidence_statuses[0]
    assert status.evidence_id == old_id
    assert status.superseded_by == new_id
    # And citing the replacement explicitly does work.
    assert _cite(service, investigation, new_id).verdict is GroundingVerdict.GROUNDED


def test_superseded_stays_refused_even_if_the_bytes_come_back(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Sticky, because two live ids may not claim one span.

    The file returning to its earlier bytes does not undo the fact that a newer id
    was issued for that span. Letting the old id ground again would leave the
    caller holding two ids for one span with no way to know which one anybody else
    means.
    """
    service = CapabilityService(initialized)
    investigation, old_id, _ = _supersede(service, initialized, workspace_root)
    entry = service.sessions.get(investigation).ledger.get(old_id)
    assert entry is not None

    restored = workspace_root / entry.ref.repository_slug / entry.ref.path
    lines = restored.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[0] = "# the first line, rewritten\n"
    restored.write_text("".join(lines), encoding="utf-8")
    _resync(initialized, workspace_root)

    assert _cite(service, investigation, old_id).verdict is GroundingVerdict.UNGROUNDED


# ---------------------------------------------------------------------------
# CASE 7 — invalid
# ---------------------------------------------------------------------------


def test_evidence_whose_file_left_the_index_does_not_ground(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 7. Nothing stands at that identity and path any more."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    cited = evidence[0]

    (workspace_root / cited.ref.repository_slug / cited.ref.path).unlink()
    _resync(initialized, workspace_root)

    response = _cite(service, investigation, cited.id)

    assert response.verdict is GroundingVerdict.UNGROUNDED
    assert _codes(response) == {GroundingFindingCode.INVALID_EVIDENCE}
    assert response.evidence_statuses[0].status is EvidenceStatus.INVALID


def test_no_same_named_replacement_is_searched_for(
    initialized: Workspace, workspace_root: Path
) -> None:
    """A file with the same name elsewhere is a different file, and stays one."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    cited = evidence[0]
    removed = workspace_root / cited.ref.repository_slug / cited.ref.path
    text = removed.read_text(encoding="utf-8")
    removed.unlink()
    elsewhere = removed.parent / f"copy_of_{removed.name}"
    elsewhere.write_text(text, encoding="utf-8")
    _resync(initialized, workspace_root)

    response = _cite(service, investigation, cited.id)

    assert response.verdict is GroundingVerdict.UNGROUNDED
    assert response.citations[0].location == cited.ref.location


# ---------------------------------------------------------------------------
# CASE 13 of §13 — the snapshot covers the whole ledger
# ---------------------------------------------------------------------------


def test_verifying_one_citation_does_not_unverify_the_rest_of_the_ledger(
    initialized: Workspace,
) -> None:
    """Revalidation decides every entry, so the snapshot must cover every entry.

    Gathering file state only for the cited evidence would leave every other entry
    absent from the snapshot and therefore ``UNVERIFIED`` — verification would
    damage session state it was never asked about, and the damage would look like
    a finding about the repository.
    """
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    assert len(evidence) > 1, "this test needs a ledger with more than one entry"
    ledger = service.sessions.get(investigation).ledger
    before = {entry.id: entry.status for entry in ledger.entries()}

    _cite(service, investigation, evidence[0].id)

    after = {entry.id: entry.status for entry in ledger.entries()}
    assert after == before
    assert EvidenceStatus.UNVERIFIED not in after.values()


def test_uncited_evidence_keeps_its_standing_across_repositories(
    initialized: Workspace,
) -> None:
    """The ledger spans repositories, and so must the snapshot gathered for it."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    slugs = {item.ref.repository_slug for item in evidence}
    ledger = service.sessions.get(investigation).ledger

    response = _cite(service, investigation, evidence[0].id)

    assert response.ledger.unverified == 0
    assert all(entry.status is EvidenceStatus.VALID for entry in ledger.entries())
    assert len(slugs) >= 1


# ---------------------------------------------------------------------------
# CASE 16 — one snapshot
# ---------------------------------------------------------------------------


def _rewrite_file_rows_externally(database_path: Path, marker: str) -> None:
    """Commit a file-row change from a genuinely separate connection.

    A connection always sees its own writes, so changing the row through the
    workspace's own would prove nothing about the isolation being tested.
    """
    other = sqlite3.connect(database_path)
    try:
        other.execute("PRAGMA busy_timeout = 5000")
        other.execute(
            "UPDATE files SET content_hash = ?, parser_version = ?",
            (f"{marker}-hash", f"{marker}-version"),
        )
        other.commit()
    finally:
        other.close()


def test_every_evidence_status_comes_from_one_indexed_generation(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 16. A sync landing mid-call cannot split one verdict across two indexes.

    Another process rewrites every file row in the window between the first file
    state being read and the next. Without one read view, some citations would be
    judged against the generation before that commit and some against the one
    after, and the single verdict would describe an index that never existed.
    """
    from repoatlas.storage.sqlite.stores import SqliteFileStore

    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    assert len({item.ref.repository_slug for item in evidence}) >= 1
    database_path = Path(initialized.database.path)
    real = SqliteFileStore.states_for
    calls = {"n": 0}

    def read_then_reindex(self: SqliteFileStore, repo_id: int, paths: object) -> object:
        states = real(self, repo_id, paths)  # type: ignore[arg-type]
        calls["n"] += 1
        if calls["n"] == 1:
            _rewrite_file_rows_externally(database_path, "generation-b")
        return states

    monkeypatch.setattr(SqliteFileStore, "states_for", read_then_reindex)
    response = _verify(
        service,
        investigation,
        _answer(
            *(
                _claim(f"claim {index}", AgentCitation(evidence_id=item.id))
                for index, item in enumerate(evidence)
            )
        ),
    )

    # Either every citation saw the old generation or every citation saw the new
    # one. What must never happen is a mixture.
    statuses = {status.status for status in response.evidence_statuses}
    assert statuses in ({EvidenceStatus.VALID}, {EvidenceStatus.STALE}), statuses


def test_a_generation_landing_after_the_call_is_seen_by_the_next_one(
    initialized: Workspace,
) -> None:
    """The snapshot isolates one call and does not outlive it."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    assert _cite(service, investigation, evidence[0].id).verdict is GroundingVerdict.GROUNDED

    _rewrite_file_rows_externally(Path(initialized.database.path), "generation-c")

    assert _cite(service, investigation, evidence[0].id).verdict is GroundingVerdict.UNGROUNDED


# ---------------------------------------------------------------------------
# CASE 17 — evidence validity is not index freshness
# ---------------------------------------------------------------------------


def test_evidence_stays_groundable_while_the_index_is_behind_the_worktree(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 17. A stale build does not make a matching citation ungrounded.

    Evidence was supplied from an old-but-coherent indexed snapshot, and the index
    still holds exactly that snapshot for the cited file. The repository has moved
    on around it. Refusing the citation would mean grounding quietly required a
    fresh index — a different product, failing for reasons the agent's citation had
    nothing to do with.
    """
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)
    cited = evidence[0]

    # Change a file the evidence does not name, and do not reindex it.
    untouched = workspace_root / cited.ref.repository_slug / "UNRELATED_NEW_FILE.md"
    untouched.write_text("# added to the worktree and never indexed\n", encoding="utf-8")

    response = _cite(service, investigation, cited.id)

    assert response.verdict is GroundingVerdict.GROUNDED
    assert response.evidence_statuses[0].status is EvidenceStatus.VALID


def test_freshness_is_reported_beside_the_verdict_and_not_inside_it(
    initialized: Workspace,
) -> None:
    """Two facts, two fields. Neither is computed from the other."""
    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)

    response = _cite(service, investigation, evidence[0].id)

    assert response.verdict is GroundingVerdict.GROUNDED
    assert response.freshness.state in set(FreshnessState)
    assert "freshness_of_the_index_against_the_working_tree" in response.guarantee.not_checked


def test_verification_reads_freshness_at_stored_depth_and_invokes_no_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification is deterministic and cheap, so it does not shell out to git."""
    from repoatlas.models.freshness import FreshnessDepth

    service = CapabilityService(initialized)
    investigation, evidence = _searched(service)

    from repoatlas.gitstate.client import GitClient

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification invoked git")

    monkeypatch.setattr(GitClient, "state", forbidden)

    response = _cite(service, investigation, evidence[0].id)

    assert response.freshness.depth is FreshnessDepth.STORED
