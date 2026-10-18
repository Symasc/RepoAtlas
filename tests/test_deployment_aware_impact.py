# ruff: noqa: E501 - the sample keys and configurations are spelled whole, as stored
"""DF3d: a configuration crossing's deployments go with the path (DF3D_PHASE_A.md §2, §7, §11).

The corpus (tests/deployment_aware_corpus.py) deploys one application as ``app-a`` and ``app-b``
(and its code in a compose service, a Tilt-bound dev chart and the two ``pair`` releases), a
second service ``svc`` calling ``app-a``, a package cycle ``app`` <-> ``ypkg``, and a consumer
calling every host.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import (
    ImpactCandidate,
    ImpactRequest,
    ImpactResponse,
    Presentation,
    SeedStatus,
)
from repoatlas.services.workspace import Workspace, open_workspace
from tests.deployment_aware_corpus import CLIENT, DEPLOY, repos
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, fresh_index, sync_all

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("deployment-aware")
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(root / "state/atlas.db")}}),
        require_root=False,
    )
    yield build(workspace, root, repos())
    workspace.close()


def key(workspace: Workspace, repository: str, path: str, qualified: str) -> str:
    [(found,)] = workspace.database.execute(
        """SELECT e.key FROM entities e JOIN files f ON f.id = e.file_id
           JOIN repositories r ON r.id = f.repo_id
           WHERE r.slug = ? AND f.rel_path = ? AND e.qualified_name = ?""",
        (repository, path, qualified),
    ).fetchall()
    return str(found)


def impact(workspace: Workspace, seed: str) -> ImpactResponse:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=seed, max_candidates=100, presentation=Presentation.FULL)
    )
    assert_paths_agree(response)
    return response


def listed(response: ImpactResponse) -> list[ImpactCandidate]:
    return [*response.candidates, *response.tests]


def callers(response: ImpactResponse, repository: str = "consumer") -> set[str]:
    return {c.name for c in listed(response) if c.repository == repository}


def skipped(response: ImpactResponse) -> dict[str, int]:
    return {k: v for k, v in response.omitted.by_reason.items() if k.startswith("deployment_")}


def chain_names(chain: str) -> list[str]:
    """The names a chain lists, its readings (which may hold " -> ") set aside."""
    parts, depth, start = [], 0, 0
    for i, char in enumerate(chain):
        depth += 1 if char == "(" else -1 if char == ")" else 0
        if depth == 0 and chain.startswith(" -> ", i):
            parts.append(chain[start:i])
            start = i + 4
    parts.append(chain[start:])
    return [part.split(" (", 1)[0] for part in parts]


def assert_paths_agree(response: ImpactResponse) -> None:
    """Every listed candidate's ``from`` trail is its own chain: no path borrows another's."""
    by_id = {c.id: c for c in listed(response)}

    def label(c: ImpactCandidate) -> str:
        names = chain_names(c.chain)
        return names[-1]

    for candidate in by_id.values():
        trail = [label(candidate)]
        at = candidate.from_
        while at:
            trail.append(label(by_id[at]))
            at = by_id[at].from_
        assert list(reversed(trail)) == chain_names(candidate.chain)[1:], candidate.chain


def releases(response: ImpactResponse, name: str) -> set[str | None]:
    return {
        c.step.http.release for c in listed(response) if c.name == name and c.step.http is not None
    }


# -- a release's own setting ----------------------------------------------------------------


def test_a_release_s_own_setting_reaches_its_own_callers_only(corpus: Workspace) -> None:
    """P1/F1, P2/F2: each values file's LIMIT, through its release's links alone."""
    a = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    assert {"call_a", "call_cycle_a", "call_either"} <= callers(a)
    assert not {
        "call_b",
        "call_cycle_b",
        "call_pair_a",
        "call_pair_b",
        "call_dev",
        "call_compose",
    } & callers(a)
    assert releases(a, "call_a") == {"app-a"}
    assert skipped(a)["deployment_inapplicable"] >= 1
    b = impact(corpus, key(corpus, "deployer", "env/b.yaml", "env.LIMIT"))
    assert {"call_b", "call_cycle_b", "call_either"} <= callers(b)
    assert not {"call_a", "call_cycle_a", "call_summary_b"} & callers(b)
    assert "fetch_item" not in callers(b, "svc")  # svc requests app-a
    assert releases(b, "call_either") == {"app-b"}


def test_a_set_on_one_release_and_a_default_shadowed_there_go_their_own_ways(
    corpus: Workspace,
) -> None:
    """P3/F3: ``--set env.REGION`` on app-a's step; P4/F4: the chart default, effective for
    app-b alone."""
    step = impact(
        corpus, key(corpus, "deployer", ".github/workflows/deploy.yml", "jobs.deploy.steps[1].run")
    )
    assert "call_a" in callers(step) and "call_b" not in callers(step)
    default = impact(corpus, key(corpus, "deployer", "charts/web/values.yaml", "env.REGION"))
    assert "call_b" in callers(default) and "call_a" not in callers(default)


def test_a_shared_setting_reaches_every_release_it_applies_to_and_names_them_all(
    corpus: Workspace,
) -> None:
    """P5 and R1: one ConfigMap key of both releases; its reading names both, and states no
    version relation it holds for one of them only."""
    response = impact(
        corpus, key(corpus, "deployer", "charts/web/templates/web-config.yaml", "data.SORT")
    )
    assert {"call_a", "call_b"} <= callers(response)
    assert not {"call_pair_a", "call_pair_b", "call_dev"} & callers(response)
    [crossing] = [c.step.configuration for c in listed(response) if c.step.configuration]
    assert crossing.deployments == ("app-a", "app-b")
    [reader] = [c for c in listed(response) if c.step.configuration is crossing]
    assert "releases app-a, app-b" in reader.chain
    assert "image version" not in reader.chain.split(" -> ")[1]


def test_shared_source_and_the_variable_itself_stay_unconstrained(corpus: Workspace) -> None:
    """P6, P7: code and the variable reach every linked deployment's callers."""
    for seed in (key(corpus, "app", "app/settings.py", "MAX"), "env:LIMIT"):
        response = impact(corpus, seed)
        assert {
            "call_a",
            "call_b",
            "call_dev",
            "call_pair_a",
            "call_pair_b",
            "call_compose",
        } <= callers(response)
        assert skipped(response) == {}
    variable = impact(corpus, "env:LIMIT")
    assert {"call_cycle_a", "call_cycle_b"} <= callers(variable)


