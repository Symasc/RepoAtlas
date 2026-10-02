"""An exact indexed entity is proof its name exists (V1_SPEC §2, §14).

The defect this file exists for: one retrieval run returned
``CoursesService.detail`` as a `DETERMINISTIC` / `EXACT_NAME` candidate and, in the
same breath, reported ``suppressed.reason = named_terms_absent`` naming that exact
term. Both halves came from the same funnel, and they contradicted each other.

The cause was an ordering one. Suppression is decided from what has been *proven to
exist*, and it counted two proofs: the literal text of a chunk (``exact``) and the
identity of a file (``file``). The third — the name of an indexed entity — was
produced by the ``symbol`` stage, which runs *after* the decision. A qualified
symbol is exactly the shape that slips through the first two: the source declares a
class and a method, and never those two spellings joined by a dot, so no chunk
contains ``CoursesService.detail`` and no file is called that. The index held it
under ``entities.qualified_name`` the whole time.

What must *not* change, and is asserted here rather than assumed:

* the proof is drawn only from spellings the question **named**. A config key
  coincidentally called ``retries`` exists, and must still not make "connection
  pool exhausted after 30 retries" look answerable — that guarantee lives in
  ``tests/adversarial/test_unanswerable.py`` and this file must not undermine it;
* a genuinely absent identifier still fails closed;
* the funnel still reports the same ten stages, and the ``symbol`` stage still
  renders the suppression sentence verbatim when suppression fires.

The ecosystem is built here rather than added to an existing fixture tree on
purpose: ``tests/evaluation/retrieval_cases.yaml`` records that adding one file to
the shared adversarial corpus moved FTS5's corpus statistics and broke two
unrelated tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceTier, MatchClass
from repoatlas.models.retrieval import RetrievalOutcome
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

#: The qualified symbol at the centre of the defect. Declared as a class and a
#: method, so this exact string appears **nowhere** in any indexed file.
QUALIFIED = "CoursesService.detail"

#: Shaped like a qualified symbol and indexed nowhere. Suppression must still fire.
ABSENT_QUALIFIED = "AbsentService.missingDetail"

#: Every stage the funnel reports, in order. Pinned here as well as in
#: ``tests/test_retrieval_ranking.py`` because moving the exact-name lookup earlier
#: is precisely the change that could have added a tenth.
STAGE_NAMES = (
    "exact",
    "file",
    "lexical",
    "symbol",
    "graph",
    "fuse",
    "dedup",
    "rerank",
    "budget",
)

#: Stages that go silent under suppression and render its sentence.
SUPPRESSIBLE = frozenset({"lexical", "symbol"})

LEARNING_SERVICE = '''"""Course reads for the learning surface."""


class CoursesService:
    """Reads courses. The class and the method are declared separately."""

    def __init__(self, courses):
        self.courses = courses

    def detail(self, course_id: str):
        """Return one course record."""
        return self.courses.by_id(course_id)
'''

LEARNING_REPOSITORY = '''"""Persistence for course records."""


class CoursesRepository:
    def by_id(self, course_id: str):
        return {"id": course_id, "title": "", "slug": ""}
'''

LEARNING_FILES: dict[str, str] = {
    "src/courses_service.py": LEARNING_SERVICE,
    "src/courses_repository.py": LEARNING_REPOSITORY,
    "README.md": "The learning service reads courses and returns course records.\n",
}

#: A second, independent declaration of the same qualified name. Ambiguity is
#: returned rather than resolved (V1_SPEC §14), so both must come back.
IDENTITY_FILES: dict[str, str] = {
    "src/courses_service.py": '''"""A second service that happens to share a name."""


class CoursesService:
    def detail(self, course_id: str):
        return {"id": course_id}
''',
    "src/only_here.py": '''"""Declared in this repository and nowhere else."""


class OnlyHereService:
    def solitary(self):
        return None
''',
}


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    init_git_repository(root)
    return root


@pytest.fixture(scope="module")
def ecosystem(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Workspace, Path]]:
    """Two repositories, indexed once. Every test here is read-only."""
    base = tmp_path_factory.mktemp("named-term-proof")
    root = base / "repos"
    _write(root / "learning", LEARNING_FILES)
    _write(root / "identity", IDENTITY_FILES)

    settings = Settings.model_validate(
        {
            "storage": {"database": str(base / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, root, config_path=base / "config.yaml")
    yield workspace, root
    workspace.close()


@pytest.fixture(scope="module")
def workspace(ecosystem: tuple[Workspace, Path]) -> Workspace:
    return ecosystem[0]


@pytest.fixture(scope="module")
def root(ecosystem: tuple[Workspace, Path]) -> Path:
    return ecosystem[1]


def _context(workspace: Workspace, root: Path, where: str = "") -> RepositoryContext:
    return workspace.resolver().resolve(root / where if where else root)


def ask(
    workspace: Workspace, root: Path, question: str, where: str = "", *, confined: bool = False
) -> RetrievalOutcome:
    return workspace.retriever().retrieve_detailed(
        question, _context(workspace, root, where), confined=confined
    )


def _stage(outcome: RetrievalOutcome, name: str):
    trace = outcome.evidence.trace
    assert trace is not None
    return next(stage for stage in trace.stages if stage.name == name)


# -- the contradiction is gone --------------------------------------------------


def test_a_qualified_symbol_exists_only_as_entity_metadata(workspace: Workspace) -> None:
    """The premise of the whole file: no file contains this string.

    If this ever stops holding, every other test here starts passing for the wrong
    reason — the literal stage would be proving the term rather than the entity
    index.
    """
    assert workspace.search.search_literal(QUALIFIED) == []
    assert workspace.search.find_files_by_path([QUALIFIED]) == []
    assert workspace.search.find_entities_by_name([QUALIFIED])


def test_an_indexed_qualified_symbol_is_not_reported_absent(
    workspace: Workspace, root: Path
) -> None:
    outcome = ask(workspace, root, f"What does {QUALIFIED} return?")
    assert outcome.suppression is None
    assert outcome.evidence.candidates


def test_the_same_run_returns_it_as_an_exact_name(workspace: Workspace, root: Path) -> None:
    outcome = ask(workspace, root, f"What does {QUALIFIED} return?")
    named = [
        candidate
        for candidate in outcome.evidence.candidates
        if candidate.match is MatchClass.EXACT_NAME
    ]
    assert named, "the entity the question spelled is missing from its own answer"
    assert all(candidate.tier is EvidenceTier.DETERMINISTIC for candidate in named)
    assert any("courses_service.py" in candidate.path for candidate in named)


def test_the_store_reports_which_spelling_matched(workspace: Workspace) -> None:
    """Proof is the store's own matched term, never re-derived downstream."""
    hits = workspace.search.find_entities_by_name([QUALIFIED])
    assert hits
    assert {hit.matched_term for hit in hits} == {QUALIFIED}


