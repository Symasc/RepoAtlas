"""Package-level dependents: coarse, evidenced, never joined by a name alone (fixture E).

A change inside a package may reach whatever declares that package. That is coarse by
construction -- it names a unit or a repository, never the declaration that uses the
change -- so it is its own section, one package hop from the seed, labelled as such:
``exact_consumers`` counts the exact candidates found inside the dependent, or says
``unresolved``. Within a repository, a declaration counts only when no other manifest
there provides the same name; across repositories, only when the linker drew its
repository edge, which it does only when exactly one managed repository provides it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, ImpactResponse, PackageImpact
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import APP, LIB, MONO, TWIN_ONE, TWIN_TWO, TWIN_USER, build

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("packages")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    repos = {
        "mono": MONO,
        "lib": LIB,
        "app": APP,
        "twin-one": TWIN_ONE,
        "twin-two": TWIN_TWO,
        "twin-user": TWIN_USER,
    }
    build(workspace, root, repos)
    yield workspace
    workspace.close()


def impact(workspace: Workspace, seed: str) -> ImpactResponse:
    return CapabilityService(workspace).impact(ImpactRequest(context=GLOBAL, seed=seed))


def test_a_unit_declaring_the_package_in_its_own_repository_is_listed(
    indexed: Workspace,
) -> None:
    response = impact(indexed, "sym:mono:packages/core/src/index.ts:CORE:1")
    assert response.packages == (
        PackageImpact(
            package="npm:@m/core",
            provided_by="mono/packages/core/package.json:1",
            dependent="mono:apps/web",
            declared_at="mono/apps/web/package.json:1",
            crosses_repository=False,
            exact_consumers=1,
        ),
    )
    # the exact consumer it counts is an exact candidate, listed as one
    [consumer] = response.candidates
    assert (consumer.entity, consumer.unit) == (
        "sym:mono:apps/web/src/use.ts:shown:2",
        "mono:apps/web",
    )


def test_another_repository_is_listed_only_on_the_linker_s_evidence(indexed: Workspace) -> None:
    response = impact(indexed, "sym:lib:src/index.ts:formatDate:1")
    assert response.packages == (
        PackageImpact(
            package="npm:@acme/lib",
            provided_by="lib/package.json:1",
            dependent="app:.",
            declared_at="app/package.json:1",
            crosses_repository=True,
            exact_consumers=1,
            consumers=(1,),
        ),
    )
    # the package link names the exact consumer across repositories (it was left
    # unresolved, and unclaimed, until a use could follow the package into ``lib``)
    [consumer] = response.candidates
    assert consumer.id == 1
    assert (consumer.entity, consumer.repository, consumer.step.edge) == (
        "sym:app:src/view.ts:view:2",
        "app",
        "USES",
    )


def test_a_name_two_repositories_provide_joins_neither(indexed: Workspace) -> None:
    for seed in ("sym:twin-one:src/index.ts:one:1", "sym:twin-two:src/index.ts:two:1"):
        assert impact(indexed, seed).packages == ()


def test_a_name_two_units_of_one_repository_provide_is_not_resolved_to_either(
    indexed: Workspace,
) -> None:
    assert impact(indexed, "sym:mono:packages/dup-one/src/index.ts:ONE:1").packages == ()


def test_a_shared_seed_has_no_package(indexed: Workspace) -> None:
    response = impact(indexed, "file:app:src/view.ts")
    assert response.packages == ()  # app provides no package
