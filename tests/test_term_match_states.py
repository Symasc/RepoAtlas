"""Whether each term a question named is in the index, and how much of that is established.

An agent asked LVL whether a password-reset implementation existed:
``password_resets table resetPassword logoutUser credentials createdDate
reset-requests reset-completions``. Five of those spellings exist nowhere in LVL
and ``resetPassword`` exists only as a fidelity test case, yet the search could
not say so. Suppression answers only when *every* definite term is absent, and one
content hit — ``resetPassword`` inside ``resetPasswordAllowed`` — kept the question
open; the twelve evidence items that came back were ranked and budgeted, so a
term's absence from them proved nothing.

``SearchResponse.terms`` states, for each term the question named, what each exact
lookup established: ``checks`` maps every lookup relevant to the term to
``match``, ``no_match``, ``incomplete`` (it stopped at a limit first) or
``not_checked``. ``state`` summarises only what those establish. ``not_found``
needs every relevant lookup to have run to completion and found nothing; when one
did not run or did not finish, the state is ``not_established``. The measured case
that forced that rule was ``architecture`` inside ``docs``: the search returned the
architecture document and reported the term ``not_found``, because the term had
been looked up as a name and never as text.

The tests hold the states to being right in both directions and honest about
coverage: an unindexed file is outside it, a prose word is not a term, a lookup
that never ran or was cut short never counts as a negative, and a limit that
crowds a term out of a shared lookup does not hide it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.retrieval import LookupOutcome, TermLookup, TermMatch, TermState
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

REPO = "identity"
CONTEXT = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=REPO)
ALL_THREE = (TermLookup.TEXT, TermLookup.FILE_PATH, TermLookup.ENTITY_NAME)

MATCH = LookupOutcome.MATCH
NO_MATCH = LookupOutcome.NO_MATCH
INCOMPLETE = LookupOutcome.INCOMPLETE
NOT_CHECKED = LookupOutcome.NOT_CHECKED
TEXT, FILE_PATH, ENTITY_NAME = ALL_THREE
NOWHERE = {TEXT: NO_MATCH, FILE_PATH: NO_MATCH, ENTITY_NAME: NO_MATCH}

FILES = {
    "src/keycloak/admin.client.ts": """export class KeycloakAdminClient {
  async findByEmail(email: string): Promise<string[]> {
    return [email];
  }

  async markEmailVerified(userId: string): Promise<void> {
    void userId;
  }
}
""",
    "src/database/schema.ts": (
        'export const emailVerifications = identitySchema.table("email_verifications", {\n'
        '  id: uuid("id"),\n'
        "});\n"
    ),
    "test/cases.ts": """export const resetPassword = { name: "reset password case" };
""",
    "docs/realm.md": """# Realm

The realm sets resetPasswordAllowed so Keycloak offers the forgot-password link.
The design notes call the flow pwResetFlow, and the PwResetFlowHelper they name is not code.
""",
    "docs/ARCHITECTURE.md": """# Architecture

