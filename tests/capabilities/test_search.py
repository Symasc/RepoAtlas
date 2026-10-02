"""The ``search`` capability (V2 Slice D).

Search is the first capability that hands an agent something citable, so the
suite is organised around the two invariants that makes load-bearing:

* every citable byte returned is recorded in exactly one investigation **before**
  the response exists;
* the response describes **this** query, while the ledger remembers the strongest
  metadata the investigation has ever established.

Budget shaping and supplied-span safety have their own suites; ledger atomicity
has a third.
"""

from __future__ import annotations

import json

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.errors import ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceTier
from repoatlas.models.freshness import FreshnessDepth, FreshnessState
from repoatlas.models.search import EvidenceItem, SearchMode, SearchRequest, SearchResponse
from repoatlas.retrieval.retriever import Retriever
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: A question the fixture corpus answers deterministically.
SERVICE_HOST = "Where is SERVICE_HOST configured?"

#: Keys that would mean an internal surrogate id reached a caller.
SURROGATE_KEYS = frozenset(
    {"entity_id", "file_id", "chunk_id", "repo_id", "repository_id", "rowid", "id_"}
)
#: Ranking internals that are not part of the public contract.
SCORE_KEYS = frozenset({"score", "fused_score", "similarity_score", "confidence"})


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _repository_scope(selector: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=selector)


