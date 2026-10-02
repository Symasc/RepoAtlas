"""Exact text lookup finds exact matches before its limit; an unfinished lookup proves nothing.

``search_literal("API_KEY", limit=30)`` returned nothing for a repository whose
``zzz/NOTES.md`` says ``Export API_KEY``. The direct pass filtered chunks with
SQL ``LIKE``, which folds ASCII case, applied ``LIMIT`` to what that admitted, and
only then checked the term case-exactly in Python. Thirty-five chunks holding
``api_key`` sort ahead of the note, so the bound filled with near misses and the
one real occurrence was never read. ``LIKE`` also stops at an embedded NUL, so
text after one — which a file past the binary sniff window can hold — was never
found at all, and that lookup even reported itself complete.

The predicate is now ``instr``: case-sensitive, code-point exact, free of
wildcards and NUL-safe, which is the contract the Python check always stated. It
sits where ``LIKE`` sat, beside the repository and path restrictions and before
``ORDER BY`` and ``LIMIT``, so every row the bound admits is a genuine match.

And suppression — "none of the named terms exist" — is decided only from lookups
that finished. A text lookup that stopped at its bound, or a shared identity
lookup a term was crowded out of, is not a negative; the funnel stays open and
the term's state says what was and was not established.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.retrieval import LookupOutcome, TermLookup, TermMatch, TermState
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import ChunkHit, LiteralMatches
from repoatlas.storage.sqlite.search import SqliteSearchIndex, path_scope
from tests.conftest import init_git_repository

REPO = "near"
#: Sorts ahead of :data:`REPO` and holds the exact term many times over.
NOISE = "alpha"
LIMIT = 30
#: More than :data:`LIMIT`, in paths that sort ahead of the exact occurrence.
FLOOD = 35
CONTEXT = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=REPO)

MATCH = LookupOutcome.MATCH
NO_MATCH = LookupOutcome.NO_MATCH
INCOMPLETE = LookupOutcome.INCOMPLETE
TEXT, FILE_PATH, ENTITY_NAME = TermLookup.TEXT, TermLookup.FILE_PATH, TermLookup.ENTITY_NAME
NOWHERE = {TEXT: NO_MATCH, FILE_PATH: NO_MATCH, ENTITY_NAME: NO_MATCH}


@pytest.fixture
def near(workspace: Workspace, tmp_path: Path) -> Iterator[Workspace]:
    """One exact ``API_KEY`` behind more case-insensitive near misses than the limit.

    Each package also keeps an extensionless ``run-tests`` script: 35 files and 35
    file entities share that name, so a question naming it fills both shared
    identity lookups, and ``aaa/zzz/deploy.yml`` sorts behind them. Neither name
    occurs in any file's text.
    """
    root = tmp_path / "repos"
    repo = root / REPO
    for n in range(FLOOD):
        package = repo / "aaa" / f"pkg-{n:02d}"
        package.mkdir(parents=True)
        (package / "index.ts").write_text(f'export const api_key = "k{n}";\n', encoding="utf-8")
        (package / "run-tests").write_text("echo ok\n", encoding="utf-8")
    (repo / "aaa" / "zzz").mkdir()
    (repo / "aaa" / "zzz" / "deploy.yml").write_text("steps: []\n", encoding="utf-8")
    (repo / "zzz").mkdir()
    (repo / "zzz" / "NOTES.md").write_text("Export API_KEY before starting.\n", encoding="utf-8")

    noise = root / NOISE / "src"
    noise.mkdir(parents=True)
    for n in range(FLOOD):
        # Plain text: a file that *reads* the variable would intern `env:API_KEY`,
        # an entity of no repository that every scope admits.
        (noise / f"env-{n:02d}.txt").write_text("API_KEY is required.\n", encoding="utf-8")
    init_git_repository(repo)
    init_git_repository(root / NOISE)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    yield workspace


def _repo_ids(workspace: Workspace, slug: str) -> list[int]:
    repo = next(r for r in workspace.repositories.list_all() if r.slug == slug)
    assert repo.id is not None
    return [repo.id]


def _chunks(workspace: Workspace, repo_ids: Sequence[int]) -> list[tuple[int, str, str]]:
    placeholders = ",".join("?" * len(repo_ids))
    sql = "SELECT c.id, f.rel_path, c.text FROM chunks c JOIN files f ON f.id = c.file_id"
    sql += f" WHERE c.repo_id IN ({placeholders})"
    rows = workspace.database.execute(sql, tuple(repo_ids)).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


# -- the store: what the bounded lookup reads -------------------------------


def test_the_near_misses_outnumber_the_limit_and_sort_ahead_of_the_occurrence(
    near: Workspace,
) -> None:
    """The shape of the defect, established from the stored text itself."""
    chunks = _chunks(near, _repo_ids(near, REPO))
    folded = sorted(path for _, path, text in chunks if "api_key" in text.lower())
    exact = [path for _, path, text in chunks if "API_KEY" in text]
    assert len(folded) > LIMIT
    assert exact == ["zzz/NOTES.md"] and folded[-1] == "zzz/NOTES.md"


def test_an_exact_occurrence_behind_more_near_misses_than_the_limit_is_found(
    near: Workspace,
) -> None:
    ids = _repo_ids(near, REPO)
    found = near.search.find_literal("API_KEY", ids, LIMIT)
    assert [hit.path for hit in found.hits] == ["zzz/NOTES.md"]
    assert found.exhaustive
    assert [hit.path for hit in near.search.find_literal("API_KEY", ids, LIMIT).hits] == [
        "zzz/NOTES.md"
    ]


@pytest.mark.parametrize("term", ["Api_Key", "API_key", "api_KEY"])
def test_a_case_variant_of_indexed_text_is_not_a_match(near: Workspace, term: str) -> None:
    found = near.search.find_literal(term, _repo_ids(near, REPO), LIMIT)
    assert found.hits == []
    assert found.exhaustive


def test_an_absent_identifier_is_absent_after_a_complete_lookup(near: Workspace) -> None:
    found = near.search.find_literal("API_TOKEN", _repo_ids(near, REPO), LIMIT)
    assert found == LiteralMatches(hits=[], exhaustive=True)


def test_a_term_with_more_matches_than_the_limit_fills_it_in_path_order(near: Workspace) -> None:
    ids = _repo_ids(near, REPO)
    found = near.search.find_literal("api_key", ids, LIMIT)
    assert [hit.path for hit in found.hits] == [f"aaa/pkg-{n:02d}/index.ts" for n in range(LIMIT)]
    assert not found.exhaustive
    assert near.search.find_literal("api_key", ids, LIMIT) == found


def test_the_repository_scope_applies_before_the_limit(near: Workspace) -> None:
    unscoped = near.search.find_literal("API_KEY", None, LIMIT).hits
    assert len(unscoped) == LIMIT
    assert {hit.repository_slug for hit in unscoped} == {NOISE}, "the bound must be full of noise"

    scoped = near.search.find_literal("API_KEY", _repo_ids(near, REPO), LIMIT).hits
    assert [(hit.repository_slug, hit.path) for hit in scoped] == [(REPO, "zzz/NOTES.md")]


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("zzz", ["zzz/NOTES.md"]),
        ("zzz/NOTES.md", ["zzz/NOTES.md"]),
        ("aaa", []),
        ("zz", []),  # a segment boundary, not a string prefix
    ],
)
def test_the_path_scope_applies_before_the_limit(
    near: Workspace, prefix: str, expected: list[str]
) -> None:
    found = near.search.find_literal("API_KEY", _repo_ids(near, REPO), LIMIT, path_prefix=prefix)
    assert [hit.path for hit in found.hits] == expected
    assert found.exhaustive


def test_a_path_scope_reaches_a_match_the_unscoped_limit_cuts_off(near: Workspace) -> None:
    ids = _repo_ids(near, REPO)
    unscoped = near.search.find_literal("api_key", ids, LIMIT).hits
    assert "aaa/pkg-34/index.ts" not in {hit.path for hit in unscoped}
    scoped = near.search.find_literal("api_key", ids, LIMIT, path_prefix="aaa/pkg-34").hits
    assert [hit.path for hit in scoped] == ["aaa/pkg-34/index.ts"]


def _prefiltered_like(
    index: SqliteSearchIndex,
    term: str,
    repo_ids: Sequence[int] | None,
    limit: int,
    path_prefix: str | None = None,
) -> tuple[list[ChunkHit], int]:
    """The direct pass as it was: bounded by a case-insensitive ``LIKE``, then checked."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    params: list[object] = [f"%{escaped}%"]
    scope = ""
    if repo_ids is not None:
        scope = f" AND c.repo_id IN ({','.join('?' * len(repo_ids))})"
        params.extend(repo_ids)
    scope += path_scope("f", path_prefix, params)
    params.append(limit)
    sql = "SELECT c.id FROM chunks c JOIN files f ON f.id = c.file_id"
    sql += " JOIN repositories r ON r.id = c.repo_id WHERE c.text LIKE ? ESCAPE '\\'"
    sql += scope + " ORDER BY r.slug, f.rel_path, c.line_start, c.span_index, c.split_index"
    admitted = [row[0] for row in index._db.execute(f"{sql} LIMIT ?", tuple(params)).fetchall()]
    exact, _ = _CURRENT(index, term, repo_ids, 1_000_000, path_prefix)
    by_id = {hit.chunk_id: hit for hit in exact}
    return [by_id[chunk_id] for chunk_id in admitted if chunk_id in by_id], len(admitted)


