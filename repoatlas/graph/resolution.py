"""The resolution index used by the global link pass (ARCHITECTURE.md §5, §6).

Every rule for turning a written reference into a concrete target lives here, so
V1_SPEC §9's "exact match only, never fuzzy" is enforced in one auditable place
rather than in nine parsers.

Three rules hold throughout:

* a target is resolved only when the resolution is **deterministic** — a computed
  path or a templated name resolves to nothing;
* an **ambiguous** match resolves to nothing. Two managed repositories providing
  the same package name is a real situation, and picking one would be a guess;
* the index is built from what is currently persisted, so it can only ever point
  at content that exists on disk (V1_SPEC §12).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from repoatlas.models.parsing import EMBEDDED_DOCUMENT, ENUM_MEMBER, FIELD, EntityType
from repoatlas.storage.protocols import FileRow, SymbolRow

#: Candidate extensions per import flavor, tried in order.
_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "node": (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
}
_PYTHON_PACKAGE_FILES = ("__init__.py", "__init__.pyi")
_NODE_INDEX_STEMS = ("index",)

_FLAVOR_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "node",
    ".tsx": "node",
    ".mts": "node",
    ".cts": "node",
    ".js": "node",
    ".jsx": "node",
    ".mjs": "node",
    ".cjs": "node",
}

_SALT_SCHEME = "salt://"

#: ``file_roots`` is not readable, so a ``salt://`` match is inferred, never proven.
_SALT_ROOT_CONFIDENCE = 0.9
_SALT_SUFFIX_CONFIDENCE = 0.8


def flavor_for(rel_path: str) -> str | None:
    """Import flavor implied by a file's extension."""
    return _FLAVOR_BY_SUFFIX.get(PurePosixPath(rel_path).suffix)


def _normalize(path: str) -> str | None:
    """Collapse ``.``/``..`` without touching the filesystem.

    Returns ``None`` when the path escapes above its base, which the caller must
    treat as unresolvable rather than clamping to the root.
    """
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part in {".", "/"}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


