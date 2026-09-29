"""Python package links: an import of a module another Python project provides (``USES`` only).

Within its own project a Python import resolves against the project's own roots
(:mod:`repoatlas.graph.py_modules`), and there it stops: sitting in the same Git
repository is no evidence that one project imports another. What is evidence is the
importing project's own declaration of a dependency, read here, and nothing else:

1. **what the import names.** Some indexed project, in any repository, other than the
   importer's own, ships a top-level module of that name
   (:meth:`~repoatlas.graph.py_modules.PythonModules.shipped`). A distribution name is
   never read as a module name: the module is found on disk, and the distribution is
   whatever the project shipping it is called;
2. **the declaration.** The importer's project declares that project's distribution,
   PEP 503-normalised, in its ``pyproject.toml``'s PEP 621 dependencies. A name Poetry's
   tables declare, or a PEP 508 direct reference (``name @ ...``), is a form this does
   not read: ``dependency-unread``, never a version;
3. **the provider it names.** A ``[tool.uv.sources]`` entry for the name -- the
   project's own, or else its workspace root's, which uv applies to every member that
   does not override it -- pins it:

   * ``path``: followed from the manifest that declares the source, it must reach the
     directory of an indexed manifest providing the name, in whichever repository;
   * ``workspace = true``: the importing project must be a member of the innermost
     indexed uv workspace around it (its root, or a directory its ``members`` match and
     its ``exclude`` does not), and exactly one member of that workspace must provide
     the name;
   * any other source (git, URL, an index, another workspace, one for some platforms
     only) joins nothing.

   A pin that fails never falls back to the name. Without one, the declaration names
   the distribution wherever it is provided, and exactly one indexed manifest other
   than the importer's own must provide it -- two, in one repository or two, is an
   ambiguity, never a choice;
4. **agreement.** That provider must be the project shipping the module. Two projects
   shipping it that both pass is ``provider-ambiguous``.

The module is then found inside the provider's own roots, and the trace continues there
under the provider's own rules. The edge records what each step established -- the
declaration, the provision, the pin -- and, when the provider is in another repository,
a ``crossing`` exactly as a TypeScript package link does, which is what lets the impact
walk cross. Residual risk, stated: a dependency that is not indexed may ship the same
top-level module, and wins at run time; nothing indexed can show it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from repoatlas.dependencies.normalize import PYPI, normalize
from repoatlas.graph.declared import path_target, root_path
from repoatlas.graph.modules import ModuleTarget, member_matcher
from repoatlas.graph.packages import ProviderTargets, Provision
from repoatlas.graph.py_modules import Project, PythonModules, Root, root_detail
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.storage.protocols import FileRow

#: The ``basis`` of a module another project provides, on the evidence above.
PYTHON_DEPENDENCY = "python-dependency"

#: What a uv source other than a path or this workspace is refused as.
_OTHER_SOURCES = {
    "git": "dependency-elsewhere",
    "url": "dependency-elsewhere",
    "index": "dependency-elsewhere",
}


@lru_cache(maxsize=1 << 16)
def _parent(rel_path: str) -> str:
    parent = str(PurePosixPath(rel_path).parent)
    return "" if parent == "." else parent


@dataclass(frozen=True, slots=True)
class PythonLink:
    """The module another project provides, and the evidence that the import names it."""

    target: ModuleTarget
    #: The ``crossing`` record, when the provider is in another repository.
    crossing: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _Declaration:
    """The first row a manifest declares a distribution with, and every constraint it does."""

    manifest: str
    line: int
    constraint: str | None
    constraints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Source:
    """A ``[tool.uv.sources]`` entry, and the manifest that states it."""

    value: Mapping[str, Any]
    repo_id: int
    #: The project's own manifest, or its workspace root's when inherited from there.
    manifest: str


@dataclass(frozen=True, slots=True)
class _Workspace:
    """A uv workspace: its root's manifest, members, exclusions and sources."""

    repo_id: int
    manifest: str
    root: str  # absolute
    members: tuple[str, ...]
    exclude: tuple[str, ...]
    sources: Mapping[str, Any]

    @property
    def directory(self) -> str:
        return _parent(self.manifest)

    def holds(self, place: str) -> bool:
        """Whether the project at absolute ``place`` is a member: the root, or matched."""
        if place == self.root:
            return True
        if not place.startswith(self.root + "/"):
            return False
        globs = [*self.members, *(f"!{g}" for g in self.exclude)]
        return member_matcher(globs)(place[len(self.root) + 1 :])


