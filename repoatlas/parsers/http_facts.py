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
"""

from __future__ import annotations

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference

EXACT = "exact"
COMPOSED = "composed"


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