_CURRENT = SqliteSearchIndex._literal_chunks


@pytest.mark.parametrize(
    ("term", "prefix"),
    [
        ("api_key", None),  # more matches than the limit, none of them case variants within it
        ("api_key", "aaa/pkg-3"),
        ("export const", None),
        ("k3", None),
        ("echo ok", None),
        ("API_TOKEN", None),
    ],
)
def test_a_term_the_old_prefilter_did_not_crowd_gets_the_same_hits_in_the_same_order(
    near: Workspace, term: str, prefix: str | None
) -> None:
    """Nothing but the crowded case changes: same rows, same order, same bound."""
    ids = _repo_ids(near, REPO)
    before = _prefiltered_like(near.search, term, ids, LIMIT, prefix)
    assert near.search._literal_chunks(term, ids, LIMIT, prefix) == before


# -- the store: characters the predicate must take literally ----------------

SPECIAL_FILES = {
    "notes/symbols.txt": (
        "progress: 100% done\nsnake a_b and axb\nC:\\path\\to\\thing\nglob x*y arr[0] what?\n"
    ),
    "notes/unicode.txt": (
        "\u00c9lan cr\u00e9er_utilisateur\n\u63a5\u7d9a\u3067\u304d\u307e\u305b\u3093\u3067\u3057"
        "\u305f \U0001f680\ndecomposed e\u0301 and composed \u00e9\n"
    ),
    "notes/quotes.txt": "say \"hi\" and 'bye'\ntab\there\r\nwindows line\n",
    # Past the binary sniff window, so the file is indexed as text with the NUL in it.
    "notes/tail.txt": "".join(f"// padding line {n:04d}\n" for n in range(420))
    + "x = '\x00' NUL_TAIL_MARKER\n",
}
SPECIAL_TERMS = [
    "100%",
    "100 done",
    "a_b",
    "axb",
    "\\path",
    "/path",
    "x*y",
    "xzzy",
    "arr[0]",
    "arr0",
    "t?",
    "\u00c9lan",
    "\u00e9lan",
    "cr\u00e9er_utilisateur",
    "CR\u00c9ER_UTILISATEUR",
    "creer_utilisateur",
    "\u3067\u304d\u307e\u305b\u3093",
    "\U0001f680",
    "e\u0301",
    "\u00e9 ",
    "\"hi\" and 'bye'",
    "\there\r\n",
    "here\nwindows",
    "NUL_TAIL_MARKER",
    "\x00' NUL",
    "%_absent_%",
]


