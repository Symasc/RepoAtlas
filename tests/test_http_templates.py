"""Client path templates and their structural match against declared routes.

A template names a *shape* — fixed segments and positions one value fills — never
a route key. These tests pin the two halves of that: which templates can be read
as a shape at all (everything else is refused with a stable reason), and the
narrow rule that decides whether a declared route has the shape. The rule's most
important property is what it refuses: a literal never matches a parameter, in
either direction, so ``/users/42`` still does not become ``/users/:id``.
"""

from __future__ import annotations

import pytest
from repoatlas.models import http
from repoatlas.models.http import RouteTemplate, route_template, template_matches

L, S, Q = http.TEMPLATE_LITERAL, http.TEMPLATE_SUBSTITUTION, http.TEMPLATE_QUERY


def _template(*parts: tuple[str, str]) -> RouteTemplate:
    result = route_template([list(part) for part in parts])
    assert isinstance(result, RouteTemplate), result
    return result


# -- reading a template -----------------------------------------------------------


def test_a_whole_segment_substitution_is_one_dynamic_segment() -> None:
    template = _template((L, "/v1/courses/"), (S, "courseId"), (L, "/roster"))
    assert template.segments == ("v1", "courses", None, "roster")
    assert template.dynamic
    assert template.path is None
    assert template.display == "/v1/courses/${courseId}/roster"


def test_a_template_without_substitutions_is_its_literal_path() -> None:
    template = _template((L, "/v1/me/organizations"))
    assert not template.dynamic
    assert template.path == "/v1/me/organizations"


def test_a_proven_query_tail_ends_the_path_and_adds_nothing() -> None:
    """``${params ? `?${params}` : ""}`` is empty or a query string."""
    template = _template((L, "/v1/courses/"), (S, "courseId"), (L, "/roster"), (Q, "params"))
    assert template.display == "/v1/courses/${courseId}/roster"
    assert template.query


def test_a_literal_question_mark_ends_the_path() -> None:
    template = _template((L, "/v1/announcements?courseId="), (S, "courseId"))
    assert template.path == "/v1/announcements"
    assert template.query
    assert template.expressions == ()


def test_a_fragment_ends_the_path_and_is_not_a_query() -> None:
    template = _template((L, "/v1/help#"), (S, "section"))
    assert template.path == "/v1/help"
    assert not template.query


def test_a_trailing_slash_and_an_empty_segment_are_kept_byte_exact() -> None:
    assert _template((L, "/v1/x/")).path == "/v1/x/"
    assert _template((L, "/v1//x/"), (S, "id")).segments == ("v1", "", "x", None)


def test_the_display_names_each_substitution_in_order() -> None:
    template = _template((L, "/a/"), (S, "first"), (L, "/b/"), (S, "second"))
    assert template.display == "/a/${first}/b/${second}"


@pytest.mark.parametrize(
    ("parts", "reason"),
    [
        pytest.param([], http.NOT_A_PATH, id="empty"),
        pytest.param([(S, "base"), (L, "/users")], http.NOT_A_PATH, id="leading-substitution"),
        pytest.param([(L, "//api.example.com/"), (S, "id")], http.NOT_A_PATH, id="host"),
        pytest.param([(L, "users/"), (S, "id")], http.NOT_A_PATH, id="relative"),
        pytest.param([(L, "/a b/"), (S, "id")], http.NOT_A_PATH, id="space"),
        pytest.param([(L, "/v1/x-"), (S, "y"), (L, "/z")], http.MIXED_SEGMENT, id="prefix"),
        pytest.param([(L, "/files/"), (S, "name"), (L, ".csv")], http.MIXED_SEGMENT, id="suffix"),
        pytest.param([(L, "/v1/"), (S, "a"), (S, "b")], http.MIXED_SEGMENT, id="two-in-one"),
        pytest.param(
            [(L, "/v1/access-events"), (S, "query(filters)")],
            http.GLUED_SUBSTITUTION,
            id="glued-query-helper",
        ),
        pytest.param(
            [(L, "/v1/x/"), (S, "id"), (Q, "params"), (L, "/more")],
            http.GLUED_SUBSTITUTION,
            id="query-tail-not-last",
        ),
        pytest.param([(L, "/"), (S, "id")], http.NO_LITERAL_SEGMENT, id="only-dynamic"),
        pytest.param([(L, "/"), (S, "a"), (L, "/"), (S, "b")], http.NO_LITERAL_SEGMENT, id="two"),
        pytest.param([(L, "/v1/\\u0061/"), (S, "id")], http.ESCAPE_SEQUENCE, id="escape"),
        pytest.param([(L, "/v1/"), ("x", "?")], http.NOT_A_PATH, id="unknown-kind"),
    ],
)
def test_a_template_that_cannot_be_read_as_a_shape_is_refused(
    parts: list[tuple[str, str]], reason: str
) -> None:
    assert route_template([list(part) for part in parts]) == reason


