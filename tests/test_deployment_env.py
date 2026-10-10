# ruff: noqa: E501 - the expected keys and configurations are spelled whole, as stored
"""DF3a: where a deployment configures the variables its application reads (DF3A_PHASE_A.md
§3, §12), on indexed invented repositories (:mod:`tests.deployment_env_corpus`).

Each variable of the ``app`` release is one precedence case; the crossings are asserted exactly;
the change-impact walk crosses only on their records; ``seed_repository`` restricts the first
step and refuses what it cannot start from; and every change equals a fresh index.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.errors import InvalidImpactRequestError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.deployment_env import DEPLOYMENT_ENV
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactCandidate, ImpactRequest, ImpactResponse, ImpactRole
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot
from tests.deployment_env_corpus import COMPOSE, DEPLOY, repos
from tests.impact_corpus import build

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SETTINGS = "sym:builder:app/settings.py"
OVERLAY_SETTER = "sym:shipper:env/prod.yaml:env.VALUES_OVERLAY:2"
CHART_OVERLAY = "sym:shipper:charts/app/values.yaml:env.VALUES_OVERLAY:6"
STEP = "sym:shipper:.github/workflows/deploy.yml:jobs.deploy.steps[1].run:10"
STAGED_STEP = "sym:shipper:.github/workflows/staged.yml:jobs.deploy.steps[0].run:9"
VALUES = "sym:shipper:charts/app/values.yaml"
TEMPLATE = "charts/app/templates/deployment.yaml"
#: Words no configuration crossing may use: none of it is a value in effect (D3-4, F7).
WORDING = ("set to", "runtime value", "effective value", "is running", "currently deployed")


def decided(workspace: Workspace) -> dict[tuple[str, str, str], tuple[str, dict[str, Any]]]:
    """Each decision by (release or service, variant, variable)."""
    found: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
    for row in workspace.knowledge.list_link_decisions(family=DEPLOYMENT_ENV):
        evidence = json.loads(row.evidence_json)
        key = (
            str(evidence.get("release") or evidence.get("service")),
            str(evidence.get("variant", "")),
            str(evidence["variable"]),
        )
        assert key not in found
        found[key] = (row.decision, evidence)
    return found


def crossings(workspace: Workspace) -> dict[tuple[str, str], dict[str, Any]]:
    """Every CONFIGURES edge with a deployment_env record: (setter, reader) -> the record."""
    rows = workspace.database.execute(
        """
        SELECT s.key, d.key, e.resolution FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        WHERE e.type = 'CONFIGURES'
        """
    ).fetchall()
    return {
        (row[0], row[1]): json.loads(row[2])[DEPLOYMENT_ENV]
        for row in rows
        if row[2] and DEPLOYMENT_ENV in json.loads(row[2])
    }


def impact(workspace: Workspace, seed: str, **fields: Any) -> ImpactResponse:
    context = fields.pop("context", GLOBAL)
    return CapabilityService(workspace).impact(
        ImpactRequest(context=context, seed=seed, max_candidates=100, **fields)
    )


def by_entity(response: ImpactResponse) -> dict[str, ImpactCandidate]:
    return {c.entity: c for c in (*response.candidates, *response.tests)}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("deployment-env")
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(root / "state/atlas.db")}}),
        require_root=False,
    )
    yield build(workspace, root, repos())
    workspace.close()


# -- the setter of each variable --------------------------------------------------------

APP = {
    # variable: (decision, reason or setter kind, value, setter_at or entry_at)
    "VALUES_DEFAULT": ("linked", "values-file", "literal", "charts/app/values.yaml:5"),
    "VALUES_OVERLAY": ("linked", "values-file", "literal", "env/prod.yaml:2"),
    "SET_LITERAL": ("linked", "set", "literal", ".github/workflows/deploy.yml:10"),
    "SET_SYMBOLIC": ("linked", "set", "symbolic", ".github/workflows/deploy.yml:10"),
    "SET_SECRET": ("linked", "set", "withheld", ".github/workflows/deploy.yml:10"),
    "LITERAL_VALUE": ("setter-unresolved", "not-indexed", None, f"{TEMPLATE}:28"),
    "SECRET_REF": ("setter-unresolved", "secret", None, f"{TEMPLATE}:30"),
    "CM_REF": ("linked", "configmap-key", "literal", "charts/app/templates/app-config.yaml:6"),
    "CM_MISSING": ("setter-unresolved", "unread-source", None, f"{TEMPLATE}:40"),
    "DEFAULTED": ("setter-unresolved", "values-unresolved", None, f"{TEMPLATE}:45"),
    "DUPLICATE": ("setter-unresolved", "duplicate-entry", None, f"{TEMPLATE}:49"),
    "ABSENT": ("setter-unresolved", "not-indexed", None, f"{TEMPLATE}:51"),
    "FROM_EARLY": ("setter-unresolved", "unread-source", None, f"{TEMPLATE}:64"),
    "FROM_LAST": ("linked", "env-from", "literal", "charts/app/templates/late-config.yaml:6"),
    "FROM_DIRECT": ("linked", "values-file", "literal", "charts/app/values.yaml:11"),
    "ONLY_OUTSIDE": ("reader-outside-image", "values-file", "literal", "charts/app/values.yaml:9"),
    "OUTSIDE": ("linked", "values-file", "literal", "charts/app/values.yaml:10"),
    "STAGE": ("setter-unresolved", "not-indexed", None, f"{TEMPLATE}:59"),
}


@pytest.mark.parametrize("variable", sorted(APP))
def test_each_variable_is_decided_by_the_precedence_its_configuration_states(
    corpus: Workspace, variable: str
) -> None:
    decision, why, value, place = APP[variable]
    got, evidence = decided(corpus)[("app", "", variable)]
    assert got == decision
    if decision == "setter-unresolved":
        assert (evidence["reason"], evidence["entry_at"]) == (why, place)
        assert "setter_kind" not in evidence and "value" not in evidence
    else:
        assert (evidence["setter_kind"], evidence["value"], evidence["setter_at"]) == (
            why,
            value,
            place,
        )
    # DF2's artifact evidence on every one
    assert (evidence["deployed_by"], evidence["built_by"]) == ("shipper", "builder")
    assert (evidence["version_relation"], evidence["binding_basis"], evidence["build_basis"]) == (
        "unknown",
        "source",
        "source",
    )


def test_a_later_unreadable_source_refuses_an_earlier_one_and_names_it(corpus: Workspace) -> None:
    _, evidence = decided(corpus)[("app", "", "FROM_EARLY")]
    assert evidence["source"] == "outside-config"
    # the direct entry wins over every envFrom source, readable or not
    assert decided(corpus)[("app", "", "FROM_DIRECT")][1]["setter_kind"] == "values-file"


def test_a_secret_reference_stays_opaque_and_known(corpus: Workspace) -> None:
    _, direct = decided(corpus)[("app", "", "SECRET_REF")]
    assert (direct["reference_kind"], direct["reference"]) == ("secretKeyRef", "app-secrets/token")
    decision, listed = decided(corpus)[("worker", "", "WORKER_MODE")]
    assert decision == "setter-unresolved"
    assert (listed["reason"], listed["reference_kind"], listed["reference"]) == (
        "secret",
        "secretRef",
        "worker-secrets",
    )
    readers = {reader for _, reader in crossings(corpus)}
    assert f"{SETTINGS}:SECRET_REF:9" not in readers
    assert "sym:builder:worker/main.py:MODE:4" not in readers


def test_exclusion_from_an_image_is_claimed_only_where_its_contents_are_read(
    corpus: Workspace,
) -> None:
    outside, evidence = decided(corpus)[("app", "", "ONLY_OUTSIDE")]
    assert (outside, evidence["outside"]) == ("reader-outside-image", "tools/report.py:3")
    unread, evidence = decided(corpus)[("worker", "", "THREADS")]
    assert (unread, evidence["reason"]) == ("image-unread", "copy-unresolved")
    assert evidence["dockerfile"] == "deploy/worker.Dockerfile"
    # a reader in the image crosses; one outside it is listed, never crossed to
    linked, evidence = decided(corpus)[("app", "", "OUTSIDE")]
    assert (linked, evidence["reader_at"], evidence["outside"]) == (
        "linked",
        "app/settings.py:18",
        "tools/report.py:4",
    )
    readers = {reader for _, reader in crossings(corpus)}
    assert not any(reader.startswith("sym:builder:tools/") for reader in readers)
    assert not any(reader.startswith("sym:builder:worker/") for reader in readers)


def test_a_shadowed_setter_crosses_only_for_the_variant_it_is_effective_in(
    corpus: Workspace,
) -> None:
    # app reads env/prod.yaml over the chart's default; staged reads no overlay
    assert decided(corpus)[("app", "", "VALUES_OVERLAY")][1]["setter_at"] == "env/prod.yaml:2"
    for branch in ("main", "release"):
        staged = decided(corpus)[("staged", f"branch={branch}", "VALUES_OVERLAY")][1]
        assert staged["setter_at"] == "charts/app/values.yaml:6"
    found = crossings(corpus)
    reader = f"{SETTINGS}:VALUES_OVERLAY:4"
    assert "deployments" not in found[(OVERLAY_SETTER, reader)]
    assert found[(OVERLAY_SETTER, reader)]["release"] == "app"
    assert found[(CHART_OVERLAY, reader)]["deployments"] == [
        "staged#branch=main",
        "staged#branch=release",
    ]


def test_two_variants_of_one_release_share_one_setter_and_one_edge(corpus: Workspace) -> None:
    for branch in ("main", "release"):
        decision, evidence = decided(corpus)[("staged", f"branch={branch}", "STAGE")]
        assert (decision, evidence["setter_kind"], evidence["value"]) == (
            "linked",
            "set",
            "literal",
        )
    record = crossings(corpus)[(STAGED_STEP, f"{SETTINGS}:STAGE:19")]
    assert record["deployments"] == ["staged#branch=main", "staged#branch=release"]


def test_compose_entries_by_their_form(corpus: Workspace) -> None:
    got = {
        variable: (decision, evidence.get("reason") or evidence.get("value"))
        for (service, _, variable), (decision, evidence) in decided(corpus).items()
        if service in ("web", "jobs", "app")
        and variable.isupper()
        and variable in COMPOSE + "LEVEL"
    }
    assert got == {
        "PORT": ("linked", "literal"),
        "EMPTY": ("linked", "literal"),  # an explicit empty string is a value
        "PASSED": ("setter-unresolved", "passthrough"),  # a null mapping value
        "INTERP": ("linked", "symbolic"),  # interpolated: never resolved
        "JOB_LIMIT": ("linked", "literal"),
        "JOB_PASSED": ("setter-unresolved", "passthrough"),  # a bare list entry
        "JOB_TWICE": ("setter-unresolved", "duplicate-entry"),
        "LEVEL": ("setter-unresolved", "unread-source"),  # an override file may override it
    }
    assert decided(corpus)[("app", "", "LEVEL")][1]["override"] == "docker-compose.override.yml"
    # a list entry's setter is its service's environment key; its line, the entry's
    record = crossings(corpus)[
        (
            "sym:composer:docker-compose.yml:services.jobs.environment:13",
            "sym:composer:jobs/run.py:JOB_LIMIT:3",
        )
    ]
    assert (record["setter_at"], record["deployed_by"], record["built_by"]) == (
        "docker-compose.yml:14",
        "composer",
        "composer",
    )


def test_the_crossings_exactly(corpus: Workspace) -> None:
    def config(key: str, line: int) -> str:
        return f"{VALUES}:env.{key}:{line}"

    def reads(name: str, line: int) -> str:
        return f"{SETTINGS}:{name}:{line}"

    assert set(crossings(corpus)) == {
        (
            "sym:composer:docker-compose.yml:services.jobs.environment:13",
            "sym:composer:jobs/run.py:JOB_LIMIT:3",
        ),
        (
            "sym:composer:docker-compose.yml:services.web.environment.EMPTY:8",
            "sym:composer:web/main.py:EMPTY:4",
        ),
        (
            "sym:composer:docker-compose.yml:services.web.environment.INTERP:10",
            "sym:composer:web/main.py:INTERP:6",
        ),
        (
            "sym:composer:docker-compose.yml:services.web.environment.PORT:7",
            "sym:composer:web/main.py:PORT:3",
        ),
        ("sym:shipper:charts/app/templates/app-config.yaml:data.CM_REF:6", reads("CM_REF", 10)),
        (
            "sym:shipper:charts/app/templates/late-config.yaml:data.FROM_LAST:6",
            reads("FROM_LAST", 16),
        ),
        (config("FROM_DIRECT", 11), reads("FROM_DIRECT", 17)),
        (config("OUTSIDE", 10), reads("OUTSIDE", 18)),
        (config("VALUES_DEFAULT", 5), reads("VALUES_DEFAULT", 3)),
        (config("SET_LITERAL", 7), reads("SET_LITERAL", 5)),
        (CHART_OVERLAY, reads("VALUES_OVERLAY", 4)),
        (OVERLAY_SETTER, reads("VALUES_OVERLAY", 4)),
        (STEP, reads("SET_LITERAL", 5)),
        (STEP, reads("SET_SYMBOLIC", 6)),
        (STEP, reads("SET_SECRET", 7)),
        (STAGED_STEP, reads("STAGE", 19)),
    }


def test_nothing_crosses_through_a_variable_node_or_to_a_namesake(corpus: Workspace) -> None:
    for (setter, reader), record in crossings(corpus).items():
        assert not setter.startswith("env:") and not reader.startswith("env:")
        assert not reader.startswith("sym:namesake:") and not reader.startswith("file:namesake:")
        assert record["built_by"] and record["deployed_by"]
    # nothing of this pass writes to a variable's node
    written = corpus.database.execute(
        """
        SELECT COUNT(*) FROM edges e JOIN entities t ON t.id = e.dst_id OR t.id = e.src_id
        WHERE e.parser = 'linker:global' AND t.key LIKE 'env:%'
        """
    ).fetchone()[0]
    assert written == 0


def test_a_crossing_carries_the_artifact_join_and_its_decision(corpus: Workspace) -> None:
    record = crossings(corpus)[(OVERLAY_SETTER, f"{SETTINGS}:VALUES_OVERLAY:4")]
    assert {k: record[k] for k in ("release", "deployed_by", "built_by", "version_relation")} == {
        "release": "app",
        "deployed_by": "shipper",
        "built_by": "builder",
        "version_relation": "unknown",
    }
    assert (record["binding_basis"], record["build_basis"], record["value"]) == (
        "source",
        "source",
        "literal",
    )
    assert record["image"] == "registry.example:5000/team/app"
    assert record["reader_at"] == "app/settings.py:4"
    refs = {row.decision_ref for row in corpus.knowledge.list_link_decisions(family=DEPLOYMENT_ENV)}
    assert record["decision_ref"] in refs
    assert record["artifact_ref"] in {
        row.decision_ref
        for row in corpus.knowledge.list_link_decisions(family="container_artifact")
    }


def test_no_value_is_ever_stored(corpus: Workspace) -> None:
    text = json.dumps(
        [
            json.loads(r.evidence_json)
            for r in corpus.knowledge.list_link_decisions(family=DEPLOYMENT_ENV)
        ]
    ) + json.dumps(list(crossings(corpus).values()))
    for value in ('"20"', '"json"', "APP_TOKEN", "8000", '"debug"', '"fast"'):
        assert value not in text


# -- the change-impact walk ----------------------------------------------------------------


def test_a_setter_reaches_its_reader_and_the_code_using_it_across_repositories(
    corpus: Workspace,
) -> None:
    reached = by_entity(impact(corpus, OVERLAY_SETTER))
    reader = reached[f"{SETTINGS}:VALUES_OVERLAY:4"]
    assert reader.step.edge == "CONFIGURES" and reader.role is ImpactRole.DEPENDENT
    configuration = reader.step.configuration
    assert configuration is not None
    assert (configuration.setter_kind, configuration.release, configuration.value) == (
        "values-file",
        "app",
        "literal",
    )
    assert (configuration.deployment_repository, configuration.builder_repository) == (
        "shipper",
        "builder",
    )
    assert configuration.setter_at == "shipper/env/prod.yaml:2"
    assert configuration.reader_at == "builder/app/settings.py:4"
    assert "shipper -> builder via VALUES_OVERLAY" in reader.chain
    assert "sym:builder:app/main.py:list_items:9" in reached
    assert not any(key.startswith(("sym:namesake:", "file:namesake:")) for key in reached)
    # the chart's default is shadowed for app; it sets the same reader for staged, so it is
    # listed as what else sets it -- a leaf, read with its own release -- never as a dependent
    other = reached[CHART_OVERLAY]
    assert other.role is ImpactRole.SETS and other.step.configuration is not None
    assert other.step.configuration.deployments == ("staged#branch=main", "staged#branch=release")


def test_a_reader_reaches_each_effective_setter_as_a_leaf(corpus: Workspace) -> None:
    reached = by_entity(impact(corpus, f"{SETTINGS}:VALUES_OVERLAY:4"))
    for setter in (OVERLAY_SETTER, CHART_OVERLAY):
        assert reached[setter].role is ImpactRole.SETS
    assert not any(key.startswith(("sym:namesake:", "file:namesake:")) for key in reached)


def test_a_configures_edge_without_its_record_never_crosses(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    database = workspace.database
    setter = f"{VALUES}:env.VALUES_DEFAULT:5"
    planted = "sym:namesake:app/settings.py:OVERLAY:3"
    [(src, repo_id, file_id)] = database.execute(
        "SELECT id, repo_id, file_id FROM entities WHERE key = ?", (setter,)
    ).fetchall()
    [(dst,)] = database.execute("SELECT id FROM entities WHERE key = ?", (planted,)).fetchall()
    # as Salt's and a project's CONFIGURES edges are: no deployment_env record
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO edges (src_id, dst_id, type, confidence, repo_id, file_id,"
            " line_start, line_end, parser, resolution)"
            " VALUES (?, ?, 'CONFIGURES', 1.0, ?, ?, 5, 5, 'linker:global', NULL)",
            (src, dst, repo_id, file_id),
        )
    reached = by_entity(impact(workspace, setter))
    assert planted not in reached
    assert f"{SETTINGS}:VALUES_DEFAULT:3" in reached  # the recorded crossing still is


def test_a_setter_reached_from_a_reader_is_a_leaf(corpus: Workspace) -> None:
    reached = by_entity(impact(corpus, f"{SETTINGS}:SET_LITERAL:5"))
    assert reached[STEP].role is ImpactRole.SETS
    # the step sets other variables too; none of their readers depends on this one
    assert f"{SETTINGS}:SET_SYMBOLIC:6" not in reached
    assert f"{SETTINGS}:SET_SECRET:7" not in reached


def test_a_symbolic_value_is_never_presented_as_a_value(corpus: Workspace) -> None:
    reached = by_entity(impact(corpus, STEP))
    step = reached[f"{SETTINGS}:SET_SYMBOLIC:6"]
    assert step.step.configuration is not None and step.step.configuration.value == "symbolic"
    assert "value from a variable, not established" in step.chain
    withheld = reached[f"{SETTINGS}:SET_SECRET:7"]
    assert "value withheld" in withheld.chain
    text = json.dumps([c.model_dump(mode="json") for c in reached.values()]).lower()
    for phrase in WORDING:
        assert phrase not in text


def test_a_refused_setter_crosses_nothing_and_says_why_where_the_walk_goes(
    corpus: Workspace,
) -> None:
    response = impact(corpus, f"{SETTINGS}:SECRET_REF:9")
    assert not any(c.entity.startswith("sym:shipper:") for c in response.candidates)
    reasons = {(d.family, d.decision, d.evidence.get("reason")) for d in response.decisions}
    assert (DEPLOYMENT_ENV, "setter-unresolved", "secret") in reasons


# -- seed_repository (D3-1) ----------------------------------------------------------------


def test_an_unscoped_variable_seed_still_reaches_every_reader_through_the_shared_node(
    corpus: Workspace,
) -> None:
    reached = by_entity(impact(corpus, "env:VALUES_OVERLAY"))
    assert f"{SETTINGS}:VALUES_OVERLAY:4" in reached
    assert "sym:namesake:app/settings.py:OVERLAY:3" in reached


def test_a_source_specific_seed_starts_from_that_repository_alone(corpus: Workspace) -> None:
    reached = by_entity(impact(corpus, "env:VALUES_OVERLAY", seed_repository="builder"))
    assert f"{SETTINGS}:VALUES_OVERLAY:4" in reached
    assert reached[OVERLAY_SETTER].role is ImpactRole.SETS
    assert not any(key.startswith(("sym:namesake:", "file:namesake:")) for key in reached)


def test_a_source_specific_seed_never_returns_through_the_shared_node(corpus: Workspace) -> None:
    reached = by_entity(impact(corpus, "env:VALUES_OVERLAY", seed_repository="namesake"))
    assert set(reached) == {"sym:namesake:app/settings.py:OVERLAY:3"}


def test_a_repository_scope_stays_a_boundary(corpus: Workspace) -> None:
    builder = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="builder")
    scoped = impact(corpus, "env:VALUES_OVERLAY", context=builder)
    named = impact(corpus, "env:VALUES_OVERLAY", context=builder, seed_repository="builder")
    assert set(by_entity(named)) == set(by_entity(scoped))
    assert all(c.repository in (None, "builder") for c in named.candidates)  # or shared


@pytest.mark.parametrize(
    ("seed", "repository", "context", "refusal"),
    [
        ("env:VALUES_OVERLAY", "composer", GLOBAL, "no file or declaration in composer reads"),
        (
            "env:VALUES_OVERLAY",
            "namesake",
            CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="builder"),
            "outside this call's scope",
        ),
        ("env:VALUES_OVERLAY", "nowhere", GLOBAL, "outside this call's scope"),
        (f"{SETTINGS}:VALUES_OVERLAY:4", "builder", GLOBAL, "only to an environment variable"),
    ],
)
def test_a_source_it_cannot_start_from_is_refused(
    corpus: Workspace, seed: str, repository: str, context: CapabilityContext, refusal: str
) -> None:
    with pytest.raises(InvalidImpactRequestError, match=refusal) as raised:
        impact(corpus, seed, seed_repository=repository, context=context)
    assert raised.value.field == "seed_repository"


def test_a_blank_seed_repository_is_malformed() -> None:
    with pytest.raises(ValueError, match="seed_repository must name a repository"):
        ImpactRequest(context=GLOBAL, seed="env:X", seed_repository=" ")


# -- incremental equals fresh ----------------------------------------------------------------


def state(workspace: Workspace) -> tuple[object, ...]:
    decisions = workspace.database.execute(
        "SELECT family, subject, decision, evidence_json FROM link_decisions ORDER BY family, subject"
    ).fetchall()
    return (fact_snapshot(workspace), [tuple(r) for r in decisions])


def fresh(settings: Settings, root: Path, tmp_path: Path, tag: str) -> tuple[object, ...]:
    from repoatlas.services.init_service import initialize

    other = open_workspace(
        settings=settings.model_copy(
            update={
                "storage": settings.storage.model_copy(
                    update={"database": tmp_path / tag / "atlas.db"}
                )
            }
        ),
        require_root=False,
    )
    try:
        initialize(other, root, config_path=tmp_path / f"{tag}-config.yaml")
        return state(other)
    finally:
        other.close()


def test_every_change_to_a_setter_a_source_or_an_image_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, repos())
    root = tmp_path / "repos"

    def step(repo: str, tag: str) -> None:
        sync(workspace, workspace.resolver().resolve(root / repo))
        assert state(workspace) == fresh(settings, root, tmp_path, tag), tag

    shipper, builder, composer = root / "shipper", root / "builder", root / "composer"
    # the overlay drops its key: the chart's default becomes app's setter
    (shipper / "env/prod.yaml").write_text("env: {}\n")
    step("shipper", "overlay")
    assert (
        decided(workspace)[("app", "", "VALUES_OVERLAY")][1]["setter_at"]
        == "charts/app/values.yaml:6"
    )
    # a --set removed: the values file sets it
    (shipper / ".github/workflows/deploy.yml").write_text(
        DEPLOY.replace(" --set env.SET_LITERAL=on", "")
    )
    step("shipper", "set")
    assert (
        decided(workspace)[("app", "", "SET_LITERAL")][1]["setter_at"] == "charts/app/values.yaml:7"
    )
    # a ConfigMap loses its key: the unreadable source before it may set it
    (shipper / "charts/app/templates/late-config.yaml").write_text(
        'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: late-config\ndata:\n  OTHER: "v"\n'
    )
    step("shipper", "configmap")
    assert decided(workspace)[("app", "", "FROM_LAST")][1]["reason"] == "unread-source"
    # a compose entry turned pass-through
    (composer / "docker-compose.yml").write_text(COMPOSE.replace('PORT: "8000"', "PORT:"))
    step("composer", "passthrough")
    assert decided(workspace)[("web", "", "PORT")][1]["reason"] == "passthrough"
    # the image narrowed to one file: the settings' readers are now outside it, established
    (builder / "deploy/app.Dockerfile").write_text(
        'FROM python:3.12-slim\nWORKDIR /app\nCOPY app/main.py /app/app/main.py\nCMD ["uvicorn", "app.main:app"]\n'
    )
    step("builder", "image")
    assert decided(workspace)[("app", "", "VALUES_DEFAULT")][0] == "reader-outside-image"
    # a reader moved to another declaration
    (builder / "app/settings.py").write_text(
        (builder / "app/settings.py").read_text().replace("VALUES_DEFAULT = ", "DEFAULT = ")
    )
    shutil.copy(builder / "app/settings.py", builder / "app/copied.py")
    step("builder", "reader")
