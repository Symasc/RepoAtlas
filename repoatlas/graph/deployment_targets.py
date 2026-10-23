"""Deployment targets for one repository, built from its claims when asked.

Nothing here is stored: a target is computed from the claims a sync keeps
(:mod:`repoatlas.parsers.deploy_targets`, the Helm and CI readers), the HTTP edges an
OpenAPI document or a framework declares, and the links a user stated
(:mod:`repoatlas.storage.sqlite.deployment_links`). Each target is resolved under every
context that may supply its variables -- the chart's own defaults, each release that
installs the chart, each link that names the target -- and each resolution says where
every value came from. A hole stays a hole: a value no context supplies, a credential, or
an expression the reader did not read is named, never guessed. Nothing is contacted, so
nothing here says a deployment is live; only a user's link can say they confirmed it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from repoatlas.graph.ci_facts import CiRelease, plan_ci_facts
from repoatlas.graph.release_values import (
    EffectiveValues,
    branch_symbols,
    release_values,
)
from repoatlas.models.container_image import EXPR, TEXT, VAR
from repoatlas.models.deployment_conditions import ConditionContext, ConditionState, evaluate
from repoatlas.models.deployment_targets import (
    DeploymentGuard,
    DeploymentLink,
    DeploymentResolution,
    DeploymentTarget,
    DeploymentVariable,
    GuardState,
    ResolutionBasis,
)
from repoatlas.models.http import DEPLOYMENT_KEY_PREFIX
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.ci_config import CI_STEP_UNSUPPORTED
from repoatlas.parsers.http_facts import (
    DEPLOYMENT_TARGET_INGRESS,
    DEPLOYMENT_TARGET_VALUES,
    HELM_CHART,
    HELM_RELEASE,
    HELM_VALUES,
    HELM_VALUES_FILE,
    PENDING_FACT,
)
from repoatlas.parsers.withhold import WITHHELD as WITHHELD_VALUE
from repoatlas.storage.protocols import FileRow, HttpEdgeRow, UnresolvedRefRecord

#: Targets one repository may report, and contexts one target may be resolved under.
MAX_TARGETS = 200
MAX_RESOLUTIONS = 32
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9*]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
_STAR = re.compile(r"\[\*\]")
_FALSY = frozenset({"", "false", "False", "FALSE", "0", "null", "~"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

Part = list[str]


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    return json.loads(claim.meta_json or "{}")


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def target_id(repository: str, path: str, line: int, binding: str) -> str:
    """Stable while the stating file, its line and the range item stay put."""
    digest = hashlib.sha256(f"{repository}\0{path}\0{line}\0{binding}".encode()).hexdigest()
    return f"dt_{digest[:16]}"


@dataclass(slots=True)
class _Context:
    """One way a target's variables get values: what it overrides, and how it is shown."""

    basis: str  # "chart-defaults" | "release" | "link" | "declared"
    label: str
    #: Values path -> (value, basis, withheld). ``None`` as value: set, but not literal.
    overrides: dict[str, tuple[str | None, ResolutionBasis, bool]] = field(default_factory=dict)
    defaults: Mapping[str, str] = field(default_factory=dict)
    release: str | None = None
    environment: str | None = None
    conditions: ConditionState = "none"
    link: DeploymentLink | None = None
    notes: list[str] = field(default_factory=list)
    #: A CI release's values, layered as the HTTP join layers them (``release_values``):
    #: when set, every values path is read from it rather than from ``defaults``.
    effective: EffectiveValues | None = None
    #: Where the chart's ``values.yaml`` states each key: ``(path, {key: line})``.
    default_lines: tuple[str, Mapping[str, int]] | None = None
    #: The CI parts that hold the branch the release's pipeline runs for, and that branch
    #: as asked about: such a part takes the asked branch, with the basis ``context``.
    branch_symbols: frozenset[tuple[str, str]] = frozenset()
    branch: str | None = None


@dataclass(slots=True)
class _Value:
    value: str | None
    basis: ResolutionBasis | None
    withheld: bool = False
    at: str | None = None
    #: Why a value that is set has no literal value here.
    note: str | None = None


#: Why a layer of a release's values left everything after it unknown.
_OPEN_REASONS = {
    "values-unresolved": "a values file the release names is not a literal path",
    "values-unread": "a values file the release names is not indexed",
    "set-unread": "a --set the release passes is not read",
    "truncated": "a values file is too large to read whole",
}


