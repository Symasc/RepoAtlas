"""Bounded, deterministic paging over the dependency inventory (V2 Slice G).

Dependencies is the first capability that walks an inventory rather than
answering one question, so it is the first that has to keep a promise across
calls. Two properties make that promise worth anything.

**The pages concatenate to the whole answer.** Within one unchanged index, every
page size produces the same ordered result — no duplicate, no omission. That only
holds if the order is a fact about the repositories: a cut over SQLite's incidental
row order would let two indexes built from the same managed root disagree about
which packages are on page one, and neither would be wrong.

**A cursor that cannot be honoured is refused, not reinterpreted.** A token names
a position in one filtered dataset drawn from one index. Honouring it against a
different question silently slices a set it never described; honouring it against
a changed index returns a result assembled from two generations that no single
state of the repositories ever contained. Both are worse than an error, because a
page that is quietly wrong looks exactly like a page that is right.
"""

from __future__ import annotations

import base64
import json

import pytest
from repoatlas.capabilities.dependencies import CURSOR_VERSION, encode_cursor
from repoatlas.capabilities.errors import InvalidCursorError, StaleCursorError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependencies import DependencyRole
from repoatlas.models.dependency_inventory import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DependenciesRequest,
    DependenciesResponse,
    Ecosystem,
)
from repoatlas.services.workspace import Workspace
from tests.capabilities.test_dependencies import _plant

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
COMMON = "common-lib"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _ask(workspace: Workspace, **kwargs: object) -> DependenciesResponse:
    fields: dict[str, object] = {"context": GLOBAL, "include_provenance": False}
    fields.update(kwargs)
    return _service(workspace).dependencies(DependenciesRequest(**fields))  # type: ignore[arg-type]


def _keys(response: DependenciesResponse) -> list[tuple[str, str, str]]:
    return [(p.repository_slug, p.ecosystem, p.normalized_name) for p in response.packages]


def _walk(workspace: Workspace, size: int, **kwargs: object) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []
    cursor: str | None = None
    while True:
        response = _ask(workspace, limit=size, cursor=cursor, **kwargs)
        seen += _keys(response)
        cursor = response.next_cursor
        if cursor is None:
            assert response.has_more is False
            break
        assert response.has_more is True
    return seen


# ---------------------------------------------------------------------------
# CASE 14 — the pages are the whole answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 100])
def test_every_page_size_reproduces_the_whole_ordered_result(
    initialized: Workspace, size: int
) -> None:
    """CASE 14. No duplicate, no omission, same order, at any width."""
    whole = _keys(_ask(initialized, limit=MAX_PAGE_SIZE))
    assert len(whole) > 5, "the fixture no longer exercises paging"

    walked = _walk(initialized, size)
    assert walked == whole
    assert len(walked) == len(set(walked))


def test_a_page_is_a_prefix_of_the_remaining_order(initialized: Workspace) -> None:
    whole = _keys(_ask(initialized, limit=MAX_PAGE_SIZE))
    first = _ask(initialized, limit=3)
    assert _keys(first) == whole[:3]

    second = _ask(initialized, limit=3, cursor=first.next_cursor)
    assert _keys(second) == whole[3:6]


def test_has_more_is_observed_and_not_inferred_from_a_full_page(
    initialized: Workspace,
) -> None:
    """A page that exactly exhausts the inventory is the last one.

    Inferring "more exists" from a page being full would promise a next page that
    turns out empty, and the caller would have no way to tell the difference until
    they asked.
    """
    whole = _keys(_ask(initialized, limit=MAX_PAGE_SIZE))
    exact = _ask(initialized, limit=len(whole))

    assert _keys(exact) == whole
    assert exact.has_more is False
    assert exact.next_cursor is None


def test_the_last_page_carries_no_cursor(initialized: Workspace) -> None:
    last: DependenciesResponse | None = None
    cursor: str | None = None
    while True:
        response = _ask(initialized, limit=2, cursor=cursor)
        cursor = response.next_cursor
        last = response
        if cursor is None:
            break
    assert last is not None
    assert last.next_cursor is None
    assert last.has_more is False