The architecture has two layers: the realm, and the admin client that drives it.
Each layer is described in its own section of this architecture note.
""",
    "apps/desktop/table.spec.ts": """it("renders", () => {
  const table = render();
  const credentials = login();
});
""",
    # Outside the indexed coverage: excluded by the repository's own ignore file.
    ".repoatlasignore": "private/notes.md\n",
    "private/notes.md": "IGNORED_ONLY_MARKER lives here and is never indexed.\n",
}


@pytest.fixture
def identity(workspace: Workspace, tmp_path: Path) -> Iterator[Workspace]:
    root = tmp_path / "repos"
    for rel, text in FILES.items():
        path = root / REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # Outside the indexed coverage: larger than the file-size cap.
    oversized = root / REPO / "generated" / "huge.ts"
    oversized.parent.mkdir(parents=True)
    oversized.write_text(
        "export const HUGE_ONLY_MARKER = 1;\n" + "// padding\n" * 110_000, encoding="utf-8"
    )
    init_git_repository(root / REPO)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    yield workspace


def _search(workspace: Workspace, question: str, **kwargs: object) -> SearchResponse:
    return CapabilityService(workspace).search(
        SearchRequest(question=question, context=CONTEXT, **kwargs)  # type: ignore[arg-type]
    )


def _terms(workspace: Workspace, question: str, **kwargs: object) -> dict[str, TermMatch]:
    return {match.term: match for match in _search(workspace, question, **kwargs).terms}


# -- the measured case -------------------------------------------------------


def test_architecture_within_docs_is_not_established_rather_than_not_found(
    identity: Workspace,
) -> None:
    """The search returns the document and must not call the word absent from it."""
    response = _search(identity, "architecture", within_path="docs")
    assert "docs/ARCHITECTURE.md" in {item.ref.path for item in response.evidence}
    match = {m.term: m for m in response.terms}["architecture"]
    # Looked up as a file name and as an entity name, and neither exists; never
    # searched for as text, so nothing about its text has been established.
    assert match.checks == {TEXT: NOT_CHECKED, FILE_PATH: NO_MATCH, ENTITY_NAME: NO_MATCH}
    assert match.state is TermState.NOT_ESTABLISHED
    assert match.lookups == (FILE_PATH, ENTITY_NAME)


# -- exact, text only, not found: each established by what ran --------------


def test_an_existing_identifier_is_exact(identity: Workspace) -> None:
    terms = _terms(identity, "findByEmail")
    assert terms["findByEmail"].state is TermState.EXACT
    assert terms["findByEmail"].lookups == ALL_THREE
    assert terms["findByEmail"].checks == {TEXT: MATCH, FILE_PATH: NO_MATCH, ENTITY_NAME: MATCH}


def test_a_nonexistent_identifier_is_not_found_even_when_suppressed(identity: Workspace) -> None:
    response = _search(identity, "logoutUser")
    assert response.suppressed is not None
    assert [(m.term, m.state, m.checks) for m in response.terms] == [
        ("logoutUser", TermState.NOT_FOUND, NOWHERE)
    ]


def test_a_thing_that_exists_under_another_name_is_not_found_by_this_one(
    identity: Workspace,
) -> None:
    terms = _terms(
        identity, "setEmailVerified markEmailVerified password_resets emailVerifications"
    )
    assert terms["setEmailVerified"].state is TermState.NOT_FOUND
    assert terms["markEmailVerified"].state is TermState.EXACT
    assert terms["password_resets"].state is TermState.NOT_FOUND
    assert terms["emailVerifications"].state is TermState.EXACT


def test_an_existing_and_a_nonexistent_qualified_identifier(identity: Workspace) -> None:
    # A bare name that also appears inside a dotted one is not a separate term of
    # the question, so the bare spelling is quoted to ask about both at once.
    terms = _terms(
        identity,
        'KeycloakAdminClient.findByEmail KeycloakAdminClient.resetPassword "resetPassword"',
    )
    assert terms["KeycloakAdminClient.findByEmail"].state is TermState.EXACT
    assert terms["KeycloakAdminClient.findByEmail"].checks[ENTITY_NAME] is MATCH
    # The unqualified name exists — a test case called resetPassword — and that
    # says nothing about the client: the qualified name is looked up as itself.
    assert terms["resetPassword"].state is TermState.EXACT
    assert terms["KeycloakAdminClient.resetPassword"].state is TermState.NOT_FOUND
    assert terms["KeycloakAdminClient.resetPassword"].checks == NOWHERE
    # And asked as the agent asked it — beside the class, unqualified — the bare
    # name is exact about the index, and still nothing more.
    q3 = _terms(identity, "KeycloakAdminClient findByEmail resetPassword setEmailVerified")
    assert q3["resetPassword"].state is TermState.EXACT
    assert q3["setEmailVerified"].state is TermState.NOT_FOUND


def test_one_entity_proves_every_spelling_it_answers_to(identity: Workspace) -> None:
    """The method is named ``markEmailVerified`` *and* qualified by its class.

    The store credits a row to the first requested spelling it matched. Asked
    for both, the quoted bare name took the only row's proof, and the qualified
    name of a method that exists was reported ``not_found``.
    """
    terms = _terms(identity, '"markEmailVerified" KeycloakAdminClient.markEmailVerified')
    assert terms["markEmailVerified"].state is TermState.EXACT
    assert terms["KeycloakAdminClient.markEmailVerified"].checks[ENTITY_NAME] is MATCH
    assert terms["KeycloakAdminClient.markEmailVerified"].state is TermState.EXACT


def test_the_agents_question_answers_every_named_term_and_no_prose_word(
    identity: Workspace,
) -> None:
    question = (
        "password_resets table resetPassword logoutUser credentials createdDate "
        "reset-requests reset-completions"
    )
    terms = _terms(identity, question)
    assert {term: match.state for term, match in terms.items()} == {
        "password_resets": TermState.NOT_FOUND,
        "resetPassword": TermState.EXACT,
        "logoutUser": TermState.NOT_FOUND,
        "createdDate": TermState.NOT_FOUND,
        "reset-requests": TermState.NOT_FOUND,
        "reset-completions": TermState.NOT_FOUND,
    }
    # Every negative is a completed lookup of all three kinds, not a guess.
    for term in ("password_resets", "logoutUser", "createdDate", "reset-requests"):
        assert terms[term].checks == NOWHERE
    assert terms["resetPassword"].checks[ENTITY_NAME] is MATCH
    # `table` and `credentials` are words the question used, not names it gave,
    # even though local variables in a test file are called exactly that.
    assert "table" not in terms and "credentials" not in terms


# -- text, and a lookup that never ran ---------------------------------------


def test_text_inside_a_longer_word_is_text_only_not_a_declaration(identity: Workspace) -> None:
    # Only the document mentions it, and only inside `resetPasswordAllowed`.
    in_docs = _terms(identity, "resetPassword", within_path="docs")
    assert in_docs["resetPassword"].state is TermState.TEXT_ONLY
    assert in_docs["resetPassword"].checks == {
        TEXT: MATCH,
        FILE_PATH: NO_MATCH,
        ENTITY_NAME: NO_MATCH,
    }
    # Where it is declared, the declaration makes it exact.
    in_tests = _terms(identity, "resetPassword", within_path="test")
    assert in_tests["resetPassword"].state is TermState.EXACT


def test_a_name_present_only_in_source_text(identity: Workspace) -> None:
    # A camelCase name is searched as text, so its text-only presence is proven.
    assert _terms(identity, "pwResetFlow")["pwResetFlow"].state is TermState.TEXT_ONLY
    # A PascalCase name is looked up only as a name. No entity or file carries it,
    # and whether any text does was never asked — which is not a negative.
    helper = _terms(identity, "Where is PwResetFlowHelper?")["PwResetFlowHelper"]
    assert helper.checks == {TEXT: NOT_CHECKED, FILE_PATH: NO_MATCH, ENTITY_NAME: NO_MATCH}
    assert helper.state is TermState.NOT_ESTABLISHED


def test_a_pascal_case_name_reports_the_lookups_it_had(identity: Workspace) -> None:
    match = _terms(identity, "Which methods does KeycloakAdminClient have?")["KeycloakAdminClient"]
    assert match.state is TermState.EXACT
    assert TermLookup.TEXT not in match.lookups
    assert match.checks[TEXT] is NOT_CHECKED
    assert match.checks[ENTITY_NAME] is MATCH


def test_a_lookup_that_never_ran_is_reported_as_not_checked(identity: Workspace) -> None:
    """Under suppression the symbol stage does not run, so it establishes nothing."""
    response = _search(identity, "KeycloakAdminClient logout_user")
    assert response.suppressed is not None
    terms = {m.term: m for m in response.terms}
    assert terms["logout_user"].state is TermState.NOT_FOUND
    # The class exists; it was never looked up, and is not reported missing.
    assert terms["KeycloakAdminClient"].state is TermState.NOT_ESTABLISHED
    assert terms["KeycloakAdminClient"].checks == {
        TEXT: NOT_CHECKED,
        FILE_PATH: NO_MATCH,
        ENTITY_NAME: NOT_CHECKED,
    }


# -- shapes of question -------------------------------------------------------


def test_membership_is_asked_by_qualified_name(identity: Workspace) -> None:
    terms = _terms(identity, "KeycloakAdminClient.resetPassword KeycloakAdminClient.findByEmail")
    assert terms["KeycloakAdminClient.resetPassword"].state is TermState.NOT_FOUND
    assert terms["KeycloakAdminClient.findByEmail"].state is TermState.EXACT


def test_a_broad_question_names_no_terms(identity: Workspace) -> None:
    assert _terms(identity, "How does the identity service verify email addresses?") == {}


def test_an_http_key_is_looked_up_as_a_key(identity: Workspace) -> None:
    match = _terms(identity, "Who serves GET /password-resets?")["http-route:GET:/password-resets"]
    assert match.state is TermState.NOT_FOUND
    assert match.lookups == (TermLookup.ENTITY_KEY,)
    assert match.checks == {TermLookup.ENTITY_KEY: NO_MATCH}


def test_a_path_and_its_basename_are_both_exact(identity: Workspace) -> None:
    """Two terms naming one file each get the file's proof, not only the closer one."""
    terms = _terms(identity, "docs/realm.md")
    assert terms["docs/realm.md"].state is TermState.EXACT
    assert terms["realm.md"].state is TermState.EXACT


