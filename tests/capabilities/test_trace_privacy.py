"""What a diagnostic trace is allowed to say (RC audit, RCA-08).

``include_trace=True`` asks RepoAtlas to describe its own work. It is not
permission to describe the host it is running on.

The semantic stage is the only stage in the funnel that survives its own failure.
Every other stage raises, and the operation-level boundary answers with the fixed
``INTERNAL_ERROR`` envelope RCA-03 established. Semantic retrieval is different by
design --- V1_SPEC §14 makes the deterministic stages the mandatory ones and the
vector stage an aid --- so an embedding backend that refuses degrades the stage
while the search still succeeds.

That is the right behaviour, and it had the wrong diagnostic.
``EmbeddingUnavailable`` interpolates whatever the backend raised, and the
retriever copied that string into the stage detail of a **successful** response. A
loader failure named a model cache directory under someone's home; an import
failure would name a shared object; an HTTP failure would quote a response body.
All of it reached the agent through ``search(include_trace=True)``, through MCP,
and through V1's ``locate --json``.

Two things are asserted here, and like RCA-03 they pull against each other on
purpose:

* **a backend's words never cross** --- no path, no sentinel, no fragment of the
  exception, in any field of the response;
* **RepoAtlas's own words all still do** --- the stage still reports that it was
  unavailable, still reports it *distinctly* from a stage that ran and matched
  nothing, and every other stage detail is untouched. A trace that answered
  everything with silence would trade a privacy defect for a blindness one.

The failure is injected at the backend, through a real
:class:`~repoatlas.embeddings.fastembed_backend.FastEmbedModel` whose loader
raises. Everything downstream of that --- the wrapping into
``EmbeddingUnavailable``, the stage handling, the trace, the response --- is
production code, which is the only way to prove the path the audit reported
rather than one this file imagined.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.errors import INTERNAL_ERROR_MESSAGE, capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.embeddings.fastembed_backend import FastEmbedModel
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import EvidenceItem
from repoatlas.models.evidence import MatchClass
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.retrieval.retriever import SEMANTIC_UNAVAILABLE
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: Wide enough to reach the semantic stage rather than being answered outright.
QUESTION = "how does the service read its configuration"

#: A question the fixture corpus answers deterministically *and* associatively, so
#: the two halves of a response can be told apart when one of them disappears.
MIXED_QUESTION = "src/config.py"

#: A question whose semantic stage runs and matches nothing. The contrast CASE 7
#: needs, and a real one: no stub backend is involved.
NO_MATCH_QUESTION = "where is the database url set"

#: The model id the fixture workspace embedded under. A backend must answer to the
#: same id, or the stage stops at "no vectors stored" and never loads anything.
MODEL_ID = "hashing-128"
DIM = 128


# --- sentinels, one per shape of private thing an exception can carry -------
POSIX_PATH = "/home/private-user/.cache/repoatlas/private.bin"
WINDOWS_PATH = r"C:\Users\PrivateUser\AppData\Local\repoatlas\private.bin"
UNC_PATH = r"\\server\private-share\model.bin"
SECRET = "RCA08_SECRET_SENTINEL_918273"  # noqa: S105 - a marker, not a secret
SOURCE = "RCA08_SOURCE_SENTINEL_PRIVATE_CODE"
CREDENTIAL_URL = "https://private-user:super-secret@example.invalid/model"
SQL = "SELECT secret FROM credentials"

SENTINELS = [
    pytest.param(POSIX_PATH, id="posix-path"),
    pytest.param(WINDOWS_PATH, id="windows-path"),
    pytest.param(UNC_PATH, id="unc-path"),
    pytest.param(SECRET, id="secret"),
    pytest.param(SOURCE, id="repository-source"),
    pytest.param(CREDENTIAL_URL, id="credential-url"),
    pytest.param(SQL, id="sql"),
]

#: The audit's own reproduction, verbatim.
AUDIT_MESSAGE = (
    "embedding loader failed: cannot open "
    "/home/private-user/.cache/huggingface/PRIVATE_MODEL_DIR "
    "RCA08_SECRET_SENTINEL_918273"
)


# ---------------------------------------------------------------------------
# Reading a response the way a leak would actually appear in one
# ---------------------------------------------------------------------------


def walk(value: object) -> list[str]:
    """Every string anywhere in the structure, unescaped.

    ``json.dumps`` escapes a backslash, so a Windows or UNC path searched for in
    serialized text can never match the form it was injected in. A sentinel that
    cannot match is a test that cannot fail, and this suite exists to catch leaks
    of exactly that shape. RCA-03 learned this the hard way; the lesson is reused
    rather than relearned.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [part for item in value.items() for part in walk(item)]
    if isinstance(value, (list, tuple)):
        return [part for item in value for part in walk(item)]
    return [repr(value)]


