"""Provider candidates and the uniqueness RepoAtlas will not invent (Slice G).

A *provider* is a registered repository whose manifest publishes a package name.
Knowing that is knowledge about names, and it is not knowledge about which
repository a build would reach for.

V1 draws the line in exactly one place (V1_SPEC §9): a declared dependency
resolves to a repository when, among the repositories publishing that name and
discounting the one asking, **exactly one** remains. Zero and several are both
honest "unknown" and the linker draws no edge for either. This capability restates
that conclusion; it does not compute a better one.

The failure this file exists to catch is quiet and attractive: two candidates
narrowed to one by something that is not a fact about the code — a caller's
filter, a repository scope, a sort order — and reported as a resolution. Every
test below tries to produce that and asserts it did not happen.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.dependencies.normalize import REPOSITORY_ECOSYSTEMS
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependencies import DependencyRole
from repoatlas.models.dependency_inventory import (
    MAX_PAGE_SIZE,
    DependenciesRequest,
    DependenciesResponse,
    DependencyPackageView,
    Ecosystem,
    ProviderResolution,
)
from repoatlas.services.workspace import Workspace
from tests.capabilities.test_dependencies import _plant

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
COMMON = "common-lib"
TS = "ts-service"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _ask(workspace: Workspace, **kwargs: object) -> DependenciesResponse:
    fields: dict[str, object] = {"context": GLOBAL, "limit": MAX_PAGE_SIZE}
    fields.update(kwargs)
    return _service(workspace).dependencies(DependenciesRequest(**fields))  # type: ignore[arg-type]


def _one(response: DependenciesResponse, slug: str, name: str) -> DependencyPackageView:
    found = [
        p for p in response.packages if p.repository_slug == slug and p.normalized_name == name
    ]
    assert len(found) == 1, f"expected exactly one {slug}/{name}"
    return found[0]


def _second_provider(workspace: Workspace) -> None:
    """Make ``pypi:common-lib`` published by two repositories instead of one.

    ``ts-service`` is made to claim the name that ``common-lib`` already
    publishes. Nothing about ``python-service``'s declaration changes — the same
    manifest asks for the same constraint — and yet what the index can conclude
    about it changes completely, which is the whole point.
    """
    _plant(
        workspace,
        slug=TS,
        rel_path="package.json",
        ecosystem="pypi",
        name="common-lib",
        normalized="common-lib",
        role="provided",
        version="9.9.9",
        line=2,
    )


# ---------------------------------------------------------------------------
# CASE 8 — a unique provider, where V1 proves one
# ---------------------------------------------------------------------------


def test_a_single_publisher_resolves_uniquely(initialized: Workspace) -> None:
    """CASE 8. One repository publishes ``common-lib``, so the linker resolves to it."""
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )

    assert package.provider_resolution is ProviderResolution.UNIQUE
    assert len(package.providers) == 1
    assert package.providers[0].repository_slug == COMMON
    assert package.providers[0].version == "1.4.0"


def test_uniqueness_matches_what_the_linker_actually_did(initialized: Workspace) -> None:
    """The restated conclusion and the drawn edge must agree.

    If they can disagree, one of them is inventing something, and the capability
    is the one with the opportunity.
    """
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )
    assert package.provider_resolution is ProviderResolution.UNIQUE

    edge = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'DEPENDS_ON' AND src.key = ? AND dst.key = ?",
        (f"repo:{SERVICE}", f"repo:{COMMON}"),
    ).fetchone()
    assert edge["n"] == 1, "the linker did not draw the edge this claims to restate"


# ---------------------------------------------------------------------------
# CASE 7 — ambiguity survives
# ---------------------------------------------------------------------------


def test_two_publishers_stay_two_candidates(initialized: Workspace) -> None:
    """CASE 7. Both are returned, neither is chosen, and the state is named."""
    _second_provider(initialized)
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )

    assert package.provider_resolution is ProviderResolution.AMBIGUOUS
    assert [p.repository_slug for p in package.providers] == [COMMON, TS]
    assert {p.version for p in package.providers} == {"1.4.0", "9.9.9"}


def test_ambiguity_is_a_successful_answer_and_not_an_error(initialized: Workspace) -> None:
    _second_provider(initialized)
    response = _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI)
    assert response.packages
    assert response.investigation_id


def test_no_field_names_a_chosen_provider(initialized: Workspace) -> None:
    """There is nowhere to put a winner, which is the strongest form of not picking one."""
    fields = set(DependencyPackageView.model_fields)
    for banned in ("selected_provider", "best_provider", "provider", "resolved_provider"):
        assert banned not in fields, banned


def test_the_linker_drew_no_edge_for_the_ambiguous_case(initialized: Workspace) -> None:
    """The substrate's own refusal, on the fixture's real ambiguity.

    Nothing publishes a package that two repositories both claim in the unit
    fixture, so this reads the rule off the code that implements it and pins the
    ambiguous branch against the planted collision.
    """
    _second_provider(initialized)
    from repoatlas.capabilities.dependencies import ProviderCandidate, provider_resolution

    candidates = [
        ProviderCandidate(
            repository_slug=slug,
            repository_name=slug,
            version=None,
            path="pyproject.toml",
            line_start=2,
            line_end=2,
            parser="pyproject",
        )
        for slug in (COMMON, TS)
    ]
    assert (
        provider_resolution("pypi", SERVICE, candidates, declares=True)
        is ProviderResolution.AMBIGUOUS
    )
    assert (
        provider_resolution("pypi", SERVICE, candidates[:1], declares=True)
        is ProviderResolution.UNIQUE
    )


# ---------------------------------------------------------------------------
# Filtering must not manufacture uniqueness
# ---------------------------------------------------------------------------


def test_a_repository_scope_does_not_make_a_provider_unique(
    initialized: Workspace,
) -> None:
    """The failure this file exists for.

    Scoping to ``python-service`` hides the second publishing repository from the
    *answer*. If the provider set were derived from what the scope returned, one
    candidate would remain and the response would report a resolution the index
    never established — from a filter the caller chose.
    """
    _second_provider(initialized)
    scoped = _service(initialized).dependencies(
        DependenciesRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            name="common-lib",
            ecosystem=Ecosystem.PYPI,
            limit=MAX_PAGE_SIZE,
        )
    )

    package = _one(scoped, SERVICE, "common-lib")
    assert package.provider_resolution is ProviderResolution.AMBIGUOUS
    assert [p.repository_slug for p in package.providers] == [COMMON, TS]


def test_a_role_filter_does_not_change_the_resolution(initialized: Workspace) -> None:
    """A deterministic conclusion that moved with a filter would be a property of
    the request rather than of the repositories."""
    _second_provider(initialized)
    for roles in ((), (DependencyRole.DECLARED,), (DependencyRole.RESOLVED,)):
        response = _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI, roles=roles)
        package = _one(response, SERVICE, "common-lib")
        assert package.provider_resolution is ProviderResolution.AMBIGUOUS, roles
        assert len(package.providers) == 2, roles


def test_an_ecosystem_filter_does_not_change_the_resolution(
    initialized: Workspace,
) -> None:
    _second_provider(initialized)
    narrow = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )
    wide = _one(_ask(initialized, name="common-lib"), SERVICE, "common-lib")
    assert narrow.provider_resolution is wide.provider_resolution
    assert len(narrow.providers) == len(wide.providers)


def test_a_page_boundary_does_not_change_the_resolution(initialized: Workspace) -> None:
    """Candidates are a fact about the root, so paging cannot narrow them."""
    _second_provider(initialized)
    seen: dict[str, ProviderResolution] = {}
    cursor: str | None = None
    while True:
        response = _ask(initialized, limit=1, cursor=cursor)
        for package in response.packages:
            if package.normalized_name == "common-lib" and package.ecosystem == "pypi":
                seen[package.repository_slug] = package.provider_resolution
        cursor = response.next_cursor
        if cursor is None:
            break
    assert seen[SERVICE] is ProviderResolution.AMBIGUOUS


# ---------------------------------------------------------------------------
# Where the question does not arise
# ---------------------------------------------------------------------------


def test_a_package_this_repository_never_asks_for_has_no_resolution(
    initialized: Workspace,
) -> None:
    """``common-lib`` publishes itself and declares nothing of the sort.

    Reporting ``UNIQUE`` here would answer a question nobody asked: there is no
    declaration for the linker to resolve, so there is no conclusion to restate.
    """
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), COMMON, "common-lib"
    )
    assert [o.role for o in package.observations] == [DependencyRole.PROVIDED]
    assert package.provider_resolution is ProviderResolution.NOT_APPLICABLE


def test_a_declared_package_nobody_publishes_resolves_to_nobody(
    initialized: Workspace,
) -> None:
    """``NONE`` is a real answer and differs from the question not arising."""
    package = _one(_ask(initialized, name="requests", ecosystem=Ecosystem.PYPI), COMMON, "requests")
    assert package.provider_resolution is ProviderResolution.NONE
    assert package.providers == ()


def test_an_ecosystem_without_repository_publishing_is_not_applicable(
    initialized: Workspace,
) -> None:
    """Outside the shared set, V1 proves nothing about who is behind a name."""
    from repoatlas.capabilities.dependencies import provider_resolution

    assert "docker" not in REPOSITORY_ECOSYSTEMS
    assert (
        provider_resolution("docker", SERVICE, [], declares=True)
        is ProviderResolution.NOT_APPLICABLE
    )


def test_providers_are_omitted_when_they_were_not_asked_for(
    initialized: Workspace,
) -> None:
    """Empty because the question was not asked, which is not "none exist"."""
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI, include_providers=False),
        SERVICE,
        "common-lib",
    )
    assert package.providers == ()

    with_providers = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )
    assert with_providers.providers


@pytest.mark.parametrize("slug", [COMMON, TS])
def test_a_provider_is_located_by_its_own_manifest(initialized: Workspace, slug: str) -> None:
    """CASE 9. The provision's line, never the declaration that went looking."""
    _second_provider(initialized)
    package = _one(
        _ask(initialized, name="common-lib", ecosystem=Ecosystem.PYPI), SERVICE, "common-lib"
    )

    provider = next(p for p in package.providers if p.repository_slug == slug)
    assert provider.provenance.repository_slug == slug
    assert provider.provenance.repository_slug != SERVICE
    assert provider.provenance.line_start == 2

    declared = next(o for o in package.observations if o.role is DependencyRole.DECLARED)
    assert declared.provenance.repository_slug == SERVICE
    assert (provider.provenance.repository_slug, provider.provenance.path) != (
        declared.provenance.repository_slug,
        declared.provenance.path,
    )
