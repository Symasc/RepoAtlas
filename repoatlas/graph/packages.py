"""Package links: an import that names a package another repository provides (``USES`` only).

A bare specifier that resolves inside its own repository is the import graph's
business (:mod:`repoatlas.graph.modules`). One that does not may still name source
RepoAtlas holds: a package that another managed repository provides. A use of a name
imported from it is then a use of the declaration that package exports, and following
it is how a change in one repository reaches the exact declarations that consume it in
another. A repository is not a package manager's boundary: a path or a workspace may
lead from one repository's manifest into another's, and a package so declared is as
much the consumer's as one it names by version. This module decides when the link is
proven, and it decides it only on what the repositories' own files state:

1. the specifier is a package name, never a path: a relative or absolute specifier is
   the import graph's alone and never leaves its repository;
2. the importing repository does not settle the name itself: no ``paths`` pattern of
   its project's tsconfig claims the specifier, and none of its own manifests provides
   the package;
3. the nearest manifest at or above the importing file that *declares* the package is
   the consumer's evidence, and every row of it for the package is read as the target
   it names (:mod:`repoatlas.graph.declared`). Another package (an alias), a remote
   source or an unreadable form joins nothing. A path pins the provider: it must lead,
   followed from that manifest, to the directory of an indexed manifest providing the
   package, in whichever repository that is. A ``workspace:`` range pins it too: it
   must be the one member of that name of the innermost workspace an indexed file
   declares around the manifest, whichever repository each member is indexed as.
   Otherwise, a version names the package wherever it is provided, and then exactly
   one manifest in all the other repositories must provide the name: two -- in two
   repositories or in one -- is an ambiguity, never a choice;
4. that package's entry for the specifier's subpath is exactly one indexed file
   (:meth:`~repoatlas.graph.modules.ModuleResolver.package_entry`).

Anything short of all four is a named refusal, not a guess: the name joins nothing, and
a pin that fails never falls back to the name. What each step established is recorded
on the edge (``crossing``), so every repository crossing can be read back to the
manifest lines and the entry that proved it; a pinned one also says what pinned it
(``resolved_by``).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from repoatlas.dependencies.normalize import NPM, normalize
from repoatlas.graph.declared import (
    REFUSED_AS,
    TargetKind,
    classify_dependency_target,
    path_target,
)
from repoatlas.graph.modules import (
    ModuleResolver,
    WorkspaceDeclaration,
    member_matcher,
    split_package,
    workspace_declaration,
)
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.parsers.module_facts import MODULE_RESOLUTION
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

#: The ``basis`` a ``USES`` edge's resolution records when a package link proved it.
PACKAGE_BASIS = "package"
#: The resolution key holding a crossing's evidence; the impact walk crosses on nothing else.
CROSSING = "crossing"


@dataclass(frozen=True, slots=True)
class PackageLink:
    """The provider entry a package specifier names, and the evidence that it does."""

    file: FileRow
    #: The ``crossing`` record: package, provider, manifests, entry.
    crossing: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Provision:
    """A manifest that provides a package: where, on which line, under which name."""

    repo_id: int
    directory: str
    manifest: str
    line: int
    name: str
    normalized: str


@dataclass(frozen=True, slots=True)
class _Declaration:
    """The first row a manifest declares a package with, and every constraint it does."""

    manifest: str
    line: int
    constraint: str | None
    constraints: tuple[str, ...] = ()


@lru_cache(maxsize=1 << 16)
def _parent(rel_path: str) -> str:
    parent = str(PurePosixPath(rel_path).parent)
    return "" if parent == "." else parent


class PackageLinks:
    """Decides, for one global pass, which package specifiers cross into another repository."""

    def __init__(
        self,
        modules: ModuleResolver,
        index: ResolutionIndex,
        records: Sequence[DependencyRecord],
    ) -> None:
        self._modules = modules
        self._index = index
        paths = {
            row.file_id: row.rel_path
            for rows in index.files_by_repo.values()
            for row in rows.values()
        }
        self._provided: dict[str, list[Provision]] = defaultdict(list)
        self._provided_in: dict[int, set[str]] = defaultdict(set)
        self._declared: dict[tuple[int, str], dict[str, _Declaration]] = defaultdict(dict)
        constraints: dict[tuple[int, str, str], set[str]] = defaultdict(set)
        located = [
            (record, manifest)
            for record in records
            if record.ecosystem == NPM and (manifest := paths.get(record.file_id)) is not None
        ]
        # Ordered on facts about the repositories, never on row ids.
        located.sort(
            key=lambda item: (
                index.repo_slugs.get(item[0].repo_id, ""),
                item[1],
                item[0].line_start,
                item[0].role,
                item[0].name,
            )
        )
        for record, manifest in located:
            if record.role == "provided":
                provision = Provision(
                    record.repo_id,
                    _parent(manifest),
                    manifest,
                    record.line_start,
                    record.name,
                    record.normalized_name,
                )
                self._provided[record.normalized_name].append(provision)
                self._provided_in[record.repo_id].add(record.normalized_name)
            elif record.role == "declared":
                self._declared[(record.repo_id, _parent(manifest))].setdefault(
                    record.normalized_name,
                    _Declaration(manifest, record.line_start, record.constraint),
                )
                constraints[(record.repo_id, _parent(manifest), record.normalized_name)].add(
                    (record.constraint or "").strip()
                )
        for (repo_id, directory), names in self._declared.items():
            for name, first in names.items():
                every = tuple(sorted(constraints[(repo_id, directory, name)]))
                names[name] = _Declaration(first.manifest, first.line, first.constraint, every)
        self._targets = ProviderTargets(
            {repo_id: str(path) for path, repo_id in index.repo_roots},
            [p for provisions in self._provided.values() for p in provisions],
            modules.workspace_declarations,
            index.repo_slugs,
        )
        self._memo: dict[tuple[int, str, str], PackageLink | str] = {}

    def resolve(self, origin: FileRow, specifier: str) -> PackageLink | str:
        """The provider entry ``specifier`` names from ``origin``, or the refusal's reason."""
        key = (origin.repo_id, _parent(origin.rel_path), specifier)
        found = self._memo.get(key)
        if found is None:
            found = self._resolve(origin, specifier)
            self._memo[key] = found
        return found

    def _resolve(self, origin: FileRow, specifier: str) -> PackageLink | str:
        if not specifier or specifier.startswith((".", "/")):
            return "relative"
        if specifier.startswith("node:") or any(c in specifier for c in "${}\\"):
            return "not-a-package"
        split = split_package(specifier)
        if split is None:
            return "not-a-package"
        name, subpath = split
        wanted = normalize(NPM, name)
        if self._modules.aliased(origin, specifier):
            return "local-alias"
        if wanted in self._provided_in.get(origin.repo_id, set()):
            return "provided-locally"
        declaration = self._declaration(origin, wanted)
        if declaration is None:
            return "dependency-absent"
        targets = [classify_dependency_target(c, wanted) for c in declaration.constraints]
        for target in targets:
            refused = REFUSED_AS.get(target.kind)
            if refused is not None:
                return refused
        pins: dict[Provision, dict[str, str]] = {}
        for target in targets:
            if target.kind is TargetKind.PATH:
                found = self._targets.by_path(
                    origin.repo_id, declaration.manifest, target.path or "", wanted
                )
            elif target.kind is TargetKind.WORKSPACE:
                found = self._targets.by_workspace(origin.repo_id, declaration.manifest, wanted)
            else:
                continue
            if isinstance(found, str):
                return found
            pins.setdefault(*found)
        if len(pins) > 1:
            return "provider-ambiguous"  # two pins that disagree
        if pins:
            provider, pinned = next(iter(pins.items()))
        else:
            providers = [p for p in self._provided.get(wanted, []) if p.repo_id != origin.repo_id]
            if not providers:
                return "provider-absent"
            if len(providers) > 1:
                return "provider-ambiguous"
            provider, pinned = providers[0], {}
        entry = self._modules.package_entry(
            provider.repo_id, provider.directory, subpath, build_output=True
        )
        if entry.file is None:
            return entry.reason or "entry-unresolved"
        crossing: dict[str, Any] = {
            "package": f"{NPM}:{provider.name}",
            "provider": self._index.repo_slugs.get(provider.repo_id, ""),
            "provided_at": f"{provider.manifest}:{provider.line}",
            "entry": entry.file.rel_path,
            "entry_fields": ",".join(entry.fields),
            "declared_at": f"{declaration.manifest}:{declaration.line}",
        }
        if subpath:
            crossing["subpath"] = subpath
        if declaration.constraint:
            crossing["constraint"] = declaration.constraint
        if entry.mapped:
            crossing["entry_mapped"] = dict(entry.mapped)
        crossing.update(pinned)
        return PackageLink(entry.file, crossing)

    def _declaration(self, origin: FileRow, wanted: str) -> _Declaration | None:
        """The nearest manifest at or above ``origin`` that declares ``wanted``."""
        directory = _parent(origin.rel_path)
        while True:
            found = self._declared.get((origin.repo_id, directory), {}).get(wanted)
            if found is not None:
                return found
            if not directory:
                return None
            directory = _parent(directory)


