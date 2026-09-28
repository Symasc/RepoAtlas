"""What a package declaration names: one reading, shared by every resolver that asks.

A manifest declares a package with a constraint, and the constraint says where the
package comes from as much as which versions will do. ``^1.2.0`` names a version of the
package the key names; ``npm:@acme/other@1`` names another package; ``file:../shared``
names a directory. Whether an import of the key reaches a given provider depends on
which of these it is, so every resolver that decides it -- a workspace member inside a
repository (:mod:`repoatlas.graph.modules`), a package another repository provides
(:mod:`repoatlas.graph.packages`), a package-level dependent of a change
(:mod:`repoatlas.capabilities.impact`) -- reads the constraint here, and only here.

The kinds (:class:`TargetKind`):

* ``version``: a range, a tag, ``catalog:``, or an ``npm:`` alias spelling the key's own
  name. It names the key's package, wherever that is provided;
* ``workspace``: ``workspace:*``, ``^``, ``~``, a range, or a ``workspace:`` alias of the
  key's own name. It names the member of that name in the declaring package's workspace;
* ``alias``: ``npm:`` or ``workspace:`` naming another package;
* ``path``: ``file:``, ``link:``, ``portal:``, ``workspace:`` with a relative path, or a
  bare relative path. It names the directory the path leads to from the manifest's own;
* ``elsewhere``: a source no indexed package can be shown to be -- ``git+...``,
  ``http(s):``, ``github:``, ``jsr:``, any other protocol, ``owner/repo``, and an
  absolute or ``~`` path, which depends on the machine that installs it;
* ``unreadable``: a ``workspace:`` form that is none of the above.

A path is followed lexically, as the package managers themselves resolve it
(:func:`path_target`): nothing on disk is read, so the answer is a function of indexed
facts alone.
"""

from __future__ import annotations

import enum
import posixpath
from dataclasses import dataclass

from repoatlas.dependencies.normalize import NPM, normalize

#: Declarations that name a directory, read from the declaring manifest's directory.
PATH_PROTOCOLS = frozenset({"file", "link", "portal"})
#: A version, never a location: a ``catalog:`` entry is a range kept in the workspace file.
VERSION_PROTOCOLS = frozenset({"catalog"})


class TargetKind(enum.StrEnum):
    VERSION = "version"
    WORKSPACE = "workspace"
    ALIAS = "alias"
    PATH = "path"
    ELSEWHERE = "elsewhere"
    UNREADABLE = "unreadable"


#: The refusal each kind is, whatever provider is asked about; a ``version`` or a
#: ``workspace`` range contradicts none, and a ``path`` depends on where it leads.
REFUSED_AS: dict[TargetKind, str] = {
    TargetKind.ALIAS: "dependency-aliased",
    TargetKind.ELSEWHERE: "dependency-elsewhere",
    TargetKind.UNREADABLE: "dependency-unreadable",
}


@dataclass(frozen=True, slots=True)
class DeclaredTarget:
    """What one declaration names (see the module docstring)."""

    kind: TargetKind
    #: The protocol as written, ``""`` for none.
    protocol: str = ""
    #: For a ``path``: the relative path as written, from the declaring manifest's directory.
    path: str | None = None


def classify_dependency_target(constraint: str, wanted: str) -> DeclaredTarget:
    """What a declaration of the package normalized as ``wanted`` names."""
    constraint = constraint.strip()
    protocol, colon, rest = constraint.partition(":")
    if not colon:
        if constraint.startswith("."):
            return DeclaredTarget(TargetKind.PATH, path=constraint)
        if constraint.startswith(("/", "~/")):
            return DeclaredTarget(TargetKind.ELSEWHERE)
        # `owner/repo` is a remote source; a range or a tag never holds a slash
        kind = TargetKind.ELSEWHERE if "/" in constraint else TargetKind.VERSION
        return DeclaredTarget(kind)
    if protocol in VERSION_PROTOCOLS:
        return DeclaredTarget(TargetKind.VERSION, protocol)
    if protocol == "npm":
        same = alias_name(rest) == wanted
        return DeclaredTarget(TargetKind.VERSION if same else TargetKind.ALIAS, protocol)
    if protocol in PATH_PROTOCOLS:
        if rest.startswith(("/", "~")):
            return DeclaredTarget(TargetKind.ELSEWHERE, protocol)
        return DeclaredTarget(TargetKind.PATH, protocol, rest)
    if protocol != "workspace":
        return DeclaredTarget(TargetKind.ELSEWHERE, protocol)
    if rest.startswith("."):
        return DeclaredTarget(TargetKind.PATH, protocol, rest)
    if rest.startswith("/"):
        return DeclaredTarget(TargetKind.ELSEWHERE, protocol)
    if "@" in rest[1:]:
        same = alias_name(rest) == wanted
        return DeclaredTarget(TargetKind.WORKSPACE if same else TargetKind.ALIAS, protocol)
    if "/" in rest or ":" in rest:
        return DeclaredTarget(TargetKind.UNREADABLE, protocol)
    return DeclaredTarget(TargetKind.WORKSPACE, protocol)


def alias_name(value: str) -> str:
    """The package ``pkg@range``, ``@scope/pkg@range`` or ``pkg`` names, normalized."""
    scoped = value.startswith("@")
    name = value[1:].partition("@")[0] if scoped else value.partition("@")[0]
    return normalize(NPM, f"@{name}" if scoped else name)


def path_target(root: str, manifest_dir: str, path: str) -> str:
    """The absolute directory a ``path`` declaration leads to, followed lexically.

    ``root`` is the declaring repository's root, ``manifest_dir`` the manifest's
    directory relative to it. ``..`` climbs out of the repository as freely as it does
    on disk; that the result lies in some indexed repository is the caller's to prove.
    """
    return posixpath.normpath(posixpath.join(root, manifest_dir, path.rstrip("/")))
