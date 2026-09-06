"""Explicit suffix-path intent must survive fusion, reranking and budgeting (V1_SPEC §14).

Pass 9 made a complete repository-relative path a ranking boundary: a file whose
whole path the user spelled outranks anything merely named less exactly. It
anchored that one class and deliberately stopped there — the `MatchClass` ladder
below it was kept as provenance, "retained provenance rather than additional
precedence".

That left the same information loss one level down. ``locate api/config.py``
classified ``services/api/config.py`` as `EXACT_SUBPATH` — correctly, on segment
boundaries, exactly as designed — and then ranked it **tenth**, behind six chunks
of a ``README.md`` whose only relationship to the query is that it quotes the
string ``api/config.py`` two hundred times. The class was computed and then used
for nothing, so a document *about* the file outranked the file.

The fix is one more boundary in the same structure, not a bonus and not a
gradient: `EXACT_PATH` first, `EXACT_SUBPATH` second, everything else ordered by
retrieval agreement exactly as before.

What makes this suite adversarial is the second direction. A suffix is *explicit
intent without a claim of uniqueness*, so every test that a suffix wins is paired
with one that it does not overreach:

* it must not beat a complete path;
* it must not be granted to a bare basename, which says what a file is called
  rather than where it lives;
* it must not be granted on a substring — ``pi/config.py`` and ``myapi/config.py``
  name nothing;
* and when two files genuinely end with ``api/config.py``, both are anchored and
  neither is declared the answer.

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
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import (
    EvidenceCandidate,
    EvidenceSet,
    EvidenceSource,
    EvidenceTier,
    MatchClass,
)
from repoatlas.retrieval.ranking import apply_budget, enforce_precedence, fuse
from repoatlas.services.init_service import initialize
from repoatlas.services.query_service import QueryIntent, locate, retrieve_for
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial

CLI = Path(sys.executable).parent / "repoatlas"

SUFFIX = "api/config.py"

# The two files the suffix genuinely names. Deliberately *quiet*: no repeated
# `config` symbols, no mention of their own paths. Their whole claim is that
# their paths end where the user said.
SERVICES_API_CONFIG = '''"""Runtime configuration for the API service."""

import os

DATABASE_URL = os.environ["DATABASE_URL"]
REQUEST_TIMEOUT = 30


def build_client():
    return {"url": DATABASE_URL, "timeout": REQUEST_TIMEOUT}
'''

LEGACY_API_CONFIG = '''"""Configuration for the retired API."""

import os

LEGACY_DATABASE_URL = os.environ["LEGACY_DATABASE_URL"]
'''

# Symbol adversary: nothing but `config`-shaped names, and it shares the basename
# only. It should collect entity, lexical and semantic votes and still lose.
TESTS_CONFIG = (
    '"""Configuration test helpers."""\n\n'
    + "".join(f"def test_config_{i}():\n    return {i}\n\n\n" for i in range(20))
    + "".join(f"def build_config_{i}(): ...\n" for i in range(20))
    + "\n"
    + "".join(f"def load_api_config_{i}(): ...\n" for i in range(20))
)

# Content adversary: quotes the queried suffix two hundred times.
DOCS_README = "# Service documentation\n\n" + f"Configuration lives in {SUFFIX} — see it.\n" * 200

# A second explicit path for the two-anchor tests.
WORKER_CONFIG = '''"""Worker configuration."""

WORKER_TIMEOUT = 45
'''

SVC_FILES = {
    "services/api/config.py": SERVICES_API_CONFIG,
    "legacy/api/config.py": LEGACY_API_CONFIG,
    "tests/config.py": TESTS_CONFIG,
    "docs/README.md": DOCS_README,
    "worker/config.py": WORKER_CONFIG,
    "services/API/Config.py": '"""Differently cased."""\n\nCASED = True\n',
    "services/résumé/配置/設定.py": '"""Unicode path."""\n\nUNICODE_SETTING = 1\n',
    "services/my api/config-file.v2.py": '"""Spaced and punctuated."""\n\nSPACED = 1\n',
    "src/app.py": "from services.api import config\n\n\ndef main():\n    return config\n",
}

# A second repository holding the *same* suffix, for the cross-repository and
# context-bias cases.
OTHER_FILES = {
    "services/api/config.py": '"""Another repository\'s API configuration."""\n\nOTHER_URL = 1\n',
    "src/main.py": "OTHER_MAIN = 1\n",
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
    base = tmp_path_factory.mktemp("suffix-path-ranking")
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


