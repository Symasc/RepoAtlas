"""Python module identity: which indexed file a dotted module name names, inside one project.

A Python import names a module by a dotted name, and a dotted name names a file only
relative to a **source root** -- the directory an installed or running project puts on
the import path. Every directory of a repository is not one: ``scripts/bar.py`` is not the
module ``bar``, and ``src/`` in a src layout is where packages start, never a package
itself. This module decides roots from what the index holds, and nothing else: no
``sys.path``, no installed package, no interpreter, no file read at link time.

**Projects.** A ``pyproject.toml`` that is a project (:mod:`repoatlas.parsers.python_project`)
owns every file under its directory that no project nested deeper owns. A repository
with no Python project at all is one implicit project, its root; with no ``setup.py`` or
``setup.cfg`` at its root either, it is *metadata-less* (:meth:`PythonModules.metadata_less`).
A file outside every project of a repository that has projects belongs to none.

**Roots.** A project's roots are the directories its hatchling ``packages`` name, which
also limit the top-level names it provides; or, for flit's ``[tool.flit.module] name``,
the one directory holding that module -- the project's own or its ``src/``, as flit
itself looks, and never both; else ``src/`` when that holds a regular package or module;
else the project's own directory. A project stating any other layout is refused
(``project-layout-unread``): a layout this does not read is not guessed from its
directories, and an explicit layout that names nothing indexed is refused as well,
never replaced by discovery.

**Absolute names** resolve only in the importer's own project roots. A project is a
boundary: another project's modules -- in this repository or any other -- are reached
only on dependency evidence, never because they sit in the same Git repository, and an
import that only another project here could satisfy is refused as ``project-boundary``.

**Relative names** resolve within the chain of regular packages holding the importer,
which never climbs to or above its source root.

Within one directory CPython's own order holds: a regular package (``x/__init__.py``)
before a module (``x.py``), and a stub (``.pyi``) only where no source exists. A directory
without ``__init__`` is a namespace portion, refused (``namespace-package``) rather than
treated as a package. Module names are matched exactly, case and all, on the index's own
repository-relative paths.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from repoatlas.graph.modules import ModuleTarget
from repoatlas.graph.resolution import ResolutionIndex, _normalize
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_PROJECT
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

PYTHON_RELATIVE = "python-relative"
PYTHON_ROOT = "python-root"
#: The implicit project of a repository with no Python project at all.
REPOSITORY_ROOT = "repository-root"

_PACKAGE_FILES = ("__init__.py", "__init__.pyi")
#: Top-level directories a discovered layout never ships: a project's own tooling.
_NOT_SHIPPED = frozenset(
    {"tests", "test", "docs", "doc", "examples", "example", "scripts", "benchmarks", "tools"}
    | {"build", "dist"}
)


@lru_cache(maxsize=1 << 16)
def _parent(rel_path: str) -> str:
    parent = str(PurePosixPath(rel_path).parent)
    return "" if parent == "." else parent


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _inside(path: str, directory: str) -> bool:
    return directory == "" or path == directory or path.startswith(directory + "/")


@dataclass(frozen=True, slots=True)
class Project:
    """One Python project: its directory, the file declaring it, and its stated layout."""

    repo_id: int
    directory: str
    #: The declaring ``pyproject.toml``, or :data:`REPOSITORY_ROOT` for the implicit one.
    manifest: str
    packages: tuple[str, ...] | None = None
    unread: bool = False
    #: The distribution name as written (``[project] name``), if any.
    name: str | None = None
    #: flit's ``[tool.flit.module] name``: the module the project ships.
    flit: str | None = None


@dataclass(frozen=True, slots=True)
class Root:
    """A source root of a project, and the top-level names it provides (``None``: any)."""

    path: str
    basis: str
    names: frozenset[str] | None = None


@dataclass(slots=True)
class _Repository:
    projects: dict[str, Project] = field(default_factory=dict)
    implicit: bool = False


class PythonModules:
    """Resolves dotted module names written in Python files to indexed files."""

    def __init__(self, index: ResolutionIndex, facts: Sequence[UnresolvedRefRecord]) -> None:
        self._index = index
        by_id = {row.file_id: row for rows in index.files_by_repo.values() for row in rows.values()}
        self._repos: dict[int, _Repository] = defaultdict(_Repository)
        #: Every project fact by (repository, directory), a virtual workspace root's too.
        self.stated: dict[tuple[int, str], tuple[str, dict[str, Any]]] = {}
        for claim in facts:
            meta = json.loads(claim.meta_json) if claim.meta_json else {}
            row = by_id.get(claim.file_id)
            if row is None or meta.get(MODULE_RESOLUTION) != PYTHON_PROJECT:
                continue
            stated = json.loads(meta.get("facts") or "{}")
            self.stated[(row.repo_id, _parent(row.rel_path))] = (row.rel_path, stated)
            if stated.get("virtual"):
                continue  # a workspace root with no project of its own owns no modules
            packages = stated.get("packages")
            name, flit = stated.get("name"), stated.get("flit_module")
            directory = _parent(row.rel_path)
            self._repos[row.repo_id].projects[directory] = Project(
                row.repo_id,
                directory,
                row.rel_path,
                tuple(packages) if isinstance(packages, list) else None,
                bool(stated.get("unread")),
                name if isinstance(name, str) else None,
                flit if isinstance(flit, str) else None,
            )
        for repo_id in index.files_by_repo:
            repository = self._repos[repo_id]
            if not repository.projects:
                repository.implicit = True
                repository.projects[""] = Project(repo_id, "", REPOSITORY_ROOT)
        self._dirs: dict[int, frozenset[str]] = {}
        self._roots: dict[tuple[int, str], list[Root] | str] = {}
        self._resolved: dict[tuple[int, str, str], ModuleTarget | str] = {}
        self._shipping: dict[str, list[Project]] | None = None
        #: Repositories stating any Python project fact.
        self._stating = frozenset(repo_id for repo_id, _ in self.stated)

    # -- projects and roots ------------------------------------------------------

    def metadata_less(self, repo_id: int) -> bool:
        """Whether a repository states nothing about Python packaging: no project fact of
        any kind, a virtual workspace root's included, and no ``setup.py`` or ``setup.cfg``
        at its root, whatever they say.

        Only such a repository's implicit project is declared by its root
        ``requirements.txt`` (:mod:`repoatlas.graph.py_packages`).
        """
        repository = self._repos.get(repo_id)
        if repository is None or not repository.implicit or repo_id in self._stating:
            return False
        return not any(self._file(repo_id, name) for name in ("setup.py", "setup.cfg"))

    def project_of(self, repo_id: int, rel_path: str) -> Project | None:
        """The innermost project whose directory holds ``rel_path``, if any."""
        projects = self._repos[repo_id].projects
        directory = _parent(rel_path)
        while True:
            found = projects.get(directory)
            if found is not None:
                return found
            if not directory:
                return None
            directory = _parent(directory)

    def roots(self, project: Project) -> list[Root] | str:
        """A project's source roots, or why it has none this can read."""
        key = (project.repo_id, project.directory)
        if key not in self._roots:
            self._roots[key] = self._find_roots(project)
        return self._roots[key]

    def _find_roots(self, project: Project) -> list[Root] | str:
        if project.unread or (project.packages is not None and project.flit is not None):
            return "project-layout-unread"
        if project.flit is not None:
            return self._flit_root(project, project.flit)
        if project.packages is not None:
            names: dict[str, set[str]] = defaultdict(set)
            for entry in project.packages:
                path = _normalize(_join(project.directory, entry.strip().rstrip("/")))
                if not path or not _inside(path, project.directory) or path == project.directory:
                    return "project-layout-unread"
                names[_parent(path)].add(PurePosixPath(path).name)
            return [
                Root(root, "tool.hatch.build.targets.wheel.packages", frozenset(found))
                for root, found in sorted(names.items())
            ]
        src = _join(project.directory, "src")
        if any(
            self._top_level(project.repo_id, src, name) not in (None, "namespace-package")
            for name in self._children(project.repo_id, src)
        ):
            return [Root(src, "src-layout")]
        basis = REPOSITORY_ROOT if project.manifest == REPOSITORY_ROOT else "flat-layout"
        return [Root(project.directory, basis)]

    def _flit_root(self, project: Project, module: str) -> list[Root] | str:
        """The one directory flit finds ``module`` in: the project's own, or its ``src/``.

        In each, a package (``module/__init__.py``) before a module (``module.py``), as
        flit looks. A module found in both is ``module-ambiguous``, in neither
        ``project-layout-unread``; a dotted name is a module inside a namespace package,
        refused as ``namespace-package`` where its parent is one, and as
        ``project-layout-unread`` otherwise, since flit ships no such module.
        """
        parts = module.split(".")
        if not all(part.isidentifier() for part in parts):
            return "project-layout-unread"
        found: list[str] = []
        for base in (project.directory, _join(project.directory, "src")):
            place = _join(base, "/".join(parts))
            if self._file(project.repo_id, f"{place}/__init__.py") or self._file(
                project.repo_id, f"{place}.py"
            ):
                found.append(base)
        if len(found) > 1:
            return "module-ambiguous"
        if not found:
            return "project-layout-unread"
        if len(parts) > 1:
            parents = [_join(found[0], "/".join(parts[:k])) for k in range(1, len(parts))]
            if all(self._file(project.repo_id, f"{d}/__init__.py") for d in parents):
                return "project-layout-unread"
            return "namespace-package"
        return [Root(found[0], "tool.flit.module.name", frozenset(parts))]

    def _children(self, repo_id: int, directory: str) -> list[str]:
        """Names of the modules and directories directly inside ``directory``."""
        found: set[str] = set()
        prefix = f"{directory}/" if directory else ""
        for rel_path in self._index.files_by_repo.get(repo_id, {}):
            if not rel_path.startswith(prefix):
                continue
            head = rel_path[len(prefix) :].split("/", 1)
            if len(head) == 2:
                found.add(head[0])
            elif head[0].endswith((".py", ".pyi")):
                found.add(head[0].rsplit(".", 1)[0])
        return sorted(found)

    def _occupied(self, repo_id: int) -> frozenset[str]:
        if repo_id not in self._dirs:
            dirs: set[str] = set()
            for rel_path in self._index.files_by_repo.get(repo_id, {}):
                parent = _parent(rel_path)
                while parent and parent not in dirs:
                    dirs.add(parent)
                    parent = _parent(parent)
            self._dirs[repo_id] = frozenset(dirs)
        return self._dirs[repo_id]

    # -- files -------------------------------------------------------------------

    def _file(self, repo_id: int, rel_path: str) -> FileRow | None:
        return self._index.file(repo_id, rel_path)

    def _top_level(self, repo_id: int, directory: str, name: str) -> FileRow | str | None:
        """``name`` inside ``directory``: a package, a module, a stub, a namespace, or nothing."""
        base = _join(directory, name)
        for candidate in (
            f"{base}/__init__.py",
            f"{base}.py",
            f"{base}/__init__.pyi",
            f"{base}.pyi",
        ):
            found = self._file(repo_id, candidate)
            if found is not None:
                return found
        return "namespace-package" if base in self._occupied(repo_id) else None

    def submodule(self, package: FileRow, name: str) -> FileRow | None:
        """Submodule ``name`` of ``package``, when that is a regular package's ``__init__``."""
        if PurePosixPath(package.rel_path).name not in _PACKAGE_FILES or not name.isidentifier():
            return None
        found = self._top_level(package.repo_id, _parent(package.rel_path), name)
        if not isinstance(found, FileRow):
            return None
        return found if self._same_project(package, found) else None

    def _same_project(self, a: FileRow, b: FileRow) -> bool:
        return self.project_of(a.repo_id, a.rel_path) == self.project_of(b.repo_id, b.rel_path)

    def _walk(self, start: FileRow, parts: Sequence[str]) -> FileRow | str:
        """From a package, down ``parts`` as submodules; each step must be a regular package."""
        current = start
        for part in parts:
            if PurePosixPath(current.rel_path).name not in _PACKAGE_FILES:
                return "module-not-found"
            found = self._top_level(current.repo_id, _parent(current.rel_path), part)
            if found is None:
                return "module-not-found"
            if isinstance(found, str):
                return found
            current = found
        return current

    # -- names -------------------------------------------------------------------

    def resolve(self, origin: FileRow, spec: str) -> ModuleTarget | str:
        """The file ``spec`` names when written in ``origin``, or why it names none."""
        key = (origin.repo_id, _parent(origin.rel_path), spec)
        if key not in self._resolved:
            self._resolved[key] = self._resolve(origin, spec)
        return self._resolved[key]

    def _resolve(self, origin: FileRow, spec: str) -> ModuleTarget | str:
        level = len(spec) - len(spec.lstrip("."))
        dotted = spec[level:]
        parts = dotted.split(".") if dotted else []
        if any(not part.isidentifier() for part in parts) or (not level and not parts):
            return "module-not-found"
        project = self.project_of(origin.repo_id, origin.rel_path)
        if level:
            found = self._relative(origin, project, level, parts)
        else:
            if project is None:
                return "no-project"
            found = self._absolute(origin, project, parts)
        if isinstance(found, str):
            return found
        target, detail = found
        if self.project_of(target.repo_id, target.rel_path) != project:
            return "project-boundary"
        return ModuleTarget(
            target,
            PYTHON_RELATIVE if level else PYTHON_ROOT,
            tuple(sorted(detail)),
        )

    def _relative(
        self, origin: FileRow, project: Project | None, level: int, parts: list[str]
    ) -> tuple[FileRow, list[tuple[str, str]]] | str:
        chain = self._package_chain(origin, project)
        if level > len(chain):
            return "relative-beyond-package"
        package = chain[level - 1]
        found = self._walk(package, parts)
        if isinstance(found, str):
            return found
        return found, []

    def _package_chain(self, origin: FileRow, project: Project | None) -> list[FileRow]:
        """The ``__init__`` of each regular package holding ``origin``, innermost first."""
        stops = set()
        roots = self.roots(project) if project is not None else []
        if isinstance(roots, list):
            stops = {root.path for root in roots}
        if project is not None:
            stops.add(project.directory)
        chain: list[FileRow] = []
        directory = _parent(origin.rel_path)
        while directory and directory not in stops:
            package = next(
                (
                    row
                    for name in _PACKAGE_FILES
                    if (row := self._file(origin.repo_id, f"{directory}/{name}")) is not None
                ),
                None,
            )
            if package is None:
                break
            chain.append(package)
            directory = _parent(directory)
        return chain

    def _absolute(
        self, origin: FileRow, project: Project, parts: list[str]
    ) -> tuple[FileRow, list[tuple[str, str]]] | str:
        roots = self.roots(project)
        if isinstance(roots, str):
            return roots
        head = parts[0]
        found = self._head(project.repo_id, roots, head)
        if isinstance(found, str):
            if found == "module-not-found" and self._elsewhere(origin, project, head):
                return "project-boundary"
            return found
        top, root = found
        shadow = self._script_shadow(origin, root, head, top)
        if shadow is not None:
            return shadow
        target = self._walk(top, parts[1:])
        if isinstance(target, str):
            return target
        return target, root_detail(root, project)

    def _head(self, repo_id: int, roots: Sequence[Root], head: str) -> tuple[FileRow, Root] | str:
        """The one top-level module ``head`` names in ``roots``, or why none does."""
        found: dict[int, tuple[FileRow, Root]] = {}
        namespace = False
        for root in roots:
            if root.names is not None and head not in root.names:
                continue
            top = self._top_level(repo_id, root.path, head)
            if isinstance(top, FileRow):
                found.setdefault(top.file_id, (top, root))
            elif top == "namespace-package":
                namespace = True
        if len(found) > 1:
            return "module-ambiguous"
        if not found:
            return "namespace-package" if namespace else "module-not-found"
        return next(iter(found.values()))

    # -- what a project provides to others ---------------------------------------

    def holds(self, project: Project, head: str) -> bool:
        """Whether the project's own roots hold a top-level module ``head`` at all.

        An import of one the project holds is the project's to resolve, whatever it then
        finds -- another project nested inside it included; only one it lacks may be
        another project's.
        """
        roots = self.roots(project)
        return isinstance(roots, list) and not isinstance(
            self._head(project.repo_id, roots, head), str
        )

    def resolve_in(self, project: Project, parts: Sequence[str]) -> tuple[FileRow, Root] | str:
        """The module ``parts`` names inside ``project``, imported from another project.

        As within the project itself: its own roots, then submodules of regular
        packages, and the module must be the project's own.
        """
        roots = self.roots(project)
        if isinstance(roots, str):
            return roots
        found = self._head(project.repo_id, roots, parts[0])
        if isinstance(found, str):
            return found
        top, root = found
        target = self._walk(top, parts[1:])
        if isinstance(target, str):
            return target
        if self.project_of(target.repo_id, target.rel_path) != project:
            return "project-boundary"
        return target, root

    def providing(self, head: str) -> list[Project]:
        """Every project, in any repository, that ships a top-level module ``head``."""
        if self._shipping is None:
            shipping: dict[str, list[Project]] = defaultdict(list)
            for repo_id in sorted(self._repos):
                projects = self._repos[repo_id].projects
                for directory in sorted(projects):
                    for name in sorted(self.shipped(projects[directory])):
                        shipping[name].append(projects[directory])
            self._shipping = dict(shipping)
        return self._shipping.get(head, [])

    def shipped(self, project: Project) -> frozenset[str]:
        """The top-level modules a project ships, as far as its layout says.

        An explicit layout ships what it names. A discovered one ships every regular
        package under its roots and, under ``src/``, every module too -- never a flat
        project's own top-level scripts, nor its tests, docs, examples or tooling. The
        implicit project of a repository with none is no distribution and ships nothing.
        """
        roots = self.roots(project)
        if isinstance(roots, str) or project.manifest == REPOSITORY_ROOT:
            return frozenset()
        names: set[str] = set()
        for root in roots:
            listed = root.names
            for name in (
                sorted(listed) if listed is not None else self._children(project.repo_id, root.path)
            ):
                if listed is None and (name in _NOT_SHIPPED or not name.isidentifier()):
                    continue
                top = self._top_level(project.repo_id, root.path, name)
                if not isinstance(top, FileRow):
                    continue
                package = PurePosixPath(top.rel_path).name in _PACKAGE_FILES
                if listed is None and root.basis == "flat-layout" and not package:
                    continue
                if self.project_of(top.repo_id, top.rel_path) == project:
                    names.add(name)
        return frozenset(names)

    def _script_shadow(self, origin: FileRow, root: Root, head: str, top: FileRow) -> str | None:
        """Run as a script, a file outside every package sees its own directory first."""
        directory = _parent(origin.rel_path)
        if directory == root.path or any(
            self._file(origin.repo_id, _join(directory, name)) for name in _PACKAGE_FILES
        ):
            return None
        beside = self._top_level(origin.repo_id, directory, head)
        if isinstance(beside, FileRow) and beside.file_id != top.file_id:
            return "script-shadow"
        return None

    def _elsewhere(self, origin: FileRow, project: Project, head: str) -> bool:
        """Whether another project of this repository would provide ``head``."""
        for other in self._repos[origin.repo_id].projects.values():
            if other == project:
                continue
            roots = self.roots(other)
            if isinstance(roots, str):
                continue
            for root in roots:
                if root.names is not None and head not in root.names:
                    continue
                if isinstance(self._top_level(origin.repo_id, root.path, head), FileRow):
                    return True
        return False


def root_detail(root: Root, project: Project) -> list[tuple[str, str]]:
    """How a module was placed: the root it was found under, why that is one, the project."""
    return [("root", root.path or "."), ("root_basis", root.basis), ("project", project.manifest)]
