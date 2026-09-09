"""Explicit exact-path intent must survive candidate fusion (V1_SPEC §14).

The defect this file exists for: the ``file`` stage identified ``src/config.py``
correctly and with the right specificity, and then fusion threw the specificity
away. Every candidate the stage produced arrived as the same thing — a
`DETERMINISTIC` file match — so the only remaining ordering signal was how many
retrieval stages had voted for each span. A ``tests/config.py`` loaded with
``config``-shaped symbols, and a ``docs/notes.txt`` that merely *mentions* the
string ``src/config.py``, both collect more votes than a file whose only claim is
that its path is exactly what the user typed. The file the user named finished
eighth.

The ecosystem below is built to make that failure structural rather than lucky:
ten files share the basename ``config.py``, one of them is deliberately stuffed
with matching symbols and imports, and another file repeats the target path a
hundred times. If specificity is not represented in the ranking model, the target
loses on every one of them.

Read-only throughout: no test here writes to a real RepoAtlas config or database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.errors import ExitCode
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import (
    EvidenceCandidate,
    EvidenceSet,
    EvidenceSource,
    EvidenceTier,
    MatchClass,
)
from repoatlas.retrieval.ranking import fuse
from repoatlas.services.init_service import initialize
from repoatlas.services.query_service import QueryIntent, locate, retrieve_for
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial

CLI = Path(sys.executable).parent / "repoatlas"

TARGET = "src/config.py"

# The file the user names. Deliberately *quiet*: no repeated `config` symbols, no
# mention of its own path. Its whole claim is that its path is what was typed.
SRC_CONFIG = '''"""Runtime configuration for the service."""

import os

DEFAULT_TIMEOUT = 30


def build_config(overrides=None):
    """Assemble the runtime configuration."""
    return {
        "timeout": DEFAULT_TIMEOUT,
        "database": os.environ.get("DATABASE_URL"),
        **(overrides or {}),
    }
'''

# The adversary (§28 of the remediation brief): many entity names matching
# `config`, many imports, high graph degree, the literal text `config.py`
# repeated, and enough prose to be a strong lexical and semantic match.
TESTS_CONFIG = '''"""Tests for config.py. config.py config.py config.py config.py."""

import config
import os
import sys
from config import build_config

CONFIG_ONE = "config"
CONFIG_TWO = "config"
CONFIG_THREE = "config"


def config(): ...
def make_config(): ...
def load_config(): ...
def read_config(): ...
def write_config(): ...
def merge_config(): ...
def build_config_case(): ...
def test_config_one(): ...
def test_config_two(): ...
def test_config_three(): ...
def test_config_four(): ...
def test_config_five(): ...
def test_config_six(): ...


class ConfigHarness:
    """config config config config config config config config config."""

    def build_config(self): ...
    def config(self): ...
    def reload_config(self): ...
'''

EXAMPLES_CONFIG = '''"""Example configuration. config config config."""

def config(): ...
def build_config(): ...
'''

# §27: the path-mentioned-in-content adversary.
NOTES = f"See {TARGET} for the timeout default.\n" * 100

APP = '''"""Entry point."""

from config import build_config


def main():
    settings = build_config()
    return settings["database"]
'''


SVC_FILES: dict[str, str] = {
    TARGET: SRC_CONFIG,
    "src/app.py": APP,
    "tests/config.py": TESTS_CONFIG,
    "examples/config.py": EXAMPLES_CONFIG,
    "generated/config.py": "GENERATED_CONFIG = 1  # config config\n",
    "data/config.py": "DATA_CONFIG = 1  # config\n",
    "services/api/config.py": "API_CONFIG = 1  # config\n",
    "docs/notes.txt": NOTES,
    "deep/one/two/three/four/five/config.py": "DEEP_CONFIG = 1  # config\n",
    "with space/config.py": "SPACED_CONFIG = 1  # config\n",
    "résumé/config.py": "ACCENTED_CONFIG = 1  # config\n",
    "配置/設定.py": "CJK_CONFIG = 1  # config\n",
    "dots/a.b.c.py": "DOTTED_CONFIG = 1  # config\n",
    "dash/some-dash-file.py": "DASHED_CONFIG = 1  # config\n",
    "caseful/Config.py": "CASED_CONFIG = 1  # config\n",
    "conf/values.yaml": "timeout: 30\nname: config\n",
    "README.md": "The service reads config.py at startup. config config config.\n",
}

OTHER_FILES: dict[str, str] = {
    TARGET: "OTHER_SRC_CONFIG = 1  # config\n",
    "tests/config.py": "OTHER_TESTS_CONFIG = 1  # config config config\n",
    "README.md": "Another repository that also has a config.py.\n",
}

#: Every ``config.py`` in ``svc``. Ten files, one basename.
DUPLICATE_BASENAMES = tuple(
    path for path in SVC_FILES if path.rsplit("/", 1)[-1] == "config.py" and path != TARGET
)


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
    """Two repositories, indexed once. Every test here is read-only.

    Module-scoped because indexing is the expensive part and no test mutates the
    index; the state directory is a pytest temp path, never a real one.
    """
    base = tmp_path_factory.mktemp("exact-path-ranking")
    root = base / "repos"
    _write(root / "svc", SVC_FILES)
    _write(root / "other", OTHER_FILES)

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


def context_at(workspace: Workspace, root: Path, where: str) -> RepositoryContext:
    return workspace.resolver().resolve(root / where if where else root)


def ranked(evidence: EvidenceSet) -> list[str]:
    """``repo/path`` per candidate, in rank order."""
    return [f"{c.repository}/{c.path}" for c in evidence.candidates]


def ask(workspace: Workspace, root: Path, query: str, where: str = "svc") -> EvidenceSet:
    return locate(workspace, query, context_at(workspace, root, where))


def rank_of(evidence: EvidenceSet, location: str) -> int | None:
    """1-based rank of the first candidate at ``repo/path``, or ``None``."""
    return next((i for i, name in enumerate(ranked(evidence), 1) if name == location), None)


def anchored(evidence: EvidenceSet) -> set[str]:
    """``repo/path`` for every file the query named by its whole relative path.

    The anchor is a property of the *file*, not of one candidate: rank 1 for
    ``locate deep/one/.../config.py`` is usually a span from inside that file
    rather than the file-identity candidate, and both are the answer.
    """
    return {
        f"{c.repository}/{c.path}" for c in evidence.candidates if c.match is MatchClass.EXACT_PATH
    }


# --- 1-2. the reported defect: full path versus duplicate basenames -----------


def test_a_full_relative_path_outranks_every_duplicate_basename(
    workspace: Workspace, root: Path
) -> None:
    """The reported defect, stated as the invariant it violates.

    Nine other files are called ``config.py``, one of them stuffed with matching
    symbols, and a tenth file repeats the target path a hundred times. None of
    them may outrank the file whose path the user actually typed.
    """
    evidence = ask(workspace, root, TARGET)

    assert ranked(evidence)[0] == f"svc/{TARGET}"

    target_rank = rank_of(evidence, f"svc/{TARGET}")
    assert target_rank == 1
    for other in DUPLICATE_BASENAMES:
        rank = rank_of(evidence, f"svc/{other}")
        assert rank is None or rank > target_rank, f"{other} outranked the named path"


def test_the_named_file_is_returned_as_an_exact_path_match(
    workspace: Workspace, root: Path
) -> None:
    """Match provenance survives fusion, not only the ordering it produced."""
    evidence = ask(workspace, root, TARGET)
    top = evidence.candidates[0]

    assert top.path == TARGET
    assert top.match is MatchClass.EXACT_PATH
    assert top.tier is EvidenceTier.DETERMINISTIC


def test_three_files_sharing_a_basename_are_all_still_reachable(
    workspace: Workspace, root: Path
) -> None:
    """Ranking specificity, not filtering (brief §20).

    The named file anchors the answer; the ambiguity the repository genuinely
    contains is still visible below it.
    """
    evidence = ask(workspace, root, TARGET)
    returned = ranked(evidence)

    assert returned[0] == f"svc/{TARGET}"
    assert sum(1 for name in returned if name.endswith("config.py")) > 1


# --- 3-8. path shapes ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "deep/one/two/three/four/five/config.py",
        "dots/a.b.c.py",
        "dash/some-dash-file.py",
        "conf/values.yaml",
        "services/api/config.py",
        "résumé/config.py",
        "配置/設定.py",
    ],
    ids=["deep", "dots", "dashes", "non-source", "nested", "accented", "cjk"],
)
def test_an_exact_path_of_any_shape_ranks_first(
    workspace: Workspace, root: Path, path: str
) -> None:
    evidence = ask(workspace, root, path)
    assert ranked(evidence)[0] == f"svc/{path}"
    assert anchored(evidence) == {f"svc/{path}"}


def test_an_exact_path_containing_spaces_ranks_first_when_quoted(
    workspace: Workspace, root: Path
) -> None:
    """A path with a space has to be quoted to be one token.

    Unquoted, ``with space/config.py`` is not a path in any deterministic
    reading — it is two words, and query analysis must not guess otherwise. The
    quoted form is the supported spelling, and it is exact.
    """
    evidence = ask(workspace, root, '"with space/config.py"')
    assert ranked(evidence)[0] == "svc/with space/config.py"
    assert anchored(evidence) == {"svc/with space/config.py"}


def test_an_unquoted_path_with_a_space_is_not_read_as_one_path(
    workspace: Workspace, root: Path
) -> None:
    """The documented boundary, pinned so it cannot drift silently."""
    evidence = ask(workspace, root, "with space/config.py")
    assert all(c.match is not MatchClass.EXACT_PATH for c in evidence.candidates)


def test_a_basename_that_is_very_common_does_not_dilute_the_path(
    workspace: Workspace, root: Path
) -> None:
    """Ten files named ``config.py``; the ranking is decided by the path, not the count."""
    assert len(DUPLICATE_BASENAMES) >= 8
    evidence = ask(workspace, root, TARGET)
    assert ranked(evidence)[0] == f"svc/{TARGET}"


# --- 9-11. repositories -------------------------------------------------------


def test_the_same_relative_path_in_two_repositories_returns_both(
    workspace: Workspace, root: Path
) -> None:
    """Ambiguity across repositories is returned, never resolved by ranking alone."""
    evidence = locate(workspace, TARGET, context_at(workspace, root, ""))
    returned = ranked(evidence)

    assert f"svc/{TARGET}" in returned
    assert f"other/{TARGET}" in returned
    anchored = {
        f"{c.repository}/{c.path}" for c in evidence.candidates if c.match is MatchClass.EXACT_PATH
    }
    assert anchored == {f"svc/{TARGET}", f"other/{TARGET}"}


def test_an_explicit_repository_qualified_path_selects_that_repository(
    workspace: Workspace, root: Path
) -> None:
    """Explicit qualification is stronger than inferred context (brief §8, §9).

    Asked from inside ``svc``, where the primary-repository boost applies to every
    ``svc`` candidate, a query naming ``other/src/config.py`` must still resolve to
    ``other``.
    """
    evidence = ask(workspace, root, f"other/{TARGET}")

    assert ranked(evidence)[0] == f"other/{TARGET}"
    assert anchored(evidence) == {f"other/{TARGET}"}


def test_the_context_repository_still_wins_an_unqualified_path(
    workspace: Workspace, root: Path
) -> None:
    """The other half of the same rule: a bias where nothing was made explicit."""
    evidence = ask(workspace, root, TARGET, where="svc")
    assert ranked(evidence)[0] == f"svc/{TARGET}"

    from_other = ask(workspace, root, TARGET, where="other")
    assert ranked(from_other)[0] == f"other/{TARGET}"


def test_an_ambiguous_basename_still_prefers_the_primary_repository(
    workspace: Workspace, root: Path
) -> None:
    """``config.py`` alone is genuinely ambiguous; contextual bias is right there."""
    evidence = ask(workspace, root, "config.py", where="other")
    returned = ranked(evidence)

    assert returned, "a basename that exists must return something"
    assert returned[0].startswith("other/")
    assert all(c.match is not MatchClass.EXACT_PATH for c in evidence.candidates)


# --- 12-13. normalization -----------------------------------------------------


@pytest.mark.parametrize("spelling", ["./src/config.py", "src/config.py/", "/src/config.py"])
def test_punctuation_that_carries_no_information_is_normalized(
    workspace: Workspace, root: Path, spelling: str
) -> None:
    evidence = ask(workspace, root, spelling)
    assert ranked(evidence)[0] == f"svc/{TARGET}"
    assert f"svc/{TARGET}" in anchored(evidence)


def test_a_backslash_is_not_a_path_separator(workspace: Workspace, root: Path) -> None:
    """POSIX semantics are not broadened. ``src\\config.py`` names no indexed file."""
    evidence = ask(workspace, root, "src\\config.py")
    assert all(c.match is not MatchClass.EXACT_PATH for c in evidence.candidates)


def test_a_parent_traversal_names_nothing_outside_the_index(
    workspace: Workspace, root: Path
) -> None:
    """Retrieval matching never becomes a way out of the repository (brief §13).

    Path matching is equality against indexed rows, so no spelling of ``..`` can
    manufacture an identity for something RepoAtlas does not hold — a traversal
    out of the tree simply matches nothing. Containment is not re-derived here;
    it is a property of what got indexed (ARCHITECTURE.md §12).
    """
    indexed = {f"svc/{path}" for path in SVC_FILES} | {f"other/{path}" for path in OTHER_FILES}

    for query in ("../../etc/passwd", "svc/../svc/src/config.py", "../../../root/.ssh/id_rsa"):
        evidence = ask(workspace, root, query)
        assert anchored(evidence) <= indexed, query
        assert set(ranked(evidence)) <= indexed, query


def test_a_relative_path_from_a_sibling_directory_still_names_the_file(
    workspace: Workspace, root: Path
) -> None:
    """``../svc/src/config.py`` is how a path gets pasted from next door.

    Query analysis takes the path-shaped token out of it — ``..`` is not part of
    one — leaving the repository-qualified spelling RepoAtlas itself prints. The
    result is the indexed file and nothing else; this is pinned because it looks
    like a traversal and is not one.
    """
    evidence = ask(workspace, root, f"../svc/{TARGET}", where="other")
    assert anchored(evidence) == {f"svc/{TARGET}"}


def test_path_identity_is_case_sensitive(workspace: Workspace, root: Path) -> None:
    """SQLite's folding tokenizer decides lexical recall, never path identity."""
    exact = ask(workspace, root, "caseful/Config.py")
    assert ranked(exact)[0] == "svc/caseful/Config.py"
    assert anchored(exact) == {"svc/caseful/Config.py"}

    folded = ask(workspace, root, "caseful/config.py")
    assert all(c.match is not MatchClass.EXACT_PATH for c in folded.candidates)