def every_string(response: SearchResponse) -> str:
    """The whole response as one searchable string, nested fields included."""
    return "\u0000".join(walk(response.model_dump(mode="json")))


# ---------------------------------------------------------------------------
# One service, one workspace, a backend that can be broken between calls
# ---------------------------------------------------------------------------


def refusing_backend(message: str) -> FastEmbedModel:
    """A real backend whose loader fails, answering to the indexed model id.

    The production ``_load`` path is what turns the loader's exception into
    ``EmbeddingUnavailable(f"embedding loader failed: {exc}")``. Constructing that
    wrapper by hand would test this file's idea of the message rather than the one
    the backend actually produces.
    """

    def loader() -> Any:
        raise OSError(message)

    return FastEmbedModel(MODEL_ID, dim=DIM, loader=loader)


def break_backend(workspace: Workspace, message: str = AUDIT_MESSAGE) -> None:
    """Replace the workspace's embedding backend with one that refuses.

    Mutated in place rather than through a second service, because a
    :class:`CapabilityService` closes the workspace it was given: two of them over
    one workspace would leave the second reading a closed database. The retriever
    is built per call, so swapping the backend between two searches is enough ---
    and is closer to what a real host does when a model cache disappears
    mid-session.
    """
    workspace._embeddings = refusing_backend(message)
    workspace._embeddings_built = True


def disable_backend(workspace: Workspace) -> None:
    """Configure embeddings off, which is a different state from broken."""
    workspace._embeddings = None
    workspace._embeddings_built = True


@pytest.fixture
def service(initialized: Workspace) -> Iterator[CapabilityService]:
    """One service over the indexed fixture workspace, healthy to begin with."""
    capability_service = CapabilityService(initialized)
    yield capability_service
    capability_service.close()


def search(
    capability_service: CapabilityService,
    question: str = QUESTION,
    *,
    trace: bool = True,
) -> SearchResponse:
    return capability_service.search(
        SearchRequest(context=GLOBAL, question=question, include_trace=trace)
    )


def semantic_stage(response: SearchResponse) -> Any:
    assert response.trace is not None
    return next(stage for stage in response.trace.stages if stage.name == "semantic")


def similar(response: SearchResponse) -> list[EvidenceItem]:
    """Evidence a vector neighbour put there, and nothing else."""
    return [item for item in response.evidence if item.match is MatchClass.SIMILAR]


def deterministic(response: SearchResponse) -> list[str]:
    return [item.location for item in response.evidence if item.tier.name == "DETERMINISTIC"]


# ---------------------------------------------------------------------------
# CASES 1-6: nothing the backend said crosses
# ---------------------------------------------------------------------------


def test_the_audit_reproduction_no_longer_discloses_the_path(
    service: CapabilityService, initialized: Workspace
) -> None:
    """CASE 1. The exact failure the RC audit reported, through the real path."""
    break_backend(initialized)
    response = search(service)

    # The finding was never that search broke. It was that search succeeded and
    # told the agent where the operator keeps their model cache.
    assert response.evidence
    assert response.trace is not None

    body = every_string(response)
    assert "/home/private-user/.cache/huggingface/PRIVATE_MODEL_DIR" not in body
    assert "PRIVATE_MODEL_DIR" not in body
    assert "embedding loader failed" not in body
    assert semantic_stage(response).detail == SEMANTIC_UNAVAILABLE


def test_the_audit_secret_sentinel_no_longer_crosses(
    service: CapabilityService, initialized: Workspace
) -> None:
    """CASE 2. The half of the audit message that is not a path."""
    break_backend(initialized)
    assert SECRET not in every_string(search(service))


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_no_shape_of_private_text_crosses(
    service: CapabilityService, initialized: Workspace, sentinel: str
) -> None:
    """CASES 1-6 generalized: every shape, over every field of the response.

    Parametrized rather than written once, because the failure mode this guards is
    *partial* coverage --- a fix that handles POSIX paths and lets a backslash
    through is precisely the defect RCA-03 found in its own tests.
    """
    break_backend(initialized, f"loader exploded: {sentinel}")
    body = every_string(search(service))

    assert sentinel not in body
    # Not only the whole string: a fix that split it and kept the interesting half
    # would sail past the assertion above.
    for fragment in sentinel.replace("\\", "/").split("/"):
        if len(fragment) > 8:
            assert fragment not in body, fragment


def test_the_exception_is_not_reachable_from_the_stage_at_all() -> None:
    """The structural half, so the guarantee cannot erode quietly.

    Asserting the text is absent proves today's behaviour. Asserting the handler
    never *binds* the exception proves it cannot come back later as "just the
    class name" or "just the first line", which is how a sanitization regresses
    without anyone deciding to regress it. The same reasoning gave
    ``_internal_error`` no exception parameter in RCA-03.
    """
    source = Path("repoatlas/retrieval/retriever.py").read_text(encoding="utf-8")

    assert "except EmbeddingUnavailable:" in source
    assert "except EmbeddingUnavailable as" not in source


