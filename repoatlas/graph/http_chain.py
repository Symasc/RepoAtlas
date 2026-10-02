"""The evidence chain behind one HTTP call decision (HTTP linkage).

A decision of the cross-repository join (:mod:`repoatlas.graph.http_crossings`) rests on
seven hops, in this order:

* the **caller** making the request;
* the **origin** it states: at the call, at a constant or literal alternative
  (``base_at``), or through an injected transport (``injected_at``, ``base_in``);
* the **service identity** that origin is;
* the **deployment binding** that sets the identity up;
* the **source binding** that ties it to code;
* the **route** that code serves;
* the **handler** serving it.

Each hop is ``proven``, ``unresolved``, ``ambiguous`` or ``not_reached``:

* a proven hop cites where the source proves it;
* an unresolved or ambiguous hop gives a reason code, the text behind it, and whatever
  partial provenance there is;
* a hop not reached cites nothing, because nothing was established there.

A link proves all seven. A refusal marks the hop that failed first; a later hop the
record proved anyway (an Ingress chart whose source tie holds) keeps its status.

The identity claims say what proves each identity-related hop under source-agnostic
keys, so this module never asks which kind of deployment stated them:

* ``identity_in`` (with ``scheme_in`` and ``backend_in``) for the identity;
* ``binding_in`` for the deployment binding;
* ``source_in`` for the source binding.

These are ``path:line`` in the claim's repository, or a bare path for a binding a file
proves as a whole. ``identity_at`` and ``bound_at`` are lines in the claim's own file. A
claim with no ``binding_in`` states its deployment where it states its identity, as a
compose file or a specification's host does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

CALLER = "caller"
ORIGIN = "origin"
SERVICE_IDENTITY = "service_identity"
DEPLOYMENT_BINDING = "deployment_binding"
SOURCE_BINDING = "source_binding"
ROUTE = "route"
HANDLER = "handler"
#: The hops of every chain, in order.
HOPS = (CALLER, ORIGIN, SERVICE_IDENTITY, DEPLOYMENT_BINDING, SOURCE_BINDING, ROUTE, HANDLER)

PROVEN = "proven"
UNRESOLVED = "unresolved"
AMBIGUOUS = "ambiguous"
NOT_REACHED = "not_reached"

#: Why a hop is unresolved or ambiguous. Stable codes: a surface may show them under any
#: scope, since they name a kind of failure and nothing of the source.
REASONS: dict[str, str] = {
    "host_not_stated": "the source does not state the request's host",
    "claim_unread": "the request is not read",
    "no_identity": "no identity, pattern or published service has this origin",
    "several_identities": "more than one identity could be what this origin reaches",
    "binding_unresolved": "no locally established deployment resolves this identity",
    "source_unresolved": "the identity reaches no code",
    "route_unmatched": "the service publishes no route matching the request",
    "several_routes": "more than one route of the service matches the request",
}


@dataclass(frozen=True, slots=True)
class Provenance:
    """One place the source proves a hop: lines of a file, or a whole file."""

    repo_id: int
    path: str
    line_start: int | None
    line_end: int | None
    #: What the place is, as the claim names it (``called_at``, ``identity_in``, …).
    role: str

    def as_json(self) -> dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class Hop:
    kind: str
    status: str
    provenance: tuple[Provenance, ...] = ()
    #: A :data:`REASONS` code, for an unresolved or ambiguous hop.
    reason: str = ""
    #: What the source says at this hop: a deployment, a pattern, the unresolved values.
    detail: str = ""
    #: The handler's entity, on a proven handler hop.
    entity_id: int | None = None

    def as_json(self) -> dict[str, object]:
        found: dict[str, object] = {"kind": self.kind, "status": self.status}
        if self.provenance:
            found["provenance"] = [item.as_json() for item in self.provenance]
        if self.reason:
            found["reason"] = self.reason
        if self.detail:
            found["detail"] = self.detail
        if self.entity_id is not None:
            found["entity_id"] = self.entity_id
        return found


def chain_json(chain: Sequence[Hop]) -> str:
    return json.dumps([hop.as_json() for hop in chain], sort_keys=True)


def chain_from_json(text: str) -> tuple[Hop, ...]:
    hops: list[Hop] = []
    for item in json.loads(text or "[]"):
        hops.append(
            Hop(
                kind=str(item["kind"]),
                status=str(item["status"]),
                provenance=tuple(
                    Provenance(
                        repo_id=int(p["repo_id"]),
                        path=str(p["path"]),
                        line_start=p.get("line_start"),
                        line_end=p.get("line_end"),
                        role=str(p.get("role", "")),
                    )
                    for p in item.get("provenance", ())
                ),
                reason=str(item.get("reason", "")),
                detail=str(item.get("detail", "")),
                entity_id=item.get("entity_id"),
            )
        )
    return tuple(hops)


def places(repo_id: int, value: str, role: str, *, path: str | None = None) -> list[Provenance]:
    """Each place ``value`` names: ``path:line`` or a bare path, several joined by ``; ``;
    or, given the file's ``path``, a bare line in it."""
    found: list[Provenance] = []
    for part in (piece.strip() for piece in value.split(";")):
        if not part:
            continue
        if path is not None:
            if part.isdigit():
                found.append(Provenance(repo_id, path, int(part), int(part), role))
            continue
        name, _, line = part.rpartition(":")
        if name and line.isdigit():
            found.append(Provenance(repo_id, name, int(line), int(line), role))
        else:
            found.append(Provenance(repo_id, part, None, None, role))
    return found


