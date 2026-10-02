"""The retrieval funnel (ARCHITECTURE.md §10).

Turns a question plus a ``RepositoryContext`` into a bounded ``EvidenceSet``. Every
stage is deterministic — this is the layer that makes the trust rule affordable,
and it is the whole of what RepoAtlas contributes to an answer.

The funnel's governing rule is V1_SPEC §14: **deterministic evidence outranks
association.** Every candidate carries an :class:`EvidenceTier` that fusion sorts on
before it looks at any score. Exact identifiers, exact error strings, exact symbol
names, environment variables and graph-supported relationships are therefore
unreachable by word-overlap recall, which is only ever considered after all of them.

Every stage records its cost and its effect on the candidate count, so retrieval
performance can be measured later without changing anything structural
(V1_SPEC §20).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from repoatlas.config.settings import RetrievalSettings
from repoatlas.graph.traversal import expand_from_seeds
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.evidence import (
    EvidenceCandidate,
    EvidenceSet,
    EvidenceSource,
    EvidenceTier,
    MatchClass,
)
from repoatlas.models.retrieval import (
    LookupOutcome,
    RetrievalOutcome,
    SuppressionOutcome,
    TermLookup,
    TermMatch,
    TermState,
)
from repoatlas.models.telemetry import RetrievalTrace, StageTiming, Stopwatch
from repoatlas.retrieval.query import QueryAnalysis, analyze
from repoatlas.retrieval.ranking import (
    NoopReranker,
    Reranker,
    apply_budget,
    deduplicate,
    enforce_precedence,
    fuse,
)
from repoatlas.storage.protocols import ChunkHit, EntityHit, FileHit
from repoatlas.storage.sqlite.search import SqliteSearchIndex
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore


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


class Retriever:
    """Builds bounded evidence sets from deterministic signals, ranked by tier."""

    def __init__(
        self,
        *,
        search: SqliteSearchIndex,
        knowledge: SqliteKnowledgeStore,
        reranker: Reranker | None = None,
        limits: RetrievalLimits | None = None,
    ) -> None:
        self._search = search
        self._knowledge = knowledge
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
        self,
        question: str,
        context: RepositoryContext,
        *,
        literal_phrase: bool = False,
        quote_signatures: bool = False,
        confined: bool = False,
        path_prefix: str | None = None,
    ) -> RetrievalOutcome:
        """The funnel: one pass, and everything that pass decided.

        The same work :meth:`retrieve` does, returning the suppression decision and
        the exact entity hits that seeded graph expansion alongside the evidence.
        Both were already computed here and both used to survive only as an English
        sentence in a trace field, which is not something a caller can act on
        without re-deriving it — either by parsing prose or by running the funnel a
        second time. Neither is acceptable, so the funnel says what it decided.

        ``quote_signatures`` supplies the declaration the indexer stored for each
        entity-name hit, so that a caller which turns candidates into citable
        evidence has bytes to cite rather than a bare location. Nothing about
        ranking changes. V1 renders these candidates as locations, passes nothing
        here, and is unchanged.

        ``confined`` makes ``REPOSITORY`` scope a boundary rather than a bias: only
        the named repository is searched, and expansion is given it as a home, so a
        shared node stays reachable and nothing on the far side of one is followed.
        That is what an explicit capability context means, and what ``inspect``,
        ``relationships`` and ``dependencies`` already enforce. V1's own scope is a
        bias (ARCHITECTURE.md §10), its second pass is untouched, and it passes
        nothing here.

        ``path_prefix`` narrows every scope searched to one normalized
        repository-relative file or directory. It is handed to each stage's own
        lookup and applied there, before that lookup's limit — never to the
        candidates a limited lookup already returned, where files outside the path
        would have used up the bound first. Graph expansion may walk through nodes
        outside it; what it reaches is filtered before it becomes a candidate. The
        capability passes it only with a confined repository scope.
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
        scopes = self._scopes(context, primary_ids, confined=confined)
        # The repository a confined walk may not leave. Taken from the primary
        # rather than from the first scoped id, so the walk and the scoped lookups
        # can never disagree about which repository "the" repository is. ``None``
        # in global scope, where there is no one repository to be confined to.
        home_repo_id = (
            context.primary.id
            if confined and context.scope is not ContextScope.GLOBAL and context.primary
            else None
        )

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

        #: What each exact lookup establishes about each named term, recorded by
        #: the stages below as they run. Read once, for ``term_matches``.
        proofs = _TermProofs()

        exact = self._stage_exact(
            literal_terms, scopes, stages, path_prefix=path_prefix, proofs=proofs
        )
        if exact:
            ranked_lists.append(exact)

        # File identity is searched *before* suppression is decided, because it is
        # one of the three ways a named term can be proven to exist. Deciding
        # suppression on content alone is what made RepoAtlas answer "no indexed
        # evidence matches" for `src/config.py` while holding that exact file.
        files, proven = self._stage_files(
            [*literal_terms, *analysis.terms],
            scopes,
            stages,
            path_prefix=path_prefix,
            proofs=proofs,
        )
        if files:
            ranked_lists.append(files)

        # And the third way: an exact indexed entity *name*. This lookup used to
        # live wholly inside the symbol stage, which runs after the decision below
        # — so a qualified symbol that exists in entity metadata without appearing
        # as contiguous text in any file was returned as an exact hit by the same
        # run that declared its name absent. `CoursesService.detail` is that shape:
        # the source declares a class and a method, and never those two spellings
        # joined by a dot.
        #
        # Deliberately over `literal_terms` and the HTTP keys only, never over
        # `analysis.terms`. Words lifted out of prose are not what the question
        # named, and a config key coincidentally called `retries` must not prove
        # that "connection pool exhausted after 30 retries" is answerable. That is
        # the same set the symbol stage looks up under suppression, so the two can
        # never disagree about what was asked for.
        named_exact = [*literal_terms, *analysis.http_keys]
        entity_hits, proven_names, entity_ms = self._named_entity_hits(
            named_exact, scopes, path_prefix=path_prefix, proofs=proofs
        )

        # A question that names something specific and does not find it is not
        # answered by anything associative. Decided once, here, and honoured by
        # every stage that follows. See _suppress_associative. A definite term a
        # full shared lookup crowded out is asked alone first, because the
        # decision may rest only on lookups that finished.
        self._settle_crowded(proofs, set(definite_terms), scopes, path_prefix=path_prefix)
        suppressed = self._suppress_associative(
            definite_terms, exact, proven, proven_names, path_prefix=path_prefix, proofs=proofs
        )

        lexical = self._stage_lexical(
            question, scopes, stages, suppressed=suppressed, path_prefix=path_prefix
        )
        ranked_lists.extend(lexical)

        symbols, entity_ids = self._stage_symbols(
            question,
            analysis.terms,
            literal_terms,
            analysis.symbol_terms,
            scopes,
            stages,
            suppressed=suppressed,
            http_keys=analysis.http_keys,
            quote_signatures=quote_signatures,
            named_hits=entity_hits,
            named_ms=entity_ms,
            path_prefix=path_prefix,
            proofs=proofs,
        )
        if symbols:
            ranked_lists.append(symbols)

        # What the exact lookups established for each term the question named.
        # The stages above made those lookups; a term a full shared lookup may have
        # crowded out is asked again alone, and nothing else is looked up for it.
        self._settle_crowded(proofs, _reported_terms(analysis), scopes, path_prefix=path_prefix)
        term_matches = _term_matches(analysis, proofs)

        graph, visited = self._stage_graph(
            entity_ids, stages, home_repo_id=home_repo_id, path_prefix=path_prefix
        )
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
                    ),
                ),
                suppression=suppressed,
                seed_entity_ids=tuple(entity_ids),
                term_matches=term_matches,
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
            term_matches=term_matches,
        )

    # -- stages --------------------------------------------------------------

    def _stage_exact(
        self,
        literal_terms: list[str],
        scopes: list[list[int] | None],
        stages: list[StageTiming],
        *,
        path_prefix: str | None = None,
        proofs: _TermProofs | None = None,
    ) -> list[EvidenceCandidate]:
        """Literal terms, verified by substring. Deterministic by construction.

        Also records, in ``proofs``, which terms matched and which lookups stopped
        at their limit without a match: facts about each term that the candidate
        list alone cannot carry once the terms share it.
        """
        with Stopwatch("exact") as watch:
            hits: list[EvidenceCandidate] = []
            for term in literal_terms:
                for scope in scopes:
                    found = self._search.find_literal(
                        term, scope, self._limits.per_source, path_prefix=path_prefix
                    )
                    if proofs is not None:
                        proofs.text(term, found.hits, exhaustive=found.exhaustive)
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
                        for hit in found.hits
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
        path_prefix: str | None = None,
    ) -> list[list[EvidenceCandidate]]:
        """FTS recall over chunk text. Recall, not proof, so it is associative."""
        lists: list[list[EvidenceCandidate]] = []
        with Stopwatch("lexical") as watch:
            if suppressed is not None:
                watch.detail = suppressed.message
                scopes = []
            for scope in scopes:
                hits = self._search.search_text(
                    question, scope, self._limits.per_source, path_prefix=path_prefix
                )
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

    def _named_entity_hits(
        self,
        named: list[str],
        scopes: list[list[int] | None],
        *,
        path_prefix: str | None = None,
        proofs: _TermProofs | None = None,
    ) -> tuple[list[EntityHit], set[str], float]:
        """Exact entity-name lookup, run before suppression is decided.

        Deliberately records **no stage of its own**. It is the first half of the
        symbol stage, moved earlier because suppression needs its answer; the trace
        still shows one ``symbol`` stage, and the time spent here is added to it
        (see :meth:`_stage_symbols`). A stage that appeared only sometimes, or a
        tenth stage in a nine-stage funnel, would make the trace describe a
        pipeline that does not exist.

        The second return value is the set of requested spellings that **exist**.
        It comes from the store's own matched term rather than being re-derived
        here, so "this name resolves" means exactly what the lookup's predicate
        means — including for an interned key such as ``env:SERVICE_HOST``, whose
        entity is found by key rather than by the spelling the question used.

        Matching is byte-exact: :meth:`SqliteSearchIndex.find_entities_by_name`
        compares with SQL ``IN`` against columns carrying SQLite's default BINARY
        collation. That is what keeps a diacritic-folded near-miss such as
        ``creer_utilisateur`` from proving that ``créer_utilisateur`` was asked
        for. Adding ``COLLATE NOCASE`` to those columns would silently turn this
        proof into a guess.
        """
        start = time.perf_counter()
        hits: list[EntityHit] = []
        seen: set[int] = set()
        proven: set[str] = set()
        if named:
            for scope in scopes:
                # Retrieval names no Python module value: those are declarations for
                # USES alone, which search and locate answer as they did without them.
                found = self._search.find_entities_by_name(
                    named,
                    scope,
                    self._limits.per_source,
                    path_prefix=path_prefix,
                    module_values=False,
                )
                if proofs is not None:
                    proofs.names(named, found, self._limits.per_source)
                for hit in found:
                    if hit.entity_id in seen:
                        continue
                    seen.add(hit.entity_id)
                    hits.append(hit)
                    if hit.matched_term:
                        proven.add(hit.matched_term)
        return hits, proven, (time.perf_counter() - start) * 1000.0

    def _lookup_names(
        self,
        names: list[str],
        scopes: list[list[int] | None],
        *,
        path_prefix: str | None = None,
        proofs: _TermProofs | None = None,
    ) -> list[EntityHit]:
        """Exact entity-name lookup for one group of spellings, in every scope."""
        if not names:
            return []
        hits: list[EntityHit] = []
        for scope in scopes:
            found = self._search.find_entities_by_name(
                names, scope, self._limits.per_source, path_prefix=path_prefix, module_values=False
            )
            if proofs is not None:
                proofs.names(names, found, self._limits.per_source)
            hits.extend(found)
        return hits

    def _stage_symbols(
        self,
        question: str,
        terms: tuple[str, ...],
        literal_terms: list[str],
        symbol_terms: tuple[str, ...],
        scopes: list[list[int] | None],
        stages: list[StageTiming],
        *,
        suppressed: SuppressionOutcome | None = None,
        http_keys: tuple[str, ...] = (),
        quote_signatures: bool = False,
        named_hits: list[EntityHit] | None = None,
        named_ms: float = 0.0,
        path_prefix: str | None = None,
        proofs: _TermProofs | None = None,
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

        **An exact name match is a fact about the index; it is only a fact about
        the question when the question named it.** Both halves used to treat every
        word of the question as a name, so an entity called ``call``, ``services``
        or ``now`` arrived `DETERMINISTIC` — ahead of every contextual candidate,
        and seeding a walk besides — for a sentence that merely contained the word.
        ``symbol_terms`` is the set the question named (see
        :attr:`~repoatlas.retrieval.query.QueryAnalysis.symbol_terms`); everything
        else is still looked up — after the named terms, under a bound of its own,
        so it cannot crowd them out — and returns as recall. Nothing is excluded,
        and a symbol genuinely named ``call`` is still found — by asking for it.

        ``quote_signatures`` supplies the declaration the indexer stored for each
        hit, which is what the graph stage has always done with the same column. A
        caller that turns candidates into citable evidence needs the bytes: the
        definition of the symbol a question names is frequently the only candidate
        that points at the defining file at all, and returning it with no text
        turns the answer into a location nobody can quote. V1 renders these
        candidates as locations and passes nothing here, so its output is unchanged.
        """
        # Only the spellings the question *named* may promote an entity. A word
        # lifted out of prose is recall wherever it is found, including here: FTS
        # finding an entity called `call` does not mean the question asked for one.
        exact_names = {term.lower() for term in symbol_terms}
        with Stopwatch("symbol") as watch:
            candidates: list[EvidenceCandidate] = []
            #: Seeds for graph expansion. Named-term matches only.
            entity_ids: list[int] = []
            #: Everything already returned, exact or fuzzy, for deduplication.
            seen_entities: set[int] = set()
            # Which spellings the question actually *named*, as opposed to merely
            # containing. Under suppression that is only what it spelled literally:
            # words lifted out of the surrounding prose are recall, and an exact-name
            # hit on one of them would enter as DETERMINISTIC — the strongest tier —
            # for a question whose subject does not exist. An HTTP key is an exact
            # identity the question spelled — `GET /users` names one route — so it
            # counts under suppression as well; it can only ever match an entity
            # *key*, which is what keeps it exact.
            explicit = (
                {*literal_terms, *http_keys}
                if suppressed is not None
                else {*symbol_terms, *http_keys}
            )
            # Looked up in order of how exactly the question named them, each group
            # under its own bound, because a shared bound is a competition the
            # named term can lose. One `LIMIT` over a qualified symbol and every
            # prose word beside it is cut by key order, so a question naming
            # `CoursesService.detail` alongside "record", "course" and "courses"
            # could come back without the one entity it named — each of those words
            # matches more entities than the bound holds, and most of their keys
            # sort first.
            #
            # First what the question spelled literally, which is the lookup
            # suppression was decided on, reused rather than fetched twice; then the
            # other names it made explicit; then, only when nothing is suppressed,
            # the words it merely used. The first sighting of an entity stands, so
            # an entity both named and merely mentioned is counted as named.
            groups: list[list[str]] = []
            if suppressed is None:
                spelled_first = {*literal_terms, *http_keys}
                groups.append([term for term in symbol_terms if term not in spelled_first])
                groups.append([term for term in dict.fromkeys(terms) if term not in explicit])
            resolved = (
                list(named_hits)
                if named_hits is not None
                else self._lookup_names(
                    [*literal_terms, *http_keys], scopes, path_prefix=path_prefix
                )
            )
            for index, group in enumerate(groups):
                if group:
                    # The first group is the names the question gave; what their
                    # lookup establishes is part of the per-term answer. The second
                    # is words it merely used, which that answer never reports.
                    resolved.extend(
                        self._lookup_names(
                            group,
                            scopes,
                            path_prefix=path_prefix,
                            proofs=proofs if index == 0 else None,
                        )
                    )
            #: Split for the trace: a match on a spelling the question named, a
            #: match on a word merely present in it, and a full-text hit are three
            #: different claims, and the trace must not blur them.
            qualified_ids = {
                hit.entity_id
                for hit in resolved
                if hit.qualified_name
                and "." in hit.qualified_name
                and hit.qualified_name.lower() in exact_names
            }
            # Only a whole-query qualified lookup suppresses component-name seeds.
            # Prose may independently name several symbols; keep those alternatives.
            qualified_lookup = any(
                hit.entity_id in qualified_ids
                and hit.qualified_name == question.strip().strip("`\"'")
                for hit in resolved
            )
            by_name = 0
            by_recall = 0
            for hit in resolved:
                if hit.entity_id in seen_entities:
                    continue
                seen_entities.add(hit.entity_id)
                # An empty matched term cannot happen — the store derives it from
                # the same predicate that selected the row — and if it ever did,
                # the established reading is that the row was named.
                spelled = not hit.matched_term or hit.matched_term in explicit
                if spelled:
                    # Only a named term seeds expansion. Seeding from a prose word
                    # would send the walk out from whatever happened to share it,
                    # and expansion labels everything it reaches DETERMINISTIC.
                    if not qualified_lookup or hit.entity_id in qualified_ids:
                        entity_ids.append(hit.entity_id)
                    by_name += 1
                else:
                    by_recall += 1
                if hit.path is None or hit.line_start is None:
                    continue
                candidates.append(
                    EvidenceCandidate(
                        repository=hit.repository,
                        repository_slug=hit.repository_slug,
                        path=hit.path,
                        line_start=hit.line_start,
                        line_end=hit.line_end or hit.line_start,
                        text=hit.signature if quote_signatures else "",
                        score=hit.score,
                        source=EvidenceSource.SYMBOL,
                        tier=(EvidenceTier.DETERMINISTIC if spelled else EvidenceTier.ASSOCIATIVE),
                        match=MatchClass.EXACT_NAME if spelled else MatchClass.LEXICAL,
                        description=f"defines {hit.name}",
                        entity_id=hit.entity_id,
                        entity_type=hit.type,
                        qualified_match=hit.entity_id in qualified_ids,
                        # The recorded declaration, not the span's own lines.
                        declaration=bool(quote_signatures and hit.signature),
                    )
                )
            for scope in scopes if suppressed is None else []:
                for hit in self._search.search_entities(
                    question,
                    scope,
                    None,
                    self._limits.per_source,
                    path_prefix=path_prefix,
                    module_values=False,
                ):
                    if hit.entity_id in seen_entities:
                        continue
                    seen_entities.add(hit.entity_id)
                    exact = hit.name.lower() in exact_names
                    if exact and not qualified_lookup:
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
                            text=hit.signature if quote_signatures else "",
                            score=hit.score,
                            source=EvidenceSource.SYMBOL,
                            tier=(
                                EvidenceTier.DETERMINISTIC if exact else EvidenceTier.ASSOCIATIVE
                            ),
                            match=MatchClass.EXACT_NAME if exact else MatchClass.LEXICAL,
                            description=f"defines {hit.name}",
                            entity_id=hit.entity_id,
                            entity_type=hit.type,
                            declaration=bool(quote_signatures and hit.signature),
                        )
                    )
            watch.candidates_out = len(candidates)
            watch.detail = (
                suppressed.message
                if suppressed is not None
                else (
                    f"{by_name} exact entity name, {by_recall} name recall, "
                    f"{len(seen_entities) - by_name - by_recall} full-text"
                )
            )
        # The exact-name lookup runs before this stage so that suppression can see
        # its answer, but it is still this stage's work and still this stage's
        # cost. Folding the time back in is what keeps the trace a description of
        # the funnel rather than of the order the code happens to be written in.
        recorded = _recorded(watch)
        stages.append(
            recorded.model_copy(update={"duration_ms": round(recorded.duration_ms + named_ms, 3)})
        )
        return candidates, entity_ids

    def _stage_files(
        self,
        names: list[str],
        scopes: list[list[int] | None],
        stages: list[StageTiming],
        *,
        path_prefix: str | None = None,
        proofs: _TermProofs | None = None,
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

        What each term established is recorded in ``proofs``, and it can say more
        than the second return value: the store keeps one hit per file, the most
        specific, so when ``docs/ARCHITECTURE.md`` and ``ARCHITECTURE.md`` both name
        one file only the exact-path hit comes back, and ``ARCHITECTURE.md`` has no
        hit of its own. It names that file all the same, by the store's own rule —
        see :func:`_names_file`. Suppression needs one proof and reads the return
        value; a per-term answer needs each term's own.
        """
        with Stopwatch("file") as watch:
            hits: dict[tuple[str, str], FileHit] = {}
            for scope in scopes:
                found = self._search.find_files_by_path(
                    names, scope, self._limits.per_source, path_prefix=path_prefix
                )
                if proofs is not None:
                    proofs.files(names, found, self._limits.per_source)
                for hit in found:
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

    def _settle_crowded(
        self,
        proofs: _TermProofs,
        reported: set[str],
        scopes: list[list[int] | None],
        *,
        path_prefix: str | None = None,
    ) -> None:
        """Ask again, alone, each reported term a full shared lookup left unmatched.

        The file and entity lookups take every term of the question in one query
        under one limit, so a name that thirty-five declarations share can fill it
        and leave a rarer one unanswered. That is not a negative. Asked alone with a
        limit of one, the same exact lookup is complete: one row proves the term and
        none shows it absent. These are equality lookups against the file and
        entity tables — no source text is read — and they run only for a term that
        a *full* lookup left without a match, so a question whose lookups came back
        short of their limits asks nothing more.

        A term asked here is no longer crowded: its answer is complete either way.
        Suppression settles the definite terms before it decides, and the per-term
        answer settles the rest afterwards, so no term is asked twice.
        """
        for term in sorted((proofs.files_crowded & reported) - proofs.files_found):
            proofs.files_crowded.discard(term)
            if any(
                self._search.find_files_by_path([term], scope, 1, path_prefix=path_prefix)
                for scope in scopes
            ):
                proofs.files_found.add(term)
        for term in sorted((proofs.names_crowded & reported) - proofs.names_found):
            proofs.names_crowded.discard(term)
            if any(
                self._search.find_entities_by_name(
                    [term], scope, 1, path_prefix=path_prefix, module_values=False
                )
                for scope in scopes
            ):
                proofs.names_found.add(term)

    @staticmethod
    def _suppress_associative(
        literal_terms: list[str],
        exact: list[EvidenceCandidate],
        proven_files: set[str] | None = None,
        proven_names: set[str] | None = None,
        *,
        path_prefix: str | None = None,
        proofs: _TermProofs | None = None,
    ) -> SuppressionOutcome | None:
        """Whether every associative source would be answering a different question.

        When the question names literal terms — a quoted string, an environment
        variable, a dotted name, a path — and **none of them exist anywhere in the
        index, the honest answer is that they do not exist**. Offering spans that
        merely read like the name would be plausible, unfounded output, which is
        the failure V1_SPEC §2 exists to prevent.

        This holds for *every* associative source. FTS recall that matches
        `@company` when the user asked for `@company/absent-918273` is one such
        mistake, and an entity coincidentally named `retries` when the user quoted
        an error string is a worse one, because an exact-name lookup marks it
        `DETERMINISTIC` and it then outranks everything.

        A question with no literal terms ("where is deployment configuration
        generated?") is exactly what the associative half of the funnel is for, so
        nothing is suppressed there. One term that *does* exist is likewise enough
        to keep the funnel open: the question is answerable, and the rest of the
        stages are how the surrounding context is found.

        Existence has three proofs, and a term needs only one of them: the literal
        text of a chunk, the identity of a file, and the name of an entity. They
        are genuinely different questions about the index, and a term that fails
        two of them has not been shown to be absent.

        ``proven_names`` carries only spellings the *question* named — never a word
        lifted out of its prose. That distinction is the whole safety of this
        function: a config key coincidentally called ``retries`` exists, and it
        still must not make "connection pool exhausted after 30 retries" look
        answerable.

        And absence has one proof only: every lookup of the term ran to completion
        and matched nothing. ``proofs`` says which did. A text lookup that stopped
        at its bound, or a shared lookup that filled before it reached the term, has
        not shown the term absent, and one such term keeps the question open — its
        per-term state then reports what was not established, rather than this
        sentence reporting it absent.
        """
        if not literal_terms or exact:
            return None
        # A term the file index resolved exists just as definitely as one found in
        # chunk text. `src/config.py` is not absent because no file *mentions* it.
        if proven_files and any(term in proven_files for term in literal_terms):
            return None
        # And a term the entity index resolved exists on the same footing. A
        # qualified symbol frequently appears in no file as contiguous text —
        # `CoursesService.detail` is a class declaration and a method declaration,
        # never those two spellings joined by a dot — so content and file identity
        # both miss it while the index holds it under `entities.qualified_name`.
        # Without this, one run returned the entity as an exact match and, in the
        # same breath, reported its name absent.
        if proven_names and any(term in proven_names for term in literal_terms):
            return None
        if proofs is not None and not all(proofs.absent(term) for term in literal_terms):
            return None
        named = ", ".join(literal_terms[:3])
        # Under a path scope the lookups that failed were confined to it, so the
        # sentence says where they looked rather than claiming absence everywhere.
        where = f" under {path_prefix}" if path_prefix is not None else ""
        return SuppressionOutcome(
            terms=tuple(literal_terms),
            # Produced here and read from here. The trace renders this sentence; it
            # is never the source anything recovers the decision from.
            message=f"suppressed: none of the named terms exist{where} ({named})",
        )

    def _stage_graph(
        self,
        entity_ids: list[int],
        stages: list[StageTiming],
        *,
        home_repo_id: int | None = None,
        path_prefix: str | None = None,
    ) -> tuple[list[EvidenceCandidate], int]:
        with Stopwatch("graph", candidates_in=len(entity_ids)) as watch:
            candidates, visited = self._expand(
                entity_ids, home_repo_id=home_repo_id, path_prefix=path_prefix
            )
            watch.candidates_out = len(candidates)
            watch.detail = f"{visited} node(s) over {self._limits.graph_hops} hop(s)"
        stages.append(_recorded(watch))
        return candidates, visited

    def _expand(
        self,
        entity_ids: list[int],
        *,
        home_repo_id: int | None = None,
        path_prefix: str | None = None,
    ) -> tuple[list[EvidenceCandidate], int]:
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
            home_repo_id=home_repo_id,
        )
        if not walk.reached:
            return [], walk.visited
        return (
            self._knowledge_candidates(walk.reached, walk.reasons, path_prefix=path_prefix),
            walk.visited,
        )

    def _knowledge_candidates(
        self,
        entity_ids: list[int],
        reason: dict[int, str] | None = None,
        *,
        path_prefix: str | None = None,
    ) -> list[EvidenceCandidate]:
        """Describe reached entities, **preserving traversal order**.

        Order is the proximity signal: entities found on the first hop precede
        those found on the second, so rank fusion sees the closer relationship as
        the stronger one. A set-returning query loses that, so it is restored here.

        Under a ``path_prefix`` the walk may have passed through entities declared
        elsewhere, and those are dropped *before* the ``per_source`` cut: taking the
        first ``per_source`` reached and then filtering would let entities outside
        the path use up the bound. Without one, the cut comes first, as it always
        has.
        """
        if path_prefix is None:
            wanted = entity_ids[: self._limits.per_source]
            described = self._knowledge.describe_entities(wanted)
        else:
            wanted = list(entity_ids)
            described = [
                row
                for start in range(0, len(wanted), _DESCRIBE_BATCH)
                for row in self._knowledge.describe_entities(
                    wanted[start : start + _DESCRIBE_BATCH], path_prefix=path_prefix
                )
            ]
        position = {entity_id: index for index, entity_id in enumerate(wanted)}
        rows = sorted(described, key=lambda row: position.get(row.entity_id, len(position)))
        if path_prefix is not None:
            rows = rows[: self._limits.per_source]
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
                entity_type=row.entity_type,
                # What the store describes an entity with is its recorded
                # declaration, so any text here is that and not source lines.
                declaration=bool(row.text),
            )
            for row in rows
        ]

    # -- helpers -------------------------------------------------------------

    def _scopes(
        self, context: RepositoryContext, primary_ids: list[int], *, confined: bool = False
    ) -> list[list[int] | None]:
        """Search scopes in priority order.

        In repository scope the primary repository is searched first; other managed
        repositories are reachable, so a justified cross-repository answer is still
        possible (V1_SPEC §6). In global scope there is a single unscoped pass.

        ``confined`` drops that second, unscoped pass. A capability call states its
        scope explicitly, and every other capability treats the statement as a
        boundary: answering outside it would make the scope advisory, which is the
        one property that makes a scoped answer worth having. Interned entities are
        unaffected — they belong to no repository, and the scoped lookups admit them
        by ``repo_id IS NULL`` rather than by scope.
        """
        if context.scope is ContextScope.GLOBAL:
            return [None]
        if confined:
            return [primary_ids]
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


