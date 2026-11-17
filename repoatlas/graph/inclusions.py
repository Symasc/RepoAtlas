"""Repository inclusions: which indexed repository each stated inclusion is.

Every format :mod:`repoatlas.parsers.inclusions` reads becomes one claim, made at the entry
that states it: *this repository includes, at this path, the repository at this URL*. The
global pass decides each one here, in order, and the first rule that decides wins:

1. **Path.** The entry's path, under what it is relative to, is the root of an indexed
   repository. A ``.gitmodules`` or ``.gitrepo`` path is relative to the including
   repository, and an indexed repository there *is* the checkout, so the path decides alone.
   A workspace manifest's path is relative to a workspace top the index infers (the
   manifest repository's parent; for ``.repo/manifests``, the directory holding ``.repo``),
   so a repository found there decides only when its remotes do not contradict the URL.
2. **Remote.** Exactly one other indexed repository has the entry's URL among its remotes
   (:func:`~repoatlas.gitstate.config.normalize_git_url`; a relative URL resolved against
   the including repository's first remote, else its directory), or is at the local path
   the URL names.
3. **Ambiguous.** More than one does: clones of one remote. Nothing is drawn, and the
   candidates are listed; choosing one would be a guess.
4. **Unresolved.** None does.
5. **Declared.** A user stated which indexed repository the entry is
   (``inclusion_declarations``). Read only for 3 and 4, so a declaration never overrides
   what the path or a remote proves.

A decision of every entry is recorded (``link_decisions``, family
:data:`~repoatlas.models.inclusions.INCLUSION_FAMILY`), and a linked one is also an
``INCLUDES_REPOSITORY`` edge from the declaring file to the included repository, asserted at
the entry's lines. Its resolution records how it was found and what the entry pins -- the
commit in the including repository's index for a submodule, the revision a manifest states
otherwise. Whether the included repository's HEAD is that commit is read when it is asked,
from the HEAD indexed then, so a checkout that moves never leaves a stale answer in an edge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoatlas.gitstate.config import normalize_git_url
from repoatlas.graph.keys import repository_key
from repoatlas.models.inclusions import INCLUSION_FAMILY, InclusionDeclaration
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.repository import Repository
from repoatlas.parsers.inclusions import BASE_REPOSITORY, BASE_WORKSPACE, GITMODULES
from repoatlas.storage.protocols import (
    EntityRecord,
    FileRow,
    LinkDecisionRow,
    UnresolvedRefRecord,
)

#: A pin that names a commit: a full object name, SHA-1 (40) or SHA-256 (64) hexadecimal. A
#: shorter hex string is as likely a branch or tag (``deadbeef``) as an abbreviated commit.
_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


@dataclass(frozen=True, slots=True)
class PendingInclusion:
    """An inclusion edge whose target repository entity is interned in the swap."""

    src_id: int
    target: EntityRecord
    repo_id: int
    file_id: int
    line_start: int | None
    line_end: int | None
    resolution: str


@dataclass(slots=True)
class InclusionPlan:
    edges: list[PendingInclusion] = field(default_factory=list)
    decisions: list[LinkDecisionRow] = field(default_factory=list)
    #: Distinct (including, included) repository pairs linked.
    crossings: int = 0
    linked: int = 0
    unsettled: int = 0


def is_inclusion_claim(claim: UnresolvedRefRecord) -> bool:
    return claim.edge_type is EdgeType.INCLUDES_REPOSITORY


def pin_in_sync(pin: str | None, head: str | None) -> bool | None:
    """Whether ``head`` is the commit ``pin`` names; ``None`` when that cannot be told: no pin
    or head, a pin that is not a full-length commit (a branch, a tag, an abbreviation), or a
    pin and head of different lengths (a SHA-1 pin against a SHA-256 head is no mismatch)."""
    if not pin or not head or not _COMMIT.fullmatch(pin):
        return None
    if len(pin) != len(head):
        return None
    return head.lower() == pin.lower()


def decision_ref(repository: str, file: str, path: str, line: int | None) -> str:
    """Stable across reindexing: where the entry is and what it says, never a row id."""
    material = "\0".join((INCLUSION_FAMILY, repository, file, str(line or 0), path))
    return "inc1-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def workspace_tops(repository: Path) -> tuple[Path, ...]:
    """Where a manifest's workspace-relative paths start: the directory holding ``.repo`` for
    a repo manifest checkout, else the manifest repository's parent."""
    if repository.parts[-2:] == (".repo", "manifests"):
        return (repository.parent.parent,)
    return (repository.parent,)


