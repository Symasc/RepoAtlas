"""A limited lookup must not be decided by insertion order (ARCHITECTURE.md §3).

Several lookups applied ``LIMIT ?`` with no ``ORDER BY``. SQLite then returns
whatever it reaches first, which is the order rows were written — so two indexes
built from the *same* repositories in a different file order kept different
candidates, and neither was wrong. A reindex from scratch could quietly change
which evidence an answer rested on.

The fix states the order in facts about the repositories: ``repositories.slug``
and ``files.rel_path``, then position within the file, then the entity's natural
key. Ordering by ``files.id`` or ``entities.id`` would have looked like a fix and
been none — those are allocation order, which is the very thing being removed.

These tests write the same logical index twice, **in opposite orders**, through the
store API rather than through the indexer. That is deliberate: going through the
indexer would test whichever order the directory walk happens to produce, and the
property under test is precisely that the answer does not depend on it.

Ordering is not ranking, and nothing here claims otherwise. An alphabetical list of
irrelevant candidates is still irrelevant; which candidates are eligible and how
they are prioritised is decided by the funnel's tiering and by ``fuse``. What this
file asserts is only that the *cut* falls in the same place twice.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EntityType
from repoatlas.models.repository import IndexedFile, Repository
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import ChunkRecord, EntityRecord, FileFacts

#: Present in every seeded file, so the literal lookup has more matches than its
#: limit and the cut is real rather than theoretical.
LITERAL = "SHARED_LITERAL_MARKER_42"

#: One name, declared in every seeded file. Ambiguity the repository genuinely
#: contains — which is exactly when a limit has to cut somewhere stated.
SYMBOL = "shared_symbol"

#: Two repositories, so ordering is exercised across the slug boundary too.
REPOSITORIES = ("alpha", "beta")

#: Paths chosen so that alphabetical order is **not** the order they are written
#: in either direction: a test that seeds them already sorted proves nothing.
#:
#: Three of them share the basename ``common.py``, six across the two
#: repositories, so a basename lookup has more matches than :data:`LIMIT` and the
#: SQL cut is exercised rather than merely present.
PATHS = (
    "src/zeta.py",
    "src/alpha.py",
    "tools/common.py",
    "lib/middle.py",
    "src/beta.py",
    "vendor/common.py",
    "docs/notes.py",
    "lib/common.py",
    "src/gamma.py",
)

#: A basename several files share. Ambiguity is returned rather than resolved, so
#: a limit below the number of matches has to choose — and must choose the same
#: way every time.
SHARED_BASENAME = "common.py"

#: Small enough to cut, larger than one, and the same for both spellings.
LIMIT = 4


def _seed(workspace: Workspace, base: Path, reverse: bool) -> None:
    """Write the same logical index, in one order or its exact opposite."""
    plan = [(repo, path) for repo in REPOSITORIES for path in PATHS]
    if reverse:
        plan.reverse()

    repo_ids: dict[str, int] = {}
    for slug, rel_path in plan:
        repo_id = repo_ids.get(slug)
        if repo_id is None:
            repo_id = workspace.repositories.upsert(
                Repository(name=slug, slug=slug, path=base / slug)
            )
            repo_ids[slug] = repo_id
        file_id = workspace.files.upsert(
            repo_id,
            IndexedFile(
                rel_path=rel_path,
                size=len(LITERAL),
                mtime_ns=1,
                content_hash=f"{slug}:{rel_path}",
                language="Python",
            ),
        )
        workspace.knowledge.replace_file_facts(
            file_id,
            FileFacts(
                entities=(
                    EntityRecord(
                        type=EntityType.FUNCTION,
                        key=f"sym:{slug}:{rel_path}:{SYMBOL}:1",
                        name=SYMBOL,
                        repo_id=repo_id,
                        file_id=file_id,
                        line_start=1,
                        line_end=1,
                    ),
                ),
                chunks=(
                    ChunkRecord(
                        file_id=file_id,
                        repo_id=repo_id,
                        line_start=1,
                        line_end=1,
                        text=f"{LITERAL} in {rel_path}",
                    ),
                ),
            ),
        )


def _workspace(tmp_path: Path, name: str, *, reverse: bool) -> Iterator[Workspace]:
    base = tmp_path / name
    settings = Settings.model_validate(
        {
            "storage": {"database": str(base / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    _seed(workspace, base, reverse)
    yield workspace
    workspace.close()


@pytest.fixture
def forward(tmp_path: Path) -> Iterator[Workspace]:
    yield from _workspace(tmp_path, "forward", reverse=False)


@pytest.fixture
def backward(tmp_path: Path) -> Iterator[Workspace]:
    yield from _workspace(tmp_path, "backward", reverse=True)


def _chunk_identities(workspace: Workspace) -> list[tuple[str, str, int]]:
    return [
        (hit.repository_slug, hit.path, hit.line_start)
        for hit in workspace.search.find_literal(LITERAL, None, LIMIT).hits
    ]


def _file_identities(workspace: Workspace, terms: Sequence[str]) -> list[tuple[str, str]]:
    return [
        (hit.repository_slug, hit.path)
        for hit in workspace.search.find_files_by_path(terms, None, LIMIT)
    ]


def _entity_identities(workspace: Workspace) -> list[tuple[str, str, str]]:
    return [
        (hit.repository_slug, hit.path or "", hit.name)
        for hit in workspace.search.find_entities_by_name([SYMBOL], None, LIMIT)
    ]


# -- the two indexes really are written differently ---------------------------


def test_the_two_indexes_are_built_in_opposite_orders(
    forward: Workspace, backward: Workspace
) -> None:
    """Otherwise every assertion below would hold for the wrong reason."""

    def by_row_id(workspace: Workspace) -> list[str]:
        rows = workspace.database.execute(
            "SELECT r.slug AS slug, f.rel_path AS path FROM files f "
            "JOIN repositories r ON r.id = f.repo_id ORDER BY f.id"
        ).fetchall()
        return [f"{row['slug']}/{row['path']}" for row in rows]

    ahead = by_row_id(forward)
    behind = by_row_id(backward)
    assert ahead == list(reversed(behind))
    assert ahead != behind


def test_both_indexes_hold_the_same_facts(forward: Workspace, backward: Workspace) -> None:
    def census(workspace: Workspace) -> set[str]:
        rows = workspace.database.execute(
            "SELECT r.slug AS slug, f.rel_path AS path FROM files f "
            "JOIN repositories r ON r.id = f.repo_id"
        ).fetchall()
        return {f"{row['slug']}/{row['path']}" for row in rows}

    assert census(forward) == census(backward)
    assert len(census(forward)) == len(REPOSITORIES) * len(PATHS)


# -- and the limited lookups agree anyway -------------------------------------


def test_the_literal_lookup_cuts_in_the_same_place(forward: Workspace, backward: Workspace) -> None:
    ahead = _chunk_identities(forward)
    assert len(ahead) == LIMIT, "the limit must actually cut for this to mean anything"
    assert ahead == _chunk_identities(backward)


def test_the_basename_lookup_cuts_in_the_same_place(
    forward: Workspace, backward: Workspace
) -> None:
    terms = [SHARED_BASENAME]
    ahead = _file_identities(forward, terms)
    assert len(ahead) == LIMIT, "the limit must actually cut for this to mean anything"
    assert ahead == _file_identities(backward, terms)


def test_the_path_lookup_cuts_in_the_same_place(forward: Workspace, backward: Workspace) -> None:
    terms = [path for path in PATHS if path.endswith(SHARED_BASENAME)]
    ahead = _file_identities(forward, terms)
    assert len(ahead) == LIMIT, "the limit must actually cut for this to mean anything"
    assert ahead == _file_identities(backward, terms)


def test_the_qualified_path_lookup_cuts_in_the_same_place(
    forward: Workspace, backward: Workspace
) -> None:
    terms = [f"{slug}/{path}" for slug in REPOSITORIES for path in PATHS]
    ahead = _file_identities(forward, terms)
    assert len(ahead) == LIMIT
    assert ahead == _file_identities(backward, terms)


def test_the_entity_name_lookup_cuts_in_the_same_place(
    forward: Workspace, backward: Workspace
) -> None:
    ahead = _entity_identities(forward)
    assert len(ahead) == LIMIT, "the limit must actually cut for this to mean anything"
    assert ahead == _entity_identities(backward)


def test_the_order_is_stated_in_repository_facts_not_row_ids(forward: Workspace) -> None:
    """The surviving prefix is the alphabetically first, by slug then path.

    Asserted as the property rather than as a recorded list: a golden file would
    pass just as happily if the order became something else deterministic.
    """
    identities = [(slug, path) for slug, path, _ in _entity_identities(forward)]
    assert identities == sorted(identities)

    chunks = [(slug, path) for slug, path, _ in _chunk_identities(forward)]
    assert chunks == sorted(chunks)


# -- ordering did not become ranking ------------------------------------------


def test_an_explicit_exact_match_is_unaffected(forward: Workspace, backward: Workspace) -> None:
    """A term present in one file only still resolves to that file, both ways."""
    for workspace in (forward, backward):
        hits = workspace.search.find_literal(
            "SHARED_LITERAL_MARKER_42 in lib/middle.py", None, 50
        ).hits
        assert {hit.path for hit in hits} == {"lib/middle.py"}
        assert {hit.repository_slug for hit in hits} == set(REPOSITORIES)


def test_repository_scope_still_narrows(forward: Workspace) -> None:
    alpha = forward.database.execute("SELECT id FROM repositories WHERE slug = 'alpha'").fetchone()[
        "id"
    ]
    hits = forward.search.find_literal(LITERAL, [alpha], 50).hits
    assert hits
    assert {hit.repository_slug for hit in hits} == {"alpha"}
