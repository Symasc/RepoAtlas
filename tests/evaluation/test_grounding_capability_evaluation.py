"""The deterministic grounding-capability evaluation corpus (V2 Slice H, §39).

A fourth corpus, separate from retrieval, graph and dependencies, because it
measures something none of those does: whether a verdict about **provenance**
stays a verdict about provenance.

Retrieval is scored on recall — the right answer is near the top and "close"
earns a grade. Grounding has no such gradient. Either the citation names evidence
this investigation supplied, inside the span it supplied, or it does not, so
every case is scored exactly and closed on both sides. Closed on both sides
matters more here than anywhere else: a verifier that accepts everything passes
any expected-positive suite, and only the expected-negatives catch it.

The corpus carries one pair that is the whole point of the file.
``semantic-consistent`` and ``semantic-contradiction`` submit the same citation
against the same evidence with statements that mean opposite things, and the
corpus demands the **same** classification, and a dedicated test asserts it
directly rather than leaving it to two rows that a future edit could drift apart.

Ground truth was written by reasoning about which id is cited and what the ledger
holds for it. No expectation was taken from a run, and none was taken by reading
the cited source and judging whether it supports the sentence — that judgment is
exactly what this capability does not make.

No model is consulted and nothing is embedded.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import EvidenceItem
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFindingCode,
    GroundingVerdict,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace
from tests.evaluation.harness import evaluation_workspace

CASES_PATH = Path(__file__).parent / "grounding_cases.yaml"

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: The path a `summary: fabricated` case plants. Chosen so it exists in no fixture
#: repository, which is what makes the prose check's refusal correct rather than
#: an accident of which files happen to be indexed.
FABRICATED = "infra/never-indexed-secrets.yaml"

#: What a `path: wrong` / `slug: wrong` citation substitutes. Deliberately not a
#: real path: the point is that the id's reference wins, and using a real file
#: would let a reader wonder whether existence was what decided it.
WRONG_PATH = "src/this-file-is-not-what-that-id-refers-to.py"
WRONG_SLUG_SUFFIX = "-not-the-repository-that-id-came-from"


@dataclasses.dataclass(frozen=True, slots=True)
class ClaimSpec:
    statement: str
    cite: int | str | None
    extra_cite: int | str | None
    start: int | None
    end: int | None
    shift: str | None
    path: str | None
    slug: str | None
    omit: bool


@dataclasses.dataclass(frozen=True, slots=True)
class GroundingCase:
    id: str
    category: str
    supply: dict[str, Any]
    mutate: str
    summary: str | None
    claims: tuple[ClaimSpec, ...]
    verify_under: str
    expected_verdict: GroundingVerdict
    expected_findings: frozenset[GroundingFindingCode]
    expected_claims: int
    notes: str


def load_grounding_cases() -> tuple[GroundingCase, ...]:
    raw: dict[str, Any] = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases: list[GroundingCase] = []
    for entry in raw["cases"]:
        claims = tuple(
            ClaimSpec(
                statement=claim["statement"],
                cite=claim.get("cite"),
                extra_cite=claim.get("extra_cite"),
                start=claim.get("start"),
                end=claim.get("end"),
                shift=claim.get("shift"),
                path=claim.get("path"),
                slug=claim.get("slug"),
                omit=bool(claim.get("omit", False)),
            )
            for claim in entry["claims"]
        )
        cases.append(
            GroundingCase(
                id=entry["id"],
                category=entry["category"],
                supply=dict(entry["supply"]),
                mutate=entry.get("mutate", "none"),
                summary=entry.get("summary"),
                claims=claims,
                verify_under=entry.get("verify_under", "same"),
                expected_verdict=GroundingVerdict(entry["expected_verdict"]),
                expected_findings=frozenset(
                    GroundingFindingCode(code) for code in entry.get("expected_findings", ())
                ),
                expected_claims=int(entry["expected_claims"]),
                notes=entry.get("notes", ""),
            )
        )
    if len({case.id for case in cases}) != len(cases):
        raise AssertionError("grounding case ids must be unique")
    return tuple(cases)


CASES = load_grounding_cases()


@pytest.fixture(scope="module")
def shared_workspace() -> Iterator[tuple[Workspace, Path]]:
    """One indexed corpus, reused by every case that leaves it alone."""
    with evaluation_workspace() as (workspace, root):
        yield workspace, root


@pytest.fixture
def fresh_workspace() -> Iterator[tuple[Workspace, Path]]:
    """A corpus of its own, for a case that edits or deletes an indexed file."""
    with evaluation_workspace() as (workspace, root):
        yield workspace, root


@pytest.fixture
def grounding_workspace(
    request: pytest.FixtureRequest,
) -> Callable[[GroundingCase], tuple[Workspace, Path]]:
    """The workspace a case is entitled to.

    A case that moves the index gets its own, because the next case must not
    inherit a deleted file. Every other case shares one, which is what keeps a
    corpus this size from reindexing the adversarial repositories per assertion.
    """

    def choose(case: GroundingCase) -> tuple[Workspace, Path]:
        name = "fresh_workspace" if case.mutate != "none" else "shared_workspace"
        return request.getfixturevalue(name)  # type: ignore[no-any-return]

    return choose


# ---------------------------------------------------------------------------
# Supplying evidence
# ---------------------------------------------------------------------------


def _supply(
    service: CapabilityService, spec: dict[str, Any], investigation: str | None = None
) -> tuple[str, list[EvidenceItem]]:
    """Run the capability the case names, and return what it put in the ledger."""
    capability = spec["capability"]
    if capability == "search":
        response = service.search(
            SearchRequest(question=spec["question"], context=GLOBAL, investigation_id=investigation)
        )
        return response.investigation_id, list(response.evidence)
    if capability == "inspect":
        inspected = service.inspect(
            InspectRequest(
                context=CapabilityContext(
                    scope=CapabilityScope.REPOSITORY, repository=spec["repo"]
                ),
                target=InspectTarget(path=spec["path"]),
                line_start=spec["line_start"],
                line_end=spec["line_end"],
                context_lines=0,
                allow_worktree=False,
                investigation_id=investigation,
            )
        )
        return inspected.investigation_id, list(inspected.segments)
    if capability == "relationships":
        related = service.relationships(
            RelationshipsRequest(
                context=GLOBAL,
                target=spec["target"],
                max_neighbors=200,
                investigation_id=investigation,
            )
        )
        return related.investigation_id, list(related.evidence)
    if capability == "dependencies":
        deps = service.dependencies(
            DependenciesRequest(context=GLOBAL, limit=50, investigation_id=investigation)
        )
        return deps.investigation_id, list(deps.evidence)
    raise AssertionError(f"unknown supply capability: {capability}")


def _mutate(
    service: CapabilityService,
    workspace: Workspace,
    root: Path,
    case: GroundingCase,
    investigation: str,
    evidence: Sequence[EvidenceItem],
) -> None:
    """Move the index the way the case says, between supply and verification."""
    if case.mutate == "none":
        return
    from repoatlas.services.sync_service import sync

    cited = evidence[0]
    target = root / cited.ref.repository_slug / cited.ref.path
    if case.mutate == "edit-cited-file":
        target.write_text(
            "# a line inserted at the very top\n" + target.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    elif case.mutate == "delete-cited-file":
        target.unlink()
    elif case.mutate == "resupply-span":
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[0] = "# the first line, rewritten\n"
        target.write_text("".join(lines), encoding="utf-8")
    else:
        raise AssertionError(f"unknown mutation: {case.mutate}")

    sync(workspace, workspace.resolver().resolve(root))

    if case.mutate == "resupply-span":
        # Supply the same span again from the new bytes, which is what makes the
        # original id superseded rather than merely stale.
        _supply(service, dict(case.supply), investigation)


# ---------------------------------------------------------------------------
# Building the submitted answer
# ---------------------------------------------------------------------------


def _citation(spec: ClaimSpec, which: int | str, evidence: Sequence[EvidenceItem]) -> AgentCitation:
    if which == "unknown":
        return AgentCitation(evidence_id="E9999")
    item = evidence[-1] if which == "last" else evidence[int(which)]
    start, end = spec.start, spec.end
    if spec.shift == "inner":
        # One line strictly inside the supplied span.
        assert item.ref.line_start is not None and item.ref.line_end is not None
        assert item.ref.line_end > item.ref.line_start, f"{item.location} has no interior"
        start = end = item.ref.line_start + 1
    elif spec.shift == "below":
        # One line above the supplied span, reaching down into it.
        assert item.ref.line_start is not None and item.ref.line_start > 1
        start, end = item.ref.line_start - 1, item.ref.line_end
    return AgentCitation(
        evidence_id=item.id,
        repository_slug=(
            f"{item.ref.repository_slug}{WRONG_SLUG_SUFFIX}" if spec.slug == "wrong" else None
        ),
        path=WRONG_PATH if spec.path == "wrong" else None,
        line_start=start,
        line_end=end,
    )


def _answer(case: GroundingCase, evidence: Sequence[EvidenceItem]) -> AgentAnswer:
    claims: list[AgentClaim] = []
    for spec in case.claims:
        citations: list[AgentCitation] = []
        if not spec.omit:
            assert spec.cite is not None
            citations.append(_citation(spec, spec.cite, evidence))
            if spec.extra_cite is not None:
                citations.append(_citation(spec, spec.extra_cite, evidence))
        claims.append(
            AgentClaim(statement=spec.statement, claimed_confidence=0.9, citations=tuple(citations))
        )
    summary = (
        f"the value is overridden in {FABRICATED}"
        if case.summary == "fabricated"
        else "what the evidence shows"
    )
    return AgentAnswer(summary=summary, claims=tuple(claims))


def _run(
    workspace: Workspace, root: Path, case: GroundingCase
) -> tuple[CapabilityService, VerifyGroundingResponse]:
    service = CapabilityService(workspace)
    investigation, evidence = _supply(service, dict(case.supply))
    assert evidence, f"{case.id}: the fixture supplied no evidence"
    assert len(evidence) > max(
        (int(spec.cite) for spec in case.claims if isinstance(spec.cite, int)), default=0
    ), f"{case.id}: the fixture supplied too few pieces of evidence"

    _mutate(service, workspace, root, case, investigation, evidence)
    answer = _answer(case, evidence)

    verifying = investigation
    if case.verify_under == "other":
        # A second investigation, supplied deliberately narrowly so that the id the
        # answer cites is one this ledger genuinely never issued. Both ledgers do
        # hold an ``E1`` — that collision is the premise — so the case cites a
        # high id instead, and the refusal is unambiguous rather than a coincidence
        # of which file happened to be numbered first.
        other, held = _supply(
            service,
            {
                "capability": "inspect",
                "repo": "python-service",
                "path": "src/config.py",
                "line_start": 1,
                "line_end": 1,
            },
        )
        cited = {citation.evidence_id for claim in answer.claims for citation in claim.citations}
        assert cited.isdisjoint({entry.id for entry in held}), (
            f"{case.id}: the second investigation happens to hold the cited id "
            f"({sorted(cited)} vs {sorted(entry.id for entry in held)})"
        )
        verifying = other

    return service, service.verify_grounding(
        VerifyGroundingRequest(investigation_id=verifying, answer=answer)
    )


# ---------------------------------------------------------------------------
# Scored exactly, on both sides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_verdict_is_exactly_the_expected_one(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    workspace, root = grounding_workspace(case)
    _, response = _run(workspace, root, case)

    assert response.verdict is case.expected_verdict, case.notes


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_findings_are_exactly_the_expected_ones(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    """Closed on both sides: no missing finding, and no extra one either."""
    workspace, root = grounding_workspace(case)
    _, response = _run(workspace, root, case)

    assert {finding.code for finding in response.findings} == case.expected_findings, case.notes


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_sanitized_answer_keeps_exactly_the_expected_claims(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    workspace, root = grounding_workspace(case)
    _, response = _run(workspace, root, case)

    assert len(response.sanitized_answer.claims) == case.expected_claims, case.notes
    assert len(response.original_answer.claims) == len(case.claims)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_every_surviving_citation_lies_inside_what_was_supplied(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    """The property the whole capability exists for, checked on every case.

    Whatever the verdict, a verified range never reaches past the span the ledger
    holds for the id it names. A case that produced one would have manufactured
    provenance, which is the failure no amount of correct verdicts would excuse.
    """
    workspace, root = grounding_workspace(case)
    service, response = _run(workspace, root, case)
    ledger = service.sessions.get(response.investigation_id).ledger

    for verdict in response.citations:
        if verdict.verified_range is None:
            continue
        entry = ledger.get(verdict.evidence_id)
        assert entry is not None, verdict.evidence_id
        assert entry.ref.line_start is not None and entry.ref.line_end is not None
        assert verdict.verified_range.start >= entry.ref.line_start, case.notes
        assert verdict.verified_range.end <= entry.ref.line_end, case.notes


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_verification_allocates_no_evidence(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    """Across the whole corpus: verification consumes evidence and produces none."""
    workspace, root = grounding_workspace(case)
    service, response = _run(workspace, root, case)
    ledger = service.sessions.get(response.investigation_id).ledger
    before = {entry.id: entry for entry in ledger.entries()}

    service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=response.investigation_id, answer=response.original_answer
        )
    )

    assert {entry.id: entry for entry in ledger.entries()} == before, case.notes


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_no_response_carries_a_storage_identifier(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    workspace, root = grounding_workspace(case)
    _, response = _run(workspace, root, case)
    payload = json.dumps(json.loads(response.model_dump_json()))

    for surrogate in ("file_id", "entity_id", "edge_id", "dependency_id", "rowid", "fused_score"):
        assert f'"{surrogate}"' not in payload, f"{case.id} exposes {surrogate}"


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_no_response_uses_truth_vocabulary(
    grounding_workspace: Callable[[GroundingCase], tuple[Workspace, Path]],
    case: GroundingCase,
) -> None:
    """Across the corpus, not only on the happy path."""
    workspace, root = grounding_workspace(case)
    _, response = _run(workspace, root, case)
    payload = response.model_copy(update={"guarantee": None}).model_dump_json().lower()

    for word in ("supported", "unsupported", "entail", "verified true", "factual"):
        assert word not in payload, f"{case.id} says {word!r}"


# ---------------------------------------------------------------------------
# The pair that locks the product guarantee
# ---------------------------------------------------------------------------


def test_meaning_does_not_change_a_grounding_classification(
    shared_workspace: tuple[Workspace, Path],
) -> None:
    """The corpus's reason for existing, asserted rather than implied.

    Two rows expecting the same verdict could drift apart under a future edit and
    nobody would notice that the guarantee had moved. So the pair is compared
    directly: same citation, same evidence, opposite meanings, and every part of
    the response that describes grounding must be identical. Only the statement
    text differs, because only the statement text differed going in.
    """
    workspace, root = shared_workspace
    consistent = next(case for case in CASES if case.id == "semantic-consistent")
    contradiction = next(case for case in CASES if case.id == "semantic-contradiction")

    _, first = _run(workspace, root, consistent)
    _, second = _run(workspace, root, contradiction)

    assert first.verdict is second.verdict is GroundingVerdict.GROUNDED
    assert first.citations == second.citations
    assert first.findings == second.findings == ()
    assert first.dropped_claims == second.dropped_claims == 0
    assert first.clamped_citations == second.clamped_citations == 0
    assert len(second.sanitized_answer.claims) == 1
    assert (
        second.sanitized_answer.claims[0].statement
        == "this deletes every row of the production database"
    )


def test_the_corpus_scores_both_positives_and_negatives() -> None:
    """A suite of only-positives would pass against a verifier that accepts everything."""
    grounded = [case for case in CASES if case.expected_verdict is GroundingVerdict.GROUNDED]
    refused = [case for case in CASES if case.expected_verdict is GroundingVerdict.UNGROUNDED]
    partial = [
        case for case in CASES if case.expected_verdict is GroundingVerdict.PARTIALLY_GROUNDED
    ]

    assert len(grounded) >= 4
    assert len(refused) >= 6
    assert len(partial) >= 2


def test_the_corpus_covers_every_finding_the_capability_can_produce() -> None:
    """A code with no case is a code nobody has measured."""
    covered = {code for case in CASES for code in case.expected_findings}
    uncovered = set(GroundingFindingCode) - covered

    # ``unverified_evidence`` is unreachable through the capability: the snapshot
    # verification gathers covers every file the ledger names, so no entry can be
    # absent from it. It is pinned at the rule in test_verify_grounding_status.
    # ``citation_without_supplied_lines`` needs file-identity evidence, which the
    # adversarial corpus does not reliably produce; it is pinned there too.
    assert uncovered <= {
        GroundingFindingCode.UNVERIFIED_EVIDENCE,
        GroundingFindingCode.CITATION_WITHOUT_SUPPLIED_LINES,
    }, uncovered


def test_the_corpus_covers_every_supplying_capability() -> None:
    """Origin neutrality is a property of the corpus, not only of one test."""
    capabilities = {case.supply["capability"] for case in CASES}

    assert capabilities == {"search", "inspect", "relationships", "dependencies"}
