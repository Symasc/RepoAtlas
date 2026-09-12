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

from collections import Counter
from dataclasses import dataclass, replace

from repoatlas.config.settings import RetrievalSettings
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable
from repoatlas.graph.traversal import expand_from_seeds
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.evidence import (
    EvidenceCandidate,
    EvidenceSet,
    EvidenceSource,
    EvidenceTier,
    MatchClass,
)
from repoatlas.models.retrieval import RetrievalOutcome, SuppressionOutcome
from repoatlas.models.telemetry import RetrievalTrace, StageTiming, Stopwatch
from repoatlas.retrieval.query import analyze
from repoatlas.retrieval.ranking import (
    NoopReranker,
    Reranker,
    apply_budget,
    deduplicate,
    enforce_precedence,
    fuse,
)
from repoatlas.storage.protocols import ChunkHit, FileHit, VectorHit
from repoatlas.storage.sqlite.search import SqliteSearchIndex
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore
from repoatlas.storage.sqlite.vectors import SqliteVectorIndex


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


#: The widened bounds ``debug`` retrieves under (V1_SPEC §16).
#:
#: A diagnosis benefits from more surrounding evidence than a direct lookup, so
#: ``debug`` retrieves a wider set within the same token budget. Named here rather
#: than in the service that happens to use it first: it is a property of retrieval,
#: and the caller chooses a documented profile instead of assembling raw limits.
DIAGNOSTIC_LIMITS = RetrievalLimits(per_source=40, final=18, token_budget=8000)