def test_the_pieces_of_a_one_token_question_are_not_terms(identity: Workspace) -> None:
    """`docs` is a directory the question passed through, not a name it gave.

    The pieces are still looked up as entity names -- that is how `api/config.py`
    finds the module `config` -- but reporting them would say "docs: not_found"
    about a directory that plainly exists.
    """
    assert set(_terms(identity, "docs/realm.md")) == {"docs/realm.md", "realm.md"}
    assert set(_terms(identity, "src/keycloak/admin.client.ts")) == {
        "src/keycloak/admin.client.ts",
        "admin.client.ts",
    }


# -- coverage -----------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "HUGE_ONLY_MARKER",  # larger than the file-size cap
        "IGNORED_ONLY_MARKER",  # excluded by .repoatlasignore
    ],
)
def test_a_source_outside_the_indexed_coverage_is_not_found_in_it(
    identity: Workspace, marker: str
) -> None:
    """Honest about coverage: the file exists on disk and is not indexed text.

    ``not_found`` is a statement about the indexed coverage searched, and says so
    in the field and tool descriptions; it is not repository-wide absence.
    """
    assert _terms(identity, marker)[marker].state is TermState.NOT_FOUND


def test_states_follow_the_path_scope(identity: Workspace) -> None:
    scoped = _terms(identity, "findByEmail", within_path="docs")
    assert scoped["findByEmail"].state is TermState.NOT_FOUND
    assert scoped["findByEmail"].checks == NOWHERE
    assert _terms(identity, "findByEmail")["findByEmail"].state is TermState.EXACT


