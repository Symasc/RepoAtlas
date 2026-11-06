"""R1 preview diagnostics: hints for a declaration that would join nothing (gate G4).

A preview that says "no request of this group matches a route this service serves" is true, and
unhelpful when the service declares the very route the callers request and nothing proves its
mount. With ``include_diagnostics`` the preview also says how many served routes the declaration
could expose, how many have no declaration line (F1), and which declared or unpublished routes of
the target the unmatched requests would have matched: hints, never edges or decisions.

These tests were ``xfail(strict=True)`` until the gate landed (G4) and are plain tests now. The
unchanged halves of cases 12-15 are pinned in ``test_r1_characterization.py`` and
``tests/data/r1_golden.json``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.http_connections import HttpConnectionPreviewRequest
from repoatlas.models.http_inventory import OperationStatus
from repoatlas.services.workspace import Workspace
from tests.r1_provider_corpus import (
    GLOBAL,
    NEST_ORIGIN,
    ORDERS_ORIGIN,
    Corpora,
    caller_decisions,
    group_key,
    inventory,
    preview,
    slug_of,
)

PUBLISHED = {OperationStatus.CALLED, OperationStatus.NO_INDEXED_CALLER}
NOTHING_JOINED = (
    "no request of this group matches a route this service serves: nothing would be joined"
)


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-preview"))
    yield built
    built.close()


def decision_ref_of(workspace: Workspace, caller: str) -> str:
    """The stored decision of the request a function makes."""
    [row] = workspace.database.execute(
        "SELECT d.decision_ref FROM http_call_decisions d JOIN entities e ON e.id = d.src_id "
        "WHERE e.qualified_name = ?",
        (caller,),
    ).fetchall()
    return row[0]


def written(response: Any, drop: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {k: v for k, v in json.loads(response.model_dump_json()).items() if k not in drop}


# -- case 12: a caller with a declared-but-unserved matching provider ----------------------------


def test_case_12_the_preview_still_joins_nothing_and_now_names_the_declared_route(
    corpora: Corpora,
) -> None:
    py = corpora.workspace("py")
    result = preview(py, ORDERS_ORIGIN, "pyprov", include_diagnostics=True)
    # unchanged
    assert result.routes_served == 2
    assert result.counts == {"no-matching-route": 2}
    assert result.requirements == (NOTHING_JOINED,)
    assert result.can_create is False
    # new: /health states its line; /v1/ping, served through a prefix, states none (F1)
    hints = result.provider_diagnostics
    assert hints is not None
    assert hints.routes_exposable == 1
    assert hints.routes_without_declaration_line == 1
    assert hints.exposures_truncated is False
    assert hints.unpublished_match_count == 1
    [match] = hints.unpublished_matches
    assert match.decision_ref == decision_ref_of(py, "fetch_order")
    assert match.route == "GET /orders/{oid}"
    assert match.status == "declared"
    assert match.path == "svc/orders.py"
    assert match.code == "python-not-mounted"
    assert hints.lifecycle.recognized == 8


def test_case_13_a_request_nothing_declares_gets_no_hint(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    hints = preview(py, ORDERS_ORIGIN, "pyprov", include_diagnostics=True).provider_diagnostics
    assert hints is not None
    hinted = {match.decision_ref for match in hints.unpublished_matches}
    assert decision_ref_of(py, "fetch_nothing") not in hinted
    assert decision_ref_of(py, "fetch_order") in hinted


def test_the_flag_changes_nothing_of_a_preview_but_the_field_it_adds(corpora: Corpora) -> None:
    """P7, M17: requirements, warnings, rows, counts and can_create are the flag-off answer."""
    py = corpora.workspace("py")
    for name in ("pyprov", "twinprov", "emptyprov"):
        plain = preview(py, ORDERS_ORIGIN, name)
        diagnosed = preview(py, ORDERS_ORIGIN, name, include_diagnostics=True)
        assert diagnosed.provider_diagnostics is not None, name
        assert written(diagnosed, frozenset({"provider_diagnostics"})) == written(plain), name


# -- case 14: several possible providers ---------------------------------------------------------


def test_case_14_two_declared_providers_of_one_shape_are_both_listed_and_none_is_ambiguous(
    corpora: Corpora,
) -> None:
    py = corpora.workspace("py")
    before = caller_decisions(py)
    result = preview(py, ORDERS_ORIGIN, "twinprov", include_diagnostics=True)
    assert result.counts == {"no-matching-route": 2}
    assert result.requirements == (NOTHING_JOINED,)
    assert result.can_create is False
    hints = result.provider_diagnostics
    assert hints is not None
    assert hints.unpublished_match_count == 2
    assert {match.path for match in hints.unpublished_matches} == {"tw/a.py", "tw/b.py"}
    assert {match.route for match in hints.unpublished_matches} == {
        "GET /orders/{oid}",
        "GET /orders/{order_id}",
    }
    # a hint is never a decision: nothing became ambiguous, nothing was stored
    assert caller_decisions(py) == before


def test_the_hints_of_one_repository_never_name_another_repository_s_files(
    corpora: Corpora,
) -> None:
    """The misattribution guard: ``pyprov`` and ``twinprov`` both declare ``/orders/...``."""
    py = corpora.workspace("py")
    twin = preview(py, ORDERS_ORIGIN, "twinprov", include_diagnostics=True)
    prov = preview(py, ORDERS_ORIGIN, "pyprov", include_diagnostics=True)
    twin_hints, prov_hints = twin.provider_diagnostics, prov.provider_diagnostics
    assert twin_hints is not None and prov_hints is not None
    assert all((m.path or "").startswith("tw/") for m in twin_hints.unpublished_matches)
    assert all((m.path or "").startswith("svc/") for m in prov_hints.unpublished_matches)


# -- case 15: a missing or unindexed provider ----------------------------------------------------


def test_case_15_a_repository_that_is_not_indexed_is_still_refused(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    with pytest.raises(InvalidDeploymentLinkError, match="not indexed"):
        CapabilityService(py).http_connection_preview(
            HttpConnectionPreviewRequest(
                context=GLOBAL,
                group=group_key(py, ORDERS_ORIGIN),
                repository="nowhere",
                include_diagnostics=True,
            )
        )


def test_case_15_a_repository_with_no_route_is_not_a_service_and_recognises_nothing(
    corpora: Corpora,
) -> None:
    py = corpora.workspace("py")
    result = preview(py, ORDERS_ORIGIN, "emptyprov", include_diagnostics=True)
    assert result.requirements[0] == (
        f"{slug_of(py, 'emptyprov')} serves no route this index holds: it is not an indexed service"
    )
    hints = result.provider_diagnostics
    assert hints is not None
    assert hints.lifecycle.recognized == 0
    assert hints.unpublished_matches == () and hints.unpublished_match_count == 0


# -- case 17b: served, but not declarable ---------------------------------------------------------


def test_case_17b_a_route_with_a_controller_prefix_is_published_and_can_now_be_exposed(
    corpora: Corpora,
) -> None:
    """The composed route states its decorator line, so it has no join limit."""
    nest = corpora.workspace("nest")
    rows, _ = inventory(nest, "nestok", include_diagnostics=True)
    routes = {row.route: row for row in rows if row.role == "provider"}
    assert set(routes) == {"/health", "/shop/items/:id"}
    assert all(row.status in PUBLISHED for row in routes.values())
    for row in routes.values():
        assert row.diagnostic is not None
        assert tuple(row.diagnostic.join_limits) == ()
        assert row.diagnostic.join_eligible


def test_case_17b_the_preview_counts_what_the_declaration_can_now_expose(
    corpora: Corpora,
) -> None:
    """Both routes are exposable and none lacks a line."""
    result = preview(corpora.workspace("nest"), NEST_ORIGIN, "nestok", include_diagnostics=True)
    assert result.counts == {"would-link": 2}
    hints = result.provider_diagnostics
    assert hints is not None
    assert hints.routes_exposable == 2
    assert hints.routes_without_declaration_line == 0
