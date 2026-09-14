"""The verification contract: vocabulary, schema hygiene, and read-only proof.

Three guards that are cheap to write and expensive to lose.

**Vocabulary.** ``GROUNDED`` and ``SUPPORTED`` differ by one word and by the whole
guarantee. V1's internal grounding report genuinely uses "unsupported" for the
material it removes, and that vocabulary is correct where it lives — it describes
a citation that resolved to nothing. Promoting it to a public verdict would tell a
caller that RepoAtlas agreed with a sentence, which it never does. So the
translation happens at this boundary, and a test fails if anybody reverses it.

**Schema hygiene.** The public surface carries natural identities only:
``investigation_id`` plus ``E<n>`` for evidence, slug and path and line range for
provenance. A surrogate row id leaking into a response is not merely untidy — it
is an identity a caller may store, which outlives the row it names and cannot be
resolved by anyone else.

**Read-only.** Verification touches the standing of evidence and nothing else. It
runs no retrieval, opens no working-tree file, calls no model, and takes no writer
lock. The proof here is structural rather than observational: the write verbs are
made fatal and the call is run anyway.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFinding,
    GroundingFindingCode,
    GroundingGuarantee,
    GroundingVerdict,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE_HOST = "SERVICE_HOST"

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "repoatlas"

#: Every model on the public verification surface.
PUBLIC_MODELS = [
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFinding,
    GroundingGuarantee,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
]


def _grounded(service: CapabilityService) -> VerifyGroundingResponse:
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert found.evidence
    return service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="the host is configured in the indexed files",
                claims=(
                    AgentClaim(
                        statement="the host is configured here",
                        claimed_confidence=0.9,
                        citations=(AgentCitation(evidence_id=found.evidence[0].id),),
                    ),
                ),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------


def test_the_public_verdicts_are_grounding_verdicts() -> None:
    """Exactly three, and none of them is a word about truth."""
    assert {verdict.value for verdict in GroundingVerdict} == {
        "grounded",
        "partially_grounded",
        "ungrounded",
    }


@pytest.mark.parametrize(
    "forbidden",
    ["supported", "unsupported", "true", "false", "verified", "correct", "incorrect"],
)
def test_no_verdict_uses_truth_vocabulary(forbidden: str) -> None:
    """``SUPPORTED`` names a relation between evidence and meaning. This is not that."""
    for verdict in GroundingVerdict:
        assert forbidden not in verdict.value
        assert forbidden not in verdict.name.lower()


def test_no_finding_code_makes_a_semantic_judgment() -> None:
    """Every code names something the implementation can prove from the ledger.

    ``CLAIM_FALSE`` and ``DOES_NOT_ENTAIL`` are absent because nothing here is in a
    position to decide either. A finding a caller cannot act on deterministically
    is worse than no finding at all.
    """
    banned = ("false", "entail", "weak", "insufficient_reasoning", "truth", "wrong")
    for code in GroundingFindingCode:
        for word in banned:
            assert word not in code.value, f"{code.value} makes a semantic judgment"


@pytest.mark.parametrize("model", PUBLIC_MODELS, ids=lambda m: m.__name__)
def test_no_public_field_is_named_for_truth_or_agreement(model: type) -> None:
    """A field name is read far more often than a docstring, so it carries weight.

    ``confidence`` is banned bare and appears only as ``claimed_confidence``: the
    number belongs to the claimant, travels through unchanged or lowered, and is
    never RepoAtlas's assessment of anything. Attributing it in the name is what
    keeps a future reader from mistaking it for a verdict strength.
    """
    banned = {"supported", "unsupported", "entailment", "truth", "correct", "confidence"}
    for name in model.model_fields:  # type: ignore[attr-defined]
        assert name not in banned, f"{model.__name__}.{name}"
    if model is AgentClaim:
        assert "claimed_confidence" in model.model_fields  # type: ignore[attr-defined]


def test_the_guarantee_names_what_is_not_checked() -> None:
    """Machine-readable, so a client cannot present grounding as truth by accident."""
    guarantee = GroundingGuarantee()

    assert "semantic_entailment" in guarantee.not_checked
    assert "factual_accuracy_of_the_statement" in guarantee.not_checked
    assert "quality_of_the_reasoning" in guarantee.not_checked
    assert "completeness_of_the_investigation" in guarantee.not_checked
    assert not set(guarantee.checked) & set(guarantee.not_checked)


def test_the_capability_is_not_reachable_under_a_stronger_name() -> None:
    """No alias that promises more than the method delivers."""
    for name in ("verify_answer", "verify_claim", "fact_check", "validate_truth", "check_support"):
        assert not hasattr(CapabilityService, name), name
    assert hasattr(CapabilityService, "verify_grounding")


def test_the_sanitized_answer_is_not_called_corrected() -> None:
    """RepoAtlas removes what it cannot stand behind. It does not rewrite truth."""
    fields = set(VerifyGroundingResponse.model_fields)

    assert "sanitized_answer" in fields
    assert "corrected_answer" not in fields
    assert "fixed_answer" not in fields
    assert "improved_answer" not in fields


# ---------------------------------------------------------------------------
# Schema hygiene
# ---------------------------------------------------------------------------


SURROGATES = [
    "file_id",
    "entity_id",
    "edge_id",
    "dependency_id",
    "repository_id",
    "chunk_id",
    "rowid",
    "vector_distance",
    "fused_score",
    "score",
    "model_confidence",
]


@pytest.mark.parametrize("surrogate", SURROGATES)
def test_no_public_model_carries_a_surrogate_identity(surrogate: str) -> None:
    """A database row id a caller could store is an identity that outlives its row."""
    for model in PUBLIC_MODELS:
        schema = json.dumps(model.model_json_schema())  # type: ignore[attr-defined]
        assert f'"{surrogate}"' not in schema, f"{model.__name__} exposes {surrogate}"


def test_a_response_serializes_without_any_surrogate_or_absolute_path(
    initialized: Workspace,
) -> None:
    """The schema guard above, checked against a real payload as well."""
    service = CapabilityService(initialized)
    payload = json.loads(_grounded(service).model_dump_json())

    flat = json.dumps(payload)
    for surrogate in SURROGATES:
        assert f'"{surrogate}"' not in flat, surrogate
    assert str(initialized.settings.repos.root) not in flat
    # A host absolute path is an identity nobody else can resolve, and it leaks
    # where the operator keeps their code.
    for prefix in ("/tmp", "/home", "/Users", "C:\\"):  # noqa: S108 — matched, never opened
        assert f'"{prefix}' not in flat, prefix


def test_evidence_is_identified_by_investigation_and_e_number(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized)
    response = _grounded(service)

    assert response.investigation_id
    for verdict in response.citations:
        assert verdict.evidence_id.startswith("E")
        assert verdict.evidence_id[1:].isdigit()


def test_provenance_is_a_slug_a_path_and_a_line_range(initialized: Workspace) -> None:
    """The only coordinates a caller ever receives, and the only ones it may send."""
    service = CapabilityService(initialized)
    response = _grounded(service)

    verdict = response.citations[0]
    slug, _, rest = verdict.location.partition("/")
    assert slug and rest
    assert not verdict.location.startswith("/")


def test_no_public_model_uses_transport_or_vendor_vocabulary() -> None:
    """The capability layer is transport-neutral, and the schema has to prove it."""
    for model in PUBLIC_MODELS:
        schema = json.dumps(model.model_json_schema()).lower()  # type: ignore[attr-defined]
        for word in ("mcp", "jsonrpc", "stdio", "anthropic", "openai", "ollama", "tool_call"):
            assert word not in schema, f"{model.__name__} mentions {word}"


def test_the_request_cannot_carry_coordinates_without_an_evidence_id() -> None:
    """An E-id proves the coordinates were supplied; a path proves only that a file exists."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentCitation(
            repository_slug="teams/service", path="src/config.py", line_start=1, line_end=2
        )  # type: ignore[call-arg]