def classes(evidence: EvidenceSet, location: str) -> set[MatchClass]:
    return {c.match for c in evidence.candidates if f"{c.repository}/{c.path}" == location}


def anchored_suffixes(evidence: EvidenceSet) -> set[str]:
    return {
        f"{c.repository}/{c.path}"
        for c in evidence.candidates
        if c.match is MatchClass.EXACT_SUBPATH
    }


# --- 1. the original reproduction ---------------------------------------------


def test_a_suffix_path_outranks_a_readme_that_quotes_it(workspace: Workspace, root: Path) -> None:
    """The defect: the file finished tenth behind six chunks of a README."""
    evidence = ask(workspace, root, SUFFIX)
    order = ranked(evidence)

    services = rank_of(evidence, "svc/services/api/config.py")
    legacy = rank_of(evidence, "svc/legacy/api/config.py")
    readme = rank_of(evidence, "svc/docs/README.md")

    assert services is not None and legacy is not None
    assert readme is None or (services < readme and legacy < readme), order


def test_the_readme_still_scores_higher_and_still_loses(workspace: Workspace, root: Path) -> None:
    """Precedence is structural, not a tuned bonus.

    The README's fused score is *larger* than the anchored files' — it wins the
    literal stage outright. It ranks below them anyway, which is only possible if
    specificity is a rank position rather than a number added to a score.
    """
    evidence = ask(workspace, root, SUFFIX)
    anchored = [c for c in evidence.candidates if c.match is MatchClass.EXACT_SUBPATH]
    readmes = [c for c in evidence.candidates if c.path == "docs/README.md"]
    if not readmes:  # budget may exclude it entirely, which is a stronger result
        return

    assert max(c.score for c in readmes) > max(c.score for c in anchored), (
        "the fixture no longer reproduces the score inversion this test exists for"
    )
    assert ranked(evidence).index("svc/docs/README.md") > min(
        i for i, n in enumerate(ranked(evidence)) if n.endswith("api/config.py")
    )


# --- 2-5. what the suffix must not overreach ----------------------------------


def test_a_complete_path_outranks_a_suffix_path(workspace: Workspace, root: Path) -> None:
    """`EXACT_PATH` remains strictly stronger. Pass 9's invariant is untouched."""
    evidence = ask(workspace, root, "services/api/config.py")

    assert MatchClass.EXACT_PATH in classes(evidence, "svc/services/api/config.py")
    exact = rank_of(evidence, "svc/services/api/config.py")
    other = rank_of(evidence, "other/services/api/config.py")
    assert exact == 1 or exact is not None
    if other is not None:
        assert exact < other, ranked(evidence)


def test_a_repository_qualified_path_is_exact_not_a_suffix(
    workspace: Workspace, root: Path
) -> None:
    """``svc/services/api/config.py`` is a complete identity, not a trailing path."""
    evidence = ask(workspace, root, "svc/services/api/config.py")

    assert MatchClass.EXACT_PATH in classes(evidence, "svc/services/api/config.py")
    assert rank_of(evidence, "svc/services/api/config.py") == 1, ranked(evidence)


def test_a_bare_basename_is_not_a_suffix_anchor(workspace: Workspace, root: Path) -> None:
    """``config.py`` says what a file is called, not where it lives (§5)."""
    evidence = ask(workspace, root, "config.py")

    assert anchored_suffixes(evidence) == set(), ranked(evidence)
    for location in ("svc/services/api/config.py", "svc/tests/config.py"):
        assert MatchClass.EXACT_SUBPATH not in classes(evidence, location)


