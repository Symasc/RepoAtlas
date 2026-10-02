"""Evidence already held by live investigations, after sync changes the index.

After RepoAtlas changes its index through this service, no active ledger may go on
silently claiming validity against the snapshot that was replaced. So every live
investigation is re-checked eagerly, against one coherent read of the index as the
sync left it, using Slice B's rules unchanged:

* same identity, same bytes, same facts version — ``VALID`` stays ``VALID``;
* changed bytes, or identical bytes under a new facts version — ``STALE``;
* no indexed file there any more, or no repository under that slug — ``INVALID``;
* ``SUPERSEDED`` is sticky, whatever the bytes do next;
* evidence whose exact snapshot returns may become ``VALID`` again.

Eager re-checking is a convenience and a correctness improvement, and it is **not**
the defence. Another process can run ``repoatlas sync`` without this service ever
knowing, which is why verification still revalidates before it grounds anything —
and why a failure in this housekeeping is reported rather than treated as a failed
sync.

Housekeeping must also stay housekeeping. Walking every investigation to re-check
it is not activity inside any of them, so it touches no idle clock, reorders
nothing in the LRU, resurrects nothing that expired, allocates no id and
strengthens no metadata. It changes statuses and nothing else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import SyncFailedError, capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceRef, EvidenceStatus
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import (
    EvidenceRevalidationSummary,
    RevalidationStatus,
    SyncRequest,
    SyncResponse,
    SyncStatus,
)
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFindingCode,
    GroundingVerdict,
    VerifyGroundingRequest,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
CONFIG = "src/config.py"
SERVICE_HOST = "SERVICE_HOST"


def _sync(service: CapabilityService) -> SyncResponse:
    return service.sync(SyncRequest(context=GLOBAL))


def _search(
    service: CapabilityService, question: str = SERVICE_HOST, investigation: str | None = None
) -> tuple[str, list]:
    response = service.search(
        SearchRequest(question=question, context=GLOBAL, investigation_id=investigation)
    )
    assert response.evidence, f"the fixture corpus must answer {question!r}"
    return response.investigation_id, list(response.evidence)


def _ledger(service: CapabilityService, investigation: str):
    """The ledger, reached without touching the investigation's idle clock."""
    return service.sessions._sessions[investigation].ledger


def _status(service: CapabilityService, investigation: str, evidence_id: str) -> EvidenceStatus:
    entry = _ledger(service, investigation).get(evidence_id)
    assert entry is not None, evidence_id
    return entry.status


def _append(root: Path, ref: EvidenceRef) -> None:
    path = root / ref.repository_slug / ref.path
    path.write_text(
        path.read_text(encoding="utf-8", errors="replace") + "\n# appended by a sync test\n",
        encoding="utf-8",
    )


def _pairs(
    summary: EvidenceRevalidationSummary,
) -> dict[tuple[EvidenceStatus, EvidenceStatus], int]:
    return {(t.previous, t.current): t.count for t in summary.transitions}


def _file_hash(workspace: Workspace, ref: EvidenceRef) -> str | None:
    repo = next(r for r in workspace.repositories.list_all() if r.slug == ref.repository_slug)
    assert repo.id is not None
    state = workspace.files.states_for(repo.id, [ref.path]).get(ref.path)
    return state.content_hash if state else None


# ---------------------------------------------------------------------------
# CASE 10 — nothing changed
# ---------------------------------------------------------------------------


def test_evidence_whose_file_did_not_change_stays_valid(initialized: Workspace) -> None:
    """CASE 10. Same identity, same bytes, same facts version."""
    service = CapabilityService(initialized)
    investigation, _ = _search(service)
    before = {entry.id: entry.status for entry in _ledger(service, investigation).entries()}

    summary = _sync(service).evidence_revalidation

    after = {entry.id: entry.status for entry in _ledger(service, investigation).entries()}
    assert after == before
    assert set(after.values()) == {EvidenceStatus.VALID}
    assert summary.status is RevalidationStatus.COMPLETED
    assert summary.investigations_checked == 1
    assert summary.evidence_checked == len(before)
    assert summary.valid == len(before)
    assert summary.transitions == ()


# ---------------------------------------------------------------------------
# CASE 11 — changed bytes
# ---------------------------------------------------------------------------


