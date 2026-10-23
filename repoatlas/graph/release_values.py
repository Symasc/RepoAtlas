"""A Helm release's values as it renders them: the chart's ``values.yaml``, each ``-f`` file
in order, then each ``--set`` (DF2_PHASE_A.md §6).

The artifact join (:mod:`repoatlas.graph.deploy_artifacts`) renders a CI release's workload
and Service with them; deployment targets (:mod:`repoatlas.graph.deployment_targets`) resolve
its Ingress hosts with them. One definition, so both read a release's values alike. Pure: it
reads the claims the parsers keep and writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from repoatlas.graph.ci_facts import CiRelease, Place
from repoatlas.models.container_image import EXPR, TEXT, VAR, WITHHELD

#: The link-decision family the artifact join records a container's image under.
CONTAINER_ARTIFACT = "container_artifact"

#: The branch symbols whose literal domain gives a release's variants, per CI system.
BRANCH = {
    "circleci": frozenset({(VAR, "CIRCLE_BRANCH")}),
    "github": frozenset({(EXPR, "github.ref_name"), (VAR, "GITHUB_REF_NAME")}),
}


@dataclass(frozen=True, slots=True)
class _Value:
    #: ``lit``, ``sym`` (CI parts, a symbol among them), ``withheld`` (a credential leaf or
    #: ``--set``: where it is set is known, never what) or ``unknown``. Only a literal
    #: renders; anything else is read as unknown here.
    kind: str
    text: str = ""
    parts: tuple[tuple[str, str], ...] = ()
    place: Place | None = None


_UNKNOWN = _Value("unknown")


def _ancestors(key: str) -> list[str]:
    """``a``, ``a.b`` and ``a.b[0]`` for ``a.b[0].c``: every path above ``key``."""
    found: list[str] = []
    for index, char in enumerate(key):
        if char in ".[" and index:
            found.append(key[:index])
    return found


class _Effective:
    """The values a release renders with, layer by layer, lowest first."""

    def __init__(self) -> None:
        self.entries: dict[str, _Value] = {}
        #: Whether a layer that may set anything was applied: an absent key is then unknown.
        self.open = False
        #: Keys a later layer removed (a null): absent whatever was open before.
        self.absent: set[str] = set()
        self.reasons: list[str] = []

    def _drop(self, key: str) -> None:
        for above in _ancestors(key):
            self.entries.pop(above, None)
        for under in [k for k in self.entries if k.startswith((f"{key}.", f"{key}["))]:
            del self.entries[under]

    def put(self, key: str, value: _Value) -> None:
        self._drop(key)
        self.entries[key] = value
        self.absent.discard(key)

    def unknown(self, reason: str) -> None:
        """A layer that may set any key to anything: everything known so far is unknown."""
        self.entries = dict.fromkeys(self.entries, _UNKNOWN)
        self.open = True
        self.absent.clear()
        if reason not in self.reasons:
            self.reasons.append(reason)

    def file(self, meta: Mapping[str, str], repo_id: int, path: str) -> None:
        leaves: dict[str, str] = json.loads(meta.get("values", "{}"))
        lines: dict[str, int] = json.loads(meta.get("lines", "{}"))
        cleared: list[list[str]] = json.loads(meta.get("cleared", "[]"))
        if meta.get("truncated") == "true":
            # What the cut part sets is not known, and may be anything.
            self.unknown("truncated")
        # Helm replaces a list whole; it merges maps.
        lists = {key.split("[", 1)[0] for key in leaves if "[" in key}
        lists |= {key for key, kind in cleared if kind == "list"}
        for root in lists:
            for key in [k for k in self.entries if k.startswith(f"{root}[")]:
                del self.entries[key]
        for key, kind in cleared:
            if kind == "null":
                self._drop(key)
                self.entries.pop(key, None)
                self.absent.add(key)
        for key, text in leaves.items():
            place = Place(repo_id, path, int(lines[key])) if key in lines else None
            self.put(
                key,
                _Value("withheld", place=place)
                if text == "<withheld>"
                else _Value("lit", text, place=place),
            )

    def setting(self, key: str | None, parts: Sequence[tuple[str, str]], place: Place) -> None:
        if key is None:
            self.unknown("set-unread")
            return
        if any(kind == WITHHELD for kind, _ in parts):
            self.put(key, _Value("withheld", place=place))
        elif all(kind == TEXT for kind, _ in parts):
            text = "".join(value for _, value in parts)
            if text in ("null", "Null", "NULL", "~"):
                self._drop(key)
                self.entries.pop(key, None)
                self.absent.add(key)
            else:
                self.put(key, _Value("lit", text, place=place))
        else:
            self.put(key, _Value("sym", parts=tuple(parts), place=place))

    def get(self, key: str) -> _Value | None:
        """The value at ``key``; ``None`` when absent (Helm's nil); unknown when a layer
        that may set anything left it open."""
        found = self.entries.get(key)
        if found is not None:
            return found
        if self.open and key not in self.absent:
            return _UNKNOWN
        return None

    def nested(self, key: str) -> bool:
        return any(k.startswith((f"{key}.", f"{key}[")) for k in self.entries)

    def literals(self, *keys: str) -> dict[str, str] | None:
        """Literal values at ``keys`` (absent ones left out), or ``None`` if one is not."""
        found: dict[str, str] = {}
        for key in keys:
            value = self.get(key)
            if value is None:
                continue
            if value.kind != "lit":
                return None
            found[key] = value.text
        return found


#: The values a release renders with (:func:`release_values`), and one value of them.
EffectiveValues = _Effective
ReleaseValue = _Value
#: What a layer that may set anything leaves at every key.
UNKNOWN_VALUE = _UNKNOWN


def release_values(
    chart_repo: int,
    chart_values: tuple[Mapping[str, str], str] | None,
    values_files: Mapping[str, Mapping[str, str]],
    release: CiRelease,
    substitute: Mapping[tuple[str, str], str],
) -> _Effective:
    """The values ``release`` renders its chart with, as Helm layers them: the chart's
    ``values.yaml``, each ``-f`` file in order, then each ``--set``. ``values_files`` are
    the release repository's values files by path; ``substitute`` gives CI symbols a
    literal value (a branch variant). A layer that is not read makes what it may set
    unknown, never absent. Shared by the HTTP join and deployment targets, so both render
    a release's values the same way."""
    values = _Effective()
    if chart_values is not None:
        values.file(chart_values[0], chart_repo, chart_values[1])
    for path, _how in release.values:
        if path is None:
            values.unknown("values-unresolved")
            continue
        meta = values_files.get(path)
        if chart_values is not None and path == chart_values[1]:
            meta = chart_values[0]
        if meta is None:
            values.unknown("values-unread")
            continue
        values.file(meta, release.origin.repo_id, path)
    if release.set_unread:
        values.unknown("set-unread")
    place = Place(release.origin.repo_id, release.origin.rel_path, release.claim.line_start or 1)
    for key, parts in release.sets:
        values.setting(key, substituted(parts, substitute), place)
    return values


def branch_symbols(system: str) -> frozenset[tuple[str, str]]:
    """The CI parts that hold the branch a pipeline of ``system`` runs for."""
    return BRANCH.get(system, frozenset())


def substituted(
    parts: Iterable[tuple[str, str]], substitute: Mapping[tuple[str, str], str]
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for kind, text in parts:
        literal = substitute.get((kind, text))
        found.append((TEXT, literal) if literal is not None else (kind, text))
    return found


__all__ = [
    "BRANCH",
    "CONTAINER_ARTIFACT",
    "UNKNOWN_VALUE",
    "EffectiveValues",
    "ReleaseValue",
    "branch_symbols",
    "release_values",
    "substituted",
]
