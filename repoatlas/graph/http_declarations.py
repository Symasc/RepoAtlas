"""What an operator's HTTP declarations rest on, read from the join's stored decisions.

Pure: no store, no clock beyond what it is handed. The worklist (:mod:`repoatlas.capabilities.
http_connections`) calls it; nothing here decides a link, which only :mod:`repoatlas.graph.
http_crossings` does at a sync.

* A decision's stored URL is parsed back into an origin, a base expression or neither, and the
  path's shape; only the stated origin is ever shown as a host, and a path is shown with
  identifier-like segments masked (display only: matching uses the shape, never the mask).
* A group key is a hash of the target, so the same origin has the same key after any sync.
* A declaration's state is derived from the last global pass, never stored.
* A request whose base the source does not state also carries what a scoped declaration names
  (its symbol, the identity of its base text) and whether one can; the rules are
  :mod:`repoatlas.graph.http_scopes`', never repeated here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from repoatlas.graph import http_scopes
from repoatlas.graph.http_chain import ESTABLISHED
from repoatlas.graph.http_crossings import (
    BINDING_UNRESOLVED,
    DECLARABLE,
    LINKED,
    SERVICE_UNMATCHED,
    SOURCE_UNRESOLVED,
    UNKNOWN_BASE,
)
from repoatlas.models import http
from repoatlas.models.deployment_targets import (
    DeclarationScope,
    DeploymentLink,
    declared_base,
    is_loopback_url,
)
from repoatlas.models.http_connections import DeclarationReason, DeclarationState
from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import HttpCallDecisionRow

_ABSOLUTE = re.compile(r"^(https?)://([^/?#\s{}]+)(/[^?#\s]*)?$")
_BASE_EXPRESSION = re.compile(r"^(\{[^{}]*\})(/.*)?$")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][\w.]{0,39}$")
#: A segment that reads as an identifier or token, not a word: shown as ``:id``.
_OPAQUE = re.compile(r"^(?:[0-9a-fA-F-]{16,}|[A-Za-z0-9_-]{32,}|\d{9,})$")
MAX_SEGMENT = 48
#: Why a request is not scopable when it is not a base-unknown one (a stated origin, a joined one).
NOT_BASE_UNKNOWN = "not-base-unknown"
#: The entities that are a file's own level rather than a symbol of it.
_FILE_LEVEL = frozenset({EntityType.FILE.value, EntityType.MODULE.value})
#: What a pass can decline a scoped declaration's requests for, most telling first.
_SCOPED_DECLINES = (
    "ambiguous",
    "scope-too-broad",
    "route-unmatched",
    "scope-unidentified",
    "injected-root-unselected",
)

_PYTHON = frozenset({".py", ".pyi"})
_NODE = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"})


def language_of(path: str) -> str:
    """``python``, ``typescript`` (JavaScript included, as the parsers treat them) or ``other``."""
    suffix = PurePosixPath(path).suffix.lower()
    return "python" if suffix in _PYTHON else "typescript" if suffix in _NODE else "other"


@dataclass(frozen=True, slots=True)
class Target:
    """What a stored request URL names: a stated origin, an unstated base expression, or neither."""

    origin: str | None
    base_expression: str | None
    #: The path by segment, ``None`` where a value fills one.
    segments: tuple[str | None, ...]


def parse_target(url: str) -> Target:
    """Read a decision's stored URL (``origin/path`` or ``{base}/path``) without guessing."""
    stated = _ABSOLUTE.match(url)
    if stated is not None:
        scheme, authority, path = stated.group(1), stated.group(2), stated.group(3) or "/"
        canonical = http.client_target(f"{scheme}://{authority}/")
        origin = canonical.origin if canonical is not None else None
        return Target(origin, None, _segments(path) if origin else ())
    held = _BASE_EXPRESSION.match(url)
    if held is not None:
        # A bare base reads no path (``()``); ``{base}/`` asks for the root, as the parsers do.
        return Target(None, held.group(1), _segments(held.group(2)) if held.group(2) else ())
    return Target(None, None, _segments(url) if url.startswith("/") else ())


def _segments(path: str) -> tuple[str | None, ...]:
    """The path by segment as the parsers record it: the root is ``("",)``."""
    return tuple(None if "{" in part else part for part in path.split("/")[1:])