def _keys_anywhere(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= _keys_anywhere(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _keys_anywhere(item)
    return found


# ---------------------------------------------------------------------------
# Investigation lifecycle
# ---------------------------------------------------------------------------


def test_a_search_without_an_id_begins_a_new_investigation(initialized: Workspace) -> None:
    service = _service(initialized)
    assert len(service.sessions) == 0

    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert response.investigation_id
    assert len(service.sessions) == 1
    assert service.sessions.get(response.investigation_id) is not None


def test_two_searches_without_an_id_are_two_investigations(initialized: Workspace) -> None:
    """A long-lived service is not one investigation.

    The server may serve a client for hours across unrelated tasks. If the
    connection were the investigation, ``E1`` would mean "something you were shown
    at some point today", which is a materially weaker thing to have verified.
    """
    service = _service(initialized)
    first = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    second = service.search(SearchRequest(question="load_config", context=GLOBAL))

    assert first.investigation_id != second.investigation_id
    assert len(service.sessions) == 2


def test_evidence_from_one_investigation_does_not_enter_another(
    initialized: Workspace,
) -> None:
    service = _service(initialized)
    first = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    second = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    left = service.sessions.get(first.investigation_id).ledger
    right = service.sessions.get(second.investigation_id).ledger
    # Same question, same spans, two investigations: both hold an E1, and they are
    # unrelated records of two separate supply events.
    assert left.get("E1") is not None and right.get("E1") is not None
    assert left.get("E1") is not right.get("E1")


def test_a_supplied_id_reuses_that_exact_investigation(initialized: Workspace) -> None:
    service = _service(initialized)
    first = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    second = service.search(
        SearchRequest(
            question="load_config", context=GLOBAL, investigation_id=first.investigation_id
        )
    )

    assert second.investigation_id == first.investigation_id
    assert len(service.sessions) == 1
    assert second.ledger.total >= first.ledger.total


def test_an_id_the_server_never_issued_is_refused(initialized: Workspace) -> None:
    """Never a silently fresh ledger: that would accept the id and then reject
    every citation into it, which reads exactly like fabricated evidence."""
    service = _service(initialized)
    with pytest.raises(UnknownInvestigationError):
        service.search(
            SearchRequest(question=SERVICE_HOST, context=GLOBAL, investigation_id="investigation-1")
        )
    assert len(service.sessions) == 0


def test_a_client_cannot_mint_an_investigation_id(initialized: Workspace) -> None:
    """The refusal is what makes a server-minted id more than decoration."""
    service = _service(initialized)
    real = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    invented = real.investigation_id[:-1] + ("z" if real.investigation_id[-1] != "z" else "y")

    with pytest.raises(UnknownInvestigationError):
        service.search(
            SearchRequest(question=SERVICE_HOST, context=GLOBAL, investigation_id=invented)
        )


# ---------------------------------------------------------------------------
# Failure atomicity — no ghost investigations
# ---------------------------------------------------------------------------


def test_an_unresolvable_context_creates_no_investigation(initialized: Workspace) -> None:
    """Context is validated before anything is minted."""
    service = _service(initialized)
    with pytest.raises(ContextResolutionError):
        service.search(
            SearchRequest(question=SERVICE_HOST, context=_repository_scope("does-not-exist"))
        )
    assert len(service.sessions) == 0


def test_a_failed_first_search_leaves_no_empty_investigation(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed call must not hand out an id that means nothing.

    Otherwise the first search of a task fails, an investigation is left behind
    holding nothing, and it either leaks for an hour or is handed to a caller
    whose every citation into it will be rejected.
    """

    def exploding(*_: object, **__: object) -> object:
        raise RuntimeError("retrieval failed")

    monkeypatch.setattr(Retriever, "retrieve_detailed", exploding)
    service = _service(initialized)

    with pytest.raises(RuntimeError):
        service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert len(service.sessions) == 0


def test_a_failure_leaves_an_existing_investigation_intact(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a session this call invented is cleaned up. One the caller owns is not."""
    service = _service(initialized)
    first = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    before = service.sessions.get(first.investigation_id).ledger.summary()

    def exploding(*_: object, **__: object) -> object:
        raise RuntimeError("retrieval failed")

    monkeypatch.setattr(Retriever, "retrieve_detailed", exploding)
    with pytest.raises(RuntimeError):
        service.search(
            SearchRequest(
                question="load_config", context=GLOBAL, investigation_id=first.investigation_id
            )
        )

    assert len(service.sessions) == 1
    assert service.sessions.get(first.investigation_id).ledger.summary() == before


# ---------------------------------------------------------------------------
# Retrieval wrapping
# ---------------------------------------------------------------------------


def test_the_funnel_runs_exactly_once(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One composed retrieval run per search. No second pass, ever.

    A second pass would be a second opinion about relevance formed at a layer the
    trust guarantees were not written for — and it could disagree with the first.
    """
    calls: list[str] = []
    real = Retriever.retrieve_detailed

    def counting(self: Retriever, question: str, context: object, **kwargs: object) -> object:
        calls.append(question)
        return real(self, question, context, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Retriever, "retrieve_detailed", counting)
    _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert calls == [SERVICE_HOST]


def test_the_capability_reimplements_no_retrieval_stage() -> None:
    """Search is an adapter around one run, not a second funnel.

    Asserted as an absence because the failure is gradual: one stage reproduced
    "just for the capability" is how two rankings with two sets of guarantees come
    to exist.
    """
    from pathlib import Path

    source = Path("repoatlas/capabilities/search.py").read_text(encoding="utf-8")
    service = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    for marker in ("find_literal", "search_text", "fuse(", "deduplicate", "enforce_precedence"):
        assert marker not in source, f"search.py reimplements {marker}"
        assert marker not in service, f"service.py reimplements {marker}"


def test_the_trusted_order_is_preserved(initialized: Workspace) -> None:
    """Shaping narrows the ranked set; it never reorders it."""
    service = _service(initialized)
    assert initialized.root is not None
    context = initialized.resolver().resolve(initialized.root)
    expected = [
        (c.repository_slug, c.path, c.line_start)
        for c in initialized.retriever()
        .retrieve_detailed(SERVICE_HOST, context)
        .evidence.candidates
    ]

    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    returned = [(i.ref.repository_slug, i.ref.path, i.ref.line_start) for i in response.evidence]

    # A prefix of the trusted order, allowing for line narrowing on the way.
    assert len(returned) <= len(expected)
    for got, want in zip(returned, expected[: len(returned)], strict=True):
        assert got[:2] == want[:2]


def test_ranks_are_consecutive_from_one(initialized: Workspace) -> None:
    """Rank is position in this response, never a score and never gapped."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert [item.rank for item in response.evidence] == list(range(1, len(response.evidence) + 1))


def test_deterministic_evidence_still_leads_after_shaping(initialized: Workspace) -> None:
    """Tier precedence survives the budget layer (V1_SPEC §14)."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    tiers = [item.tier for item in response.evidence]
    assert tiers[0] is EvidenceTier.DETERMINISTIC
    # Once an associative item appears, no deterministic one may follow it.
    seen_associative = False
    for tier in tiers:
        if tier is EvidenceTier.ASSOCIATIVE:
            seen_associative = True
        elif seen_associative:
            raise AssertionError("deterministic evidence ranked below associative")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def test_diagnostic_mode_uses_the_widened_profile(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same funnel with wider bounds, plus the whole question as a phrase."""
    seen: list[tuple[int, bool]] = []
    real = Retriever.retrieve_detailed

    def spy(self: Retriever, question: str, context: object, **kwargs: object) -> object:
        seen.append((self._limits.per_source, bool(kwargs.get("literal_phrase"))))
        return real(self, question, context, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Retriever, "retrieve_detailed", spy)
    service = _service(initialized)

    service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL, mode=SearchMode.DIAGNOSTIC))

    (lookup_per_source, lookup_phrase), (diag_per_source, diag_phrase) = seen
    assert diag_per_source > lookup_per_source
    assert lookup_phrase is False and diag_phrase is True


def test_search_needs_no_reasoning_model(initialized: Workspace) -> None:
    """Deterministic all the way down. The gate environment points the model base
    URL at a closed port, so a call would fail rather than hang unnoticed."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert response.evidence


# ---------------------------------------------------------------------------
# Current-query metadata versus accumulated ledger metadata
# ---------------------------------------------------------------------------


def _find(response: SearchResponse, needle: str) -> EvidenceItem | None:
    return next((item for item in response.evidence if needle in item.ref.path), None)


def test_a_later_weak_run_reports_weakly_while_the_ledger_stays_strong(
    initialized: Workspace,
) -> None:
    """The secondary invariant, in the direction that matters.

    The ledger strengthens across an investigation and keeps the strongest view.
    The response must still describe *this* run: reporting the accumulated tier
    would tell the caller this query proved something it did not.
    """
    service = _service(initialized)
    strong = service.search(SearchRequest(question="src/config.py", context=GLOBAL))
    item = _find(strong, "src/config.py")
    assert item is not None

    ledger = service.sessions.get(strong.investigation_id).ledger
    recorded = ledger.get(item.id)
    assert recorded is not None
    assert recorded.tier is item.tier

    # A second, weaker route to the same span inside the same investigation.
    weak = service.search(
        SearchRequest(
            question="how does the service read its configuration",
            context=GLOBAL,
            investigation_id=strong.investigation_id,
        )
    )
    repeat = _find(weak, "src/config.py")
    if repeat is None:
        pytest.skip("the weaker query did not reach the same span in this corpus")

    entry = ledger.get(repeat.id)
    assert entry is not None
    # The response reports what this run established...
    assert repeat.tier is not None
    # ...and the ledger never weakened.
    assert entry.tier <= repeat.tier
    assert entry.match <= repeat.match


def test_the_response_is_not_a_serialized_ledger_entry(initialized: Workspace) -> None:
    """The ledger accumulates every route; one response reports one route.

    If the response were built from ledger entries, a span found twice would
    report both origins as though this query used both.
    """
    service = _service(initialized)
    first = service.search(SearchRequest(question="src/config.py", context=GLOBAL))
    second = service.search(
        SearchRequest(
            question="src/config.py",
            context=GLOBAL,
            investigation_id=first.investigation_id,
        )
    )
    item = _find(second, "src/config.py")
    assert item is not None
    entry = service.sessions.get(first.investigation_id).ledger.get(item.id)
    assert entry is not None

    # The entry accumulates reasons across the investigation; the item carries one.
    assert isinstance(item.why, str)
    assert not hasattr(item, "origins")
    assert not hasattr(item, "reasons")


def test_the_same_span_and_bytes_keep_one_evidence_id(initialized: Workspace) -> None:
    service = _service(initialized)
    first = service.search(SearchRequest(question="src/config.py", context=GLOBAL))
    second = service.search(
        SearchRequest(
            question="src/config.py", context=GLOBAL, investigation_id=first.investigation_id
        )
    )
    left = {(i.ref.span_key, i.ref.span_digest): i.id for i in first.evidence}
    for item in second.evidence:
        key = (item.ref.span_key, item.ref.span_digest)
        if key in left:
            assert item.id == left[key]


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_a_named_term_that_exists_nowhere_suppresses_associative_evidence(
    initialized: Workspace,
) -> None:
    """Empty evidence with a suppression report is a truthful answer, not a failure."""
    response = _service(initialized).search(
        SearchRequest(question="where is zzz_nonexistent_symbol_xyz used?", context=GLOBAL)
    )

    assert response.suppressed is not None
    assert "zzz_nonexistent_symbol_xyz" in response.suppressed.terms
    assert response.evidence == ()
    assert response.ledger.total == 0


def test_the_suppression_report_is_the_funnels_own_structured_outcome(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taken from the run, never parsed out of a trace sentence."""
    captured: list[object] = []
    real = Retriever.retrieve_detailed

    def spy(self: Retriever, question: str, context: object, **kwargs: object) -> object:
        outcome = real(self, question, context, **kwargs)  # type: ignore[arg-type]
        captured.append(outcome.suppression)
        return outcome

    monkeypatch.setattr(Retriever, "retrieve_detailed", spy)
    response = _service(initialized).search(
        SearchRequest(question="where is zzz_nonexistent_symbol_xyz used?", context=GLOBAL)
    )

    assert response.suppressed is captured[0]


def test_no_capability_module_parses_a_trace_string() -> None:
    from pathlib import Path

    for name in ("search.py", "service.py"):
        source = Path(f"repoatlas/capabilities/{name}").read_text(encoding="utf-8")
        assert ".detail.startswith" not in source
        assert "suppressed:" not in source


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_search_reports_stored_freshness_and_invokes_no_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrieval should not pay for a git process per repository to answer."""
    from pathlib import Path

    from repoatlas.gitstate.client import GitClient, GitRepositoryState

    def forbidden(self: GitClient, repo_path: Path) -> GitRepositoryState:
        raise AssertionError("search invoked git at stored depth")

    monkeypatch.setattr(GitClient, "state", forbidden)
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert response.freshness.depth is FreshnessDepth.STORED
    assert all(r.head_matches is None for r in response.freshness.repositories)


def test_search_never_detects_repository_set_drift(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search is evidence retrieval, not a workspace audit."""
    from repoatlas.services import freshness_service

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("search walked the managed root")

    monkeypatch.setattr(freshness_service, "plan_repository_set", forbidden)
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert response.freshness.repository_set_drifted is None


def test_live_freshness_is_available_when_asked_for(initialized: Workspace) -> None:
    response = _service(initialized).search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, depth=FreshnessDepth.LIVE)
    )
    assert response.freshness.depth is FreshnessDepth.LIVE
    assert all(r.head_matches is not None for r in response.freshness.repositories)