#: Entity ids described per query when a path scope means every reached entity must
#: be located before the ``per_source`` cut. Well under SQLite's bound-parameter
#: limit, whatever it was compiled with.
_DESCRIBE_BATCH = 500

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


def _names_file(term: str, hit: FileHit) -> bool:
    """Whether ``term`` names the file ``hit`` found, by the file store's rule.

    The whole relative path, a trailing sub-path on a segment boundary (the
    basename is the shortest), or the path qualified by the repository's slug —
    exactly the shapes :meth:`SqliteSearchIndex.find_files_by_path` matches, and
    nothing looser: ``onfig.py`` does not name ``src/config.py``.
    """
    path = hit.path
    return term == path or path.endswith(f"/{term}") or term == f"{hit.repository_slug}/{path}"


def _answers_to(hit: EntityHit, asked: Sequence[str]) -> set[str]:
    """Every requested spelling this entity answers to, not only the first.

    The store records one ``matched_term`` per row — the first requested spelling
    it matched — which is all ranking needs. A per-term answer needs every one:
    asked for ``"markEmailVerified"`` and ``KeycloakAdminClient.markEmailVerified``
    together, the method's one row used to prove the bare name and leave the
    qualified name of a method that exists reported ``not_found``. The name and
    qualified name are compared exactly as the store's predicate compares them; a
    key match is known only through ``matched_term``.
    """
    answered = {term for term in asked if term in (hit.name, hit.qualified_name)}
    if hit.matched_term:
        answered.add(hit.matched_term)
    return answered