def test_paging_a_filtered_inventory_stays_inside_the_filter(
    initialized: Workspace,
) -> None:
    whole = _keys(_ask(initialized, limit=MAX_PAGE_SIZE, ecosystem=Ecosystem.PYPI))
    assert _walk(initialized, 1, ecosystem=Ecosystem.PYPI) == whole
    assert all(key[1] == "pypi" for key in whole)


def test_changing_page_size_mid_walk_is_allowed(initialized: Workspace) -> None:
    """Continuation is by sort key, not by offset, so width is not part of identity.

    Refusing it would be a restriction with nothing behind it.
    """
    whole = _keys(_ask(initialized, limit=MAX_PAGE_SIZE))

    first = _ask(initialized, limit=2)
    second = _ask(initialized, limit=5, cursor=first.next_cursor)
    assert _keys(first) + _keys(second) == whole[:7]


def test_the_default_page_size_is_applied_when_none_is_asked_for(
    initialized: Workspace,
) -> None:
    response = _ask(initialized)
    assert response.limits.applied == DEFAULT_PAGE_SIZE
    assert response.limits.requested is None
    assert response.limits.clamped == ()


# ---------------------------------------------------------------------------
# CASE 15 — a cursor belongs to one question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        {"ecosystem": Ecosystem.PYPI},
        {"name": "common-lib"},
        {"roles": (DependencyRole.DECLARED,)},
        {"include_providers": False},
        {"include_provenance": True},
    ],
)
def test_a_cursor_from_another_question_is_refused(
    initialized: Workspace, changed: dict[str, object]
) -> None:
    """CASE 15. Reusing it would slice a dataset it never described."""
    first = _ask(initialized, limit=2)
    assert first.next_cursor is not None

    with pytest.raises(InvalidCursorError):
        _ask(initialized, limit=2, cursor=first.next_cursor, **changed)


def test_a_cursor_from_another_scope_is_refused(initialized: Workspace) -> None:
    """A scope resolving to different repositories is a different question."""
    first = _ask(initialized, limit=2)
    assert first.next_cursor is not None

    with pytest.raises(InvalidCursorError):
        _service(initialized).dependencies(
            DependenciesRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
                limit=2,
                cursor=first.next_cursor,
                include_provenance=False,
            )
        )


@pytest.mark.parametrize(
    "token",
    ["", "   ", "not-base64!!", "YWJj", "e30", "*" * 40],
)
def test_a_damaged_cursor_is_refused_rather_than_guessed_at(
    initialized: Workspace, token: str
) -> None:
    if not token.strip():
        with pytest.raises(ValueError):
            DependenciesRequest(context=GLOBAL, cursor=token)
        return
    with pytest.raises(InvalidCursorError):
        _ask(initialized, limit=2, cursor=token)


def test_a_cursor_from_another_payload_version_is_refused(
    initialized: Workspace,
) -> None:
    payload = json.dumps({"v": CURSOR_VERSION + 1, "r": "x", "i": "y", "a": ["a", "b", "c"]})
    token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCursorError):
        _ask(initialized, limit=2, cursor=token)


def test_a_cursor_naming_a_package_outside_the_dataset_is_refused(
    initialized: Workspace,
) -> None:
    """The digests agreed but the key is absent, which is a corrupted token."""
    first = _ask(initialized, limit=2)
    assert first.next_cursor is not None
    decoded = json.loads(
        base64.urlsafe_b64decode(first.next_cursor + "=" * (-len(first.next_cursor) % 4))
    )
    forged = encode_cursor(
        request=decoded["r"], inventory=decoded["i"], after=("nowhere", "pypi", "nothing")
    )
    with pytest.raises(InvalidCursorError):
        _ask(initialized, limit=2, cursor=forged)