def test_configuration_with_no_deployment_evidence_constrains_nothing(corpus: Workspace) -> None:
    """A Salt state's context sets REGION: a CONFIGURES edge with no deployment_env record is
    no deployment's crossing, so the path stays unconstrained and reaches every caller, as
    before DF3d."""
    response = impact(corpus, key(corpus, "app", "salt/app.sls", "app_config"))
    assert {"call_a", "call_b", "call_dev", "call_compose", "call_summary_b"} <= callers(response)
    assert skipped(response) == {}


def test_a_compose_setting_reaches_its_service_s_callers_never_a_release_s(
    corpus: Workspace,
) -> None:
    """F5: the compose service's CACHE reaches the caller of ``http://app:8080`` -- a link
    through that service, matched by its file and line -- and never a caller of any
    Kubernetes release of the same code."""
    response = impact(
        corpus, key(corpus, "app", "docker-compose.yml", "services.app.environment.CACHE")
    )
    assert callers(response) == {"call_compose"}
    [caller] = [c for c in listed(response) if c.name == "call_compose"]
    assert caller.step.http is not None
    assert caller.step.http.identity_source == "docker-compose-service"
    assert skipped(response)["deployment_inapplicable"] >= 1


def test_an_unestablished_deployment_never_matches_a_constrained_path(corpus: Workspace) -> None:
    """F6: the Tilt-bound dev link states no release: skipped under app-a's context, counted
    apart; reached from code."""
    constrained = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    assert "call_dev" not in callers(constrained)
    assert skipped(constrained)["deployment_unestablished"] >= 1
    assert "call_dev" in callers(impact(corpus, key(corpus, "app", "app/settings.py", "MAX")))


def test_a_request_matched_to_its_route_by_path_alone_states_no_deployment(
    corpus: Workspace,
) -> None:
    """app's own literal request of ``https://b.example/v1/limits`` is linked to get_limits
    through app-b, and matched to the route by its path, which names no deployment. Under
    app-a's context only the path match remains, skipped as unestablished; under app-b's the
    link is followed; unconstrained, the request is reached."""
    a = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    assert "get_limits" in callers(a, "app") and "smoke_b" not in callers(a, "app")
    b = impact(corpus, key(corpus, "deployer", "env/b.yaml", "env.LIMIT"))
    assert releases(b, "smoke_b") == {"app-b"}
    assert "smoke_b" in callers(impact(corpus, "env:LIMIT"), "app")


def test_skips_are_neither_candidates_refusals_nor_truncation(corpus: Workspace) -> None:
    """F7 and R3: counted under their own reasons only."""
    response = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    assert response.summary.complete is True
    assert set(response.omitted.by_reason) <= {
        "deployment_inapplicable",
        "deployment_unestablished",
    }
    assert response.omitted.by_unit == {}
    assert not [d for d in response.decisions if d.family == "http"]
    assert response.summary.policy == "impact/8"