def plan_inclusions(
    claims: Sequence[UnresolvedRefRecord],
    by_id: Mapping[int, FileRow],
    repositories: Sequence[Repository],
    declarations: Sequence[InclusionDeclaration] = (),
) -> InclusionPlan:
    """Decide every inclusion claim. Writes nothing."""
    plan = InclusionPlan()
    by_repo_id = {r.id: r for r in repositories if r.id is not None and r.slug}
    by_slug = {r.slug: r for r in by_repo_id.values()}
    by_path = {os.path.normpath(str(r.path)): r for r in by_repo_id.values()}
    by_remote: dict[str, list[Repository]] = {}
    for repository in by_repo_id.values():
        for identity in {*repository.remotes, f"file:{os.path.normpath(str(repository.path))}"}:
            by_remote.setdefault(identity, []).append(repository)
    declared = {(d.repository, d.file, d.path): d for d in declarations}
    pairs: set[tuple[int, int]] = set()

    # Claims are told apart by where they are and what path they state (see decision_ref):
    # a second claim at one line for one path, as a one-line flow mapping can make, is
    # dropped, so the decision and its edge are written once and the first one stands.
    stated: set[tuple[int, int, str]] = set()

    for claim in sorted(
        claims, key=lambda c: (c.file_id, c.line_start or 0, c.target_key, c.id or 0)
    ):
        origin = by_id.get(claim.file_id)
        parent = by_repo_id.get(claim.repo_id)
        if origin is None or parent is None or parent.id is None:
            continue
        place = (claim.file_id, claim.line_start or 0, claim.target_key)
        if place in stated:
            continue
        stated.add(place)
        meta: dict[str, Any] = json.loads(claim.meta_json) if claim.meta_json else {}
        path = claim.target_key
        url = meta.get("url")
        base = meta.get("base")
        identity = (
            normalize_git_url(
                url,
                base=parent.remotes[0] if parent.remotes else None,
                base_dir=os.path.normpath(str(parent.path)),
            )
            if url
            else None
        )
        by_url = sorted(
            (r for r in by_remote.get(identity or "", ()) if r.id != parent.id),
            key=lambda r: r.slug,
        )

        found: Repository | None = None
        method: str | None = None
        roots: tuple[Path, ...] = ()
        if base == BASE_REPOSITORY:
            roots = (parent.path,)
        elif base == BASE_WORKSPACE:
            roots = workspace_tops(parent.path)
        for root in roots:
            checkout = by_path.get(os.path.normpath(str(root / path)))
            if checkout is None or checkout.id == parent.id:
                continue
            agrees = (
                base == BASE_REPOSITORY
                or identity is None
                or not checkout.remotes
                or checkout.id in {r.id for r in by_url}
            )
            if agrees:
                found, method = checkout, "path"
                break
        if found is None and len(by_url) == 1:
            found, method = by_url[0], "remote"

        entry = declared.get((parent.slug, origin.rel_path, path))
        declaration: dict[str, Any] | None = None
        if entry is not None:
            declaration = {"id": entry.id, "target": entry.target}
            if found is not None:
                declaration["status"] = "superseded"
            else:
                target = by_slug.get(entry.target)
                if target is not None and target.id != parent.id:
                    found, method = target, "declared"
                    declaration["status"] = "applied"
                else:
                    declaration["status"] = "stale"

        if found is not None:
            decision = "linked"
        elif len(by_url) > 1:
            decision = "ambiguous"
        else:
            decision = "unresolved"

        pin = meta.get("revision")
        if pin is None and meta.get("format") == GITMODULES:
            pin = dict(parent.gitlinks).get(path)
        evidence: dict[str, Any] = {
            "format": meta.get("format"),
            "path": path,
            "url": url,
            "identity": identity,
            "revision": pin,
        }
        # A local identity is an absolute path of this machine, and nothing reads it back.
        if identity is not None and identity.startswith("file:"):
            del evidence["identity"]
        if found is not None:
            evidence.update(method=method, target=found.slug)
        if decision == "ambiguous":
            evidence["candidates"] = [r.slug for r in by_url]
        if declaration is not None:
            evidence["declaration"] = declaration
        plan.decisions.append(
            LinkDecisionRow(
                decision_ref=decision_ref(parent.slug, origin.rel_path, path, claim.line_start),
                family=INCLUSION_FAMILY,
                repo_id=parent.id,
                file_id=origin.file_id,
                src_id=claim.src_id,
                line_start=claim.line_start,
                line_end=claim.line_end,
                subject=path,
                decision=decision,
                evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            )
        )
        if found is None or found.id is None:
            plan.unsettled += 1
            continue
        plan.linked += 1
        pairs.add((parent.id, found.id))
        resolution = {
            "inclusion": {
                key: value
                for key, value in {
                    "format": meta.get("format"),
                    "path": path,
                    "url": url,
                    "revision": pin,
                    "method": method,
                }.items()
                if value is not None
            }
        }
        plan.edges.append(
            PendingInclusion(
                src_id=origin.file_entity_id,
                target=EntityRecord(
                    type=EntityType.REPOSITORY,
                    key=repository_key(found.slug),
                    name=found.name,
                    qualified_name=found.slug,
                    repo_id=found.id,
                ),
                repo_id=parent.id,
                file_id=origin.file_id,
                line_start=claim.line_start,
                line_end=claim.line_end,
                resolution=json.dumps(resolution, sort_keys=True, separators=(",", ":")),
            )
        )
    plan.crossings = len(pairs)
    return plan