@pytest.mark.parametrize("near", ["pi/config.py", "myapi/config.py", "onfig.py", "api/config"])
def test_a_near_miss_is_not_a_suffix_match(workspace: Workspace, root: Path, near: str) -> None:
    """Component boundaries, not `endswith`. A substring names nothing."""
    evidence = ask(workspace, root, near)
    assert anchored_suffixes(evidence) == set(), (near, ranked(evidence))


# --- 6. normalization ---------------------------------------------------------


@pytest.mark.parametrize("spelling", ["api/config.py", "./api/config.py", "/api/config.py"])
def test_supported_spellings_all_anchor(workspace: Workspace, root: Path, spelling: str) -> None:
    evidence = ask(workspace, root, spelling)
    assert "svc/services/api/config.py" in anchored_suffixes(evidence), (
        spelling,
        ranked(evidence),
    )


@pytest.mark.parametrize("unsupported", ["api/./config.py", "api//config.py"])
def test_unsupported_spellings_keep_their_existing_behaviour(
    workspace: Workspace, root: Path, unsupported: str
) -> None:
    """Normalization is not widened by this pass; these matched nothing before."""
    evidence = ask(workspace, root, unsupported)
    assert anchored_suffixes(evidence) == set(), (unsupported, ranked(evidence))


def test_a_parent_traversal_token_anchors_through_the_clean_suffix_it_leaves(
    workspace: Workspace, root: Path
) -> None:
    """``..`` is removed by query analysis, before anything classifies a path.

    ``_PATHLIKE`` starts matching at a word boundary, so ``../api/config.py``
    yields the token ``api/config.py`` — a clean component suffix by the time the
    file stage sees it. This pass does not change that; it is the same extraction
    the ``exact-path-token`` evaluation case has always relied on, where
    ``../python-service/src/app.py`` resolves to a repository-qualified exact
    path. Pinned here so the behaviour is a decision rather than an accident.
    """
    traversal = ask(workspace, root, "../api/config.py")
    clean = ask(workspace, root, SUFFIX)
    assert anchored_suffixes(traversal) == anchored_suffixes(clean)


def test_a_parent_traversal_token_still_cannot_beat_an_exact_path(
    workspace: Workspace, root: Path
) -> None:
    """Whatever ``..`` leaves behind, it is never stronger than a complete path."""
    evidence = ask(workspace, root, "../services/api/config.py")
    assert MatchClass.EXACT_PATH in classes(evidence, "svc/services/api/config.py")
    assert rank_of(evidence, "svc/services/api/config.py") == 1, ranked(evidence)


# --- 8. ambiguity -------------------------------------------------------------


def test_both_genuine_suffix_matches_are_anchored(workspace: Workspace, root: Path) -> None:
    """A suffix names a shape, not a file. Neither is declared the answer."""
    evidence = ask(workspace, root, SUFFIX)
    assert anchored_suffixes(evidence) >= {
        "svc/services/api/config.py",
        "svc/legacy/api/config.py",
    }, ranked(evidence)


def test_ambiguity_is_resolved_within_the_class_not_by_erasing_it(
    workspace: Workspace, root: Path
) -> None:
    """Both suffix files precede every weaker candidate; their order is fusion's."""
    evidence = ask(workspace, root, SUFFIX)
    order = ranked(evidence)
    anchored = {"svc/services/api/config.py", "svc/legacy/api/config.py"}
    positions = [i for i, name in enumerate(order) if name in anchored]
    weaker = [
        i for i, name in enumerate(order) if name in {"svc/docs/README.md", "svc/tests/config.py"}
    ]
    assert positions, order
    if weaker:
        assert max(positions) < min(weaker), order


# --- 9. context -----------------------------------------------------------------


