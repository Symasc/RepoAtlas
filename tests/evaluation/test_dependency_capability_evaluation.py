"""The deterministic dependency-capability evaluation corpus (V2 Slice G, §34).

Separate from the retrieval corpus because the two measure incompatible things.
Retrieval is scored on recall over a ranked, partly associative result: the right
answer is somewhere near the top, and "close" is a grade it can earn. A dependency
answer is exact. The correct result is a specific set of observations carrying
specific values, and an extra one is not a near miss — it is a statement RepoAtlas
made about code that does not contain it.

So every case is closed on both sides: the returned observations must equal the
expected ones, value for value and line for line. The corpus lives in
``dependency_cases.yaml`` and its ground truth was written from the fixture
manifests, never from a run.

The properties checked across the whole corpus are the ones §34 names: no invented
package, no invented version, no invented unique provider, no raw ids, citable
evidence only where source text was supplied, and stable ordering.

No model is consulted and nothing is embedded.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependencies import DependencyRole
from repoatlas.models.dependency_inventory import (
    MAX_PAGE_SIZE,
    DependenciesRequest,
    DependenciesResponse,
    Ecosystem,
    ProviderResolution,
)
from repoatlas.services.workspace import Workspace
from tests.evaluation.harness import evaluation_workspace

CASES_PATH = Path(__file__).parent / "dependency_cases.yaml"

#: ``role, value, path, line`` — one stored observation as the corpus writes it.
Observation = tuple[str, str | None, str, int]

#: ``slug, version, path, line`` — one provider candidate as the corpus writes it.
Provider = tuple[str, str | None, str, int]


@dataclasses.dataclass(frozen=True)
class DependencyCase:
    """One inventory question with an exactly known answer."""

    id: str
    category: str
    context_repo: str
    ecosystem: str | None = None
    name: str | None = None
    roles: tuple[str, ...] = ()
    include_providers: bool = True
    expected: dict[str, tuple[Observation, ...]] | None = None
    expected_providers: dict[str, tuple[Provider, ...]] = dataclasses.field(default_factory=dict)
    expected_resolution: dict[str, str] = dataclasses.field(default_factory=dict)
    forbidden_packages: tuple[str, ...] = ()
    expected_evidence: tuple[tuple[str, str], ...] = ()
    paginate: tuple[int, ...] = ()
    notes: str = ""

    @property
    def is_global(self) -> bool:
        return self.context_repo == "__root__"


def load_dependency_cases() -> tuple[DependencyCase, ...]:
    raw: dict[str, Any] = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases: list[DependencyCase] = []
    for entry in raw["cases"]:
        fields = dict(entry)
        expected = fields.pop("expected", None)
        cases.append(
            DependencyCase(
                expected=(
                    {
                        key: tuple((r, v, p, int(line)) for r, v, p, line in observations)
                        for key, observations in expected.items()
                    }
                    if expected is not None
                    else None
                ),
                expected_providers={
                    key: tuple((s, v, p, int(line)) for s, v, p, line in providers)
                    for key, providers in fields.pop("expected_providers", {}).items()
                },
                expected_resolution=dict(fields.pop("expected_resolution", {})),
                forbidden_packages=tuple(fields.pop("forbidden_packages", ())),
                expected_evidence=tuple(
                    (str(item["location"]), str(item["text"]))
                    for item in fields.pop("expected_evidence", ())
                ),
                roles=tuple(fields.pop("roles", ())),
                paginate=tuple(int(size) for size in fields.pop("paginate", ())),
                **fields,
            )
        )
    return tuple(cases)


CASES = load_dependency_cases()
CONTENT_CASES = tuple(case for case in CASES if case.expected is not None)


@pytest.fixture(scope="module")
def dependency_workspace() -> Iterator[Workspace]:
    with evaluation_workspace() as (workspace, _root):
        yield workspace


def _request(case: DependencyCase, **overrides: object) -> DependenciesRequest:
    fields: dict[str, object] = {
        "context": (
            CapabilityContext(scope=CapabilityScope.GLOBAL)
            if case.is_global
            else CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=case.context_repo)
        ),
        "ecosystem": Ecosystem(case.ecosystem) if case.ecosystem else None,
        "name": case.name,
        "roles": tuple(DependencyRole(role) for role in case.roles),
        "include_providers": case.include_providers,
        "limit": MAX_PAGE_SIZE,
    }
    fields.update(overrides)
    return DependenciesRequest(**fields)  # type: ignore[arg-type]


def _run(
    workspace: Workspace, case: DependencyCase, **overrides: object
) -> tuple[CapabilityService, DependenciesResponse]:
    service = CapabilityService(workspace)
    return service, service.dependencies(_request(case, **overrides))


def _observed(response: DependenciesResponse) -> dict[str, tuple[Observation, ...]]:
    return {
        f"{p.repository_slug}/{p.ecosystem}/{p.normalized_name}": tuple(
            (
                str(o.role),
                o.value,
                o.provenance.path,
                o.provenance.line_start,
            )
            for o in p.observations
        )
        for p in response.packages
    }


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CONTENT_CASES, ids=[c.id for c in CONTENT_CASES])
def test_the_returned_observations_are_exactly_the_expected_ones(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """No invented package, no invented version, and nothing lost.

    Stated as equality rather than containment because both failures matter and
    they are different bugs: a missing observation loses a statement a file makes,
    and an extra one attributes a statement to a file that does not make it.
    """
    _service, response = _run(dependency_workspace, case)
    assert case.expected is not None

    observed = _observed(response)
    assert set(observed) - set(case.expected) == set(), f"{case.id}: unexpected packages"
    assert set(case.expected) - set(observed) == set(), f"{case.id}: missing packages"
    for key, expected in case.expected.items():
        assert observed[key] == expected, f"{case.id}: {key}"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_no_forbidden_package_appears(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """Mostly a scoping assertion: the same package elsewhere must stay elsewhere."""
    _service, response = _run(dependency_workspace, case)
    observed = set(_observed(response))
    for forbidden in case.forbidden_packages:
        assert forbidden not in observed, f"{case.id}: {forbidden}"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_declarations_and_locks_are_never_read_as_each_other(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """The distinction, across the whole corpus.

    A constraint copied into ``locked_version`` would be the single most useful
    lie this capability could tell: a caller reading it would believe the index
    knows what a resolver chose, when all it read was a request.
    """
    _service, response = _run(dependency_workspace, case)
    for package in response.packages:
        for observation in package.observations:
            if observation.role is DependencyRole.DECLARED:
                assert observation.declared_constraint == observation.value
                assert observation.locked_version is None, case.id
                assert observation.provided_version is None, case.id
            elif observation.role is DependencyRole.RESOLVED:
                assert observation.locked_version == observation.value
                assert observation.declared_constraint is None, case.id
            else:
                assert observation.provided_version == observation.value
                assert observation.declared_constraint is None, case.id
                assert observation.locked_version is None, case.id


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_provider_candidates_and_their_resolution_are_exact(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """No invented unique provider, and no candidate quietly dropped."""
    _service, response = _run(dependency_workspace, case)
    packages = {
        f"{p.repository_slug}/{p.ecosystem}/{p.normalized_name}": p for p in response.packages
    }

    for key, expected in case.expected_providers.items():
        package = packages[key]
        observed = tuple(
            (
                provider.repository_slug,
                provider.version,
                provider.provenance.path,
                provider.provenance.line_start,
            )
            for provider in package.providers
        )
        assert observed == expected, f"{case.id}: {key}"

    for key, expected_resolution in case.expected_resolution.items():
        assert packages[key].provider_resolution is ProviderResolution(expected_resolution), (
            f"{case.id}: {key}"
        )


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_a_resolution_is_never_created_by_a_filter(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """Uniqueness belongs to the index, so widening the request cannot remove it.

    Each case is re-run without its narrowing filters. Every package that appears
    in both answers must carry the same provider set and the same conclusion — if
    the narrow view found a resolution the wide one does not, the filter made it.
    """
    if not case.include_providers:
        pytest.skip("this case does not ask for providers")
    _service, narrow = _run(dependency_workspace, case)
    _wide_service, wide = _run(
        dependency_workspace,
        case,
        context=CapabilityContext(scope=CapabilityScope.GLOBAL),
        name=None,
        ecosystem=None,
        roles=(),
    )

    wide_packages = {
        f"{p.repository_slug}/{p.ecosystem}/{p.normalized_name}": p for p in wide.packages
    }
    for package in narrow.packages:
        key = f"{package.repository_slug}/{package.ecosystem}/{package.normalized_name}"
        counterpart = wide_packages[key]
        assert package.provider_resolution is counterpart.provider_resolution, f"{case.id}: {key}"
        assert [p.repository_slug for p in package.providers] == [
            p.repository_slug for p in counterpart.providers
        ], f"{case.id}: {key}"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_citable_evidence_exists_only_where_source_text_was_supplied(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """``citable`` means recorded, not "has coordinates"."""
    service, response = _run(dependency_workspace, case)
    ledger = service.sessions.get(response.investigation_id).ledger
    held = {entry.id for entry in ledger.entries()}

    for package in response.packages:
        for source in (*package.observations, *package.providers):
            assert source.provenance.citable is bool(source.evidence_ids), case.id
            for evidence_id in source.evidence_ids:
                assert evidence_id in held, case.id

    supplied = {(item.location, item.text) for item in response.evidence}
    for location, text in case.expected_evidence:
        assert (location, text) in supplied, f"{case.id}: {location} not supplied verbatim"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_no_storage_identifier_reaches_the_answer(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """Row ids are renumbered by a reindex; natural identities are not."""
    _service, response = _run(dependency_workspace, case)
    payload = json.loads(response.model_dump_json())

    def field_names(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(str(key))
                found |= field_names(item)
        elif isinstance(value, list):
            for item in value:
                found |= field_names(item)
        return found

    names = field_names(payload)
    for banned in ("dependency_id", "file_id", "repo_id", "entity_id", "rowid", "score"):
        assert banned not in names, f"{case.id}: {banned}"


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_the_order_is_stable_and_documented(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    _service, response = _run(dependency_workspace, case)
    keys = [(p.repository_slug, p.ecosystem, p.normalized_name) for p in response.packages]
    assert keys == sorted(keys), case.id
    assert len(keys) == len(set(keys)), case.id


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_every_answer_is_reproducible(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """Same index, same question, same bytes. Twice."""
    _first_service, first = _run(dependency_workspace, case)
    _second_service, second = _run(dependency_workspace, case)

    fields = {"investigation_id", "ledger", "freshness"}
    assert first.model_dump_json(exclude=fields) == second.model_dump_json(exclude=fields), case.id


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_every_page_size_reproduces_the_whole_answer(
    dependency_workspace: Workspace, case: DependencyCase
) -> None:
    """The paging case, and a spot check on every other one."""
    sizes = case.paginate or (2,)
    service = CapabilityService(dependency_workspace)
    whole = [
        (p.repository_slug, p.ecosystem, p.normalized_name)
        for p in service.dependencies(_request(case, include_provenance=False)).packages
    ]

    for size in sizes:
        walked: list[tuple[str, str, str]] = []
        cursor: str | None = None
        while True:
            response = service.dependencies(
                _request(case, limit=size, cursor=cursor, include_provenance=False)
            )
            walked += [
                (p.repository_slug, p.ecosystem, p.normalized_name) for p in response.packages
            ]
            cursor = response.next_cursor
            if cursor is None:
                break
        assert walked == whole, f"{case.id}: page size {size}"
        assert len(walked) == len(set(walked)), f"{case.id}: page size {size}"


def test_the_corpus_covers_every_required_area() -> None:
    """§34 names the areas; this fails if one is quietly dropped."""
    assert {case.category for case in CASES} == {
        "declaration",
        "lock",
        "multiplicity",
        "scoping",
        "ecosystem",
        "role",
        "provider",
        "provenance",
        "zero-result",
        "pagination",
    }


def test_every_expected_observation_is_a_stored_row(
    dependency_workspace: Workspace,
) -> None:
    """The corpus itself is checked against the index, in the safe direction.

    This does not derive expectations from RepoAtlas — it asserts that
    hand-written ground truth names rows the indexer actually produced, so a
    corpus typo fails as a corpus problem rather than as a capability one.
    """
    stored = {
        (row["slug"], row["ecosystem"], row["normalized_name"], row["role"], row["line_start"])
        for row in dependency_workspace.database.execute(
            "SELECT r.slug, d.ecosystem, d.normalized_name, d.role, d.line_start "
            "FROM dependencies d JOIN repositories r ON r.id = d.repo_id"
        ).fetchall()
    }
    for case in CASES:
        for key, observations in (case.expected or {}).items():
            slug, ecosystem, name = key.split("/", 2)
            for role, _value, _path, line in observations:
                assert (slug, ecosystem, name, role, line) in stored, f"{case.id}: {key} {role}"


def test_no_case_asks_the_index_to_be_searched(dependency_workspace: Workspace) -> None:
    """The whole corpus, with retrieval made fatal."""
    from repoatlas.retrieval.retriever import Retriever

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("a dependency case reached retrieval")

    original = (Retriever.retrieve, Retriever.retrieve_detailed)
    Retriever.retrieve = forbidden  # type: ignore[method-assign, assignment]
    Retriever.retrieve_detailed = forbidden  # type: ignore[method-assign, assignment]
    try:
        for case in CASES:
            _run(dependency_workspace, case)
    finally:
        Retriever.retrieve, Retriever.retrieve_detailed = original  # type: ignore[method-assign]