@dataclass
class _TermProofs:
    """What the exact lookups established about each term, gathered as they ran.

    The stages that make those lookups record here as a side effect of work they
    do anyway, and :func:`_term_matches` reads it once. Nothing here ranks, admits
    or budgets a candidate.

    A term a lookup was *asked* about and did not match is a negative only when
    the lookup examined everything it could have. Each shared lookup takes every
    term of the question under one limit, so when it comes back full, a term it
    did not match may simply have been crowded out; such terms are kept in the
    ``*_crowded`` sets until :meth:`Retriever._settle_crowded` has asked each alone.
    The text lookup cannot be settled that cheaply and is recorded as cut instead.
    """

    text_asked: set[str] = field(default_factory=set)
    text_found: set[str] = field(default_factory=set)
    #: Terms whose text lookup stopped at its limit without a case-exact match.
    text_cut: set[str] = field(default_factory=set)
    files_asked: set[str] = field(default_factory=set)
    files_found: set[str] = field(default_factory=set)
    files_crowded: set[str] = field(default_factory=set)
    names_asked: set[str] = field(default_factory=set)
    names_found: set[str] = field(default_factory=set)
    names_crowded: set[str] = field(default_factory=set)

    def text(self, term: str, hits: Sequence[ChunkHit], *, exhaustive: bool) -> None:
        self.text_asked.add(term)
        if hits:
            self.text_found.add(term)
        elif not exhaustive:
            self.text_cut.add(term)

    def files(self, asked: Sequence[str], hits: Sequence[FileHit], limit: int) -> None:
        self.files_asked.update(asked)
        found = {hit.term for hit in hits} | {
            term for term in asked if any(_names_file(term, hit) for hit in hits)
        }
        self.files_found |= found
        if len(hits) >= limit:
            self.files_crowded.update(term for term in asked if term not in found)

    def names(self, asked: Sequence[str], hits: Sequence[EntityHit], limit: int) -> None:
        self.names_asked.update(asked)
        found = {term for hit in hits for term in _answers_to(hit, asked)}
        self.names_found |= found
        if len(hits) >= limit:
            self.names_crowded.update(term for term in asked if term not in found)

    def absent(self, term: str) -> bool:
        """Whether every lookup of ``term`` ran to completion and matched nothing.

        Read from the same records as the per-term answer, so suppression and the
        term's state cannot disagree. A lookup never asked, one cut off at its
        limit, and one a full shared lookup crowded out that has not been asked
        alone since, each establish nothing.
        """
        outcomes = (
            _text_outcome(term, self),
            _outcome(term, self.files_asked, self.files_found),
            _outcome(term, self.names_asked, self.names_found),
        )
        unsettled = term in self.files_crowded or term in self.names_crowded
        return not unsettled and all(outcome is LookupOutcome.NO_MATCH for outcome in outcomes)