def test_context_orders_suffix_matches_across_repositories(
    workspace: Workspace, root: Path
) -> None:
    """Primary-repository bias sorts *within* the class; it does not create one."""
    from_svc = ask(workspace, root, SUFFIX, where="svc")
    from_other = ask(workspace, root, SUFFIX, where="other")

    # Standing in a repository puts its own match first, and standing elsewhere
    # puts the other one first. The boost is added to the score, so it competes
    # inside the anchored class and never creates or crosses one.
    def before(evidence: EvidenceSet, first: str, second: str) -> bool:
        left, right = rank_of(evidence, first), rank_of(evidence, second)
        assert left is not None and right is not None, (first, second, ranked(evidence))
        return left < right

    assert before(from_svc, "svc/services/api/config.py", "other/services/api/config.py"), ranked(
        from_svc
    )
    assert before(from_other, "other/services/api/config.py", "svc/services/api/config.py"), ranked(
        from_other
    )

    # And every genuine match stays anchored from either side: context orders the
    # ambiguity, it does not resolve it into a single answer.
    for view in (from_svc, from_other):
        assert anchored_suffixes(view) >= {
            "svc/services/api/config.py",
            "svc/legacy/api/config.py",
            "other/services/api/config.py",
        }, ranked(view)


def test_an_explicit_path_beats_the_inferred_repository(workspace: Workspace, root: Path) -> None:
    """Pass 9: explicit identity outranks contextual preference. Still true."""
    evidence = ask(workspace, root, "other/services/api/config.py", where="svc")
    assert rank_of(evidence, "other/services/api/config.py") == 1, ranked(evidence)


# --- 11-13. adversaries -------------------------------------------------------


def test_an_entity_name_flood_does_not_outrank_a_suffix(workspace: Workspace, root: Path) -> None:
    """``tests/config.py`` is nothing but ``config``-shaped symbols. It still loses."""
    evidence = ask(workspace, root, SUFFIX)
    tests = rank_of(evidence, "svc/tests/config.py")
    anchored = [rank_of(evidence, n) for n in anchored_suffixes(evidence)]
    assert anchored, ranked(evidence)
    if tests is not None:
        assert max(r for r in anchored if r is not None) < tests, ranked(evidence)


def test_an_entity_named_like_the_path_does_not_outrank_the_file(
    workspace: Workspace, root: Path
) -> None:
    """Path identity beats entity-name coincidence for path-shaped input (§22)."""
    evidence = ask(workspace, root, SUFFIX)
    anchored_ranks = [
        rank for name in anchored_suffixes(evidence) if (rank := rank_of(evidence, name))
    ]
    assert anchored_ranks, ranked(evidence)
    best_anchor = min(anchored_ranks)

    for candidate in evidence.candidates:
        if candidate.match is not MatchClass.EXACT_NAME:
            continue
        named = f"{candidate.repository}/{candidate.path}"
        if named in anchored_suffixes(evidence):
            continue  # the anchored file's own symbols rise with it, by design
        rank = rank_of(evidence, named)
        assert rank is not None and rank > best_anchor, (named, ranked(evidence))


def test_associative_evidence_never_crosses_the_tier(workspace: Workspace, root: Path) -> None:
    """Suffix precedence is applied inside a tier, never across one."""
    evidence = ask(workspace, root, SUFFIX)
    tiers = [c.tier for c in evidence.candidates]
    assert tiers == sorted(tiers), "a tier boundary was crossed"


# --- 14-15. natural-language anchoring ----------------------------------------


def test_a_question_about_a_suffix_path_surfaces_that_file_s_own_lines(
    workspace: Workspace, root: Path
) -> None:
    """The anchor is the *file*, so its spans rise with it — not just its name."""
    evidence = ask(workspace, root, "why does api/config.py read DATABASE_URL?")
    order = ranked(evidence)
    assert "svc/services/api/config.py" in order, order
    readme = rank_of(evidence, "svc/docs/README.md")
    target = rank_of(evidence, "svc/services/api/config.py")
    assert target is not None
    if readme is not None:
        assert target < readme, order