# ---------------------------------------------------------------------------
# CASE 7: unavailable is not zero results
# ---------------------------------------------------------------------------


def test_a_failed_stage_is_not_reported_as_a_stage_that_matched_nothing(
    service: CapabilityService, initialized: Workspace
) -> None:
    """CASE 7. The distinction the sanitization is not allowed to erase.

    "The backend would not answer" and "the backend answered and nothing was close
    enough" have different remedies --- repair the host, or ask differently --- and
    an agent that cannot tell them apart will pick the wrong one. Both produce zero
    semantic candidates, so the whole difference lives in the diagnostic.

    Neither side of this is simulated: the fixture corpus genuinely returns no
    vector neighbours for this question, and the backend genuinely refuses.
    """
    ran = search(service, NO_MATCH_QUESTION)
    break_backend(initialized)
    failed = search(service, NO_MATCH_QUESTION)

    assert ran.trace is not None and failed.trace is not None
    # Both found nothing by similarity.
    assert semantic_stage(ran).candidates_out == 0
    assert semantic_stage(failed).candidates_out == 0
    # And they still say different things, in the machine field and in the prose.
    assert ran.trace.semantic_used is True
    assert failed.trace.semantic_used is False
    assert semantic_stage(ran).detail != semantic_stage(failed).detail
    assert semantic_stage(failed).detail == SEMANTIC_UNAVAILABLE
    assert "hit(s)" in semantic_stage(ran).detail
    assert "hit(s)" not in semantic_stage(failed).detail


def test_the_safe_detail_is_distinct_from_a_stage_that_was_never_configured(
    service: CapabilityService, initialized: Workspace
) -> None:
    """A stage that was switched off is not a stage that broke.

    Four situations produce no semantic candidates and the trace distinguishes all
    four. Collapsing them into one sentence would be a smaller leak and a bigger
    lie.
    """
    disable_backend(initialized)
    response = search(service)

    assert semantic_stage(response).detail == "embeddings not configured"
    assert semantic_stage(response).detail != SEMANTIC_UNAVAILABLE
    assert response.trace is not None
    assert response.trace.semantic_used is False


# ---------------------------------------------------------------------------
# CASE 8: include_trace=False
# ---------------------------------------------------------------------------


def test_without_a_trace_there_is_no_diagnostic_at_all(
    service: CapabilityService, initialized: Workspace
) -> None:
    """CASE 8. Sanitizing must not open a new place to leak from.

    The obvious wrong fix is to move the failure somewhere else --- a warning list,
    an error field, a note on the response --- which would hand the same text to
    every caller, including the ones that asked for no diagnostics at all.
    """
    break_backend(initialized)
    response = search(service, trace=False)

    assert response.trace is None
    body = every_string(response)
    assert SEMANTIC_UNAVAILABLE not in body
    assert SECRET not in body
    assert "PRIVATE_MODEL_DIR" not in body


def test_the_untraced_response_is_the_traced_one_minus_its_trace(
    service: CapabilityService, initialized: Workspace
) -> None:
    """Nothing else moved when the trace was withheld, and no field appeared."""
    break_backend(initialized)
    traced = search(service, trace=True).model_dump(mode="json")
    plain = search(service, trace=False).model_dump(mode="json")

    volatile = {"investigation_id", "trace", "ledger", "freshness"}
    assert {k: v for k, v in traced.items() if k not in volatile} == {
        k: v for k, v in plain.items() if k not in volatile
    }
    assert set(traced) == set(plain), "a field appeared or vanished with the trace"


# ---------------------------------------------------------------------------
# CASE 10: the diagnostics that were always safe are all still there
# ---------------------------------------------------------------------------


def test_every_other_stage_detail_survives_the_correction(service: CapabilityService) -> None:
    """CASE 10. The trace is still worth reading.

    Each of these is program-generated from RepoAtlas's own counters. None is
    interpolated from an exception and none was in scope --- so what matters is
    that all of them are still populated and still specific.
    """
    response = search(service)
    assert response.trace is not None
    details = {stage.name: stage.detail for stage in response.trace.stages}

    assert "literal term(s) in content" in details["exact"]
    assert "exact path" in details["file"]
    assert "exact entity name" in details["symbol"]
    assert "hop(s)" in details["graph"]
    assert details["rerank"].startswith("NoopReranker")
    assert "tokens" in details["budget"]
    # The healthy semantic stage still names the model and the hit count.
    assert MODEL_ID in details["semantic"]
    assert "hit(s)" in details["semantic"]