def test_evidence_whose_file_changed_becomes_stale(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 11. The evidence described something real; it no longer describes this."""
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    edited = evidence[0].ref
    _append(workspace_root, edited)

    response = _sync(service)

    for entry in _ledger(service, investigation).entries():
        same_file = (entry.ref.repository_slug, entry.ref.path) == (
            edited.repository_slug,
            edited.path,
        )
        expected = EvidenceStatus.STALE if same_file else EvidenceStatus.VALID
        assert entry.status is expected, entry.ref.location
    assert _pairs(response.evidence_revalidation)[(EvidenceStatus.VALID, EvidenceStatus.STALE)] >= 1


def test_the_evidence_reference_is_never_rebound(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The record is immutable. Only whether the index still agrees can change."""
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    refs = {entry.id: entry.ref for entry in _ledger(service, investigation).entries()}
    _append(workspace_root, evidence[0].ref)

    _sync(service)

    assert {entry.id: entry.ref for entry in _ledger(service, investigation).entries()} == refs


# ---------------------------------------------------------------------------
# CASE 12 — identical bytes, new facts version
# ---------------------------------------------------------------------------


def test_identical_bytes_under_a_new_facts_version_become_stale(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 12. A parser change alters the facts derived from unchanged bytes.

    The version is changed where V1 derives it, so the sync reparses every file
    exactly as it would after an upgrade. No byte on disk moves.
    """
    import repoatlas.sync.engine as engine

    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    hashes = {e.id: _file_hash(initialized, e.ref) for e in evidence}
    real = engine.facts_version

    def upgraded(*args: object, **kwargs: object) -> str:
        return f"{real(*args, **kwargs)}+upgraded"  # type: ignore[arg-type]

    monkeypatch.setattr(engine, "facts_version", upgraded)

    _sync(service)

    assert {e.id: _file_hash(initialized, e.ref) for e in evidence} == hashes
    assert {entry.status for entry in _ledger(service, investigation).entries()} == {
        EvidenceStatus.STALE
    }


# ---------------------------------------------------------------------------
# CASE 13 — gone from the index
# ---------------------------------------------------------------------------


def test_evidence_whose_file_left_the_index_becomes_invalid(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 13. No indexed file stands at that identity and path any more."""
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    removed = evidence[0]
    (workspace_root / removed.ref.repository_slug / removed.ref.path).unlink()

    response = _sync(service)

    assert _status(service, investigation, removed.id) is EvidenceStatus.INVALID
    assert (
        _pairs(response.evidence_revalidation)[(EvidenceStatus.VALID, EvidenceStatus.INVALID)] >= 1
    )


def test_evidence_from_a_retired_repository_becomes_invalid(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The slug is gone, so nothing stands at that identity. No same-named substitute."""
    import subprocess

    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    retired = evidence[0].ref.repository_slug
    subprocess.run(["rm", "-rf", str(workspace_root / retired)], check=True)

    _sync(service)

    for entry in _ledger(service, investigation).entries():
        if entry.ref.repository_slug == retired:
            assert entry.status is EvidenceStatus.INVALID, entry.ref.location


# ---------------------------------------------------------------------------
# CASE 14 — the exact snapshot returns
# ---------------------------------------------------------------------------


def test_evidence_recovers_when_its_exact_snapshot_returns(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 14. Slice B's recovery rule: status describes the index now."""
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    cited = evidence[0]
    path = workspace_root / cited.ref.repository_slug / cited.ref.path
    original = path.read_bytes()

    _append(workspace_root, cited.ref)
    _sync(service)
    assert _status(service, investigation, cited.id) is EvidenceStatus.STALE

    path.write_bytes(original)
    response = _sync(service)

    assert _status(service, investigation, cited.id) is EvidenceStatus.VALID
    assert _pairs(response.evidence_revalidation)[(EvidenceStatus.STALE, EvidenceStatus.VALID)] >= 1


# ---------------------------------------------------------------------------
# CASE 15 — superseded is sticky
# ---------------------------------------------------------------------------


def test_superseded_evidence_stays_superseded_across_syncs(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 15. A newer id was issued for that span; restored bytes do not undo it."""
    service = CapabilityService(initialized)
    request = InspectRequest(
        context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
        target=InspectTarget(path=CONFIG),
        line_start=1,
        line_end=4,
        context_lines=0,
        allow_worktree=False,
    )
    first = service.inspect(request)
    investigation, old = first.investigation_id, first.segments[0]

    path = workspace_root / SERVICE / CONFIG
    original = path.read_bytes()
    lines = original.decode("utf-8").splitlines(keepends=True)
    lines[0] = "# the first line, rewritten\n"
    path.write_text("".join(lines), encoding="utf-8")
    _sync(service)
    service.inspect(request.model_copy(update={"investigation_id": investigation}))
    assert _status(service, investigation, old.id) is EvidenceStatus.SUPERSEDED

    path.write_bytes(original)
    response = _sync(service)

    assert _status(service, investigation, old.id) is EvidenceStatus.SUPERSEDED
    assert response.evidence_revalidation.superseded >= 1


# ---------------------------------------------------------------------------
# CASE 16 — every origin, one rule
# ---------------------------------------------------------------------------


def test_evidence_from_every_origin_is_revalidated_by_the_same_rule(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 16. Search, inspection, graph and dependency evidence move together."""
    service = CapabilityService(initialized)
    investigation, searched = _search(service)
    inspected = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path=CONFIG),
            line_start=1,
            line_end=6,
            context_lines=0,
            allow_worktree=False,
            investigation_id=investigation,
        )
    ).segments
    related = service.relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target="file:scripts:deploy.sh",
            max_neighbors=200,
            investigation_id=investigation,
        )
    ).evidence
    declared = service.dependencies(
        DependenciesRequest(context=GLOBAL, limit=50, investigation_id=investigation)
    ).evidence
    assert inspected and related and declared, "every origin must supply evidence"
    cited = [searched[0], inspected[0], related[0], declared[0]]

    touched: set[tuple[str, str]] = set()
    for item in cited:
        key = (item.ref.repository_slug, item.ref.path)
        if key not in touched:
            _append(workspace_root, item.ref)
            touched.add(key)

    _sync(service)

    for item in cited:
        assert _status(service, investigation, item.id) is EvidenceStatus.STALE, item.location


