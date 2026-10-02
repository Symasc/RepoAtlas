"""A search narrowed to one path inside one repository (``within_path``).

An agent investigating ``services/identity/`` in LVL could not say so, and desktop
tests took part of its result budget. A filter over the returned evidence would
not have helped: every retrieval channel applies its own ``LIMIT``, ordered by
path, and ``apps/…`` sorts before ``services/…``, so the in-path match is cut
before any post-filter could see it.

The ecosystem below makes that failure structural. Forty desktop test files
declare and mention the same names as the one identity file, more than the
per-source bound holds, and every one of them sorts first. Next to the identity
directory sit ``services/identity-client`` and ``services/identity0``, which a
string-prefix or range comparison done carelessly would admit.

Each channel is tested at the storage layer, where the bound lives, and the
capability is tested end to end for the contract: validation, the refusals, the
echo, and that without ``within_path`` nothing changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.errors import FileNotIndexedError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import MatchClass
from repoatlas.models.search import SearchRequest
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

MONO = "mono"
SCOPE = "services/identity"
IN_PATH = "services/identity/src/reset.ts"
NOISE = 40  # more than the default per-source bound of 30

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
REPO = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=MONO)

IDENTITY_SOURCE = """import { helper } from "../../../packages/shared/src/util";

export class ResetLedger {
  record(): string {
    return "IDENTITY_RESET_MARKER";
  }
}

export function resetToken(): string {
  const window = process.env.RESET_WINDOW_SECONDS;
  return helper(window ?? "900");
}
"""


def _noise(n: int) -> str:
    return f"""import {{ helper }} from "../../../../packages/shared/src/util";

