"""The retrieval funnel (ARCHITECTURE.md §10).

Turns a question plus a ``RepositoryContext`` into a bounded ``EvidenceSet``. No
LLM is involved at any stage — this is the layer that makes the trust rule
affordable, and it is why ``locate`` works with no model installed.

The funnel's governing rule is V1_SPEC §14: **vector search must not replace exact
search.** Semantic retrieval is one more source feeding fusion, and every
candidate carries an :class:`EvidenceTier` that fusion sorts on before it looks at
any score. Exact identifiers, exact error strings, exact symbol names, environment
variables and graph-supported relationships are therefore unreachable by
similarity — an embedding neighbour can only ever be considered after all of them.

Every stage records its cost and its effect on the candidate count, so retrieval
performance can be measured later without changing anything structural
(V1_SPEC §20).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from repoatlas.config.settings import RetrievalSettings
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.evidence import (
    EvidenceCandidate,
    EvidenceSet,
    EvidenceSource,
    EvidenceTier,
)
from repoatlas.models.parsing import EdgeType
from repoatlas.models.telemetry import RetrievalTrace, StageTiming, Stopwatch
from repoatlas.retrieval.query import analyze
from repoatlas.retrieval.ranking import (
    NoopReranker,
    Reranker,
    apply_budget,
    deduplicate,
    enforce_tier_order,
    fuse,
)
from repoatlas.storage.protocols import ChunkHit, VectorHit
from repoatlas.storage.sqlite.search import SqliteSearchIndex
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore
from repoatlas.storage.sqlite.vectors import SqliteVectorIndex

#: Edge types graph expansion may traverse.
#:
#: These are *relationship* edges. ``DEFINES`` and ``CONTAINS`` are deliberately
#: absent even though they are perfectly good facts: they are containment, so
#: traversing them turns any file node into a fan-out over every symbol it
#: declares, and two hops of that buries the relationship the question was about
#: under an inventory of the neighbourhood.
_EXPANSION_EDGES = (
    EdgeType.READS,
    EdgeType.WRITES,
    EdgeType.EXPORTS,
    EdgeType.CONFIGURES,
    EdgeType.DEPENDS_ON,
    EdgeType.IMPORTS,
    EdgeType.REFERENCES,
    EdgeType.EXECUTES,
    EdgeType.SOURCES,
    EdgeType.INCLUDES,
    EdgeType.REQUIRES,
    EdgeType.WATCHES,
    EdgeType.MANAGES,
    EdgeType.GENERATES,
)

#: Edge types a traversal may follow **out of one repository into another**.
#:
#: Every one of these is produced by the linker's global pass from an exact match
#: — a resolved package name, a resolved path, a resolved import — so a
#: cross-repository answer always has a structural reason (V1_SPEC §9). A `CALLS`
#: or `DEFINES` edge is deliberately absent: those are meaningful within a
#: repository but would let a traversal wander between repositories on nothing
#: stronger than a shared symbol name.
_CROSS_REPOSITORY_EDGES = frozenset(
    {
        EdgeType.DEPENDS_ON,
        EdgeType.REFERENCES,
        EdgeType.EXECUTES,
        EdgeType.SOURCES,
        EdgeType.IMPORTS,
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalLimits:
    """Hard caps and biases. The evidence set is bounded by count and by tokens."""

    per_source: int = 30
    final: int = 12
    token_budget: int = 6000
    graph_hops: int = 2
    graph_fanout: int = 25
    graph_seeds: int = 20
    primary_boost: float = 0.5
    semantic_limit: int = 30
    semantic_floor: float = 0.25

    @classmethod
    def from_settings(cls, settings: RetrievalSettings) -> RetrievalLimits:
        return cls(
            per_source=settings.per_source,
            final=settings.final,
            token_budget=settings.token_budget,
            graph_hops=settings.graph_hops,
            graph_fanout=settings.graph_fanout,
            graph_seeds=settings.graph_seeds,
            primary_boost=settings.primary_boost,
            semantic_limit=settings.semantic_limit,
            semantic_floor=settings.semantic_floor,
        )

    def widened(self, *, per_source: int, final: int, token_budget: int) -> RetrievalLimits:
        """The same biases with larger bounds, for ``debug`` (V1_SPEC §16)."""
        return replace(self, per_source=per_source, final=final, token_budget=token_budget)


class Retriever:
    """Builds bounded evidence sets from deterministic signals, ranked by tier."""

    def __init__(
        self,
        *,
        search: SqliteSearchIndex,
        knowledge: SqliteKnowledgeStore,
        vectors: SqliteVectorIndex | None = None,
        embeddings: EmbeddingModel | None = None,
        reranker: Reranker | None = None,
        limits: RetrievalLimits | None = None,
    ) -> None:
        self._search = search
        self._knowledge = knowledge
        self._vectors = vectors
        self._embeddings = embeddings
        self._reranker = reranker or NoopReranker()
        self._limits = limits or RetrievalLimits()

    def retrieve(
        self, question: str, context: RepositoryContext, *, literal_phrase: bool = False
    ) -> EvidenceSet:
        """Build a bounded evidence set.

        ``literal_phrase`` additionally searches the whole question verbatim,
        which is how ``debug`` finds an unquoted error string embedded in source
        (V1_SPEC §16).
        """
        stages: list[StageTiming] = []
        analysis = analyze(question)

        # Scoping happens before scoring, so we never search every repository for
        # every question (V1_SPEC §20).
        primary_ids = list(context.repository_ids)
        primary_name = context.primary.name if context.primary else None
        scopes = self._scopes(context, primary_ids)

        ranked_lists: list[list[EvidenceCandidate]] = []

        literal_terms = list(analysis.literal_terms)
        # Suppression is decided on the *definite* subset: a bare dashed token in
        # prose is searched exactly but never gets to declare a question
        # unanswerable. See QueryAnalysis.definite_terms.
        definite_terms = list(analysis.definite_terms)
        if literal_phrase:
            phrase = question.strip().strip("?.!").strip()
            if phrase and phrase not in literal_terms:
                literal_terms.insert(0, phrase)
            if phrase and phrase not in definite_terms:
                definite_terms.insert(0, phrase)

        exact = self._stage_exact(literal_terms, scopes, stages)
        if exact:
            ranked_lists.append(exact)

        # A question that names something specific and does not find it is not
        # answered by anything associative. Decided once, here, and honoured by
        # every stage that follows. See _suppress_associative.
        suppressed = self._suppress_associative(definite_terms, exact)

        lexical = self._stage_lexical(question, scopes, stages, suppressed=suppressed)
        ranked_lists.extend(lexical)

        symbols, entity_ids = self._stage_symbols(
            question, analysis.terms, literal_terms, scopes, stages, suppressed=suppressed
        )
        if symbols:
            ranked_lists.append(symbols)

        semantic, semantic_used, semantic_detail = self._stage_semantic(
            question,
            primary_ids if context.scope is not ContextScope.GLOBAL else None,
            stages,
            suppressed=suppressed,
        )
        if semantic:
            ranked_lists.append(semantic)

        graph, visited = self._stage_graph(entity_ids, stages)
        if graph:
            ranked_lists.append(graph)

        incoming = sum(len(ranked) for ranked in ranked_lists)

        if not ranked_lists:
            return EvidenceSet(
                question=question,
                scope=str(context.scope),
                primary_repository=primary_name,
                trace=RetrievalTrace(
                    stages=tuple(stages),
                    literal_terms=tuple(literal_terms),
                    scoped_repositories=len(primary_ids),
                    semantic_used=semantic_used,
                    semantic_detail=semantic_detail,
                ),
            )

        with Stopwatch("fuse", candidates_in=incoming) as watch:
            fused = fuse(
                ranked_lists,
                primary_repository=primary_name,
                primary_boost=self._limits.primary_boost,
            )
            watch.candidates_out = len(fused)
        stages.append(_recorded(watch))

        with Stopwatch("dedup", candidates_in=len(fused)) as watch:
            deduped = deduplicate(fused)
            watch.candidates_out = len(deduped)
        stages.append(_recorded(watch))

        with Stopwatch("rerank", candidates_in=len(deduped)) as watch:
            # The tier order is re-imposed on whatever comes back. The reranker is
            # a seam for code that does not exist yet, and the guarantee of
            # V1_SPEC §14 must hold at this boundary rather than depend on every
            # future implementation choosing to honour it.
            reordered = self._reranker.rerank(question, deduped)
            reranked = enforce_tier_order(reordered)
            watch.candidates_out = len(reranked)
            watch.detail = type(self._reranker).__name__
            if [c.tier for c in reordered] != [c.tier for c in reranked]:
                watch.detail += " (tier order restored)"
        stages.append(_recorded(watch))

        with Stopwatch("budget", candidates_in=len(reranked)) as watch:
            bounded, truncated, tokens = apply_budget(
                reranked, max_items=self._limits.final, token_budget=self._limits.token_budget
            )
            watch.candidates_out = len(bounded)
            watch.detail = f"{tokens} tokens" + (", truncated" if truncated else "")
        stages.append(_recorded(watch))

        return EvidenceSet(
            question=question,
            candidates=tuple(bounded),
            scope=str(context.scope),
            primary_repository=primary_name,
            truncated=truncated,
            trace=RetrievalTrace(
                stages=tuple(stages),
                literal_terms=tuple(literal_terms),
                scoped_repositories=len(primary_ids),
                semantic_used=semantic_used,
                semantic_detail=semantic_detail,
                graph_hops=self._limits.graph_hops,
                graph_nodes_visited=visited,
                deterministic_candidates=sum(
                    1 for c in bounded if c.tier is EvidenceTier.DETERMINISTIC
                ),
                associative_candidates=sum(
                    1 for c in bounded if c.tier is EvidenceTier.ASSOCIATIVE
                ),
                token_estimate=tokens,
            ),
        )

    # -- stages --------------------------------------------------------------

    def _stage_exact(
        self,
        literal_terms: list[str],
        scopes: list[list[int] | None],
        stages: list[StageTiming],
    ) -> list[EvidenceCandidate]:
        """Literal terms, verified by substring. Deterministic by construction."""
        with Stopwatch("exact") as watch:
            hits: list[EvidenceCandidate] = []
            for term in literal_terms:
                for scope in scopes:
                    hits.extend(
                        self._chunk_candidate(
                            hit,
                            EvidenceSource.EXACT,
                            EvidenceTier.DETERMINISTIC,
                            f"contains {term}",
                        )
                        for hit in self._search.search_literal(term, scope, self._limits.per_source)
                    )
            watch.candidates_out = len(hits)
            watch.detail = f"{len(literal_terms)} literal term(s)"
        stages.append(_recorded(watch))
        return hits

    def _stage_lexical(
        self,
        question: str,
        scopes: list[list[int] | None],
        stages: list[StageTiming],
        *,
        suppressed: str | None = None,
    ) -> list[list[EvidenceCandidate]]:
        """FTS recall over chunk text. Recall, not proof, so it is associative."""
        lists: list[list[EvidenceCandidate]] = []
        with Stopwatch("lexical") as watch:
            if suppressed is not None:
                watch.detail = suppressed
                scopes = []
            for scope in scopes:
                hits = self._search.search_text(question, scope, self._limits.per_source)
                if hits:
                    lists.append(
                        [
                            self._chunk_candidate(
                                hit, EvidenceSource.LEXICAL, EvidenceTier.ASSOCIATIVE
                            )
                            for hit in hits
                        ]
                    )
            watch.candidates_out = sum(len(entries) for entries in lists)
        stages.append(_recorded(watch))
        return lists

    def _stage_symbols(
        self,
        question: str,
        terms: tuple[str, ...],
        literal_terms: list[str],
        scopes: list[list[int] | None],
        stages: list[StageTiming],
        *,
        suppressed: str | None = None,
    ) -> tuple[list[EvidenceCandidate], list[int]]:
        """Entity lookup. An exact name match is a fact; a fuzzy one is a lead.

        Exact-name lookup runs *first* and independently of FTS, so an environment
        variable, package or symbol the question spells out is found regardless of
        how the tokenizer treated it. **Only those hits become graph seeds**, which
        is what makes ``env:SERVICE_HOST`` reachable as the hub connecting a service
        that reads it to the Salt state that sets it (V1_SPEC §10).

        The full-text half is recall: an entity whose name merely shares a token
        with the question. It is returned as `ASSOCIATIVE`, and it is deliberately
        **not** a graph seed. Expansion labels everything it reaches `DETERMINISTIC`
        — correctly, since an edge is a fact — so seeding it from a fuzzy match
        would launder a guess into the tier that outranks every genuine lead.
        ARCHITECTURE §10 specifies expansion "from the exact entity hits" for
        exactly this reason.
        """
        exact_names = {term.lower() for term in terms}
        with Stopwatch("symbol") as watch:
            candidates: list[EvidenceCandidate] = []
            #: Seeds for graph expansion. Exact name matches only.
            entity_ids: list[int] = []
            #: Everything already returned, exact or fuzzy, for deduplication.
            seen_entities: set[int] = set()
            # Under suppression only the terms the user actually named may be
            # looked up. Words lifted out of the surrounding prose are recall, and
            # an exact-name hit on one of them would enter as DETERMINISTIC — the
            # strongest tier — for a question whose subject does not exist.
            named = list(literal_terms) if suppressed is not None else [*literal_terms, *terms]
            for scope in scopes:
                for hit in self._search.find_entities_by_name(
                    named, scope, self._limits.per_source
                ):
                    if hit.entity_id in seen_entities:
                        continue
                    seen_entities.add(hit.entity_id)
                    entity_ids.append(hit.entity_id)
                    if hit.path is None or hit.line_start is None:
                        continue
                    candidates.append(
                        EvidenceCandidate(
                            repository=hit.repository,
                            path=hit.path,
                            line_start=hit.line_start,
                            line_end=hit.line_end or hit.line_start,
                            text="",
                            score=hit.score,
                            source=EvidenceSource.SYMBOL,
                            tier=EvidenceTier.DETERMINISTIC,
                            description=f"defines {hit.name}",
                            entity_id=hit.entity_id,
                        )
                    )
            for scope in scopes if suppressed is None else []:
                for hit in self._search.search_entities(
                    question, scope, None, self._limits.per_source
                ):
                    if hit.entity_id in seen_entities:
                        continue
                    seen_entities.add(hit.entity_id)
                    exact = hit.name.lower() in exact_names
                    if exact:
                        # The question named this entity outright; FTS merely found
                        # it first. It is a fact, so it may seed expansion.
                        entity_ids.append(hit.entity_id)
                    if hit.path is None or hit.line_start is None:
                        continue
                    candidates.append(
                        EvidenceCandidate(
                            repository=hit.repository,
                            path=hit.path,
                            line_start=hit.line_start,
                            line_end=hit.line_end or hit.line_start,
                            text="",
                            score=hit.score,
                            source=EvidenceSource.SYMBOL,
                            tier=(
                                EvidenceTier.DETERMINISTIC if exact else EvidenceTier.ASSOCIATIVE
                            ),
                            description=f"defines {hit.name}",
                            entity_id=hit.entity_id,
                        )
                    )
            watch.candidates_out = len(candidates)
            if suppressed is not None:
                watch.detail = suppressed
        stages.append(_recorded(watch))
        return candidates, entity_ids

    @staticmethod
    def _suppress_associative(
        literal_terms: list[str], exact: list[EvidenceCandidate]
    ) -> str | None:
        """Whether every associative source would be answering a different question.

        When the question names literal terms — a quoted string, an environment
        variable, a dotted name, a path — and **none of them exist anywhere in the
        index, the honest answer is that they do not exist**. Offering spans that
        merely read like the name would be plausible, unfounded output, which is
        the failure V1_SPEC §2 exists to prevent.

        This holds for *every* associative source, not only for embeddings. FTS
        recall that matches `@company` when the user asked for
        `@company/absent-918273`, and an entity coincidentally named `retries` when
        the user quoted an error string, are the same mistake as a cosine
        neighbour — and the entity match is worse, because an exact-name lookup
        marks it `DETERMINISTIC` and it then outranks everything.

        A question with no literal terms ("where is deployment configuration
        generated?") is exactly what the associative half of the funnel is for, so
        nothing is suppressed there. One term that *does* exist is likewise enough
        to keep the funnel open: the question is answerable, and the rest of the
        stages are how the surrounding context is found.
        """
        if literal_terms and not exact:
            named = ", ".join(literal_terms[:3])
            return f"suppressed: none of the named terms exist ({named})"
        return None

    def _stage_semantic(
        self,
        question: str,
        repo_ids: list[int] | None,
        stages: list[StageTiming],
        *,
        suppressed: str | None = None,
    ) -> tuple[list[EvidenceCandidate], bool, str]:
        """Vector neighbours. Always associative — a guess about what was meant."""
        with Stopwatch("semantic") as watch:
            if suppressed is not None:
                candidates, used, detail = [], False, suppressed
            else:
                candidates, used, detail = self._semantic_candidates(question, repo_ids)
            watch.candidates_out = len(candidates)
            watch.detail = detail
        stages.append(_recorded(watch))
        return candidates, used, detail

    def _semantic_candidates(
        self, question: str, repo_ids: list[int] | None
    ) -> tuple[list[EvidenceCandidate], bool, str]:
        if self._vectors is None or self._embeddings is None:
            return [], False, "embeddings not configured"

        model_id = self._embeddings.id
        # Checking the index first means a workspace that was never embedded never
        # triggers a model load, so `locate` stays instant and offline.
        if self._vectors.count(model_id) == 0:
            return [], False, f"no vectors stored for {model_id}"

        try:
            vector = self._embeddings.embed_query(question)
        except EmbeddingUnavailable as exc:
            # Never fatal: the deterministic stages are the mandatory ones.
            return [], False, str(exc)
        if not vector:
            return [], False, "empty query vector"

        hits = self._vectors.query(
            vector,
            model_id,
            repo_ids,
            self._limits.semantic_limit,
            self._limits.semantic_floor,
        )
        return (
            [self._vector_candidate(hit) for hit in hits],
            True,
            f"{model_id}: {len(hits)} hit(s)",
        )

    def _stage_graph(
        self, entity_ids: list[int], stages: list[StageTiming]
    ) -> tuple[list[EvidenceCandidate], int]:
        with Stopwatch("graph", candidates_in=len(entity_ids)) as watch:
            candidates, visited = self._expand(entity_ids)
            watch.candidates_out = len(candidates)
            watch.detail = f"{visited} node(s) over {self._limits.graph_hops} hop(s)"
        stages.append(_recorded(watch))
        return candidates, visited

    def _expand(self, entity_ids: list[int]) -> tuple[list[EvidenceCandidate], int]:
        """Bounded traversal from matched entities (ARCHITECTURE.md §10 stage 4).

        Bounded on three axes at once — hop count, fan-out per hop and total nodes
        — because a popular hub such as ``env:SERVICE_HOST`` legitimately touches
        every repository, and an unbounded walk from it would return the world.

        Two hops is the useful minimum, not an arbitrary depth: an interned node
        like an environment variable has no location of its own, so hop one reaches
        the hub and hop two reaches the files on the other side of it. That is what
        makes "where is SERVICE_HOST configured?" answerable from inside a service
        that only reads it.
        """
        if not entity_ids:
            return [], 0

        frontier = entity_ids[: self._limits.graph_seeds]
        visited: set[int] = set(frontier)
        reached: list[int] = []
        reason: dict[int, str] = {}

        for hop in range(max(0, self._limits.graph_hops)):
            if not frontier:
                break
            edges = self._knowledge.expand_neighbors(
                frontier,
                _EXPANSION_EDGES,
                limit=self._limits.graph_fanout * max(1, len(frontier)),
            )
            next_frontier: list[int] = []
            for edge in edges:
                if edge.crosses_repositories and edge.type not in _CROSS_REPOSITORY_EDGES:
                    continue
                for neighbor in (edge.src_id, edge.dst_id):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
                    reached.append(neighbor)
                    reason[neighbor] = f"{edge.type.value.lower()} (hop {hop + 1})"
                if len(next_frontier) >= self._limits.graph_fanout:
                    break
            frontier = next_frontier

        if not reached:
            return [], len(visited)
        return self._knowledge_candidates(reached, reason), len(visited)

    def _knowledge_candidates(
        self, entity_ids: list[int], reason: dict[int, str] | None = None
    ) -> list[EvidenceCandidate]:
        """Describe reached entities, **preserving traversal order**.

        Order is the proximity signal: entities found on the first hop precede
        those found on the second, so rank fusion sees the closer relationship as
        the stronger one. A set-returning query loses that, so it is restored here.
        """
        wanted = entity_ids[: self._limits.per_source]
        position = {entity_id: index for index, entity_id in enumerate(wanted)}
        rows = sorted(
            self._knowledge.describe_entities(wanted),
            key=lambda row: position.get(row.entity_id, len(position)),
        )
        return [
            EvidenceCandidate(
                repository=row.repository,
                path=row.path,
                line_start=row.line_start,
                line_end=row.line_end,
                text=row.text,
                score=1.0,
                source=EvidenceSource.GRAPH,
                tier=EvidenceTier.DETERMINISTIC,
                # The edge that reached it is better evidence than what it
                # declares: the reader asked about a relationship.
                description=(reason or {}).get(row.entity_id) or row.description,
                entity_id=row.entity_id,
            )
            for row in rows
        ]

    # -- helpers -------------------------------------------------------------

    def _scopes(self, context: RepositoryContext, primary_ids: list[int]) -> list[list[int] | None]:
        """Search scopes in priority order.

        In repository scope the primary repository is searched first; other managed
        repositories are reachable, so a justified cross-repository answer is still
        possible (V1_SPEC §6). In global scope there is a single unscoped pass.
        """
        if context.scope is ContextScope.GLOBAL:
            return [None]
        return [primary_ids, None]

    @staticmethod
    def _chunk_candidate(
        hit: ChunkHit,
        source: EvidenceSource,
        tier: EvidenceTier,
        description: str = "",
    ) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository=hit.repository,
            path=hit.path,
            line_start=hit.line_start,
            line_end=hit.line_end,
            text=hit.text,
            score=hit.score,
            source=source,
            tier=tier,
            description=description,
        )

    @staticmethod
    def _vector_candidate(hit: VectorHit) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository=hit.repository,
            path=hit.path,
            line_start=hit.line_start,
            line_end=hit.line_end,
            text=hit.text,
            score=hit.score,
            source=EvidenceSource.SEMANTIC,
            tier=EvidenceTier.ASSOCIATIVE,
            description="semantically similar",
        )


def _recorded(watch: Stopwatch) -> StageTiming:
    assert watch.recorded is not None
    return watch.recorded