class ProviderTargets:
    """Where a path or a workspace declaration leads, across every indexed repository.

    Shared by package links and by the package-level dependents of a change, so both
    take a declaration to name the same provider. Everything is placed by absolute
    path, followed lexically from each repository's root: a workspace's members are the
    manifests under its root that its globs match, in whichever repository each is
    indexed -- a repository nested inside the workspace is as much a member as a
    directory of the repository declaring it.
    """

    def __init__(
        self,
        roots: Mapping[int, str],
        provisions: Iterable[Provision],
        workspaces: Iterable[WorkspaceDeclaration] = (),
        slugs: Mapping[int, str] | None = None,
    ) -> None:
        #: Each repository's absolute root, innermost first: a path lands in the first
        #: that holds it, never in the repository around that one.
        self._roots = sorted(
            ((root, repo_id) for repo_id, root in roots.items()),
            key=lambda item: (-len(PurePosixPath(item[0]).parts), item[0]),
        )
        self._root_of = dict(roots)
        self._slugs = dict(slugs or {})
        self._provisions = list(provisions)
        #: The provision of each manifest directory, where a path declaration may lead.
        self._at: dict[tuple[int, str], Provision] = {}
        for provision in self._provisions:
            self._at.setdefault((provision.repo_id, provision.directory), provision)
        #: Each workspace by its absolute root; its members by name, built when first asked.
        self._workspaces: dict[str, list[WorkspaceDeclaration]] = defaultdict(list)
        for declared in workspaces:
            root = self._root_of.get(declared.repo_id)
            if root is not None:
                self._workspaces[path_target(root, declared.root, "")].append(declared)
        self._members: dict[WorkspaceDeclaration, dict[str, list[Provision]]] = {}

    def by_path(
        self, repo_id: int, manifest: str, path: str, wanted: str
    ) -> tuple[Provision, dict[str, str]] | str:
        """The manifest a path declared in ``manifest`` leads to, or why none provides ``wanted``.

        ``path-outside-index`` when it lands under no indexed repository,
        ``path-no-package`` when no manifest providing a package is indexed in exactly
        that directory (a directory above or below one, a file, a tarball), and
        ``dependency-elsewhere`` when the manifest there provides another package.
        """
        root = self._root_of.get(repo_id)
        if root is None:
            return "path-outside-index"
        owner = self._owner(path_target(root, _parent(manifest), path))
        if owner is None:
            return "path-outside-index"
        provision = self._at.get(owner)
        if provision is None:
            return "path-no-package"
        if provision.normalized != wanted:
            return "dependency-elsewhere"
        return provision, {"resolved_by": "path"}

    def by_workspace(
        self, repo_id: int, manifest: str, wanted: str
    ) -> tuple[Provision, dict[str, str]] | str:
        """The member ``wanted`` of the workspace holding ``manifest``, or why there is none.

        The workspace is the innermost one whose root holds the declaring manifest, as a
        package manager finds it; an outer one never stands in for it. ``workspace-unindexed``
        when no indexed file declares one there, ``workspace-member-absent`` when it has no
        member of that name, ``provider-ambiguous`` when it has two. Two files declaring a
        workspace at the same root (``pnpm-workspace.yaml`` and ``package.json``) both count.
        """
        root = self._root_of.get(repo_id)
        if root is None:
            return "workspace-unindexed"
        here = path_target(root, _parent(manifest), "")
        holding = [w for w in self._workspaces if here == w or here.startswith(w + "/")]
        if not holding:
            return "workspace-unindexed"
        found: dict[Provision, WorkspaceDeclaration] = {}
        for declared in self._workspaces[max(holding, key=len)]:
            for member in self._members_of(declared).get(wanted, []):
                found.setdefault(member, declared)
        if not found:
            return "workspace-member-absent"
        if len(found) > 1:
            return "provider-ambiguous"  # two members share the name: never a guess
        [(member, declared)] = found.items()
        slug = self._slugs.get(declared.repo_id, "")
        return member, {"resolved_by": "workspace", "workspace": f"{slug}/{declared.file}"}

    def _members_of(self, declared: WorkspaceDeclaration) -> dict[str, list[Provision]]:
        if declared not in self._members:
            root = path_target(self._root_of[declared.repo_id], declared.root, "")
            matches = member_matcher(declared.globs)
            members: dict[str, list[Provision]] = defaultdict(list)
            for provision in self._provisions:
                place = path_target(self._root_of[provision.repo_id], provision.directory, "")
                if place.startswith(root + "/") and matches(place[len(root) + 1 :]):
                    members[provision.normalized].append(provision)
            self._members[declared] = dict(members)
        return self._members[declared]

    def _owner(self, target: str) -> tuple[int, str] | None:
        """The innermost indexed repository holding ``target``, and the path within it."""
        for root, repo_id in self._roots:
            if target == root:
                return repo_id, ""
            if target.startswith(root + "/"):
                return repo_id, target[len(root) + 1 :]
        return None