export function resetToken(): string {{
  // IDENTITY_RESET_MARKER mentioned by desktop test {n}
  return helper(process.env.RESET_WINDOW_SECONDS ?? "{n}");
}}
"""


@pytest.fixture
def mono_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    mono = root / MONO
    (mono / "services" / "identity" / "src").mkdir(parents=True)
    (mono / IN_PATH).write_text(IDENTITY_SOURCE, encoding="utf-8")
    for near in ("services/identity-client/src", "services/identity0/src"):
        (mono / near).mkdir(parents=True)
        (mono / near / "reset.ts").write_text(_noise(999), encoding="utf-8")
    tests = mono / "apps" / "desktop" / "src" / "__tests__"
    tests.mkdir(parents=True)
    for n in range(NOISE):
        (tests / f"t{n:02d}").mkdir()
        (tests / f"t{n:02d}" / "reset.ts").write_text(_noise(n), encoding="utf-8")
    shared = mono / "packages" / "shared" / "src"
    shared.mkdir(parents=True)
    (shared / "util.ts").write_text(
        "export function helper(value: string): string {\n  return value;\n}\n", encoding="utf-8"
    )
    init_git_repository(mono)
    return root


@pytest.fixture
def indexed(workspace: Workspace, mono_root: Path, tmp_path: Path) -> Iterator[Workspace]:
    initialize(workspace, mono_root, config_path=tmp_path / "config.yaml")
    yield workspace


def _repo_id(workspace: Workspace) -> int:
    repo = next(r for r in workspace.repositories.list_all() if r.slug == MONO)
    assert repo.id is not None
    return repo.id


def _in_scope(path: str, prefix: str = SCOPE) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


# ---------------------------------------------------------------------------
# Each channel, at the layer where its bound is applied
# ---------------------------------------------------------------------------


def test_literal_search_finds_the_in_path_match_past_a_full_bound(indexed: Workspace) -> None:
    ids = [_repo_id(indexed)]
    unscoped = indexed.search.find_literal("IDENTITY_RESET_MARKER", ids, 30).hits
    assert len(unscoped) == 30
    assert all(not _in_scope(hit.path) for hit in unscoped), "the bound must be full of noise"

    scoped = indexed.search.find_literal("IDENTITY_RESET_MARKER", ids, 30, path_prefix=SCOPE).hits
    assert [hit.path for hit in scoped] == [IN_PATH]


def test_lexical_search_is_confined_before_its_limit(indexed: Workspace) -> None:
    ids = [_repo_id(indexed)]
    scoped = indexed.search.search_text("resetToken helper", ids, 5, path_prefix=SCOPE)
    assert scoped and {hit.path for hit in scoped} == {IN_PATH}


def test_exact_name_lookup_finds_the_in_path_declaration_past_a_full_bound(
    indexed: Workspace,
) -> None:
    ids = [_repo_id(indexed)]
    unscoped = indexed.search.find_entities_by_name(["resetToken"], ids, 30)
    assert len(unscoped) == 30 and all(not _in_scope(hit.path or "") for hit in unscoped)

    scoped = indexed.search.find_entities_by_name(["resetToken"], ids, 30, path_prefix=SCOPE)
    assert [hit.path for hit in scoped] == [IN_PATH]


def test_entity_full_text_search_is_confined(indexed: Workspace) -> None:
    ids = [_repo_id(indexed)]
    scoped = indexed.search.search_entities("resetToken", ids, None, 5, path_prefix=SCOPE)
    assert scoped and all(_in_scope(hit.path or "") for hit in scoped)


def test_file_identity_lookup_is_confined(indexed: Workspace) -> None:
    ids = [_repo_id(indexed)]
    unscoped = indexed.search.find_files_by_path(["reset.ts"], ids, 30)
    assert len(unscoped) == 30 and all(not _in_scope(hit.path) for hit in unscoped)

    scoped = indexed.search.find_files_by_path(["reset.ts"], ids, 30, path_prefix=SCOPE)
    assert [hit.path for hit in scoped] == [IN_PATH]


def test_described_entities_are_confined(indexed: Workspace) -> None:
    ids = [_repo_id(indexed)]
    everything = [
        hit.entity_id for hit in indexed.search.find_entities_by_name(["resetToken"], ids, 100)
    ]
    described = indexed.knowledge.describe_entities(everything, path_prefix=SCOPE)
    assert [row.path for row in described] == [IN_PATH]


@pytest.mark.parametrize(
    "near", ["services/identity-client/src/reset.ts", "services/identity0/src/reset.ts"]
)
def test_a_similarly_named_directory_is_not_inside_the_path(indexed: Workspace, near: str) -> None:
    ids = [_repo_id(indexed)]
    everywhere = {hit.path for hit in indexed.search.find_files_by_path(["reset.ts"], ids, 100)}
    assert near in everywhere
    scoped = {
        hit.path
        for hit in indexed.search.find_files_by_path(["reset.ts"], ids, 100, path_prefix=SCOPE)
    }
    assert near not in scoped


def test_an_exact_file_is_a_path_scope_of_one_file(indexed: Workspace) -> None:
    ids = [_repo_id(indexed)]
    scoped = indexed.search.find_literal("RESET_WINDOW_SECONDS", ids, 30, path_prefix=IN_PATH).hits
    assert [hit.path for hit in scoped] == [IN_PATH]
    assert indexed.search.has_files_under(ids, IN_PATH)
    assert not indexed.search.has_files_under(ids, "services/identity/src/reset")


# ---------------------------------------------------------------------------
# The capability contract
# ---------------------------------------------------------------------------


def _search(service: CapabilityService, question: str, **kwargs: object):
    return service.search(SearchRequest(question=question, context=REPO, **kwargs))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "prefix",
    ["services/identity", "services/identity/", "./services/identity", "services\\identity"],
)
def test_every_spelling_of_the_path_returns_only_evidence_beneath_it(
    indexed: Workspace, prefix: str
) -> None:
    service = CapabilityService(indexed)
    response = _search(
        service, "Where is resetToken defined and what does it read?", within_path=prefix
    )
    assert response.within_path == SCOPE
    assert response.evidence, "the identity file answers this question"
    assert all(_in_scope(item.ref.path) for item in response.evidence)
    assert any(item.ref.path == IN_PATH for item in response.evidence)


def test_without_a_path_the_noise_wins_and_with_it_the_identity_file_is_found(
    indexed: Workspace,
) -> None:
    """The observed failure, end to end: the bound is spent before the path is seen."""
    service = CapabilityService(indexed)
    unscoped = _search(service, "IDENTITY_RESET_MARKER")
    assert all(not _in_scope(item.ref.path) for item in unscoped.evidence)
    assert unscoped.within_path is None

    scoped = _search(service, "IDENTITY_RESET_MARKER", within_path=SCOPE)
    assert [item.ref.path for item in scoped.evidence][:1] == [IN_PATH]


def test_graph_expansion_does_not_carry_evidence_out_of_the_path(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    question = "ResetLedger"
    unscoped = _search(service, question)
    # The walk from the named class reaches the shared helper its file imports.
    reached = {item.ref.path for item in unscoped.evidence if item.match is MatchClass.RELATED}
    assert "packages/shared/src/util.ts" in reached

    scoped = _search(service, question, within_path=SCOPE)
    related = [item for item in scoped.evidence if item.match is MatchClass.RELATED]
    assert related, "expansion inside the path still happens"
    assert all(_in_scope(item.ref.path) for item in scoped.evidence)


def test_interned_handles_are_not_returned_under_a_path(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    unscoped = _search(service, "RESET_WINDOW_SECONDS")
    assert "env:RESET_WINDOW_SECONDS" in {handle.key for handle in unscoped.entities}

    scoped = _search(service, "RESET_WINDOW_SECONDS", within_path=SCOPE)
    assert "env:RESET_WINDOW_SECONDS" not in {handle.key for handle in scoped.entities}
    # The name is still found where it is written inside the path.
    assert any(item.ref.path == IN_PATH for item in scoped.evidence)


def test_a_term_absent_under_the_path_is_suppressed_as_absent_there(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    response = _search(service, "packages/shared/src/util.ts", within_path=SCOPE)
    assert response.suppressed is not None
    assert "under services/identity" in response.suppressed.message
    assert response.evidence == ()


def test_a_path_with_nothing_indexed_beneath_it_is_refused(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    before = len(service.sessions)
    with pytest.raises(FileNotIndexedError):
        _search(service, "resetToken", within_path="services/identiy")
    # A refused search leaves no investigation behind.
    assert len(service.sessions) == before


def test_a_path_scope_needs_a_repository_scope(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    with pytest.raises(ContextResolutionError):
        service.search(SearchRequest(question="resetToken", context=GLOBAL, within_path=SCOPE))


@pytest.mark.parametrize(
    "prefix",
    [
        "/services/identity",
        "\\services\\identity",
        "C:/repo/services",
        "../other",
        "services/../..",
        ".",
        "./",
        "",
    ],
)
def test_a_path_outside_the_repository_is_malformed(prefix: str) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(question="resetToken", context=REPO, within_path=prefix)


def test_without_a_path_the_search_is_unchanged(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    question = "Where is resetToken defined and what does it read?"
    omitted = _search(service, question)
    explicit_none = _search(service, question, within_path=None)
    assert [(i.ref, i.tier, i.match) for i in omitted.evidence] == [
        (i.ref, i.tier, i.match) for i in explicit_none.evidence
    ]
    assert omitted.within_path is None