def test_the_stage_shape_of_a_trace_is_unchanged(
    service: CapabilityService, initialized: Workspace
) -> None:
    """Same stages, in the same order, whether or not the backend answered."""
    good = search(service)
    break_backend(initialized)
    bad = search(service)

    assert good.trace is not None and bad.trace is not None
    assert [s.name for s in good.trace.stages] == [s.name for s in bad.trace.stages]
    assert "semantic" in [s.name for s in bad.trace.stages]


def test_suppression_still_speaks_for_itself(service: CapabilityService) -> None:
    """The other producer of a semantic detail, and it is RepoAtlas's own sentence."""
    response = search(service, "RCA08_TERM_THAT_EXISTS_NOWHERE_918273")

    assert response.trace is not None
    assert response.suppressed is not None
    assert response.suppressed.message == semantic_stage(response).detail
    assert "suppressed" in semantic_stage(response).detail


# ---------------------------------------------------------------------------
# CASE 11 and §16: the answer itself is unchanged, and honest
# ---------------------------------------------------------------------------


def test_deterministic_evidence_is_unaffected_by_the_backend_failure(
    service: CapabilityService, initialized: Workspace
) -> None:
    """CASE 11. Fallback behaviour is what it always was.

    The deterministic stages neither know nor care that the vector stage failed, so
    the evidence they produced is the same evidence, in the same order.
    """
    good = search(service, MIXED_QUESTION)
    break_backend(initialized)
    bad = search(service, MIXED_QUESTION)

    assert deterministic(good), "the question must exercise a deterministic stage"
    assert deterministic(good) == deterministic(bad)
    # And everything the response says about itself, other than the evidence the
    # vector stage would have contributed, is the same answer.
    assert good.budget == bad.budget
    assert good.suppressed == bad.suppressed
    assert good.context == bad.context
    assert good.truncated == bad.truncated
    by_location = {item.location: item for item in good.evidence}
    for item in bad.evidence:
        counterpart = by_location[item.location]
        assert (item.tier, item.match, item.entity) == (
            counterpart.tier,
            counterpart.match,
            counterpart.entity,
        )
        assert item.ref.span_digest == counterpart.ref.span_digest


@pytest.mark.parametrize("question", [MIXED_QUESTION, QUESTION])
def test_a_failed_backend_fabricates_no_semantic_evidence(
    service: CapabilityService, initialized: Workspace, question: str
) -> None:
    """§16. A stage that did not run produced nothing, and claims nothing.

    The dangerous shape of this defect is not a missing result; it is a candidate
    that appears anyway, carrying a similarity it never had. The healthy run is
    asserted first so the second half cannot pass by checking an empty set.

    Two questions, because the assertion has to hold at the *stage* as well as in
    the final evidence, and those are not the same test. A fabrication that the
    budget happened to drop would leave the response clean while the funnel was
    already inventing candidates --- so a question with lexical recall is included
    deliberately, since that recall is what a plausible fabrication would be built
    out of.
    """
    good = search(service, question)
    assert similar(good), "the healthy run must produce vector-sourced evidence"

    break_backend(initialized)
    bad = search(service, question)

    assert bad.trace is not None
    assert bad.trace.semantic_used is False
    # At the stage: nothing was produced, not merely nothing that survived.
    assert semantic_stage(bad).candidates_out == 0
    # And in the answer: no item carries the vector stage's match class.
    assert similar(bad) == []
    # What is left was reached deterministically and is still fully formed.
    assert bad.evidence
    assert all(item.ref.span_digest for item in bad.evidence)


# ---------------------------------------------------------------------------
# CASE 12: the two boundaries stay apart
# ---------------------------------------------------------------------------


def test_a_fatal_failure_is_still_an_rca_03_envelope(
    service: CapabilityService, initialized: Workspace
) -> None:
    """CASE 12. Two error boundaries, two answers, neither borrowing the other's.

    A non-fatal stage failure becomes a safe trace on a successful response. An
    unrecognized failure of the whole operation becomes ``INTERNAL_ERROR`` with
    fixed text. Routing either into the other would be a regression: the first
    would turn a degraded search into a refusal, the second would turn a defect
    into a diagnostic nobody acts on.
    """
    error = capability_error(OSError(f"cannot open {POSIX_PATH} {SECRET}"))

    assert error.code == "INTERNAL_ERROR"
    assert error.message == INTERNAL_ERROR_MESSAGE
    envelope = "\u0000".join(walk(error.model_dump(mode="json")))
    assert POSIX_PATH not in envelope
    assert SECRET not in envelope

    # And the degraded search is not an error at all.
    break_backend(initialized)
    response = search(service)
    assert response.evidence
    assert response.trace is not None
    assert semantic_stage(response).detail == SEMANTIC_UNAVAILABLE
