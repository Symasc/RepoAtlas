"""Every family the impact walk follows is proven by a fixture, in its stored direction (G).

A row of the propagation table is admitted only with a case here: the edge as the index
stores it (source and target), the direction a change travels along it, and the role the
far end plays. A family with no case fails :func:`test_every_admitted_family_has_a_case`,
so an edge type cannot be added to the walk on the strength of its name. For a family the
walk follows one way only, walking back from the dependent must not reach the seed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.impact import DERIVED_READINGS, FAMILIES, MEMBER, MEMBER_OF
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactCandidate, ImpactRequest, ImpactResponse, ImpactRole
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import DIRECTION, HTTP_API, HTTP_WEB, build, verify_steps
from tests.schema_corpus import API as CODEGEN_API

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: Two enums and a typed mapping between them (repoatlas.graph.value_mappings).
VALUE_MAP = {
    "package.json": '{"name": "value-map", "private": true}',
    "src/stages.ts": (
        "export enum Stage {\n"  # 1
        "  Packed = 'PACKED',\n"  # 2
        "  Sent = 'SENT',\n"  # 3
        "}\n"  # 4
        "export enum Column {\n"  # 5
        "  packed = 'packed',\n"  # 6
        "  sent = 'sent',\n"  # 7
        "}\n"  # 8
        "export const toColumn: Record<Stage, Column> = {\n"  # 9
        "  [Stage.Packed]: Column.packed,\n"  # 10
        "  [Stage.Sent]: Column.sent,\n"  # 11
        "};\n"  # 12
    ),
}
D = "sym:direction:"


@dataclass(frozen=True)
class Case:
    seed: str
    reached: str
    role: ImpactRole
    #: The stored edge (type, source key, target key); ``None`` for a derived step.
    stored: tuple[str, str, str] | None
    #: The family is followed one way only: from ``reached``, no step of this family
    #: reaches the seed.
    one_way: bool = True


CASES: dict[str, Case] = {
    "uses": Case(
        f"{D}src/a.ts:A:1",
        f"{D}src/b.ts:useA:2",
        ImpactRole.DEPENDENT,
        ("USES", f"{D}src/b.ts:useA:2", f"{D}src/a.ts:A:1"),
    ),
    "references": Case(
        f"{D}src/base.ts:Base:1",
        f"{D}src/sub.ts:Sub:2",
        ImpactRole.DEPENDENT,
        ("REFERENCES", f"{D}src/sub.ts:Sub:2", f"{D}src/base.ts:Base:1"),
    ),
    "calls": Case(
        f"{D}src/calls.ts:callee:1",
        f"{D}src/calls.ts:caller:4",
        ImpactRole.DEPENDENT,
        ("CALLS", f"{D}src/calls.ts:caller:4", f"{D}src/calls.ts:callee:1"),
    ),
    "reads": Case(
        "env:G_KEY",
        f"{D}src/env.ts:readsEnv:1",
        ImpactRole.DEPENDENT,
        ("READS", f"{D}src/env.ts:readsEnv:1", "env:G_KEY"),
    ),
    "configures": Case(
        f"{D}salt/app.sls:app_config:1",
        "env:G_KEY",
        ImpactRole.DEPENDENT,
        ("CONFIGURES", f"{D}salt/app.sls:app_config:1", "env:G_KEY"),
        one_way=False,
    ),
    "configures-setter": Case(
        "env:G_KEY",
        f"{D}salt/app.sls:app_config:1",
        ImpactRole.SETS,
        ("CONFIGURES", f"{D}salt/app.sls:app_config:1", "env:G_KEY"),
        one_way=False,
    ),
    "exports-env": Case(
        f"{D}Dockerfile:app:1",
        "env:G_ARG",
        ImpactRole.DEPENDENT,
        ("EXPORTS", f"{D}Dockerfile:app:1", "env:G_ARG"),
        one_way=False,
    ),
    "exports-env-setter": Case(
        "env:G_ARG",
        f"{D}Dockerfile:app:1",
        ImpactRole.SETS,
        ("EXPORTS", f"{D}Dockerfile:app:1", "env:G_ARG"),
        one_way=False,
    ),
    "serves": Case(
        "file:direction:src/server.ts",
        "http-route:GET:/v1/items",
        ImpactRole.DEPENDENT,
        ("SERVES_HTTP_ROUTE", "file:direction:src/server.ts", "http-route:GET:/v1/items"),
        one_way=False,
    ),
    "served-by": Case(
        "http-route:GET:/v1/items",
        "file:direction:src/server.ts",
        ImpactRole.DEPENDENT,
        ("SERVES_HTTP_ROUTE", "file:direction:src/server.ts", "http-route:GET:/v1/items"),
        one_way=False,
    ),
    "requests": Case(
        "http-route:GET:/v1/items",
        f"{D}src/client.ts:loadItems:1",
        ImpactRole.DEPENDENT,
        ("CALLS_HTTP_ROUTE", f"{D}src/client.ts:loadItems:1", "http-route:GET:/v1/items"),
    ),
    "http": Case(
        "sym:http-api:app.py:get_item:9",
        "sym:http-web:client.py:load_item:4",
        ImpactRole.DEPENDENT,
        (
            "CALLS_HTTP_HANDLER",
            "sym:http-web:client.py:load_item:4",
            "sym:http-api:app.py:get_item:9",
        ),
    ),
    "imports": Case(
        "file:direction:src/a.ts",
        "file:direction:src/b.ts",
        ImpactRole.DEPENDENT,
        ("IMPORTS", "file:direction:src/b.ts", "file:direction:src/a.ts"),
    ),
    "generates": Case(
        "sym:parcel-api:schema/parcels.graphql:Stage:1",
        "sym:parcel-api:src/gen/types.ts:Stage:2",
        ImpactRole.DEPENDENT,
        (
            "GENERATES",
            "sym:parcel-api:schema/parcels.graphql:Stage:1",
            "sym:parcel-api:src/gen/types.ts:Stage:2",
        ),
    ),
    "maps-to": Case(
        "sym:value-map:src/stages.ts:Stage.Packed:2",
        "sym:value-map:src/stages.ts:Column.packed:6",
        ImpactRole.DEPENDENT,
        (
            "MAPS_TO",
            "sym:value-map:src/stages.ts:Stage.Packed:2",
            "sym:value-map:src/stages.ts:Column.packed:6",
        ),
    ),
    MEMBER: Case("file:direction:src/a.ts", f"{D}src/a.ts:A:1", ImpactRole.MEMBER, None),
    MEMBER_OF: Case(f"{D}src/a.ts:A:1", f"{D}src/holder.ts:Holder:2", ImpactRole.DEPENDENT, None),
}


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("direction")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    build(
        workspace,
        root,
        {
            "direction": DIRECTION,
            "http-api": HTTP_API,
            "http-web": HTTP_WEB,
            "parcel-api": CODEGEN_API,
            "value-map": VALUE_MAP,
        },
    )
    yield workspace
    workspace.close()


def impact(workspace: Workspace, seed: str) -> ImpactResponse:
    request = ImpactRequest(context=GLOBAL, seed=seed, presentation=Presentation.FULL)
    response = CapabilityService(workspace).impact(request)
    verify_steps(workspace, response)
    return response


def found(response: ImpactResponse, key: str) -> ImpactCandidate | None:
    return next((c for c in response.candidates if c.entity == key), None)


def test_every_admitted_family_has_a_case() -> None:
    assert set(CASES) == {f.name for f in FAMILIES} | {MEMBER, MEMBER_OF}


@pytest.mark.parametrize("family", sorted(CASES))
def test_each_family_travels_in_its_stored_direction(indexed: Workspace, family: str) -> None:
    case = CASES[family]
    response = impact(indexed, case.seed)
    candidate = found(response, case.reached)
    assert candidate is not None, [c.entity for c in response.candidates]
    assert candidate.role is case.role
    table = {f.name: f for f in FAMILIES}
    if family in table:
        assert candidate.step.edge is table[family].edge_type
        assert candidate.step.reading == table[family].reading
        assert case.stored is not None
        kind, source, target = case.stored
        [row] = indexed.database.execute(
            "SELECT COUNT(*) FROM edges e JOIN entities s ON s.id = e.src_id "
            "JOIN entities d ON d.id = e.dst_id WHERE e.type = ? AND s.key = ? AND d.key = ?",
            (kind, source, target),
        ).fetchall()
        assert row[0] >= 1, f"{family}: the index stores no {kind} {source} -> {target}"
    else:
        assert candidate.step.derived == family
        assert candidate.step.reading == DERIVED_READINGS[family]
        assert candidate.step.edge is None
    if case.one_way and family in table:
        # Walking back from the dependent, the seed may be reached through another
        # family, never through this one run backwards.
        back = found(impact(indexed, case.reached), case.seed)
        assert back is None or back.step.edge is not table[family].edge_type, (
            f"{family} was followed against its direction"
        )


def test_a_member_s_container_carries_the_change_to_its_own_users(indexed: Workspace) -> None:
    response = impact(indexed, f"{D}src/a.ts:A:1")
    run = found(response, f"{D}src/holder.ts:Holder.run:3")
    holder = found(response, f"{D}src/holder.ts:Holder:2")
    held = found(response, f"{D}src/holder.ts:held:7")
    assert run and holder and held
    assert (holder.from_, held.from_) == (run.id, holder.id)
    assert held.chain == "A -> Holder.run (uses) -> Holder (contains) -> held (uses)"


def test_a_handler_change_reaches_the_requests_through_the_route(indexed: Workspace) -> None:
    response = impact(indexed, f"{D}src/server.ts:listItems:3")
    caller = found(response, f"{D}src/client.ts:loadItems:1")
    assert caller is not None
    assert caller.chain == (
        "listItems -> src/server.ts (uses) -> http-route:GET:/v1/items (is served by) "
        "-> loadItems (requests)"
    )