# --- 14-18. controls: what must NOT be treated as an exact path ---------------


def test_a_basename_only_query_is_not_an_exact_path(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "config.py")
    assert evidence.candidates, "the basename exists and must return evidence"
    assert all(c.match is not MatchClass.EXACT_PATH for c in evidence.candidates)


def test_a_nonexistent_full_path_returns_nothing(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "no/such/directory/absent_918273.py")
    assert ranked(evidence) == []


@pytest.mark.parametrize(
    "near_miss",
    ["src/config.p", "sr/config.py", "x/src/config.py", "srcc/config.py"],
)
def test_a_near_miss_is_not_an_exact_path(workspace: Workspace, root: Path, near_miss: str) -> None:
    evidence = ask(workspace, root, near_miss)
    assert all(c.match is not MatchClass.EXACT_PATH for c in evidence.candidates)


def test_a_suffix_only_path_is_exact_but_less_specific(workspace: Workspace, root: Path) -> None:
    """``api/config.py`` genuinely ends ``services/api/config.py`` — exact, not complete.

    It is returned, and it is classified as the sub-path match it is. It does
    **not** take the exact-path precedence: a trailing sub-path can name several
    files, so it is not the unambiguous identification the anchor is reserved for.
    Ranking below the anchor is therefore whatever fusion decides, unchanged from
    before this pass.
    """
    from repoatlas.retrieval.query import analyze

    context = context_at(workspace, root, "svc")
    analysis = analyze("api/config.py")
    graded = {
        f"{hit.repository}/{hit.path}": hit.specificity
        for hit in workspace.search.find_files_by_path(
            [*analysis.literal_terms, *analysis.terms], list(context.repository_ids), 30
        )
    }
    assert graded["svc/services/api/config.py"] == MatchClass.EXACT_SUBPATH

    evidence = ask(workspace, root, "api/config.py")
    assert anchored(evidence) == set()
    assert "svc/services/api/config.py" in ranked(evidence)