@dataclass(slots=True)
class ResolutionIndex:
    """Everything currently indexed, arranged for exact lookup."""

    files_by_repo: dict[int, dict[str, FileRow]] = field(default_factory=dict)
    repo_roots: list[tuple[Path, int]] = field(default_factory=list)
    repo_names: dict[int, str] = field(default_factory=dict)
    #: Canonical repository identity by repo id. Keys are built from this, never
    #: from ``repo_names`` — two repositories may share a display name.
    repo_slugs: dict[int, str] = field(default_factory=dict)
    symbols_by_file: dict[int, dict[str, list[SymbolRow]]] = field(default_factory=dict)
    salt_states_by_repo: dict[int, dict[str, list[SymbolRow]]] = field(default_factory=dict)
    files_by_name: dict[str, list[FileRow]] = field(default_factory=dict)

    @classmethod
    def build(cls, files: list[FileRow], symbols: list[SymbolRow]) -> ResolutionIndex:
        index = cls()
        roots: dict[int, Path] = {}

        for row in files:
            index.files_by_repo.setdefault(row.repo_id, {})[row.rel_path] = row
            roots[row.repo_id] = row.repo_path
            index.repo_names[row.repo_id] = row.repo_name
            index.repo_slugs[row.repo_id] = row.repo_slug
            index.files_by_name.setdefault(PurePosixPath(row.rel_path).name, []).append(row)

        # Longest path first, so a nested repository wins over its parent.
        index.repo_roots = sorted(
            ((path, repo_id) for repo_id, path in roots.items()),
            key=lambda pair: len(pair[0].parts),
            reverse=True,
        )

        by_file: dict[int, dict[str, list[SymbolRow]]] = defaultdict(lambda: defaultdict(list))
        by_repo_states: dict[int, dict[str, list[SymbolRow]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for symbol in symbols:
            by_file[symbol.file_id][symbol.name].append(symbol)
            if symbol.qualified_name and symbol.qualified_name != symbol.name:
                by_file[symbol.file_id][symbol.qualified_name].append(symbol)
            if symbol.type is EntityType.SALT_STATE:
                by_repo_states[symbol.repo_id][symbol.name].append(symbol)

        index.symbols_by_file = {k: dict(v) for k, v in by_file.items()}
        index.salt_states_by_repo = {k: dict(v) for k, v in by_repo_states.items()}
        return index

    # -- files ---------------------------------------------------------------

    def file(self, repo_id: int, rel_path: str) -> FileRow | None:
        return self.files_by_repo.get(repo_id, {}).get(rel_path)

    def resolve_repo_relative_path(self, origin: FileRow, raw: str) -> FileRow | None:
        """Resolve a path written in ``origin`` against the managed set.

        A path that stays inside the origin repository resolves there. A path that
        climbs out — ``../python-service/src/app.py`` from a sibling repository —
        is resolved on the real filesystem and matched against every managed
        repository root, which is how "shell script referencing another repo"
        (V1_SPEC §9) becomes a cross-repository edge without guessing.
        """
        if not raw or "$" in raw or "{" in raw or "*" in raw:
            return None

        if raw.startswith("/"):
            return self._by_absolute(Path(raw))

        base = PurePosixPath(origin.rel_path).parent
        joined = _normalize(str(base / raw)) if str(base) != "." else _normalize(raw)
        if joined is not None:
            found = self.file(origin.repo_id, joined)
            if found is not None:
                return found
            # Also accept a path written relative to the repository root.
            found = self.file(origin.repo_id, _normalize(raw) or raw)
            if found is not None:
                return found

        absolute = (origin.repo_path / PurePosixPath(origin.rel_path).parent / raw).resolve()
        return self._by_absolute(absolute)

    def _by_absolute(self, absolute: Path) -> FileRow | None:
        for root, repo_id in self.repo_roots:
            if absolute == root or absolute.is_relative_to(root):
                relative = absolute.relative_to(root).as_posix()
                found = self.file(repo_id, relative)
                if found is not None:
                    return found
        return None

    # -- modules -------------------------------------------------------------

    def resolve_module(self, origin: FileRow, specifier: str, flavor: str) -> FileRow | None:
        """Resolve an import specifier to a file in the managed set."""
        if not specifier or "$" in specifier or "{" in specifier:
            return None
        if flavor == "python":
            return self._resolve_python(origin, specifier)
        if flavor == "node":
            return self._resolve_node(origin, specifier)
        return None

    def _candidates(self, base: str, flavor: str) -> list[str]:
        extensions = _EXTENSIONS[flavor]
        found = [f"{base}{extension}" for extension in extensions]
        if flavor == "python":
            # `base` is empty when the specifier resolves to the repository root,
            # which is what a repository that *is* the package looks like. Joining
            # unconditionally would ask for `/__init__.py`, an absolute path that
            # can never match a repository-relative one, so the root package was
            # invisible and `from . import helper` beside it resolved to nothing.
            found.extend(f"{base}/{name}" if base else name for name in _PYTHON_PACKAGE_FILES)
        elif flavor == "node":
            found.extend(
                f"{base}/{stem}{extension}"
                for stem in _NODE_INDEX_STEMS
                for extension in extensions
            )
        return found

    def _first_existing(self, repo_id: int, bases: list[str], flavor: str) -> FileRow | None:
        for base in bases:
            for candidate in self._candidates(base, flavor):
                found = self.file(repo_id, candidate)
                if found is not None:
                    return found
        return None

    def _resolve_python(self, origin: FileRow, specifier: str) -> FileRow | None:
        directory = PurePosixPath(origin.rel_path).parent

        if specifier.startswith("."):
            level = len(specifier) - len(specifier.lstrip("."))
            remainder = specifier[level:]
            base = directory
            for _ in range(level - 1):
                if base == PurePosixPath("."):
                    return None
                base = base.parent
            target = str(base / remainder.replace(".", "/")) if remainder else str(base)
            normalized = _normalize(target)
            return (
                None
                if normalized is None
                else self._first_existing(origin.repo_id, [normalized], "python")
            )

        relative = specifier.replace(".", "/")
        bases = [relative]
        # An absolute import may be rooted at any ancestor of the importing file,
        # which is how ``src/`` and ``lib/`` layouts resolve without configuration.
        ancestor = directory
        while str(ancestor) not in {".", ""}:
            bases.append(f"{ancestor}/{relative}")
            ancestor = ancestor.parent
        return self._first_existing(origin.repo_id, bases, "python")

    def resolve_relative_submodule(
        self, origin: FileRow, prefix: str, specifier: str
    ) -> FileRow | None:
        """``from . import helper`` → ``pkg/sub/helper.py``, but only inside a package.

        The extra condition over :meth:`resolve_module` is ``prefix``: the dots
        the import was written with must themselves resolve to a **package** —
        an ``__init__.py`` — in this repository, before the name after them may
        be read as a module in it.

        That condition is doing real work, and it is the difference between
        restoring a missing edge and inventing one. Two cases it decides:

        * ``from . import helper`` in a file that is in no package at all. At
          runtime that raises ``ImportError``; the module has no parent package
          to be relative to. Without the check, a ``helper.py`` sitting beside it
          would look like the answer.
        * ``from ... import x`` written with more dots than there is package
          above it. The prefix climbs out of the package and lands somewhere that
          is not one, so the name resolves to nothing rather than to whatever
          ``x.py`` happens to exist up there.

        Note this is *not* applied to :meth:`resolve_module`. An import that names
        its module — ``from .helper import X`` — is resolved as written; the
        package question is the user's. Here RepoAtlas is inferring that a name
        denotes a module, and an inference whose premise is unchecked is a guess.

        A package is identified by ``__init__.py``, which is the convention used
        everywhere else here; namespace packages are not recognized, and are not
        recognized by this either.
        """
        package = self._resolve_python(origin, prefix)
        if package is None or PurePosixPath(package.rel_path).name not in _PYTHON_PACKAGE_FILES:
            return None
        return self._resolve_python(origin, prefix + specifier)

    def _resolve_node(self, origin: FileRow, specifier: str) -> FileRow | None:
        if not specifier.startswith((".", "/")):
            return None  # a bare specifier is a package, resolved by ecosystem
        directory = PurePosixPath(origin.rel_path).parent
        target = _normalize(str(directory / specifier))
        if target is None:
            return None
        direct = self.file(origin.repo_id, target)
        return (
            direct if direct is not None else self._first_existing(origin.repo_id, [target], "node")
        )

    # -- symbols -------------------------------------------------------------

    def resolve_symbol(self, file_row: FileRow, name: str) -> SymbolRow | None:
        """A definition of ``name`` in ``file_row``, when there is exactly one.

        A Python file's ``symbol`` entities are its module values, which a use lands on
        (``USES_ONLY_KINDS``) and a callee matched by name never is.
        """
        exact = self.symbol_candidates(file_row, name)
        return exact[0] if len(exact) == 1 else None

    def symbol_candidates(self, file_row: FileRow, name: str) -> list[SymbolRow]:
        """Every definition of ``name`` in ``file_row`` a callee matched by name may land on.

        :meth:`resolve_symbol` links when this has exactly one; the outcome report counts it
        to tell *nothing declared* from *several declared*.
        """
        matches = self.symbols_by_file.get(file_row.file_id, {}).get(name, [])
        python = flavor_for(file_row.rel_path) == "python"
        # An enum member is reached through its enum, never by its bare name, and a model
        # field only through a use of its model (DF3b).
        return [
            s
            for s in matches
            if s.name == name
            and not (python and s.type is EntityType.SYMBOL)
            and s.meta.get("kind") not in {ENUM_MEMBER, EMBEDDED_DOCUMENT, FIELD}
        ]

    def salt_state_candidates(self, repo_id: int, name: str) -> int:
        """How many Salt states of this repository carry the ID ``name``."""
        return len(self.salt_states_by_repo.get(repo_id, {}).get(name, []))

    def salt_source_candidates(self, url: str) -> int:
        """How many managed files a ``salt://`` URL could name, by the rule
        :meth:`resolve_salt_source` applies: its written path is the file's path or its tail."""
        if not url.startswith(_SALT_SCHEME):
            return 0
        path = _normalize(url[len(_SALT_SCHEME) :])
        if not path:
            return 0
        name = PurePosixPath(path).name
        return sum(
            1
            for row in self.files_by_name.get(name, [])
            if row.rel_path == path or row.rel_path.endswith(f"/{path}")
        )

    def resolve_salt_state(self, repo_id: int, name: str) -> SymbolRow | None:
        """A Salt state ID, which is unique within a repository's state tree."""
        matches = self.salt_states_by_repo.get(repo_id, {}).get(name, [])
        return matches[0] if len(matches) == 1 else None

    # -- salt ----------------------------------------------------------------

    def resolve_salt_include(self, origin: FileRow, dotted: str) -> FileRow | None:
        """``include: - service.pillar`` → ``service/pillar.sls``.

        A leading dot means "relative to this state's directory", exactly as Salt
        itself resolves it.
        """
        if dotted.startswith("."):
            level = len(dotted) - len(dotted.lstrip("."))
            base = PurePosixPath(origin.rel_path).parent
            for _ in range(level - 1):
                base = base.parent
            target = str(base / dotted[level:].replace(".", "/"))
        else:
            target = dotted.replace(".", "/")
        normalized = _normalize(target)
        if normalized is None:
            return None
        for candidate in (f"{normalized}.sls", f"{normalized}/init.sls"):
            found = self.file(origin.repo_id, candidate)
            if found is not None:
                return found
        return None

    def resolve_salt_source(self, url: str) -> tuple[FileRow, float] | None:
        """``salt://service/files/x.jinja`` → the unique managed file at that path.

        Salt resolves this through ``file_roots``, which RepoAtlas cannot read, so
        **no** ``salt://`` resolution is proven. The match is on the full written
        path being that of exactly one managed file, and confidence stays below
        1.0 to record that the file-roots mapping was inferred: a path equal to a
        repository-relative path scores higher than one that merely ends there. An
        ambiguous match resolves to nothing at all.
        """
        if not url.startswith(_SALT_SCHEME):
            return None
        path = _normalize(url[len(_SALT_SCHEME) :])
        if not path:
            return None
        name = PurePosixPath(path).name
        matches = [
            row
            for row in self.files_by_name.get(name, [])
            if row.rel_path == path or row.rel_path.endswith(f"/{path}")
        ]
        if len(matches) != 1:
            return None
        exact = matches[0].rel_path == path
        return matches[0], _SALT_ROOT_CONFIDENCE if exact else _SALT_SUFFIX_CONFIDENCE
