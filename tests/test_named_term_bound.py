"""A name the question asked for cannot be crowded out by the words around it.

The symbol stage resolves names exactly, under a per-source bound. It used to
resolve the names a question *named* and the words it merely *used* in one lookup,
under one bound, and a bound shared that way is a competition the named term can
lose: rows come back in key order, so a question naming ``CoursesService.detail``
beside "course" and "record" came back without the one entity it named whenever
those words matched more entities than the bound holds and their keys sorted
first. Found on the audited LVL snapshot, where the named method was absent from
the answer entirely while ``function record`` filled the head.

Now each group is resolved under its own bound, most exactly named first: what the
question spelled literally, then the other names it made explicit, then --- as
recall only --- the words it used. Recall is not reduced; it is just unable to
displace a name.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.evidence import EvidenceTier, MatchClass
from repoatlas.models.retrieval import RetrievalOutcome
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

#: More than the default per-source bound (30), so one shared cut cannot hold both.
NOISE = 40

#: A compound question: one qualified symbol, and ordinary words that are also the
#: names of many indexed functions.
QUESTION = "What does CoursesService.detail return for a course record?"

COURSES = '''"""The one entity the question names, by its qualified name."""


class CoursesService:
    def detail(self, course_id: str):
        return {"id": course_id}
'''


def _noise(index: int) -> str:
    return f'"""Noise module {index}."""\n\n\ndef record():\n    return {index}\n'


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    init_git_repository(root)


@pytest.fixture(scope="module")
def ecosystem(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Workspace, Path]]:
    base = tmp_path_factory.mktemp("named-term-bound")
    root = base / "repos"
    # `alpha` sorts before `learning`, so every noise key sorts before the target's.
    _write(root / "alpha", {f"src/n{i:02}.py": _noise(i) for i in range(NOISE)})
    _write(root / "learning", {"src/courses.py": COURSES})

    settings = Settings.model_validate(
        {
            "storage": {"database": str(base / "state" / "atlas.db")},
        }
    )
    ws = open_workspace(settings=settings, require_root=False)
    initialize(ws, root, config_path=base / "config.yaml")
    assert ws.settings.retrieval.per_source < NOISE, "the fixture no longer overflows the bound"
    yield ws, root
    ws.close()


@pytest.fixture(scope="module")
def workspace(ecosystem: tuple[Workspace, Path]) -> Workspace:
    return ecosystem[0]


@pytest.fixture(scope="module")
def outcome(ecosystem: tuple[Workspace, Path]) -> RetrievalOutcome:
    ws, root = ecosystem
    return ws.retriever().retrieve_detailed(QUESTION, ws.resolver().resolve(root))


def _entity_id(workspace: Workspace, qualified: str) -> int:
    row = workspace.database.execute(
        "SELECT id FROM entities WHERE qualified_name = ? ORDER BY key", (qualified,)
    ).fetchone()
    assert row is not None, qualified
    return int(row["id"])


def _symbol_stage_detail(outcome: RetrievalOutcome) -> str:
    trace = outcome.evidence.trace
    assert trace is not None
    return next(stage.detail for stage in trace.stages if stage.name == "symbol")


def test_the_named_symbol_is_resolved_by_name_despite_the_overflow(
    workspace: Workspace, outcome: RetrievalOutcome
) -> None:
    """The exact lookup itself returns it --- not full-text search standing in.

    Seeding is the observable that tells the two apart: only an exact-name hit on
    a named spelling seeds expansion, and the method the question named is not a
    full-text match for any word in it.
    """
    assert outcome.suppression is None
    exact = int(_symbol_stage_detail(outcome).split(" exact entity name")[0])
    assert exact >= 1
    assert _entity_id(workspace, "CoursesService.detail") in outcome.seed_entity_ids


def test_the_named_file_heads_the_answer_at_the_exact_tier(outcome: RetrievalOutcome) -> None:
    head = outcome.evidence.candidates[0]
    assert (head.repository, head.path) == ("learning", "src/courses.py")
    assert head.tier is EvidenceTier.DETERMINISTIC
    assert head.match is MatchClass.EXACT_NAME


def test_the_words_are_still_recalled_and_still_only_recall(
    workspace: Workspace, outcome: RetrievalOutcome
) -> None:
    """Recall is not reduced to make room: it is resolved after, not instead."""
    recalled = int(_symbol_stage_detail(outcome).split(" name recall")[0].rsplit(", ", 1)[1])
    assert recalled >= workspace.settings.retrieval.per_source

    for candidate in outcome.evidence.candidates:
        if candidate.description == "defines record":
            assert candidate.tier is EvidenceTier.ASSOCIATIVE
            assert candidate.match is not MatchClass.EXACT_NAME