# ---------------------------------------------------------------------------
# CASE 17 / 18 — housekeeping is not use
# ---------------------------------------------------------------------------


def _clocked(workspace: Workspace, *, ttl: float) -> tuple[CapabilityService, list[datetime]]:
    service = CapabilityService(workspace, idle_ttl_seconds=ttl)  # type: ignore[arg-type]
    now = [datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)]
    service.sessions._clock = lambda: now[0]
    return service, now


def test_an_expired_investigation_is_not_resurrected_by_sync(initialized: Workspace) -> None:
    """CASE 17. Discarded first, rather than revived by being looked at."""
    service, now = _clocked(initialized, ttl=60)
    investigation, _ = _search(service)
    now[0] += timedelta(seconds=120)

    response = _sync(service)

    assert response.evidence_revalidation.investigations_checked == 0
    with pytest.raises(UnknownInvestigationError):
        service.sessions.get(investigation)


def test_sync_touches_no_surviving_investigations_clock_or_order(initialized: Workspace) -> None:
    """CASE 18. Neither ``last_used_at`` nor LRU position moves."""
    service, now = _clocked(initialized, ttl=3600)
    first, _ = _search(service)
    now[0] += timedelta(seconds=1)
    second, _ = _search(service, "deploy.sh")
    order = list(service.sessions._sessions)
    used = {key: session.last_used_at for key, session in service.sessions._sessions.items()}
    now[0] += timedelta(minutes=10)

    response = _sync(service)

    assert list(service.sessions._sessions) == order == [first, second]
    assert {k: s.last_used_at for k, s in service.sessions._sessions.items()} == used
    assert response.evidence_revalidation.investigations_checked == 2


# ---------------------------------------------------------------------------
# CASE 19 — the sync committed; the housekeeping did not finish
# ---------------------------------------------------------------------------


