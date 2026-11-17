"""Repository inclusions: files that say a repository includes another one at a path.

A repository can include another in more than one way, and each way is written in a file
the including repository commits. Every format here becomes the same claim, an
``INCLUDES_REPOSITORY`` reference whose target key is the path the included repository is
checked out at and whose ``meta`` says what else the file states:

* ``format`` -- which way: ``gitmodules``, ``git-subrepo``, ``vcstool``, ``west`` or
  ``repo-manifest`` (:data:`INCLUSION_FORMATS`);
* ``url`` -- the repository URL as written, credentials removed (relative URLs kept
  relative: the linker resolves them against the including repository's remote);
* ``revision`` -- the commit, tag or branch the file pins, when it pins one;
* ``branch`` -- the branch a ``.gitmodules`` entry follows, when it names one;
* ``name`` -- the name the file gives the entry;
* ``base`` -- what ``path`` is relative to: ``repository`` (the including repository's
  root), ``workspace`` (the top of a multi-repository workspace a manifest describes,
  which the linker infers) or ``none`` (a directory nothing in the file determines).

The formats, and where each one is read:

* **Git submodules** -- ``.gitmodules``: ``[submodule "name"]`` with ``path`` and ``url``.
  The commit pinned is not in the file but in the index, recorded at sync
  (:meth:`repoatlas.gitstate.client.GitClient.gitlinks`).
* **git-subrepo** -- ``<dir>/.gitrepo``: ``[subrepo]`` with ``remote`` and ``commit``;
  the included repository is the directory holding the file.
* **vcstool** -- ``*.repos``: ``repositories:`` mapping a path to ``type``, ``url`` and
  ``version``. Only ``git`` entries are read.
* **Zephyr west** -- a YAML manifest whose ``manifest.projects`` lists projects
  (:func:`west_references`, read by the structured parser for every YAML file).
* **Google repo** -- an XML ``<manifest>`` listing ``<project>`` elements with the
  ``<remote>`` their ``fetch`` URL comes from and the ``<default>`` they inherit.

Not read: ``git subtree``, which leaves no file -- only commit-message trailers -- and
gclient ``DEPS``, a Python file whose URLs are built by evaluating it.

Like every parser here, a pure function of the path and the content.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from xml.parsers import expat

import yaml

from repoatlas.gitstate.config import parse_git_config, strip_credentials
from repoatlas.models.parsing import (
    INVALID_YAML,
    PARSE_BOUND_EXCEEDED,
    PARSE_FAILED,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    Severity,
)

GITMODULES = "gitmodules"
GIT_SUBREPO = "git-subrepo"
VCSTOOL = "vcstool"
WEST = "west"
REPO_MANIFEST = "repo-manifest"
INCLUSION_FORMATS = (GITMODULES, GIT_SUBREPO, VCSTOOL, WEST, REPO_MANIFEST)

#: What a claim's path is relative to (``meta["base"]``).
BASE_REPOSITORY = "repository"
BASE_WORKSPACE = "workspace"
BASE_NONE = "none"

#: The most inclusions one file states before the rest are left unread.
MAX_INCLUSIONS = 2_000
#: The most XML elements one manifest is read up to.
MAX_ELEMENTS = 50_000
#: The most distinct YAML nodes read to find where one entry ends.
MAX_YAML_NODE_VISITS = 10_000
#: The most entries of one list a file is read for, claims or not.
MAX_ENTRIES_READ = 50_000
#: The longest value a file field may hold: a path, URL, name, revision, branch or ``fetch``.
#: An entry that reads a longer one makes no claim, so a single long string shared by thousands
#: of entries is not copied into every claim. It is not read as absent either: an absent field
#: has defaults (a west project's path is its name), so dropping it would let the file state a
#: claim it never made.
MAX_FIELD_CHARS = 4_096
#: The longest URL a claim may carry: room for one built from two capped fields (a remote's base
#: and a project's path, joined).
MAX_URL_CHARS = 2 * MAX_FIELD_CHARS + 8

#: The fields each format reads from one entry. An entry with any of them over-long is skipped
#: whole (see ``_scalars`` for how a long field is recorded). A west remote and the two
#: defaults are not listed: they are not entries of their own, but a project that would read
#: one left unread, directly or by inheritance, makes no claim (a remote it names, or a
#: default remote or revision it inherits).
_VCSTOOL_FIELDS = frozenset({"type", "url", "version"})
_WEST_PROJECT_FIELDS = frozenset({"name", "url", "remote", "repo-path", "path", "revision"})
_REPO_PROJECT_FIELDS = frozenset({"name", "path", "remote", "revision"})
_REPO_REMOTE_FIELDS = frozenset({"name", "fetch", "revision"})

_Scalars = tuple[dict[str, str], frozenset[str]]


def inclusion_reference(
    *,
    format: str,
    path: str,
    url: str | None,
    line_start: int,
    line_end: int,
    base: str,
    name: str | None = None,
    revision: str | None = None,
    branch: str | None = None,
) -> ParsedReference | None:
    """One inclusion claim, or ``None`` when ``path`` is not a relative path below its base or
    a field is longer than a field may be. A field is never dropped to keep the claim: a
    claim without its URL would be matched by path instead, a claim the file did not make."""
    if len(path) > MAX_FIELD_CHARS:
        return None
    if any(v is not None and len(v) > MAX_FIELD_CHARS for v in (name, revision, branch)):
        return None
    if url is not None and len(url) > MAX_URL_CHARS:
        return None
    clean = clean_path(path)
    if clean is None:
        return None
    meta = {"format": format, "base": base}
    # A URL that does not parse is dropped whole, as a missing one: the claim keeps its path.
    safe_url = strip_credentials(url) if url else None
    if safe_url:
        meta["url"] = safe_url
    if name:
        meta["name"] = name
    if revision:
        meta["revision"] = revision.strip()
    if branch:
        meta["branch"] = branch.strip()
    return ParsedReference(
        edge_type=EdgeType.INCLUDES_REPOSITORY,
        target_kind=EntityType.REPOSITORY,
        target_key=clean,
        line_start=line_start,
        line_end=max(line_start, line_end),
        meta=meta,
    )


def clean_path(path: str) -> str | None:
    """``path`` as a normalized relative POSIX path, or ``None`` if it leaves its base."""
    text = path.strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return None
    normalized = posixpath.normpath(text)
    if normalized in (".", "") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


# -- .gitmodules and .gitrepo ---------------------------------------------------------


def gitmodules_references(content: str) -> list[ParsedReference]:
    """Each ``[submodule "name"]`` with a ``path``, cited from its header to its last key."""
    # The keys are kept apart from the lines: a key named ``start`` or ``end`` is a key. A key
    # stated twice is read as git reads it, the last value winning.
    sections: dict[str, dict[str, Any]] = {}
    for entry in parse_git_config(content):
        if entry.section != "submodule" or entry.subsection is None:
            continue
        found = sections.setdefault(
            entry.subsection, {"start": entry.section_line, "end": entry.line, "keys": {}}
        )
        found["end"] = max(found["end"], entry.line)
        if entry.value is not None:
            found["keys"][entry.key] = entry.value
    references: list[ParsedReference] = []
    for name, found in sections.items():
        keys: dict[str, str] = found["keys"]
        reference = inclusion_reference(
            format=GITMODULES,
            path=keys.get("path", ""),
            url=keys.get("url"),
            line_start=found["start"],
            line_end=found["end"],
            base=BASE_REPOSITORY,
            name=name,
            branch=keys.get("branch"),
        )
        if reference is not None:
            references.append(reference)
    return references[:MAX_INCLUSIONS]


def gitrepo_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """The ``[subrepo]`` a ``.gitrepo`` states: the directory holding it is the inclusion."""
    keys: dict[str, str] = {}
    start: int | None = None
    end = 0
    for entry in parse_git_config(content):
        if entry.section != "subrepo":
            continue
        if start is None:
            start = entry.section_line
        end = max(end, entry.line)
        if entry.value is not None:
            keys[entry.key] = entry.value
    if start is None:
        return []
    reference = inclusion_reference(
        format=GIT_SUBREPO,
        path=str(path.parent),
        url=keys.get("remote"),
        line_start=start,
        line_end=end,
        base=BASE_REPOSITORY,
        revision=keys.get("commit"),
        branch=keys.get("branch"),
    )
    return [reference] if reference is not None else []


# -- vcstool ------------------------------------------------------------------------------


def vcstool_references(content: str) -> tuple[list[ParsedReference], list[ParseDiagnostic]]:
    """Each ``git`` entry of a ``.repos`` file's ``repositories`` mapping."""
    try:
        root = yaml.compose(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        return [], [ParseDiagnostic(severity=Severity.WARNING, message=INVALID_YAML, line=line)]
    except RecursionError:
        # A file nested past the interpreter's recursion limit is no readable YAML.
        return [], [ParseDiagnostic(severity=Severity.WARNING, message=INVALID_YAML)]
    repositories = _mapping_value(root, "repositories")
    if not isinstance(repositories, yaml.MappingNode):
        return [], []
    references: list[ParsedReference] = []
    seen: set[int] = set()
    cache: dict[int, _Scalars] = {}
    examined = 0
    for key, value in repositories.value:
        examined += 1
        if examined > MAX_ENTRIES_READ:
            break
        if not isinstance(key, yaml.ScalarNode) or not isinstance(value, yaml.MappingNode):
            continue
        if len(key.value) > MAX_FIELD_CHARS:
            continue
        fields, over_long = _scalars(value, cache)
        # An over-long ``type`` is no ``git`` by default, nor is an over-long ``url`` none.
        if not over_long.isdisjoint(_VCSTOOL_FIELDS):
            continue
        if fields.get("type", "git") != "git":
            continue
        reference = inclusion_reference(
            format=VCSTOOL,
            path=key.value,
            url=fields.get("url"),
            line_start=key.start_mark.line + 1,
            line_end=_end_line(value, seen),
            base=BASE_NONE,
            revision=fields.get("version"),
        )
        if reference is not None:
            references.append(reference)
        if len(references) >= MAX_INCLUSIONS:
            break
    return references, []


# -- west ---------------------------------------------------------------------------------


def west_references(documents: Sequence[yaml.Node | None]) -> list[ParsedReference]:
    """The projects of a west manifest: any YAML document whose ``manifest`` maps
    ``projects`` to a list of mappings with a ``name``. A project's URL is its ``url``, or
    its remote's ``url-base`` and its ``repo-path`` (else its name); its path is its
    ``path``, else its name; its revision its own, else the defaults'."""
    references: list[ParsedReference] = []
    seen: set[int] = set()
    cache: dict[int, _Scalars] = {}
    # Per file, over every document: a long ``remotes`` list must not hide the ``projects``.
    remotes_read = projects_read = 0
    for document in documents:
        manifest = _mapping_value(document, "manifest")
        projects = _mapping_value(manifest, "projects")
        if not isinstance(projects, yaml.SequenceNode):
            continue
        # A project that would read a value left unread makes no claim: a default over-long
        # that it inherits, a remote it names whose ``url-base`` is over-long, or a remote
        # past the cap of the list. A remote whose own ``name`` is over-long is no remote
        # anyone names, so a project naming a name never defined keeps its claim, URL-less.
        defaults, defaults_over_long = _scalars(_mapping_value(manifest, "defaults"), cache)
        remotes: dict[str, str] = {}
        unread_remotes: set[str] = set()
        # The cap is per file: a later document's remotes are unread once it has been reached.
        remotes_truncated = False
        listed = _mapping_value(manifest, "remotes")
        if isinstance(listed, yaml.SequenceNode):
            for item in listed.value:
                remotes_read += 1
                if remotes_read > MAX_ENTRIES_READ:
                    remotes_truncated = True
                    break
                fields, remote_over_long = _scalars(item, cache)
                if "name" in fields and "url-base" in remote_over_long:
                    unread_remotes.add(fields["name"])
                if "name" in fields and "url-base" in fields:
                    remotes[fields["name"]] = fields["url-base"]
        for item in projects.value:
            projects_read += 1
            if projects_read > MAX_ENTRIES_READ:
                return references
            if not isinstance(item, yaml.MappingNode):
                continue
            fields, over_long = _scalars(item, cache)
            # An over-long ``path`` would put the claim at the name, an over-long ``repo-path``
            # or ``remote`` would build or lose a URL the file never stated: no claim.
            if not over_long.isdisjoint(_WEST_PROJECT_FIELDS):
                continue
            name = fields.get("name")
            if not name:
                continue
            url = fields.get("url")
            if url is None:
                remote_name = fields.get("remote")
                if not remote_name:
                    if "remote" in defaults_over_long:
                        continue
                    remote_name = defaults.get("remote") or ""
                if remote_name in unread_remotes:
                    continue
                if remotes_truncated and remote_name and remote_name not in remotes:
                    continue
                base = remotes.get(remote_name)
                if base is not None:
                    url = f"{base.rstrip('/')}/{fields.get('repo-path') or name}"
            revision = fields.get("revision")
            if not revision:
                if "revision" in defaults_over_long:
                    continue
                revision = defaults.get("revision")
            reference = inclusion_reference(
                format=WEST,
                path=fields.get("path") or name,
                url=url,
                line_start=item.start_mark.line + 1,
                line_end=_end_line(item, seen),
                base=BASE_WORKSPACE,
                name=name,
                revision=revision,
            )
            if reference is not None:
                references.append(reference)
            if len(references) >= MAX_INCLUSIONS:
                return references
    return references


# -- Google repo ----------------------------------------------------------------------------


def repo_manifest_references(content: str) -> tuple[list[ParsedReference], list[ParseDiagnostic]]:
    """The ``<project>`` elements of a repo ``<manifest>``. Anything else is no manifest
    and yields nothing. A document type is refused unread: no entity is ever expanded."""
    if "<manifest" not in content:
        return [], []
    if "<!DOCTYPE" in content or "<!ENTITY" in content:
        return [], [ParseDiagnostic(severity=Severity.WARNING, message=PARSE_FAILED)]
    elements: list[tuple[int, str, dict[str, str], int]] = []
    depth = 0
    parser = expat.ParserCreate()

    def start(tag: str, attributes: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if len(elements) >= MAX_ELEMENTS:
            raise _TooLarge
        elements.append((depth, tag, attributes, parser.CurrentLineNumber))

    def end(_tag: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(content, True)
    except _TooLarge:
        return [], [ParseDiagnostic(severity=Severity.WARNING, message=PARSE_BOUND_EXCEEDED)]
    except expat.ExpatError as exc:
        return [], [
            ParseDiagnostic(severity=Severity.WARNING, message=PARSE_FAILED, line=exc.lineno)
        ]
    if not elements or elements[0][1] != "manifest":
        return [], []
    # Each element's attributes within the bound, and the names of those past it.
    top = [
        (
            tag,
            {k: v for k, v in attributes.items() if len(v) <= MAX_FIELD_CHARS},
            frozenset(k for k, v in attributes.items() if len(v) > MAX_FIELD_CHARS),
            line,
        )
        for depth_, tag, attributes, line in elements
        if depth_ == 2
    ]
    # A remote with an over-long field is not read, and a project that would read it (by its
    # short name, own or the default's) makes no claim. A name defined twice is unread if either
    # definition is, so the skip is decided by name before the remote is looked up.
    remotes: dict[str, dict[str, str]] = {}
    unread_remotes: set[str] = set()
    for tag, attributes, over_long, _ in top:
        if tag != "remote" or not attributes.get("name"):
            continue
        if over_long.isdisjoint(_REPO_REMOTE_FIELDS):
            remotes[attributes["name"]] = attributes
        else:
            unread_remotes.add(attributes["name"])
    # A default over-long is not read either: a project inheriting it makes no claim.
    default, default_over_long = next(
        ((a, o) for tag, a, o, _ in top if tag == "default"), ({}, frozenset())
    )
    references: list[ParsedReference] = []
    for tag, attributes, over_long, line in top:
        if tag != "project" or not attributes.get("name"):
            continue
        if not over_long.isdisjoint(_REPO_PROJECT_FIELDS):
            continue
        name = attributes["name"]
        remote_name = attributes.get("remote")
        if not remote_name:
            if "remote" in default_over_long:
                continue
            remote_name = default.get("remote") or ""
        if remote_name in unread_remotes:
            continue
        remote = remotes.get(remote_name)
        revision = attributes.get("revision") or (remote or {}).get("revision")
        if not revision:
            if "revision" in default_over_long:
                continue
            revision = default.get("revision")
        url = None
        if remote is not None and remote.get("fetch"):
            url = _fetch_url(remote["fetch"], name)
        reference = inclusion_reference(
            format=REPO_MANIFEST,
            path=attributes.get("path") or name,
            url=url,
            line_start=line,
            line_end=line,
            base=BASE_WORKSPACE,
            name=name,
            revision=revision,
        )
        if reference is not None:
            references.append(reference)
        if len(references) >= MAX_INCLUSIONS:
            break
    return references, []


class _TooLarge(Exception):
    pass


def _fetch_url(fetch: str, name: str) -> str:
    """A project's URL from its remote's ``fetch``. repo joins a relative ``fetch`` to the
    manifest repository's URL as a URL is joined (the last segment dropped), so it is
    written here relative to that URL as a directory, the way the linker resolves one."""
    joined = f"{fetch.rstrip('/')}/{name}"
    if fetch.startswith("."):
        return f"../{joined}"
    return joined


# -- YAML helpers -----------------------------------------------------------------------


def _mapping_value(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _scalars(node: yaml.Node | None, cache: dict[int, _Scalars]) -> _Scalars:
    """The scalar fields of a mapping and the keys of those too long to keep, read once per
    node: ``cache`` is one file's, keyed by node identity, because an alias makes thousands
    of entries one node and each read of it is a pass over all its pairs."""
    if not isinstance(node, yaml.MappingNode):
        return {}, frozenset()
    found = cache.get(id(node))
    if found is None:
        found = cache[id(node)] = _read_scalars(node)
    return found


def _read_scalars(node: yaml.MappingNode) -> _Scalars:
    """``(fields, over_long)``: a field longer than ``MAX_FIELD_CHARS`` is not among the
    fields, but its key is in ``over_long``, so a reader can tell it was stated from it never
    having been. A key stated twice is over-long if either value is."""
    fields: dict[str, str] = {}
    over_long: set[str] = set()
    for k, v in node.value:
        if not (isinstance(k, yaml.ScalarNode) and isinstance(v, yaml.ScalarNode)):
            continue
        if v.value == "":
            continue
        if len(v.value) > MAX_FIELD_CHARS:
            over_long.add(k.value)
        else:
            fields[k.value] = str(v.value)
    return fields, frozenset(over_long)


def _end_line(node: yaml.Node, seen: set[int]) -> int:
    """The last line ``node`` occupies: its last scalar's. A collection's own end mark is
    where the next sibling starts, often a line past it. ``seen`` is shared by every entry of
    one file."""
    last = node.start_mark.line + 1
    pending = [node]
    # An alias makes the node graph a graph, not a tree: a node that holds itself is a cycle
    # and nested aliases multiply a walk exponentially. Each node is read once, and a walk
    # that still passes the cap is left where it is. An anchor precedes its aliases, so a node
    # that starts before ``node`` is an alias to text outside the entry: not walked, which
    # keeps the work over all entries proportional to the file and the last line the entry's.
    # ``seen`` spans the file: a node an earlier entry already walked lies outside this entry,
    # because the entries' texts are disjoint, so an alias to it is not this entry's own text.
    visits = 0
    first = node.start_mark.index
    while pending and visits < MAX_YAML_NODE_VISITS:
        current = pending.pop()
        if current.start_mark.index < first or id(current) in seen:
            continue
        seen.add(id(current))
        visits += 1
        if isinstance(current, yaml.ScalarNode):
            last = max(last, current.end_mark.line + 1)
        elif isinstance(current, yaml.MappingNode):
            for k, v in current.value:
                pending.extend((k, v))
        elif isinstance(current, yaml.SequenceNode):
            pending.extend(current.value)
    return last


# -- the parser -------------------------------------------------------------------------------


class RepositoryInclusionParser:
    """``.gitmodules``, ``.gitrepo``, vcstool ``.repos`` and repo manifest XML.

    West manifests are YAML, and the structured parser reads them (:func:`west_references`)
    so a YAML file keeps its configuration keys.
    """

    name = "repository-inclusion"
    version = "1"
    #: Not a language a repository is written in, so none is recorded for it.
    language = ""

    def supports(self, path: Path) -> bool:
        return path.name in (".gitmodules", ".gitrepo") or path.suffix in (".repos", ".xml")

    def parse(self, path: Path, content: str) -> ParsedFile:
        posix = PurePosixPath(path.as_posix())
        diagnostics: list[ParseDiagnostic] = []
        if path.name == ".gitmodules":
            references = gitmodules_references(content)
        elif path.name == ".gitrepo":
            references = gitrepo_references(posix, content)
        elif path.suffix == ".repos":
            references, diagnostics = vcstool_references(content)
        else:
            references, diagnostics = repo_manifest_references(content)
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            references=tuple(references),
            diagnostics=tuple(diagnostics),
        )
