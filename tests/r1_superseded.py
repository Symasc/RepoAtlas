"""What F1, F2 and F4 are allowed to change in ``tests/data/r1_golden.json``, entry by entry.

The golden file is never edited or regenerated. Where the work supersedes a pinned section, the
section is compared with the golden *through an allowlist*: the entries (a repository's plan, a
preview, a decision, an edge ...) that may differ, and for each the exact way it may. An entry that
differs and is not listed, a listed entry that does not differ, an entry added or removed that is
not listed: each is a problem to escalate, never a reason to widen the list.

The expected differences are the frozen ones: the declaration line of a composed NestJS route
(F1: ``declared_at`` 5 in the plan, a handler in the preview) and the state of a declaration of a
service that serves only promoted routes (F2: applied, joined, the request linked).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: The shop controller's route decorator is on line 5 of ``src/shop.controller.ts``.
SHOP_ROUTE = "GET /shop/items/:id"
SHOP_REQUEST = "/shop/items/42"
SHOP_HANDLER = "src/shop.controller.ts:5"
SHOP_CALLER = "sym:buyer3:buyer3/client.py:fetch_item:8"
FETCH_ITEM_DECISION = "hcd1-146c23cb2f939d5915a8"
FETCH_HEALTH_DECISION = "hcd1-0a292f1908cf2ba48e8b"
ORIGIN = "https://nest.example.test"

Check = Callable[[Any, Any], bool]


@dataclass(frozen=True)
class Rule:
    #: Entries that must differ, by key, each with the check on (golden entry, entry now).
    changed: dict[str, Check] = field(default_factory=dict)
    #: Entries that must be added, each matched by one check on the new entry.
    added: tuple[Callable[[Any], bool], ...] = ()


def entries(section: str, value: Any) -> dict[str, Any]:
    """A section as the independent entries it is made of, each under a stable key."""
    if section == "edges":
        return {json.dumps(edge, sort_keys=True): edge for edge in value}
    if section == "decisions":
        return {decision["decision_ref"]: decision for decision in value}
    if section == "declarations":
        return {str(declaration["id"]): declaration for declaration in value}
    if section == "previews":
        return {f"{item['origin']}|{item['repository']}": item for item in value}
    if section in {"operations", "plans"}:
        return dict(value)
    return {section: value}  # repositories, worklist


# -- the expected changes -------------------------------------------------------------------------


def _with_declared_line(plan: Any) -> Any:
    expected = copy.deepcopy(plan)
    [route] = [r for r in expected["code_routes"] if r["body"] == SHOP_ROUTE]
    route["declared_at"] = 5
    return expected


def _plan_gains_the_declaration_line(then: Any, now: Any) -> bool:
    return bool(now == _with_declared_line(then))


def _preview_joins_the_shop_request(
    counts: dict[str, int], *, can_create: bool | None = None, requirements: list[str] | None = None
) -> Check:
    def check(then: Any, now: Any) -> bool:
        expected = copy.deepcopy(then)
        preview = expected["preview"]
        preview["counts"] = counts
        for row in preview["rows"]:
            if row["route"] == SHOP_REQUEST:
                row["outcome"], row["handler"] = "would-link", SHOP_HANDLER
        if can_create is not None:
            preview["can_create"] = can_create
        if requirements is not None:
            preview["requirements"] = requirements
        return bool(now == expected)

    return check


def _the_linked_request_leaves(counts: dict[str, int]) -> Check:
    """A preview in the ``nest-declared`` corpus: ``fetch_item`` is linked by the declaration, so
    it is no longer a request of the group and its row is gone. Counts are the remaining rows';
    every other field is what it was."""

    def check(then: Any, now: Any) -> bool:
        expected = copy.deepcopy(then)
        preview = expected["preview"]
        preview["counts"] = counts
        preview["rows"] = [row for row in preview["rows"] if row["route"] != SHOP_REQUEST]
        return bool(now == expected)

    return check


# -- the nest-declared corpus: the request the declaration links --------------------------------

_LINK_IN = {
    "basis": "link",
    "line_end": None,
    "line_start": None,
    "path": "@deployment-links/1",
    "role": "link_in",
}
_SERVED_AT = {
    "line_end": 5,
    "line_start": 5,
    "path": "src/shop.controller.ts",
    "role": "served_at",
}


def _the_linked_decision_chain(then_chain: list[Any]) -> list[Any]:
    return [
        *then_chain[:2],
        {
            "detail": "mapped: https://nest.example.test",
            "kind": "service_identity",
            "provenance": [_LINK_IN],
            "status": "declared",
        },
        {"kind": "deployment_binding", "provenance": [_LINK_IN], "status": "declared"},
        {"kind": "source_binding", "provenance": [_LINK_IN], "status": "declared"},
        {
            "detail": "GET https://nest.example.test/shop/items/:id",
            "kind": "route",
            "provenance": [_SERVED_AT],
            "status": "proven",
        },
        {"kind": "handler", "provenance": [_SERVED_AT], "status": "proven"},
    ]


def _the_shop_request_is_linked(then: Any, now: Any) -> bool:
    expected = copy.deepcopy(then)
    expected["chain"] = _the_linked_decision_chain(then["chain"])
    expected["decision"] = "linked"
    expected["evidence"] = {
        "called_at": "buyer3/client.py:9",
        "consumer": "buyer3",
        "decision_ref": FETCH_ITEM_DECISION,
        "deployment": ORIGIN,
        "endpoint": "GET https://nest.example.test/shop/items/:id",
        "identity_source": "deployment-link",
        "link_id": "1",
        "link_label": "mapped",
        "link_verification": "unverified",
        "method": "GET",
        "provider": "nestcomposed",
        "served_at": SHOP_HANDLER,
        "url": "https://nest.example.test/shop/items/42",
    }
    return bool(now == expected)


def _the_health_request_is_declined_by_route(then: Any, now: Any) -> bool:
    """``/health`` is requested at the declared origin and ``nestcomposed`` serves no such route:
    still unmatched, now with the declaration's refusal in its evidence."""
    expected = copy.deepcopy(then)
    expected["evidence"] = {
        **then["evidence"],
        "declaration_ids": "1",
        "declaration_outcome": "route-unmatched",
    }
    return bool(now == expected and now["decision"] == "http-service-unmatched")