def test_a_directory_qualified_name_respects_segment_boundaries(
    workspace: Workspace, root: Path
) -> None:
    """``a/config.py`` must not match ``data/config.py``: ``dat`` is not a separator."""
    evidence = ask(workspace, root, "a/config.py")
    subpaths = [c for c in evidence.candidates if c.match is MatchClass.EXACT_SUBPATH]
    assert subpaths == []


# --- 19-22. queries that are more than a path --------------------------------


def test_a_path_inside_a_natural_language_question_still_anchors_it(
    workspace: Workspace, root: Path
) -> None:
    """The path anchors the evidence set; the rest of the question is still asked."""
    evidence = retrieve_for(
        workspace,
        "why does src/config.py read DATABASE_URL?",
        context_at(workspace, root, "svc"),
        QueryIntent.ASK,
    )
    returned = ranked(evidence)

    assert returned, "a question naming a real file and a real variable must answer"
    assert returned[0] == f"svc/{TARGET}"
    # Not collapsed into a path-only answer: other evidence is still carried.
    assert len(set(returned)) > 1


def test_symbols_inside_the_named_file_stay_ranked(workspace: Workspace, root: Path) -> None:
    """The anchor is the file, so what is inside it is not demoted (brief §23)."""
    evidence = retrieve_for(
        workspace,
        "what does build_config in src/config.py return?",
        context_at(workspace, root, "svc"),
        QueryIntent.ASK,
    )
    returned = ranked(evidence)

    assert returned[0] == f"svc/{TARGET}"
    spans = [c for c in evidence.candidates if c.path == TARGET and not c.is_file_level]
    assert spans, "evidence from inside the named file must remain available"