def _unique(items: Iterable[Provenance]) -> tuple[Provenance, ...]:
    return tuple(dict.fromkeys(items))


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """What one identity claim proves at the identity, binding and source hops."""

    identity: tuple[Provenance, ...]
    binding: tuple[Provenance, ...]
    source: tuple[Provenance, ...]
    #: The deployment or pattern the claim is, for the identity hop's detail.
    detail: str


def identity_evidence(
    claim: UnresolvedRefRecord, meta: Mapping[str, str], row: FileRow
) -> IdentityEvidence:
    repo = claim.repo_id
    identity = [
        *places(repo, meta.get("identity_in", ""), "identity_in"),
        *places(repo, meta.get("identity_at", ""), "identity_at", path=row.rel_path),
        *places(repo, meta.get("scheme_in", ""), "scheme_in"),
        *places(repo, meta.get("backend_in", ""), "backend_in"),
    ]
    source = [
        *places(repo, meta.get("source_in", ""), "source_in"),
        *places(repo, meta.get("bound_at", ""), "bound_at", path=row.rel_path),
    ]
    return IdentityEvidence(
        identity=_unique(identity),
        binding=_unique(places(repo, meta.get("binding_in", ""), "binding_in")),
        source=_unique(source),
        detail=meta.get("pattern") or meta.get("deployment", ""),
    )


def merged(evidence: Sequence[IdentityEvidence]) -> IdentityEvidence:
    return IdentityEvidence(
        identity=_unique(p for e in evidence for p in e.identity),
        binding=_unique(p for e in evidence for p in e.binding),
        source=_unique(p for e in evidence for p in e.source),
        detail="; ".join(dict.fromkeys(e.detail for e in evidence if e.detail)),
    )


def caller_hop(claim: UnresolvedRefRecord, row: FileRow | None) -> Hop:
    if row is None:
        return Hop(CALLER, PROVEN)
    return Hop(
        CALLER,
        PROVEN,
        (Provenance(claim.repo_id, row.rel_path, claim.line_start, claim.line_end, "called_at"),),
    )


def origin_places(
    claim: UnresolvedRefRecord, meta: Mapping[str, str], row: FileRow | None
) -> tuple[Provenance, ...]:
    """Where the request's origin is stated: a constant or alternative's line, an injected
    transport's root and base, or the call itself."""
    if row is None:
        return ()
    repo = claim.repo_id
    found = [
        *places(repo, meta.get("base_at", ""), "base_at", path=row.rel_path),
        *places(repo, meta.get("injected_at", ""), "injected_at"),
        *places(repo, meta.get("base_in", ""), "base_in"),
    ]
    return _unique(found)


def proven_identity(evidence: IdentityEvidence) -> list[Hop]:
    """The identity, binding and source hops an identity with a source tie proves."""
    return [
        Hop(SERVICE_IDENTITY, PROVEN, evidence.identity, detail=evidence.detail),
        # A claim with no separate binding states its deployment where it states its identity.
        Hop(DEPLOYMENT_BINDING, PROVEN, evidence.binding or evidence.identity),
        Hop(SOURCE_BINDING, PROVEN, evidence.source),
    ]


def unreached(*kinds: str) -> list[Hop]:
    return [Hop(kind, NOT_REACHED) for kind in kinds]
