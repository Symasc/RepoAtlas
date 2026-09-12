"""Questions RepoAtlas must refuse to answer (V1_SPEC §2, §14).

When a question **names something specific** — a quoted string, an environment
variable, a package, a dotted name, a path — and none of it exists anywhere in the
index, the honest answer is that it does not exist. Returning spans that merely
share a word with the question presents a guess as evidence for a literal, which
is the failure §2 exists to prevent.

This applies to *every* associative source, not only to embeddings. FTS recall on
`@company` and an entity coincidentally named `retries` are the same mistake as a
cosine neighbour: they answer a question the user did not ask, and they do it with
whatever tier the stage happens to assign.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.models.evidence import EvidenceSet
from repoatlas.services.retrieval_service import locate, locate_diagnostic
from repoatlas.services.workspace import Workspace

pytestmark = pytest.mark.adversarial


def _locate(workspace: Workspace, root: Path, repo: str, question: str) -> EvidenceSet:
    return locate(workspace, question, workspace.resolver().resolve(root / repo))


def _debug(workspace: Workspace, root: Path, repo: str, question: str) -> EvidenceSet:
    context = workspace.resolver().resolve(root / repo)
    return locate_diagnostic(workspace, question, context)


# -- literals that exist nowhere ---------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "ZZZ_NO_SUCH_TOKEN_918273",
        "nonexistent_function_918273",
        "@company/absent-918273",
        "absent.module.path.918273",
        "src/does_not_exist_918273.py",
        '"a quoted phrase that appears in no file at all"',
    ],
)
def test_a_named_literal_that_exists_nowhere_yields_no_evidence(
    adversarial: Workspace, adversarial_root: Path, question: str
) -> None:
    evidence = _locate(adversarial, adversarial_root, "python-service", question)
    assert evidence.candidates == (), [c.location for c in evidence.candidates]


def test_a_shared_prefix_does_not_make_a_different_package_the_answer(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """`@company/absent-918273` shares the `@company` scope with a real package.
    Lexical recall on the scope alone answers a question nobody asked."""
    evidence = _locate(adversarial, adversarial_root, "ts-service", "@company/absent-918273")
    assert evidence.candidates == ()


def test_a_prose_word_matching_an_entity_does_not_answer_a_missing_error_string(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """`debug` searches the whole question literally. When that phrase is absent, a
    config key coincidentally named `retries` is not the cause of the error."""
    evidence = _debug(
        adversarial,
        adversarial_root,
        "python-service",
        '"connection pool exhausted after 30 retries"',
    )
    assert evidence.candidates == (), [c.location for c in evidence.candidates]


def test_the_suppression_is_recorded_so_it_can_be_audited(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    evidence = _locate(adversarial, adversarial_root, "python-service", "ZZZ_NO_SUCH_TOKEN_918273")
    assert evidence.trace is not None
    assert evidence.trace.literal_terms
    assert not evidence.trace.semantic_used
    suppressed = [s for s in evidence.trace.stages if "suppressed" in s.detail]
    assert {s.name for s in suppressed} >= {"lexical", "symbol", "semantic"}


# -- the converse: suppression must not silence real questions ---------------


def test_a_named_literal_that_does_exist_is_answered_normally(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    evidence = _locate(adversarial, adversarial_root, "python-service", "SERVICE_HOST")
    assert evidence.candidates
    assert any("config.py" in c.path for c in evidence.candidates)


def test_a_prose_question_naming_no_literal_is_never_suppressed(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Suppression keys on *named* literals. A question that names none is exactly
    what the associative half of the funnel is for."""
    evidence = _locate(
        adversarial, adversarial_root, "python-service", "how does this service start up"
    )
    assert evidence.candidates
    assert evidence.trace is not None and not evidence.trace.literal_terms


def test_one_existing_literal_among_several_keeps_the_funnel_open(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Suppression requires that *none* of the named terms exist. One real term is
    enough to make the question answerable."""
    evidence = _locate(
        adversarial,
        adversarial_root,
        "python-service",
        "does SERVICE_HOST relate to ZZZ_NO_SUCH_TOKEN_918273",
    )
    assert evidence.candidates
    assert any("config.py" in c.path for c in evidence.candidates)


def test_secrets_are_unreachable_rather_than_approximated(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The sentinel exists on disk in eight files and in none of them is readable."""
    evidence = _locate(
        adversarial,
        adversarial_root,
        "python-service",
        "REPOATLAS_FAKE_SECRET_SENTINEL_918273",
    )
    assert evidence.candidates == ()
