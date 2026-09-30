"""The ``USES`` stage of the global pass: which uses to resolve again, and resolving them.

Every other edge the global pass writes is rebuilt from scratch on every sync
(:mod:`repoatlas.graph.linker`). ``USES`` edges are not: they are kept between syncs and
replaced one consumer file at a time, because resolving every use again is most of what
a one-file sync used to cost. A file's ``USES`` edges are a function of indexed facts
alone -- its own claims, the export tables its traces read, its repository's files and
module facts, and the package declarations and providers a package link consults
(:mod:`repoatlas.graph.uses`, :mod:`repoatlas.graph.packages`) -- so a file none of whose
inputs changed keeps edges identical to the ones a fresh index would write.

This module plans the stage while writing nothing (:func:`plan_uses`) and applies it
inside the global pass's one transaction (:func:`apply_uses`), which also records that
the stored edges are complete. When they are not known to be -- a first index, a
database from before this stage, a run that died before its commit, or a change to how
uses resolve (:data:`USES_RESOLUTION_VERSION`) -- every use is resolved again. A
TypeScript or JavaScript use is resolved by :class:`~repoatlas.graph.uses.UseResolver`, a
Python one by :class:`~repoatlas.graph.py_uses.PythonUseResolver`; both read the same
export tables and record the same reads, so everything below holds for either.

Otherwise :class:`UsesChangeLog` gathers, as each file is written, what changed that a
use can depend on, and :meth:`UsesChangeLog.scope` turns it into the files to resolve:

* a file whose facts were replaced: its own uses, and every file with a use landing in
  it, because its entities are written again with new ids and those edges went with them;
* a file whose **export surface** changed -- the names it exports, what each is bound to
  (lines aside), the modules it re-exports wholesale, and whether each local export names
  exactly one declaration -- every file whose uses read its export table (``use_reads``);
* a file added or deleted, or a change to a repository's module-resolution facts
  (tsconfig, package manifests, workspaces, Python projects): every file of that
  repository and of every repository declaring a package it provides, since specifiers
  resolve within their own repository and only a package link, which needs that
  declaration, reaches into it; and
  every file of each repository nested inside it, since a workspace it declares may hold
  them, and a package link resolves a ``workspace:`` declaration by that workspace
  whichever repository each member is indexed as;
* a change to a repository's declared packages, npm or PyPI: its own files; to the
  packages it provides: its own files and those of every repository declaring such a
  package -- a declaration a root ``requirements.txt`` reaches through ``-r`` counting as
  one (:mod:`repoatlas.graph.py_requirements`);
* a change to what a repository's root ``requirements.txt`` reaches -- an include added,
  removed or pointed elsewhere, a requirement in a reached file, a reached file gone or
  back, a cycle -- which the sync reports once it has recorded them anew
  (:meth:`UsesChangeLog.requirements_changed`): its own files;
* a file added or deleted, or a change to a Python project's facts: every file of each
  repository whose root ``requirements.txt``, or a file it reaches, requires, by the legacy key
  (:func:`~repoatlas.graph.py_packages.legacy_key`), a top-level module name the change
  may have given or taken -- since a Python import of another repository's module that
  no manifest provides is decided by which projects ship that name and which
  metadata-less repositories hold it (:mod:`repoatlas.graph.py_packages`). Those names
  are the plausible **top-level heads** of the change (:func:`top_level_heads`), never
  every name along its path: its first segment below each base a module can start from
  -- the repository root, ``src/``, each Python project's directory and its ``src/``, and
  every directory a setuptools layout roots, a pyproject's own ``[tool.setuptools]`` one
  included -- and the names a project's facts state.
  Every head below a base is taken only when what the base means may have changed: a
  root ``setup.py`` or ``setup.cfg`` added or deleted, a project's facts changed (a
  ``setup.cfg`` or ``setup.py`` stating another name or layout included), or a regular
  package or module added or deleted directly under a ``src/``.

Nothing else a use reads can change without one of these.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from repoatlas.dependencies.normalize import NPM, PYPI, normalize
from repoatlas.graph.modules import ModuleResolver, binding_pairs, declared_constraints
from repoatlas.graph.packages import CROSSING, PackageLinks
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.py_packages import PythonPackageLinks, legacy_key
from repoatlas.graph.py_requirements import REQUIREMENTS_FACTS
from repoatlas.graph.py_uses import PythonUseResolver
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses import UseResolver, declared_entity, table_from_meta, use_resolution
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.models.telemetry import UsesWork
from repoatlas.parsers.module_facts import (
    MODULE_RESOLUTION,
    PYTHON_PROJECT,
    PYTHON_REQUIREMENTS,
    PYTHON_SETUPTOOLS,
)
from repoatlas.storage.protocols import EdgeRecord, SymbolRow, UnresolvedRefRecord
from repoatlas.storage.sqlite.stores import SqliteDependencyStore, SqliteKnowledgeStore

#: Changed whenever what a use resolves to changes for the same facts, so edges kept
#: from before the change are never trusted: the next sync resolves every use again.
USES_RESOLUTION_VERSION = "10"
COMPLETE = f"complete:{USES_RESOLUTION_VERSION}"
#: The ecosystems whose declarations and provisions a package link reads.
LINKED_ECOSYSTEMS = (NPM, PYPI)
PENDING = "pending"


@dataclass(frozen=True, slots=True)
class UsesScope:
    """The consumer files whose uses a pass resolves again; ``None`` means every file.

    The counts say how many files each rule reached; one file may be reached by several.
    """

    files: frozenset[int] | None
    reason: str | None = None
    own: int = 0
    landed: int = 0
    readers: int = 0
    repository: int = 0
    package: int = 0
    repositories: int = 0
    packages: int = 0

    @classmethod
    def everything(cls, reason: str) -> UsesScope:
        return cls(files=None, reason=reason)


@dataclass(frozen=True, slots=True)
class FileState:
    """What one file contributes to other files' uses, compared before and after a write."""

    #: Its export surface (:func:`export_surface`).
    surface: str = "none"
    #: Its module-resolution facts, as recorded.
    facts: tuple[str, ...] = ()
    #: Its npm and PyPI declared and provided rows
    #: (ecosystem, role, name, display name, constraint, line).
    packages: tuple[tuple[str, str, str, str, str, int], ...] = ()


