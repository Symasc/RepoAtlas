"""Git configuration syntax and repository URLs, as pure functions.

Two things read the same syntax: a repository's own ``config`` (its remotes) and the
files a repository commits in that syntax to say what it includes -- ``.gitmodules`` and
git-subrepo's ``.gitrepo``. Nothing here touches the disk or runs git.

**Configuration** is read in the documented subset that states remotes and submodules:
``[section]`` and ``[section "subsection"]`` headers, ``key = value`` lines with quoted
values and escapes, ``#`` and ``;`` comments, and a trailing ``\\`` continuing a value on
the next line. ``include`` and ``includeIf`` are not followed: they name other files, and
only the file given is read.

**A URL's identity** (:func:`normalize_git_url`) is what two spellings of one repository
share: the host, case-folded, and the path, case-folded, without a port, user, password,
trailing slash or ``.git``. ``git@host:org/x.git``, ``ssh://git@host:22/org/x`` and
``https://user:token@host/org/x/`` are one identity, ``host/org/x``. A local path is
``file:`` and its absolute path, case preserved. A relative URL (``./x``, ``../x``) is
resolved as git resolves a submodule's: against the including repository's remote, or,
when it has none, against its directory.

**Credentials never leave** :func:`strip_credentials`: a URL is shown, stored or matched
only without its user information.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The most configuration lines one file is read up to.
MAX_CONFIG_LINES = 20_000

#: Schemes whose URL names a host and a path on it.
_NETWORK_SCHEMES = frozenset(
    {"http", "https", "ssh", "git", "git+ssh", "ssh+git", "ftp", "ftps", "rsync"}
)
_HEADER = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=(.*))?$")
#: ``[user@]host:path``, git's scp-like syntax: no scheme, and no slash before the colon.
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.*)$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    """One ``key = value`` of a configuration file, with the section it is in."""

    #: Lower-cased, as git compares it.
    section: str
    #: As written: a subsection is case-sensitive.
    subsection: str | None
    #: Lower-cased.
    key: str
    #: ``None`` for a key written without ``=`` (git reads it as ``true``).
    value: str | None
    #: 1-based line of the key.
    line: int
    #: 1-based line of the section header, so a whole section can be cited.
    section_line: int


def parse_git_config(text: str) -> list[ConfigEntry]:
    """Every entry of a configuration file, in file order. Never raises: a line that
    is not configuration is skipped, and reading stops at :data:`MAX_CONFIG_LINES`."""
    entries: list[ConfigEntry] = []
    section: str | None = None
    subsection: str | None = None
    section_line = 0
    lines = text.splitlines()[:MAX_CONFIG_LINES]
    index = 0
    while index < len(lines):
        number = index + 1
        raw = lines[index].strip()
        index += 1
        if not raw or raw[0] in "#;":
            continue
        if raw.startswith("["):
            header = _HEADER.match(raw)
            if header is None:
                section = None
                continue
            name = header.group(1).lower()
            quoted = header.group(2)
            if quoted is not None:
                section, subsection = name, re.sub(r"\\(.)", r"\1", quoted)
            elif "." in name:
                # The deprecated ``[section.subsection]`` spelling, lower-cased by git.
                section, _, subsection = name.partition(".")
            else:
                section, subsection = name, None
            section_line = number
            rest = raw[header.end() :].strip()
            if not rest or rest[0] in "#;":
                continue
            raw = rest
        if section is None:
            continue
        body = raw if "=" in raw else re.split(r"[#;]", raw, maxsplit=1)[0].strip()
        key = _KEY.match(body)
        if key is None:
            continue
        value: str | None = None
        if key.group(2) is not None:
            text_value = key.group(2)
            # A trailing backslash outside quotes continues the value on the next line.
            while _continues(text_value) and index < len(lines):
                text_value = text_value[:-1] + lines[index]
                index += 1
            value = _value(text_value)
        entries.append(
            ConfigEntry(
                section=section,
                subsection=subsection,
                key=key.group(1).lower(),
                value=value,
                line=number,
                section_line=section_line,
            )
        )
    return entries


def _continues(value: str) -> bool:
    trailing = len(value) - len(value.rstrip("\\"))
    return trailing % 2 == 1


def _value(raw: str) -> str:
    """A value as git reads it: quotes removed, escapes applied, a comment outside
    quotes dropped, and surrounding whitespace outside quotes trimmed."""
    out: list[str] = []
    quoted = False
    pending_space = ""
    escapes = {"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}
    index = 0
    raw = raw.strip()
    while index < len(raw):
        char = raw[index]
        index += 1
        if char == "\\" and index < len(raw):
            out.append(pending_space + escapes.get(raw[index], raw[index]))
            pending_space = ""
            index += 1
        elif char == '"':
            quoted = not quoted
        elif not quoted and char in "#;":
            break
        elif not quoted and char.isspace():
            pending_space += char
        else:
            if out or quoted:
                out.append(pending_space)
            pending_space = ""
            out.append(char)
    return "".join(out)


def strip_credentials(url: str) -> str:
    """``url`` without a user or password. Anything not a scheme URL is returned as it
    is but for an scp-like user, which is a login name and not a secret, kept."""
    url = url.strip()
    if _SCHEME.match(url):
        parts = urlsplit(url)
        if parts.username is None and parts.password is None:
            return url
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        try:
            port = f":{parts.port}" if parts.port is not None else ""
        except ValueError:
            port = ""
        return parts._replace(netloc=f"{host}{port}").geturl()
    return url


def is_relative_url(url: str) -> bool:
    """Whether git resolves ``url`` against the including repository: ``./x`` or ``../x``."""
    return url.startswith(("./", "../"))


def normalize_git_url(
    url: str, *, base: str | None = None, base_dir: str | None = None
) -> str | None:
    """The identity of the repository ``url`` names, or ``None`` when it names none.

    ``base`` is the including repository's own remote, already normalized, and
    ``base_dir`` its absolute directory: a relative URL is resolved against the first
    that is given, as git resolves it.
    """
    url = url.strip()
    if not url:
        return None
    if is_relative_url(url):
        if base is not None:
            return _relative_to(base, url)
        if base_dir is not None:
            return _local(posixpath.join(base_dir, url))
        return None
    if _SCHEME.match(url):
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "file":
            return _local(parts.path) if parts.path.startswith("/") else None
        if scheme not in _NETWORK_SCHEMES or not parts.hostname:
            return None
        return _network(parts.hostname, parts.path)
    if url.startswith("/"):
        return _local(url)
    scp = _SCP.match(url)
    if scp is not None and len(scp.group("host")) > 1:
        return _network(scp.group("host"), scp.group("path"))
    return None


def _network(host: str, path: str) -> str | None:
    segments = [s for s in path.replace("\\", "/").split("/") if s and s != "."]
    if not segments:
        return None
    last = segments[-1]
    if last.lower().endswith(".git"):
        last = last[:-4]
        if not last:
            segments.pop()
        else:
            segments[-1] = last
    if not segments:
        return None
    return f"{host.lower()}/{'/'.join(segments)}".casefold()


def _local(path: str) -> str | None:
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        return None
    if normalized.endswith(".git") and normalized != "/.git":
        normalized = normalized[:-4].rstrip("/") or "/"
    return f"file:{normalized}"


def _relative_to(base: str, url: str) -> str | None:
    """``url`` (``./x``, ``../x``) resolved against a normalized ``base``, as git does:
    the base is a directory, ``..`` leaves it and ``.`` stays in it."""
    if base.startswith("file:"):
        return _local(posixpath.join(base[len("file:") :], url))
    host, _, path = base.partition("/")
    segments = [s for s in path.split("/") if s]
    for part in url.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not segments:
                return None
            segments.pop()
        else:
            segments.append(part)
    return _network(host, "/".join(segments))