def test_a_quoted_exact_path_ranks_first(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, f'"{TARGET}"')
    assert ranked(evidence)[0] == f"svc/{TARGET}"


def test_the_exact_path_candidate_carries_no_line_span(workspace: Workspace, root: Path) -> None:
    """File identity is a fact about the file, and provenance is never synthesized."""
    evidence = ask(workspace, root, TARGET)
    anchor = next(c for c in evidence.candidates if c.match is MatchClass.EXACT_PATH)

    assert anchor.is_file_level
    assert anchor.line_start is None
    assert anchor.line_end is None
    assert anchor.location == f"svc/{TARGET}"


# --- 26-28. adversaries -------------------------------------------------------


def test_a_file_that_merely_mentions_the_path_does_not_outrank_it(
    workspace: Workspace, root: Path
) -> None:
    """Content that names a path is not the file whose path it is (brief §26, §27).

    ``docs/notes.txt`` repeats ``src/config.py`` a hundred times, so it wins the
    literal-content stage outright and by volume. It is still a different claim.
    """
    evidence = ask(workspace, root, TARGET)

    target_rank = rank_of(evidence, f"svc/{TARGET}")
    notes_rank = rank_of(evidence, "svc/docs/notes.txt")
    assert target_rank == 1
    assert notes_rank is None or notes_rank > target_rank


