"""Releases a CI configuration establishes, the images their workloads run, and the code the
builds configured to publish those images put in them (DF2: the artifact join).

DF1 decided which releases CI configurations establish and which images their jobs build
(:mod:`repoatlas.graph.ci_facts`). This stage, run only by the global pass, joins them:

* **Release binding.** A ``bound`` release renders its chart, in the same repository, with
  Helm's precedence: the chart's ``values.yaml``, each ``-f`` file in order (a file only a
  release names is read for it, D2-1), then each ``--set`` in order. A value is a literal, a
  CI symbol, or unknown; only a literal renders. A branch symbol over a literal finite
  domain (``CIRCLE_BRANCH`` with ``filters.branches.only``, ``github.ref_name`` with
  ``on.push.branches``) gives one variant per branch, at most :data:`MAX_VARIANTS`; nothing
  else -- a condition, a filter pattern -- ever adds or narrows one (D2-4, E4).
* **Workload and container.** A Service selects a workload only when that is proven from
  the templates as written: every helper its selector includes, the workload's labels
  include too, and every literal label agrees once rendered; a literal label with another
  value excludes it. Exactly one selected, every other excluded; then the one container its
  ``targetPort`` names (D2-2, E2). With the release's name unresolved, a selector including
  a helper is undecided: the helper may read the name (E9).
* **Artifact.** Each container of each rendered workload has one ``container_artifact``
  decision. Its image renders to a join key -- registry host, port and path, all literal,
  the registry never dropped -- and is joined to the ``publish-configured`` builds with
  that key (D2-6): one compatible publisher is ``linked``, several ``artifact-ambiguous``,
  only conflicting literal versions ``tag-unmatched``, none ``publisher-unresolved``. A
  link's version relation is ``exact`` (the same literal tag or digest), ``symbolic`` (the
  same execution-stable CI built-in, in one workflow whose deploy job requires the build,
  set by nothing in either scope, D2-3/E3) or ``unknown``.
* **Source.** A linked artifact ties the deployment to the code its build's Dockerfile and
  context put in the image, in the builder's repository (:func:`deploy_identity.source_tie`).
  A chart stating no workload at all ties as M3 does, to its repository's one application
  (D2-5, E5); any workload evidence -- read or not -- forbids that.
* **Competing deployments.** Every established release contributes an Ingress host it does
  not fully render as a pattern of its own, a rival to every exposure but its own (E8).

The results are exposure and identity claims for the HTTP join -- in memory, never stored
-- and ``container_artifact`` link decisions. Nothing here says that a deployment ran, that
an image was pushed or exists, or which version runs: only what the files configure.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import yaml

from repoatlas.graph.ci_facts import CiBuild, CiFactsPlan, CiRelease, DecisionRows, Place
from repoatlas.graph.deploy_identity import (
    _FALSY,
    _INCLUDE,
    _TEMPLATE,
    _VALUE_PATH,
    ARTIFACT_AMBIGUOUS,
    IMAGE_UNREAD,
    INGRESS,
    KUBERNETES,
    NAME_DEPENDENT,
    PUBLISHER_UNRESOLVED,
    REGISTRY_UNRESOLVED,
    TAG_UNMATCHED,
    TIED,
    WORKLOAD_UNMATCHED,
    Applications,
    DeploymentPlan,
    ServedRoute,
    _expose,
    _ingress_origins,
    _origins,
    _pattern,
    _spelled,
    _unsourced,
    helm_fullname,
    helm_name,
    source_tie,
)
from repoatlas.models.container_image import (
    EXPR,
    RESOLVED,
    TEXT,
    VAR,
    WITHHELD,
    parse_reference,
    text_of,
    version_parts,
)
from repoatlas.parsers.http_facts import (
    DOCKERFILE_COMMAND,
    HELM_CHART,
    HELM_HELPER,
    HELM_INGRESS,
    HELM_SERVICE,
    HELM_VALUES,
    HELM_VALUES_FILE,
    K8S_WORKLOAD,
    PENDING_FACT,
)
from repoatlas.storage.protocols import FileRow, LinkDecisionRow, UnresolvedRefRecord

CONTAINER_ARTIFACT = "container_artifact"
LINKED = "linked"
PUBLISHER_UNRESOLVED_DECISION = "publisher-unresolved"
ARTIFACT_AMBIGUOUS_DECISION = "artifact-ambiguous"
TAG_UNMATCHED_DECISION = "tag-unmatched"
IMAGE_UNRESOLVED_DECISION = "image-unresolved"
REGISTRY_UNRESOLVED_DECISION = "registry-unresolved"

EXACT = "exact"
SYMBOLIC = "symbolic"
UNKNOWN = "unknown"

#: Variants one release may expand into; more and it renders nothing (D2-4).
MAX_VARIANTS = 16
ALTERNATIVES_CAPPED = "alternatives-capped"

#: The branch symbols whose literal domain gives a release's variants, per CI system.
_BRANCH = {
    "circleci": frozenset({(VAR, "CIRCLE_BRANCH")}),
    "github": frozenset({(EXPR, "github.ref_name"), (VAR, "GITHUB_REF_NAME")}),
}
#: Execution-stable CI built-ins: a value every job of one run shares, a rerun included,
#: by the identity it stands for (E3). Nothing else is ever ``symbolic``.
STABLE: dict[str, dict[str, str]] = {
    "circleci": {
        "CIRCLE_SHA1": "commit",
        "pipeline.git.revision": "commit",
        "CIRCLE_BRANCH": "branch",
        "CIRCLE_TAG": "tag",
    },
    "github": {
        "github.sha": "commit",
        "GITHUB_SHA": "commit",
        "github.ref_name": "ref",
        "GITHUB_REF_NAME": "ref",
        "github.run_id": "run",
        "GITHUB_RUN_ID": "run",
        "github.run_number": "run-number",
        "GITHUB_RUN_NUMBER": "run-number",
    },
}

#: Why an artifact refusal fails the source hop: the chain's reason code (E6).
_REASONS = {
    IMAGE_UNRESOLVED_DECISION: "image_unread",
    REGISTRY_UNRESOLVED_DECISION: "registry_unresolved",
    PUBLISHER_UNRESOLVED_DECISION: "publisher_unresolved",
    ARTIFACT_AMBIGUOUS_DECISION: "artifact_ambiguous",
    TAG_UNMATCHED_DECISION: "tag_unmatched",
}
_OUTCOMES = {
    IMAGE_UNRESOLVED_DECISION: IMAGE_UNREAD,
    REGISTRY_UNRESOLVED_DECISION: REGISTRY_UNRESOLVED,
    PUBLISHER_UNRESOLVED_DECISION: PUBLISHER_UNRESOLVED,
    ARTIFACT_AMBIGUOUS_DECISION: ARTIFACT_AMBIGUOUS,
    TAG_UNMATCHED_DECISION: TAG_UNMATCHED,
}

_DOLLAR_ROOT = re.compile(r"(?<![\w$])\$\.")


@dataclass(slots=True)
class ArtifactPlan:
    #: ``http-exposed-endpoint`` claims, one per CI-bound identity and route.
    exposures: list[UnresolvedRefRecord] = field(default_factory=list)
    #: Identities that reach no code, and every established release's unrendered hosts.
    identities: list[UnresolvedRefRecord] = field(default_factory=list)
    decisions: list[LinkDecisionRow] = field(default_factory=list)
    detail: str = "artifacts: none"


# -- values, with Helm's precedence ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Value:
    #: ``lit``, ``sym`` (CI parts, a symbol among them) or ``unknown``.
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
            self.put(key, _UNKNOWN if text == "<withheld>" else _Value("lit", text, place=place))

    def setting(self, key: str | None, parts: Sequence[tuple[str, str]], place: Place) -> None:
        if key is None:
            self.unknown("set-unread")
            return
        if any(kind == WITHHELD for kind, _ in parts):
            self.put(key, _UNKNOWN)
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


# -- charts -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Chart:
    repo_id: int
    directory: str
    name: str
    app_version: str | None
    helpers: frozenset[str]
    values: tuple[dict[str, str], str] | None = None
    services: list[UnresolvedRefRecord] = field(default_factory=list)
    ingresses: list[UnresolvedRefRecord] = field(default_factory=list)
    workloads: list[UnresolvedRefRecord] = field(default_factory=list)


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


@dataclass(slots=True)
class _Release:
    """One variant of one established release, rendered."""

    ci: CiRelease
    variant: str
    values: _Effective
    #: The release's name, or ``None`` when not literal.
    name: str | None
    scope: str


@dataclass(slots=True)
class _Artifact:
    decision: str
    evidence: dict[str, object]
    ref: str
    build: CiBuild | None = None
    relation: str = ""
    #: The values places that set the image (its repository and tag).
    image_in: str = ""
    workload_in: str = ""


@dataclass(slots=True)
class _Tie:
    outcome: str
    #: The chain's reason code when the tie fails at the artifact, else ``""``.
    reason: str = ""
    served: tuple[ServedRoute, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


class _Planner:
    def __init__(
        self,
        claims: Sequence[UnresolvedRefRecord],
        by_id: Mapping[int, FileRow],
        routes: Sequence[ServedRoute],
        ci: CiFactsPlan,
    ) -> None:
        self.by_id = by_id
        self.slugs = {row.repo_id: row.repo_slug for row in by_id.values()}
        self.files: dict[int, dict[str, int]] = defaultdict(dict)
        for row in by_id.values():
            self.files[row.repo_id][row.rel_path] = row.file_id
        self.applications = Applications(routes, by_id)
        self.by_root: dict[int, list[ServedRoute]] = defaultdict(list)
        for route in routes:
            for root in route.roots:
                self.by_root[root].append(route)
        self.charts: dict[tuple[int, str], _Chart] = {}
        self.values_files: dict[tuple[int, str], dict[str, str]] = {}
        self.commands: dict[int, UnresolvedRefRecord] = {}
        grouped: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
        for claim in claims:
            if claim.file_id in by_id:
                grouped[_meta(claim).get(PENDING_FACT, "")].append(claim)
        helpers: dict[tuple[int, str], set[str]] = defaultdict(set)
        for claim in grouped[HELM_HELPER]:
            helpers[(claim.repo_id, _meta(claim).get("chart", ""))].add(claim.target_key)
        for claim in grouped[HELM_CHART]:
            meta = _meta(claim)
            key = (claim.repo_id, meta.get("chart", ""))
            self.charts[key] = _Chart(
                claim.repo_id,
                key[1],
                claim.target_key,
                meta.get("app_version"),
                frozenset(helpers.get(key, set())),
            )
        for claim in grouped[HELM_VALUES]:
            meta = _meta(claim)
            chart = self.charts.get((claim.repo_id, meta.get("chart", "")))
            if chart is not None:
                chart.values = (meta, by_id[claim.file_id].rel_path)
        for claim in grouped[HELM_VALUES_FILE]:
            self.values_files[(claim.repo_id, by_id[claim.file_id].rel_path)] = _meta(claim)
        for role, attribute in (
            (HELM_SERVICE, "services"),
            (HELM_INGRESS, "ingresses"),
            (K8S_WORKLOAD, "workloads"),
        ):
            for claim in grouped[role]:
                chart = self.charts.get((claim.repo_id, _meta(claim).get("chart", "")))
                if chart is not None:
                    getattr(chart, attribute).append(claim)
        for claim in grouped[DOCKERFILE_COMMAND]:
            self.commands[claim.file_id] = claim
        self.builds: dict[str, list[CiBuild]] = defaultdict(list)
        for build in ci.builds:
            if build.key:
                self.builds[build.key].append(build)
        # Each workflow's jobs and what each requires, as the claims of its jobs state.
        self.requires: dict[tuple[int, str, str], set[str]] = defaultdict(set)
        for item in (*ci.releases, *ci.builds):
            self.requires[(item.origin.file_id, item.workflow, item.job)].update(item.requires)
        self.releases = [r for r in ci.releases if r.decision in ("bound", "values-unresolved")]
        self.plan = DeploymentPlan()
        self.decisions = DecisionRows()

    # -- variants ------------------------------------------------------------------------

    def variants(self, release: CiRelease) -> list[tuple[str, dict[tuple[str, str], str]]]:
        """``("", {})`` for a release with no branch symbol; one ``branch=<b>`` each over a
        literal finite domain; none past the cap; one unexpanded when there is no domain
        or the symbol may be set by a scope or script (E3)."""
        symbols = _BRANCH.get(release.system, frozenset())
        used = {
            (kind, text)
            for parts in (release.release, *(p for _, p in release.sets))
            for kind, text in parts
            if (kind, text) in symbols
        }
        branches = release.domains.get("branches")
        settable = release.env_written or any(text in release.overridable for _, text in used)
        if not used or not isinstance(branches, list) or settable:
            return [("", {})]
        if len(branches) > MAX_VARIANTS:
            return []
        return [
            (f"branch={branch}", dict.fromkeys(used, str(branch))) for branch in sorted(branches)
        ]

    def effective(
        self, chart: _Chart, release: CiRelease, substitute: Mapping[tuple[str, str], str]
    ) -> _Effective:
        values = _Effective()
        if chart.values is not None:
            values.file(chart.values[0], chart.repo_id, chart.values[1])
        for path, _how in release.values:
            if path is None:
                values.unknown("values-unresolved")
                continue
            meta = self.values_files.get((release.origin.repo_id, path))
            if path == (chart.values[1] if chart.values else None):
                meta = chart.values[0] if chart.values else None
            if meta is None:
                values.unknown("values-unread")
                continue
            values.file(meta, release.origin.repo_id, path)
        if release.set_unread:
            values.unknown("set-unread")
        place = Place(
            release.origin.repo_id, release.origin.rel_path, release.claim.line_start or 1
        )
        for key, parts in release.sets:
            values.setting(key, _substituted(parts, substitute), place)
        return values

    # -- rendering ------------------------------------------------------------------------

    def render(self, text: str, chart: _Chart, release: _Release) -> tuple[str | None, str]:
        """A name, port or label as written, every action one this reads, under the
        release's values; else ``None`` and why (``unknown``, ``name``, ``unread``)."""
        rendered, cursor = "", 0
        for match in _TEMPLATE.finditer(text):
            rendered += text[cursor : match.start()]
            cursor = match.end()
            action = _DOLLAR_ROOT.sub(".", match.group(1))
            path = _VALUE_PATH.fullmatch(action)
            included = _INCLUDE.fullmatch(action)
            if action == ".Chart.Name":
                rendered += chart.name
            elif action == ".Release.Name":
                if release.name is None:
                    return None, "name"
                rendered += release.name
            elif path is not None:
                value = release.values.get(path.group(1)[1:])
                if value is not None and value.kind != "lit":
                    return None, "unknown"
                rendered += value.text if value is not None else ""
            elif included is not None and f"{included.group(1)}.{included.group(2)}" in (
                chart.helpers
            ):
                overrides = release.values.literals("nameOverride", "fullnameOverride")
                if overrides is None:
                    return None, "unknown"
                if included.group(2) == "name":
                    rendered += helm_name(chart.name, overrides)
                else:
                    if release.name is None and overrides.get("fullnameOverride", "") in _FALSY:
                        return None, "name"
                    rendered += helm_fullname(chart.name, release.name or "", overrides)
            else:
                return None, "unread"
        rendered += text[cursor:]
        if "{{" in rendered or "}}" in rendered:
            return None, "unread"
        return rendered, ""

    def condition(self, path: str, release: _Release) -> bool | None:
        """Whether an ``if .Values.<path>`` holds; ``None`` when not known."""
        value = release.values.get(path)
        if value is None:
            return release.values.nested(path)
        if value.kind != "lit":
            return None
        return value.text not in _FALSY

    # -- workload, container, artifact ---------------------------------------------------

    def artifacts(self, chart: _Chart, release: _Release) -> dict[tuple[int, int], _Artifact]:
        """Each container of each rendered workload: its ``container_artifact`` decision."""
        found: dict[tuple[int, int], _Artifact] = {}
        for workload in chart.workloads:
            meta = _meta(workload)
            if meta.get("if") and self.condition(meta["if"], release) is False:
                continue
            containers = json.loads(meta.get("containers", "[]")) if "unread" not in meta else []
            if not containers:
                containers = [{"name": "", "image_unread": meta.get("unread", "unread")}]
            for index, container in enumerate(containers):
                artifact = self.artifact(chart, release, workload, meta, container)
                if workload.id is not None:
                    found[(workload.id, index)] = artifact
        return found

    def artifact(
        self,
        chart: _Chart,
        release: _Release,
        workload: UnresolvedRefRecord,
        meta: Mapping[str, str],
        container: Mapping[str, object],
    ) -> _Artifact:
        origin = self.by_id[workload.file_id]
        line = container.get("image_line")
        line = int(line) if isinstance(line, int) else workload.line_start or 1
        written = str(container.get("name", ""))
        name = self.render(written, chart, release)[0] or written
        workload_in = f"{origin.rel_path}:{line}"
        evidence: dict[str, object] = {
            "release": release.name if release.name is not None else text_of(release.ci.release),
            "variant": release.variant,
            "container": name,
            "workload_in": workload_in,
            "binding_basis": release.ci.basis,
        }
        if release.ci.declared is not None:
            evidence["declared_in"] = _place(release.ci.declared, self.slugs)
        subject = (
            f"{release.ci.subject}#{release.variant}/{workload.target_key}"
            f"@{workload.line_start}/{name}"
        )
        if meta.get("if") and self.condition(meta["if"], release) is None:
            container = {**container, "image_unread": "its condition is not known"}
        parts, places, defaulted = (
            self.image(container.get("image"), chart, release)
            if "image_unread" not in container
            and "unread" not in container
            and isinstance(container.get("image"), list)
            else (None, [], False)
        )
        if parts is None:
            decision = IMAGE_UNRESOLVED_DECISION
            evidence["image"] = ""
        else:
            reference = parse_reference(parts)
            evidence["image"] = reference.join_key() or text_of([list(p) for p in parts])
            tag, digest = version_parts(parts)
            if tag is not None:
                evidence["tag"] = text_of(tag)
            if digest is not None:
                evidence["digest"] = text_of(digest)
            if reference.state == "registry-unresolved":
                decision = REGISTRY_UNRESOLVED_DECISION
            elif reference.state != RESOLVED or reference.join_key() is None:
                decision = IMAGE_UNRESOLVED_DECISION
            else:
                return self.join(
                    release,
                    workload,
                    origin,
                    line,
                    subject,
                    evidence,
                    str(reference.join_key()),
                    (tag, digest),
                    defaulted,
                    "; ".join(dict.fromkeys(places)),
                )
        ref = self.decide(origin, workload, line, subject, decision, evidence)
        return _Artifact(decision, evidence, ref, workload_in=workload_in)

    def image(
        self, written: object, chart: _Chart, release: _Release
    ) -> tuple[list[list[str]] | None, list[str], bool]:
        """A workload image's parts under the release's values, the places of the values
        it reads, and whether a symbol in it stands under a ``default``."""
        parts: list[list[str]] = []
        places: list[str] = []
        defaulted = False
        for item in written if isinstance(written, list) else []:
            kind, value = str(item[0]), str(item[1])
            default = str(item[3]) if len(item) > 3 and item[2] == "default" else None
            found = self.part(kind, value, chart, release)
            if found is not None and found.place is not None:
                places.append(_place(found.place, self.slugs, short=True))
            if (found is None or (found.kind == "lit" and found.text == "")) and default:
                found = self.default(default, chart, release)
                if found is not None and found.place is not None:
                    places.append(_place(found.place, self.slugs, short=True))
            elif found is not None and found.kind == "sym" and default:
                defaulted = True
            if found is None:
                continue  # absent and no default: Helm renders nothing
            if found.kind == "lit":
                parts.append([TEXT, found.text])
            elif found.kind == "sym":
                parts.extend([list(p) for p in found.parts])
            else:
                parts.append([EXPR, ""])
        merged: list[list[str]] = []
        for part in parts:
            if merged and part[0] == TEXT and merged[-1][0] == TEXT:
                merged[-1][1] += part[1]
            elif part != [TEXT, ""]:
                merged.append(part)
        return (merged or None), places, defaulted

    def part(self, kind: str, value: str, chart: _Chart, release: _Release) -> _Value | None:
        if kind == "text":
            return _Value("lit", value)
        if kind == "value":
            return release.values.get(value)
        if value == "Name":
            return _Value("lit", chart.name)
        if value == "AppVersion" and chart.app_version is not None:
            return _Value("lit", chart.app_version)
        return _UNKNOWN

    def default(self, written: str, chart: _Chart, release: _Release) -> _Value | None:
        if written.startswith('"'):
            return _Value("lit", written.strip('"'))
        path = _VALUE_PATH.fullmatch(written.lstrip("$"))
        if path is not None:
            return release.values.get(path.group(1)[1:])
        return self.part("chart", written.removeprefix(".Chart."), chart, release)

    def join(
        self,
        release: _Release,
        workload: UnresolvedRefRecord,
        origin: FileRow,
        line: int,
        subject: str,
        evidence: dict[str, object],
        key: str,
        version: tuple[list[list[str]] | None, list[list[str]] | None],
        defaulted: bool,
        image_in: str,
    ) -> _Artifact:
        """An image with a literal join key, joined to the builds configured to publish it."""
        tag, digest = version
        compatible: list[CiBuild] = []
        unmatched: list[CiBuild] = []
        not_publishing: list[CiBuild] = []
        for build in self.builds.get(key, []):
            if build.decision != "publish-configured":
                not_publishing.append(build)
            elif _conflict(tag, digest, build):
                unmatched.append(build)
            else:
                compatible.append(build)
        if not_publishing:
            evidence["not_publishing"] = [self.entry(b) for b in not_publishing]
        relation = ""
        build: CiBuild | None = None
        if len(compatible) == 1:
            decision = LINKED
            build = compatible[0]
            relation = self.relation(release.ci, build, tag, digest, defaulted)
            evidence["publisher"] = self.at(build)
            evidence["build_basis"] = build.basis
            evidence["version_relation"] = relation
        elif compatible:
            decision = ARTIFACT_AMBIGUOUS_DECISION
            evidence["publishers"] = [self.entry(b) for b in compatible]
        elif unmatched:
            decision = TAG_UNMATCHED_DECISION
            evidence["unmatched"] = [self.entry(b) for b in unmatched]
        else:
            decision = PUBLISHER_UNRESOLVED_DECISION
        ref = self.decide(origin, workload, line, subject, decision, evidence)
        return _Artifact(
            decision,
            evidence,
            ref,
            build,
            relation,
            image_in,
            workload_in=str(evidence["workload_in"]),
        )

    def relation(
        self,
        release: CiRelease,
        build: CiBuild,
        tag: list[list[str]] | None,
        digest: list[list[str]] | None,
        defaulted: bool,
    ) -> str:
        built_tag, built_digest = version_parts(build.parts)
        for mine, theirs in ((tag, built_tag), (digest, built_digest)):
            if _literal(mine) is not None and _literal(mine) == _literal(theirs):
                return EXACT
        if defaulted or digest is not None or built_digest is not None:
            return UNKNOWN
        deployed, built = _symbol(tag), _symbol(built_tag)
        if deployed is None or built is None:
            return UNKNOWN
        stable = STABLE.get(release.system, {})
        same_run = (
            release.system == build.system
            and release.origin.file_id == build.origin.file_id
            and release.workflow == build.workflow
            and self.requires_job(release, build.job)
        )
        settable = (
            release.env_written
            or build.env_written
            or deployed in release.overridable
            or built in build.overridable
        )
        if (
            same_run
            and not settable
            and deployed in stable
            and built in stable
            and stable[deployed] == stable[built]
        ):
            return SYMBOLIC
        return UNKNOWN

    def requires_job(self, release: CiRelease, job: str) -> bool:
        """Whether the release's job requires ``job``, directly or through jobs of the same
        workflow whose own requirements are stated."""
        seen: set[str] = set()
        pending = list(release.requires)
        while pending:
            current = pending.pop()
            if current == job:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(
                self.requires.get((release.origin.file_id, release.workflow, current), ())
            )
        return False

    def at(self, build: CiBuild) -> str:
        lines = [build.claim.line_start, build.published_line]
        listed = "; ".join(
            f"{build.origin.repo_slug}:{build.origin.rel_path}:{line}"
            for line in dict.fromkeys(lines)
            if line
        )
        return listed

    def entry(self, build: CiBuild) -> dict[str, object]:
        tag, digest = version_parts(build.parts)
        found: dict[str, object] = {"publisher": self.at(build), "basis": build.basis}
        if tag is not None:
            found["tag"] = text_of(tag)
        if digest is not None:
            found["digest"] = text_of(digest)
        if build.decision != "publish-configured":
            found["decision"] = build.decision
        return found

    def decide(
        self,
        origin: FileRow,
        workload: UnresolvedRefRecord,
        line: int,
        subject: str,
        decision: str,
        evidence: Mapping[str, object],
    ) -> str:
        self.decisions.add(
            CONTAINER_ARTIFACT,
            origin,
            workload.model_copy(update={"line_start": line, "line_end": line}),
            subject,
            decision,
            evidence,
        )
        return hashlib.sha256(f"{CONTAINER_ARTIFACT}\0{subject}".encode()).hexdigest()[:24]

    def select(
        self, service: UnresolvedRefRecord, chart: _Chart, release: _Release
    ) -> tuple[UnresolvedRefRecord, int] | str:
        """The one workload container the Service sends to, or why there is none (E2, E9)."""
        meta = _meta(service)
        selector = json.loads(meta.get("selector", "null"))
        if not isinstance(selector, list) or not selector:
            return WORKLOAD_UNMATCHED
        wanted_helpers = {str(value) for kind, value in selector if kind == "include"}
        if wanted_helpers and release.name is None:
            return NAME_DEPENDENT
        wanted: dict[str, str] = {}
        for key, value in selector:
            if key == "include":
                continue
            rendered, why = self.render(str(value), chart, release)
            if rendered is None:
                return NAME_DEPENDENT if why == "name" else WORKLOAD_UNMATCHED
            wanted[str(key)] = rendered
        selected: list[UnresolvedRefRecord] = []
        for workload in chart.workloads:
            wmeta = _meta(workload)
            held = self.condition(wmeta["if"], release) if wmeta.get("if") else True
            if held is False:
                continue
            if "unread" in wmeta or held is None:
                return WORKLOAD_UNMATCHED
            labels = json.loads(wmeta.get("labels", "[]"))
            helpers = {str(value) for kind, value in labels if kind == "include"}
            literal: dict[str, str | None] = {}
            for key, value in labels:
                if key != "include":
                    literal[str(key)] = self.render(str(value), chart, release)[0]
            conflict = any(
                key in literal and literal[key] is not None and literal[key] != value
                for key, value in wanted.items()
            )
            if conflict:
                continue
            if wanted_helpers <= helpers and all(literal.get(k) == v for k, v in wanted.items()):
                selected.append(workload)
                continue
            return WORKLOAD_UNMATCHED  # neither selected nor excluded: undecided
        if len(selected) != 1:
            return WORKLOAD_UNMATCHED
        workload = selected[0]
        index = self.container(service, workload, chart, release)
        return (workload, index) if index is not None else WORKLOAD_UNMATCHED

    def container(
        self,
        service: UnresolvedRefRecord,
        workload: UnresolvedRefRecord,
        chart: _Chart,
        release: _Release,
    ) -> int | None:
        """The container every one of the Service's target ports names (E2)."""
        containers = json.loads(_meta(workload).get("containers", "[]"))
        targets = json.loads(_meta(service).get("target_ports", "[]"))
        if not containers or not targets:
            return None
        chosen: set[int] = set()
        for port, target in targets:
            written = target if target is not None else port
            rendered, _ = self.render(str(written), chart, release)
            if rendered is None:
                return None
            matching: list[int] = []
            unknown = False
            for index, container in enumerate(containers):
                if "unread" in container or container.get("ports_open"):
                    unknown = True
                    continue
                for entry in container.get("ports", []):
                    if rendered.isdigit():
                        number, _ = self.render(str(entry.get("containerPort", "")), chart, release)
                        if number is None:
                            unknown = True
                        elif number == rendered:
                            matching.append(index)
                    elif entry.get("name") == rendered:
                        matching.append(index)
            if len(containers) == 1 and rendered.isdigit() and not matching:
                matching = [0]  # one container: a numbered target port reaches it
            if len(set(matching)) != 1 or (unknown and len(containers) > 1):
                return None
            chosen.add(matching[0])
        return chosen.pop() if len(chosen) == 1 else None

    # -- ties -----------------------------------------------------------------------------

    def tie(
        self,
        service: UnresolvedRefRecord,
        chart: _Chart,
        release: _Release,
        artifacts: Mapping[tuple[int, int], _Artifact],
    ) -> _Tie:
        """What a Service of a bound release reaches: through its workload's image, or --
        with no workload evidence at all -- through M3's one application (E5)."""
        if not chart.workloads:
            untied = self.applications.untied(chart.repo_id)
            if untied is not None:
                return _Tie(untied, extra={"source_rule": "same-repository"})
            return _Tie(
                TIED,
                served=tuple(self.applications.routes[chart.repo_id]),
                extra={
                    "source_rule": "same-repository",
                    "source_in": self.applications.source_in(chart.repo_id),
                },
            )
        chosen = self.select(service, chart, release)
        if isinstance(chosen, str):
            return _Tie(chosen, "workload_unmatched", extra={"source_rule": "image"})
        workload, index = chosen
        artifact = artifacts.get((workload.id or 0, index))
        if artifact is None:
            return _Tie(WORKLOAD_UNMATCHED, "workload_unmatched", extra={"source_rule": "image"})
        extra: dict[str, str] = {
            "source_rule": "image",
            "artifact_ref": artifact.ref,
            "workload_in": artifact.workload_in,
            "image": str(artifact.evidence.get("image", "")),
        }
        if artifact.image_in:
            extra["image_in"] = artifact.image_in
        if artifact.decision != LINKED or artifact.build is None:
            return _Tie(_OUTCOMES[artifact.decision], _REASONS[artifact.decision], extra=extra)
        build = artifact.build
        builder = build.origin.repo_id
        extra.update(
            {
                "built_by": build.origin.repo_slug,
                "build_basis": build.basis,
                "version_relation": artifact.relation,
                "published_in": "; ".join(
                    f"{build.origin.rel_path}:{line}"
                    for line in dict.fromkeys([build.claim.line_start, build.published_line])
                    if line
                ),
                "published_in_repo": str(builder),
            }
        )
        if build.basis != "source":
            extra["published_in_basis"] = build.basis
        if build.dockerfile:
            extra["built_in"] = build.dockerfile
            extra["built_in_repo"] = str(builder)
        if build.context is None or not build.dockerfile:
            return _Tie(DOCKERFILE_UNREAD_TIE, extra=extra)
        container = json.loads(_meta(workload).get("containers", "[]"))[index]
        tie = source_tie(
            self.files[builder],
            build.dockerfile,
            build.context,
            _override(container),
            self.commands,
            self.by_root,
            self.by_id,
        )
        if tie.command_in:
            extra["source_in"] = "; ".join(place for place in (tie.command_in, tie.module) if place)
            extra["source_in_repo"] = str(builder)
        return _Tie(tie.outcome, served=tie.served, extra=extra)

    # -- a release's identities -----------------------------------------------------------

    def release(self, ci: CiRelease) -> None:
        chart = self.charts.get((ci.origin.repo_id, ci.chart))
        if chart is None:
            return
        variants = self.variants(ci)
        for label, substitute in variants:
            name_parts = _substituted(ci.release, substitute)
            name = (
                "".join(text for _, text in name_parts)
                if name_parts and all(kind == TEXT for kind, _ in name_parts)
                else None
            )
            release = _Release(
                ci,
                label,
                self.effective(chart, ci, substitute),
                name,
                f"release:{ci.subject}#{label}",
            )
            bound = ci.decision == "bound"
            artifacts = self.artifacts(chart, release) if bound else {}
            ties: dict[int, _Tie] = {}
            for service in chart.services:
                meta = _meta(service)
                if meta.get("controlled") == "true" or not bound:
                    continue
                tie = self.tie(service, chart, release, artifacts)
                if service.id is not None:
                    ties[service.id] = tie
                rendered, _ = self.render(meta.get("name", ""), chart, release)
                if rendered is None or not _dns(rendered):
                    continue
                ports = [
                    self.render(str(text), chart, release)[0]
                    for text, _ in json.loads(meta.get("ports", "[]"))
                ]
                origins = _origins([rendered], [p for p in ports if p and p.isdigit()])
                self.identify(
                    service,
                    chart,
                    release,
                    tie,
                    origins,
                    KUBERNETES,
                    f"{self.by_id[service.file_id].rel_path}:{service.line_start}",
                    {},
                )
            for ingress in chart.ingresses:
                self.ingress(ingress, chart, release, ties if bound else None)

    def ingress(
        self,
        claim: UnresolvedRefRecord,
        chart: _Chart,
        release: _Release,
        ties: Mapping[int, _Tie] | None,
    ) -> None:
        """An Ingress under one variant of an established release: its rendered hosts, each
        an identity tied as its backend Service is, and its unrendered hosts, each a pattern
        rival to every other deployment (E8). ``ties`` is ``None`` for a release whose
        values are not read: it identifies nothing, but competes wherever it may."""
        meta = _meta(claim)
        if "unread" in meta:
            return
        origin = self.by_id[claim.file_id]
        backends = json.loads(meta.get("backends", "[]"))
        services = [s for s in chart.services if _meta(s).get("controlled") != "true"]
        named = [
            service
            for service in services
            if backends and _spelled(_meta(service).get("name", "")) == _spelled(backends[0][0])
        ]
        if not backends or not all(
            any(
                _spelled(_meta(s).get("name", "")) == _spelled(name)
                and _spelled(port)
                in {_spelled(str(t)) for t, _ in json.loads(_meta(s).get("ports", "[]"))}
                for s in services
            )
            for name, port, _ in backends
        ):
            return  # BACKEND_UNMATCHED, as the M-series pass reports it
        listen = [(str(scheme), int(port)) for scheme, port in json.loads(meta["listen"])]
        hosts = [(parts, int(line)) for parts, line in json.loads(meta["hosts"])]
        condition = meta.get("if", "")
        held = self.condition(condition, release) if condition else True
        if held is False:
            return
        iterable = meta.get("range", "")
        items: list[str | None] = [""]
        if iterable:
            indices = sorted(
                {
                    int(m.group(1))
                    for k in release.values.entries
                    if (m := re.match(re.escape(iterable) + r"\[(\d+)\]", k))
                }
            )
            items = [f"{iterable}[{index}]" for index in indices]
            if release.values.open or not items:
                items.append(None)  # items no layer states may exist
        extra = {
            "scheme_in": f"{origin.rel_path}:{meta.get('scheme_at', '')}",
            "backend_in": f"{origin.rel_path}:{backends[0][2]}",
        }
        tie = (
            ties.get(named[0].id or 0)
            if ties is not None and len(named) == 1
            else (_Tie(WORKLOAD_UNMATCHED, "workload_unmatched") if ties is not None else None)
        )
        for current in items:
            for parts, line in hosts:
                filled, values_in, complete = self.host(parts, chart, release, current, held)
                if complete and tie is not None:
                    host = "".join(text for _, text in filled)
                    origins = _ingress_origins(host, listen)
                    if origins:
                        self.identify(
                            claim,
                            chart,
                            release,
                            tie,
                            origins,
                            INGRESS,
                            f"{origin.rel_path}:{line}",
                            {**extra, **({"values_in": values_in} if values_in else {})},
                        )
                        continue
                for scheme, port in listen:
                    _pattern(
                        self.plan,
                        claim,
                        filled,
                        chart.name,
                        iterable,
                        scheme,
                        port,
                        {
                            "identity_source": INGRESS,
                            "identity_in": f"{origin.rel_path}:{line}",
                            **extra,
                            "chart": chart.name,
                            "bound_in": self.binding_in(release),
                            "binding_in": self.binding_in(release),
                            "binding_outcome": "; ".join(release.values.reasons)
                            or "a value it reads is not known",
                            "source_outcome": tie.outcome if tie is not None else "",
                            # An established release's own host: a rival to every exposure
                            # but its own variant's (E8).
                            "scope": release.scope,
                            **self.scalars(release),
                        },
                    )

    def host(
        self,
        parts: Sequence[Sequence[str]],
        chart: _Chart,
        release: _Release,
        item: str | None,
        held: bool | None,
    ) -> tuple[list[list[str]], str, bool]:
        """A host's parts with every known value filled in, the places of those values,
        and whether every part is now literal text."""
        filled: list[list[str]] = []
        places: list[str] = []
        complete = held is True and item is not None
        for kind, value in parts:
            if kind == "text":
                filled.append(["text", str(value)])
                continue
            if kind == "chart":
                filled.append(["chart", chart.name])
                continue
            path = str(value) if kind == "value" else (f"{item}.{value}" if item else None)
            found = release.values.get(path) if path is not None else _UNKNOWN
            if found is not None and found.kind == "lit":
                filled.append(["text", found.text])
                if found.place is not None:
                    places.append(_place(found.place, self.slugs, short=True))
            elif found is None:
                filled.append(["text", ""])
            else:
                filled.append([str(kind), str(value)])
                complete = False
        return filled, "; ".join(dict.fromkeys(places)), complete

    def binding_in(self, release: _Release) -> str:
        return f"{release.ci.origin.rel_path}:{release.ci.claim.line_start}"

    def scalars(self, release: _Release) -> dict[str, str]:
        """What every claim of one release variant carries: who deploys it, how, where."""
        found = {
            "deployed_by": release.ci.origin.repo_slug,
            "release": release.name if release.name is not None else text_of(release.ci.release),
            "binding_basis": release.ci.basis,
            "binding_in_repo": str(release.ci.origin.repo_id),
            "release_scope": release.scope,
        }
        if release.variant:
            found["variant"] = release.variant
        if release.ci.namespace:
            found["namespace"] = release.ci.namespace
        if release.ci.basis != "source":
            found["binding_in_basis"] = release.ci.basis
        if release.ci.declared is not None:
            found["declared_in"] = f"{release.ci.declared.path}:{release.ci.declared.line}"
            found["declared_in_repo"] = str(release.ci.declared.repo_id)
            found["declared_in_basis"] = "declared"
        return found

    def identify(
        self,
        claim: UnresolvedRefRecord,
        chart: _Chart,
        release: _Release,
        tie: _Tie,
        origins: Sequence[str],
        source: str,
        identity_in: str,
        extra: Mapping[str, str],
    ) -> None:
        if not origins:
            return
        binding_in = self.binding_in(release)
        meta = {**extra, "binding_in": binding_in, **self.scalars(release), **tie.extra}
        # The chart, its values and the release are the deployment repository's places;
        # the build's and the code's are the builder's (set by the tie).
        for key in (
            "identity_in",
            "scheme_in",
            "backend_in",
            "values_in",
            "workload_in",
            "image_in",
        ):
            if key in meta or key == "identity_in":
                meta[f"{key}_repo"] = str(chart.repo_id)
        scope = f"chart:{chart.repo_id}:{chart.directory}"
        if tie.outcome == TIED and tie.served:
            _expose(
                self.plan,
                tie.served,
                origins,
                source=source,
                identity_in=identity_in,
                bound_in=binding_in,
                kind="kubernetes",
                scope=scope,
                extra=meta,
            )
            return
        if tie.reason:
            meta["source_reason"] = tie.reason
        _unsourced(
            self.plan,
            claim,
            origins,
            source=source,
            identity_in=identity_in,
            bound_in=binding_in,
            outcome=tie.outcome,
            scope=scope,
            extra=meta,
        )


