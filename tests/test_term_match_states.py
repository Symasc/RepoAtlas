"""Whether each term a question named is in the index: exact, text only, or not found.

An agent asked LVL whether a password-reset implementation existed:
``password_resets table resetPassword logoutUser credentials createdDate
reset-requests reset-completions``. Five of those spellings exist nowhere in LVL
and ``resetPassword`` exists only as a fidelity test case, yet the search could
not say so. Suppression answers only when *every* definite term is absent, and one
content hit — ``resetPassword`` inside ``resetPasswordAllowed`` — kept the question
open; the twelve evidence items that came back were ranked and budgeted, so a
term's absence from them proved nothing.

``SearchResponse.terms`` states what the exhaustive exact lookups found for each
term the question named, and which lookups those were. It is built from proofs
the funnel already makes. The tests hold it to being right in both directions and
honest about coverage: an unindexed file is outside it, a prose word is not a
term, and a name the symbol stage never looked up is not reported as missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.retrieval import TermLookup, TermMatch, TermState
from repoatlas.models.search import SearchRequest
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

REPO = "identity"
CONTEXT = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=REPO)
ALL_THREE = (TermLookup.TEXT, TermLookup.FILE_PATH, TermLookup.ENTITY_NAME)

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
The design notes call the flow pwResetFlow.
""",
    "apps/desktop/table.spec.ts": """it("renders", () => {
  const table = render();
  const credentials = login();
});
""",
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


def _terms(workspace: Workspace, question: str, **kwargs: object) -> dict[str, TermMatch]:
    response = CapabilityService(workspace).search(
        SearchRequest(question=question, context=CONTEXT, **kwargs)  # type: ignore[arg-type]
    )
    return {match.term: match for match in response.terms}


def test_an_existing_identifier_is_exact(identity: Workspace) -> None:
    terms = _terms(identity, "findByEmail")
    assert terms["findByEmail"].state is TermState.EXACT
    assert terms["findByEmail"].lookups == ALL_THREE


def test_a_nonexistent_identifier_is_not_found_even_when_suppressed(identity: Workspace) -> None:
    service = CapabilityService(identity)
    response = service.search(SearchRequest(question="logoutUser", context=CONTEXT))
    assert response.suppressed is not None
    assert [(m.term, m.state) for m in response.terms] == [("logoutUser", TermState.NOT_FOUND)]


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
    # `table` and `credentials` are words the question used, not names it gave,
    # even though local variables in a test file are called exactly that.
    assert "table" not in terms and "credentials" not in terms


def test_text_inside_a_longer_word_is_text_only_not_a_declaration(identity: Workspace) -> None:
    # Only the document mentions it, and only inside `resetPasswordAllowed`.
    in_docs = _terms(identity, "resetPassword", within_path="docs")
    assert in_docs["resetPassword"].state is TermState.TEXT_ONLY
    # Where it is declared, the declaration makes it exact.
    in_tests = _terms(identity, "resetPassword", within_path="test")
    assert in_tests["resetPassword"].state is TermState.EXACT
    # And a name written only in prose is text, never a declaration.
    assert _terms(identity, "pwResetFlow")["pwResetFlow"].state is TermState.TEXT_ONLY


def test_a_pascal_case_name_reports_the_lookups_it_had(identity: Workspace) -> None:
    terms = _terms(identity, "Which methods does KeycloakAdminClient have?")
    match = terms["KeycloakAdminClient"]
    assert match.state is TermState.EXACT
    assert TermLookup.TEXT not in match.lookups
    assert TermLookup.ENTITY_NAME in match.lookups


def test_membership_is_asked_by_qualified_name(identity: Workspace) -> None:
    terms = _terms(identity, "KeycloakAdminClient.resetPassword KeycloakAdminClient.findByEmail")
    assert terms["KeycloakAdminClient.resetPassword"].state is TermState.NOT_FOUND
    assert terms["KeycloakAdminClient.findByEmail"].state is TermState.EXACT


def test_a_broad_question_names_no_terms(identity: Workspace) -> None:
    assert _terms(identity, "How does the identity service verify email addresses?") == {}


def test_a_name_the_symbol_stage_never_looked_up_is_not_reported_missing(
    identity: Workspace,
) -> None:
    """Under suppression the symbol stage does not run, so it proves nothing."""
    service = CapabilityService(identity)
    response = service.search(
        SearchRequest(question="KeycloakAdminClient logout_user", context=CONTEXT)
    )
    assert response.suppressed is not None
    assert [m.term for m in response.terms] == ["logout_user"]


def test_an_http_key_is_looked_up_as_a_key(identity: Workspace) -> None:
    match = _terms(identity, "Who serves GET /password-resets?")["http-route:GET:/password-resets"]
    assert match.state is TermState.NOT_FOUND
    assert match.lookups == (TermLookup.ENTITY_KEY,)


def test_a_file_outside_the_indexed_coverage_is_not_found(identity: Workspace) -> None:
    """Honest about coverage: the oversized file exists and is not indexed text."""
    assert _terms(identity, "HUGE_ONLY_MARKER")["HUGE_ONLY_MARKER"].state is TermState.NOT_FOUND


def test_states_follow_the_path_scope(identity: Workspace) -> None:
    scoped = _terms(identity, "findByEmail", within_path="docs")
    assert scoped["findByEmail"].state is TermState.NOT_FOUND
    assert _terms(identity, "findByEmail")["findByEmail"].state is TermState.EXACT