@pytest.fixture
def specials(workspace: Workspace, tmp_path: Path) -> Iterator[Workspace]:
    root = tmp_path / "repos"
    for rel, text in SPECIAL_FILES.items():
        path = root / REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    init_git_repository(root / REPO)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    yield workspace


@pytest.mark.parametrize("term", SPECIAL_TERMS)
def test_exact_text_lookup_is_the_substring_contract(specials: Workspace, term: str) -> None:
    """Every chunk whose stored text contains the term, and no other, however it is spelled."""
    ids = _repo_ids(specials, REPO)
    expected = sorted(chunk_id for chunk_id, _, text in _chunks(specials, ids) if term in text)
    found = specials.search.find_literal(term, ids, 1_000)
    assert sorted(hit.chunk_id for hit in found.hits) == expected
    assert found.exhaustive


def test_text_after_an_embedded_nul_is_found(specials: Workspace) -> None:
    ids = _repo_ids(specials, REPO)
    assert any("\x00" in text for _, _, text in _chunks(specials, ids)), "the NUL must be indexed"
    assert [hit.path for hit in specials.search.find_literal("NUL_TAIL_MARKER", ids).hits] == [
        "notes/tail.txt"
    ]


# -- the search: term states and suppression -------------------------------


def _search(workspace: Workspace, question: str, **kwargs: object) -> SearchResponse:
    return CapabilityService(workspace).search(
        SearchRequest(question=question, context=CONTEXT, **kwargs)  # type: ignore[arg-type]
    )