def stored_targets(
    records: Sequence[DependencyRecord],
    facts: Sequence[UnresolvedRefRecord],
    paths: Mapping[int, str],
    roots: Mapping[int, str],
    slugs: Mapping[int, str],
) -> ProviderTargets:
    """The targets a package link reads, rebuilt from stored rows to answer a query.

    ``records`` are the dependency rows, ``facts`` the module-resolution claims, and
    ``paths`` each of their files' repository-relative path.
    """
    provisions = [
        Provision(r.repo_id, _parent(path), path, r.line_start, r.name, r.normalized_name)
        for r in records
        if r.ecosystem == NPM and r.role == "provided" and (path := paths.get(r.file_id))
    ]
    provisions.sort(key=lambda p: (slugs.get(p.repo_id, ""), p.manifest, p.line, p.name))
    workspaces: list[WorkspaceDeclaration] = []
    for claim in facts:
        meta = json.loads(claim.meta_json) if claim.meta_json else {}
        kind, path = meta.get(MODULE_RESOLUTION), paths.get(claim.file_id)
        if not kind or path is None:
            continue
        declared = json.loads(meta.get("facts") or "{}")
        found = workspace_declaration(kind, declared, claim.repo_id, path)
        if found is not None:
            workspaces.append(found)
    return ProviderTargets(roots, provisions, workspaces, slugs)
