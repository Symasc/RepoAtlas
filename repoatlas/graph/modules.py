"""Workspace-aware module resolution for the global link pass.

A relative specifier names a file by its text. A bare one -- ``@acme/contracts``,
``@/api/client`` -- names a file only through configuration, and this module reads
that configuration from the facts recorded when it was indexed
(:mod:`repoatlas.parsers.module_facts`), never from the working tree. It resolves
the uses a declaration makes of imported names, and nothing else: the import,
reference and call edges the global pass writes are resolved exactly as they were
before this module existed. It reads:

* **tsconfig path aliases**: the ``tsconfig.json`` (or ``jsconfig.json``) nearest the
  importing file, with ``extends`` followed inside the repository; ``paths`` is
  matched as TypeScript matches it (an exact key, else the wildcard with the longest
  prefix, substitutions tried in order) against ``baseUrl`` or the declaring file's
  directory; a bare ``baseUrl`` is tried after ``paths``;
* **workspace packages**: a ``pnpm-workspace.yaml`` or a ``package.json`` with
  ``workspaces`` declares which directories are packages. A specifier resolves to a
  member of the importing file's own workspace indexed in its own repository, and
  only when exactly one such member has that name. That is the reach of this pass,
  not a claim that a workspace ends where a repository does: a member indexed as
  another repository is simply not decided here.

A package's entry comes from ``exports``, else ``source``, ``types``, ``typings``,
``module`` and ``main``. Every field present must lead to the same indexed file, or
the import stays unresolved. An entry inside build output (``main: dist/index.js``)
is mapped to source only on the package's own evidence, described below: no
``dist``-to-``src`` rewrite is ever assumed.

**Barrels.** For a named import, :meth:`ModuleResolver.definition` follows
``export { X } from``, renamed re-exports, ``export * as NS from`` and ``export *
from`` until it reaches the file whose own ``EXPORTS`` declare the name. An explicit
re-export wins over ``export *``; two ``export *`` paths to different definitions are
ambiguous and resolve to nothing, as TypeScript itself treats them; a cycle, more
than :data:`MAX_REEXPORT_HOPS` hops or more than :data:`MAX_TRACE_STEPS` steps in all
resolves to nothing, so no arrangement of barrels makes one lookup unbounded.

Everything :meth:`ModuleResolver.resolve` answers is repository-local by construction:
every map is keyed by the importing file's repository, and a path that climbs out of
its repository resolves to nothing. A same-named package in another repository is a
dependency fact (``DEPENDS_ON``), never an import target of these edges. Only the
package link that ``USES`` alone reads (:mod:`repoatlas.graph.packages`) enters another
repository's package, through :meth:`ModuleResolver.package_entry`.

**Build output.** A package usually names its entry in build output that is not in
the repository (``exports: ./dist/index.mjs``), whether it is published or a workspace
member built before its siblings run. A tsconfig in the package that declares both
``outDir`` and ``rootDir`` maps such an entry through that pair. Otherwise
:meth:`ModuleResolver.package_entry` maps it only when asked to -- a workspace member
and a package link both ask -- and only on the package's own evidence: the output
directory holds no indexed file, the package's tsconfig names exactly one source root
(its ``rootDir``, else its single ``include`` directory, which is the root ``tsc``
itself would infer), exactly one indexed source file answers the entry's stem there,
and every condition of the entry maps to that same file. The edge records which of
these mapped it (``mapped``).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from repoatlas.graph.resolution import ResolutionIndex, _normalize
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PACKAGE, TSCONFIG, WORKSPACE
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

#: Re-exporting files a name may pass through before resolution gives up.
MAX_REEXPORT_HOPS = 12
#: Re-export steps one name lookup may take in all. Barrels that ``export *`` from
#: each other have exponentially many paths; past this the name resolves to nothing.
MAX_TRACE_STEPS = 256
#: ``extends`` links followed before a tsconfig chain is abandoned.
_MAX_EXTENDS = 16
_PROJECT_CONFIGS = ("tsconfig.json", "jsconfig.json")
_ENTRY_FIELDS = ("source", "types", "typings", "module", "main")
#: Glob characters: an ``include`` entry holding one after its trailing wildcard is
#: stripped names no single directory.
_GLOB = re.compile(r"[*?{}\[\]!]")
#: The wildcard tails an ``include`` directory may carry: ``src``, ``src/**``,
#: ``src/**/*`` and ``src/**/*.ts`` all name ``src``.
_INCLUDE_TAIL = re.compile(r"(/\*\*)?(/\*[^/]*)?/?$")
#: Build-output suffixes, longest first, mapped back to a source stem.
_OUTPUT_SUFFIXES = (".d.mts", ".d.cts", ".d.ts", ".mjs", ".cjs", ".jsx", ".js")
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",), ".jsx": (".tsx",)}

RELATIVE = "relative"
TSCONFIG_PATHS = "tsconfig-paths"
TSCONFIG_BASE_URL = "tsconfig-base-url"
WORKSPACE_PACKAGE = "workspace-package"


class _Ambiguous:
    __slots__ = ()


_AMBIGUOUS = _Ambiguous()


@dataclass(frozen=True, slots=True)
class ModuleTarget:
    """The file a specifier names, and how that was established."""

    file: FileRow
    basis: str
    #: Small, sorted provenance: the tsconfig and pattern, or the package and entry.
    detail: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """The file one package entry names, or why it names none.

    ``reason`` is set exactly when ``file`` is ``None``: ``entry-not-exported`` (the
    package's ``exports`` list no root), ``subpath-not-exported``, ``entry-ambiguous``
    (its conditions lead to different files), ``subpath-outside-package`` (the subpath
    climbs out of the package) or ``entry-unresolved``.
    """

    file: FileRow | None
    fields: tuple[str, ...] = ()
    #: How an entry in build output was mapped to source, as sorted pairs; empty when
    #: the entry named source directly.
    mapped: tuple[tuple[str, str], ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Definition:
    """Where an imported name is defined, reached from the module it was imported from."""

    file: FileRow
    #: The defining symbol, when exactly one declaration carries the name.
    symbol_id: int | None
    #: Repository-relative paths of the re-exporting files passed through, in order.
    via: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Reexport:
    specifier: str
    kind: str  # "*", "namespace" or "named"
    #: exported name -> imported name, for named re-exports.
    names: tuple[tuple[str, str], ...] = ()
    namespace: str | None = None


@dataclass(slots=True)
class _Workspace:
    root: str
    members: dict[str, list[str]] = field(default_factory=dict)


def _join(directory: str, path: str) -> str | None:
    if path.startswith("/"):
        return None
    return _normalize(f"{directory}/{path}" if directory else path)


@lru_cache(maxsize=1 << 16)
def _parent(rel_path: str) -> str:
    parent = str(PurePosixPath(rel_path).parent)
    return "" if parent == "." else parent


def _inside(path: str, directory: str) -> bool:
    return directory == "" or path == directory or path.startswith(directory + "/")


def _glob(pattern: str) -> re.Pattern[str]:
    out = []
    index = 0
    pattern = pattern.strip().strip("/")
    pattern = pattern.removeprefix("./")
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def split_package(specifier: str) -> tuple[str, str] | None:
    """``@scope/name/sub/path`` -> (``@scope/name``, ``sub/path``)."""
    parts = specifier.split("/")
    if specifier.startswith("@"):
        if len(parts) < 2 or not parts[1]:
            return None
        return "/".join(parts[:2]), "/".join(parts[2:])
    if not parts[0]:
        return None
    return parts[0], "/".join(parts[1:])


def binding_pairs(value: str | None) -> list[tuple[str, str]]:
    """``A,B:C`` -> [(A, A), (B, C)]."""
    pairs = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        first, _, second = item.partition(":")
        pairs.append((first, second or first))
    return pairs


class ModuleResolver:
    """Resolves specifiers and traces imported names, from indexed facts only."""

    def __init__(
        self,
        index: ResolutionIndex,
        claims: list[UnresolvedRefRecord],
        exported: dict[int, dict[str, list[int]]],
    ) -> None:
        self._index = index
        self._exported = exported
        self._by_id: dict[int, FileRow] = {
            row.file_id: row for rows in index.files_by_repo.values() for row in rows.values()
        }
        self._tsconfigs: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        #: Each tsconfig's ``include``, kept apart: a config that records nothing else
        #: stays invisible to import resolution, exactly as before ``include`` was read.
        self._includes: dict[int, dict[str, list[str]]] = defaultdict(dict)
        #: Every directory that holds an indexed file, per repository; built on demand.
        self._occupied: dict[int, frozenset[str]] = {}
        self._packages: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._workspaces: dict[int, list[_Workspace]] = defaultdict(list)
        self._reexports: dict[int, list[_Reexport]] = defaultdict(list)
        self._effective: dict[tuple[int, str], tuple[dict[str, Any], int]] = {}
        #: What each (repository, directory, specifier) resolved to in this pass. A
        #: specifier's target depends on the importing file only through its repository and
        #: its directory: relative paths are joined to the directory, the project config is
        #: the nearest one above it, and the workspace is the innermost one holding it.
        self._resolved: dict[tuple[int, str, str], ModuleTarget | None] = {}
        self._build_dirs_of: dict[tuple[int, str], list[tuple[str, str]]] = {}
        self._source_roots: dict[tuple[int, str], tuple[str, str] | None] = {}
        self._traced: dict[tuple[int, str], Definition | None] = {}
        #: Re-export steps taken by the current lookup, and by all lookups so far.
        self._steps = 0
        self.steps = 0
        declared: list[tuple[int, str, list[str]]] = []
        for claim in claims:
            meta = json.loads(claim.meta_json) if claim.meta_json else {}
            origin = self._by_id.get(claim.file_id)
            if origin is None:
                continue
            kind = meta.get(MODULE_RESOLUTION)
            if kind:
                facts = json.loads(meta.get("facts") or "{}")
                if kind == TSCONFIG:
                    include = facts.pop("include", None)
                    if include:
                        self._includes[origin.repo_id][origin.rel_path] = include
                    if facts:
                        self._tsconfigs[origin.repo_id][origin.rel_path] = facts
                elif kind == PACKAGE:
                    self._packages[origin.repo_id][_parent(origin.rel_path)] = facts
                    if facts.get("workspaces"):
                        declared.append(
                            (origin.repo_id, _parent(origin.rel_path), facts["workspaces"])
                        )
                elif kind == WORKSPACE and facts.get("packages"):
                    declared.append((origin.repo_id, _parent(origin.rel_path), facts["packages"]))
            elif meta.get("role") == "re-export" and meta.get("reexport"):
                self._reexports[claim.file_id].append(
                    _Reexport(
                        specifier=claim.target_key,
                        kind=meta["reexport"],
                        names=tuple(
                            (exported, imported)
                            for imported, exported in binding_pairs(meta.get("bindings"))
                        ),
                        namespace=meta.get("namespace"),
                    )
                )
        for repo_id, root, globs in sorted(declared):
            self._workspaces[repo_id].append(self._members(repo_id, root, globs))
        for reexports in self._reexports.values():
            reexports.sort(key=lambda r: (r.kind, r.specifier, r.names, r.namespace or ""))

    # -- workspaces ----------------------------------------------------------

    def _members(self, repo_id: int, root: str, globs: list[str]) -> _Workspace:
        include = [_glob(g) for g in globs if not g.startswith("!")]
        exclude = [_glob(g[1:]) for g in globs if g.startswith("!")]
        workspace = _Workspace(root=root)
        for directory, facts in sorted(self._packages[repo_id].items()):
            name = facts.get("name")
            if not isinstance(name, str) or directory == root or not _inside(directory, root):
                continue
            relative = directory[len(root) + 1 :] if root else directory
            if any(p.match(relative) for p in include) and not any(
                p.match(relative) for p in exclude
            ):
                workspace.members.setdefault(name, []).append(directory)
        return workspace

    def _workspace_of(self, origin: FileRow) -> _Workspace | None:
        found = [
            w for w in self._workspaces.get(origin.repo_id, []) if _inside(origin.rel_path, w.root)
        ]
        return max(found, key=lambda w: len(w.root)) if found else None

    # -- specifiers ----------------------------------------------------------

    def resolve(self, origin: FileRow, specifier: str) -> ModuleTarget | None:
        """The indexed file a specifier written in ``origin`` names, or ``None``."""
        key = (origin.repo_id, _parent(origin.rel_path), specifier)
        if key not in self._resolved:
            self._resolved[key] = self._resolve(origin, specifier)
        return self._resolved[key]

    def _resolve(self, origin: FileRow, specifier: str) -> ModuleTarget | None:
        if not specifier or "$" in specifier or "{" in specifier:
            return None
        if specifier.startswith((".", "/")):
            found = self._index.resolve_module(origin, specifier, "node")
            return ModuleTarget(found, RELATIVE) if found else None
        if specifier.startswith("node:"):
            return None
        aliased = self._by_tsconfig(origin, specifier)
        if aliased is not None:
            return aliased
        return self._by_workspace(origin, specifier)

    def _module_file(self, repo_id: int, path: str | None) -> FileRow | None:
        if path is None:
            return None
        return self._index.file(repo_id, path) or self._index._first_existing(
            repo_id, [path], "node"
        )

    def _project_config(self, origin: FileRow) -> str | None:
        directory = _parent(origin.rel_path)
        configs = self._tsconfigs.get(origin.repo_id, {})
        while True:
            for name in _PROJECT_CONFIGS:
                candidate = f"{directory}/{name}" if directory else name
                if candidate in configs:
                    return candidate
            if not directory:
                return None
            directory = _parent(directory)

    def _options(self, repo_id: int, config: str) -> dict[str, Any]:
        """Effective ``compilerOptions`` for one config, each path anchored where declared.

        The answer is the one following this config's own ``extends`` chain gives: a cycle
        reads an empty parent rather than looping, and a chain longer than
        :data:`_MAX_EXTENDS` ends there. An answer is remembered for another config's chain
        only when neither cut it short and the chain still fits the bound from there, so
        no config's options depend on which config was asked about first.
        """
        return self._chain(repo_id, config, frozenset(), 0)[0]

    def _chain(
        self, repo_id: int, config: str, visiting: frozenset[str], depth: int
    ) -> tuple[dict[str, Any], int | None]:
        """The options, and how many ``extends`` links lie below; ``None`` when cut short."""
        key = (repo_id, config)
        known = self._effective.get(key)
        if known is not None and depth + known[1] <= _MAX_EXTENDS:
            return known
        own = self._tsconfigs.get(repo_id, {}).get(config)
        if own is None:
            return {}, 0
        if depth > _MAX_EXTENDS or config in visiting:
            return {}, None
        merged: dict[str, Any] = {}
        below: int | None = 0
        directory = _parent(config)
        for parent in own.get("extends", []):
            target = self._extends(repo_id, directory, parent)
            if target is not None:
                options, links = self._chain(repo_id, target, visiting | {config}, depth + 1)
                merged.update(options)
                below = None if links is None or below is None else max(below, links + 1)
        for option in ("baseUrl", "rootDir", "outDir"):
            if option in own:
                anchored = _join(directory, own[option])
                if anchored is not None:
                    merged[option] = anchored
        if "paths" in own:
            merged["paths"] = own["paths"]
            merged["pathsBase"] = directory
            merged["pathsConfig"] = config
        if below is not None:
            self._effective[key] = (merged, below)
        return merged, below

    def _extends(self, repo_id: int, directory: str, value: str) -> str | None:
        configs = self._tsconfigs.get(repo_id, {})
        if value.startswith("."):
            path = _join(directory, value)
            for candidate in (path, f"{path}.json" if path else None):
                if candidate and candidate in configs:
                    return candidate
            return None
        split = split_package(value)
        workspace = next(
            (w for w in self._workspaces.get(repo_id, []) if _inside(directory, w.root)), None
        )
        if split is None or workspace is None:
            return None
        homes = workspace.members.get(split[0], [])
        if len(homes) != 1:
            return None
        sub = split[1] or "tsconfig.json"
        path = _join(homes[0], sub)
        for candidate in (path, f"{path}.json" if path else None):
            if candidate and candidate in configs:
                return candidate
        return None

    def _by_tsconfig(self, origin: FileRow, specifier: str) -> ModuleTarget | None:
        config = self._project_config(origin)
        if config is None:
            return None
        options = self._options(origin.repo_id, config)
        paths: dict[str, list[str]] = options.get("paths") or {}
        base = options.get("baseUrl", options.get("pathsBase", ""))
        match = _match_paths(paths, specifier)
        if match is not None:
            pattern, captured = match
            for substitution in paths[pattern]:
                found = self._module_file(
                    origin.repo_id, _join(base, substitution.replace("*", captured, 1))
                )
                if found is not None:
                    return ModuleTarget(
                        found,
                        TSCONFIG_PATHS,
                        (("pattern", pattern), ("tsconfig", options.get("pathsConfig", config))),
                    )
        if "baseUrl" in options:
            found = self._module_file(origin.repo_id, _join(options["baseUrl"], specifier))
            if found is not None:
                return ModuleTarget(found, TSCONFIG_BASE_URL, (("tsconfig", config),))
        return None

    def aliased(self, origin: FileRow, specifier: str) -> bool:
        """Whether a ``paths`` pattern of ``origin``'s own tsconfig claims ``specifier``.

        The compiler maps such a specifier through the pattern, so what it names is the
        project's decision, not a package's, even when no mapped file is indexed.
        """
        config = self._project_config(origin)
        if config is None:
            return False
        paths = self._options(origin.repo_id, config).get("paths") or {}
        return _match_paths(paths, specifier) is not None

    def _by_workspace(self, origin: FileRow, specifier: str) -> ModuleTarget | None:
        workspace = self._workspace_of(origin)
        split = split_package(specifier)
        if workspace is None or split is None:
            return None
        name, subpath = split
        homes = workspace.members.get(name, [])
        if len(homes) != 1:
            return None  # absent, or two members share the name: never a guess
        directory = homes[0]
        entry = self.package_entry(origin.repo_id, directory, subpath, build_output=True)
        if entry.file is None:
            return None
        detail: list[tuple[str, Any]] = [
            ("entry", ",".join(entry.fields)),
            ("manifest", f"{directory}/package.json" if directory else "package.json"),
            ("package", name),
        ]
        if entry.mapped:
            detail.append(("mapped", dict(entry.mapped)["by"]))
        return ModuleTarget(entry.file, WORKSPACE_PACKAGE, tuple(sorted(detail)))

    def package_entry(
        self, repo_id: int, directory: str, subpath: str, *, build_output: bool = False
    ) -> PackageEntry:
        """The file the package at ``directory`` names for ``subpath``, or why none.

        ``build_output`` admits the mapping from build output to source described in the
        module docstring. A subpath with a ``.`` or ``..`` segment names nothing: Node never
        resolves one to a file of the package it starts from.
        """
        if subpath and any(part in (".", "..") for part in subpath.split("/")):
            return PackageEntry(None, reason="subpath-outside-package")
        facts = self._packages[repo_id].get(directory, {})
        exports = facts.get("exports")
        if exports is not None:
            candidates = _export_leaves(exports, subpath)
            if not candidates:
                reason = "subpath-not-exported" if subpath else "entry-not-exported"
                return PackageEntry(None, reason=reason)
        elif subpath:
            candidates = [("subpath", subpath)]
        else:
            candidates = [(f, facts[f]) for f in _ENTRY_FIELDS if isinstance(facts.get(f), str)]
            candidates = candidates or [("index", "index")]
        found: dict[int, tuple[FileRow, list[str], tuple[tuple[str, str], ...]]] = {}
        for label, raw in candidates:
            source = self._source_of(repo_id, directory, raw, build_output=build_output)
            if isinstance(source, _Ambiguous):
                return PackageEntry(None, reason="entry-ambiguous")
            if source is None:
                continue
            row, mapped = source
            entry = found.setdefault(row.file_id, (row, [], mapped))
            entry[1].append(label)
        if len(found) > 1:
            return PackageEntry(None, reason="entry-ambiguous")
        if not found:
            return PackageEntry(None, reason="entry-unresolved")
        row, labels, mapped = next(iter(found.values()))
        return PackageEntry(row, tuple(labels), mapped)

    def _source_of(
        self, repo_id: int, directory: str, raw: str, *, build_output: bool = False
    ) -> tuple[FileRow, tuple[tuple[str, str], ...]] | _Ambiguous | None:
        path = _join(directory, raw)
        if path is None:
            return None
        direct = self._module_file(repo_id, path)
        if direct is not None:
            return direct, ()
        suffix = next((s for s in _JS_TO_TS if path.endswith(s)), None)
        if suffix is not None:
            stem = path[: -len(suffix)]
            beside = [
                row
                for ext in _JS_TO_TS[suffix]
                if (row := self._index.file(repo_id, stem + ext)) is not None
            ]
            if len(beside) == 1:
                return beside[0], ()
        mapped: dict[int, FileRow] = {}
        for out_dir, root_dir in self._build_dirs(repo_id, directory):
            if not _inside(path, out_dir) or path == out_dir:
                continue
            relative = path[len(out_dir) + 1 :] if out_dir else path
            output = next((s for s in _OUTPUT_SUFFIXES if relative.endswith(s)), None)
            if output is None:
                continue
            stem = _join(root_dir, relative[: -len(output)])
            if stem is None:
                continue
            row = self._first_source(repo_id, stem)
            if row is not None:
                mapped[row.file_id] = row
        if len(mapped) > 1:
            return _AMBIGUOUS
        if mapped:
            return next(iter(mapped.values())), (("by", "outDir->rootDir"),)
        if build_output:
            return self._build_output(repo_id, directory, path)
        return None

    def _build_output(
        self, repo_id: int, directory: str, path: str
    ) -> tuple[FileRow, tuple[tuple[str, str], ...]] | _Ambiguous | None:
        """An entry in unindexed build output, mapped to the one source file it names.

        ``dist/sub/index.mjs`` in a package whose tsconfig names the single source root
        ``src`` is ``src/sub/index.*``, when exactly one indexed file answers. Anything
        less than that -- output that is indexed, two roots, two answering files, a root
        outside the package -- maps to nothing.
        """
        relative = path[len(directory) + 1 :] if directory else path
        head, _, rest = relative.partition("/")
        if not rest or not _inside(path, directory) or path == directory:
            return None
        if _join(directory, head) in self._occupied_dirs(repo_id):
            return None  # the output is indexed: it is not build output
        suffix = next((s for s in _OUTPUT_SUFFIXES if rest.endswith(s)), None)
        if suffix is None:
            return None
        root = self._source_root(repo_id, directory)
        if root is None:
            return None
        root_path, basis = root
        stem = _join(root_path, rest[: -len(suffix)])
        if stem is None:
            return None
        answering = self._sources(repo_id, stem)
        if len(answering) > 1:
            return _AMBIGUOUS
        if not answering:
            return None
        row = answering[0]
        return row, (("by", basis), ("from", relative), ("to", row.rel_path))

    def _source_root(self, repo_id: int, directory: str) -> tuple[str, str] | None:
        """The one source root the package's own tsconfig names, and which option named it."""
        key = (repo_id, directory)
        if key not in self._source_roots:
            self._source_roots[key] = self._find_source_root(repo_id, directory)
        return self._source_roots[key]

    def _find_source_root(self, repo_id: int, directory: str) -> tuple[str, str] | None:
        roots: set[tuple[str, str]] = set()
        for config in sorted({*self._tsconfigs.get(repo_id, {}), *self._includes.get(repo_id, {})}):
            if _parent(config) != directory:
                continue
            options = self._options(repo_id, config)
            if "rootDir" in options:
                roots.add((options["rootDir"], "tsconfig rootDir"))
                continue
            for entry in self._includes.get(repo_id, {}).get(config, []):
                stripped = _INCLUDE_TAIL.sub("", entry.strip().removeprefix("./"))
                if not stripped or _GLOB.search(stripped):
                    return None  # an include this cannot read: the root is unknown
                anchored = _join(directory, stripped)
                if anchored is None:
                    return None
                roots.add((anchored, "tsconfig include"))
        paths = {path for path, _ in roots}
        if len(paths) != 1:
            return None
        path = paths.pop()
        if not _inside(path, directory) or path == directory:
            return None
        return path, min(basis for root, basis in roots if root == path)

    def _occupied_dirs(self, repo_id: int) -> frozenset[str]:
        if repo_id not in self._occupied:
            found: set[str] = set()
            for rel_path in self._index.files_by_repo.get(repo_id, {}):
                parent = _parent(rel_path)
                while parent and parent not in found:
                    found.add(parent)
                    parent = _parent(parent)
            self._occupied[repo_id] = frozenset(found)
        return self._occupied[repo_id]

    def _sources(self, repo_id: int, stem: str) -> list[FileRow]:
        """Every indexed source file answering ``stem`` as a file or as a directory index."""
        return [
            row
            for base in (stem, f"{stem}/index")
            for suffix in _SOURCE_SUFFIXES
            if (row := self._index.file(repo_id, base + suffix)) is not None
        ]

    def _first_source(self, repo_id: int, stem: str) -> FileRow | None:
        for base in (stem, f"{stem}/index"):
            for suffix in _SOURCE_SUFFIXES:
                row = self._index.file(repo_id, base + suffix)
                if row is not None:
                    return row
        return None

    def _build_dirs(self, repo_id: int, directory: str) -> list[tuple[str, str]]:
        """(outDir, rootDir) pairs declared by the package's own tsconfig files."""
        key = (repo_id, directory)
        if key not in self._build_dirs_of:
            self._build_dirs_of[key] = self._find_build_dirs(repo_id, directory)
        return self._build_dirs_of[key]

    def _find_build_dirs(self, repo_id: int, directory: str) -> list[tuple[str, str]]:
        pairs = set()
        for config in self._tsconfigs.get(repo_id, {}):
            if _parent(config) != directory:
                continue
            options = self._options(repo_id, config)
            if "outDir" in options and "rootDir" in options:
                pairs.add((options["outDir"], options["rootDir"]))
        return sorted(pairs)

    # -- names ---------------------------------------------------------------

    def definition(self, module: FileRow, name: str) -> Definition | None:
        """Where ``name``, imported from ``module``, is defined; ``None`` if not exactly one."""
        self._steps = 0
        return self._trace(module, name, frozenset(), 0)[0]

    def _trace(
        self, module: FileRow, name: str, visiting: frozenset[tuple[int, str]], depth: int
    ) -> tuple[Definition | None, bool]:
        """The definition, and whether a cycle or the hop bound cut the search short.

        Only an uncut answer is remembered: one computed while a cycle was being cut
        depends on where the walk entered the cycle.
        """
        key = (module.file_id, name)
        if key in self._traced:
            return self._traced[key], False
        if key in visiting or depth > MAX_REEXPORT_HOPS or self._steps >= MAX_TRACE_STEPS:
            return None, True
        self._steps += 1
        self.steps += 1
        result, cut = self._trace_once(module, name, visiting | {key}, depth)
        if not cut:
            self._traced[key] = result
        return result, cut

    def _trace_once(
        self, module: FileRow, name: str, visiting: frozenset[tuple[int, str]], depth: int
    ) -> tuple[Definition | None, bool]:
        local = self._exported.get(module.file_id, {}).get(name)
        if local:
            return Definition(module, local[0] if len(local) == 1 else None), False
        if name == "default":
            return None, False
        reexports = self._reexports.get(module.file_id, [])
        for reexport in reexports:
            if reexport.kind == "named":
                imported = dict(reexport.names).get(name)
                if imported is None:
                    continue
                target = self.resolve(module, reexport.specifier)
                if target is None:
                    return None, False
                found, cut = self._trace(target.file, imported, visiting, depth + 1)
                return (None if found is None else _through(module, found)), cut
            if reexport.kind == "namespace" and reexport.namespace == name:
                target = self.resolve(module, reexport.specifier)
                if target is None:
                    return None, False
                return Definition(target.file, None, (module.rel_path,)), False
        found_all: dict[tuple[int, int | None], Definition] = {}
        cut_any = False
        for reexport in reexports:
            if reexport.kind != "*":
                continue
            target = self.resolve(module, reexport.specifier)
            if target is None:
                continue
            definition, cut = self._trace(target.file, name, visiting, depth + 1)
            cut_any = cut_any or cut
            if definition is not None:
                found_all.setdefault((definition.file.file_id, definition.symbol_id), definition)
        if len(found_all) != 1:
            return None, cut_any  # not re-exported, or two star exports disagree
        return _through(module, next(iter(found_all.values()))), cut_any


def _through(module: FileRow, definition: Definition) -> Definition:
    return Definition(definition.file, definition.symbol_id, (module.rel_path, *definition.via))


def _match_paths(paths: dict[str, list[str]], specifier: str) -> tuple[str, str] | None:
    """TypeScript's choice: an exact key, else the matching wildcard with the longest prefix."""
    if specifier in paths and "*" not in specifier:
        return specifier, ""
    best: tuple[int, str, str] | None = None
    for pattern in paths:
        if pattern.count("*") != 1:
            continue
        prefix, _, suffix = pattern.partition("*")
        if (
            len(specifier) >= len(prefix) + len(suffix)
            and specifier.startswith(prefix)
            and specifier.endswith(suffix)
        ):
            captured = specifier[len(prefix) : len(specifier) - len(suffix)]
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), pattern, captured)
    return None if best is None else (best[1], best[2])


