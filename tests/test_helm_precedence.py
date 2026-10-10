"""Helm's value precedence as DF2 reads it (DF2_PHASE_A.md §2.2): the chart's values, each
``-f`` file in order, each ``--set`` in order; maps merge, lists are replaced whole, a null
deletes; a layer that may say anything leaves everything it could touch unknown; a withheld
or symbolic value never renders as text."""

from __future__ import annotations

import json

import yaml
from repoatlas.graph.ci_facts import Place
from repoatlas.graph.deploy_artifacts import _Effective
from repoatlas.models.container_image import EXPR, TEXT, VAR, WITHHELD
from repoatlas.parsers.deploy_helm import read_values

PLACE = Place(1, ".github/workflows/deploy.yml", 12)


def layer(text: str, **extra: str) -> dict[str, str]:
    return {**read_values(yaml.compose(text)).meta(), **extra}


def literal(values: _Effective, key: str) -> str | None:
    found = values.get(key)
    if found is None:
        return None
    assert found.kind == "lit", (key, found)
    return found.text


def test_a_later_file_wins_a_set_wins_over_every_file_and_maps_merge() -> None:
    values = _Effective()
    values.file(layer("host: chart.example\nenv:\n  A: '1'\n  B: '2'\n"), 1, "charts/x/values.yaml")
    values.file(layer("host: test.example\nenv:\n  A: '10'\n"), 1, "env/test.yaml")
    values.setting("env.B", ((TEXT, "20"),), PLACE)
    assert literal(values, "host") == "test.example"
    assert (literal(values, "env.A"), literal(values, "env.B")) == ("10", "20")
    # each value keeps its place: the leaf's line, or the line carrying the --set
    assert values.get("host").place == Place(1, "env/test.yaml", 1)  # type: ignore[union-attr]
    assert values.get("env.B").place == PLACE  # type: ignore[union-attr]


def test_a_later_scalar_replaces_a_map_and_a_later_map_a_scalar() -> None:
    values = _Effective()
    values.file(layer("a:\n  b: 1\n  c: 2\nd: x\n"), 1, "v.yaml")
    values.setting("a", ((TEXT, "flat"),), PLACE)
    values.setting("d.e", ((TEXT, "deep"),), PLACE)
    assert literal(values, "a") == "flat" and values.get("a.b") is None
    assert literal(values, "d.e") == "deep" and values.get("d") is None


def test_a_later_list_replaces_the_earlier_one_whole() -> None:
    values = _Effective()
    values.file(layer("hosts:\n  - a.example\n  - b.example\n"), 1, "v.yaml")
    values.file(layer("hosts:\n  - c.example\n"), 1, "w.yaml")
    assert literal(values, "hosts[0]") == "c.example"
    assert values.get("hosts[1]") is None


def test_a_null_deletes_and_an_empty_list_empties() -> None:
    values = _Effective()
    values.file(layer("ingress:\n  host: a.example\ngroups:\n  - x\n"), 1, "v.yaml")
    values.file(layer("ingress:\n  host: null\ngroups: []\n"), 1, "w.yaml")
    assert values.get("ingress.host") is None
    assert values.get("groups[0]") is None


def test_an_unread_layer_makes_everything_before_it_and_every_absent_key_unknown() -> None:
    values = _Effective()
    values.file(layer("host: a.example\n"), 1, "v.yaml")
    values.unknown("values-unresolved")
    assert values.get("host").kind == "unknown"  # type: ignore[union-attr]
    assert values.get("anything").kind == "unknown"  # type: ignore[union-attr]
    # what a later layer sets is known again
    values.setting("host", ((TEXT, "b.example"),), PLACE)
    assert literal(values, "host") == "b.example"
    # and a later null makes a key absent, not unknown
    values.setting("gone", ((TEXT, "null"),), PLACE)
    assert values.get("gone") is None


def test_a_truncated_file_leaves_what_it_may_have_cut_unknown() -> None:
    values = _Effective()
    values.file(layer("host: a.example\n"), 1, "v.yaml")
    values.file(layer("other: y\n", truncated="true"), 1, "big.yaml")
    assert values.get("host").kind == "unknown"  # type: ignore[union-attr]
    assert literal(values, "other") == "y"


def test_a_withheld_value_is_unknown_never_text() -> None:
    values = _Effective()
    values.file(layer("auth:\n  password: hunter2\n"), 1, "v.yaml")
    assert json.loads(layer("auth:\n  password: hunter2\n")["values"]) == {
        "auth.password": "<withheld>"
    }
    # DF3a (§12.2): where it is set is kept, what it is never; it renders as unknown
    found = values.get("auth.password")
    assert found is not None and (found.kind, found.text) == ("withheld", "")
    assert found.place is not None and found.place.path == "v.yaml"
    assert values.literals("auth.password") is None
    values.setting("token", ((WITHHELD, ""),), PLACE)
    token = values.get("token")
    assert token is not None and (token.kind, token.text, token.place) == ("withheld", "", PLACE)


def test_a_ci_symbol_is_a_symbol_never_literal_text() -> None:
    values = _Effective()
    values.setting("image.tag", ((VAR, "CIRCLE_SHA1"),), PLACE)
    values.setting("stage", ((TEXT, "x-"), (EXPR, "github.ref_name")), PLACE)
    assert values.get("image.tag").kind == "sym"  # type: ignore[union-attr]
    assert values.get("stage").kind == "sym"  # type: ignore[union-attr]
    assert values.literals("image.tag") is None
    # an unread --set (a path not read) may set anything
    values.setting(None, ((EXPR, ""),), PLACE)
    assert values.get("host").kind == "unknown"  # type: ignore[union-attr]