# -- limits -------------------------------------------------------------------

CROWDED = "crowded"
#: More than the per-source limit (30), in paths that sort ahead of everything else.
FLOOD = 35


@pytest.fixture
def crowded(workspace: Workspace, tmp_path: Path) -> Iterator[Workspace]:
    """Terms that match more than a lookup's limit, sorted ahead of a rarer one.

    Every lookup a term state reads is bounded, and three of them are shared by
    every term in the question. Each package here holds an ``index.ts`` (a basename
    35 files share), a ``handle_event`` (an entity name 35 declarations share) and
    a lowercase ``api_key`` (which a case-insensitive text prefilter cannot tell
    from ``API_KEY``). The rare names live in ``src/`` and ``zzz/``, behind them.
    """
    root = tmp_path / "repos"
    repo = root / CROWDED
    for n in range(FLOOD):
        package = repo / "aaa" / f"pkg-{n:02d}"
        package.mkdir(parents=True)
        (package / "index.ts").write_text(
            f'export function handle_event(): void {{}}\nexport const api_key = "k{n}";\n',
            encoding="utf-8",
        )
    (repo / "src").mkdir()
    (repo / "src" / "schema.ts").write_text(
        "export const emailVerifications = 1;\n", encoding="utf-8"
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "target.md").write_text("# Target\n\nA quiet document.\n", encoding="utf-8")
    (repo / "zzz").mkdir()
    (repo / "zzz" / "NOTES.md").write_text("Export API_KEY before starting.\n", encoding="utf-8")
    init_git_repository(repo)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    yield workspace


