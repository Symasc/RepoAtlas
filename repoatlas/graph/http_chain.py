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

Each hop is ``proven``, ``declared``, ``version_unverified``, ``unresolved``, ``ambiguous`` or
``not_reached``:

* a proven hop cites where the source proves it -- a place resting on RepoAtlas's own
  version-specific semantics of a CI action says so with its ``basis`` (``builtin``);
* a declared hop (DF2) cites where the operator's declaration supplies what no indexed
  source proves, and every place of it carries the basis ``declared``;
* a ``version_unverified`` hop (accepted from DF2, written from DF4) rests on an indexed
  implementation not proven to be the referenced version;
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

A CI-established deployment (DF2, :mod:`repoatlas.graph.deploy_artifacts`) spans
repositories: the chart and the release in one, the image's build and its code in another,
an operator declaration in a third. Its claims name each place's repository with a sibling
``<key>_repo`` and any basis but source with ``<key>_basis``; a claim without them is read
exactly as before. It adds ``values_in`` (the host's effective value) to the identity,
``declared_in`` to the binding, and ``workload_in``, ``image_in``, ``published_in`` and
``built_in`` to the source binding.
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
#: DF2 (K1): a hop an operator declaration supplies, and one an indexed implementation
#: supplies without proof that it is the referenced version (written from DF4).
DECLARED = "declared"
VERSION_UNVERIFIED = "version_unverified"
#: The hop statuses a surface may show a link through: established, by source or otherwise.
ESTABLISHED = frozenset({PROVEN, DECLARED, VERSION_UNVERIFIED})

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
    # DF2: the artifact a CI-established deployment runs. Each says what is configured,
    # never what was built, pushed or run.
    "image_unread": "the image the deployment's workload runs is not read",
    "registry_unresolved": "the registry of the image the deployment runs is not stated literally",
    "publisher_unresolved": "no indexed build is configured to publish this image",
    "artifact_ambiguous": "more than one indexed build is configured to publish this image",
    "tag_unmatched": "every indexed build configured to publish this image names another version",
    "workload_unmatched": "the deployment's Service does not select exactly one workload container",
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
    #: What proves it, when not the source itself: ``builtin``, ``declared`` or
    #: ``version-unverified``. Written only when set, so a source chain reads as before.
    basis: str = ""

    def as_json(self) -> dict[str, object]:
        found: dict[str, object] = {
            "repo_id": self.repo_id,
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "role": self.role,
        }
        if self.basis:
            found["basis"] = self.basis
        return found


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
                        basis=str(p.get("basis", "")),
                    )
                    for p in item.get("provenance", ())
                ),
                reason=str(item.get("reason", "")),
                detail=str(item.get("detail", "")),
                entity_id=item.get("entity_id"),
            )
        )
    return tuple(hops)


def places(
    repo_id: int, value: str, role: str, *, path: str | None = None, basis: str = ""
) -> list[Provenance]:
    """Each place ``value`` names: ``path:line`` or a bare path, several joined by ``; ``;
    or, given the file's ``path``, a bare line in it."""
    found: list[Provenance] = []
    for part in (piece.strip() for piece in value.split(";")):
        if not part:
            continue
        if path is not None:
            if part.isdigit():
                found.append(Provenance(repo_id, path, int(part), int(part), role, basis))
            continue
        name, _, line = part.rpartition(":")
        if name and line.isdigit():
            found.append(Provenance(repo_id, name, int(line), int(line), role, basis))
        else:
            found.append(Provenance(repo_id, part, None, None, role, basis))
    return found


def _at(claim: UnresolvedRefRecord, meta: Mapping[str, str], key: str) -> list[Provenance]:
    """The places ``meta[key]`` names, in the repository and with the basis its siblings
    ``<key>_repo`` and ``<key>_basis`` give (DF2), else in the claim's own repository."""
    repo = meta.get(f"{key}_repo", "")
    return places(
        int(repo) if repo.isdigit() else claim.repo_id,
        meta.get(key, ""),
        key,
        basis=meta.get(f"{key}_basis", ""),
    )


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
    #: DF2: the binding's and the source's hop status when not proven by source.
    binding_status: str = PROVEN
    source_status: str = PROVEN


def identity_evidence(
    claim: UnresolvedRefRecord, meta: Mapping[str, str], row: FileRow
) -> IdentityEvidence:
    repo = claim.repo_id
    identity = [
        *_at(claim, meta, "identity_in"),
        *places(repo, meta.get("identity_at", ""), "identity_at", path=row.rel_path),
        *_at(claim, meta, "scheme_in"),
        *_at(claim, meta, "backend_in"),
        *_at(claim, meta, "values_in"),
    ]
    source = [
        *_at(claim, meta, "workload_in"),
        *_at(claim, meta, "image_in"),
        *_at(claim, meta, "published_in"),
        *_at(claim, meta, "built_in"),
        *_at(claim, meta, "source_in"),
        *places(repo, meta.get("bound_at", ""), "bound_at", path=row.rel_path),
    ]
    return IdentityEvidence(
        identity=_unique(identity),
        binding=_unique([*_at(claim, meta, "binding_in"), *_at(claim, meta, "declared_in")]),
        source=_unique(source),
        detail=meta.get("pattern") or meta.get("deployment", ""),
        binding_status=DECLARED if meta.get("binding_basis") == "declared" else PROVEN,
        source_status=DECLARED if meta.get("build_basis") == "declared" else PROVEN,
    )


def _status(statuses: Iterable[str]) -> str:
    """Several claims' status for one hop: declared when any rests on a declaration."""
    found = set(statuses)
    if DECLARED in found:
        return DECLARED
    return VERSION_UNVERIFIED if VERSION_UNVERIFIED in found else PROVEN


def merged(evidence: Sequence[IdentityEvidence]) -> IdentityEvidence:
    return IdentityEvidence(
        identity=_unique(p for e in evidence for p in e.identity),
        binding=_unique(p for e in evidence for p in e.binding),
        source=_unique(p for e in evidence for p in e.source),
        detail="; ".join(dict.fromkeys(e.detail for e in evidence if e.detail)),
        binding_status=_status(e.binding_status for e in evidence),
        source_status=_status(e.source_status for e in evidence),
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
    """The identity, binding and source hops an identity with a source tie establishes:
    proven, or declared where an operator declaration supplies them (DF2)."""
    return [
        Hop(SERVICE_IDENTITY, PROVEN, evidence.identity, detail=evidence.detail),
        # A claim with no separate binding states its deployment where it states its identity.
        Hop(DEPLOYMENT_BINDING, evidence.binding_status, evidence.binding or evidence.identity),
        Hop(SOURCE_BINDING, evidence.source_status, evidence.source),
    ]


def unreached(*kinds: str) -> list[Hop]:
    return [Hop(kind, NOT_REACHED) for kind in kinds]