def _declaration_is_applied(then: Any, now: Any) -> bool:
    expected = {
        **then,
        "state": "applied",
        "reason": "",
        "joined": 1,
        "declined": {"route-unmatched": 1},
    }
    return bool(now == expected)


def _a_handler_edge_for_the_shop_request(edge: Any) -> bool:
    http = (edge.get("resolution") or {}).get("http") or {}
    return bool(
        edge["type"] == "CALLS_HTTP_HANDLER"
        and edge["src"] == SHOP_CALLER
        and edge["dst"] == "sym:nestcomposed:src/shop.controller.ts:ShopController.item:6"
        and edge["asserted_in"] == "buyer3/client.py"
        and edge["line_start"] == edge["line_end"] == 9
        and edge["parser"] == "linker:global"
        and http.get("provider") == "nestcomposed"
        and http.get("served_at") == SHOP_HANDLER
        and http.get("identity_source") == "deployment-link"
    )


def _the_shop_crossing_is_linked(then: Any, now: Any) -> bool:
    """buyer3's inventory: the ``fetch_item`` crossing is linked, with the declared chain and the
    endpoint; the operation's own status and everything else are what they were."""
    expected = copy.deepcopy(then)
    [crossing] = [
        crossing
        for operation in expected["operations"]
        for crossing in operation["crossings"]
        if crossing["decision_ref"] == FETCH_ITEM_DECISION
    ]
    crossing["status"] = "linked"
    crossing["endpoint"] = "http-endpoint:GET:https://nest.example.test/shop/items/:id"
    crossing["chain"] = [
        *crossing["chain"][:2],
        {"kind": "service_identity", "status": "declared", "withheld": True},
        {"kind": "deployment_binding", "status": "declared", "withheld": True},
        {"kind": "source_binding", "status": "declared", "withheld": True},
        {"kind": "route", "status": "proven", "withheld": True},
        {"kind": "handler", "status": "proven", "withheld": True},
    ]
    expected["summary"]["by_crossing"] = {"http-service-unmatched": 1, "linked": 1}
    return bool(now == expected)


