"""Scoped HTTP declarations: which requests a declaration's own words cover (pure rules)."""

from __future__ import annotations

from collections.abc import Mapping

from repoatlas.graph.http_scopes import (
    FileScope,
    ScopeDeclaration,
    ScopeRequest,
    ScopeSymbol,
    base_key,
    base_kind,
    base_text,
    covers,
    root_name,
    scope_refusal,
    symbol_covers,
)

CALLER = "web"
PATH = "src/client.py"


def _scope(*named: tuple[int, str, str], uses: tuple[tuple[int, int], ...] = ()) -> FileScope:
    symbols = [ScopeSymbol(1, 1, "file", PATH), ScopeSymbol(2, 1, "module", "client")]
    symbols += [ScopeSymbol(i, 1, kind, name) for i, kind, name in named]
    return FileScope.build(symbols, uses)


def _declaration(text: str, symbol: str | None = None) -> ScopeDeclaration:
    return ScopeDeclaration(CALLER, PATH, base_key(text), symbol)


def _request(
    text: str, qualified_name: str = "", src_id: int = 9, meta: Mapping[str, object] | None = None
) -> ScopeRequest:
    return ScopeRequest(CALLER, PATH, "{" + text + "}/items", qualified_name, src_id, meta or {})


def test_symbol_prefix_needs_a_dot_boundary():
    assert symbol_covers("A", "A")
    assert symbol_covers("A", "A.m")
    assert not symbol_covers("A", "AB")
    assert not symbol_covers("A", "AB.m")
    scope = _scope((5, "class", "A"), (6, "class", "AB"))
    assert covers(_declaration("base", "A"), _request("base", "AB.m"), scope) == "out-of-scope"
    assert covers(_declaration("base", "A"), _request("base", "A"), scope) == "covered"


def test_truncated_text_never_matches_even_when_its_key_does():
    cut = "b" * 60
    whole = "b" * 59
    assert scope_refusal("{" + cut + "}/x", {}) == "base-text-truncated"
    assert scope_refusal("{" + whole + "}/x", {}) is None
    scope = _scope((5, "function", "f"))
    declaration = _declaration(cut, "f")
    assert declaration.base_key == base_key(cut)
    assert covers(declaration, _request(cut, "f"), scope) == "scope-unidentified"
    assert covers(_declaration(whole, "f"), _request(whole, "f"), scope) == "covered"
    # Unreadable and host-stating texts are refused the same way, whatever their key.
    unreadable = ScopeRequest(CALLER, PATH, "{a{b}}/x", "f", 9)
    assert scope_refusal(unreadable.target_key, {}) == "base-text-unreadable"
    assert covers(_declaration("a{b}", "f"), unreadable, scope) == "scope-unidentified"
    assert scope_refusal("https://{host}/x", {}) == "host-expression"
    assert base_text("https://{host}/x") is None


def test_receiver_needs_a_symbol_and_a_direct_member():
    scope = _scope((5, "class", "C"), (6, "method", "C.m"), (7, "function", "C.m.inner"))
    text = "self.base"
    assert base_kind(text) == "receiver"
    assert covers(_declaration(text), _request(text, "C.m"), scope) == "scope-too-broad"
    declaration = _declaration(text, "C")
    assert covers(declaration, _request(text, "C.m"), scope) == "covered"
    assert covers(declaration, _request(text, "C"), scope) == "covered"
    assert covers(declaration, _request(text, "C.m.inner"), scope) == "scope-too-broad"
    assert base_kind("this['base']") == "receiver"


def test_file_scope_needs_a_uses_edge_to_the_one_module_binding():
    scope = _scope((5, "symbol", "BASE"), uses=((9, 5),))
    assert covers(_declaration("BASE"), _request("BASE", "f", src_id=9), scope) == "covered"
    # Another request of the file that never uses the binding is not covered.
    unused = _request("BASE", "g", src_id=10)
    assert covers(_declaration("BASE"), unused, scope) == "scope-too-broad"
    # A name bound twice is not one binding, however much it is used.
    twice = _scope((5, "symbol", "BASE"), (6, "function", "BASE"), uses=((9, 5), (9, 6)))
    assert covers(_declaration("BASE"), _request("BASE", "f"), twice) == "scope-too-broad"
    # A name that is only the file or its module is not a binding.
    only_module = _scope(uses=((9, 2),))
    assert covers(_declaration("client"), _request("client"), only_module) == "scope-too-broad"


def test_a_qualified_expression_needs_the_same_evidence_as_a_bare_name():
    text = "config.api.base"
    assert base_kind(text) == "qualified"
    assert root_name(text) == "config"
    assert root_name("get_base()") == "get_base"
    assert base_kind("urls[0]") == "qualified"
    declaration = _declaration(text)
    assert covers(declaration, _request(text, "f"), _scope((5, "symbol", "config"))) == (
        "scope-too-broad"
    )
    used = _scope((5, "symbol", "config"), uses=((9, 5),))
    assert covers(declaration, _request(text, "f"), used) == "covered"
    # The evidence is for the root, not for another name the expression passes through.
    other = _scope((5, "symbol", "api"), uses=((9, 5),))
    assert covers(declaration, _request(text, "f"), other) == "scope-too-broad"


def test_a_parameter_in_two_members_is_too_broad_for_their_class():
    scope = _scope((5, "class", "C"), (6, "method", "C.one"), (7, "method", "C.two"))
    assert base_kind("base") == "bare"
    for member in ("C.one", "C.two"):
        too_broad = covers(_declaration("base", "C"), _request("base", member), scope)
        assert too_broad == "scope-too-broad"
        # Declaring the one member that makes the request covers exactly that request.
        declaration = _declaration("base", member)
        assert covers(declaration, _request("base", member), scope) == "covered"
    other = covers(_declaration("base", "C.one"), _request("base", "C.two"), scope)
    assert other == "out-of-scope"


def test_an_injected_request_is_never_covered():
    scope = _scope((5, "function", "f"), (6, "symbol", "BASE"), uses=((9, 6),))
    injected = {"injected_at": "src/app.ts:3"}
    assert scope_refusal("{BASE}/x", injected) == "injected-transport"
    for symbol in (None, "f"):
        request = _request("BASE", "f", meta=injected)
        assert covers(_declaration("BASE", symbol), request, scope) == "injected-root-unselected"
    # The same request without the injection root is covered, so the refusal is its own.
    assert covers(_declaration("BASE", "f"), _request("BASE", "f"), scope) == "covered"


def test_a_symbol_named_twice_is_unidentified():
    twice = _scope((5, "function", "f"), (6, "function", "f"))
    assert covers(_declaration("base", "f"), _request("base", "f"), twice) == "scope-unidentified"
    # A symbol the file does not hold is unidentified too.
    missing = covers(_declaration("base", "g"), _request("base", "g"), _scope())
    assert missing == "scope-unidentified"
    # Other callers and files are out of scope before anything else is read.
    elsewhere = ScopeRequest("api", PATH, "{base}/x", "f", 9)
    assert covers(_declaration("base", "f"), elsewhere, twice) == "out-of-scope"
    moved = ScopeRequest(CALLER, "src/other.py", "{base}/x", "f", 9)
    assert covers(_declaration("base", "f"), moved, twice) == "out-of-scope"
