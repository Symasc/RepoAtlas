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
    """One inclusion claim, or ``None`` when ``path`` is not a relative path below its base."""
    clean = clean_path(path)
    if clean is None:
        return None
    meta = {"format": format, "base": base}
    if url:
        meta["url"] = strip_credentials(url)
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
    sections: dict[str, dict[str, Any]] = {}
    for entry in parse_git_config(content):
        if entry.section != "submodule" or entry.subsection is None:
            continue
        found = sections.setdefault(
            entry.subsection, {"start": entry.section_line, "end": entry.line}
        )
        found["end"] = max(found["end"], entry.line)
        if entry.value is not None:
            found.setdefault(entry.key, entry.value)
    references: list[ParsedReference] = []
    for name, found in sections.items():
        reference = inclusion_reference(
            format=GITMODULES,
            path=found.get("path", ""),
            url=found.get("url"),
            line_start=found["start"],
            line_end=found["end"],
            base=BASE_REPOSITORY,
            name=name,
            branch=found.get("branch"),
        )
        if reference is not None:
            references.append(reference)
    return references[:MAX_INCLUSIONS]


def gitrepo_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """The ``[subrepo]`` a ``.gitrepo`` states: the directory holding it is the inclusion."""
    found: dict[str, Any] = {}
    for entry in parse_git_config(content):
        if entry.section != "subrepo":
            continue
        found.setdefault("start", entry.section_line)
        found["end"] = max(found.get("end", entry.line), entry.line)
        if entry.value is not None:
            found.setdefault(entry.key, entry.value)
    if "start" not in found:
        return []
    reference = inclusion_reference(
        format=GIT_SUBREPO,
        path=str(path.parent),
        url=found.get("remote"),
        line_start=found["start"],
        line_end=found["end"],
        base=BASE_REPOSITORY,
        revision=found.get("commit"),
        branch=found.get("branch"),
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
    repositories = _mapping_value(root, "repositories")
    if not isinstance(repositories, yaml.MappingNode):
        return [], []
    references: list[ParsedReference] = []
    for key, value in repositories.value:
        if not isinstance(key, yaml.ScalarNode) or not isinstance(value, yaml.MappingNode):
            continue
        fields = _scalars(value)
        if fields.get("type", "git") != "git":
            continue
        reference = inclusion_reference(
            format=VCSTOOL,
            path=key.value,
            url=fields.get("url"),
            line_start=key.start_mark.line + 1,
            line_end=_end_line(value),
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
    for document in documents:
        manifest = _mapping_value(document, "manifest")
        projects = _mapping_value(manifest, "projects")
        if not isinstance(projects, yaml.SequenceNode):
            continue
        defaults = _scalars(_mapping_value(manifest, "defaults"))
        remotes: dict[str, str] = {}
        listed = _mapping_value(manifest, "remotes")
        if isinstance(listed, yaml.SequenceNode):
            for item in listed.value:
                fields = _scalars(item)
                if "name" in fields and "url-base" in fields:
                    remotes[fields["name"]] = fields["url-base"]
        for item in projects.value:
            if not isinstance(item, yaml.MappingNode):
                continue
            fields = _scalars(item)
            name = fields.get("name")
            if not name:
                continue
            url = fields.get("url")
            if url is None:
                base = remotes.get(fields.get("remote") or defaults.get("remote") or "")
                if base is not None:
                    url = f"{base.rstrip('/')}/{fields.get('repo-path') or name}"
            reference = inclusion_reference(
                format=WEST,
                path=fields.get("path") or name,
                url=url,
                line_start=item.start_mark.line + 1,
                line_end=_end_line(item),
                base=BASE_WORKSPACE,
                name=name,
                revision=fields.get("revision") or defaults.get("revision"),
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
    top = [(tag, attributes, line) for depth_, tag, attributes, line in elements if depth_ == 2]
    remotes = {a["name"]: a for tag, a, _ in top if tag == "remote" and "name" in a}
    default = next((a for tag, a, _ in top if tag == "default"), {})
    references: list[ParsedReference] = []
    for tag, attributes, line in top:
        if tag != "project" or not attributes.get("name"):
            continue
        name = attributes["name"]
        remote = remotes.get(attributes.get("remote") or default.get("remote") or "")
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
            revision=(
                attributes.get("revision")
                or (remote or {}).get("revision")
                or default.get("revision")
            ),
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


def _scalars(node: yaml.Node | None) -> dict[str, str]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {
        k.value: str(v.value)
        for k, v in node.value
        if isinstance(k, yaml.ScalarNode) and isinstance(v, yaml.ScalarNode) and v.value != ""
    }


def _end_line(node: yaml.Node) -> int:
    """The last line ``node`` occupies: its last scalar's. A collection's own end mark is
    where the next sibling starts, often a line past it."""
    last = node.start_mark.line + 1
    pending = [node]
    while pending:
        current = pending.pop()
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