def test_a_citation_range_must_be_ordered() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentCitation(evidence_id="E1", line_start=20, line_end=10)


# ---------------------------------------------------------------------------
# Errors versus grounding failures
# ---------------------------------------------------------------------------


def test_a_grounding_failure_is_a_successful_call(initialized: Workspace) -> None:
    """The distinction that matters for a transport: refused is not broken.

    "Your answer is ungrounded" and "verification could not run" are different
    outcomes with different remedies, and an agent that cannot tell them apart will
    retry the wrong one.
    """
    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    response = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(AgentClaim(statement="x", citations=(AgentCitation(evidence_id="E999"),)),),
            ),
        )
    )

    assert isinstance(response, VerifyGroundingResponse)
    assert response.verdict is GroundingVerdict.UNGROUNDED
    assert response.findings


def test_a_malformed_request_maps_to_an_invalid_request_error(
    initialized: Workspace,
) -> None:
    from repoatlas.capabilities.errors import ErrorCategory, InvalidGroundingRequestError

    error = capability_error(InvalidGroundingRequestError("nothing to ground", field="answer"))

    assert error.code == "INVALID_GROUNDING_REQUEST"
    assert error.category is ErrorCategory.INVALID_REQUEST
    assert error.retryable is False
    assert error.context == {"field": "answer"}
    assert initialized is not None