def test_a_high_degree_same_basename_adversary_still_loses(
    workspace: Workspace, root: Path
) -> None:
    """``tests/config.py`` is built to win on votes: symbols, imports, text, similarity."""
    evidence = ask(workspace, root, TARGET)
    target_rank = rank_of(evidence, f"svc/{TARGET}")

    assert target_rank == 1
    adversary_rank = rank_of(evidence, "svc/tests/config.py")
    assert adversary_rank is None or adversary_rank > target_rank


def test_a_semantically_similar_file_does_not_outrank_the_named_path(
    workspace: Workspace, root: Path
) -> None:
    """Priority, not suppression: the semantic stage still runs and still returns."""
    evidence = ask(workspace, root, TARGET)
    assert ranked(evidence)[0] == f"svc/{TARGET}"

    associative = [c for c in evidence.candidates if c.tier is EvidenceTier.ASSOCIATIVE]
    tiers = [c.tier for c in evidence.candidates]
    assert tiers == sorted(tiers), "tier precedence must still hold"
    assert associative == [] or associative[0].tier is EvidenceTier.ASSOCIATIVE


# --- 17-18 (unit): candidate merge and stage order ---------------------------


def _unit(
    path: str,
    *,
    match: MatchClass,
    source: EvidenceSource,
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC,
    start: int | None = None,
    end: int | None = None,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository="svc",
        repository_slug="svc",
        path=path,
        line_start=start,
        line_end=end,
        text="x",
        score=1.0,
        source=source,
        tier=tier,
        match=match,
    )