def _the_worklist_counts_one_request_linked(then: Any, now: Any) -> bool:
    expected = {
        **then,
        "by_status": {"http-service-unmatched": 1},
        "declarable": 1,
        "declarations": {"applied": 1},
        "linked_by_declaration": 1,
        "unresolved": 1,
        "unresolved_identity": 1,
    }
    return bool(now == expected)


_PLANS = Rule(
    changed={
        "nestok": _plan_gains_the_declaration_line,
        "nestcomposed": _plan_gains_the_declaration_line,
    }
)

#: (corpus, section) -> what may differ. A pair that is not here must equal the golden.
ALLOWED: dict[tuple[str, str], Rule] = {
    ("nest", "plans"): _PLANS,
    ("nest", "previews"): Rule(
        changed={
            f"{ORIGIN}|nestok": _preview_joins_the_shop_request({"would-link": 2}),
            f"{ORIGIN}|nestcomposed": _preview_joins_the_shop_request(
                {"no-matching-route": 1, "would-link": 1}, can_create=True, requirements=[]
            ),
        }
    ),
    ("nest-declared", "plans"): _PLANS,
    ("nest-declared", "previews"): Rule(
        changed={
            f"{ORIGIN}|buyer3": _the_linked_request_leaves({"no-matching-route": 1}),
            f"{ORIGIN}|nestbare": _the_linked_request_leaves({"no-matching-route": 1}),
            f"{ORIGIN}|nestcomposed": _the_linked_request_leaves({"no-matching-route": 1}),
            f"{ORIGIN}|nestok": _the_linked_request_leaves({"would-link": 1}),
            f"{ORIGIN}|nestprov": _the_linked_request_leaves({"no-matching-route": 1}),
        }
    ),
    ("nest-declared", "declarations"): Rule(changed={"1": _declaration_is_applied}),
    ("nest-declared", "decisions"): Rule(
        changed={
            FETCH_HEALTH_DECISION: _the_health_request_is_declined_by_route,
            FETCH_ITEM_DECISION: _the_shop_request_is_linked,
        }
    ),
    ("nest-declared", "edges"): Rule(added=(_a_handler_edge_for_the_shop_request,)),
    ("nest-declared", "operations"): Rule(changed={"buyer3": _the_shop_crossing_is_linked}),
    ("nest-declared", "worklist"): Rule(
        changed={"worklist": _the_worklist_counts_one_request_linked}
    ),
}

#: The pairs the golden comparison alone cannot pass any more.
SUPERSEDED = tuple(sorted(ALLOWED))


def problems(corpus: str, section: str, pinned: Any, now: Any) -> list[str]:
    """Every way ``now`` differs from the golden other than the allowlist says; empty when none."""
    rule = ALLOWED.get((corpus, section))
    if rule is None:
        return [] if pinned == now else [f"{corpus}.{section} differs and is not allowed to"]
    before, after = entries(section, pinned), entries(section, now)
    found = [f"removed: {key}" for key in sorted(set(before) - set(after))]
    unmatched = list(rule.added)
    for key in sorted(set(after) - set(before)):
        match = next((check for check in unmatched if check(after[key])), None)
        if match is None:
            found.append(f"added and not allowed: {after[key]!r:.300}")
        else:
            unmatched.remove(match)
    found += [f"{len(unmatched)} expected addition(s) missing"] if unmatched else []
    changed = {key for key in set(before) & set(after) if before[key] != after[key]}
    for key in sorted(changed - set(rule.changed)):
        found.append(f"changed and not allowed: {key}")
    for key, check in sorted(rule.changed.items()):
        if key not in changed:
            found.append(f"expected to change and did not: {key}")
        elif not check(before[key], after[key]):
            found.append(f"changed otherwise than allowed: {key}")
    return found
