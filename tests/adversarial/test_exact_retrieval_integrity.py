"""Exact retrieval integrity (V1_SPEC §14, ARCHITECTURE.md §10).

The invariant under test, in one sentence:

    If a literal, filename, path, identifier or exact error string exists in
    indexed repository content or in indexed file identity, RepoAtlas must not
    confidently report that it does not exist.

Two mechanisms used to break it, and they broke it in the same visible way — a
confident "No indexed evidence matches this question" about something the index
was holding at the time:

1. **File identity was not searchable.** `src/config.py` was indexed as a file
   entity, found by exact-name lookup, and then discarded because a file has no
   line range. Suppression then concluded the named term did not exist.
2. **Chunk boundaries decided discoverability.** A span with no line structure is
   split at a fixed character count, and a literal straddling one of those splits
   existed in no single chunk, so exact search could not see it.

Both directions matter. The tests below assert that real things are found *and*
that absent things are still refused: a false negative must not be traded for a
loose associative guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import IndexingSettings, Settings
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.evidence import EvidenceSet, EvidenceSource, EvidenceTier
from repoatlas.services.init_service import initialize
from repoatlas.services.query_service import locate
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial


# --- fixtures -----------------------------------------------------------------


TREE: dict[str, str] = {
    "src/config.py": 'import os\nSERVICE_HOST = os.environ["SERVICE_HOST"]\n',
    "deployment/service/values.yaml": "host: backend.internal\n",
    "deployment/service/init.sls": "service:\n  file.managed:\n    - name: /etc/svc\n",
    "deployment/service/map.jinja": "{% set host = 'x' %}\n",
    "app.py": "def main():\n    pass\n",
    "Dockerfile": "FROM python:3.13-slim\n",
    "package.json": '{"name": "svc"}\n',
    "pyproject.toml": '[project]\nname = "svc"\n',
    "docs/release notes.md": "# Notes\n",
    "src/module.test.py": "assert True\n",
    "service-a/config.py": "A = 1\n",
    "service-b/config.py": "B = 2\n",
}


def build_tree(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "root"
    build_tree(root, "svc", TREE)
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    return space


def ask(space: Workspace, question: str, cwd: Path | None = None) -> EvidenceSet:
    root = space.settings.root
    assert root is not None
    context = space.resolver().resolve(cwd or root / "svc")
    return locate(space, question, context)


def located(evidence: EvidenceSet) -> set[str]:
    return {f"{c.repository}/{c.path}" for c in evidence.candidates}


# --- 1. file and path identity is searchable ----------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("config.py", {"svc/src/config.py", "svc/service-a/config.py", "svc/service-b/config.py"}),
        ("src/config.py", {"svc/src/config.py"}),
        ("values.yaml", {"svc/deployment/service/values.yaml"}),
        ("deployment/service/values.yaml", {"svc/deployment/service/values.yaml"}),
        ("service/values.yaml", {"svc/deployment/service/values.yaml"}),
        ("app.py", {"svc/app.py"}),
        ("Dockerfile", {"svc/Dockerfile"}),
        ("package.json", {"svc/package.json"}),
        ("pyproject.toml", {"svc/pyproject.toml"}),
        ("init.sls", {"svc/deployment/service/init.sls"}),
        ("map.jinja", {"svc/deployment/service/map.jinja"}),
        ("module.test.py", {"svc/src/module.test.py"}),
    ],
)
def test_a_filename_or_path_finds_the_file(
    workspace: Workspace, question: str, expected: set[str]
) -> None:
    """An indexed file must be findable by the name it has on disk."""
    evidence = ask(workspace, question)

    assert evidence.candidates, f"{question!r} returned nothing"
    assert expected <= located(evidence), f"{question!r} missed {expected - located(evidence)}"


def test_a_path_inside_a_prose_question_still_finds_the_file(workspace: Workspace) -> None:
    assert "svc/src/config.py" in located(ask(workspace, "where is src/config.py?"))


def test_a_filename_with_spaces_is_found(workspace: Workspace) -> None:
    """Quoting is how a user names a path containing a space (V1_SPEC §14)."""
    assert "svc/docs/release notes.md" in located(ask(workspace, '"docs/release notes.md"'))


def test_file_evidence_is_deterministic(workspace: Workspace) -> None:
    """File identity is a fact about the repository, not a guess about the query."""
    evidence = ask(workspace, "src/config.py")
    match = next(c for c in evidence.candidates if c.path == "src/config.py")

    assert match.tier is EvidenceTier.DETERMINISTIC
    assert match.source is EvidenceSource.FILE


def test_file_evidence_carries_no_invented_line(workspace: Workspace) -> None:
    """A filename match is about the file, so it must not claim to be about line 1.

    A module symbol that happens to span the same file is a different candidate
    with a different provenance, and it is allowed to carry the lines it really
    covers. What must not exist is a *filename* match wearing a line number.
    """
    evidence = ask(workspace, "app.py")
    match = next(
        c for c in evidence.candidates if c.path == "app.py" and c.source is EvidenceSource.FILE
    )

    assert match.line_start is None
    assert match.line_end is None
    assert match.location == "svc/app.py"
    assert all(c.is_file_level for c in evidence.candidates if c.source is EvidenceSource.FILE)


def test_an_exact_path_outranks_a_basename_collision(workspace: Workspace) -> None:
    """`service-a/config.py` is more specific than `config.py`, and ranks so."""
    evidence = ask(workspace, "service-a/config.py")

    assert evidence.candidates[0].path == "service-a/config.py"


# --- 2. ambiguity is reported, never silently resolved -------------------------


def test_a_duplicate_basename_returns_every_match(workspace: Workspace) -> None:
    """Three files are named config.py. Picking one would be a guess."""
    found = located(ask(workspace, "config.py"))

    assert {
        "svc/src/config.py",
        "svc/service-a/config.py",
        "svc/service-b/config.py",
    } <= found


def test_a_duplicate_basename_across_repositories_returns_both(tmp_path: Path) -> None:
    root = tmp_path / "root"
    build_tree(root, "alpha", {"src/config.py": "A = 1\n"})
    build_tree(root, "beta", {"src/config.py": "B = 2\n"})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    found = located(ask(space, "config.py", cwd=root))

    assert {"alpha/src/config.py", "beta/src/config.py"} <= found


# --- 3. absent names are still refused ----------------------------------------


@pytest.mark.parametrize(
    "absent",
    [
        "definitely-does-not-exist-918273.py",
        "src/definitely-does-not-exist-918273.py",
        "ZZZ_NO_SUCH_FILE_918273.py",
        "ZZZ_NO_SUCH_LITERAL_918273",
    ],
)
def test_an_absent_name_is_still_refused(workspace: Workspace, absent: str) -> None:
    """A false negative must not be traded for a loose associative guess."""
    evidence = ask(workspace, absent)

    assert evidence.candidates == ()
    assert evidence.trace is not None
    assert any("suppressed" in stage.detail for stage in evidence.trace.stages)


def test_a_partial_filename_is_not_a_match(workspace: Workspace) -> None:
    """Basename matching is exact, not a substring search."""
    assert located(ask(workspace, "onfig.py")) == set()


# --- 4. suppression consults file identity ------------------------------------


def test_suppression_does_not_fire_when_a_file_proves_the_term_exists(
    workspace: Workspace,
) -> None:
    """The exact-file index proving existence must silence the suppression."""
    evidence = ask(workspace, "where is config.py used")

    assert evidence.trace is not None
    assert not any("suppressed" in stage.detail for stage in evidence.trace.stages)


def test_a_path_and_an_identifier_together_keep_the_in_file_match_first(
    workspace: Workspace,
) -> None:
    """Content evidence is more specific than file identity (§13)."""
    evidence = ask(workspace, "where is SERVICE_HOST configured in src/config.py?")
    top = evidence.candidates[0]

    assert top.path == "src/config.py"
    assert top.line_start is not None, "the in-file match must outrank the filename match"


# --- 5. chunk boundaries do not decide discoverability ------------------------


BOUNDARY = IndexingSettings().max_chunk_chars


def one_line_file(marker: str, offset: int, tail: int = 4000) -> str:
    """A file with no line structure, with ``marker`` starting at ``offset``."""
    return ("x" * offset) + marker + ("y" * tail)


@pytest.mark.parametrize(
    "marker",
    [
        "ConnectionResetByPeerWhileReadingUpstream",
        "Traceback (most recent call last): KeyError: 'SERVICE_HOST'",
        "Rendering SLS 'base:service.init' failed: Jinja variable 'host' is undefined",
        '{"error":{"code":500,"message":"upstream connect error or disconnect"}}',
        "HTTP/1.1 502 Bad Gateway upstream_reset_before_response_started",
        "handle_upstream_reset_error",
        "@company/common-runtime",
        "Grüße_从这里开始_διαμόρφωση",
    ],
)
@pytest.mark.parametrize("straddle", [0, 1, 2])
def test_a_literal_crossing_a_chunk_boundary_is_still_indexed(marker: str, straddle: int) -> None:
    """`straddle` 0 = ends at the split, 1 = crosses by one, 2 = crosses in half."""
    if straddle == 0:
        offset = BOUNDARY - len(marker)
    elif straddle == 1:
        offset = BOUNDARY - len(marker) + 1
    else:
        offset = BOUNDARY - len(marker) // 2

    content = one_line_file(marker, offset)
    chunks = chunk_content(content, (), IndexingSettings())

    assert marker in content
    assert "".join(c.text for c in chunks) == content, "chunking must lose no character"


@pytest.mark.parametrize("straddle", [0, 1, 20])
def test_a_literal_crossing_a_boundary_is_found_by_exact_search(
    tmp_path: Path, straddle: int
) -> None:
    """The end-to-end guarantee: indexed text is discoverable by exact search."""
    marker = "ConnectionResetByPeerWhileReadingUpstream"
    offset = BOUNDARY - len(marker) + straddle
    root = tmp_path / "root"
    build_tree(root, "svc", {"bundle.min.js": one_line_file(marker, offset)})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    hits = space.search.search_literal(marker)

    assert hits, f"a literal straddling offset {offset} was lost"
    assert located(ask(space, f'"{marker}"')) == {"svc/bundle.min.js"}


def test_a_literal_crossing_the_second_boundary_is_found(tmp_path: Path) -> None:
    """Every boundary, not only the first."""
    marker = "SecondBoundaryMarker_918273"
    content = one_line_file(marker, (BOUNDARY * 2) - len(marker) // 2, tail=2000)
    root = tmp_path / "root"
    build_tree(root, "svc", {"bundle.min.js": content})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    assert space.search.search_literal(marker)


def test_a_boundary_match_cites_a_real_line_range(tmp_path: Path) -> None:
    """The recovered match must not invent provenance."""
    marker = "BoundaryProvenanceMarker_918273"
    content = one_line_file(marker, BOUNDARY - len(marker) // 2)
    root = tmp_path / "root"
    build_tree(root, "svc", {"bundle.min.js": content})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    hit = space.search.search_literal(marker)[0]

    assert hit.line_start == 1
    assert hit.line_end == 1


def test_a_literal_absent_from_a_split_file_is_still_absent(tmp_path: Path) -> None:
    """Boundary recovery must not invent matches."""
    root = tmp_path / "root"
    build_tree(root, "svc", {"bundle.min.js": one_line_file("PresentMarker_1", 100)})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    assert space.search.search_literal("ZZZ_ABSENT_MARKER_918273") == []


def test_one_boundary_match_is_one_candidate(tmp_path: Path) -> None:
    """A single logical occurrence must not fan out into accumulating candidates."""
    marker = "SingleOccurrenceMarker_918273"
    content = one_line_file(marker, BOUNDARY - len(marker) // 2)
    root = tmp_path / "root"
    build_tree(root, "svc", {"bundle.min.js": content})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    assert len(space.search.search_literal(marker)) == 1


# --- 6. the file index tracks the working tree --------------------------------


def _sync(space: Workspace) -> None:
    root = space.settings.root
    assert root is not None
    sync(space, space.resolver().resolve(root))


def test_a_renamed_file_leaves_no_stale_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    repo = build_tree(root, "svc", {"src/config.py": "A = 1\n"})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    assert located(ask(space, "src/config.py")) == {"svc/src/config.py"}

    (repo / "src" / "config.py").rename(repo / "src" / "settings.py")
    _sync(space)

    assert located(ask(space, "config.py")) == set()
    assert located(ask(space, "src/config.py")) == set()
    assert located(ask(space, "settings.py")) == {"svc/src/settings.py"}
    assert located(ask(space, "src/settings.py")) == {"svc/src/settings.py"}


def test_a_deleted_file_leaves_no_stale_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    repo = build_tree(root, "svc", {"src/config.py": "A = 1\n", "app.py": "B = 2\n"})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    (repo / "src" / "config.py").unlink()
    _sync(space)

    assert located(ask(space, "config.py")) == set()
    assert located(ask(space, "app.py")) == {"svc/app.py"}


def test_an_ignored_file_disappears_and_returns(tmp_path: Path) -> None:
    root = tmp_path / "root"
    repo = build_tree(root, "svc", {"src/config.py": "A = 1\n", "app.py": "B = 2\n"})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    assert located(ask(space, "config.py")) == {"svc/src/config.py"}

    (repo / ".repoatlasignore").write_text("src/\n")
    _sync(space)
    assert located(ask(space, "config.py")) == set()

    (repo / ".repoatlasignore").unlink()
    _sync(space)
    assert located(ask(space, "config.py")) == {"svc/src/config.py"}


def test_a_boundary_literal_survives_an_incremental_edit(tmp_path: Path) -> None:
    """The seam metadata must be rebuilt by an ordinary sync, not only by a reindex.

    Editing the file shifts every offset, so the marker lands across a *different*
    boundary. If an incremental sync wrote chunk text without the split columns,
    the literal would silently stop being findable while the file still held it.
    """
    marker = "IncrementalBoundaryMarker_918273"
    root = tmp_path / "root"
    repo = build_tree(root, "svc", {"bundle.min.js": one_line_file(marker, BOUNDARY - 5)})
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    assert space.search.search_literal(marker)

    (repo / "bundle.min.js").write_text(one_line_file(marker, (BOUNDARY * 2) - 7), encoding="utf-8")
    _sync(space)

    assert space.search.search_literal(marker), "the marker was lost by an incremental sync"


def test_a_full_reindex_finds_exactly_what_the_incremental_sync_found(tmp_path: Path) -> None:
    """`--full` must not be the only way to reach a correct exact index."""
    marker = "FullReindexBoundaryMarker_918273"
    root = tmp_path / "root"
    build_tree(
        root,
        "svc",
        {"bundle.min.js": one_line_file(marker, BOUNDARY - 9), "src/config.py": "A = 1\n"},
    )
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    incremental = (
        located(ask(space, f'"{marker}"')),
        located(ask(space, "src/config.py")),
        len(space.search.search_literal(marker)),
    )

    space_root = space.settings.root
    assert space_root is not None
    sync(space, space.resolver().resolve(space_root), full=True)

    assert (
        located(ask(space, f'"{marker}"')),
        located(ask(space, "src/config.py")),
        len(space.search.search_literal(marker)),
    ) == incremental


# --- 7. tier precedence still holds -------------------------------------------


def test_file_evidence_outranks_associative_evidence(workspace: Workspace) -> None:
    """A file match is deterministic, so nothing associative may precede it."""
    evidence = ask(workspace, "config.py")
    tiers = [c.tier for c in evidence.candidates]

    assert tiers == sorted(tiers)
    assert evidence.candidates[0].tier is EvidenceTier.DETERMINISTIC
