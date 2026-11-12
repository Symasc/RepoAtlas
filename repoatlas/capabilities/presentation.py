"""Writing a capability response out: in full, or compact.

The one place a response becomes the payload a transport sends. ``FULL`` is the
response exactly as the capability built it. ``COMPACT`` is a projection of that
same finished response, made after the ledger recorded everything in it, so the
retrieval, the recording and the verification behind a compact payload are the
full one's without exception. See :mod:`repoatlas.models.compact` for what is
written once instead of per item, and why nothing an agent acts on is lost.

A capability with no compact form — repositories, status, dependencies, sync —
takes no ``presentation`` field and is always written in full.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel

from repoatlas.models.capability import Presentation
from repoatlas.models.capability_io import EvidenceItem
from repoatlas.models.compact import (
    CompactEvidence,
    CompactFreshness,
    CompactGroundingResponse,
    CompactInspectResponse,
    CompactRelationshipsResponse,
    CompactRepositoryFreshness,
    CompactSearchResponse,
    CompactSource,
)
from repoatlas.models.freshness import IndexFreshness, severity
from repoatlas.models.impact import ImpactResponse
from repoatlas.models.inspection import InspectResponse
from repoatlas.models.relationships import RelationshipsResponse
from repoatlas.models.search import SearchResponse
from repoatlas.models.verification import VerifyGroundingResponse


def present(request: BaseModel, response: BaseModel) -> dict[str, Any]:
    """The JSON payload for ``response``, in the presentation ``request`` asked for."""
    if getattr(request, "presentation", Presentation.FULL) is not Presentation.COMPACT:
        return response.model_dump(mode="json")
    if isinstance(response, ImpactResponse):
        return compact_impact(response)
    # Null fields are omitted in the compact form, as its description says.
    return compact(response).model_dump(mode="json", exclude_none=True)


def compact_impact(response: ImpactResponse) -> dict[str, Any]:
    """An impact answer with its empty fields left out, sections kept.

    Every top-level section stays, empty or not, so an answer always has the same
    keys; inside candidates, steps and summaries an empty list, empty mapping or null
    is left out. Freshness is written for the repositories the answer draws on.
    """
    payload = response.model_dump(mode="json", exclude_none=True)
    drawn = {c.repository for c in (*response.candidates, *response.tests) if c.repository}
    if response.seed.repository:
        drawn.add(response.seed.repository)
    payload["freshness"] = _freshness(response.freshness, drawn).model_dump(
        mode="json", exclude_none=True
    )
    return {key: _pruned(value) if key != "freshness" else value for key, value in payload.items()}


def _pruned(value: Any) -> Any:
    if isinstance(value, dict):
        kept = {k: _pruned(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if v not in ({}, [], None)}
    if isinstance(value, list):
        return [_pruned(v) for v in value]
    return value


def compact(response: BaseModel) -> BaseModel:
    """The compact projection of one finished response."""
    if isinstance(response, SearchResponse):
        return compact_search(response)
    if isinstance(response, InspectResponse):
        return compact_inspect(response)
    if isinstance(response, RelationshipsResponse):
        return compact_relationships(response)
    if isinstance(response, VerifyGroundingResponse):
        return compact_grounding(response)
    # A capability without a compact form is written in full: nothing to fold.
    return response


def compact_search(response: SearchResponse) -> CompactSearchResponse:
    facts, sources = _sources(response.evidence)
    return CompactSearchResponse(
        investigation_id=response.investigation_id,
        scope=response.context.scope,
        repositories=response.context.repositories,
        within_path=response.within_path,
        facts_version=facts,
        sources=sources,
        evidence=_evidence(response.evidence),
        entities=tuple(handle.key for handle in response.entities),
        terms=response.terms,
        suppressed=response.suppressed,
        truncated=response.truncated,
        budget_clamped=response.budget.clamped or None,
        freshness=_freshness(
            response.freshness, (item.ref.repository_slug for item in response.evidence)
        ),
        ledger=response.ledger,
        trace=response.trace,
        withheld_crossings=response.withheld_crossings,
    )


def compact_inspect(response: InspectResponse) -> CompactInspectResponse:
    return CompactInspectResponse(
        investigation_id=response.investigation_id,
        target_evidence_status=response.target_evidence_status,
        file=response.file,
        segments=_evidence(response.segments),
        coverage=response.coverage,
        budget_clamped=response.budget.clamped or None,
        freshness=_freshness(response.freshness, (response.file.repository_slug,)),
        ledger=response.ledger,
    )


def compact_relationships(response: RelationshipsResponse) -> CompactRelationshipsResponse:
    facts, sources = _sources(response.evidence)
    drawn_on = {item.ref.repository_slug for item in response.evidence}
    for edge in response.edges:
        for end in (edge.source, edge.target):
            if end.repository_slug is not None:
                drawn_on.add(end.repository_slug)
    if response.target.repository_slug is not None:
        drawn_on.add(response.target.repository_slug)
    return CompactRelationshipsResponse(
        investigation_id=response.investigation_id,
        target=response.target,
        edges=response.edges,
        paths=response.paths,
        facts_version=facts,
        sources=sources,
        evidence=_evidence(response.evidence),
        truncated=response.truncated,
        limits=response.limits,
        freshness=_freshness(response.freshness, drawn_on),
        ledger=response.ledger,
    )


def compact_grounding(response: VerifyGroundingResponse) -> CompactGroundingResponse:
    changed = response.sanitized_answer != response.original_answer
    known = [repo.repository_slug for repo in response.freshness.repositories]
    cited = {
        slug
        for location in (
            *(citation.location for citation in response.citations),
            *(status.location for status in response.evidence_statuses),
        )
        if (slug := _slug_of(location, known)) is not None
    }
    return CompactGroundingResponse(
        investigation_id=response.investigation_id,
        verdict=response.verdict,
        guarantee=response.guarantee,
        answer_changed=changed,
        sanitized_answer=response.sanitized_answer if changed else None,
        findings=response.findings,
        citations=response.citations,
        evidence_statuses=response.evidence_statuses,
        dropped_claims=response.dropped_claims,
        dropped_citations=response.dropped_citations,
        clamped_citations=response.clamped_citations,
        ledger=response.ledger,
        freshness=_freshness(response.freshness, cited),
        checked_at=response.checked_at,
    )


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _evidence(items: Sequence[EvidenceItem]) -> tuple[CompactEvidence, ...]:
    return tuple(
        CompactEvidence(
            preview=item.preview,
            id=item.id,
            rank=item.rank,
            location=item.location,
            text=item.text,
            tier=item.tier,
            match=item.match,
            declaration=item.declaration,
            why=item.why,
            entity_key=item.entity.key if item.entity is not None else None,
            truncated=item.truncated,
            status=item.status,
        )
        for item in items
    )


def _sources(items: Sequence[EvidenceItem]) -> tuple[str | None, tuple[CompactSource, ...]]:
    """The response's facts version, and each distinct source file once.

    The response-level facts version is the one most of the evidence was built
    under, ties broken by the value itself so the choice is deterministic. A file
    built under any other says so on its own row rather than being folded into a
    claim that is not true of it.
    """
    if not items:
        return None, ()
    versions = Counter(item.ref.indexed_facts_version for item in items)
    facts = min(versions, key=lambda version: (-versions[version], version))
    sources: dict[tuple[str, str], CompactSource] = {}
    for item in items:
        ref = item.ref
        key = (ref.repository_slug, ref.path)
        if key in sources:
            continue
        sources[key] = CompactSource(
            repository_slug=ref.repository_slug,
            path=ref.path,
            content_hash=ref.file_content_hash,
            facts_version=None if ref.indexed_facts_version == facts else ref.indexed_facts_version,
        )
    return facts, tuple(sources.values())


def _freshness(full: IndexFreshness, drawn_on: Iterable[str]) -> CompactFreshness:
    """Freshness of the repositories a result draws on.

    Falls back to the full list when the result draws on none — an empty search
    still has to say how current the index it searched was, and "nothing to be
    current about" would be a claim about the index rather than about the result.
    """
    wanted = set(drawn_on)
    chosen = [repo for repo in full.repositories if repo.repository_slug in wanted]
    if not chosen:
        chosen = list(full.repositories)
    state = min(chosen, key=lambda repo: severity(repo.state)).state if chosen else full.state
    return CompactFreshness(
        depth=full.depth,
        state=state,
        repositories=tuple(
            CompactRepositoryFreshness(
                repository_slug=repo.repository_slug,
                state=repo.state,
                outdated_files=repo.outdated_files,
                last_indexed_at=repo.last_indexed_at,
                head_matches=repo.head_matches,
                working_tree_dirty=repo.working_tree_dirty,
            )
            for repo in chosen
        ),
        repository_set_drifted=full.repository_set_drifted,
    )


def _slug_of(location: str, known: Sequence[str]) -> str | None:
    """The repository a ``slug/path…`` location belongs to, by longest known slug.

    A slug can contain a slash (``teamA/common``), so the location cannot simply be
    split. The longest registered slug that prefixes it on a segment boundary is
    the owner: a file beneath a nested repository belongs to the nested one.
    """
    owners = [slug for slug in known if location.startswith(f"{slug}/")]
    return max(owners, key=len) if owners else None