# -- and fail-closed behaviour is intact ----------------------------------------


def test_an_absent_qualified_symbol_still_suppresses(workspace: Workspace, root: Path) -> None:
    outcome = ask(workspace, root, f"What does {ABSENT_QUALIFIED} return?")
    assert outcome.suppression is not None
    assert outcome.suppression.reason == "named_terms_absent"
    assert ABSENT_QUALIFIED in outcome.suppression.terms
    assert outcome.evidence.candidates == ()


def test_a_prose_word_matching_an_entity_is_not_proof(workspace: Workspace, root: Path) -> None:
    """The `retries` trap, restated on this ecosystem.

    ``detail`` is a real indexed method name here. It is also an ordinary word in
    the question below, and the question's *named* term is a quoted string that
    exists nowhere. A word lifted out of prose must not prove the question.
    """
    outcome = ask(workspace, root, '"course detail lookup failed after 3 retries"')
    assert outcome.suppression is not None
    assert outcome.evidence.candidates == ()


# -- a compound question keeps working on its other half ------------------------


def test_a_valid_symbol_does_not_silence_the_rest_of_a_compound_question(
    workspace: Workspace, root: Path
) -> None:
    outcome = ask(
        workspace,
        root,
        f"What does {QUALIFIED} return, and what shape does the courses "
        f"repository produce for a course record?",
    )
    assert outcome.suppression is None
    details = {
        stage.name: stage.detail
        for stage in (outcome.evidence.trace.stages if outcome.evidence.trace else ())
        if stage.name in SUPPRESSIBLE
    }
    assert not any("suppressed" in detail for detail in details.values())
    paths = {candidate.path for candidate in outcome.evidence.candidates}
    assert any("courses_repository.py" in path for path in paths), (
        "the second half of the question was not retrieved for"
    )


# -- ambiguity is returned, never resolved --------------------------------------


def test_two_entities_sharing_a_qualified_name_both_come_back(workspace: Workspace) -> None:
    hits = workspace.search.find_entities_by_name([QUALIFIED])
    repositories = {hit.repository_slug for hit in hits}
    assert len(hits) >= 2
    assert repositories == {"learning", "identity"}


# -- repository confinement is untouched ----------------------------------------


def test_a_confined_search_is_not_proven_by_another_repository(
    workspace: Workspace, root: Path
) -> None:
    """A symbol that exists only in `identity` cannot answer a walk confined to
    `learning` — and, not existing *there*, must still fail closed."""
    outcome = ask(
        workspace, root, "What does OnlyHereService.solitary do?", "learning", confined=True
    )
    assert outcome.suppression is not None
    assert "OnlyHereService.solitary" in outcome.suppression.terms
    assert outcome.evidence.candidates == ()


def test_the_same_symbol_resolves_when_its_own_repository_is_the_scope(
    workspace: Workspace, root: Path
) -> None:
    outcome = ask(
        workspace, root, "What does OnlyHereService.solitary do?", "identity", confined=True
    )
    assert outcome.suppression is None
    assert {candidate.repository_slug for candidate in outcome.evidence.candidates} <= {
        "identity",
        "",
    }


# -- the trace still describes the funnel that exists ---------------------------


def test_the_funnel_still_reports_exactly_nine_stages(workspace: Workspace, root: Path) -> None:
    """Moving the exact-name lookup earlier must not add a tenth stage."""
    outcome = ask(workspace, root, f"What does {QUALIFIED} return?")
    assert outcome.evidence.trace is not None
    assert tuple(stage.name for stage in outcome.evidence.trace.stages) == STAGE_NAMES


def test_the_symbol_stage_still_renders_the_suppression_sentence(
    workspace: Workspace, root: Path
) -> None:
    outcome = ask(workspace, root, f"What does {ABSENT_QUALIFIED} return?")
    assert outcome.suppression is not None
    details = {
        stage.name: stage.detail
        for stage in (outcome.evidence.trace.stages if outcome.evidence.trace else ())
        if stage.name in SUPPRESSIBLE
    }
    assert set(details.values()) == {outcome.suppression.message}


def test_the_symbol_stage_still_carries_the_cost_of_its_own_lookup(
    workspace: Workspace, root: Path
) -> None:
    """The lookup moved; the accounting did not."""
    outcome = ask(workspace, root, f"What does {QUALIFIED} return?")
    assert _stage(outcome, "symbol").duration_ms > 0.0