def _hole(text: str) -> str:
    inner = text.strip("{}")
    return text if _PLAIN_NAME.match(inner) else "{…}"


def safe_route(url: str) -> str:
    """The request's path as a template for display: holes by name when that is a plain
    identifier, identifier-like literals as ``:id``, long literals cut. Never a query."""
    target = parse_target(url)
    shown: list[str] = []
    for raw, segment in zip(_raw_segments(url), target.segments, strict=False):
        if segment is None:
            shown.append(_hole(raw))
        elif _OPAQUE.match(segment):
            shown.append(":id")
        else:
            shown.append(segment if len(segment) <= MAX_SEGMENT else segment[:MAX_SEGMENT] + "…")
    return "/" + "/".join(shown)


def _raw_segments(url: str) -> list[str]:
    stated = _ABSOLUTE.match(url)
    if stated is not None:
        path = stated.group(3) or "/"
    else:
        held = _BASE_EXPRESSION.match(url)
        path = (held.group(2) or "/") if held is not None else url
    parts = path.split("/")[1:]
    return [] if parts == [""] else parts


def display_base(expression: str | None) -> str | None:
    """A base expression for display: its name when plain, otherwise only that it is a value."""
    return None if expression is None else _hole(expression)


def group_key(target: Target) -> str:
    """The worklist group of a request: its stated origin, or its base expression."""
    token = target.origin if target.origin is not None else f"expr:{target.base_expression or ''}"
    return "hcg1-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]


def refusal_reason(chain_json: str) -> str:
    """Why the join refused: the reason of the first hop it could not establish, else that hop."""
    try:
        chain = json.loads(chain_json or "[]")
    except json.JSONDecodeError:
        return ""
    for hop in chain:
        if isinstance(hop, dict) and hop.get("status") not in ESTABLISHED:
            return str(hop.get("reason") or f"{hop.get('kind', '')}:{hop.get('status', '')}")
    return ""


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """One stored decision, read for the worklist."""

    row: HttpCallDecisionRow
    repository: str
    target: Target
    status: str
    language: str
    reason: str
    declaration_ids: tuple[int, ...]
    declaration_outcome: str | None
    joined_by: int | None
    #: The qualified name of the entity that asks; ``None`` at module level (the file's own).
    symbol: str | None = None
    #: The identity of the base text of a request whose base the source does not state, else
    #: ``None``. A hash: the text itself is never carried.
    base_key: str | None = None
    #: Whether a scoped declaration can name this request's base, and if not why.
    scopable: bool = False
    scope_refusal: str | None = None

    @property
    def declarable(self) -> bool:
        return self.target.origin is not None and self.status in DECLARABLE

    @property
    def linked(self) -> bool:
        return self.status == LINKED

    @property
    def by_declaration(self) -> bool:
        return self.joined_by is not None


def facts_of(row: HttpCallDecisionRow, repository: str) -> RequestFacts:
    evidence = _evidence(row.evidence_json)
    ids = tuple(int(i) for i in str(evidence.get("declaration_ids", "")).split(",") if i.isdigit())
    joined = evidence.get("link_id") if row.decision == LINKED else None
    target = parse_target(row.url)
    unstated = target.origin is None and row.decision == UNKNOWN_BASE
    text = http_scopes.base_text(row.url) if unstated else None
    refusal = http_scopes.scope_refusal(row.url, evidence) if unstated else NOT_BASE_UNKNOWN
    return RequestFacts(
        row=row,
        repository=repository,
        target=target,
        status=row.decision,
        language=language_of(row.path or ""),
        reason=refusal_reason(row.chain_json),
        declaration_ids=ids,
        declaration_outcome=(
            str(evidence["declaration_outcome"]) if "declaration_outcome" in evidence else None
        ),
        joined_by=int(joined) if isinstance(joined, str) and joined.isdigit() else None,
        symbol=(row.src_qualified_name or None) if row.src_type not in _FILE_LEVEL else None,
        base_key=http_scopes.base_key(text) if text is not None else None,
        scopable=refusal is None,
        scope_refusal=refusal,
    )


