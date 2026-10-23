"""The scope a declaration carries: its words are validated before anything stores them."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from repoatlas.graph.http_scopes import base_key
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationScope,
    DeploymentLinkFields,
    LinkConflictError,
    ScopeConflictError,
)
from repoatlas.models.http_connections import HttpConnectionPreviewRequest

KEY = base_key("baseUrl")


def _scope(**over: Any) -> DeclarationScope:
    words: dict[str, Any] = {
        "path": "src/client.py",
        "symbol": "Client.fetch",
        "base_key": KEY,
        "base_shown": "{baseUrl}",
    }
    return DeclarationScope(**{**words, **over})


def _link(**over: Any) -> DeploymentLinkFields:
    fields: dict[str, Any] = {"label": "shop", "callers": ["web"], "scope": _scope()}
    return DeploymentLinkFields(**{**fields, **over})


def test_a_scope_states_no_url_and_exactly_one_caller() -> None:
    assert _link().scope == _scope()
    with pytest.raises(ValidationError, match="no URL"):
        _link(url="https://shop.example.test/")
    with pytest.raises(ValidationError, match="exactly one caller"):
        _link(callers=["web", "mobile"])
    with pytest.raises(ValidationError, match="exactly one caller"):
        _link(callers=[])
    with pytest.raises(ValidationError, match="no URL"):
        _link(target_id="dt_" + "0" * 16)
    with pytest.raises(ValidationError, match="no variables"):
        _link(variables={"stage": "prod"})


def test_a_shown_base_is_only_the_masked_name() -> None:
    assert _scope(base_shown="{baseUrl}").base_shown == "{baseUrl}"
    assert _scope(base_shown="{…}").base_shown == "{…}"
    for refused in ("{self.base}", "{http://x}", "baseUrl", "{}", "{a b}", "{{x}}", "{…}x"):
        with pytest.raises(ValidationError):
            _scope(base_shown=refused)


def test_a_scope_path_must_be_relative_and_plain() -> None:
    assert _scope(path="src/a.py").path == "src/a.py"
    for refused in ("/a.py", "../a.py", "a/../b.py", "a\\b.py", ""):
        with pytest.raises(ValidationError):
            _scope(path=refused)
    for refused in ("", "a b", "A..B.c", ".A", "A.", "1A"):
        with pytest.raises(ValidationError):
            _scope(symbol=refused)
    assert _scope(symbol=None).symbol is None
    assert _scope(symbol="$ref._x1").symbol == "$ref._x1"


def test_a_base_key_has_the_hashed_form_only() -> None:
    assert _scope(base_key="hbk1-" + "0123456789abcdef0123").base_key.startswith("hbk1-")
    for refused in ("baseUrl", "hbk1-" + "A" * 20, "hbk1-" + "0" * 19, "hbk1-" + "0" * 21):
        with pytest.raises(ValidationError):
            _scope(base_key=refused)


@pytest.mark.parametrize(
    "value",
    ["", "/", "/api", "/api/", "/v1/items", "api", "/a?x=1", "/a#f", "/{x}", "/a b", "/a\tb"],
)
def test_a_base_path_is_validated_like_the_preview(value: str) -> None:
    preview = dict(
        context=CapabilityContext(scope=CapabilityScope.GLOBAL),
        group="hcg1-" + "0" * 20,
        repository="shop",
    )
    try:
        expected: str | None = HttpConnectionPreviewRequest(**preview, base_path=value).base_path
    except ValidationError:
        expected = None
    if expected is None:
        with pytest.raises(ValidationError):
            _scope(base_path=value)
    else:
        assert _scope(base_path=value).base_path == expected
    assert _scope().base_path == ""


def test_an_a1_link_without_a_scope_validates_unchanged() -> None:
    link = DeploymentLinkFields(label="shop", url="https://shop.example.test/api")
    assert link.scope is None and link.callers == []
    url = "https://shop.example.test/"
    plain = DeploymentLinkFields(label="shop", url=url, callers=["a", "b"])
    assert plain.scope is None and plain.callers == ["a", "b"]


def test_a_scope_conflict_is_a_link_conflict_with_its_own_message() -> None:
    error = ScopeConflictError("another declaration covers this scope")
    assert isinstance(error, LinkConflictError)
    assert str(error) == "another declaration covers this scope"