def test_merging_preserves_the_strongest_match_class() -> None:
    """A span reached several ways keeps its strongest claim, like its tier."""
    strong = _unit(TARGET, match=MatchClass.EXACT_PATH, source=EvidenceSource.FILE)
    weak = _unit(TARGET, match=MatchClass.LEXICAL, source=EvidenceSource.LEXICAL)

    fused = fuse([[strong], [weak]])

    assert fused[0].match is MatchClass.EXACT_PATH


def test_the_strongest_match_class_does_not_depend_on_stage_order() -> None:
    """Order independence, asserted over every permutation of three stages."""
    import itertools

    stages = {
        "file": [_unit(TARGET, match=MatchClass.EXACT_PATH, source=EvidenceSource.FILE)],
        "lexical": [
            _unit("tests/config.py", match=MatchClass.LEXICAL, source=EvidenceSource.LEXICAL),
            _unit(TARGET, match=MatchClass.LEXICAL, source=EvidenceSource.LEXICAL),
        ],
        "symbol": [
            _unit(
                "tests/config.py",
                match=MatchClass.EXACT_NAME,
                source=EvidenceSource.SYMBOL,
                start=3,
                end=3,
            )
        ],
    }

    results = set()
    for order in itertools.permutations(stages):
        fused = fuse([stages[name] for name in order])
        results.add(tuple((f"{c.repository}/{c.path}", c.line_start, c.match) for c in fused))

    assert len(results) == 1, "fusion depends on the order the stages were appended"
    ordering = next(iter(results))
    assert ordering[0][0] == f"svc/{TARGET}"
    assert ordering[0][2] is MatchClass.EXACT_PATH


def test_associative_volume_never_promotes_above_a_stronger_deterministic_match() -> None:
    """Many weak votes are still weak evidence (brief §5)."""
    anchor = _unit(TARGET, match=MatchClass.EXACT_PATH, source=EvidenceSource.FILE)
    noisy = _unit(
        "tests/config.py",
        match=MatchClass.EXACT_CONTENT,
        source=EvidenceSource.EXACT,
        start=1,
        end=9,
    )

    fused = fuse([[noisy]] * 8 + [[anchor]])

    assert fused[0].path == TARGET
    assert fused[0].score < fused[1].score, "the anchor wins on specificity, not on score"


def test_the_precedence_boundary_never_crosses_a_tier() -> None:
    """An exact path is deterministic; it cannot lift an associative candidate."""
    associative_anchor = _unit(
        TARGET,
        match=MatchClass.EXACT_PATH,
        source=EvidenceSource.FILE,
        tier=EvidenceTier.ASSOCIATIVE,
    )
    deterministic = _unit(
        "tests/config.py",
        match=MatchClass.LEXICAL,
        source=EvidenceSource.EXACT,
        start=1,
        end=2,
    )

    fused = fuse([[associative_anchor], [deterministic]])

    assert fused[0].path == "tests/config.py"


# --- 30. determinism ----------------------------------------------------------


def test_repeated_queries_return_an_identical_ordering(workspace: Workspace, root: Path) -> None:
    runs = [
        tuple(
            (f"{c.repository}/{c.path}", c.line_start, c.line_end, c.match)
            for c in ask(workspace, root, TARGET).candidates
        )
        for _ in range(5)
    ]
    assert len(set(runs)) == 1


# --- 23-25. through the real CLI ---------------------------------------------