def test_a_truncated_template_is_refused_whatever_it_reads_as() -> None:
    """A cut template could match a route it does not name."""
    parts = [[L, "/v1/courses/"], [S, "id"], [L, "/roster/"], [S, "studentId"]]
    assert isinstance(route_template(parts), RouteTemplate)
    assert route_template(parts, truncated=True) == http.TRUNCATED


def test_a_template_longer_than_any_identity_is_refused() -> None:
    parts = [[L, "/" + "a" * http.MAX_IDENTITY_LENGTH + "/"], [S, "id"]]
    assert route_template(parts) == http.NOT_A_PATH


# -- matching a declared route -----------------------------------------------------


ROSTER = _template((L, "/v1/courses/"), (S, "courseId"), (L, "/roster"))


@pytest.mark.parametrize(
    "declared",
    [
        "/v1/courses/:courseId/roster",
        "/v1/courses/:id/roster",
        "/v1/courses/{courseId}/roster",
        "/v1/courses/<courseId>/roster",
        "/v1/courses/<int:courseId>/roster",
    ],
)
def test_a_dynamic_segment_matches_any_whole_declared_parameter(declared: str) -> None:
    assert template_matches(ROSTER, declared)


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param("/v1/courses/42/roster", id="literal-value"),
        pytest.param("/v1/courses/roster/roster", id="literal-word"),
        pytest.param("/v1/courses/:id?/roster", id="optional"),
        pytest.param("/v1/courses/:id(\\d+)/roster", id="regex"),
        pytest.param("/v1/courses/*/roster", id="wildcard"),
        pytest.param("/v1/courses/x-:id/roster", id="mixed-parameter"),
        pytest.param("/v1/courses/:courseId/roster/", id="trailing-slash"),
        pytest.param("/v1/courses/:courseId", id="shorter"),
        pytest.param("/v1/courses/:courseId/roster/:studentId", id="longer"),
        pytest.param("/V1/courses/:courseId/roster", id="case"),
        pytest.param("v1/courses/:courseId/roster", id="relative"),
    ],
)
def test_nothing_but_the_same_shape_matches(declared: str) -> None:
    assert not template_matches(ROSTER, declared)


def test_a_literal_never_matches_a_parameter_in_either_direction() -> None:
    """HTTP evaluation scenario I: ``/users/42`` is not ``/users/:id``."""
    concrete = _template((L, "/users/42"))
    assert not template_matches(concrete, "/users/:id")
    literal_parameter = _template((L, "/users/:id"))
    assert template_matches(literal_parameter, "/users/:id")
    assert not template_matches(literal_parameter, "/users/42")


def test_literal_segments_compare_byte_for_byte() -> None:
    template = _template((L, "/v1/Users/"), (S, "id"))
    assert template_matches(template, "/v1/Users/:id")
    assert not template_matches(template, "/v1/users/:id")


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        (":id", True),
        (":courseId", True),
        ("{id}", True),
        ("{user-id}", True),
        ("<id>", True),
        ("<int:id>", True),
        (":", False),
        (":id?", False),
        (":id(\\d+)", False),
        ("*", False),
        ("{id", False),
        ("id", False),
        ("x:id", False),
    ],
)
def test_a_parameter_segment_is_one_whole_declared_parameter(segment: str, expected: bool) -> None:
    assert http.is_parameter_segment(segment) is expected