# -- links, services and packages -----------------------------------------------------------


def test_a_caller_with_links_to_two_deployments_is_reached_through_the_matching_one(
    corpus: Workspace,
) -> None:
    a = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    b = impact(corpus, key(corpus, "deployer", "env/b.yaml", "env.LIMIT"))
    assert releases(a, "call_either") == {"app-a"}
    assert releases(b, "call_either") == {"app-b"}


def test_a_second_service_carries_the_change_on_with_its_whole_chain(corpus: Workspace) -> None:
    """The allowed HTTP round trip: app-a's setting -> svc (through app-a) -> app's own client
    of svc -> app's summary route -> its caller through app-b. Every hop is a link through
    the deployment it names."""
    response = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    [summary] = [c for c in listed(response) if c.name == "call_summary_b"]
    names = chain_names(summary.chain)
    assert names.index("fetch_item") < names.index("get_answer") < names.index("ask_svc")
    assert names.index("ask_svc") < names.index("get_summary") < names.index("call_summary_b")
    assert summary.step.http is not None and summary.step.http.release == "app-b"


def test_a_package_cycle_never_carries_a_setting_back_past_its_deployment(
    corpus: Workspace,
) -> None:
    """X -> Y -> X: app's LIMIT, used by ypkg's doubled, used by app's get_cycle: the context
    holds across both package crossings, so app-b's caller is never reached from app-a."""
    response = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    assert {"doubled", "get_cycle"} <= {c.name for c in listed(response)}
    assert "call_cycle_a" in callers(response)
    assert "call_cycle_b" not in callers(response)


def test_each_release_s_own_workload_decides_its_match(corpus: Workspace) -> None:
    """R4: pair-a selects the api workload, pair-b the admin one. A setting in both containers
    reaches both callers, each through its own workload; one in api's alone never pair-b's."""
    both = impact(corpus, key(corpus, "deployer", "charts/pair/values.yaml", "shared"))
    assert {"call_pair_a", "call_pair_b"} <= callers(both)
    api = impact(corpus, key(corpus, "deployer", "charts/pair/values.yaml", "apiOnly"))
    assert "call_pair_a" in callers(api)
    assert "call_pair_b" not in callers(api)
    assert skipped(api)["deployment_inapplicable"] >= 1


# -- several deployments meeting ------------------------------------------------------------


def test_paths_no_single_entity_path_represents_make_the_answer_ambiguous(
    corpus: Workspace,
) -> None:
    """``env/`` holds app-a's and app-b's LIMIT: their paths meet at the reader and the handler
    and part at the callers. The answer is ambiguous, names both setters -- real, indexed ones
    -- and lists no path."""
    response = CapabilityService(corpus).impact(
        ImpactRequest(context=GLOBAL, seed="deployer:env/", max_candidates=100)
    )
    assert response.seed.status is SeedStatus.AMBIGUOUS
    assert response.seed.reason == "several_deployments"
    suggested = {c.entity for c in response.seed.candidates}
    assert suggested == {
        key(corpus, "deployer", "env/a.yaml", "env.LIMIT"),
        key(corpus, "deployer", "env/b.yaml", "env.LIMIT"),
    }
    assert response.candidates == () and response.tests == ()
    for entity in suggested:
        assert impact(corpus, entity).seed.status is SeedStatus.RESOLVED


def test_paths_that_meet_and_never_part_are_no_ambiguity(corpus: Workspace) -> None:
    """``charts/web/`` holds the shared SORT (app-a and app-b) and REGION's default (app-b).
    They meet at get_item, and ask_svc (reading REGION) is reached under app-b's context and
    again, unconstrained, after the round trip through svc. Each entity is listed by its
    best-ranked path that every successor agrees with: ask_svc by its direct one."""
    response = impact(corpus, "deployer:charts/web/")
    assert response.seed.status is SeedStatus.RESOLVED
    assert {"call_a", "call_b", "call_summary_b"} <= callers(response)
    [ask] = [c for c in listed(response) if c.name == "ask_svc"]
    assert chain_names(ask.chain)[-2:] == ["REGION", "ask_svc"], ask.chain


