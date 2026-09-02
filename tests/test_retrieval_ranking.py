"""Ranking, tiering, expansion and budgeting (V1_SPEC §14, ARCHITECTURE.md §10).

The single most important property in this file is that **deterministic evidence
outranks semantic similarity**. Everything else in the funnel exists to make that
affordable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import RetrievalSettings
from repoatlas.models.context import ContextScope
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSource, EvidenceTier
from repoatlas.retrieval.ranking import NoopReranker, apply_budget, deduplicate, fuse
from repoatlas.retrieval.retriever import RetrievalLimits, Retriever
from repoatlas.services.query_service import locate
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


def test_semantic_similarity_never_outranks_a_deterministic_match() -> None:
    """A cosine of 0.99 does not beat an exact identifier. Ever."""
    semantic = candidate(
        "similar.py", score=0.99, tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.SEMANTIC
    )
    exact = candidate("exact.py", score=0.01)

    # Semantic appears in three lists and exact in one, so RRF alone would put
    # semantic first.
    ordered = fuse([[semantic], [semantic], [semantic], [exact]])

    assert [c.path for c in ordered] == ["exact.py", "similar.py"]
    assert ordered[0].tier is EvidenceTier.DETERMINISTIC


@pytest.mark.parametrize(
    "source",
    [EvidenceSource.EXACT, EvidenceSource.SYMBOL, EvidenceSource.GRAPH],
)
def test_every_deterministic_source_outranks_semantics(source: EvidenceSource) -> None:
    deterministic = candidate("fact.py", score=0.0, source=source)
    semantic = candidate(
        "guess.py", score=1000.0, tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.SEMANTIC
    )
    ordered = fuse([[semantic] * 5, [deterministic]])
    assert ordered[0].path == "fact.py"


def test_lexical_recall_and_semantics_compete_on_score() -> None:
    """Only *deterministic* evidence is protected; recall and similarity are peers."""
    lexical = candidate("lex.py", tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.LEXICAL)
    semantic = candidate("sem.py", tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.SEMANTIC)
    ordered = fuse([[semantic], [semantic], [lexical]])
    assert ordered[0].path == "sem.py"


def test_a_span_found_by_two_retrievers_takes_its_strongest_claim() -> None:
    associative = candidate(
        "shared.py", tier=EvidenceTier.ASSOCIATIVE, source=EvidenceSource.SEMANTIC
    )
    deterministic = candidate("shared.py", source=EvidenceSource.EXACT)
    ordered = fuse([[associative], [deterministic]])
    assert len(ordered) == 1
    assert ordered[0].tier is EvidenceTier.DETERMINISTIC


def test_the_primary_boost_competes_inside_a_tier_not_across_one() -> None:
    primary_semantic = candidate(
        "a.py",
        repository="primary",
        tier=EvidenceTier.ASSOCIATIVE,
        source=EvidenceSource.SEMANTIC,
    )
    other_exact = candidate("b.py", repository="other")
    ordered = fuse([[primary_semantic], [other_exact]], primary_repository="primary")
    assert ordered[0].repository == "other"


def test_the_primary_repository_wins_among_equals() -> None:
    """Primary scope is a bias, not a wall (V1_SPEC §6)."""
    here = candidate("a.py", repository="primary")
    there = candidate("b.py", repository="other")
    ordered = fuse([[there], [here]], primary_repository="primary")
    assert ordered[0].repository == "primary"


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


# --- the reranker seam ----------------------------------------------------------


def test_the_noop_reranker_preserves_order() -> None:
    given = [candidate("a.py"), candidate("b.py")]
    assert NoopReranker().rerank("question", given) == given


def test_a_custom_reranker_is_used_without_changing_a_caller(initialized: Workspace) -> None:
    """The V2 slot: swapping the reranker touches nothing else (ARCHITECTURE §14)."""

    class _Reverser:
        def rerank(
            self, question: str, candidates: list[EvidenceCandidate]
        ) -> list[EvidenceCandidate]:
            return list(reversed(candidates))

    context = initialized.resolver().resolve(initialized.root or Path())
    plain = initialized.retriever().retrieve("SERVICE_HOST", context)
    reversed_retriever = Retriever(
        search=initialized.search,
        knowledge=initialized.knowledge,
        vectors=initialized.vectors,
        embeddings=initialized.embeddings(),
        reranker=_Reverser(),
        limits=RetrievalLimits.from_settings(initialized.settings.retrieval),
    )
    flipped = reversed_retriever.retrieve("SERVICE_HOST", context)
    assert plain.candidates and flipped.candidates
    assert plain.candidates[0].location != flipped.candidates[0].location


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


def test_a_named_term_that_exists_nowhere_yields_no_evidence(
    initialized: Workspace,
) -> None:
    """Similarity is not an answer to a question about a specific identifier."""
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "ZZZ_NO_SUCH_TOKEN_QQQ", context)
    assert evidence.candidates == ()
    assert evidence.trace is not None
    assert "suppressed" in evidence.trace.semantic_detail


def test_prose_questions_still_reach_the_semantic_stage(initialized: Workspace) -> None:
    context = initialized.resolver().resolve(initialized.root or Path())
    evidence = locate(initialized, "where does startup configuration come from", context)
    assert evidence.trace is not None
    assert "suppressed" not in evidence.trace.semantic_detail


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
        vectors=initialized.vectors,
        embeddings=initialized.embeddings(),
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
        vectors=initialized.vectors,
        embeddings=initialized.embeddings(),
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
        "lexical",
        "symbol",
        "semantic",
        "graph",
        "fuse",
        "dedup",
        "rerank",
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
    result = initialized.sync_engine().link_global()
    trace = result.trace
    assert trace is not None
    assert [stage.name for stage in trace.stages] == [
        "reset",
        "index",
        "resolve",
        "dependencies",
        "write",
    ]
    assert trace.files_indexed > 0
    assert trace.claims_considered > 0


def test_retrieval_without_embeddings_still_works(initialized: Workspace) -> None:
    """Exact search is mandatory; semantic search is the aid (V1_SPEC §14)."""
    context = initialized.resolver().resolve(initialized.root or Path())
    without = Retriever(
        search=initialized.search,
        knowledge=initialized.knowledge,
        vectors=None,
        embeddings=None,
    ).retrieve("SERVICE_HOST", context)
    assert without.candidates
    assert without.trace is not None
    assert without.trace.semantic_used is False