def _named(sources: object, wanted: str) -> dict[str, Any] | None:
    """The source a ``[tool.uv.sources]`` table gives the distribution ``wanted``."""
    if not isinstance(sources, dict):
        return None
    found = [v for k, v in sources.items() if normalize(PYPI, str(k)) == wanted]
    if len(found) > 1:
        return {"other": "multiple"}
    return found[0] if found and isinstance(found[0], dict) else None


class PythonTargets:
    """Where a Python project's declared source leads, across every indexed repository.

    Shared by package links and the package-level dependents of a change, so both take
    a declaration to name the same provider. Everything is placed by absolute path,
    followed lexically from each repository's root, as :class:`ProviderTargets` places it.
    """

    def __init__(
        self,
        roots: Mapping[int, str],
        provisions: Iterable[Provision],
        stated: Mapping[tuple[int, str], tuple[str, Mapping[str, Any]]],
        slugs: Mapping[int, str],
    ) -> None:
        self._root_of = dict(roots)
        self._slugs = dict(slugs)
        self._stated = stated
        self._provisions = list(provisions)
        self._paths = ProviderTargets(roots, self._provisions, (), slugs)
        self._by_name: dict[str, list[Provision]] = defaultdict(list)
        seen: set[tuple[int, str, str]] = set()
        for provision in self._provisions:
            key = (provision.repo_id, provision.directory, provision.normalized)
            if key not in seen:
                seen.add(key)
                self._by_name[provision.normalized].append(provision)
        self._workspaces: dict[str, _Workspace] = {}
        for (repo_id, _), (manifest, facts) in sorted(stated.items()):
            declared = facts.get("uv_workspace")
            root = self._root_of.get(repo_id)
            if not isinstance(declared, dict) or root is None:
                continue
            place = path_target(root, _parent(manifest), "")
            self._workspaces[place] = _Workspace(
                repo_id,
                manifest,
                place,
                tuple(declared.get("members") or ()),
                tuple(declared.get("exclude") or ()),
                facts.get("uv_sources") or {},
            )

    def provisions(self, wanted: str) -> list[Provision]:
        """Every indexed manifest directory providing the distribution ``wanted``."""
        return self._by_name.get(wanted, [])

    def source(self, repo_id: int, directory: str, wanted: str) -> _Source | None:
        """The uv source a project states for ``wanted``, and where it is stated.

        Its own ``[tool.uv.sources]``, else its workspace root's, which uv applies to
        every member that does not override it; ``None`` when neither names it.
        """
        own = self._stated.get((repo_id, directory))
        if own is not None:
            found = _named(own[1].get("uv_sources"), wanted)
            if found is not None:
                return _Source(found, repo_id, own[0])
        workspace = self.workspace_of(repo_id, directory)
        if isinstance(workspace, _Workspace) and workspace.root != self._place(repo_id, directory):
            inherited = _named(workspace.sources, wanted)
            if inherited is not None:
                return _Source(inherited, workspace.repo_id, workspace.manifest)
        return None

    def workspace_of(self, repo_id: int, directory: str) -> _Workspace | str:
        """The innermost indexed uv workspace around a project, if the project is a member."""
        here = self._place(repo_id, directory)
        if here is None:
            return "workspace-unindexed"
        holding = [w for w in self._workspaces if here == w or here.startswith(w + "/")]
        if not holding:
            return "workspace-unindexed"
        workspace = self._workspaces[max(holding, key=len)]
        return workspace if workspace.holds(here) else "workspace-not-member"

    def by_path(
        self, repo_id: int, manifest: str, path: str, wanted: str
    ) -> tuple[Provision, dict[str, str]] | str:
        """The provider a ``path`` source declared in ``manifest`` leads to.

        A path with a backslash is ``path-unreadable`` on every host; an absolute or
        ``~`` path names a directory on the installing machine, ``dependency-elsewhere``.
        Otherwise as a declared npm path (:meth:`ProviderTargets.by_path`).
        """
        if "\\" in path:
            return "path-unreadable"
        if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
            return "dependency-elsewhere"
        return self._paths.by_path(repo_id, manifest, path, wanted)

    def by_workspace(
        self, repo_id: int, directory: str, wanted: str
    ) -> tuple[Provision, dict[str, str]] | str:
        """The one member of the project's uv workspace providing ``wanted``."""
        workspace = self.workspace_of(repo_id, directory)
        if isinstance(workspace, str):
            return workspace
        members = [
            p
            for p in self.provisions(wanted)
            if PurePosixPath(p.manifest).name == "pyproject.toml"
            and (place := self._place(p.repo_id, p.directory)) is not None
            and workspace.holds(place)
        ]
        if not members:
            return "workspace-member-absent"
        if len(members) > 1:
            return "provider-ambiguous"
        slug = self._slugs.get(workspace.repo_id, "")
        return members[0], {"resolved_by": "workspace", "workspace": f"{slug}/{workspace.manifest}"}

    def pinned(
        self, repo_id: int, directory: str, wanted: str
    ) -> tuple[Provision, dict[str, str]] | str | None:
        """What a uv source pins ``wanted`` to for a project; ``None`` when none is stated.

        A pin that fails is its reason, and never falls back to the name.
        """
        found = self.source(repo_id, directory, wanted)
        if found is None:
            return None
        value = found.value
        if isinstance(value.get("path"), str):
            # followed from the manifest stating it: a workspace root's, when inherited
            pinned = self.by_path(found.repo_id, found.manifest, value["path"], wanted)
        elif value.get("workspace") is True:
            pinned = self.by_workspace(repo_id, directory, wanted)
        else:
            return _OTHER_SOURCES.get(str(value.get("other")), "dependency-unread")
        if isinstance(pinned, str):
            return pinned
        provision, evidence = pinned
        stated = f"{self._slugs.get(found.repo_id, '')}/{found.manifest}"
        return provision, {**evidence, "pinned_at": stated}

    def _place(self, repo_id: int, directory: str) -> str | None:
        root = self._root_of.get(repo_id)
        return None if root is None else path_target(root, directory, "")


