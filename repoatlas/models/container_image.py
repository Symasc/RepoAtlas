"""Container image references, by the distribution reference grammar, never by splitting text.

A reference is ``[host[:port]/]path[:tag][@digest]``, and the colon is the trap: in
``registry.example:5000/sample/api:1.0`` the first colon separates a port and the last a
tag. So:

* the **digest** is everything after ``@``;
* the **tag** is after the last ``:`` that follows the last ``/``;
* the **registry** is the first ``/``-separated component, and only when that component
  contains ``.`` or ``:`` or is ``localhost`` -- otherwise the whole name is a path on the
  implied registry, ``docker.io`` (with ``library/`` before a one-component path), and the
  reference is flagged ``registry_implicit``;
* a registry's **port** is its own field.

A reference is built from *parts* (:data:`TEXT` and unresolved kinds), because CI writes
``registry.example:5000/sample/api:$CIRCLE_SHA1``: a reference whose tag is unknown is
still the image ``registry.example:5000/sample/api``. Its **join key** -- host, port and
path -- exists only when all three are literal; nothing is ever dropped to make one, so
``registry-two.example/sample/api`` and ``registry.example/sample/api`` are different
images (DF1_PHASE_A.md §5).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: Part kinds: literal text, and the unresolved kinds a reference may hold.
TEXT = "text"
VAR = "var"
EXPR = "expr"
WITHHELD = "withheld"

RESOLVED = "resolved"
REGISTRY_UNRESOLVED = "registry-unresolved"
IMAGE_UNRESOLVED = "image-unresolved"

#: Stands for an unresolved part while the grammar is applied: one character no
#: reference may contain, so the grammar can tell which fields hold one.
_HOLE = "\x00"
_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
)
_TAG = re.compile(r"[\w][\w.-]{0,127}")
_DIGEST = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*:[0-9a-fA-F]{32,}")
_IMPLICIT_HOST = "docker.io"


@dataclass(frozen=True, slots=True)
class ImageReference:
    """One reference, its fields as written (an unresolved field is ``None``)."""

    state: str
    host: str | None = None
    port: int | None = None
    path: str | None = None
    tag: str | None = None
    digest: str | None = None
    #: The registry is Docker Hub by the grammar's default, not by the text.
    registry_implicit: bool = False
    #: The tag or digest holds an unresolved part (a CI variable, for example).
    version_unresolved: bool = False

    def join_key(self) -> str | None:
        """``host[:port]/path`` when all three are literal; else ``None``."""
        if self.state != RESOLVED or self.host is None or self.path is None:
            return None
        return (
            f"{self.host}:{self.port}/{self.path}"
            if self.port is not None
            else f"{self.host}/{self.path}"
        )


def parse_reference(parts: Sequence[Sequence[str]]) -> ImageReference:
    """The reference these ``[kind, value]`` parts spell, by the grammar."""
    text = "".join(value if kind == TEXT else _HOLE for kind, value in parts)
    if not text.strip(_HOLE) and _HOLE in text:
        return ImageReference(state=IMAGE_UNRESOLVED)
    name, digest = text, None
    if "@" in text:
        name, digest = text.split("@", 1)
    tag = None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        name, tag = name[:colon], name[colon + 1 :]
    first, _, rest = name.partition("/")
    has_registry = bool(rest) and (
        "." in first or ":" in first or first == "localhost" or _HOLE in first
    )
    host_text, path = (first, rest) if has_registry else (None, name)
    version_unresolved = _HOLE in (tag or "") or _HOLE in (digest or "")
    if not path or _HOLE in path:
        return ImageReference(state=IMAGE_UNRESOLVED, version_unresolved=version_unresolved)
    if not all(_COMPONENT.fullmatch(component) for component in path.split("/")):
        return ImageReference(state=IMAGE_UNRESOLVED)
    if tag is not None and _HOLE not in tag and not _TAG.fullmatch(tag):
        return ImageReference(state=IMAGE_UNRESOLVED)
    if digest is not None and _HOLE not in digest and not _DIGEST.fullmatch(digest):
        return ImageReference(state=IMAGE_UNRESOLVED)
    clean_tag = tag if tag is not None and _HOLE not in tag else None
    clean_digest = digest if digest is not None and _HOLE not in digest else None
    if host_text is None:
        implicit = path if "/" in path else f"library/{path}"
        return ImageReference(
            state=RESOLVED,
            host=_IMPLICIT_HOST,
            path=implicit,
            tag=clean_tag,
            digest=clean_digest,
            registry_implicit=True,
            version_unresolved=version_unresolved,
        )
    if _HOLE in host_text:
        return ImageReference(
            state=REGISTRY_UNRESOLVED,
            path=path,
            tag=clean_tag,
            digest=clean_digest,
            version_unresolved=version_unresolved,
        )
    host, _, port_text = host_text.partition(":")
    port: int | None = None
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            return ImageReference(state=IMAGE_UNRESOLVED)
        port = int(port_text)
    if host != "localhost" and not _HOST.fullmatch(host):
        return ImageReference(state=IMAGE_UNRESOLVED)
    return ImageReference(
        state=RESOLVED,
        host=host.lower(),
        port=port,
        path=path,
        tag=clean_tag,
        digest=clean_digest,
        version_unresolved=version_unresolved,
    )


def text_of(parts: Sequence[Sequence[str]]) -> str:
    """The reference as it reads, each unresolved part shown by its kind (never a value
    that was withheld)."""
    shown = []
    for kind, value in parts:
        if kind == TEXT:
            shown.append(value)
        elif kind == WITHHELD:
            shown.append("<withheld>")
        else:
            shown.append(f"<{kind}:{value}>")
    return "".join(shown)


def version_parts(
    parts: Sequence[Sequence[str]],
) -> tuple[list[list[str]] | None, list[list[str]] | None]:
    """The tag's and the digest's own parts, as written (DF2's version relation): split
    exactly where :func:`parse_reference` splits, each unresolved part kept whole; ``None``
    for one not written."""
    cells: list[tuple[str, list[str] | None]] = []
    for kind, value in parts:
        if kind == TEXT:
            cells.extend((char, None) for char in value)
        else:
            cells.append((_HOLE, [kind, value]))
    text = "".join(char for char, _ in cells)
    at = text.find("@")
    name_end = at if at >= 0 else len(text)
    name = text[:name_end]
    slash, colon = name.rfind("/"), name.rfind(":")

    def grouped(start: int, end: int) -> list[list[str]]:
        found: list[list[str]] = []
        for char, part in cells[start:end]:
            if part is not None:
                found.append(list(part))
            elif found and found[-1][0] == TEXT:
                found[-1][1] += char
            else:
                found.append([TEXT, char])
        return found

    tag = grouped(colon + 1, name_end) if colon > slash else None
    digest = grouped(at + 1, len(text)) if at >= 0 else None
    return tag, digest