def _effective_lookup(path: str, context: _Context) -> _Value:
    assert context.effective is not None
    found = context.effective.get(path)
    if found is None:
        return _Value(None, None)
    at = f"{found.place.path}:{found.place.line}" if found.place is not None else None
    defaults = context.default_lines[0] if context.default_lines is not None else None
    basis: ResolutionBasis = (
        "chart-defaults" if found.place is not None and found.place.path == defaults else "release"
    )
    if found.kind == "lit":
        return _Value(found.text, basis, at=at)
    if found.kind == "withheld":
        return _Value(None, basis, withheld=True, at=at)
    if found.kind == "sym":
        symbolic = [part for part in found.parts if part[0] != TEXT]
        if context.branch is not None and all(p in context.branch_symbols for p in symbolic):
            text = "".join(
                context.branch if (kind, value) in context.branch_symbols else value
                for kind, value in found.parts
            )
            return _Value(text, "context", at=at)
        names = [text for kind, text in found.parts if kind in (VAR, EXPR) and text]
        return _Value(
            None, basis, at=at, note=f"`{path}` is set from {', '.join(names) or 'an expression'}"
        )
    reasons = [_OPEN_REASONS.get(r, r) for r in context.effective.reasons]
    return _Value(None, None, note=f"`{path}` may be set by what is not read: {'; '.join(reasons)}")


def _lookup(path: str, context: _Context) -> _Value:
    if path in context.overrides:
        value, basis, withheld = context.overrides[path]
        return _Value(None if withheld else value, basis, withheld)
    if context.effective is not None:
        return _effective_lookup(path, context)
    if path in context.defaults:
        value = context.defaults[path]
        at = None
        if context.default_lines is not None and path in context.default_lines[1]:
            at = f"{context.default_lines[0]}:{context.default_lines[1][path]}"
        if value == WITHHELD_VALUE:
            return _Value(None, "chart-defaults", True, at=at)
        return _Value(value, "chart-defaults", at=at)
    return _Value(None, None)


def _truth(path: str, context: _Context) -> GuardState:
    """A ``.Values`` truth test, as Helm would answer it from the values at hand."""
    if "[*]" in path:
        return "unknown"
    found = _lookup(path, context)
    if found.withheld:
        return "unknown"
    if found.basis is not None:
        if found.value is None:
            return "unknown"
        return "unmet" if found.value in _FALSY else "met"
    prefix = (f"{path}.", f"{path}[")
    if context.effective is not None and context.effective.nested(path):
        return "met"
    if any(key.startswith(prefix) for key in (*context.overrides, *context.defaults)):
        return "met"
    if context.effective is not None and context.effective.open:
        return "unknown"
    # Absent from every values source this context reads: Helm renders it as nil, unless
    # a source this did not read sets it.
    return "unmet" if context.basis != "declared" else "unknown"


def _bindings(paths: Iterable[str], known: Iterable[str]) -> list[dict[str, int]]:
    """Each consistent choice of range items for the starred values paths, from the
    paths the values at hand hold: ``{"a.b[*]": 0}`` for ``a.b[0].c``."""
    known = list(known)
    found: list[dict[str, int]] = [{}]
    for path in paths:
        stars = list(_STAR.finditer(path))
        if not stars:
            continue
        prefixes = [path[: match.end()] for match in stars]
        pattern = re.compile(
            "".join(
                re.escape(piece) if piece != "[*]" else r"\[(\d+)\]"
                for piece in re.split(r"(\[\*\])", path)
            )
        )
        matches: list[dict[str, int]] = []
        for key in known:
            match = pattern.fullmatch(key)
            if match is not None:
                choice = dict(zip(prefixes, (int(i) for i in match.groups()), strict=True))
                if choice not in matches:
                    matches.append(choice)
        if not matches:
            continue
        joined: list[dict[str, int]] = []
        for current in found:
            for choice in matches:
                if all(current.get(k, v) == v for k, v in choice.items()):
                    merged = {**current, **choice}
                    if merged not in joined:
                        joined.append(merged)
        found = joined or found
    return sorted(found, key=lambda b: sorted(b.items()))[:MAX_TARGETS]