DOCKERFILE_UNREAD_TIE = "the build's Dockerfile or context is not stated literally"


def _substituted(
    parts: Iterable[tuple[str, str]], substitute: Mapping[tuple[str, str], str]
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for kind, text in parts:
        literal = substitute.get((kind, text))
        found.append((TEXT, literal) if literal is not None else (kind, text))
    return found


def _place(place: Place, slugs: Mapping[int, str], *, short: bool = False) -> str:
    return (
        f"{place.path}:{place.line}"
        if short
        else f"{slugs.get(place.repo_id, place.repo_id)}/{place.path}:{place.line}"
    )


def _literal(parts: Sequence[Sequence[str]] | None) -> str | None:
    if parts is None or any(kind != TEXT for kind, _ in parts):
        return None
    return "".join(text for _, text in parts)


def _symbol(parts: Sequence[Sequence[str]] | None) -> str | None:
    """The one CI symbol a version consists of, or ``None``."""
    if parts is None or len(parts) != 1 or parts[0][0] not in (VAR, EXPR):
        return None
    return str(parts[0][1]) or None


def _conflict(tag: list[list[str]] | None, digest: list[list[str]] | None, build: CiBuild) -> bool:
    """Whether both sides state a literal tag that differs, or a literal digest that does."""
    built_tag, built_digest = version_parts(build.parts)
    for mine, theirs in ((tag, built_tag), (digest, built_digest)):
        left, right = _literal(mine), _literal(theirs)
        if left is not None and right is not None and left != right:
            return True
    return False


def _dns(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", name)) and len(name) <= 63


def _argv_text(text: object) -> str | None:
    """A container's ``command``/``args`` as compose writes a command: a JSON list, or
    ``"null"`` when not a literal flow list of strings."""
    if not isinstance(text, str) or text == "unread" or "{{" in text:
        return "null"
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return "null"
    if isinstance(loaded, list) and all(isinstance(item, (str, int)) for item in loaded):
        return json.dumps([str(item) for item in loaded])
    return "null"


def _override(container: Mapping[str, object]) -> dict[str, str]:
    """A Kubernetes container's override, as compose's: ``command`` replaces ENTRYPOINT (and
    drops CMD), ``args`` replaces CMD."""
    found: dict[str, str] = {}
    if "command" in container:
        found["entrypoint"] = _argv_text(container["command"]) or "null"
    if "args" in container:
        found["command"] = _argv_text(container["args"]) or "null"
    return found


def plan_deployment_artifacts(
    claims: Sequence[UnresolvedRefRecord],
    by_id: Mapping[int, FileRow],
    routes: Sequence[ServedRoute],
    ci: CiFactsPlan,
) -> ArtifactPlan:
    """Every CI-established release, rendered and joined. Writes nothing."""
    planner = _Planner(claims, by_id, routes, ci)
    for release in planner.releases:
        planner.release(release)
    plan = planner.plan
    rows = sorted(planner.decisions.rows.values(), key=lambda row: (row.subject, row.decision_ref))
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.decision] += 1
    detail = (
        "artifacts: "
        + ", ".join(f"{decision} {count}" for decision, count in sorted(counts.items()))
        if counts
        else "artifacts: none"
    )
    return ArtifactPlan(
        exposures=list(plan.exposures),
        identities=[*plan.identities, *plan.patterns],
        decisions=rows,
        detail=detail,
    )
