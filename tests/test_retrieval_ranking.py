"""Ranking, tiering, expansion and budgeting (V1_SPEC §14, ARCHITECTURE.md §10).

The single most important property in this file is that **deterministic evidence
outranks word overlap**. Everything else in the funnel exists to make that
affordable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import RetrievalSettings
from repoatlas.models.context import ContextScope
from repoatlas.models.evidence import (
    EvidenceCandidate,
    EvidenceSet,
    EvidenceSource,
    EvidenceTier,
)
from repoatlas.retrieval import retriever as retriever_module
from repoatlas.retrieval.ranking import apply_budget, deduplicate, fuse
from repoatlas.retrieval.retriever import RetrievalLimits, Retriever
from repoatlas.services.retrieval_service import locate
from repoatlas.services.workspace import Workspace


def candidate(
    path: str,
    *,
    repository: str = "repo",
    start: int = 1,
    end: int = 10,
    score: float = 1.0,
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC,
    source: EvidenceSource = EvidenceSource.EXACT,
    text: str = "text",
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository=repository,
        path=path,
        line_start=start,
        line_end=end,
        text=text,
        score=score,
        tier=tier,
        source=source,
    )


# --- the precedence guarantee -------------------------------------------------


def test_word_overlap_never_outranks_a_deterministic_match() -> None:
    """A strong full-text score does not beat an exact identifier. Ever."""
    lexical = candidate(
        "similar.py", score=0.99, tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.LEXICAL
    )
    exact = candidate("exact.py", score=0.01)

    # Lexical appears in three lists and exact in one, so RRF alone would put
    # lexical first.
    ordered = fuse([[lexical], [lexical], [lexical], [exact]])

    assert [c.path for c in ordered] == ["exact.py", "similar.py"]
    assert ordered[0].tier is EvidenceTier.DETERMINISTIC


@pytest.mark.parametrize(
    "source",
    [EvidenceSource.EXACT, EvidenceSource.SYMBOL, EvidenceSource.GRAPH],
)
def test_every_deterministic_source_outranks_word_overlap(source: EvidenceSource) -> None:
    deterministic = candidate("fact.py", score=0.0, source=source)
    lexical = candidate(
        "guess.py", score=1000.0, tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.LEXICAL
    )
    ordered = fuse([[lexical] * 5, [deterministic]])
    assert ordered[0].path == "fact.py"


def test_a_span_found_by_two_retrievers_takes_its_strongest_claim() -> None:
    associative = candidate(
        "shared.py", tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.LEXICAL
    )
    deterministic = candidate("shared.py", source=EvidenceSource.EXACT)
    ordered = fuse([[associative], [deterministic]])
    assert len(ordered) == 1
    assert ordered[0].tier is EvidenceTier.DETERMINISTIC


def test_the_primary_boost_competes_inside_a_tier_not_across_one() -> None:
    primary_lexical = candidate(
        "a.py",
        repository="primary",
        tier=EvidenceTier.ASSOCIATIVE,
        source=EvidenceSource.LEXICAL,
    )
    other_exact = candidate("b.py", repository="other")
    ordered = fuse([[primary_lexical], [other_exact]], primary_repository="primary")
    assert ordered[0].repository == "other"


def test_the_primary_repository_wins_among_equals() -> None:
    """Primary scope is a bias, not a wall (V1_SPEC §6)."""
    here = candidate("a.py", repository="primary")
    there = candidate("b.py", repository="other")
    ordered = fuse([[there], [here]], primary_repository="primary")
    assert ordered[0].repository == "primary"


# --- fusion counts agreement, not volume ----------------------------------------


def test_repeating_one_span_inside_a_list_does_not_buy_rank() -> None:
    """RRF ranks a *span*, and a span has one rank per retriever, not many.

    A file whose lines cannot be split — a minified bundle, a generated JSON blob,
    a one-line lockfile — becomes many chunks that all carry the same line range.
    If each of them added its own reciprocal rank, the fused score would measure
    how large the file is rather than how strongly the retrievers agree about it,
    and content-free generated data would outrank the real answer.
    """
    noise = [
        candidate(
            "generated.json",
            start=1,
            end=1,
            text=f"chunk {i}",
            tier=EvidenceTier.ASSOCIATIVE,
            source=EvidenceSource.LEXICAL,
        )
        for i in range(12)
    ]
    answer = candidate(
        "config.py",
        start=10,
        end=20,
        tier=EvidenceTier.ASSOCIATIVE,
        source=EvidenceSource.LEXICAL,
    )

    ordered = fuse([[answer, *noise]])

    assert [c.path for c in ordered] == ["config.py", "generated.json"]


def test_a_span_ranked_once_per_list_still_gains_from_agreement() -> None:
    """Collapsing within a list must not collapse *across* lists."""
    agreed = candidate("both.py", tier=EvidenceTier.ASSOCIATIVE)
    alone = candidate("one.py", tier=EvidenceTier.ASSOCIATIVE)

    ordered = fuse([[alone, agreed], [agreed]])

    assert [c.path for c in ordered] == ["both.py", "one.py"]


def test_distinct_spans_of_one_file_each_keep_their_own_rank() -> None:
    """Different line ranges are different evidence and are not collapsed."""
    first = candidate("a.py", start=1, end=10, tier=EvidenceTier.ASSOCIATIVE)
    second = candidate("a.py", start=40, end=50, tier=EvidenceTier.ASSOCIATIVE)

    ordered = fuse([[first, second]])

    assert [(c.line_start, c.line_end) for c in ordered] == [(1, 10), (40, 50)]


# --- deduplication -------------------------------------------------------------


def test_a_contained_span_is_dropped() -> None:
    outer = candidate("f.py", start=1, end=40)
    inner = candidate("f.py", start=10, end=20)
    assert [c.line_start for c in deduplicate([outer, inner])] == [1]


def test_a_mostly_overlapping_span_is_dropped() -> None:
    first = candidate("f.py", start=1, end=40)
    second = candidate("f.py", start=30, end=60)  # 11 of 31 lines shared
    third = candidate("f.py", start=35, end=45)  # 6 of 11 shared with the first two
    kept = deduplicate([first, second, third])
    assert [(c.line_start, c.line_end) for c in kept] == [(1, 40), (30, 60)]


def test_the_same_lines_in_another_file_are_kept() -> None:
    assert len(deduplicate([candidate("a.py"), candidate("b.py")])) == 2


def test_deduplication_never_invents_a_merged_range() -> None:
    """A citation must point at text that was actually retrieved (V1_SPEC §11)."""
    kept = deduplicate([candidate("f.py", start=1, end=40), candidate("f.py", start=5, end=90)])
    assert all((c.line_start, c.line_end) in {(1, 40), (5, 90)} for c in kept)


# --- budgeting -----------------------------------------------------------------


def test_the_item_cap_is_hard() -> None:
    kept, truncated, _ = apply_budget(
        [candidate(f"f{i}.py") for i in range(50)], max_items=5, token_budget=10**6
    )
    assert len(kept) == 5
    assert truncated


def test_the_token_budget_is_hard() -> None:
    big = [candidate(f"f{i}.py", text="x" * 4000) for i in range(10)]
    kept, truncated, tokens = apply_budget(big, max_items=100, token_budget=2500)
    assert truncated
    assert tokens <= 2500
    assert len(kept) < 10


def test_one_oversized_candidate_still_yields_evidence() -> None:
    """An empty answer would be worse than one candidate over budget."""
    kept, truncated, _ = apply_budget(
        [candidate("huge.py", text="x" * 100_000)], max_items=10, token_budget=10
    )
    assert len(kept) == 1
    assert not truncated


def test_nothing_is_truncated_when_everything_fits() -> None:
    kept, truncated, _ = apply_budget(
        [candidate("a.py"), candidate("b.py")], max_items=10, token_budget=10**6
    )
    assert len(kept) == 2
    assert not truncated


# --- limits ---------------------------------------------------------------------


def test_limits_come_from_settings() -> None:
    limits = RetrievalLimits.from_settings(RetrievalSettings(final=3, graph_hops=1))
    assert limits.final == 3
    assert limits.graph_hops == 1


def test_widening_keeps_the_biases(initialized: Workspace) -> None:
    base = RetrievalLimits.from_settings(RetrievalSettings(primary_boost=0.9, graph_hops=3))
    wide = base.widened(per_source=40, final=18, token_budget=8000)
    assert (wide.primary_boost, wide.graph_hops) == (0.9, 3)
    assert (wide.per_source, wide.final, wide.token_budget) == (40, 18, 8000)


# --- the funnel end to end -------------------------------------------------------


def test_an_environment_variable_is_found_across_every_language(
    initialized: Workspace, workspace_root: Path
) -> None:
    deep = workspace_root / "python-service" / "src" / "deep" / "nested" / "path"
    context = initialized.resolver().resolve(deep)
    evidence = locate(initialized, "Where is SERVICE_HOST configured?", context)

    located = {(c.repository, c.path) for c in evidence.candidates}
    assert ("python-service", "src/config.py") in located
    assert ("deployment", "service/init.sls") in located
    assert all(c.tier is EvidenceTier.DETERMINISTIC for c in evidence.candidates)


def test_the_evidence_set_is_bounded(initialized: Workspace) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "SERVICE_HOST configuration service", context)
    assert len(evidence.candidates) <= initialized.settings.retrieval.final


def _stage_detail(evidence: EvidenceSet, name: str) -> str:
    assert evidence.trace is not None
    return next(stage.detail for stage in evidence.trace.stages if stage.name == name)


def test_a_named_term_that_exists_nowhere_yields_no_evidence(
    initialized: Workspace,
) -> None:
    """Word overlap is not an answer to a question about a specific identifier."""
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "ZZZ_NO_SUCH_TOKEN_QQQ", context)
    assert evidence.candidates == ()
    assert "suppressed" in _stage_detail(evidence, "lexical")


def test_prose_questions_still_reach_the_lexical_stage(initialized: Workspace) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "where does startup configuration come from", context)
    assert "suppressed" not in _stage_detail(evidence, "lexical")


def test_graph_expansion_reaches_another_repository_through_a_typed_edge(
    initialized: Workspace, workspace_root: Path
) -> None:
    context = initialized.resolver().resolve(workspace_root / "python-service")
    evidence = locate(initialized, "SERVICE_HOST", context)
    assert {c.repository for c in evidence.candidates} > {"python-service"}


def test_expansion_is_bounded_by_hops(initialized: Workspace, workspace_root: Path) -> None:
    context = initialized.resolver().resolve(workspace_root / "python-service")
    shallow = Retriever(
        search=initialized.search,
        knowledge=initialized.knowledge,
        limits=RetrievalLimits(graph_hops=0),
    ).retrieve("SERVICE_HOST", context)
    deep = initialized.retriever().retrieve("SERVICE_HOST", context)

    assert shallow.trace is not None and deep.trace is not None
    assert shallow.trace.graph_nodes_visited < deep.trace.graph_nodes_visited


def test_expansion_is_bounded_by_fanout(initialized: Workspace, workspace_root: Path) -> None:
    context = initialized.resolver().resolve(workspace_root / "python-service")
    narrow = Retriever(
        search=initialized.search,
        knowledge=initialized.knowledge,
        limits=RetrievalLimits(graph_fanout=1),
    ).retrieve("SERVICE_HOST", context)
    wide = initialized.retriever().retrieve("SERVICE_HOST", context)

    assert narrow.trace is not None and wide.trace is not None
    assert narrow.trace.graph_nodes_visited < wide.trace.graph_nodes_visited


def test_global_scope_searches_once_without_a_primary_bias(initialized: Workspace) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    assert context.scope is ContextScope.GLOBAL
    evidence = locate(initialized, "SERVICE_HOST", context)
    assert evidence.primary_repository is None
    assert evidence.candidates


# --- instrumentation (V1_SPEC §20) ------------------------------------------------


def test_every_stage_is_instrumented(initialized: Workspace) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "SERVICE_HOST", context)
    trace = evidence.trace
    assert trace is not None
    assert [stage.name for stage in trace.stages] == [
        "exact",
        "file",
        "lexical",
        "symbol",
        "graph",
        "fuse",
        "dedup",
        "precedence",
        "budget",
    ]
    assert trace.total_ms >= 0.0
    assert trace.token_estimate > 0


def test_the_trace_reports_the_tier_split(initialized: Workspace) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "SERVICE_HOST", context)
    trace = evidence.trace
    assert trace is not None
    assert trace.deterministic_candidates + trace.associative_candidates == len(evidence.candidates)


def test_the_trace_never_changes_the_answer(initialized: Workspace) -> None:
    """Instrumentation is an observation, never an input (models/telemetry.py)."""
    context = initialized.resolver().resolve(initialized.root or Path())
    first = locate(initialized, "SERVICE_HOST", context)
    second = locate(initialized, "SERVICE_HOST", context)
    assert [c.location for c in first.candidates] == [c.location for c in second.candidates]


def test_the_global_link_pass_is_instrumented(initialized: Workspace) -> None:
    """The stages are the phases: five that compute, then the one that commits.

    ``swap`` is last and alone because it is the only stage that writes. The old
    shape opened with a ``reset`` that deleted the previous graph before any of the
    work — which is exactly the failure window this ordering closes
    (ARCHITECTURE.md §9).
    """
    result = initialized.sync_engine().link_global()
    trace = result.trace
    assert trace is not None
    assert [stage.name for stage in trace.stages] == [
        "index",
        "resolve",
        "uses",
        "persistence",
        "dependencies",
        "swap",
    ]
    assert trace.files_indexed > 0
    assert trace.claims_considered > 0


# --- the guarantee at the final boundary (V1_SPEC §14) --------------------------
#
# The tests above prove that `fuse` orders by tier. These prove the property that
# actually matters: whatever any stage does, an `EvidenceSet` handed to a caller
# never places associative evidence above deterministic evidence. Between fusion
# and the caller sit deduplication and two hard budgets, and a guarantee that
# lives inside one function is not a guarantee about the system.

QUESTIONS = (
    "SERVICE_HOST",
    "Where is SERVICE_HOST configured?",
    "how does the service load its configuration",
    "what happens when the deploy script runs",
    "load_config",
    "'SERVICE_TIMEOUT'",
    "which repository provides common-lib",
    "pillar",
)


def _tiers(evidence: EvidenceSet) -> list[EvidenceTier]:
    return [c.tier for c in evidence.candidates]


def assert_deterministic_first(evidence: EvidenceSet) -> None:
    """No associative candidate may precede a deterministic one."""
    tiers = _tiers(evidence)
    assert tiers == sorted(tiers), [
        (c.tier.name, c.source.value, c.location) for c in evidence.candidates
    ]


def inverted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the stage before the boundary hand it the tiers deliberately inverted."""

    def invert(candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        kept = deduplicate(candidates)
        associative = [c for c in kept if c.tier is EvidenceTier.ASSOCIATIVE]
        deterministic = [c for c in kept if c.tier is EvidenceTier.DETERMINISTIC]
        return [*associative, *deterministic]

    monkeypatch.setattr(retriever_module, "deduplicate", invert)


def _retriever(workspace: Workspace, **overrides) -> Retriever:
    return Retriever(
        search=workspace.search,
        knowledge=workspace.knowledge,
        limits=overrides.pop("limits", RetrievalLimits.from_settings(workspace.settings.retrieval)),
    )


@pytest.mark.parametrize("question", QUESTIONS)
def test_the_returned_evidence_set_is_always_tier_ordered(
    initialized: Workspace, question: str
) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    assert_deterministic_first(initialized.retriever().retrieve(question, context))


#: A question whose evidence set is genuinely mixed, so inverting the tiers is
#: something the boundary has to undo rather than a no-op. The identifier exists,
#: which keeps the lexical stage open and supplies the deterministic half; the
#: prose word adds full-text leads. It has more deterministic candidates than the
#: three-slot budget below, so a hostile order has something to displace.
MIXED = "load_config service"


def test_an_inverted_order_is_corrected(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stage before the boundary can break the V1 guarantee, even deliberately."""
    context = initialized.resolver().resolve(initialized.root or Path())
    honest = initialized.retriever().retrieve(MIXED, context)
    assert honest.deterministic and len(honest.candidates) > len(honest.deterministic)

    inverted(monkeypatch)
    hostile = _retriever(initialized).retrieve(MIXED, context)
    assert_deterministic_first(hostile)
    assert {c.location for c in hostile.deterministic} == {c.location for c in honest.deterministic}


def test_an_inverted_order_cannot_spend_the_budget_on_associative_evidence(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starving the deterministic half out of a hard budget would displace it just
    as effectively as ranking it lower."""
    context = initialized.resolver().resolve(initialized.root or Path())
    limits = RetrievalLimits.from_settings(
        initialized.settings.retrieval.model_copy(update={"final": 3})
    )
    honest = initialized.retriever().retrieve(MIXED, context)
    inverted(monkeypatch)
    hostile = _retriever(initialized, limits=limits).retrieve(MIXED, context)
    assert len(hostile.candidates) == 3

    # Every deterministic candidate that fits still fits. A hostile order would
    # have spent all three slots on leads and dropped the facts entirely.
    kept = [c for c in hostile.candidates if c.tier is EvidenceTier.DETERMINISTIC]
    assert len(kept) == min(3, len(honest.deterministic))
    assert {c.location for c in kept} == {c.location for c in honest.deterministic[:3]}
    assert_deterministic_first(hostile)


def test_the_trace_records_that_the_order_was_restored(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent correction is a correction nobody can audit."""
    context = initialized.resolver().resolve(initialized.root or Path())
    inverted(monkeypatch)
    evidence = _retriever(initialized).retrieve(MIXED, context)
    assert evidence.trace is not None
    stage = next(stage for stage in evidence.trace.stages if stage.name == "precedence")
    assert "tier order restored" in stage.detail


def test_locate_exposes_the_same_ordering_as_the_retriever(initialized: Workspace) -> None:
    """The guarantee holds at the service boundary the CLI actually calls."""
    context = initialized.resolver().resolve(initialized.root or Path())
    assert_deterministic_first(locate(initialized, "SERVICE_HOST", context))