def _bind_all(path: str, binding: Mapping[str, int]) -> str:
    # Bindings are keyed by the starred prefix (``a[*].b[*]``), whatever came before is bound.
    result, starred, rest = "", "", path
    while True:
        match = _STAR.search(rest)
        if match is None:
            return result + rest
        piece = rest[: match.end()]
        starred += piece
        result += piece[:-3] + f"[{binding[starred]}]" if starred in binding else piece
        rest = rest[match.end() :]


def _name(part: Part, binding: Mapping[str, int]) -> str:
    kind, text = part
    if kind == "value":
        return _bind_all(text, binding)
    return kind if kind in {"chart", "release", "namespace"} else text


@dataclass(slots=True)
class _Rendered:
    text: str
    resolved: bool
    variables: list[DeploymentVariable]
    notes: list[str] = field(default_factory=list)


def _render(
    parts: Sequence[Part], binding: Mapping[str, int], context: _Context, chart: str | None
) -> _Rendered:
    pieces: list[str] = []
    variables: list[DeploymentVariable] = []
    notes: list[str] = []
    resolved = True
    for part in parts:
        kind, text = part
        if kind == "text":
            pieces.append(text)
            continue
        name = _name(part, binding)
        value = _Value(None, None)
        if kind == "value":
            value = _lookup(name, context)
            if value.basis is None and name != text:
                value = _lookup(text, context)  # a link may bind the starred name
        elif kind == "chart":
            value = _lookup("chart", context)
            if value.basis is None and chart is not None:
                value = _Value(chart, "chart")
        elif kind == "release":
            value = _lookup("release", context)
            if value.basis is None and context.release is not None:
                value = _Value(context.release, "release")
        elif kind == "namespace":
            value = _lookup("namespace", context)
        if value.note is not None:
            notes.append(value.note)
        if value.value is not None and not value.value.strip():
            # Helm renders it, but an empty part names no host or path: a hole, not a value.
            where = f" at {value.at}" if value.at else ""
            notes.append(f"`{name}` is empty{where}")
            value = _Value(None, None, at=value.at)
        source = kind if kind in {"value", "chart", "release", "namespace"} else "opaque"
        variables.append(
            DeploymentVariable(
                name=name,
                source=source,  # type: ignore[arg-type]
                value=value.value,
                basis=value.basis if value.value is not None or value.withheld else None,
                withheld=value.withheld,
                at=value.at if value.value is not None or value.withheld else None,
            )
        )
        if value.value is None:
            resolved = False
            pieces.append("{" + name + "}")
        else:
            pieces.append(value.value)
    return _Rendered("".join(pieces), resolved, variables, notes)


def _path_prefix(text: str) -> str | None:
    """The literal prefix a rendered Ingress path serves, as a URL path; ``None`` when it
    is not one (a regular expression, a relative path)."""
    if not text.startswith("/"):
        return None
    text = text.removesuffix("*")
    if any(char in text for char in "*()[]{}?$^|\\"):
        return None
    return text.rstrip("/") or "/"


@dataclass(slots=True)
class _Scheme:
    schemes: list[str]
    ports: dict[str, int]
    basis: str | None


def _scheme(
    meta: Mapping[str, str],
    listens: Mapping[str, object],
    host_parts: Sequence[Part],
    tls: Sequence[Sequence[Part]],
) -> _Scheme:
    listen = _loads(meta.get("listen"), None)
    basis = "listen-ports annotation"
    if not listen:
        source = _loads(meta.get("annotations_from"), None)
        listen = listens.get(source) if isinstance(source, str) else None
        basis = f"listen-ports annotation in values `{source}`"
    if isinstance(listen, list) and listen:
        schemes: list[str] = []
        ports: dict[str, int] = {}
        for scheme, port in listen:
            if scheme not in schemes:
                schemes.append(scheme)
                ports[scheme] = port
        schemes.sort(key=lambda s: s != "https")
        return _Scheme(schemes, ports, basis)
    if any(list(map(list, entry)) == [list(p) for p in host_parts] for entry in tls):
        return _Scheme(["https"], {"https": 443}, "tls")
    return _Scheme([], {}, None)