def _export_leaves(exports: Any, subpath: str) -> list[tuple[str, str]]:
    """The targets ``exports`` gives a subpath, each labelled by its conditions."""
    key = f"./{subpath}" if subpath else "."
    if isinstance(exports, str | list):
        entry: Any = exports if key == "." else None
    elif isinstance(exports, dict) and exports and all(str(k).startswith(".") for k in exports):
        entry = exports.get(key)
        if entry is None:
            entry = _export_wildcard(exports, key)
    elif isinstance(exports, dict):
        entry = exports if key == "." else None
    else:
        entry = None
    leaves: list[tuple[str, str]] = []
    _collect(entry, "exports", leaves)
    return leaves


def _export_wildcard(exports: dict[str, Any], key: str) -> Any:
    for pattern, value in exports.items():
        if pattern.count("*") != 1:
            continue
        prefix, _, suffix = pattern.partition("*")
        if key.startswith(prefix) and key.endswith(suffix) and len(key) >= len(prefix + suffix):
            captured = key[len(prefix) : len(key) - len(suffix)]
            return _substitute(value, captured)
    return None


def _substitute(value: Any, captured: str) -> Any:
    if isinstance(value, str):
        return value.replace("*", captured)
    if isinstance(value, list):
        return [_substitute(v, captured) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, captured) for k, v in value.items()}
    return value


def _collect(entry: Any, label: str, leaves: list[tuple[str, str]]) -> None:
    if isinstance(entry, str):
        leaves.append((label, entry))
    elif isinstance(entry, list):
        for item in entry:
            _collect(item, label, leaves)
    elif isinstance(entry, dict):
        for condition, value in entry.items():
            _collect(value, f"{label}.{condition}", leaves)