def test_supporting_evidence_is_not_filtered_away(workspace: Workspace, root: Path) -> None:
    """Ranking, not filtering: other files may still answer the question (§19)."""
    evidence = ask(workspace, root, "why does api/config.py call build_client?")
    order = ranked(evidence)
    assert any(not name.endswith("api/config.py") for name in order), order


def test_two_suffix_matches_do_not_fabricate_a_unique_intent(
    workspace: Workspace, root: Path
) -> None:
    """Both plausible files may appear; neither is stamped as the proven one."""
    evidence = ask(workspace, root, "why does api/config.py read DATABASE_URL?")
    assert len(anchored_suffixes(evidence)) >= 1
    for candidate in evidence.candidates:
        assert candidate.match is not MatchClass.EXACT_PATH, (
            "a suffix query must not be reported as a complete-path identification"
        )


# --- 27-28. two anchors -------------------------------------------------------


def test_two_different_suffix_tokens_both_survive(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "compare api/config.py with worker/config.py")
    order = ranked(evidence)
    assert any(n.endswith("api/config.py") for n in order), order
    assert "svc/worker/config.py" in order, order


def test_an_exact_path_does_not_evict_a_second_named_file(workspace: Workspace, root: Path) -> None:
    """A stronger anchor for one file must not push another named file out (§28)."""
    evidence = ask(workspace, root, "compare services/api/config.py with worker/config.py")
    order = ranked(evidence)
    assert "svc/services/api/config.py" in order, order
    assert "svc/worker/config.py" in order, order


# --- 16-18. the ranking functions themselves ----------------------------------


def candidate(
    path: str,
    match: MatchClass,
    *,
    score: float = 0.0,
    repository: str = "svc",
    line: int | None = None,
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository=repository,
        repository_slug=repository,
        path=path,
        line_start=line,
        line_end=line,
        text="x" * 8,
        score=score,
        source=EvidenceSource.FILE,
        tier=tier,
        match=match,
    )


def test_fusion_places_suffix_above_content_however_it_scores() -> None:
    """A hundred lexical votes for the README against one for the file."""
    readme = [candidate("docs/README.md", MatchClass.EXACT_CONTENT, line=i) for i in range(1, 21)]
    target = candidate("services/api/config.py", MatchClass.EXACT_SUBPATH)
    fused = fuse([[*readme], [*readme], [*readme], [target]])
    assert fused[0].path == "services/api/config.py", [c.path for c in fused[:3]]


def test_fusion_places_exact_above_suffix() -> None:
    suffix = candidate("legacy/api/config.py", MatchClass.EXACT_SUBPATH)
    exact = candidate("services/api/config.py", MatchClass.EXACT_PATH)
    fused = fuse([[suffix, suffix], [exact]])
    assert fused[0].path == "services/api/config.py"


def test_fusion_places_suffix_above_basename() -> None:
    basename = candidate("tests/config.py", MatchClass.EXACT_BASENAME)
    suffix = candidate("services/api/config.py", MatchClass.EXACT_SUBPATH)
    fused = fuse([[basename, basename], [basename], [suffix]])
    assert fused[0].path == "services/api/config.py"


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_merged_candidates_keep_the_strongest_class_whatever_the_stage_order(
    order: tuple[int, int],
) -> None:
    """``min(MatchClass)`` is commutative; Pass 9's rule extended, not replaced."""
    strong = candidate("services/api/config.py", MatchClass.EXACT_SUBPATH, line=3)
    weak = candidate("services/api/config.py", MatchClass.LEXICAL, line=3)
    lists = [[strong], [weak]]
    fused = fuse([lists[order[0]], lists[order[1]]])
    assert fused[0].match is MatchClass.EXACT_SUBPATH


