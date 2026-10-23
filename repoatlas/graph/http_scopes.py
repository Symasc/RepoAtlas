"""Which requests an operator's scoped HTTP declaration covers.

Pure and a leaf: stdlib and models only, so the sync join and the worklist can both call it and
neither imports the other. A declaration names a caller repository, a file, the text of an
unstated base expression and, optionally, one symbol of that file; a request is covered only
when the declaration's own words fit it, and any doubt is a refusal that names its reason.

* The base text is compared by a hash of the whole text. A text that may have been cut, that
  cannot be read as ``{expression}``, or that states a host is never compared at all.
* A symbol must name exactly one entity of its file, and covers its own members at a dot
  boundary (``A`` covers ``A.m``, never ``AB``).
* Rule B1: a receiver base (``self.x``, ``this.x``) is covered only by a symbol and only for
  that symbol's own body and direct members; any other base is covered by a symbol that is the
  request's own enclosing symbol, or, with no symbol, by the one module-level binding it names
  when the request's code is proven to use it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import NamedTuple

from repoatlas.models.parsing import EntityType

#: A base text this long or longer may have been cut where the parser stopped reading.
MAX_BASE_TEXT = 60
#: The role of a claim derived at a sync for a declaration's exposure, never stored.
HTTP_SCOPED_EXPOSURE = "http-scoped-exposure"

COVERED = "covered"
OUT_OF_SCOPE = "out-of-scope"
SCOPE_UNIDENTIFIED = "scope-unidentified"
SCOPE_TOO_BROAD = "scope-too-broad"
INJECTED_ROOT_UNSELECTED = "injected-root-unselected"

# Kept in step with http_declarations._BASE_EXPRESSION; not imported, to stay a leaf.
_BASE_EXPRESSION = re.compile(r"^(\{[^{}]*\})(/.*)?$")
_HOST_EXPRESSION = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://.*\{")
_RECEIVER = re.compile(r"^(?:self|cls|this)[.\[]")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_NOT_ENTITIES = frozenset({EntityType.FILE.value, EntityType.MODULE.value})


def base_text(target_key: str) -> str | None:
    """The text inside the leading ``{...}`` of a stored target, else ``None``."""
    held = _BASE_EXPRESSION.match(target_key)
    return held.group(1)[1:-1] if held is not None else None


def scope_refusal(target_key: str, meta: Mapping[str, object]) -> str | None:
    """Why a request's base text cannot be compared, else ``None``."""
    text = base_text(target_key)
    if text is not None and len(text) >= MAX_BASE_TEXT:
        return "base-text-truncated"
    if text is None:
        return "host-expression" if _HOST_EXPRESSION.match(target_key) else "base-text-unreadable"
    if "injected_at" in meta:
        return "injected-transport"
    return None


def base_key(text: str) -> str:
    """The identity of a base text: the same text has the same key after any sync."""
    return "hbk1-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def base_kind(text: str) -> str:
    """``receiver`` (``self.x``), ``bare`` (one name) or ``qualified`` (anything else)."""
    if _RECEIVER.match(text):
        return "receiver"
    return "bare" if _IDENTIFIER.fullmatch(text) else "qualified"


def root_name(text: str) -> str:
    """The leading identifier of a base text (``config`` for ``config.api.base``), else ``""``."""
    held = _IDENTIFIER.match(text)
    return held.group(0) if held is not None else ""


def symbol_covers(declared: str, actual: str) -> bool:
    """Whether ``actual`` is ``declared`` or lies inside it, at a dot boundary."""
    return actual == declared or actual.startswith(declared + ".")


class ScopeSymbol(NamedTuple):
    """One symbol of a file, as the store holds it."""

    id: int
    file_id: int
    type: str
    qualified_name: str


@dataclass(frozen=True, slots=True)
class FileScope:
    """What one file offers a declaration: its named entities and its USES pairs."""

    entities: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    uses: frozenset[tuple[int, int]] = frozenset()

    @classmethod
    def build(
        cls, symbols: Iterable[ScopeSymbol], uses: Iterable[tuple[int, int]] = ()
    ) -> FileScope:
        named: dict[str, list[int]] = {}
        for symbol in symbols:
            if symbol.type not in _NOT_ENTITIES:
                named.setdefault(symbol.qualified_name, []).append(symbol.id)
        return cls({name: tuple(ids) for name, ids in named.items()}, frozenset(uses))

    def entity_ids(self, qualified_name: str) -> tuple[int, ...]:
        return self.entities.get(qualified_name, ())

    def entity_count(self, qualified_name: str) -> int:
        return len(self.entity_ids(qualified_name))

    def has_use(self, src_id: int, dst_id: int) -> bool:
        return (src_id, dst_id) in self.uses


@dataclass(frozen=True, slots=True)
class ScopeDeclaration:
    """The words of a declaration that bound its scope."""

    caller: str
    path: str
    base_key: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeRequest:
    """A stored request decision, as far as scope reads it."""

    repository: str
    path: str
    target_key: str
    #: The request's own enclosing symbol, ``""`` at module level.
    qualified_name: str = ""
    #: The entity that makes the request, the source of its USES pairs.
    src_id: int = 0
    meta: Mapping[str, object] = field(default_factory=dict)


def covers(declaration: ScopeDeclaration, request: ScopeRequest, scope: FileScope) -> str:
    """``covered``, or the first reason the declaration does not cover the request."""
    if request.repository != declaration.caller or request.path != declaration.path:
        return OUT_OF_SCOPE
    refusal = scope_refusal(request.target_key, request.meta)
    text = base_text(request.target_key)
    if text is None or refusal not in (None, "injected-transport"):
        return SCOPE_UNIDENTIFIED
    if base_key(text) != declaration.base_key:
        return OUT_OF_SCOPE
    symbol, own = declaration.symbol, request.qualified_name
    if symbol is not None:
        if scope.entity_count(symbol) != 1:
            return SCOPE_UNIDENTIFIED
        if not symbol_covers(symbol, own):
            return OUT_OF_SCOPE
    if refusal == "injected-transport":
        return INJECTED_ROOT_UNSELECTED
    if base_kind(text) == "receiver":
        if symbol is None:
            return SCOPE_TOO_BROAD
        member = own.removeprefix(symbol + ".")
        return COVERED if own == symbol or "." not in member else SCOPE_TOO_BROAD
    if symbol is not None:
        return COVERED if own == symbol else SCOPE_TOO_BROAD
    bound = scope.entity_ids(root_name(text))
    if len(bound) == 1 and scope.has_use(request.src_id, bound[0]):
        return COVERED
    return SCOPE_TOO_BROAD