class PythonPackageLinks:
    """Decides, for one global pass, which Python imports reach another project's module."""

    def __init__(
        self,
        modules: PythonModules,
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
        located = [
            (record, manifest)
            for record in records
            if record.ecosystem == PYPI and (manifest := paths.get(record.file_id)) is not None
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
        provisions: list[Provision] = []
        first: dict[tuple[int, str], dict[str, DependencyRecord]] = defaultdict(dict)
        constraints: dict[tuple[int, str, str], set[str]] = defaultdict(set)
        for record, manifest in located:
            if record.role == "provided":
                provisions.append(
                    Provision(
                        record.repo_id,
                        _parent(manifest),
                        manifest,
                        record.line_start,
                        record.name,
                        record.normalized_name,
                    )
                )
            elif record.role == "declared" and PurePosixPath(manifest).name == "pyproject.toml":
                first[(record.repo_id, manifest)].setdefault(record.normalized_name, record)
                constraints[(record.repo_id, manifest, record.normalized_name)].add(
                    (record.constraint or "").strip()
                )
        self._declared: dict[tuple[int, str], dict[str, _Declaration]] = {
            key: {
                name: _Declaration(
                    key[1],
                    record.line_start,
                    record.constraint,
                    tuple(sorted(constraints[(key[0], key[1], name)])),
                )
                for name, record in names.items()
            }
            for key, names in first.items()
        }
        self.targets = PythonTargets(
            {repo_id: root_path(path) for path, repo_id in index.repo_roots},
            provisions,
            modules.stated,
            index.repo_slugs,
        )
        self._memo: dict[tuple[int, str, str], PythonLink | str | None] = {}

    def resolve(self, origin: FileRow, spec: str) -> PythonLink | str | None:
        """The module ``spec`` names in another project, why it names none, or ``None``
        when no other indexed project ships its top-level module at all."""
        key = (origin.repo_id, _parent(origin.rel_path), spec)
        if key not in self._memo:
            self._memo[key] = self._resolve(origin, spec)
        return self._memo[key]

    def _resolve(self, origin: FileRow, spec: str) -> PythonLink | str | None:
        parts = spec.split(".")
        if spec.startswith(".") or not all(part.isidentifier() for part in parts):
            return None
        own = self._modules.project_of(origin.repo_id, origin.rel_path)
        if own is None or self._modules.holds(own, parts[0]):
            return None
        candidates = [p for p in self._modules.providing(parts[0]) if p != own]
        if not candidates:
            return None
        accepted: list[tuple[Project, Provision, _Declaration, dict[str, str]]] = []
        refusals: list[str] = []
        for candidate in candidates:
            if candidate.name is None:
                refusals.append("provider-unnamed")
                continue
            found = self.provider(own, normalize(PYPI, candidate.name))
            if isinstance(found, str):
                refusals.append(found)
                continue
            provision, declaration, evidence = found
            if (provision.repo_id, provision.directory) != (candidate.repo_id, candidate.directory):
                refusals.append("dependency-elsewhere")
                continue
            accepted.append((candidate, provision, declaration, evidence))
        if len(accepted) > 1:
            return "provider-ambiguous"
        if not accepted:
            specific = [r for r in refusals if r != "dependency-absent"]
            return specific[0] if specific else "dependency-absent"
        [(provider, provision, declaration, evidence)] = accepted
        placed = self._modules.resolve_in(provider, parts)
        if isinstance(placed, str):
            return placed
        module, root = placed
        return self._link(origin, provider, provision, declaration, evidence, module, root, parts)

    def provider(
        self, own: Project, wanted: str
    ) -> tuple[Provision, _Declaration, dict[str, str]] | str:
        """The provider the project's declaration of ``wanted`` names, or why none."""
        stated = self._modules.stated.get((own.repo_id, own.directory))
        poetry = stated[1].get("poetry_dependencies") if stated is not None else None
        if isinstance(poetry, list) and wanted in {normalize(PYPI, str(n)) for n in poetry}:
            return "dependency-unread"  # a Poetry declaration: a form this does not read
        declaration = self._declared.get((own.repo_id, own.manifest), {}).get(wanted)
        if declaration is None:
            return "dependency-absent"
        if any(c.startswith("@") for c in declaration.constraints):
            return "dependency-unread"  # a PEP 508 direct reference
        pinned = self.targets.pinned(own.repo_id, own.directory, wanted)
        if isinstance(pinned, str):
            return pinned
        if pinned is not None:
            return pinned[0], declaration, pinned[1]
        providers = [
            p
            for p in self.targets.provisions(wanted)
            if (p.repo_id, p.directory) != (own.repo_id, own.directory)
        ]
        if not providers:
            return "provider-absent"
        if len(providers) > 1:
            return "provider-ambiguous"
        return providers[0], declaration, {}

    def _link(
        self,
        origin: FileRow,
        provider: Project,
        provision: Provision,
        declaration: _Declaration,
        evidence: Mapping[str, str],
        module: FileRow,
        root: Root,
        parts: Sequence[str],
    ) -> PythonLink:
        package = f"{PYPI}:{provision.name}"
        declared_at = f"{declaration.manifest}:{declaration.line}"
        detail: list[tuple[str, Any]] = [
            *root_detail(root, provider),
            ("package", package),
            ("declared_at", declared_at),
            *sorted(evidence.items()),
        ]
        if declaration.constraint:
            detail.append(("constraint", declaration.constraint))
        crossing: dict[str, Any] | None = None
        if provider.repo_id != origin.repo_id:
            crossing = {
                "package": package,
                "provider": self._index.repo_slugs.get(provider.repo_id, ""),
                "provided_at": f"{provision.manifest}:{provision.line}",
                "entry": module.rel_path,
                "entry_fields": root.basis,
                "declared_at": declared_at,
                **evidence,
            }
            if len(parts) > 1:
                crossing["subpath"] = ".".join(parts[1:])
            if declaration.constraint:
                crossing["constraint"] = declaration.constraint
        return PythonLink(ModuleTarget(module, PYTHON_DEPENDENCY, tuple(sorted(detail))), crossing)