# ---------------------------------------------------------------------------
# CASE 16 — the index moved between pages
# ---------------------------------------------------------------------------


def test_a_changed_inventory_stops_the_walk_rather_than_mixing_generations(
    initialized: Workspace,
) -> None:
    """CASE 16. Page two must not be a slice of a different inventory.

    A package appearing between two calls shifts every later position. Continuing
    would silently skip or repeat rows and return an answer no single state of the
    index ever held — the kind of wrong that looks complete.
    """
    first = _ask(initialized, limit=2)
    assert first.next_cursor is not None

    _plant(
        initialized,
        slug=COMMON,
        rel_path="pyproject.toml",
        ecosystem="pypi",
        name="aaa-inserted-first",
        normalized="aaa-inserted-first",
        role="declared",
        constraint=">=1",
        line=3,
    )

    with pytest.raises(StaleCursorError):
        _ask(initialized, limit=2, cursor=first.next_cursor)


def test_a_changed_value_alone_stops_the_walk(initialized: Workspace) -> None:
    """Not only the package set: a constraint the caller would have been shown."""
    first = _ask(initialized, limit=2)
    assert first.next_cursor is not None

    initialized.database.execute(
        "UPDATE dependencies SET constraint_text = '>=99' "
        "WHERE normalized_name = 'requests' AND role = 'declared'"
    )
    initialized.database.connection.commit()

    with pytest.raises(StaleCursorError):
        _ask(initialized, limit=2, cursor=first.next_cursor)


def test_a_stale_cursor_and_a_wrong_cursor_are_different_failures(
    initialized: Workspace,
) -> None:
    """One is the caller's mistake; the other is nobody's. They need different remedies."""
    assert not issubclass(StaleCursorError, InvalidCursorError)
    assert not issubclass(InvalidCursorError, StaleCursorError)

    from repoatlas.capabilities.errors import capability_error

    wrong = capability_error(InvalidCursorError())
    stale = capability_error(StaleCursorError())
    assert wrong.code == "INVALID_CURSOR"
    assert stale.code == "STALE_CURSOR"
    assert wrong.category is not stale.category
    # Neither is retryable: the identical call carries the identical token.
    assert wrong.retryable is False and stale.retryable is False


def test_an_unchanged_index_keeps_the_cursor_valid(initialized: Workspace) -> None:
    """The detection must not be so eager that ordinary paging trips it."""
    first = _ask(initialized, limit=2)
    for _ in range(3):
        assert _ask(initialized, limit=2, cursor=first.next_cursor).packages


# ---------------------------------------------------------------------------
# §31 — the token is opaque
# ---------------------------------------------------------------------------


def test_the_cursor_exposes_no_storage_identifier(initialized: Workspace) -> None:
    """Not an offset, not a row id. A hand-editable cursor is a way to page
    through a dataset that no longer exists."""
    response = _ask(initialized, limit=2)
    assert response.next_cursor is not None

    decoded = base64.urlsafe_b64decode(
        response.next_cursor + "=" * (-len(response.next_cursor) % 4)
    ).decode("utf-8")
    payload = json.loads(decoded)

    assert set(payload) == {"v", "r", "i", "a"}
    for banned in ("offset", "rowid", "id", "dependency_id", "file_id", "repo_id", "limit"):
        assert banned not in payload, banned
    # The continuation key is the public identity, which is what makes it safe to
    # carry: a caller who decoded it would learn only what the response already said.
    assert payload["a"] == list(_keys(response)[-1])


def test_the_cursor_survives_a_url_and_a_json_round_trip(initialized: Workspace) -> None:
    """Opaque to a caller, but still an ordinary string to a transport."""
    response = _ask(initialized, limit=2)
    assert response.next_cursor is not None
    token = response.next_cursor

    assert token == json.loads(json.dumps(token))
    assert "=" not in token and "+" not in token and "/" not in token
    assert _ask(initialized, limit=2, cursor=token).packages