def _evidence(text: str) -> Mapping[str, object]:
    try:
        loaded = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def declaration_state(
    link: DeploymentLink,
    *,
    pass_reads: Mapping[int, str] | None,
    indexed: bool,
    serves_routes: bool,
    requests_at_base: int,
    joined: int,
    declined: Mapping[str, int],
) -> tuple[DeclarationState, DeclarationReason]:
    """Where a declaration stands against the last global pass; the first rule that holds."""
    if not indexed:
        return "invalidated", "repository-not-indexed"
    if pass_reads is None or pass_reads.get(link.id) != link.updated_at.isoformat():
        return "pending", "applies-at-next-sync"
    if joined:
        return "applied", ""
    if link.url is None:
        return "ineffective", "target-url-unresolved"
    if not serves_routes:
        return "invalidated", "repository-serves-no-routes"
    if requests_at_base == 0:
        return "invalidated", "origin-no-longer-requested"
    for reason in ("ambiguous", "callers-excluded", "route-unmatched", "source-established"):
        if declined.get(reason):
            return "ineffective", reason  # type: ignore[return-value]
    return "ineffective", "no-request-matched"


def scope_found(file: http_scopes.FileScope | None, scope: DeclarationScope) -> bool:
    """Whether the declared file is indexed and, when a symbol is named, that symbol names
    exactly one entity of it (the identification the join needs)."""
    if file is None:
        return False
    return scope.symbol is None or file.entity_count(scope.symbol) == 1


def base_requested(facts: Iterable[RequestFacts], caller: str, scope: DeclarationScope) -> bool:
    """Whether some request of the caller still leaves its base unstated, on the declared base
    text, in the declared file and (when one is named) inside the declared symbol."""
    return any(
        f.repository == caller
        and f.row.path == scope.path
        and f.status == UNKNOWN_BASE
        and f.base_key == scope.base_key
        and (scope.symbol is None or http_scopes.symbol_covers(scope.symbol, f.symbol or ""))
        for f in facts
    )


def scoped_declaration_state(
    link: DeploymentLink,
    *,
    pass_reads: Mapping[int, str] | None,
    indexed: bool,
    caller_indexed: bool,
    serves_routes: bool,
    found: bool,
    requested: bool,
    joined: int,
    declined: Mapping[str, int],
) -> tuple[DeclarationState, DeclarationReason]:
    """Where a scoped declaration stands against the last global pass; the first rule that holds.

    ``found``: its file, and its symbol if it names one, still identify a scope
    (:func:`scope_found`); ``requested``: some request still leaves that base unstated in it
    (:func:`base_requested`). The URL rules do not apply: it states no origin, so nothing is
    judged by the requests at one."""
    if not indexed:
        return "invalidated", "repository-not-indexed"
    if not caller_indexed:
        return "invalidated", "caller-not-indexed"
    if pass_reads is None or pass_reads.get(link.id) != link.updated_at.isoformat():
        return "pending", "applies-at-next-sync"
    if joined:
        return "applied", ""
    if not serves_routes:
        return "invalidated", "repository-serves-no-routes"
    if not found:
        return "invalidated", "scope-not-found"
    if not requested:
        return "invalidated", "base-no-longer-requested"
    for reason in _SCOPED_DECLINES:
        if declined.get(reason):
            return "ineffective", reason  # type: ignore[return-value]
    return "ineffective", "no-request-matched"


def origin_of_link(link: DeploymentLink) -> str | None:
    """The origin a link's URL names, for matching it to a group."""
    base = declared_base(link.url) if link.url else None
    if base is None:
        return None
    target = http.client_target(base + "/")
    return target.origin if target is not None else None


def is_loopback_origin(origin: str | None) -> bool:
    return origin is not None and is_loopback_url(origin)


__all__ = [
    "BINDING_UNRESOLVED",
    "DECLARABLE",
    "NOT_BASE_UNKNOWN",
    "SERVICE_UNMATCHED",
    "SOURCE_UNRESOLVED",
    "RequestFacts",
    "Target",
    "base_requested",
    "declaration_state",
    "display_base",
    "facts_of",
    "group_key",
    "is_loopback_origin",
    "language_of",
    "origin_of_link",
    "parse_target",
    "refusal_reason",
    "safe_route",
    "scope_found",
    "scoped_declaration_state",
]