def _guard_view(guard: Mapping[str, object], context: _Context) -> tuple[str, GuardState]:
    negated = bool(guard.get("negated"))
    if "value" in guard:
        path = str(guard["value"])
        state: GuardState = _truth(path, context)
        text = f".Values.{path}"
    else:
        state, text = "unknown", str(guard.get("expression", ""))
    if negated:
        flipped: dict[GuardState, GuardState] = {
            "met": "unmet",
            "unmet": "met",
            "unknown": "unknown",
        }
        state = flipped[state]
        text = f"not ({text})"
    return text, state


def _combined(states: Iterable[GuardState]) -> GuardState:
    states = list(states)
    if "unmet" in states:
        return "unmet"
    return "unknown" if "unknown" in states else "met"


def _origin(scheme: str, host: str, port: int | None) -> str | None:
    host = host.lower()
    if not _HOST.fullmatch(host) or "*" in host:
        return None
    suffix = f":{port}" if port is not None and port != _DEFAULT_PORTS.get(scheme) else ""
    return f"{scheme}://{host}{suffix}"


def _link_overrides(
    link: DeploymentLink, context: ConditionContext
) -> dict[str, tuple[str | None, ResolutionBasis, bool]]:
    found: dict[str, tuple[str | None, ResolutionBasis, bool]] = {}
    for name, value in link.variables.items():
        if value in {"{branch}", "{environment}"}:
            taken = context.branch if value == "{branch}" else context.environment
            found[name] = (taken, "context", False)
        else:
            found[name] = (value, "configured", False)
    return found


def _set_overrides(
    meta: Mapping[str, str],
) -> tuple[dict[str, tuple[str | None, ResolutionBasis, bool]], list[str]] | None:
    """What a Tilt release sets, or ``None`` when its settings were not read."""
    found: dict[str, tuple[str | None, ResolutionBasis, bool]] = {}
    notes: list[str] = []
    sets = _loads(meta.get("set"), None)
    if sets is None and "set" in meta:
        return None
    for item in sets or []:
        path, _, value = str(item).partition("=")
        found[path] = (value, "release", value == WITHHELD_VALUE)
    if meta.get("values_files") == "true":
        notes.append("the release names values files, which are not read for a Tilt release")
    return found, notes


def _literal(parts: Sequence[Sequence[str]]) -> str | None:
    """CI parts as one literal string, or ``None`` when any part is not text."""
    if parts and all(kind == TEXT for kind, _ in parts):
        return "".join(text for _, text in parts)
    return None


def _branches_state(meta: Mapping[str, str], context: ConditionContext) -> ConditionState:
    return _domain_state(_loads(meta.get("domains"), {}), context)


def _domain_state(domains: object, context: ConditionContext) -> ConditionState:
    branches = domains.get("branches") if isinstance(domains, dict) else None
    if not branches:
        return "none"
    if context.branch is None:
        return "unknown"
    return "match" if any(fnmatch.fnmatchcase(context.branch, b) for b in branches) else "no_match"


