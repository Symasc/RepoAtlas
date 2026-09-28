"""Package links: an import that names a package another repository provides (``USES`` only).

A bare specifier that resolves inside its own repository is the import graph's
business (:mod:`repoatlas.graph.modules`). One that does not may still name source
RepoAtlas holds: a package that another managed repository provides. A use of a name
imported from it is then a use of the declaration that package exports, and following
it is how a change in one repository reaches the exact declarations that consume it in
another. This module decides when that is proven, and it decides it only on what the
two repositories' own files state:

1. the specifier is a package name, never a path: a relative or absolute specifier is
   the import graph's alone and never leaves its repository;
2. the importing repository does not settle the name itself: no ``paths`` pattern of
   its project's tsconfig claims the specifier, and none of its own manifests provides
   the package;
3. the nearest manifest at or above the importing file that *declares* the package is
   the consumer's evidence. A declaration that names another package (``npm:``) or a
   location (``workspace:``, ``file:``, ``link:``, ``portal:``) is not evidence for a
   package some other repository provides;
4. exactly one manifest in all the other repositories provides the name. Two -- in two
   repositories or in one -- is an ambiguity, never a choice;
5. that package's entry for the specifier's subpath is exactly one indexed file
   (:meth:`~repoatlas.graph.modules.ModuleResolver.package_entry`).

Anything short of all five is a named refusal, not a guess: the name joins nothing.
What each step established is recorded on the edge (``crossing``), so every repository
crossing can be read back to the manifest lines and the entry that proved it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from repoatlas.dependencies.normalize import NPM, normalize
from repoatlas.graph.modules import ModuleResolver, split_package
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.storage.protocols import FileRow

#: The ``basis`` a ``USES`` edge's resolution records when a package link proved it.
PACKAGE_BASIS = "package"
#: The resolution key holding a crossing's evidence; the impact walk crosses on nothing else.
CROSSING = "crossing"

#: Declarations that name something other than a package another repository provides.
_ALIASED = ("npm:",)
_LOCATED = ("workspace:", "file:", "link:", "portal:")


@dataclass(frozen=True, slots=True)
class PackageLink:
    """The provider entry a package specifier names, and the evidence that it does."""

    file: FileRow
    #: The ``crossing`` record: package, provider, manifests, entry.
    crossing: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Provision:
    repo_id: int
    directory: str
    manifest: str
    line: int
    name: str


@dataclass(frozen=True, slots=True)
class _Declaration:
    manifest: str
    line: int
    constraint: str | None


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
        self._provided: dict[str, list[_Provision]] = defaultdict(list)
        self._provided_in: dict[int, set[str]] = defaultdict(set)
        self._declared: dict[tuple[int, str], dict[str, _Declaration]] = defaultdict(dict)
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
                self._provided[record.normalized_name].append(
                    _Provision(
                        record.repo_id,
                        _parent(manifest),
                        manifest,
                        record.line_start,
                        record.name,
                    )
                )
                self._provided_in[record.repo_id].add(record.normalized_name)
            elif record.role == "declared":
                self._declared[(record.repo_id, _parent(manifest))].setdefault(
                    record.normalized_name,
                    _Declaration(manifest, record.line_start, record.constraint),
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
        constraint = (declaration.constraint or "").strip()
        if constraint.startswith(_ALIASED):
            return "dependency-aliased"
        if constraint.startswith(_LOCATED):
            return "dependency-protocol"
        providers = [p for p in self._provided.get(wanted, []) if p.repo_id != origin.repo_id]
        if not providers:
            return "provider-absent"
        if len(providers) > 1:
            return "provider-ambiguous"
        provider = providers[0]
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