def _reported_terms(analysis: QueryAnalysis) -> set[str]:
    """The terms :func:`_term_matches` reports on — the only ones worth settling."""
    parts = set(analysis.token_parts)
    return {
        *analysis.literal_terms,
        *analysis.http_keys,
        *(name for name in analysis.symbol_names if name not in parts),
    }


def _outcome(term: str, asked: set[str], found: set[str]) -> LookupOutcome:
    if term not in asked:
        return LookupOutcome.NOT_CHECKED
    return LookupOutcome.MATCH if term in found else LookupOutcome.NO_MATCH


def _text_outcome(term: str, proofs: _TermProofs) -> LookupOutcome:
    if term not in proofs.text_asked:
        return LookupOutcome.NOT_CHECKED
    if term in proofs.text_found:
        return LookupOutcome.MATCH
    return LookupOutcome.INCOMPLETE if term in proofs.text_cut else LookupOutcome.NO_MATCH


def _state(checks: dict[TermLookup, LookupOutcome]) -> TermState:
    """The one-word summary of ``checks``: only ever what they establish.

    ``exact`` needs one identity lookup to have matched. ``text_only`` needs the
    text to have matched and every identity lookup to have run to completion
    without a match. ``not_found`` needs every relevant lookup to have run to
    completion without a match. Anything short of those — a lookup that never ran,
    or one cut off at its limit — is ``not_established``, however many of the other
    lookups came back empty.
    """
    identity = [outcome for lookup, outcome in checks.items() if lookup is not TermLookup.TEXT]
    if LookupOutcome.MATCH in identity:
        return TermState.EXACT
    if checks.get(TermLookup.TEXT) is LookupOutcome.MATCH and all(
        outcome is LookupOutcome.NO_MATCH for outcome in identity
    ):
        return TermState.TEXT_ONLY
    if all(outcome is LookupOutcome.NO_MATCH for outcome in checks.values()):
        return TermState.NOT_FOUND
    return TermState.NOT_ESTABLISHED


