"""The one shape every HTTP fact takes on its way out of a parser (HTTP linkage).

Shared by the specification, Python and TypeScript extractors so that "which
library said this" and "can one span prove it" are recorded identically
everywhere. The linker reads exactly two things from ``meta``:

* ``provenance`` — ``exact`` when the reference's span contains every literal
  that determines its target, ``composed`` when the target was assembled from
  literals in different places (a server URL and a path, a router prefix and a
  decorator). A composed fact is stored with no span at all, which leaves it a
  graph fact that nothing can cite, rather than a citation of text that does not
  say what it is quoted for;
* ``source`` — the specification format, framework or client library, kept for
  inspection and never used to decide identity.

**Pending facts.** Some HTTP facts cannot be proven from one file: a NestJS
controller serves nothing until an application registers it, and a request made
through an imported helper is only a request once the helper is read. A parser
records such a fact with :func:`pending_reference`, which tags it with the role it
plays under ``pending_fact``. The linker keeps every such reference as a claim —
never interning it and never resolving it as a symbol — and the global pass
(:mod:`repoatlas.graph.http_claims`) decides, from everything indexed, whether it
is proven. A claim that is never proven stays exactly that: a recorded
declaration, not a fact on a shared node.
"""

from __future__ import annotations

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference

EXACT = "exact"
COMPOSED = "composed"

#: The ``meta`` key that marks a reference as a pending claim for the global pass.
PENDING_FACT = "pending_fact"

#: A route a NestJS controller declares, relative to wherever it is mounted.
NESTJS_ROUTE = "nestjs-route"
#: A class this file declares to be a NestJS module (a declaration, not a claim).
NESTJS_MODULE = "nestjs-module"
#: One entry of a module's ``controllers`` list.
NESTJS_CONTROLLER = "nestjs-controller"
#: One entry of a module's ``imports`` list.
NESTJS_IMPORT = "nestjs-import"
#: The root module of an application ``NestFactory.create`` builds.
NESTJS_ROOT = "nestjs-root"
#: A construct that can change routes from outside the registration it touches.
NESTJS_MARKER = "nestjs-marker"


def pending_reference(
    edge_type: EdgeType,
    target_kind: EntityType,
    target_key: str,
    line_start: int,
    line_end: int,
    *,
    role: str,
    source_symbol: str | None = None,
    **meta: str,
) -> ParsedReference:
    """A fact only the global pass can prove, kept as a claim until it does."""
    return ParsedReference(
        edge_type=edge_type,
        target_kind=target_kind,
        target_key=target_key,
        line_start=line_start,
        line_end=max(line_start, line_end),
        source_symbol=source_symbol,
        meta={PENDING_FACT: role, **meta},
    )


def http_reference(
    edge: EdgeType,
    kind: EntityType,
    body: str,
    line_start: int,
    line_end: int,
    *,
    source: str,
    composed: bool,
    source_symbol: str | None = None,
) -> ParsedReference:
    return ParsedReference(
        edge_type=edge,
        target_kind=kind,
        target_key=body,
        line_start=line_start,
        line_end=max(line_start, line_end),
        source_symbol=source_symbol,
        meta={"source": source, "provenance": COMPOSED if composed else EXACT},
    )
