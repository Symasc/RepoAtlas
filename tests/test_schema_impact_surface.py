"""What a change-impact answer says about schema and persistence links, and about links not made.

The guarantees pinned here, on the invented fixtures of G2-G5:

* every step joining a schema definition to code names what it rests on, as a typed field:
  a migration, a generated client, a query, a typed mapping, a typed comparison (and, from
  earlier milestones, a generation or a composition);
* ``decisions`` lists the links refused or qualified where the walk went -- decided at the
  seed, a returned candidate, or in a file one of them is in -- never a link made, never one
  decided where the walk did not go; past its bound the rest are counted by family;
* an argument's container is what it is an argument of.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities import impact as impact_module
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace
from tests.document_corpus import repos as document_repos
from tests.impact_corpus import build, verify_steps
from tests.persistence_corpus import repos as persistence_repos

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _impact(workspace: Workspace, seed: str, **extra: Any) -> Any:
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed=seed, presentation=Presentation.FULL, **extra)
    )
    assert response.seed.status == "resolved"
    verify_steps(workspace, response)
    return response


def _by_name(response: Any) -> dict[str, Any]:
    return {c.entity.rsplit(":", 1)[0].split(":", 3)[3]: c for c in response.candidates}


STATE = "sym:depot-api:prisma/schema.prisma:ParcelState:11"


def test_each_persistence_step_names_what_it_rests_on(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, persistence_repos())

    found = _by_name(_impact(workspace, STATE))
    packed = found["packedParcels"].step.query
    assert (packed.database, packed.table, packed.column, packed.op) == (
        "depot-api/prisma/schema.prisma",
        "parcels",
        "state_code",
        "=",
    )
    assert packed.decision_ref
    client = found["toState"].step.client
    assert (client.project, client.specifier) == (
        "depot-api/prisma/schema.prisma",
        "@prisma/client",
    )
    assert "through @prisma/client" in found["toState"].chain
    migration = found["parcel_state"].step.migration
    assert (migration.project, migration.type) == ("depot-api/prisma/schema.prisma", "parcel_state")
    assert migration.migration.startswith("depot-api/prisma/migrations/")
    # a plain reference carries none of them
    plain = found["Parcel.state"].step
    assert (plain.query, plain.client, plain.migration, plain.mapping) == (None, None, None, None)


def test_mapping_and_comparison_steps_name_their_evidence(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, persistence_repos())

    lost = _by_name(_impact(workspace, "sym:depot-api:src/gen/types.ts:Stage.Lost:4"))
    mapping = lost["ParcelState.lost"].step.mapping
    assert mapping.mapping == "depot-api/src/mapping.ts:7"
    assert lost["ParcelState.lost"].chain.endswith("by depot-api/src/mapping.ts:7)")
    compared = _by_name(_impact(workspace, "sym:depot-ops:ops/stages.py:Stage.LOST:6"))
    comparison = compared["lost"].step.comparison
    assert (comparison.field, comparison.value) == ("state", "LOST")
    assert comparison.model.startswith("depot-ops/ops/models.py:")
    assert comparison.enum == "depot-ops/ops/stages.py:4"


def test_decisions_list_the_links_not_made_where_the_walk_went(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, persistence_repos())

    response = _impact(workspace, STATE)
    listed = {(d.subject, d.decision) for d in response.decisions}
    q = "depot-api:src/queries.ts::"
    # queries.ts holds candidates, so what was refused beside them is listed
    assert {
        (f"{q}inState", "sql-dynamic"),
        (f"{q}shadowed", "receiver-unproven"),
        (f"{q}legacySent", "database-identity-unproven"),
        (f"{q}byColour", "column-unknown"),
    } <= listed
    # never a link made, and never a decision where the walk did not go
    assert not [d for d in response.decisions if d.decision in {"linked", "resolved"}]
    assert not [s for s, _ in listed if s.startswith("depot-ops:")]
    shadowed = next(d for d in response.decisions if d.subject == f"{q}shadowed")
    assert shadowed.family == "persistence"
    assert shadowed.at == "depot-api/src/queries.ts:30"
    assert shadowed.evidence["queries"][0]["receiver"] == "db"
    order = [(d.family, d.subject) for d in response.decisions]
    assert order == sorted(order)
    assert response.decisions_omitted == {}


def test_a_resolver_bound_is_a_link_made_and_never_listed(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, document_repos())
    bound = {
        d.subject
        for d in workspace.knowledge.list_link_decisions(family="graphql_resolvers")
        if d.decision == "resolved"
    }
    assert "depot-api:schema/depot.graphql::Query.parcels" in bound

    response = _impact(workspace, "sym:depot-api:schema/depot.graphql:Stage:1")
    listed = {(d.subject, d.decision) for d in response.decisions}
    # the walk reached the file that binds the resolvers: its refusals are listed ...
    assert ("depot-api:schema/depot.graphql::Parcel.id", "default-resolver") in listed
    # ... and its bindings, links made, are not
    assert not [s for s, _ in listed if s in bound]


def test_decisions_past_the_bound_are_counted_by_family(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, persistence_repos())
    everything = _impact(workspace, STATE).decisions
    monkeypatch.setattr(impact_module, "MAX_DECISIONS", 2)

    response = _impact(workspace, STATE)
    assert response.decisions == everything[:2]
    left = everything[2:]
    counted: dict[str, int] = {}
    for decision in left:
        counted[decision.family] = counted.get(decision.family, 0) + 1
    assert response.decisions_omitted == counted


def test_a_compact_answer_keeps_its_decisions(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, persistence_repos())
    compact = CapabilityService(workspace).impact(ImpactRequest(context=GLOBAL, seed=STATE))
    assert compact.decisions == _impact(workspace, STATE).decisions


def test_an_argument_s_container_is_its_field(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, document_repos())

    response = _impact(workspace, "sym:depot-api:schema/depot.graphql:Stage:1")
    found = _by_name(response)
    field = found["Query.parcels"]
    before = next(c for c in response.candidates if c.id == field.from_)
    assert before.entity.rsplit(":", 1)[0].endswith(":Query.parcels(stage:)")
    assert field.step.derived == "member-of"
    # the argument's container is the field, not the type that holds the field
    query = found.get("Query")
    assert query is None or query.from_ != before.id