#: What the trace says when the embedding backend could not answer.
#:
#: Fixed text, authored here, and deliberately not derived from the exception.
#: :class:`~repoatlas.embeddings.protocol.EmbeddingUnavailable` carries whatever
#: the backend raised — a loader's ``OSError`` naming a model cache directory, an
#: ``ImportError`` naming a shared object, an HTTP body, a filesystem path — and a
#: stage detail is a **public field of a successful response**, reaching the agent
#: through ``search(include_trace=True)`` and V1's ``locate --json``. Repeating
#: third-party text there discloses host layout that the caller asked nothing
#: about and can act on in no way.
#:
#: It says what RepoAtlas could not do rather than what a backend complained
#: about, so it stays true when the backend is replaced (V1_SPEC §3 makes that a
#: configuration change). It is also distinct from every other semantic detail —
#: "embeddings not configured", "no vectors stored for ...", "empty query vector"
#: and "<model>: n hit(s)" — so the reason the stage produced nothing is still
#: legible, and a failure is never mistaken for a stage that ran and matched
#: nothing.
SEMANTIC_UNAVAILABLE = "semantic retrieval unavailable"


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

        The V1 entry point, unchanged. It is one call to :meth:`retrieve_detailed`
        with everything an ``EvidenceSet`` cannot carry dropped on the floor.
        """
        return self.retrieve_detailed(question, context, literal_phrase=literal_phrase).evidence

    def retrieve_detailed(
        self, question: str, context: RepositoryContext, *, literal_phrase: bool = False
    ) -> RetrievalOutcome:
        """The funnel: one pass, and everything that pass decided.

        The same work :meth:`retrieve` does, returning the suppression decision and
        the exact entity hits that seeded graph expansion alongside the evidence.
        Both were already computed here and both used to survive only as an English
        sentence in a trace field, which is not something a caller can act on
        without re-deriving it — either by parsing prose or by running the funnel a
        second time. Neither is acceptable, so the funnel says what it decided.
        """
        stages: list[StageTiming] = []
        analysis = analyze(question)

        # Scoping happens before scoring, so we never search every repository for
        # every question (V1_SPEC §20).
        primary_ids = list(context.repository_ids)
        primary_name = context.primary.name if context.primary else None
        # The boost keys on identity, not on the display name: standing inside
        # `teamB/common` must not promote evidence from `teamA/common` merely
        # because both directories are called `common` (ARCHITECTURE.md §10).
        primary_slug = context.primary.slug if context.primary else None
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

        # File identity is searched *before* suppression is decided, because it is
        # one of the two ways a named term can be proven to exist. Deciding
        # suppression on content alone is what made RepoAtlas answer "no indexed
        # evidence matches" for `src/config.py` while holding that exact file.
        files, proven = self._stage_files([*literal_terms, *analysis.terms], scopes, stages)
        if files:
            ranked_lists.append(files)

        # A question that names something specific and does not find it is not
        # answered by anything associative. Decided once, here, and honoured by
        # every stage that follows. See _suppress_associative.
        suppressed = self._suppress_associative(definite_terms, exact, proven)

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
            return RetrievalOutcome(
                evidence=EvidenceSet(
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
                ),
                suppression=suppressed,
                seed_entity_ids=tuple(entity_ids),
            )

        with Stopwatch("fuse", candidates_in=incoming) as watch:
            fused = fuse(
                ranked_lists,
                primary_repository=primary_slug or primary_name,
                primary_boost=self._limits.primary_boost,
            )
            watch.candidates_out = len(fused)
        stages.append(_recorded(watch))

        with Stopwatch("dedup", candidates_in=len(fused)) as watch:
            deduped = deduplicate(fused)
            watch.candidates_out = len(deduped)
        stages.append(_recorded(watch))

        with Stopwatch("rerank", candidates_in=len(deduped)) as watch:
            # Tier order and path specificity are re-imposed on whatever comes
            # back. The reranker is a seam for code that does not exist yet, and
            # the guarantees of V1_SPEC §14 must hold at this boundary rather than
            # depend on every future implementation choosing to honour them.
            reordered = self._reranker.rerank(question, deduped)
            reranked = enforce_precedence(reordered)
            watch.candidates_out = len(reranked)
            watch.detail = type(self._reranker).__name__
            if [c.tier for c in reordered] != [c.tier for c in reranked]:
                watch.detail += " (tier order restored)"
            elif [c.identity for c in reordered] != [c.identity for c in reranked]:
                watch.detail += " (path specificity restored)"
        stages.append(_recorded(watch))

        with Stopwatch("budget", candidates_in=len(reranked)) as watch:
            bounded, truncated, tokens = apply_budget(
                reranked, max_items=self._limits.final, token_budget=self._limits.token_budget
            )
            watch.candidates_out = len(bounded)
            watch.detail = f"{tokens} tokens" + (", truncated" if truncated else "")
        stages.append(_recorded(watch))

        evidence = EvidenceSet(
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
                named_paths=tuple(
                    dict.fromkeys(
                        f"{c.repository}/{c.path}"
                        for c in bounded
                        if c.match is MatchClass.EXACT_PATH
                    )
                ),
                suffix_paths=tuple(
                    dict.fromkeys(
                        f"{c.repository}/{c.path}"
                        for c in bounded
                        if c.match is MatchClass.EXACT_SUBPATH
                    )
                ),
                token_estimate=tokens,
            ),
        )
        return RetrievalOutcome(
            evidence=evidence,
            suppression=suppressed,
            seed_entity_ids=tuple(entity_ids),
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
                            # The literal occurs *in* this span. That is a
                            # different claim from "this file's path is that
                            # literal", and conflating them is how a note
                            # mentioning `src/config.py` outranked the file.
                            MatchClass.EXACT_CONTENT,
                            f"contains {term}",
                        )
                        for hit in self._search.search_literal(term, scope, self._limits.per_source)
                    )
            watch.candidates_out = len(hits)
            watch.detail = f"{len(literal_terms)} literal term(s) in content"
        stages.append(_recorded(watch))
        return hits

    def _stage_lexical(
        self,
        question: str,
        scopes: list[list[int] | None],
        stages: list[StageTiming],
        *,
        suppressed: SuppressionOutcome | None = None,
    ) -> list[list[EvidenceCandidate]]:
        """FTS recall over chunk text. Recall, not proof, so it is associative."""
        lists: list[list[EvidenceCandidate]] = []
        with Stopwatch("lexical") as watch:
            if suppressed is not None:
                watch.detail = suppressed.message
                scopes = []
            for scope in scopes:
                hits = self._search.search_text(question, scope, self._limits.per_source)
                if hits:
                    lists.append(
                        [
                            self._chunk_candidate(
                                hit,
                                EvidenceSource.LEXICAL,
                                EvidenceTier.ASSOCIATIVE,
                                MatchClass.LEXICAL,
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
        suppressed: SuppressionOutcome | None = None,
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
            #: Split for the trace: an exact entity-name match and a full-text one
            #: are different claims, and the trace must not blur them.
            by_name = 0
            for scope in scopes:
                for hit in self._search.find_entities_by_name(
                    named, scope, self._limits.per_source
                ):
                    if hit.entity_id in seen_entities:
                        continue
                    seen_entities.add(hit.entity_id)
                    entity_ids.append(hit.entity_id)
                    by_name += 1
                    if hit.path is None or hit.line_start is None:
                        continue
                    candidates.append(
                        EvidenceCandidate(
                            repository=hit.repository,
                            repository_slug=hit.repository_slug,
                            path=hit.path,
                            line_start=hit.line_start,
                            line_end=hit.line_end or hit.line_start,
                            text="",
                            score=hit.score,
                            source=EvidenceSource.SYMBOL,
                            tier=EvidenceTier.DETERMINISTIC,
                            match=MatchClass.EXACT_NAME,
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
                            repository_slug=hit.repository_slug,
                            path=hit.path,
                            line_start=hit.line_start,
                            line_end=hit.line_end or hit.line_start,
                            text="",
                            score=hit.score,
                            source=EvidenceSource.SYMBOL,
                            tier=(
                                EvidenceTier.DETERMINISTIC if exact else EvidenceTier.ASSOCIATIVE
                            ),
                            match=MatchClass.EXACT_NAME if exact else MatchClass.LEXICAL,
                            description=f"defines {hit.name}",
                            entity_id=hit.entity_id,
                        )
                    )
            watch.candidates_out = len(candidates)
            watch.detail = (
                suppressed.message
                if suppressed is not None
                else f"{by_name} exact entity name, {len(seen_entities) - by_name} full-text"
            )
        stages.append(_recorded(watch))
        return candidates, entity_ids

    def _stage_files(
        self,
        names: list[str],
        scopes: list[list[int] | None],
        stages: list[StageTiming],
    ) -> tuple[list[EvidenceCandidate], set[str]]:
        """Files whose own name or path the question spells out.

        A file's path is a deterministic fact about the repository, so a match is
        `DETERMINISTIC` and carries **no line range**: it is a claim about the
        file, not about any line in it. Citing line 1 to satisfy a renderer would
        be manufacturing provenance for a fact that has none (V1_SPEC §11).

        Every match is returned. Three files named `config.py` are three answers to
        a question the repository genuinely leaves open, and silently picking one
        would be RepoAtlas guessing. Ranking still says which is most specific: a
        full path precedes a trailing sub-path, which precedes a bare basename.

        That grading leaves this stage on the candidate itself, as a
        :class:`MatchClass`, and not only in the description. It used to leave as
        prose, which fusion cannot read — so a file named by its whole path and a
        file that merely shares its basename arrived downstream indistinguishable,
        and the one with more retrieval-stage votes won (ARCHITECTURE.md §10).

        The second return value is the set of terms this stage *proved to exist*.
        Suppression consumes it, which is the whole reason the stage runs this
        early — see :meth:`_suppress_associative`.
        """
        with Stopwatch("file") as watch:
            hits: dict[tuple[str, str], FileHit] = {}
            for scope in scopes:
                for hit in self._search.find_files_by_path(names, scope, self._limits.per_source):
                    key = (hit.repository, hit.path)
                    current = hits.get(key)
                    if current is None or hit.specificity < current.specificity:
                        hits[key] = hit

            ordered = sorted(
                hits.values(), key=lambda hit: (hit.specificity, hit.repository, hit.path)
            )
            candidates = [
                EvidenceCandidate(
                    repository=hit.repository,
                    repository_slug=hit.repository_slug,
                    path=hit.path,
                    line_start=None,
                    line_end=None,
                    text="",
                    score=hit.score,
                    source=EvidenceSource.FILE,
                    tier=EvidenceTier.DETERMINISTIC,
                    # The specificity the search index computed is carried here
                    # rather than only into the human-readable description. It was
                    # dropped at exactly this line, which is why fusion could no
                    # longer tell an exact path from a shared basename.
                    match=_MATCH_BY_SPECIFICITY[hit.specificity],
                    description=_file_reason(hit),
                    entity_id=hit.entity_id,
                )
                for hit in ordered
            ]
            graded = Counter(hit.specificity for hit in ordered)
            watch.candidates_out = len(candidates)
            watch.detail = (
                f"{graded[0]} exact path, {graded[1]} exact sub-path, {graded[2]} exact basename"
            )
        stages.append(_recorded(watch))
        return candidates, {hit.term for hit in ordered}

    @staticmethod
    def _suppress_associative(
        literal_terms: list[str],
        exact: list[EvidenceCandidate],
        proven_files: set[str] | None = None,
    ) -> SuppressionOutcome | None:
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
        if not literal_terms or exact:
            return None
        # A term the file index resolved exists just as definitely as one found in
        # chunk text. `src/config.py` is not absent because no file *mentions* it.
        if proven_files and any(term in proven_files for term in literal_terms):
            return None
        named = ", ".join(literal_terms[:3])
        return SuppressionOutcome(
            terms=tuple(literal_terms),
            # Produced here and read from here. The trace renders this sentence; it
            # is never the source anything recovers the decision from.
            message=f"suppressed: none of the named terms exist ({named})",
        )

    def _stage_semantic(
        self,
        question: str,
        repo_ids: list[int] | None,
        stages: list[StageTiming],
        *,
        suppressed: SuppressionOutcome | None = None,
    ) -> tuple[list[EvidenceCandidate], bool, str]:
        """Vector neighbours. Always associative — a guess about what was meant."""
        with Stopwatch("semantic") as watch:
            if suppressed is not None:
                candidates, used, detail = [], False, suppressed.message
            else:
                candidates, used, detail = self._semantic_candidates(question, repo_ids)
            watch.candidates_out = len(candidates)
            watch.detail = detail
        stages.append(_recorded(watch))
        return candidates, used, detail

    def _semantic_candidates(
        self, question: str, repo_ids: list[int] | None
    ) -> tuple[list[EvidenceCandidate], bool, str]:
        """Vector neighbours, whether the stage ran, and what the trace should say.

        Every string returned here is written here, and that is the rule rather
        than an accident of the current branches: the third element is published
        verbatim as a public stage detail. It may describe RepoAtlas state —
        configuration, stored vector counts, hit counts, the configured model id —
        and it may never repeat text RepoAtlas did not author.
        """
        if self._vectors is None or self._embeddings is None:
            return [], False, "embeddings not configured"

        model_id = self._embeddings.id
        # Checking the index first means a workspace that was never embedded never
        # triggers a model load, so `locate` stays instant and offline.
        if self._vectors.count(model_id) == 0:
            return [], False, f"no vectors stored for {model_id}"

        try:
            vector = self._embeddings.embed_query(question)
        except EmbeddingUnavailable:
            # Never fatal: the deterministic stages are the mandatory ones.
            #
            # The exception is deliberately not bound. Its text belongs to
            # whoever raised it — a backend, a loader, a transport — and this
            # value is published. Not naming it is what keeps the two apart:
            # there is no variable here through which backend text could reach a
            # trace, so the guarantee cannot erode later into "just the class
            # name" or "just the first line".
            return [], False, SEMANTIC_UNAVAILABLE
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

        The walk itself is :func:`repoatlas.graph.traversal.expand_from_seeds`,
        which owns the edge whitelist and the cross-repository rule. What remains
        here is turning what it reached into candidates — and preserving the order
        it reached them in, because that order is the proximity signal fusion reads.
        """
        walk = expand_from_seeds(
            self._knowledge,
            entity_ids,
            hops=self._limits.graph_hops,
            fanout=self._limits.graph_fanout,
            seed_limit=self._limits.graph_seeds,
        )
        if not walk.reached:
            return [], walk.visited
        return self._knowledge_candidates(walk.reached, walk.reasons), walk.visited

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
                repository_slug=row.repository_slug,
                path=row.path,
                line_start=row.line_start,
                line_end=row.line_end,
                text=row.text,
                score=1.0,
                source=EvidenceSource.GRAPH,
                tier=EvidenceTier.DETERMINISTIC,
                match=MatchClass.RELATED,
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
        match: MatchClass,
        description: str = "",
    ) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository=hit.repository,
            repository_slug=hit.repository_slug,
            path=hit.path,
            line_start=hit.line_start,
            line_end=hit.line_end,
            text=hit.text,
            score=hit.score,
            source=source,
            tier=tier,
            match=match,
            description=description,
            # Whether this text is a whole chunk or one piece of a cut span
            # decides whether anything may be cited by line. It is a fact the
            # store already established, so it travels rather than being
            # re-derived downstream from the shape of the text.
            split_total=hit.split_total,
        )

    @staticmethod
    def _vector_candidate(hit: VectorHit) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository=hit.repository,
            repository_slug=hit.repository_slug,
            path=hit.path,
            line_start=hit.line_start,
            line_end=hit.line_end,
            text=hit.text,
            score=hit.score,
            source=EvidenceSource.SEMANTIC,
            tier=EvidenceTier.ASSOCIATIVE,
            match=MatchClass.SIMILAR,
            description="semantically similar",
            split_total=hit.split_total,
        )


#: ``FileHit.specificity`` as a match class. The search index grades an identity
#: match 0/1/2 — whole path, trailing sub-path, basename — and this is where that
#: grade becomes part of the candidate rather than only part of its description.
_MATCH_BY_SPECIFICITY = {
    0: MatchClass.EXACT_PATH,
    1: MatchClass.EXACT_SUBPATH,
    2: MatchClass.EXACT_BASENAME,
}


def _file_reason(hit: FileHit) -> str:
    """Why this file matched, in the user's own vocabulary.

    Stated as identity, never as content: "file name matches config.py" cannot be
    misread as "this file contains the string config.py", and the two are
    different facts.
    """
    if hit.specificity == 0:
        return f"file path is {hit.term}"
    if hit.specificity == 1:
        return f"file path ends with {hit.term}"
    return f"file name is {hit.term}"


def _recorded(watch: Stopwatch) -> StageTiming:
    assert watch.recorded is not None
    return watch.recorded