class _Builder:
    def __init__(
        self,
        repository: str,
        claims: Sequence[UnresolvedRefRecord],
        files: Mapping[int, FileRow],
        links: Sequence[DeploymentLink],
        context: ConditionContext,
        *,
        declarations: Sequence[UnresolvedRefRecord] = (),
        only: Mapping[str, set[str]] | None = None,
    ) -> None:
        self.repository = repository
        self.files = files
        self.paths = {file_id: row.rel_path for file_id, row in files.items()}
        self.links = links
        self.context = context
        #: Chart directory -> the release names to resolve it under, and nothing else: a
        #: target another repository states, asked about from the code it runs.
        self.only = only
        grouped: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
        for claim in claims:
            grouped[_meta(claim).get(PENDING_FACT, "")].append(claim)
        self.grouped = grouped
        self.charts = {_meta(c).get("chart", ""): c.target_key for c in grouped[HELM_CHART]}
        self.values: dict[str, dict[str, str]] = {}
        #: Chart directory -> (its values.yaml path, the values meta, {key: line}).
        self.chart_values: dict[str, tuple[str, dict[str, str], dict[str, int]]] = {}
        for claim in grouped[HELM_VALUES]:
            meta = _meta(claim)
            leaves = _loads(meta.get("values"), {})
            chart_dir = meta.get("chart", "")
            self.values[chart_dir] = leaves if isinstance(leaves, dict) else {}
            lines = _loads(meta.get("lines"), {})
            self.chart_values[chart_dir] = (
                self.paths.get(claim.file_id, ""),
                meta,
                lines if isinstance(lines, dict) else {},
            )
        self.values_files: dict[str, dict[str, str]] = {
            self.paths.get(claim.file_id, ""): _meta(claim) for claim in grouped[HELM_VALUES_FILE]
        }
        self.listens: dict[str, dict[str, object]] = {}
        for claim in grouped[DEPLOYMENT_TARGET_VALUES]:
            meta = _meta(claim)
            found = _loads(meta.get("listen"), {})
            self.listens[meta.get("chart", "")] = found if isinstance(found, dict) else {}
        # The releases CI establishes, decided exactly as the HTTP join decides them, with
        # an operator's declarations when one supplies an action's semantics.
        ci_files = dict(files)
        ci = plan_ci_facts([*claims, *declarations], ci_files)
        self.ci_releases: list[CiRelease] = [
            r for r in ci.releases if r.decision in ("bound", "values-unresolved")
        ]
        self.diagnostics: list[str] = []
        #: Set by :func:`build_targets` for a target another repository states.
        self.runs: str | None = None

    def _default_lines(self, chart: str) -> tuple[str, Mapping[str, int]] | None:
        found = self.chart_values.get(chart)
        return (found[0], found[2]) if found is not None else None

    def _releases(self, chart: str) -> list[_Context]:
        wanted = self.only.get(chart) if self.only is not None else None
        found: list[_Context] = []
        for claim in self.grouped[HELM_RELEASE]:
            meta = _meta(claim)
            if meta.get("chart", "") != chart:
                continue
            if wanted is not None and claim.target_key not in wanted:
                continue
            path = self.paths.get(claim.file_id, "")
            where = f"{path}:{claim.line_start}" if claim.line_start else path
            read = _set_overrides(meta)
            context = _Context(
                basis="release",
                label=f"release `{claim.target_key}` ({where})",
                defaults=self.values.get(chart, {}),
                release=claim.target_key,
                conditions=_branches_state(meta, self.context),
                default_lines=self._default_lines(chart),
            )
            if read is None:
                context.notes.append("the release's settings are not literal")
            else:
                context.overrides, notes = read
                context.notes.extend(notes)
            found.append(context)
        for release in self.ci_releases:
            if release.chart != chart:
                continue
            name = _literal(release.release)
            if wanted is not None and name not in wanted:
                continue
            where = f"{release.origin.rel_path}:{release.claim.line_start}"
            symbols = branch_symbols(release.system)
            branch = self.context.branch
            chart_values = self.chart_values.get(chart)
            values = release_values(
                release.origin.repo_id,
                (chart_values[1], chart_values[0]) if chart_values is not None else None,
                self.values_files,
                release,
                {},
            )
            if name is None and branch is not None:
                name = _literal(
                    [
                        (TEXT, branch) if (kind, text) in symbols else (kind, text)
                        for kind, text in release.release
                    ]
                )
            context = _Context(
                basis="release",
                label=f"release `{name or release.claim.target_key}` ({where})",
                release=name,
                conditions=_domain_state(release.domains, self.context),
                effective=values,
                default_lines=self._default_lines(chart),
                branch_symbols=symbols,
                branch=branch,
            )
            uses_branch = any(
                (kind, text) in symbols
                for parts in (release.release, *(p for _, p in release.sets))
                for kind, text in parts
            )
            if uses_branch and branch is None:
                context.notes.append("the release uses the branch it runs for, not known here")
            if release.basis == "declared":
                context.notes.append("an operator declaration supplies how this release is made")
            found.append(context)
        return found

    def ingress_targets(self) -> list[DeploymentTarget]:
        targets: list[DeploymentTarget] = []
        for claim in self.grouped[DEPLOYMENT_TARGET_INGRESS]:
            meta = _meta(claim)
            path = self.paths.get(claim.file_id, "")
            templated = meta.get("source") == "chart-template"
            chart_dir = meta.get("chart", "")
            if self.only is not None and (not templated or chart_dir not in self.only):
                continue
            chart = self.charts.get(chart_dir) if templated else None
            hosts = _loads(meta.get("hosts"), [])
            tls = [entry["parts"] for entry in _loads(meta.get("tls"), [])]
            holes = _loads(meta.get("holes"), [])
            diagnostics = [
                f"line {hole['line']}: `{hole['name']}` is not read ({hole['reason']})"
                for hole in holes
            ]
            if meta.get("default_backend") == "true":
                diagnostics.append("a default backend answers any host; it names no target")
            if meta.get("annotations_unread") == "true":
                diagnostics.append("its annotations come from a helper, which is not read")
            if templated and chart is None:
                diagnostics.append(
                    "the chart's Chart.yaml is not indexed, so its name is not known"
                )
            if not hosts:
                diagnostics.append("it states no host")
            defaults = self.values.get(chart_dir, {}) if templated else {}
            defaults_context = _Context(
                "chart-defaults",
                "chart defaults",
                defaults=defaults,
                default_lines=self._default_lines(chart_dir),
            )
            contexts = (
                [
                    # Asked from the code another repository runs, only its own releases.
                    *([defaults_context] if self.only is None else []),
                    *self._releases(chart_dir),
                ]
                if templated
                else [_Context("declared", "as written")]
            )
            if not contexts:
                continue
            for host in hosts:
                parts: list[Part] = host["parts"]
                # Each path of the rule is its own target: a host serves each one.
                served: list[list[Part]] = []
                for entry in host.get("paths") or []:
                    if (entry.get("parts") or []) not in served:
                        served.append(entry.get("parts") or [])
                served = served or [[]]
                for index, path_parts in enumerate(served):
                    starred = [p[1] for p in [*parts, *path_parts] if p[0] == "value"]
                    known = set(defaults)
                    for context in contexts:
                        known.update(context.overrides)
                        if context.effective is not None:
                            known.update(context.effective.entries)
                    for binding in _bindings(starred, known):
                        key = ",".join(f"{k}={v}" for k, v in sorted(binding.items()))
                        if index:
                            # The first path keeps the id a target had before paths split.
                            key = f"{key};path={index}"
                        tid = target_id(self.repository, path, int(host["line"]), key)
                        targets.append(
                            self._ingress_target(
                                tid,
                                path,
                                host,
                                parts,
                                path_parts,
                                binding,
                                tls,
                                meta,
                                chart,
                                chart_dir,
                                contexts,
                                list(diagnostics),
                            )
                        )
        return targets

    def _ingress_target(
        self,
        tid: str,
        path: str,
        host: Mapping[str, object],
        parts: list[Part],
        path_parts: list[Part],
        binding: Mapping[str, int],
        tls: list[list[Part]],
        meta: Mapping[str, str],
        chart: str | None,
        chart_dir: str,
        contexts: list[_Context],
        diagnostics: list[str],
    ) -> DeploymentTarget:
        scheme = _scheme(meta, self.listens.get(chart_dir, {}), parts, tls)
        pattern_host = "".join(
            text if kind == "text" else "{" + _name([kind, text], binding) + "}"
            for kind, text in parts
        )
        pattern_path = "".join(
            text if kind == "text" else "{" + _name([kind, text], binding) + "}"
            for kind, text in path_parts
        )
        shown_scheme = scheme.schemes[0] if scheme.schemes else "{scheme}"
        pattern = f"{shown_scheme}://{pattern_host}{pattern_path or '/'}"
        guards_meta = list(host.get("guards") or [])  # type: ignore[call-overload]
        linked = [
            _Context(
                "link",
                link.label,
                overrides=_link_overrides(link, self.context),
                defaults=self.values.get(chart_dir, {}),
                environment=link.environment,
                conditions=evaluate(link.conditions, self.context),
                link=link,
            )
            for link in self.links
            if link.target_id == tid
        ]
        resolutions: list[DeploymentResolution] = []
        for context in [*contexts, *linked][:MAX_RESOLUTIONS]:
            resolutions.append(
                self._resolve(context, parts, path_parts, binding, scheme, guards_meta, chart)
            )
        guard_views = [
            DeploymentGuard(
                expression=_guard_view(g, contexts[0])[0],
                line=int(g.get("line", 0)),
                state=_guard_view(g, contexts[0])[1],
            )
            for g in guards_meta
        ]
        return DeploymentTarget(
            target_id=tid,
            kind="ingress",
            repository=self.repository,
            path=path,
            line=int(host["line"]),  # type: ignore[call-overload]
            pattern=pattern,
            schemes=scheme.schemes,
            scheme_basis=scheme.basis,
            guards=guard_views,
            resolutions=resolutions,
            diagnostics=list(dict.fromkeys(diagnostics)),
            runs=self.runs,
        )

    def _resolve(
        self,
        context: _Context,
        parts: list[Part],
        path_parts: list[Part],
        binding: Mapping[str, int],
        scheme: _Scheme,
        guards: list[Mapping[str, object]],
        chart: str | None,
    ) -> DeploymentResolution:
        host = _render(parts, binding, context, chart)
        served = _render(path_parts, binding, context, chart) if path_parts else None
        variables = list(host.variables)
        if served is not None:
            variables.extend(v for v in served.variables if v not in variables)
        chosen = _lookup("scheme", context) if context.basis == "link" else _Value(None, None)
        scheme_name = chosen.value or (scheme.schemes[0] if scheme.schemes else None)
        if scheme_name is None:
            variables.append(DeploymentVariable(name="scheme", source="scheme"))
        elif chosen.value is not None:
            variables.append(
                DeploymentVariable(
                    name="scheme", source="scheme", value=chosen.value, basis="configured"
                )
            )
        origin = None
        if host.resolved and scheme_name in _DEFAULT_PORTS:
            origin = _origin(scheme_name, host.text, scheme.ports.get(scheme_name))
        prefix = _path_prefix(served.text) if served is not None and served.resolved else None
        url = None
        if origin is not None:
            url = origin + (prefix if prefix not in (None, "/") else "/")
        rendered = f"{scheme_name or '{scheme}'}://{host.text}{served.text if served else '/'}"
        verification = "unverified"
        link_id = None
        if context.link is not None:
            link_id = context.link.id
            verification = context.link.verification
            if context.link.url is not None:
                url = context.link.url
                split = urlsplit(url)
                origin = f"{split.scheme}://{split.netloc}"
        enabled = _combined(_guard_view(g, context)[1] for g in guards)
        notes = [*context.notes, *host.notes, *(served.notes if served is not None else [])]
        if url is not None:
            state = "resolved"
        elif any(v.value is not None for v in variables) or any(p[0] == "text" for p in parts):
            state = "partial"
        else:
            state = "unresolved"
        return DeploymentResolution(
            basis=context.basis,  # type: ignore[arg-type]
            label=context.label,
            environment=context.environment,
            origin=origin,
            url=url,
            rendered=rendered,
            state=state,  # type: ignore[arg-type]
            variables=variables,
            enabled=enabled,
            conditions=context.conditions,
            verification=verification,  # type: ignore[arg-type]
            link_id=link_id,
            notes=list(dict.fromkeys(notes)),
        )

    def openapi_targets(self, edges: Sequence[HttpEdgeRow]) -> list[DeploymentTarget]:
        targets: dict[str, DeploymentTarget] = {}
        for edge in edges:
            if edge.type is not EdgeType.DECLARES_HTTP_DEPLOYMENT:
                continue
            if not edge.target_key.startswith(DEPLOYMENT_KEY_PREFIX):
                continue
            base = edge.target_key[len(DEPLOYMENT_KEY_PREFIX) :]
            path = edge.path or ""
            tid = target_id(self.repository, path, edge.line_start or 0, base)
            if tid in targets:
                continue
            split = urlsplit(base)
            targets[tid] = DeploymentTarget(
                target_id=tid,
                kind="openapi-server",
                repository=self.repository,
                path=edge.path,
                line=edge.line_start,
                pattern=base,
                schemes=[split.scheme],
                scheme_basis="declared",
                resolutions=[
                    DeploymentResolution(
                        basis="declared",
                        label="as declared",
                        origin=f"{split.scheme}://{split.netloc}",
                        url=base,
                        rendered=base,
                        state="resolved",
                    ),
                    *self._link_resolutions(tid),
                ],
            )
        return list(targets.values())

    def _link_resolutions(self, tid: str) -> list[DeploymentResolution]:
        return [self._link_only(link) for link in self.links if link.target_id == tid]

    def _link_only(self, link: DeploymentLink) -> DeploymentResolution:
        origin = None
        if link.url is not None:
            split = urlsplit(link.url)
            origin = f"{split.scheme}://{split.netloc}"
        return DeploymentResolution(
            basis="link",
            label=link.label,
            environment=link.environment,
            origin=origin,
            url=link.url,
            rendered=link.url or "",
            state="resolved" if link.url else "unresolved",
            conditions=evaluate(link.conditions, self.context),
            verification=link.verification,
            link_id=link.id,
        )

    def link_targets(self, known: set[str]) -> list[DeploymentTarget]:
        """Links of their own, and links whose target is no longer stated (detached)."""
        found: list[DeploymentTarget] = []
        for link in self.links:
            if link.target_id is not None and link.target_id in known:
                continue
            detached = link.target_id is not None
            found.append(
                DeploymentTarget(
                    target_id=link.target_id or f"link_{link.id}",
                    kind="link",
                    repository=self.repository,
                    pattern=link.url or "",
                    schemes=[urlsplit(link.url).scheme] if link.url else [],
                    scheme_basis="configured" if link.url else None,
                    resolutions=[self._link_only(link)],
                    diagnostics=(
                        ["no indexed file states this target any more"] if detached else []
                    ),
                    detached=detached,
                )
            )
        return found

    def ci_diagnostics(self) -> list[str]:
        """CI steps this does not interpret that look like they deploy with Helm: a
        release made there is not read, so whatever it sets is not known."""
        found: list[str] = []
        # A step an operator's declaration makes interpretable is read as a release.
        declared = {
            (r.claim.file_id, r.claim.line_start, r.claim.target_key) for r in self.ci_releases
        }
        for claim in self.grouped[CI_STEP_UNSUPPORTED]:
            if "helm" not in claim.target_key.lower():
                continue
            if (claim.file_id, claim.line_start, claim.target_key) in declared:
                continue
            path = self.paths.get(claim.file_id, "")
            found.append(
                f"{path}:{claim.line_start}: the CI step `{claim.target_key[:80]}` is not "
                "interpreted, so the values a release there sets are not known"
            )
        return found