_MODULE_SUFFIXES = (".py", ".pyi")
_PACKAGE_FILES = frozenset({"__init__.py", "__init__.pyi"})
#: Root files whose presence decides whether a repository is metadata-less.
_ROOT_METADATA = frozenset({"setup.py", "setup.cfg"})
#: The files whose facts state a Python project's name and layout.
_PROJECT_FILES = frozenset({"pyproject.toml", *_ROOT_METADATA})


def _directory(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def top_level_heads(paths: Iterable[str], bases: Iterable[str]) -> set[str]:
    """The top-level module names ``paths`` may give below each of ``bases`` holding them.

    A path's first segment below a base: a directory's name, or a module's stem when the
    path is a ``.py`` or ``.pyi`` file directly there -- when that is an identifier. Never
    a name deeper in the path: ``pkg/foo/bar.py`` gives ``pkg`` below the root, and
    ``foo`` only below ``pkg``, when that is a base.
    """
    found: set[str] = set()
    ordered = sorted(set(bases))
    for path in paths:
        for base in ordered:
            if base and not path.startswith(f"{base}/"):
                continue
            head, slash, _ = (path[len(base) + 1 :] if base else path).partition("/")
            if not slash:
                if not head.endswith(_MODULE_SUFFIXES):
                    continue
                head = head.rsplit(".", 1)[0]
            if head.isidentifier():
                found.add(head)
    return found


def _rebased(path: str, projects: Iterable[str]) -> set[str]:
    """The bases whose meaning adding or deleting ``path`` may change: the root, for a root
    ``setup.py`` or ``setup.cfg``; a base and its ``src/``, for a regular package or a
    module directly in that ``src/``, which decides whether it is the root."""
    found = {""} if path in _ROOT_METADATA else set()
    for base in {"", *projects}:
        src = _join(base, "src")
        if not path.startswith(f"{src}/"):
            continue
        parts = path[len(src) + 1 :].split("/")
        if (len(parts) == 1 and parts[0].endswith(_MODULE_SUFFIXES)) or (
            len(parts) == 2 and parts[1] in _PACKAGE_FILES
        ):
            found |= {base, src}
    return found


def _stated(facts: Iterable[str]) -> list[tuple[str, dict[str, Any]]]:
    """Each Python project fact among a file's facts, by kind."""
    found = []
    for meta_json in facts:
        meta = json.loads(meta_json) if meta_json else {}
        kind = meta.get(MODULE_RESOLUTION)
        if kind in (PYTHON_PROJECT, PYTHON_SETUPTOOLS):
            found.append((kind, json.loads(meta.get("facts") or "{}")))
    return found


def _layout_names(facts: Iterable[str]) -> set[str]:
    """The top-level names Python project facts state a project ships: flit's module, each
    hatchling package's own name and each name its wheel's ``only-include`` lists, and the
    packages and modules a setuptools layout lists."""
    found: set[str] = set()
    for kind, stated in _stated(facts):
        if kind == PYTHON_SETUPTOOLS:
            layout = stated.get("layout") or {}
            listed = layout.get("packages")
            names = [*(listed if isinstance(listed, list) else []), *layout.get("py_modules", [])]
            found |= {name.split(".")[0] for name in names if isinstance(name, str)}
            continue
        flit = stated.get("flit_module")
        if isinstance(flit, str):
            found.add(flit.split(".")[0])
        packages = stated.get("packages")
        for entry in packages if isinstance(packages, list) else []:
            if isinstance(entry, str):
                found.add(PurePosixPath(entry.strip().rstrip("/")).name)
        only = stated.get("only_include")
        found |= {
            name for name in (only if isinstance(only, list) else []) if isinstance(name, str)
        }
    return found


def _setuptools_layout(kind: str, stated: dict[str, Any]) -> dict[str, Any] | None:
    """The setuptools layout a fact states: a ``setup.cfg``'s or ``setup.py``'s, or a
    ``pyproject.toml``'s own ``[tool.setuptools.packages.find]``."""
    found = stated.get("layout" if kind == PYTHON_SETUPTOOLS else "setuptools_layout")
    return found if isinstance(found, dict) else None


def _layout_roots(facts: Iterable[str], directory: str) -> set[str]:
    """The directory a setuptools layout stated in ``directory`` roots its modules at."""
    return {
        _join(directory, layout["root"]) if layout.get("root") else directory
        for kind, stated in _stated(facts)
        if (layout := _setuptools_layout(kind, stated)) is not None
    }


@dataclass(slots=True)
class _Heads:
    """What one run changed in one repository that may change the top-level modules it
    holds or ships (see :func:`top_level_heads`)."""

    #: Paths added or deleted, and project files whose facts changed.
    touched: set[str] = field(default_factory=set)
    #: Paths no longer indexed: deleted, or of a repository forgotten.
    gone: set[str] = field(default_factory=set)
    #: Bases whose every head may have changed meaning.
    rebased: set[str] = field(default_factory=set)
    #: Names project facts stated before a change, or state after it.
    names: set[str] = field(default_factory=set)


class UsesChangeLog:
    """What one sync run changed that uses can depend on, gathered as each file is written.

    Each ``before`` is asked inside the file's own transaction, before its facts are
    replaced or deleted, and each ``after`` once they are written. When the stored edges
    are not known complete (``everything``) nothing is gathered: every use is resolved.
    """

    def __init__(
        self,
        knowledge: SqliteKnowledgeStore,
        dependencies: SqliteDependencyStore | None,
        *,
        producer: str,
        everything: str | None,
    ) -> None:
        self._knowledge = knowledge
        self._dependencies = dependencies
        self._producer = producer
        self.everything = everything
        self.own: set[int] = set()
        self.landed: set[int] = set()
        #: Files whose export surface changed and still exist, and readers of deleted ones.
        self.surface: set[int] = set()
        self.readers: set[int] = set()
        #: Repositories whose file set or module facts changed.
        self.repositories: set[int] = set()
        #: Repositories whose declared or provided packages changed.
        self.package_repositories: set[int] = set()
        #: Packages, as (ecosystem, name), whose providers changed.
        self.packages: set[tuple[str, str]] = set()
        #: Repositories nested inside a repository forgotten in this run, found before it is.
        self.nested: set[int] = set()
        #: What changed each repository's top-level Python module names may.
        self.heads: dict[int, _Heads] = defaultdict(_Heads)

    def before(self, file_id: int) -> FileState | None:
        """The file's state before its facts are replaced; notes the uses landing in it."""
        if self.everything is not None:
            return None
        self.landed |= self._knowledge.use_consumers_landing_in(file_id, self._producer)
        return self._state(file_id)

    def after(self, repo_id: int, file_id: int, before: FileState | None) -> None:
        """Compare the file's written state with ``before`` (``None`` for a new file)."""
        if self.everything is not None:
            return
        self.own.add(file_id)
        self._compare(repo_id, file_id, before or FileState(), self._state(file_id))

    def requirements_changed(self, repo_id: int) -> None:
        """What the repository's root ``requirements.txt`` reaches was recorded anew and
        differs: its declarations may, and they are read for its own files alone."""
        if self.everything is None:
            self.package_repositories.add(repo_id)

    def files_added(self, repo_id: int, paths: Sequence[str] = ()) -> None:
        if self.everything is None:
            self.repositories.add(repo_id)
            self.heads[repo_id].touched.update(paths)

    def files_deleting(self, repo_id: int, file_ids: Sequence[int]) -> None:
        """Before files are deleted: what landed in them, read them and they provided."""
        if self.everything is not None or not file_ids:
            return
        self.repositories.add(repo_id)
        gone = [path for _, path in self._knowledge.file_locations(list(file_ids)).values()]
        self.heads[repo_id].touched.update(gone)
        self.heads[repo_id].gone.update(gone)
        for file_id in file_ids:
            self.landed |= self._knowledge.use_consumers_landing_in(file_id, self._producer)
            self.readers |= self._knowledge.use_readers([file_id])
            self._compare(repo_id, file_id, self._state(file_id), FileState())

    def repository_removing(self, repo_id: int) -> None:
        """Before a repository is forgotten: the packages it provided lose a provider, the
        repositories nested inside it any workspace it declared, and the modules it held or
        shipped their place."""
        if self.everything is not None:
            return
        self.nested |= self._knowledge.nested_repositories([repo_id])
        if self._dependencies is not None:
            for ecosystem in LINKED_ECOSYSTEMS:
                provided = self._dependencies.names_provided_by([repo_id], ecosystem)
                self.packages |= {(ecosystem, name) for name in provided}
        change = self.heads[repo_id]
        for file_id, path in self._knowledge.files_of_repository(repo_id):
            change.touched.add(path)
            change.gone.add(path)
            if PurePosixPath(path).name in _PROJECT_FILES:
                facts = self._facts(file_id)
                change.names |= _layout_names(facts)
                change.rebased |= _layout_roots(facts, _directory(path))

    def scope(self) -> UsesScope:
        """The files whose uses may have changed (see the module docstring)."""
        if self.everything is not None:
            return UsesScope.everything(self.everything)
        readers = self.readers | (
            self._knowledge.use_readers(sorted(self.surface)) if self.surface else set()
        )
        dependents = set(self.repositories)
        package_files: set[int] = set()
        if self._dependencies is not None:
            changed = sorted(dependents)
            declaring: set[int] = set()
            for ecosystem in LINKED_ECOSYSTEMS:
                provided = self._dependencies.names_provided_by(changed, ecosystem)
                dependents |= self._dependencies.repositories_declaring(sorted(provided), ecosystem)
                names = sorted(name for kind, name in self.packages if kind == ecosystem)
                declaring |= self._dependencies.repositories_declaring(names, ecosystem)
                if ecosystem == PYPI:
                    dependents |= self._reaching(provided, ())
                    declaring |= self._reaching(names, ())
            keys = self._required_keys()
            declaring |= self._dependencies.repositories_requiring(sorted(keys))
            declaring |= self._reaching((), keys)
            package_files = self._knowledge.file_ids_of_repositories(sorted(declaring))
        nested = self.nested | self._knowledge.nested_repositories(sorted(self.repositories))
        repositories = dependents | self.package_repositories | nested
        repository_files = self._knowledge.file_ids_of_repositories(sorted(repositories))
        files = self.own | self.landed | readers | repository_files | package_files
        return UsesScope(
            files=frozenset(files),
            own=len(self.own),
            landed=len(self.landed),
            readers=len(readers),
            repository=len(repository_files),
            package=len(package_files),
            repositories=len(repositories),
            packages=len(self.packages),
        )

    def _state(self, file_id: int) -> FileState:
        tables = self._knowledge.list_export_tables([file_id])
        return FileState(
            surface=export_surface(
                file_id, tables.get(file_id), self._knowledge.list_symbols(file_id=file_id)
            ),
            facts=self._facts(file_id),
            packages=(
                tuple(
                    (ecosystem, *row)
                    for ecosystem in LINKED_ECOSYSTEMS
                    for row in self._dependencies.package_rows_for_file(file_id, ecosystem)
                )
                if self._dependencies is not None
                else ()
            ),
        )

    def _facts(self, file_id: int) -> tuple[str, ...]:
        """The file's module-resolution facts, but for the requirements a root
        ``requirements.txt`` reaches: the sync records those apart, once the repository's
        files are written, and reports their changes itself (:meth:`requirements_changed`)."""
        return tuple(
            sorted(
                claim.meta_json or ""
                for claim in self._knowledge.list_module_facts(file_id)
                if claim.meta_json
                and MODULE_RESOLUTION in claim.meta_json
                and json.loads(claim.meta_json).get(MODULE_RESOLUTION) != PYTHON_REQUIREMENTS
            )
        )

    def _reaching(self, names: Iterable[str], keys: Iterable[str]) -> set[int]:
        """Repositories whose root ``requirements.txt`` reaches a requirement of one of these
        PyPI names (PEP 503) or legacy keys, as recorded for the files it reaches."""
        names, keys = set(names), set(keys)
        if not names and not keys:
            return set()
        found: set[int] = set()
        for repo_id, _, meta in self._knowledge.list_facts_of_kind(REQUIREMENTS_FACTS):
            facts = json.loads(json.loads(meta or "{}").get("facts") or "{}")
            for _, name, _ in facts.get("requires", []):
                if normalize(PYPI, name) in names or legacy_key(name) in keys:
                    found.add(repo_id)
                    break
        return found

    def _required_keys(self) -> set[str]:
        """The legacy keys of the top-level heads this run changed (see the module
        docstring): whoever requires one by the legacy convention may link otherwise."""
        if self._dependencies is None or not self.heads:
            return set()
        keys: set[str] = set()
        for repo_id, change in sorted(self.heads.items()):
            rows = self._knowledge.files_of_repository(repo_id)
            paths = {path for _, path in rows} | change.gone
            projects = {_directory(p) for p in paths if PurePosixPath(p).name in _PROJECT_FILES}
            heads = set(change.names)
            stated_roots = set(change.rebased)
            for file_id, path in rows:
                if PurePosixPath(path).name in _PROJECT_FILES:
                    facts = self._facts(file_id)
                    heads |= _layout_names(facts)
                    stated_roots |= _layout_roots(facts, _directory(path))
            bases = {"", "src", *projects, *(_join(d, "src") for d in projects), *stated_roots}
            rebased = set(change.rebased)
            for path in change.touched:
                rebased |= _rebased(path, projects)
            heads |= top_level_heads(change.touched, bases)
            if rebased:
                heads |= top_level_heads(paths, rebased)
            keys |= {legacy_key(head) for head in heads}
        return keys

    def _compare(self, repo_id: int, file_id: int, before: FileState, after: FileState) -> None:
        if before.surface != after.surface:
            self.surface.add(file_id)
        if before.facts != after.facts:
            self.repositories.add(repo_id)
            # A project appearing, going or changing its layout may change what every base
            # of the repository means, and the names its facts state.
            [(_, path)] = self._knowledge.file_locations([file_id]).values()
            change = self.heads[repo_id]
            change.touched.add(path)
            directory = _directory(path)
            change.rebased |= {"", "src", directory, _join(directory, "src")}
            change.rebased |= _layout_roots(before.facts, directory)
            change.rebased |= _layout_roots(after.facts, directory)
            change.names |= _layout_names(before.facts) | _layout_names(after.facts)
        if before.packages != after.packages:
            # Declarations are read only for this repository's own files; provisions for
            # them too (``provided-locally``), and for every file declaring the package.
            self.package_repositories.add(repo_id)
            provided = {row for row in before.packages if row[1] == "provided"} ^ {
                row for row in after.packages if row[1] == "provided"
            }
            self.packages |= {(row[0], row[2]) for row in provided}


def export_surface(file_id: int, meta_json: str | None, symbols: Sequence[SymbolRow]) -> str:
    """What a trace passing through this file reads from it, whatever it lands on.

    The export table, with each well-formed ``local`` binding's line replaced by whether
    it names exactly one declaration (:func:`~repoatlas.graph.uses.declared_entity`, as a
    trace reads it). A use that lands on a declaration here is re-resolved whenever the
    file is written, so the line it lands on need not be part of this.
    """
    table = table_from_meta(meta_json) if meta_json else None
    if table is None:
        return "none"
    declared = ResolutionIndex.build([], list(symbols)).symbols_by_file.get(file_id)
    names: dict[str, Any] = {}
    for exported, spec in table.get("names", {}).items():
        if spec and spec[0] == "local" and len(spec) == 3 and spec[2].isdigit():
            found = declared_entity(declared, spec[1], int(spec[2]))
            names[exported] = ["local", spec[1], "declared" if found is not None else "none"]
        else:
            names[exported] = spec
    # Every other key a table records is read by a trace as it is (a Python table's
    # module ``__getattr__``); a TypeScript table has none, so its surface is unchanged.
    rest = {key: value for key, value in table.items() if key not in ("names", "star")}
    return json.dumps({"names": names, "star": table.get("star", []), **rest}, sort_keys=True)


@dataclass(frozen=True, slots=True)
class UsesPlan:
    """The ``USES`` edges of the files in scope, computed but not yet written."""

    scope: UsesScope
    edges: tuple[EdgeRecord, ...] = ()
    #: (consumer file, file whose export table its uses read), for every file in scope.
    reads: tuple[tuple[int, int], ...] = ()
    work: UsesWork = field(default_factory=UsesWork)


@dataclass(frozen=True, slots=True)
class UsesApplied:
    """What applying a plan wrote, and the totals the sync reports."""

    work: UsesWork
    #: Every ``USES`` edge the global pass now holds, and every use it tried to resolve.
    edges: int
    claims: int


def stored_scope(knowledge: SqliteKnowledgeStore) -> UsesScope | None:
    """Every file, with the reason, unless the stored edges are known complete."""
    state = knowledge.uses_links_state()
    if state == COMPLETE:
        return None
    if state is None:
        return UsesScope.everything("no USES edges recorded yet")
    if state == PENDING:
        return UsesScope.everything("the previous sync did not finish")
    return UsesScope.everything("USES resolution changed")


def plan_uses(
    knowledge: SqliteKnowledgeStore,
    index: ResolutionIndex,
    records: Sequence[DependencyRecord],
    *,
    producer: str,
    scope: UsesScope,
) -> UsesPlan:
    """Resolve the uses of the files in ``scope``. **Writes nothing.**"""
    files = None if scope.files is None else sorted(scope.files)
    claims = knowledge.list_use_claims(files)
    imports: dict[int, list[UnresolvedRefRecord]] = {}
    uses: list[UnresolvedRefRecord] = []
    for claim in claims:
        if claim.edge_type is EdgeType.IMPORTS:
            imports.setdefault(claim.file_id, []).append(claim)
        elif claim.target_key != USES_EXPORT_TABLE:
            uses.append(claim)

    tables = _Tables(knowledge)
    resolver = use_resolver(knowledge, index, records, tables)
    python_resolver = python_use_resolver(knowledge, index, records, tables)
    by_id = {row.file_id: row for rows in index.files_by_repo.values() for row in rows.values()}

    edges: list[EdgeRecord] = []
    for claim in uses:
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        meta = json.loads(claim.meta_json) if claim.meta_json else {}
        python = str(meta.get("parser", "")).startswith("python")
        found = (python_resolver if python else resolver).resolve(origin, meta)
        if found is None:
            continue
        entity_id, fields = found
        crossing = fields.get(CROSSING)
        if isinstance(crossing, dict):
            line = _import_line(imports.get(claim.file_id, []), meta)
            if line is not None:
                crossing["imported_at"] = line
        edges.append(
            EdgeRecord(
                src_id=claim.src_id,
                dst_id=entity_id,
                type=claim.edge_type,
                confidence=claim.confidence,
                repo_id=origin.repo_id,
                file_id=origin.file_id,
                line_start=claim.line_start,
                line_end=claim.line_end,
                parser=producer,
                resolution=use_resolution(meta, **fields),
            )
        )

    consumers = {claim.file_id for claim in uses}
    read_by: dict[int, set[int]] = {}
    for traced in (resolver, python_resolver):
        for consumer, found in traced.reads.items():
            read_by.setdefault(consumer, set()).update(found)
    reads = tuple(
        (consumer, read) for consumer in sorted(read_by) for read in sorted(read_by[consumer])
    )
    work = UsesWork(
        mode="global" if scope.files is None else "incremental",
        reason=scope.reason,
        files_invalidated=len(consumers) if scope.files is None else len(scope.files),
        own=scope.own,
        landed=scope.landed,
        readers=scope.readers,
        repository=scope.repository,
        package=scope.package,
        repositories_invalidated=scope.repositories,
        packages_invalidated=scope.packages,
        claims_examined=len(uses),
        claims_resolved=len(edges),
        reads_written=len(reads),
        repositories_touched=len({by_id[f].repo_id for f in consumers if f in by_id}),
    )
    return UsesPlan(scope=scope, edges=tuple(edges), reads=reads, work=work)


def use_resolver(
    knowledge: SqliteKnowledgeStore,
    index: ResolutionIndex,
    records: Sequence[DependencyRecord],
    tables: _Tables | None = None,
) -> UseResolver:
    """The resolver a pass resolves TypeScript uses with, built from indexed facts alone."""
    modules = ModuleResolver(
        index, knowledge.list_module_facts(), {}, declared=declared_constraints(index, records)
    )
    return UseResolver(
        modules, index, tables or _Tables(knowledge), PackageLinks(modules, index, records)
    )


def python_use_resolver(
    knowledge: SqliteKnowledgeStore,
    index: ResolutionIndex,
    records: Sequence[DependencyRecord],
    tables: _Tables | None = None,
) -> PythonUseResolver:
    """The resolver a pass resolves Python uses with, built from indexed facts alone."""
    modules = PythonModules(index, knowledge.list_module_facts())
    return PythonUseResolver(
        modules, index, tables or _Tables(knowledge), PythonPackageLinks(modules, index, records)
    )


def apply_uses(knowledge: SqliteKnowledgeStore, plan: UsesPlan, *, producer: str) -> UsesApplied:
    """Replace the planned files' ``USES`` edges. Joins the caller's transaction."""
    files = None if plan.scope.files is None else sorted(plan.scope.files)
    with knowledge.transaction():
        deleted = knowledge.delete_use_edges(producer, files)
        knowledge.add_edges(plan.edges)
        knowledge.replace_use_reads(files, plan.reads)
        knowledge.set_uses_links_state(COMPLETE)
        edges = knowledge.count_use_edges(producer)
        claims = knowledge.count_use_claims()
    work = plan.work.model_copy(
        update={"edges_deleted": deleted, "edges_inserted": len(plan.edges)}
    )
    return UsesApplied(work=work, edges=edges, claims=claims)


class _Tables:
    """Export tables by file id, read in one query and decoded as traces ask for them.

    One query for all of them, even when only a few files' uses are resolved: a trace
    may pass through any barrel, and asking for each file's table in turn scanned the
    export tables once per file.
    """

    def __init__(self, knowledge: SqliteKnowledgeStore) -> None:
        self._raw: dict[int, str] = knowledge.list_export_tables(None)
        self._parsed: dict[int, dict[str, Any] | None] = {}

    def __call__(self, file_id: int) -> dict[str, Any] | None:
        if file_id not in self._parsed:
            raw = self._raw.get(file_id)
            self._parsed[file_id] = table_from_meta(raw) if raw else None
        return self._parsed[file_id]


def _import_line(siblings: Sequence[UnresolvedRefRecord], meta: dict[str, str]) -> int | None:
    """The line of the import statement that binds a use's local name, when one does.

    Found among the same file's own claims: the import of that specifier whose bindings
    include the name. The first by line when the file imports it twice.
    """
    specifier, local = meta.get("module"), meta.get("local")
    if not specifier or not local:
        return None
    if str(meta.get("bound_at") or "").isdigit():
        # A Python use carries its import's line (repoatlas.parsers.py_uses).
        return int(meta["bound_at"])
    lines = sorted(
        claim.line_start
        for claim in siblings
        if claim.edge_type is EdgeType.IMPORTS
        and claim.line_start is not None
        and claim.target_key == specifier
        and claim.meta_json
        and _binds(json.loads(claim.meta_json), local)
    )
    return lines[0] if lines else None


def _binds(meta: dict[str, str], local: str) -> bool:
    """Whether an import (never a re-export, which binds nothing) binds ``local``."""
    if meta.get("role") == "re-export":
        return False
    return any(bound == local for bound, _ in _bound_names(meta))


def _bound_names(meta: dict[str, str]) -> list[tuple[str, str | None]]:
    """(local, exported) for each name one import binds.

    Reads the parser's binding record when there is one, and otherwise the older
    ``names`` list, whose ``type X`` and ``A as B`` spellings are taken apart here.
    """
    if any(key in meta for key in ("bindings", "default", "namespace")):
        found: list[tuple[str, str | None]] = [
            (local, name) for name, local in binding_pairs(meta.get("bindings"))
        ]
        if meta.get("default"):
            found.append((meta["default"], "default"))
        if meta.get("namespace"):
            found.append((meta["namespace"], None))
        return found
    found = []
    for raw in (meta.get("names") or "").split(","):
        name = raw.strip().removeprefix("type ").strip()
        if not name:
            continue
        exported, _, local = name.partition(" as ")
        found.append((local.strip() or exported.strip(), exported.strip()))
    return found