def test_a_file_reached_as_both_exact_and_suffix_takes_the_stronger_anchor() -> None:
    exact = candidate("services/api/config.py", MatchClass.EXACT_PATH)
    suffix = candidate("services/api/config.py", MatchClass.EXACT_SUBPATH, line=2)
    other = candidate("legacy/api/config.py", MatchClass.EXACT_SUBPATH)
    fused = fuse([[suffix, other], [exact]])
    assert fused[0].identity == ("svc", "services/api/config.py")


def test_a_reranker_cannot_erase_suffix_specificity() -> None:
    """The seam: a cross-encoder that adores the README must not undo the class."""
    readme = candidate("docs/README.md", MatchClass.EXACT_CONTENT, line=1, score=99.0)
    suffix = candidate("services/api/config.py", MatchClass.EXACT_SUBPATH)
    reranked = [readme, suffix]  # what an opaque V2 reranker might return
    restored = enforce_precedence(reranked)
    assert restored[0].path == "services/api/config.py"


def test_enforce_precedence_keeps_exact_above_suffix_above_the_rest() -> None:
    """Two anchored positions, then everything else in the order it arrived.

    Only the anchored classes are positions; `EXACT_BASENAME` and `EXACT_CONTENT`
    share the unanchored one, and the sort is stable, so their incoming order is
    preserved deliberately — that is what lets a reranker order *within* a class
    while being unable to erase the class.
    """
    exact = candidate("services/api/config.py", MatchClass.EXACT_PATH)
    suffix = candidate("legacy/api/config.py", MatchClass.EXACT_SUBPATH)
    basename = candidate("tests/config.py", MatchClass.EXACT_BASENAME)
    content = candidate("docs/README.md", MatchClass.EXACT_CONTENT, line=1)

    restored = enforce_precedence([content, basename, suffix, exact])
    assert [c.path for c in restored] == [
        "services/api/config.py",
        "legacy/api/config.py",
        "docs/README.md",
        "tests/config.py",
    ]

    # Reversing the two unanchored inputs reverses only them: the anchors hold.
    swapped = enforce_precedence([basename, content, suffix, exact])
    assert [c.path for c in swapped] == [
        "services/api/config.py",
        "legacy/api/config.py",
        "tests/config.py",
        "docs/README.md",
    ]


def test_suffix_candidates_survive_a_flooded_budget() -> None:
    """Budgeting is applied after precedence, so the anchor is never starved out."""
    flood = [candidate("docs/README.md", MatchClass.EXACT_CONTENT, line=i) for i in range(1, 61)]
    suffix = candidate("services/api/config.py", MatchClass.EXACT_SUBPATH)
    ordered = enforce_precedence(fuse([flood, flood, [suffix]]))
    kept, truncated, _ = apply_budget(ordered, max_items=12, token_budget=10_000)
    assert any(c.match is MatchClass.EXACT_SUBPATH for c in kept), "the anchor was evicted"
    assert truncated


def test_precedence_never_lifts_an_associative_candidate() -> None:
    """A suffix-classed ASSOCIATIVE candidate still sits below every fact."""
    fact = candidate("docs/README.md", MatchClass.EXACT_CONTENT, line=1)
    guess = candidate(
        "services/api/config.py", MatchClass.EXACT_SUBPATH, tier=EvidenceTier.ASSOCIATIVE
    )
    restored = enforce_precedence([guess, fact])
    assert restored[0].tier is EvidenceTier.DETERMINISTIC


# --- 23-25. identity text -----------------------------------------------------


def test_case_is_part_of_suffix_identity(workspace: Workspace, root: Path) -> None:
    """``API/Config.py`` and ``api/config.py`` are different directories."""
    upper = ask(workspace, root, "API/Config.py")
    assert "svc/services/API/Config.py" in anchored_suffixes(upper), ranked(upper)
    assert "svc/services/api/config.py" not in anchored_suffixes(upper), ranked(upper)

    lower = ask(workspace, root, SUFFIX)
    assert "svc/services/API/Config.py" not in anchored_suffixes(lower), ranked(lower)