def _term_match(term: str, checks: dict[TermLookup, LookupOutcome]) -> TermMatch:
    return TermMatch(
        term=term,
        state=_state(checks),
        lookups=tuple(
            lookup for lookup, outcome in checks.items() if outcome is not LookupOutcome.NOT_CHECKED
        ),
        checks=checks,
    )


def _term_matches(analysis: QueryAnalysis, proofs: _TermProofs) -> tuple[TermMatch, ...]:
    """What the exact lookups established for each term the question named.

    Every term is reported with every lookup relevant to its kind, and what each
    established:

    * a **literal term** is searched in text, as a file path and as an entity name,
      all before suppression is decided;
    * an **HTTP key** is looked up as an entity key, and nothing else is relevant;
    * any **other name** the question spelled as one — a compound ``PascalCase``
      token, a word a naming phrase introduced, a one-token question — is looked up
      as a file name when it was among the words file lookup took, and as an entity
      name by the symbol stage, which does not run under suppression. It is never
      searched as text, and its text is reported ``not_checked`` rather than
      assumed absent.

    Prose words the question merely used are not reported: they were never
    claimed to be names, and "not found" would be a claim about them. Nor are the
    pieces of a one-token question — ``docs`` of ``docs/ARCHITECTURE.md`` is a
    directory the question passed through, and "docs: not_found" would read as a
    claim that it does not exist.
    """
    matches: list[TermMatch] = []
    seen: set[str] = set(analysis.token_parts)

    for term in analysis.literal_terms:
        seen.add(term)
        matches.append(
            _term_match(
                term,
                {
                    TermLookup.TEXT: _text_outcome(term, proofs),
                    TermLookup.FILE_PATH: _outcome(term, proofs.files_asked, proofs.files_found),
                    TermLookup.ENTITY_NAME: _outcome(term, proofs.names_asked, proofs.names_found),
                },
            )
        )

    for key in analysis.http_keys:
        if key in seen:
            continue
        seen.add(key)
        matches.append(
            _term_match(
                key, {TermLookup.ENTITY_KEY: _outcome(key, proofs.names_asked, proofs.names_found)}
            )
        )

    for name in analysis.symbol_names:
        if name in seen:
            continue
        seen.add(name)
        matches.append(
            _term_match(
                name,
                {
                    TermLookup.TEXT: LookupOutcome.NOT_CHECKED,
                    TermLookup.FILE_PATH: _outcome(name, proofs.files_asked, proofs.files_found),
                    TermLookup.ENTITY_NAME: _outcome(name, proofs.names_asked, proofs.names_found),
                },
            )
        )
    return tuple(matches)


def _recorded(watch: Stopwatch) -> StageTiming:
    assert watch.recorded is not None
    return watch.recorded
