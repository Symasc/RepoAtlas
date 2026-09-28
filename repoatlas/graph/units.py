"""Units: the smallest package a node belongs to, named from the index's own manifests.

A change-impact walk out of a shared declaration reaches dependents in many parts
of a monorepo, and a cut made on keys alone favours whichever part sorts first. A
*unit* is what lets a walk spread what it shows over those parts. It has to be
derived from facts the index already holds, never from names a benchmark would
recognise, so it is this: the nearest directory, at or above the node's file,
holding a manifest that *provides* a package (a ``package.json`` with a name, a
``pyproject.toml`` with a project, and so on — whatever the dependency parsers
recorded as ``provided``), in the node's own repository. A file under no such
directory belongs to its repository's root.

A node with no file — an environment variable, an HTTP operation, a module
specifier, a repository — is its own unit: it belongs to no package, and
grouping all of them together would bound unrelated hubs by one another.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


def package_directories(manifest_paths: Iterable[str]) -> tuple[str, ...]:
    """The directories of manifests that provide a package; ``""`` for the root."""
    return tuple(sorted({path.rpartition("/")[0] for path in manifest_paths}))


def unit_of(
    key: str,
    repository_slug: str | None,
    path: str | None,
    directories: Mapping[str, Sequence[str]],
) -> str:
    """The unit of one node: ``<slug>:<directory>``, ``<slug>:.``, or its own key."""
    if repository_slug is None or path is None:
        return key
    enclosing = [
        directory
        for directory in directories.get(repository_slug, ())
        if directory == "" or path.startswith(directory + "/")
    ]
    nearest = max(enclosing, key=len, default="")
    return f"{repository_slug}:{nearest or '.'}"
