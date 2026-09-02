"""The retrieval funnel (ARCHITECTURE.md §10).

Turns a question plus a ``RepositoryContext`` into a bounded ``EvidenceSet``. No
LLM is involved at any stage — this is the layer that makes the trust rule
affordable, and it is why ``locate`` works with no model installed.

Slice A implements stages 1, 2, 4, 5, 6 and 8. Semantic retrieval (stage 3) and
reranking (stage 7) arrive in Slice D behind the interfaces already present here.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSet, EvidenceSource
from repoatlas.models.parsing import EdgeType
from repoatlas.retrieval.query import analyze
from repoatlas.retrieval.ranking import NoopReranker, Reranker, deduplicate, fuse
from repoatlas.storage.protocols import ChunkHit
from repoatlas.storage.sqlite.search import SqliteSearchIndex
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

#: Edge types a cross-repository expansion may traverse. Restricting the set is
#: what makes a cross-repository answer structurally justified (V1_SPEC §6).
_EXPANSION_EDGES = (
    EdgeType.READS,
    EdgeType.CONFIGURES,
    EdgeType.DEPENDS_ON,
    EdgeType.IMPORTS,
    EdgeType.REFERENCES,
    EdgeType.EXECUTES,
    EdgeType.SOURCES,
    EdgeType.DEFINES,
)


@dataclass(frozen=True, slots=True)
class RetrievalLimits:
    """Hard caps. The evidence set is bounded by count and by tokens."""

    per_source: int = 30
    final: int = 12
    token_budget: int = 6000


class Retriever:
    """Builds bounded evidence sets from deterministic signals."""

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
        """
        analysis = analyze(question)

        # Scoping happens before scoring, so we never search every repository for
        # every question (V1_SPEC §20).
        primary_ids = list(context.repository_ids)
        primary_name = context.primary.name if context.primary else None

        ranked_lists: list[list[EvidenceCandidate]] = []

        # Stage 2a — literal terms, verified by substring, scoped to primary first.
        literal_hits: list[EvidenceCandidate] = []
        literal_terms = list(analysis.literal_terms)
        if literal_phrase:
            phrase = question.strip().strip("?.!").strip()
            if phrase and phrase not in literal_terms:
                literal_terms.insert(0, phrase)
        for term in literal_terms:
            for scope in self._scopes(context, primary_ids):
                hits = self._search.search_literal(term, scope, self._limits.per_source)
                literal_hits.extend(
                    self._to_candidate(h, EvidenceSource.EXACT, f"contains {term}") for h in hits
                )
        if literal_hits:
            ranked_lists.append(literal_hits)

        # Stage 2b — lexical recall over chunk text.
        for scope in self._scopes(context, primary_ids):
            text_hits = self._search.search_text(question, scope, self._limits.per_source)
            if text_hits:
                ranked_lists.append(
                    [self._to_candidate(h, EvidenceSource.EXACT) for h in text_hits]
                )

        # Stage 2c — symbol/entity lookup.
        entity_candidates: list[EvidenceCandidate] = []
        entity_ids: list[int] = []
        for scope in self._scopes(context, primary_ids):
            for hit in self._search.search_entities(question, scope, None, self._limits.per_source):
                entity_ids.append(hit.entity_id)
                if hit.path and hit.line_start is not None:
                    entity_candidates.append(
                        EvidenceCandidate(
                            repository=hit.repository,
                            path=hit.path,
                            line_start=hit.line_start,
                            line_end=hit.line_end or hit.line_start,
                            text="",
                            score=hit.score,
                            source=EvidenceSource.SYMBOL,
                            description=f"defines {hit.name}",
                            entity_id=hit.entity_id,
                        )
                    )
        if entity_candidates:
            ranked_lists.append(entity_candidates)

        # Stage 4 — graph expansion. This is how a question asked inside one
        # repository reaches justified evidence in another.
        expanded = self._expand(entity_ids)
        if expanded:
            ranked_lists.append(expanded)

        if not ranked_lists:
            return EvidenceSet(
                question=question,
                scope=str(context.scope),
                primary_repository=primary_name,
            )

        fused = fuse(ranked_lists, primary_repository=primary_name)
        deduped = deduplicate(fused)
        reranked = self._reranker.rerank(question, deduped)
        bounded, truncated = self._apply_budget(reranked)

        return EvidenceSet(
            question=question,
            candidates=tuple(bounded),
            scope=str(context.scope),
            primary_repository=primary_name,
            truncated=truncated,
        )

    def _scopes(self, context: RepositoryContext, primary_ids: list[int]) -> list[list[int] | None]:
        """Search scopes in priority order.

        In repository scope the primary repository is searched first; other managed
        repositories are reachable, so a justified cross-repository answer is still
        possible (V1_SPEC §6). In global scope there is a single unscoped pass.
        """
        if context.scope is ContextScope.GLOBAL:
            return [None]
        return [primary_ids, None]

    def _expand(self, entity_ids: list[int]) -> list[EvidenceCandidate]:
        """One hop from matched entities along typed edges."""
        if not entity_ids:
            return []
        edges = self._knowledge.neighbors(entity_ids[:20], _EXPANSION_EDGES, limit=200)
        seen = set(entity_ids)
        neighbor_ids = [
            eid
            for edge in edges
            for eid in (edge.src_id, edge.dst_id)
            if eid not in seen and not seen.add(eid)
        ]
        if not neighbor_ids:
            return []
        return self._knowledge_candidates(neighbor_ids)

    def _knowledge_candidates(self, entity_ids: list[int]) -> list[EvidenceCandidate]:
        rows = self._knowledge.describe_entities(entity_ids[:50])
        return [
            EvidenceCandidate(
                repository=row.repository,
                path=row.path,
                line_start=row.line_start,
                line_end=row.line_end,
                text=row.text,
                score=1.0,
                source=EvidenceSource.GRAPH,
                description=row.description,
                entity_id=row.entity_id,
            )
            for row in rows
        ]

    def _apply_budget(
        self, candidates: list[EvidenceCandidate]
    ) -> tuple[list[EvidenceCandidate], bool]:
        kept: list[EvidenceCandidate] = []
        tokens = 0
        for candidate in candidates:
            if len(kept) >= self._limits.final:
                return kept, True
            estimate = max(1, len(candidate.text) // 4)
            if tokens + estimate > self._limits.token_budget and kept:
                return kept, True
            tokens += estimate
            kept.append(candidate)
        return kept, False

    @staticmethod
    def _to_candidate(
        hit: ChunkHit, source: EvidenceSource, description: str = ""
    ) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository=hit.repository,
            path=hit.path,
            line_start=hit.line_start,
            line_end=hit.line_end,
            text=hit.text,
            score=hit.score,
            source=source,
            description=description,
        )
