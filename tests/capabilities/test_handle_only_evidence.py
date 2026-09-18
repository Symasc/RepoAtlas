"""A candidate that names an entity and supplies no text is a handle, not evidence.

The LVL AuthZ audit found search admitting exact-name symbol hits as evidence with
``text=""`` and no line range. Each one took a rank slot and received a citable id,
and ``verify_grounding`` accepted a deliberately false claim resting on one of them:
the only check that constrains a citation — does it lie within what was supplied? —
cannot fail when nothing was supplied.

Withholding alone would have cost more than it fixed. For the query ``build_config``
the *only* candidate naming the file that defines it is that symbol hit: drop it and
the answer loses the definition site entirely. So the fix quotes first and withholds
second. A name hit now supplies the declaration the indexer stored — the same column
the graph stage has always quoted — and only an entity with no stored text at all is
withheld, as a handle.

File identity is untouched, because it is designed evidence (ARCHITECTURE.md §10,
*File-level provenance is representable*): a filename match, or a file an edge
reached, supports exactly "this file exists", and grounding already refuses to let a
line range rest on it. A module belongs with them — it is the file, which is why no
signature is stored for one. Exact-name hits that are withheld still reach the caller
as seed handles.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.search import shape_candidates
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import (
    AppliedBudget,
    CapabilityContext,
    CapabilityScope,
    EvidenceBudget,
    RetrievalBreadth,
)
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSource
from repoatlas.models.parsing import EntityType
from repoatlas.models.search import SearchRequest
from repoatlas.retrieval.retriever import RetrievalLimits
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
#: Ranks an exact-name symbol hit first in the fixture corpus: `servicePort` is
#: defined in ts-service/src/settings.ts and quoted in ts-service/src/index.ts.
SYMBOL_FIRST = "servicePort"
SERVICE_PORT = "sym:ts-service:src/settings.ts:servicePort:3"
#: What a text-less item may still claim: a file, or the module that is that file.
FILE_IDENTITY = {EntityType.FILE, EntityType.MODULE}


def _budget(*, max_items: int = 50) -> AppliedBudget:
    return AppliedBudget(
        max_items=max_items,
        max_total_chars=100_000,
        max_item_chars=100_000,
        breadth=RetrievalBreadth.STANDARD,
    )


def _candidate(
    *,
    path: str = "src/a.py",
    text: str = "one\ntwo\nthree",
    line_start: int | None = 10,
    line_end: int | None = 12,
    source: EvidenceSource = EvidenceSource.EXACT,
    entity_id: int | None = None,
    entity_type: EntityType | None = None,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository="svc",
        repository_slug="svc",
        path=path,
        line_start=line_start,
        line_end=line_end,
        text=text,
        source=source,
        entity_id=entity_id,
        entity_type=entity_type,
    )


def _handle(path: str = "src/h.py") -> EvidenceCandidate:
    """What the symbol stage builds for an exact-name hit: a line range, no text."""
    return _candidate(
        path=path,
        text="",
        line_start=16,
        line_end=21,
        source=EvidenceSource.SYMBOL,
        entity_id=62,
        entity_type=EntityType.FUNCTION,
    )


def _global_context(workspace: Workspace) -> RepositoryContext:
    return RepositoryContext(
        scope=ContextScope.GLOBAL,
        repositories=tuple(workspace.repositories.list_all()),
        primary=None,
        reason="test",
        cwd=None,
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "handle_only"),
    [
        pytest.param(_handle(), True, id="symbol-hit-without-text"),
        pytest.param(
            _candidate(
                text="",
                source=EvidenceSource.GRAPH,
                entity_id=7,
                entity_type=EntityType.FUNCTION,
            ),
            True,
            id="graph-symbol-without-signature",
        ),
        pytest.param(
            _candidate(
                text="",
                line_start=1,
                line_end=25,
                source=EvidenceSource.SYMBOL,
                entity_id=7,
                entity_type=EntityType.MODULE,
            ),
            False,
            id="a-module-is-its-file",
        ),
        pytest.param(
            _candidate(
                text="",
                line_start=None,
                line_end=None,
                source=EvidenceSource.FILE,
                entity_id=59,
            ),
            False,
            id="filename-match-is-file-identity",
        ),
        pytest.param(
            _candidate(
                text="",
                line_start=1,
                line_end=1,
                source=EvidenceSource.GRAPH,
                entity_id=90,
                entity_type=EntityType.FILE,
            ),
            False,
            id="file-reached-by-an-edge-is-file-identity",
        ),
        pytest.param(
            _candidate(
                text="",
                line_start=1,
                line_end=9,
                source=EvidenceSource.GRAPH,
                entity_id=72,
                entity_type=EntityType.SCRIPT,
            ),
            False,
            id="a-script-is-its-file",
        ),
        pytest.param(
            _candidate(
                text="",
                line_start=1,
                line_end=8,
                source=EvidenceSource.GRAPH,
                entity_id=73,
                entity_type=EntityType.TEMPLATE,
            ),
            False,
            id="a-template-is-its-file",
        ),
        pytest.param(
            _candidate(text="", line_start=None, line_end=None),
            False,
            id="no-entity-named-claims-only-its-file",
        ),
        pytest.param(
            _candidate(source=EvidenceSource.SYMBOL, entity_id=62, entity_type=EntityType.FUNCTION),
            False,
            id="an-entity-with-text-is-evidence",
        ),
    ],
)
def test_which_candidates_are_handles_only(candidate: EvidenceCandidate, handle_only: bool) -> None:
    assert candidate.is_handle_only is handle_only


# ---------------------------------------------------------------------------
# Shaping: the boundary where citable ids are minted
# ---------------------------------------------------------------------------


def test_shaping_never_admits_a_handle_only_candidate() -> None:
    """Whatever the funnel hands over, no id is issued for an entity with no text."""
    result = shape_candidates([_handle()], _budget())
    assert result.items == []
    assert result.withheld == 1


def test_a_withheld_handle_is_not_reported_as_an_omission() -> None:
    """Nothing citable was left out, so nothing was truncated."""
    result = shape_candidates([_handle(), _candidate()], _budget())
    assert [item.candidate.path for item in result.items] == ["src/a.py"]
    assert result.truncated is False
    assert result.dropped == 0


def test_a_withheld_handle_spends_no_slot() -> None:
    result = shape_candidates([_handle(), _candidate()], _budget(max_items=1))
    assert [item.candidate.path for item in result.items] == ["src/a.py"]


def test_a_handle_past_the_cap_is_not_an_omission() -> None:
    """A full response followed only by handles left nothing citable behind."""
    result = shape_candidates([_candidate(), _handle()], _budget(max_items=1))
    assert result.truncated is False
    assert result.dropped == 0


def test_a_handle_does_not_hide_a_real_omission() -> None:
    result = shape_candidates(
        [_candidate(), _handle(), _candidate(path="src/b.py")], _budget(max_items=1)
    )
    assert result.truncated is True
    assert result.dropped == 1


def test_the_omission_count_counts_only_what_could_have_been_cited() -> None:
    """Handles ranked after the stopping point were never candidates for a slot."""
    result = shape_candidates(
        [_candidate(), _candidate(path="src/b.py"), _handle(), _handle(path="src/i.py")],
        _budget(max_items=1),
    )
    assert result.dropped == 1


def test_file_identity_is_still_admitted_without_text() -> None:
    reached = _candidate(
        path="src/reached.py",
        text="",
        line_start=1,
        line_end=1,
        source=EvidenceSource.GRAPH,
        entity_id=90,
        entity_type=EntityType.FILE,
    )
    named = _candidate(
        path="src/named.py",
        text="",
        line_start=None,
        line_end=None,
        source=EvidenceSource.FILE,
        entity_id=59,
    )
    result = shape_candidates([reached, named], _budget())
    assert [item.candidate.path for item in result.items] == ["src/reached.py", "src/named.py"]
    assert all(item.line_start is None and item.text == "" for item in result.items)


# ---------------------------------------------------------------------------
# The funnel: withheld before the budget, so the slot goes to evidence
# ---------------------------------------------------------------------------


def test_the_funnel_quotes_an_entity_it_was_asked_to_quote(initialized: Workspace) -> None:
    context = _global_context(initialized)
    limits = RetrievalLimits(final=8, token_budget=100_000)

    plain = initialized.retriever(limits).retrieve_detailed(SYMBOL_FIRST, context)
    located = next(c for c in plain.evidence.candidates if c.source is EvidenceSource.SYMBOL)
    assert located.text == "", "V1 renders a name hit as a location and must keep doing so"
    assert located.is_handle_only

    quoted = initialized.retriever(limits).retrieve_detailed(
        SYMBOL_FIRST, context, quote_signatures=True
    )
    same = next(c for c in quoted.evidence.candidates if c.entity_id == located.entity_id)
    assert same.text, "the declaration the indexer stored was not supplied"
    assert not same.is_handle_only
    # Quoting changes what a candidate carries, never which entities seeded the walk.
    assert quoted.seed_entity_ids == plain.seed_entity_ids


def test_v1_retrieval_still_returns_what_it_always_returned(initialized: Workspace) -> None:
    """`locate` and `debug` are not a capability and mint no citable ids."""
    evidence = initialized.retriever().retrieve(SYMBOL_FIRST, _global_context(initialized))
    first = evidence.candidates[0]
    assert first.source is EvidenceSource.SYMBOL
    assert first.is_handle_only


# ---------------------------------------------------------------------------
# Search, end to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        SYMBOL_FIRST,
        "Where is load_config defined?",
        "helper_function getConfig",
        "src/config.py",
        "Where is SERVICE_HOST configured?",
    ],
)
def test_search_supplies_text_or_a_file_identity_and_nothing_else(
    initialized: Workspace, question: str
) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(question=question, context=GLOBAL, budget=EvidenceBudget(max_items=18))
    )
    for item in response.evidence:
        if item.text:
            continue
        assert item.ref.line_start is None, item.location
        assert item.entity is None or item.entity.type in FILE_IDENTITY, item.location


def test_a_named_symbol_is_quoted_rather_than_merely_located(initialized: Workspace) -> None:
    """The definition is often the only candidate naming the defining file at all."""
    response = CapabilityService(initialized).search(
        SearchRequest(question="load_config", context=GLOBAL, budget=EvidenceBudget(max_items=18))
    )
    defining = [
        item
        for item in response.evidence
        if item.ref.path == "src/config.py"
        and item.entity is not None
        and item.entity.type is EntityType.FUNCTION
    ]
    assert defining, "the definition of the named symbol is missing from the answer"
    assert defining[0].text, "the definition arrived as a location with nothing to quote"


def test_search_fills_its_slots_with_evidence(initialized: Workspace) -> None:
    """The first-ranked candidate is the definition, and it arrives quotable."""
    response = CapabilityService(initialized).search(
        SearchRequest(question=SYMBOL_FIRST, context=GLOBAL, budget=EvidenceBudget(max_items=2))
    )
    assert len(response.evidence) == 2
    assert all(item.text for item in response.evidence)


def test_a_withheld_symbol_still_reaches_the_caller_as_a_handle(initialized: Workspace) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(question=SYMBOL_FIRST, context=GLOBAL)
    )
    assert SERVICE_PORT in {handle.key for handle in response.entities}
    assert not any(
        item.entity is not None and item.entity.key == SERVICE_PORT and not item.text
        for item in response.evidence
    ), "a named entity reached the caller with nothing to quote"


def test_a_file_an_edge_reached_still_arrives_as_file_identity(initialized: Workspace) -> None:
    """`settings.ts` is reached by an EXPORTS edge from `servicePort`, and has no text."""
    response = CapabilityService(initialized).search(
        SearchRequest(question=SYMBOL_FIRST, context=GLOBAL, budget=EvidenceBudget(max_items=18))
    )
    reached = [
        item
        for item in response.evidence
        if item.entity is not None
        and item.entity.type is EntityType.FILE
        and item.ref.path == "src/settings.ts"
    ]
    assert reached, "the file the edge reached is missing from the evidence"
    assert reached[0].text == ""
    assert reached[0].ref.line_start is None


def test_a_named_file_still_arrives_as_file_identity(initialized: Workspace) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(question="src/config.py", context=GLOBAL)
    )
    named = [
        item
        for item in response.evidence
        if item.ref.path == "src/config.py" and item.ref.line_start is None and not item.text
    ]
    assert named, "the filename match no longer arrives as file identity"