def test_a_failed_recheck_does_not_pretend_the_sync_failed(
    initialized: Workspace, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 19. Two facts, reported separately, and neither rolled back.

    The index change stands. The response says the sync completed and that the
    eager re-check did not, the investigations are kept, and nothing is undone to
    make the two agree. Verification then still refuses the stale citation,
    because it revalidates for itself.
    """
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    cited = evidence[0]
    before_hash = _file_hash(initialized, cited.ref)
    _append(workspace_root, cited.ref)

    def unreadable(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected failure while re-checking evidence")

    monkeypatch.setattr(CapabilityService, "_ledger_snapshot", unreadable)
    response = _sync(service)
    monkeypatch.undo()

    assert response.status is SyncStatus.COMPLETED
    assert response.evidence_revalidation.status is RevalidationStatus.FAILED
    assert response.evidence_revalidation.detail
    assert _file_hash(initialized, cited.ref) != before_hash, "the index change must stand"
    assert len(service.sessions) == 1
    assert _status(service, investigation, cited.id) is EvidenceStatus.VALID

    verdict = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=investigation,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(statement="x", citations=(AgentCitation(evidence_id=cited.id),)),
                ),
            ),
        )
    )
    assert verdict.verdict is GroundingVerdict.UNGROUNDED
    assert {f.code for f in verdict.findings} == {GroundingFindingCode.STALE_EVIDENCE}


# ---------------------------------------------------------------------------
# CASE 20 — a failed sync that had already changed the index
# ---------------------------------------------------------------------------


def test_a_failed_sync_rechecks_evidence_against_the_index_it_left(
    initialized: Workspace, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 20. V1 commits each file as it goes; the failure comes after.

    The failure is injected inside the run's final transaction — after the edited
    file's facts committed, before the graph swap and the currency claim. So the
    index has moved, and a ledger still calling that evidence ``VALID`` would be
    describing a snapshot that is gone. The re-check runs against the index as the
    failure left it, and only then is the failure raised, saying so.
    """
    from repoatlas.graph.linker import ReferenceLinker

    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    cited = evidence[0]
    before_hash = _file_hash(initialized, cited.ref)
    _append(workspace_root, cited.ref)

    def failing_commit(self: ReferenceLinker, plan: object) -> object:
        raise RuntimeError("injected failure inside the final transaction")

    monkeypatch.setattr(ReferenceLinker, "commit_global", failing_commit)
    with pytest.raises(SyncFailedError) as raised:
        _sync(service)
    monkeypatch.undo()

    error = capability_error(raised.value)
    assert error.code == "SYNC_FAILED"
    assert error.context["index_may_have_changed"] is True
    assert error.context["cause"] == "INTERNAL_ERROR"
    assert _file_hash(initialized, cited.ref) != before_hash, "V1 committed the file first"
    assert _status(service, investigation, cited.id) is EvidenceStatus.STALE
    revalidation = error.context["revalidation"]
    assert isinstance(revalidation, dict)
    assert revalidation["status"] == "completed"

    # Nothing was rolled back, and a plain sync is the whole recovery path.
    assert _sync(service).status is SyncStatus.COMPLETED


# ---------------------------------------------------------------------------
# CASE 23 — no evidence allocated, no history rewritten
# ---------------------------------------------------------------------------


def _structure(service: CapabilityService, investigation: str) -> dict[str, object]:
    ledger = _ledger(service, investigation)
    return {
        "next_number": ledger._next_number,
        "ids": tuple(entry.id for entry in ledger.entries()),
        "refs": {entry.id: entry.ref for entry in ledger.entries()},
        "tiers": {entry.id: entry.tier for entry in ledger.entries()},
        "matches": {entry.id: entry.match for entry in ledger.entries()},
        "origins": {entry.id: entry.origins for entry in ledger.entries()},
        "reasons": {entry.id: entry.reasons for entry in ledger.entries()},
        "allocated": {entry.id: entry.allocated_at for entry in ledger.entries()},
        "superseded_by": {entry.id: entry.superseded_by for entry in ledger.entries()},
    }


def test_sync_allocates_no_evidence_and_rewrites_no_history(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 23. Statuses may move. Nothing else about any record may."""
    service = CapabilityService(initialized)
    first, evidence = _search(service)
    second, _ = _search(service, "deploy.sh")
    before = {first: _structure(service, first), second: _structure(service, second)}
    _append(workspace_root, evidence[0].ref)

    _sync(service)

    assert set(service.sessions._sessions) == {first, second}
    assert {first: _structure(service, first), second: _structure(service, second)} == before


# ---------------------------------------------------------------------------
# An external sync is still seen
# ---------------------------------------------------------------------------


def test_an_external_sync_after_this_one_is_still_seen_by_verification(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Eager re-checking happened here; verification must not rely on it.

    This service syncs and re-checks. Another writer then changes the index
    without going through this service. A verifier that trusted the last eager
    re-check would ground a citation to evidence the index no longer holds —
    which is why file state is the test.
    """
    from repoatlas.services import sync_service

    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    cited = evidence[0]
    _sync(service)

    _append(workspace_root, cited.ref)
    sync_service.sync(initialized, initialized.resolver().resolve(workspace_root))

    verdict = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=investigation,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(statement="x", citations=(AgentCitation(evidence_id=cited.id),)),
                ),
            ),
        )
    )
    assert verdict.verdict is GroundingVerdict.UNGROUNDED
    assert {f.code for f in verdict.findings} == {GroundingFindingCode.STALE_EVIDENCE}
