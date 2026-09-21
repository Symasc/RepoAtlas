"""A word in a question is not a name the question asked for (V1_SPEC §14).

The defect this file exists for: the funnel offered **every** word of a question to
exact entity-name lookup and marked whatever came back `DETERMINISTIC` — the
strongest tier, ahead of every contextual candidate, and a graph seed besides. So

    How do the frontend apps call the Learning and Identity services?

was answered with whatever happened to be named ``call`` and ``services``, and

    What are the API endpoints in Identity right now?

produced fifteen deterministic matches for ``now``.

The words were never the problem; promoting them was. A question can mention
``call`` without asking about a symbol called ``call``, and the difference is
observable without understanding the sentence — the token is code-shaped, or a
naming phrase introduces it, or it *is* the whole query. Everything else is still
looked up and still returned, as recall.

Two failures this must not trade for the first one, both asserted below:

* a symbol genuinely named ``call`` stays findable — ``locate call`` still resolves
  it deterministically;
* **short prose is still prose.** ``apps call services`` is three words of English,
  not a three-symbol lookup, and no token-count threshold is allowed to make it one.

The ecosystem is built here rather than added to a shared fixture tree:
``tests/evaluation/retrieval_cases.yaml`` records that adding one file to the
adversarial corpus moved FTS5's corpus statistics and broke two unrelated tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceSet, EvidenceTier, MatchClass
from repoatlas.retrieval.query import analyze
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

#: Ordinary English words that are also real indexed symbol names here. Every one
#: was observed in the field promoting an unrelated file to the top of an answer.
GENERIC = ("call", "services", "name", "email", "now")

#: The prose question the defect was reported against, shortened to the fixture.
COMPOUND = "How do the frontend apps call the Learning and Identity services right now?"

#: Three words of prose. Short, and still prose.
SHORT_PROSE = "apps call services"

GENERIC_NAMES = '''"""Helpers whose names are ordinary English words."""


def call(target):
    """Invoke something. Nothing here is about the frontend."""
    return target()


def services():
    return []


def name():
    return "anonymous"


def email():
    return "nobody@example.invalid"


def now():
    return 0
'''

USERS_CONTROLLER = '''"""PascalCase, which no literal pattern used to recognise."""


class UsersController:
    def profile(self, user_id: str):
        return {"id": user_id}
'''

COURSES = '''"""A qualified symbol that appears in no file as contiguous text."""


class CoursesService:
    def detail(self, course_id: str):
        return {"id": course_id}
'''

BRIDGE = '''"""How the browser bundle reaches the backend over HTTP."""

import os

PLATFORM_HOST = os.environ.get("PLATFORM_HOST")
GATEWAY_TIMEOUT_SENTINEL = 30
'''

#: Production and test copies of one fact. Nothing may prefer either for its path.
SHARED_BODY = '''"""Holds the sentinel and nothing else."""

DUPLICATED_SENTINEL_XYZ = 1
'''

PLATFORM_FILES: dict[str, str] = {
    "src/generic_names.py": GENERIC_NAMES,
    "src/users_controller.py": USERS_CONTROLLER,
    "src/courses.py": COURSES,
    "src/bridge.py": BRIDGE,
    "src/shared.py": SHARED_BODY,
    "tests/test_shared.py": SHARED_BODY,
    "tests/test_gateway.py": (
        '"""Genuinely relevant evidence that happens to live under tests/."""\n\n'
        "GATEWAY_TIMEOUT_SENTINEL_ASSERTED = 30\n"
    ),
    "README.md": "The frontend apps talk to the backend services through a gateway.\n",
}

EDGE_FILES: dict[str, str] = {
    "src/other.py": (
        '"""A second repository, reading the same environment variable."""\n\n'
        "import os\n\n"
        'PLATFORM_HOST = os.environ.get("PLATFORM_HOST")\n'
    ),
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
    base = tmp_path_factory.mktemp("symbol-term-qualification")
    root = base / "repos"
    _write(root / "platform", PLATFORM_FILES)
    _write(root / "edge", EDGE_FILES)

    settings = Settings.model_validate(
        {
            "storage": {"database": str(base / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
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


def ask(workspace: Workspace, root: Path, question: str, where: str = "") -> EvidenceSet:
    return workspace.retriever().retrieve(question, _context(workspace, root, where))


def outcome(workspace: Workspace, root: Path, question: str, *, confined: bool = False):
    return workspace.retriever().retrieve_detailed(
        question, _context(workspace, root, "platform" if confined else ""), confined=confined
    )


def named_matches(evidence: EvidenceSet) -> set[str]:
    """``repo/path`` for every candidate the query claims to have named exactly."""
    return {
        f"{c.repository}/{c.path}" for c in evidence.candidates if c.match is MatchClass.EXACT_NAME
    }


def ranked(evidence: EvidenceSet) -> list[str]:
    return [f"{c.repository}/{c.path}" for c in evidence.candidates]


# -- what the analyzer classifies, and what it refuses to --------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # Code shape, already established, unchanged.
        ("Where is PLATFORM_HOST configured?", ("PLATFORM_HOST",)),
        # Compound PascalCase is code; a lone capitalised word is English.
        ("Does UsersController handle profiles?", ("UsersController",)),
        ("What are the API endpoints in Identity right now?", ()),
        ("How does Learning talk to Identity?", ()),
        # A naming phrase points at the next token, and never at itself.
        ("Find the symbol named call.", ("call",)),
        ("the method named detail", ("detail",)),
        # Prose, at every length. This is the rule a token count would break.
        (SHORT_PROSE, ()),
        (COMPOUND, ()),
        ("which client methods exist versus which service routes are implemented?", ()),
        # A one-token question is a lookup, and its own pieces come with it.
        ("call", ("call",)),
        ("UsersController", ("UsersController",)),
    ],
)
def test_only_a_named_token_qualifies_for_exact_symbol_lookup(
    question: str, expected: tuple[str, ...]
) -> None:
    assert analyze(question).symbol_terms == expected


def test_a_lone_path_brings_its_own_components(workspace: Workspace) -> None:
    """`locate api/config.py` is one token; `api` and `config` are pieces of it.

    They are not prose, and the module inside the named file really is called
    `config`. Nothing here reaches a multi-word question.
    """
    assert analyze("src/courses.py").symbol_terms == (
        "src/courses.py",
        "courses.py",
        "src",
        "courses",
    )


# -- explicit lookups keep their precedence ----------------------------------


def test_a_qualified_symbol_keeps_deterministic_precedence(
    workspace: Workspace, root: Path
) -> None:
    evidence = ask(workspace, root, "CoursesService.detail")
    named = [c for c in evidence.candidates if c.match is MatchClass.EXACT_NAME]
    assert named
    assert all(c.tier is EvidenceTier.DETERMINISTIC for c in named)
    assert any("courses.py" in c.path for c in named)


def test_a_bare_pascal_case_symbol_stays_reliable(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "UsersController")
    assert "platform/src/users_controller.py" in named_matches(evidence)


def test_a_symbol_named_like_a_word_is_still_findable(workspace: Workspace, root: Path) -> None:
    """The repair must not make `call` unsearchable. It makes it *asked for*."""
    evidence = ask(workspace, root, "call")
    assert "platform/src/generic_names.py" in named_matches(evidence)


def test_a_naming_phrase_restores_an_ordinary_word(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "Find the symbol named call.")
    assert "platform/src/generic_names.py" in named_matches(evidence)


# -- and prose does not acquire it -------------------------------------------


def test_short_prose_is_still_prose(workspace: Workspace, root: Path) -> None:
    """Three words are not three symbol lookups.

    The tokens are identical to the ones the tests above resolve exactly. What
    differs is that this question does not point at any of them as a name.
    """
    evidence = ask(workspace, root, SHORT_PROSE)
    assert "platform/src/generic_names.py" not in named_matches(evidence)


def test_generic_words_do_not_occupy_the_deterministic_head(
    workspace: Workspace, root: Path
) -> None:
    evidence = ask(workspace, root, COMPOUND)
    assert "platform/src/generic_names.py" not in named_matches(evidence)


def test_a_prose_occurrence_does_not_seed_the_graph(workspace: Workspace, root: Path) -> None:
    """Expansion labels everything it reaches DETERMINISTIC, so a prose word that
    seeded it would launder the whole neighbourhood into the strongest tier."""
    walked = outcome(workspace, root, SHORT_PROSE)
    resolved = workspace.knowledge.keys_for_entities(walked.seed_entity_ids)
    assert not [
        identity
        for identity in resolved.values()
        if identity.name in GENERIC and identity.type.value != "file"
    ]


def test_an_explicit_lookup_still_seeds_the_graph(workspace: Workspace, root: Path) -> None:
    walked = outcome(workspace, root, "Where is PLATFORM_HOST configured?")
    assert walked.seed_entity_ids
    resolved = workspace.knowledge.keys_for_entities(walked.seed_entity_ids)
    assert "env:PLATFORM_HOST" in {identity.key for identity in resolved.values()}


# -- nothing is preferred or penalised for its path --------------------------


def test_a_test_file_stays_eligible_when_it_holds_the_evidence(
    workspace: Workspace, root: Path
) -> None:
    evidence = ask(workspace, root, "GATEWAY_TIMEOUT_SENTINEL_ASSERTED")
    assert any("tests/test_gateway.py" in path for path in ranked(evidence))


def test_a_production_path_earns_no_priority_of_its_own(workspace: Workspace, root: Path) -> None:
    """Two files hold the same fact; one is under `src/` and one under `tests/`.

    Both must come back, with the same standing. The defect was never that test
    files existed — it was that relevance was classified wrongly — so nothing here
    may start grading a candidate by the directory it sits in.
    """
    evidence = ask(workspace, root, "DUPLICATED_SENTINEL_XYZ")
    found = {
        path: (c.tier, c.match)
        for path, c in zip(ranked(evidence), evidence.candidates, strict=True)
    }
    production = found.get("platform/src/shared.py")
    testing = found.get("platform/tests/test_shared.py")
    assert production is not None, ranked(evidence)
    assert testing is not None, ranked(evidence)
    assert production == testing


# -- established anchors and boundaries are untouched ------------------------


def test_exact_path_anchoring_is_unchanged(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "src/courses.py")
    assert ranked(evidence)[0] == "platform/src/courses.py"
    assert any(c.match is MatchClass.EXACT_PATH for c in evidence.candidates)


def test_repository_scope_stays_confined(workspace: Workspace, root: Path) -> None:
    confined = outcome(workspace, root, "Where is PLATFORM_HOST configured?", confined=True)
    assert confined.evidence.candidates
    assert {c.repository_slug for c in confined.evidence.candidates} <= {"platform", ""}


def test_tier_order_still_holds(workspace: Workspace, root: Path) -> None:
    for question in (COMPOUND, SHORT_PROSE, "call", "UsersController", "src/courses.py"):
        tiers = [c.tier for c in ask(workspace, root, question).candidates]
        assert tiers == sorted(tiers), question