def test_a_stale_index_is_labelled_and_still_searched(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staleness is metadata, not a gate, and nothing syncs to fix it."""
    from repoatlas.capabilities import service as service_module

    monkeypatch.setattr(service_module, "current_facts_version", lambda _ws: "schema:999")
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert response.freshness.state is FreshnessState.STALE_BUILD
    assert response.evidence


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def test_the_response_echoes_what_was_searched(initialized: Workspace) -> None:
    response = _service(initialized).search(
        SearchRequest(question=SERVICE_HOST, context=_repository_scope("python-service"))
    )
    assert response.context.scope is CapabilityScope.REPOSITORY
    assert response.context.primary == "python-service"
    assert response.context.repositories == ("python-service",)


def test_repository_scope_answers_out_of_the_repository_it_names(
    initialized: Workspace,
) -> None:
    """An explicit context is a boundary, not a bias.

    This reverses what Slice D shipped, deliberately. Search inherited V1's
    "primary scope is a bias rather than a wall" (V1_SPEC §6) by being an adapter
    over the V1 funnel, and stated no rule of its own — while every other
    capability had already written the opposite about itself: ``inspect`` refuses a
    path outside the scope, ``relationships`` refuses a key outside it,
    ``dependencies`` "narrows the answer; it never merges across repositories".
    A search that federated while the capabilities it hands its own keys to refuse
    them is not a bias, it is an inconsistency, and the agent pays for it one call
    later. The bias remains exactly where it was written: the V1 CLI.
    """
    response = _service(initialized).search(
        SearchRequest(question=SERVICE_HOST, context=_repository_scope("python-service"))
    )
    assert response.evidence, "the question is still answerable inside one repository"
    assert {item.ref.repository_slug for item in response.evidence} == {"python-service"}


def test_a_shared_node_is_still_named_under_repository_scope(initialized: Workspace) -> None:
    """The boundary is around repositories, and a hub belongs to none of them.

    This is what keeps confinement from being a dead end. The evidence stops at the
    repository, but the handle that leads out of it still comes back, so "where is
    SERVICE_HOST set?" is one deliberate ``relationships`` call away rather than
    unanswerable — and the agent chooses to cross instead of crossing unknowingly.
    """
    response = _service(initialized).search(
        SearchRequest(question=SERVICE_HOST, context=_repository_scope("python-service"))
    )
    assert "env:SERVICE_HOST" in {handle.key for handle in response.entities}


def test_global_scope_still_sees_every_repository(initialized: Workspace) -> None:
    """The way across is stated in the request, and it still works."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert len({item.ref.repository_slug for item in response.evidence}) > 1


def test_a_repository_context_is_one_repository_and_it_is_the_primary(
    initialized: Workspace,
) -> None:
    """What "the" repository means, for the code that confines a walk to it.

    Confinement reads its home off ``context.primary``; the scoped lookups read
    their ids off ``context.repositories``. Those are two expressions of one
    repository *because resolution says so*, and this is where it says it. If
    ``REPOSITORY`` scope ever resolved to several — a nesting rule, a group — the
    two would part company silently, one confining to a repository the other was
    not searching, and this fails first instead.
    """
    from repoatlas.capabilities.context import resolve_context

    resolution = resolve_context(
        _repository_scope("python-service"), initialized.repositories.list_all()
    )
    assert len(resolution.repositories) == 1
    assert resolution.primary == resolution.repositories[0]


def test_the_v1_funnel_still_crosses_for_the_cli(initialized: Workspace) -> None:
    """Confinement is a capability-layer option, not a change to the funnel.

    ``locate`` and ``debug`` keep the documented primary-first behaviour, which
    ``tests/test_retrieval.py`` pins from the CLI side. Asserted here too, against
    the same retriever the capability uses, so that a later "simplification" that
    moves confinement into the funnel's default fails in the suite that wanted it.
    """
    from repoatlas.capabilities.context import resolve_context, to_repository_context

    resolution = resolve_context(
        _repository_scope("python-service"), initialized.repositories.list_all()
    )
    context = to_repository_context(resolution)
    outcome = initialized.retriever().retrieve_detailed(SERVICE_HOST, context)
    assert len({c.repository_slug for c in outcome.evidence.candidates}) > 1


def test_an_empty_question_is_refused() -> None:
    with pytest.raises(ValueError):
        SearchRequest(question="   ", context=GLOBAL)


# ---------------------------------------------------------------------------
# Model hygiene
# ---------------------------------------------------------------------------


def test_the_response_carries_no_surrogate_id_or_score(initialized: Workspace) -> None:
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    keys = _keys_anywhere(json.loads(response.model_dump_json()))
    assert not (keys & SURROGATE_KEYS), sorted(keys & SURROGATE_KEYS)
    assert not (keys & SCORE_KEYS), sorted(keys & SCORE_KEYS)


def test_the_response_carries_no_host_path(initialized: Workspace, workspace_root: object) -> None:
    """Evidence is located by slug and repository-relative path, never by host layout."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    payload = response.model_dump_json()
    assert str(workspace_root) not in payload


def test_the_trace_is_absent_unless_requested(initialized: Workspace) -> None:
    service = _service(initialized)
    plain = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert plain.trace is None

    traced = service.search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, include_trace=True)
    )
    assert traced.trace is not None


def test_search_is_fully_usable_with_the_trace_disabled(initialized: Workspace) -> None:
    """Every fact a caller acts on is first-class; the trace is diagnostic only."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert response.trace is None
    assert response.investigation_id and response.budget and response.freshness
    assert response.ledger.total == len(response.evidence)
    assert all(item.id and item.ref for item in response.evidence)


def test_only_the_opted_in_trace_may_carry_ranking_internals(initialized: Workspace) -> None:
    """The trace is instrumentation, and is the one place internals may appear."""
    response = _service(initialized).search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, include_trace=True)
    )
    without_trace = response.model_copy(update={"trace": None})
    assert not (_keys_anywhere(json.loads(without_trace.model_dump_json())) & SCORE_KEYS)


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def test_search_writes_nothing_to_the_index(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(initialized)

    def forbid(name: str):
        def spy(*_: object, **__: object) -> object:
            raise AssertionError(f"search called {name}")

        return spy

    monkeypatch.setattr(Workspace, "writer_lock", forbid("writer_lock"))
    monkeypatch.setattr(Workspace, "sync_engine", forbid("sync_engine"))
    monkeypatch.setattr(initialized.repositories, "upsert", forbid("repositories.upsert"))
    monkeypatch.setattr(initialized.files, "upsert", forbid("files.upsert"))
    monkeypatch.setattr(initialized.files, "delete_many", forbid("files.delete_many"))
    monkeypatch.setattr(initialized.knowledge, "add_edges", forbid("knowledge.add_edges"))
    monkeypatch.setattr(
        initialized.knowledge, "replace_file_facts", forbid("knowledge.replace_file_facts")
    )
    monkeypatch.setattr(initialized.knowledge, "intern_entity", forbid("knowledge.intern_entity"))

    service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    service.search(SearchRequest(question="load_config", context=GLOBAL))
    service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL, mode=SearchMode.DIAGNOSTIC))


def test_the_indexed_facts_are_unchanged_by_searching(initialized: Workspace) -> None:
    """A logical snapshot of every fact table, not a file mtime."""
    from tests.conftest import fact_snapshot

    service = _service(initialized)
    before = fact_snapshot(initialized)

    service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    service.search(SearchRequest(question="load_config", context=GLOBAL))
    service.search(
        SearchRequest(question="retry behaviour", context=GLOBAL, mode=SearchMode.DIAGNOSTIC)
    )

    assert fact_snapshot(initialized) == before