def _terms(response: SearchResponse) -> dict[str, TermMatch]:
    return {match.term: match for match in response.terms}


def test_api_key_is_found_as_text_behind_its_near_misses(near: Workspace) -> None:
    response = _search(near, "API_KEY", include_trace=True)
    assert response.suppressed is None
    assert response.trace is not None
    exact = next(stage for stage in response.trace.stages if stage.name == "exact")
    assert exact.candidates_out == 1  # the note, and only the note
    match = _terms(response)["API_KEY"]
    assert match.checks == {TEXT: MATCH, FILE_PATH: NO_MATCH, ENTITY_NAME: NO_MATCH}
    assert match.state is TermState.TEXT_ONLY


@pytest.mark.xfail(
    strict=True,
    reason=(
        "pre-existing, outside this change: the symbol stage's entity full-text pass "
        "labels a hit EXACT_NAME when its name equals a question term case-insensitively, "
        "so 30 `api_key` declarations outrank the exact-content hit and the budget cuts it"
    ),
)
def test_the_exact_occurrence_reaches_the_evidence(near: Workspace) -> None:
    response = _search(near, "API_KEY")
    assert "zzz/NOTES.md" in {item.ref.path for item in response.evidence}


@pytest.mark.parametrize(
    ("within_path", "state"),
    [(None, TermState.TEXT_ONLY), ("zzz", TermState.TEXT_ONLY), ("aaa", TermState.NOT_FOUND)],
)
def test_api_key_follows_the_path_scope(
    near: Workspace, within_path: str | None, state: TermState
) -> None:
    response = _search(near, "API_KEY", within_path=within_path)
    assert _terms(response)["API_KEY"].state is state
    # Under `aaa` only near misses exist, and a complete lookup establishes that.
    assert (response.suppressed is not None) is (state is TermState.NOT_FOUND)


def test_a_genuinely_absent_identifier_is_still_suppressed(near: Workspace) -> None:
    response = _search(near, "API_TOKEN")
    assert response.suppressed is not None
    assert [(m.term, m.state, m.checks) for m in response.terms] == [
        ("API_TOKEN", TermState.NOT_FOUND, NOWHERE)
    ]


@pytest.mark.parametrize("term", ["Api_Key", "API_key"])
def test_a_case_variant_of_indexed_text_is_not_found(near: Workspace, term: str) -> None:
    response = _search(near, term)
    assert response.suppressed is not None
    assert _terms(response)[term].checks == NOWHERE
    assert _terms(response)[term].state is TermState.NOT_FOUND


def test_named_terms_with_mixed_outcomes_are_each_reported(near: Workspace) -> None:
    response = _search(near, "API_KEY API_TOKEN api_key Api_Key")
    assert response.suppressed is None
    terms = _terms(response)
    assert {term: match.state for term, match in terms.items()} == {
        "API_KEY": TermState.TEXT_ONLY,
        "API_TOKEN": TermState.NOT_FOUND,
        "api_key": TermState.EXACT,
        "Api_Key": TermState.NOT_FOUND,
    }
    assert all(INCOMPLETE not in match.checks.values() for match in terms.values())


@pytest.mark.parametrize(
    ("within_path", "exists"), [(None, True), ("aaa", True), ("aaa/zzz", True), ("zzz", False)]
)
def test_a_term_crowded_out_of_a_shared_lookup_is_not_declared_absent(
    near: Workspace, within_path: str | None, exists: bool
) -> None:
    """``run-tests`` fills the file and entity lookups; ``deploy.yml`` sorts behind it.

    Only its identity proves ``deploy.yml`` exists — no text mentions it — and a
    full shared lookup that did not reach it is not a negative. It is asked again
    alone before suppression is decided, so the question is not declared
    unanswerable about a file the index holds. Where the file genuinely is not,
    suppression still says so.
    """
    response = _search(near, "run-tests deploy.yml", within_path=within_path)
    match = _terms(response)["deploy.yml"]
    if exists:
        assert response.suppressed is None
        assert match.checks == {TEXT: NO_MATCH, FILE_PATH: MATCH, ENTITY_NAME: MATCH}
        assert match.state is TermState.EXACT
    else:
        assert response.suppressed is not None
        assert match.checks == NOWHERE
        assert match.state is TermState.NOT_FOUND