def test_an_unknown_investigation_maps_to_a_session_error() -> None:
    from repoatlas.capabilities.errors import ErrorCategory
    from repoatlas.capabilities.session import UnknownInvestigationError

    error = capability_error(UnknownInvestigationError("investigation-nobody-issued"))

    assert error.code == "UNKNOWN_INVESTIGATION"
    assert error.category is ErrorCategory.SESSION
    assert error.retryable is False


# ---------------------------------------------------------------------------
# Read-only, and model-free
# ---------------------------------------------------------------------------


def test_verification_takes_no_writer_lock_and_runs_no_sync(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Made fatal, then the call is run anyway."""
    import repoatlas.services.sync_service as sync_service

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification attempted to write")

    monkeypatch.setattr(sync_service, "sync", forbidden)
    monkeypatch.setattr(type(initialized), "writer_lock", forbidden)
    monkeypatch.setattr(type(initialized.database), "transaction", forbidden)

    service = CapabilityService(initialized)
    assert _grounded(service).verdict is GroundingVerdict.GROUNDED


def test_verification_writes_no_indexed_fact(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every store's write verbs are fatal for the duration of the call."""
    from repoatlas.storage.sqlite import stores

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification wrote an indexed fact")

    targets = [
        (stores.SqliteFileStore, ("upsert", "delete", "mark_outdated")),
        (stores.SqliteRepositoryStore, ("upsert", "delete")),
        (stores.SqliteKnowledgeStore, ("add_entities", "add_edges", "replace_entities")),
        (stores.SqliteDependencyStore, ("replace", "upsert")),
    ]
    patched = 0
    for store, names in targets:
        for name in names:
            if hasattr(store, name):
                monkeypatch.setattr(store, name, forbidden)
                patched += 1
    assert patched, "no write verb was found to forbid"

    service = CapabilityService(initialized)
    assert _grounded(service).verdict is GroundingVerdict.GROUNDED


def test_verification_runs_no_retrieval(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No search, no ranking, no embedding. Verification reads a ledger."""
    from repoatlas.retrieval.retriever import Retriever

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification invoked the retriever")

    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert found.evidence

    monkeypatch.setattr(Retriever, "retrieve", forbidden)
    monkeypatch.setattr(Retriever, "retrieve_detailed", forbidden)

    response = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(
                        statement="x",
                        citations=(AgentCitation(evidence_id=found.evidence[0].id),),
                    ),
                ),
            ),
        )
    )

    assert response.verdict is GroundingVerdict.GROUNDED


def test_verification_reads_no_working_tree_file(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question is whether evidence matches the index, not the disk."""
    import repoatlas.containment as containment

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification opened a repository file")

    monkeypatch.setattr(containment.RepositoryBoundary, "resolve", forbidden, raising=False)

    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    response = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=found.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(
                        statement="x",
                        citations=(AgentCitation(evidence_id=found.evidence[0].id),),
                    ),
                ),
            ),
        )
    )

    assert response.verdict is GroundingVerdict.GROUNDED


def test_the_verification_modules_import_no_model_layer() -> None:
    """§30. The only reasoning-like function is a pure deterministic one."""
    for name in ("capabilities/verification.py", "models/verification.py"):
        source = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
        assert "repoatlas.llm" not in source, name
        assert "ollama" not in source.lower(), name
        assert "embedding" not in source.lower(), name


def test_grounding_is_imported_from_the_neutral_module_not_the_llm_layer() -> None:
    """Slice 0 moved it out of ``llm/`` precisely so this import is legal."""
    source = (PACKAGE_ROOT / "capabilities" / "verification.py").read_text(encoding="utf-8")

    assert "from repoatlas.grounding import" in source
    assert "from repoatlas.llm" not in source


def test_verification_succeeds_with_no_model_endpoint_reachable(
    initialized: Workspace,
) -> None:
    """The gate environment points the base URL at a closed port; nothing notices."""
    service = CapabilityService(initialized)

    assert _grounded(service).verdict is GroundingVerdict.GROUNDED


def test_the_verifier_stores_no_historical_source_text(initialized: Workspace) -> None:
    """The ledger keeps digests, and verification never wants bytes it does not have.

    If a future change made grounding depend on source text, this is where it would
    show: the verifier would have to start retaining it, and the ledger would become
    a second copy of the repository with none of its guarantees.
    """
    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    ledger = service.sessions.get(found.investigation_id).ledger

    for entry in ledger.entries():
        assert not hasattr(entry, "text")
        assert not hasattr(entry.ref, "text")

    # No field named ``text`` anywhere on the response, and therefore no path by
    # which source bytes could be carried back out of a ledger that never held them.
    assert '"text"' not in json.dumps(json.loads(_grounded(service).model_dump_json()))
    for model in PUBLIC_MODELS:
        assert "text" not in model.model_fields  # type: ignore[attr-defined]