def test_a_unicode_suffix_path_anchors_and_keeps_its_text(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, "配置/設定.py")
    anchored = anchored_suffixes(evidence)
    assert "svc/services/résumé/配置/設定.py" in anchored, ranked(evidence)


def test_a_quoted_path_with_spaces_anchors(workspace: Workspace, root: Path) -> None:
    """Quoted extraction is the supported spelling for a path containing a space."""
    evidence = ask(workspace, root, '"my api/config-file.v2.py"')
    assert "svc/services/my api/config-file.v2.py" in anchored_suffixes(evidence), ranked(evidence)


# --- 29-33. product surface ---------------------------------------------------


def test_locate_leads_with_the_suffix_matches(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, SUFFIX)
    leaders = ranked(evidence)[:4]
    assert all(name.endswith("api/config.py") for name in leaders), leaders


def test_debug_keeps_the_anchor_through_a_widened_funnel(workspace: Workspace, root: Path) -> None:
    evidence = retrieve_for(
        workspace, SUFFIX, context_at(workspace, root, "svc"), intent=QueryIntent.DEBUG
    )
    anchored = {
        f"{c.repository}/{c.path}"
        for c in evidence.candidates
        if c.match is MatchClass.EXACT_SUBPATH
    }
    assert anchored, ranked(evidence)
    first_anchor = min(
        i for i, c in enumerate(evidence.candidates) if c.match is MatchClass.EXACT_SUBPATH
    )
    readmes = [i for i, c in enumerate(evidence.candidates) if c.path == "docs/README.md"]
    if readmes:
        assert first_anchor < min(readmes), ranked(evidence)


def test_the_trace_distinguishes_suffix_anchors_from_exact_ones(
    workspace: Workspace, root: Path
) -> None:
    """ "Why is this first?" must be answerable from the outside (§33)."""
    suffix_query = ask(workspace, root, SUFFIX)
    assert suffix_query.trace is not None
    assert suffix_query.trace.suffix_paths, "no suffix path reported in the trace"
    assert not suffix_query.trace.named_paths, "a suffix was reported as a complete path"

    exact_query = ask(workspace, root, "services/api/config.py")
    assert exact_query.trace is not None
    assert "svc/services/api/config.py" in exact_query.trace.named_paths


def test_json_output_exposes_the_match_class(workspace: Workspace, root: Path) -> None:
    evidence = ask(workspace, root, SUFFIX)
    payload = json.loads(evidence.model_dump_json())
    by_path: dict[str, set[int]] = {}
    for item in payload["candidates"]:
        by_path.setdefault(item["path"], set()).add(item["match"])
    assert MatchClass.EXACT_SUBPATH.value in by_path["services/api/config.py"]
    assert MatchClass.EXACT_PATH.value not in by_path["services/api/config.py"]


def test_the_cli_reports_suffix_paths_under_verbose(root: Path, tmp_path: Path) -> None:
    """Through the installed console script, on a throwaway config and index."""
    config = tmp_path / "cli-config.yaml"
    config.write_text(
        f"repos:\n  root: {root}\nembeddings:\n  provider: hashing\n  dim: 128\n",
        encoding="utf-8",
    )
    database = tmp_path / "cli-state" / "atlas.db"
    environment = {
        **os.environ,
        "REPOATLAS_CONFIG": str(config),
        "REPOATLAS_DB": str(database),
    }
    subprocess.run(
        [str(CLI), "init", str(root)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(root),
        timeout=600,
    )
    result = subprocess.run(
        [str(CLI), "locate", SUFFIX, "--verbose"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(root / "svc"),
        timeout=600,
    )
    assert "suffix path(s):" in result.stderr, result.stderr
    assert "api/config.py" in result.stderr
    first_line = next(
        (line for line in result.stdout.splitlines() if "config.py" in line or "README" in line),
        "",
    )
    assert "README" not in first_line, result.stdout