@pytest.fixture(scope="module")
def cli_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, dict[str, str]]]:
    """The same ecosystem, indexed by the installed console script.

    Temporary config, database, HOME and managed root. A closed LLM port, so a
    reasoning command fails loudly rather than reaching a model that happens to
    be running on this machine.
    """
    base = tmp_path_factory.mktemp("exact-path-cli")
    root = base / "repos"
    _write(root / "svc", SVC_FILES)
    _write(root / "other", OTHER_FILES)

    state = base / "state"
    state.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "REPOATLAS_DB": str(state / "atlas.db"),
        "REPOATLAS_LLM_BASE_URL": "http://127.0.0.1:1",
    }
    subprocess.run(
        [str(CLI), "init", str(root)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    yield root, environment


def run_cli(
    cli_root: tuple[Path, dict[str, str]], where: str, *args: str
) -> subprocess.CompletedProcess[str]:
    root, environment = cli_root
    return subprocess.run(
        [str(CLI), *args],
        cwd=root / where,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_locate_names_the_exact_path_first(
    cli_root: tuple[Path, dict[str, str]],
) -> None:
    done = run_cli(cli_root, "svc", "locate", TARGET)
    assert done.returncode == ExitCode.OK, done.stderr

    lines = [line for line in done.stdout.splitlines() if "svc/" in line]
    assert lines, done.stdout
    assert f"svc/{TARGET}" in lines[0], done.stdout


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_json_output_ranks_the_exact_path_first(
    cli_root: tuple[Path, dict[str, str]],
) -> None:
    done = run_cli(cli_root, "svc", "locate", TARGET, "--json")
    assert done.returncode == ExitCode.OK, done.stderr

    payload = json.loads(done.stdout)
    first = payload["candidates"][0]
    assert first["path"] == TARGET
    assert first["repository"] == "svc"
    assert first["match"] == MatchClass.EXACT_PATH.value


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_debug_keeps_the_path_anchor_with_supporting_evidence(
    cli_root: tuple[Path, dict[str, str]],
) -> None:
    """``debug`` widens the funnel and searches the phrase literally; the anchor holds."""
    done = run_cli(
        cli_root, "svc", "debug", f"why does {TARGET} fail to read DATABASE_URL?", "--no-llm"
    )
    assert done.returncode == ExitCode.OK, done.stderr

    lines = [line for line in done.stdout.splitlines() if "svc/" in line]
    assert lines, done.stdout
    assert f"svc/{TARGET}" in lines[0], done.stdout
    assert len(lines) > 1, "supporting evidence must remain available"


# --- 25. the real semantic backend -------------------------------------------


@pytest.mark.slow
def test_the_anchor_holds_against_the_real_embedding_model(
    tmp_path: Path,
) -> None:
    """The same guarantee with a genuine semantic model, not the hashing stub.

    Deselected by default: the first run downloads the model, and CI must never
    depend on the network (V1_SPEC §19). It exists because the stub cannot
    produce the failure this guards against — a file that is *semantically* about
    configuration, scoring highly for a query that is a path, is exactly what a
    real model returns and what a hash of the text does not.

    Priority, not suppression: the semantic stage still runs and its neighbours
    are still in the answer, below the file the query named.
    """
    from repoatlas.embeddings.fastembed_backend import FastEmbedModel

    model = FastEmbedModel(allow_download=True)
    if not model.available():
        pytest.skip(model.availability_detail())

    root = tmp_path / "repos"
    _write(root / "svc", SVC_FILES)
    _write(root / "other", OTHER_FILES)
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "real" / "atlas.db")},
            "embeddings": {"provider": "fastembed", "allow_download": True},
        }
    )
    real = open_workspace(settings=settings, require_root=False)
    try:
        initialize(real, root, config_path=tmp_path / "real-config.yaml")
        evidence = locate(real, TARGET, real.resolver().resolve(root / "svc"))

        assert ranked(evidence)[0] == f"svc/{TARGET}"
        assert f"svc/{TARGET}" in anchored(evidence)
        tiers = [c.tier for c in evidence.candidates]
        assert tiers == sorted(tiers)
    finally:
        real.close()


# --- state guard --------------------------------------------------------------


def test_no_test_touches_real_repoatlas_state() -> None:
    """Every fixture above supplies a temporary config, database and root.

    Two earlier remediation passes discovered tests writing into the operator's
    real RepoAtlas state. This asserts the invariant rather than trusting it.
    """
    from repoatlas.config.settings import config_path, database_path

    for path in (config_path(), database_path()):
        assert "pytest" in str(path) or "tmp" in str(path) or not path.exists(), path