@pytest.fixture
def cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the text lookup for ``API_TOKEN`` stop at its bound, as a store may.

    ``LiteralMatches.exhaustive`` is part of the store protocol. The SQLite store
    no longer ends a lookup that way without a match, but the funnel must honour
    the flag from whichever store answers.
    """
    original = SqliteSearchIndex.find_literal

    def bounded(
        self: SqliteSearchIndex,
        term: str,
        repo_ids: Sequence[int] | None = None,
        limit: int = 50,
        *,
        path_prefix: str | None = None,
    ) -> LiteralMatches:
        found = original(self, term, repo_ids, limit, path_prefix=path_prefix)
        if term == "API_TOKEN":
            return LiteralMatches(hits=found.hits, exhaustive=False)
        return found

    monkeypatch.setattr(SqliteSearchIndex, "find_literal", bounded)


@pytest.mark.usefixtures("cut_off")
def test_an_incomplete_text_lookup_is_never_proof_of_absence(near: Workspace) -> None:
    response = _search(near, "API_TOKEN")
    assert response.suppressed is None
    match = _terms(response)["API_TOKEN"]
    assert match.checks == {TEXT: INCOMPLETE, FILE_PATH: NO_MATCH, ENTITY_NAME: NO_MATCH}
    assert match.state is TermState.NOT_ESTABLISHED


@pytest.mark.usefixtures("cut_off")
def test_an_incomplete_lookup_beside_an_absent_term_reports_each(near: Workspace) -> None:
    response = _search(near, "API_TOKEN SECRET_TOKEN")
    assert response.suppressed is None
    terms = _terms(response)
    assert terms["API_TOKEN"].state is TermState.NOT_ESTABLISHED
    assert terms["SECRET_TOKEN"].state is TermState.NOT_FOUND
    assert terms["SECRET_TOKEN"].checks == NOWHERE


@pytest.mark.parametrize(
    ("question", "within_path"),
    [
        ("API_KEY", None),  # text_only
        ("API_KEY", "aaa"),  # not_found, suppressed
        ("API_KEY API_TOKEN api_key Api_Key", None),  # mixed
        ("run-tests deploy.yml", "aaa"),  # crowded, then settled
    ],
)
def test_full_and_compact_presentations_carry_the_same_states(
    near: Workspace, question: str, within_path: str | None
) -> None:
    service = CapabilityService(near)
    full = SearchRequest(question=question, context=CONTEXT, within_path=within_path)
    compact = SearchRequest(
        question=question,
        context=CONTEXT,
        within_path=within_path,
        presentation="compact",  # type: ignore[arg-type]
    )
    full_terms = present(full, service.search(full))["terms"]
    assert full_terms == present(compact, service.search(compact))["terms"]
    assert full_terms and all(
        set(term) == {"term", "state", "lookups", "checks"} for term in full_terms
    )


def test_compact_api_key_states_serialize_every_check(near: Workspace) -> None:
    request = SearchRequest(
        question="API_KEY",
        context=CONTEXT,
        presentation="compact",  # type: ignore[arg-type]
    )
    assert present(request, CapabilityService(near).search(request))["terms"] == [
        {
            "term": "API_KEY",
            "state": "text_only",
            "lookups": ["text", "file_path", "entity_name"],
            "checks": {"text": "match", "file_path": "no_match", "entity_name": "no_match"},
        }
    ]


@pytest.mark.parametrize(
    "question", ["api_key", "k3", "run-tests", "API_TOKEN", "export const api_key"]
)
def test_an_unaffected_question_keeps_its_evidence_ranking_and_budgets(
    near: Workspace, question: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question the old prefilter did not crowd is answered exactly as before."""
    after = _search(near, question, include_trace=True)
    monkeypatch.setattr(SqliteSearchIndex, "_literal_chunks", _prefiltered_like)
    before = _search(near, question, include_trace=True)
    assert [(i.ref.path, i.ref.line_start, i.rank) for i in after.evidence] == [
        (i.ref.path, i.ref.line_start, i.rank) for i in before.evidence
    ]
    assert after.terms == before.terms
    assert after.suppressed == before.suppressed
    assert after.trace is not None and before.trace is not None
    assert [(s.name, s.candidates_out) for s in after.trace.stages] == [
        (s.name, s.candidates_out) for s in before.trace.stages
    ]