def test_a_state_under_no_context_and_one_under_a_deployment_are_both_expanded(
    corpus: Workspace,
) -> None:
    """§7, context per state: app-a's ``--set`` reaches ask_svc under {app-a} directly, and
    unconstrained after the round trip through svc. The constrained state finds call_summary_b's
    link (app-b) inapplicable; the unconstrained one follows it. The caller is listed, and so is
    the path it needs -- ask_svc and get_summary by the round trip -- and the link followed on
    one path is not counted as skipped on the other: the five skipped are call_b's, call_pair_a's,
    call_pair_b's, call_compose's and call_either's app-b link; call_dev's is unestablished."""
    response = impact(
        corpus, key(corpus, "deployer", ".github/workflows/deploy.yml", "jobs.deploy.steps[1].run")
    )
    assert response.seed.status is SeedStatus.RESOLVED
    [summary] = [c for c in listed(response) if c.name == "call_summary_b"]
    names = chain_names(summary.chain)
    assert names.index("fetch_item") < names.index("ask_svc") < names.index("get_summary")
    assert skipped(response) == {"deployment_inapplicable": 5, "deployment_unestablished": 1}


def test_the_representative_does_not_depend_on_which_path_ranks_first(corpus: Workspace) -> None:
    """``charts/pair/``: ``apiOnly`` (pair-a) ranks before ``shared`` (pair-a and pair-b), so
    get_item and pair-a's caller are first reached under the narrower context. pair-b's caller
    hangs only off the shared one: its path is adopted for get_item, and pair-a's caller
    re-rooted through it, its own chain."""
    response = impact(corpus, "deployer:charts/pair/")
    assert response.seed.status is SeedStatus.RESOLVED
    assert {"call_pair_a", "call_pair_b"} <= callers(response)
    for candidate in listed(response):
        if candidate.name in ("get_item", "call_pair_a", "call_pair_b"):
            assert "SHARED" in chain_names(candidate.chain), candidate.chain


def test_a_co_setter_is_listed_as_setting_the_reader_never_followed(corpus: Workspace) -> None:
    """D3d-4: from app-a's LIMIT, the reader's other setter (app-b's) is a leaf that sets it."""
    response = impact(corpus, key(corpus, "deployer", "env/a.yaml", "env.LIMIT"))
    other = key(corpus, "deployer", "env/b.yaml", "env.LIMIT")
    [setter] = [c for c in listed(response) if c.entity == other]
    assert setter.role.value == "sets"


# -- incremental against fresh --------------------------------------------------------------


def _answers(workspace: Workspace) -> dict[str, object]:
    seeds = {
        "a": key(workspace, "deployer", "env/a.yaml", "env.LIMIT"),
        "b": key(workspace, "deployer", "env/b.yaml", "env.LIMIT"),
        "dir": "deployer:charts/web/",
    }
    found: dict[str, object] = {}
    for name, seed in seeds.items():
        data = (
            CapabilityService(workspace)
            .impact(ImpactRequest(context=GLOBAL, seed=seed, max_candidates=100))
            .model_dump(mode="json")
        )
        found[name] = {k: v for k, v in data.items() if k not in ("investigation_id", "freshness")}
    return found


def test_every_change_to_a_release_its_values_or_compose_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, repos())
    root = tmp_path / "repos"

    def step(tag: str) -> None:
        sync_all(built, root)
        assert_equals_fresh(built, settings, root, tmp_path, tag)
        with fresh_index(settings, root, tmp_path, f"{tag}-answers") as fresh:
            assert _answers(built) == _answers(fresh)

    # app-b's value changes; a third release appears with its own caller; compose loses CACHE
    edit(root, "deployer", "env/b.yaml", 'ingress:\n  host: b.example\nenv:\n  LIMIT: "31"\n')
    step("values")
    edit(root, "deployer", "env/c.yaml", 'ingress:\n  host: c.example\nenv:\n  LIMIT: "40"\n')
    edit(
        root,
        "deployer",
        ".github/workflows/deploy.yml",
        DEPLOY
        + "      - run: helm upgrade --install app-c charts/web -f charts/web/values.yaml -f env/c.yaml --set image.tag=${{ github.sha }}\n",
    )
    edit(
        root,
        "consumer",
        "client.py",
        CLIENT
        + '\n\ndef call_c(item_id: str) -> dict:\n    return requests.get(f"https://c.example/v1/items/{item_id}", timeout=5).json()\n',
    )
    step("release-added")
    c = impact(built, key(built, "deployer", "env/c.yaml", "env.LIMIT"))
    assert "call_c" in callers(c) and not {"call_a", "call_b"} & callers(c)
    edit(
        root,
        "app",
        "docker-compose.yml",
        "services:\n  app:\n    build:\n      context: .\n      dockerfile: deploy/app.Dockerfile\n",
    )
    step("compose-entry-removed")