def _mark_duplicates(targets: list[DeploymentTarget]) -> list[DeploymentTarget]:
    seen: dict[str, str] = {}
    marked: list[DeploymentTarget] = []
    for target in targets:
        resolutions = []
        for index, resolution in enumerate(target.resolutions):
            where = f"{target.target_id}/{index}"
            first = seen.setdefault(resolution.url, where) if resolution.url else where
            if first != where:
                resolution = resolution.model_copy(update={"duplicate_of": first})
            resolutions.append(resolution)
        marked.append(target.model_copy(update={"resolutions": resolutions}))
    return marked


@dataclass(frozen=True, slots=True)
class Elsewhere:
    """Another repository whose releases are configured to run the asked repository's
    built code (its ``container_artifact`` links): its claims and files, and the release
    names to resolve each of its charts under."""

    repository: str
    claims: Sequence[UnresolvedRefRecord]
    files: Mapping[int, FileRow]
    releases: Mapping[str, set[str]]


def build_targets(
    repository: str,
    claims: Sequence[UnresolvedRefRecord],
    files: Mapping[int, FileRow],
    edges: Sequence[HttpEdgeRow],
    links: Sequence[DeploymentLink],
    context: ConditionContext,
    *,
    declarations: Sequence[UnresolvedRefRecord] = (),
    elsewhere: Sequence[Elsewhere] = (),
) -> tuple[list[DeploymentTarget], list[str]]:
    """One repository's deployment targets, and what keeps any of them from resolving
    that belongs to no single target.

    Besides what the repository's own files state, each repository in ``elsewhere`` adds
    the Ingress targets of the charts whose releases run this repository's code, resolved
    under those releases alone and marked with ``runs``."""
    builder = _Builder(repository, claims, files, links, context, declarations=declarations)
    stated = [*builder.ingress_targets(), *builder.openapi_targets(edges)]
    for other in elsewhere:
        deploying = _Builder(
            other.repository,
            other.claims,
            other.files,
            (),
            context,
            declarations=declarations,
            only=other.releases,
        )
        deploying.runs = repository
        stated.extend(deploying.ingress_targets())
    stated.sort(key=lambda t: (t.runs or "", t.path or "", t.line or 0, t.target_id))
    stated = stated[:MAX_TARGETS]
    targets = _mark_duplicates([*stated, *builder.link_targets({t.target_id for t in stated})])
    return targets, builder.ci_diagnostics()