def _crowded_terms(workspace: Workspace, question: str) -> dict[str, TermMatch]:
    context = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=CROWDED)
    response = CapabilityService(workspace).search(
        SearchRequest(question=question, context=context)
    )
    return {match.term: match for match in response.terms}


def test_an_entity_name_crowded_out_of_a_shared_lookup_is_still_exact(
    crowded: Workspace,
) -> None:
    terms = _crowded_terms(crowded, "handle_event emailVerifications")
    assert terms["handle_event"].state is TermState.EXACT
    assert terms["emailVerifications"].checks[ENTITY_NAME] is MATCH
    assert terms["emailVerifications"].state is TermState.EXACT


def test_a_file_name_crowded_out_of_a_shared_lookup_is_still_exact(crowded: Workspace) -> None:
    terms = _crowded_terms(crowded, "index.ts target.md")
    assert terms["index.ts"].state is TermState.EXACT
    assert terms["target.md"].checks[FILE_PATH] is MATCH
    assert terms["target.md"].state is TermState.EXACT


def test_a_text_lookup_cut_off_by_its_limit_establishes_nothing(crowded: Workspace) -> None:
    """``API_KEY`` is in ``zzz/NOTES.md``; the rows the lookup read held ``api_key``.

    The text lookup stops at its limit before case-exact verification, so here it
    read 30 case-insensitive near misses and never reached the real occurrence.
    That is an incomplete lookup, and the state must not call the term absent.
    """
    match = _crowded_terms(crowded, "API_KEY")["API_KEY"]
    assert match.checks[TEXT] is INCOMPLETE
    assert match.state is TermState.NOT_ESTABLISHED


# -- presentation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "within_path"),
    [
        ("architecture", "docs"),  # not_established, text not checked
        ("pwResetFlow", None),  # text_only
        ("KeycloakAdminClient.resetPassword resetPassword", None),  # not_found, exact
        ("KeycloakAdminClient logout_user", None),  # not_checked under suppression
    ],
)
def test_compact_and_full_presentations_carry_the_same_states(
    identity: Workspace, question: str, within_path: str | None
) -> None:
    service = CapabilityService(identity)
    full = SearchRequest(question=question, context=CONTEXT, within_path=within_path)
    compact = SearchRequest(
        question=question,
        context=CONTEXT,
        within_path=within_path,
        presentation="compact",  # type: ignore[arg-type]
    )
    full_terms = present(full, service.search(full))["terms"]
    compact_terms = present(compact, service.search(compact))["terms"]
    assert full_terms == compact_terms
    assert full_terms and all(
        set(term) == {"term", "state", "lookups", "checks"} for term in full_terms
    )


def test_a_not_established_term_serializes_every_check_it_depends_on(identity: Workspace) -> None:
    request = SearchRequest(
        question="architecture",
        context=CONTEXT,
        within_path="docs",
        presentation="compact",  # type: ignore[arg-type]
    )
    terms = present(request, CapabilityService(identity).search(request))["terms"]
    assert terms == [
        {
            "term": "architecture",
            "state": "not_established",
            "lookups": ["file_path", "entity_name"],
            "checks": {"text": "not_checked", "file_path": "no_match", "entity_name": "no_match"},
        }
    ]
