"""Where a deployment configures the environment variables its application reads (DF3a).

DF2 (:mod:`repoatlas.graph.deploy_artifacts`) proves which repository's code a deployed
container runs: the one build configured to publish its image, with that build's Dockerfile
and context. This stage, run only by the global pass, reads the container's own
configuration -- its ``env`` and ``envFrom``, or a compose service's ``environment`` -- and
crosses from the one place each variable is effectively set to the declarations that read it
in that image (DF3A_PHASE_A.md §3, §12):

* **Setter.** A narrow subset of the documented precedence, never a general evaluator. A
  direct ``env`` entry wins over ``envFrom``. Its ``.Values`` path takes Helm's precedence as
  DF2 renders it -- the chart's values, each ``-f`` in order, each ``--set`` -- and the setter
  is the winning leaf's key, or the CI step key carrying the ``--set``; a ``configMapKeyRef``
  names a ConfigMap of the same chart. With no direct entry, the last ``envFrom`` source
  holding the variable is the setter, unless a source after it cannot be read. A compose
  ``environment`` entry with a value is its own setter. Refusal is the default: a Secret, a
  pass-through entry, an unread, conditional or later source that may override, a duplicate,
  a value no indexed key holds -- each is ``setter-unresolved``, never a guessed setter.
* **Readers.** The builder's declarations (or files) reading the variable in the image: a
  literal final-stage copy of the build context covers the file and no later copy may
  overwrite it. ``reader-outside-image`` is claimed only when every final-stage copy is read
  and the final stage builds on no earlier stage; otherwise what the image holds is
  ``image-unread``.
* **Value.** Never read. Its kind is all that is kept: ``literal``; ``symbolic``, a CI
  variable or a compose interpolation; ``withheld``, a credential (D3-4).

The results are ``deployment_env`` link decisions -- one per container (or compose service),
release variant and variable, for every variable the configuration names that a reader in
the builder's repository reads -- and one ``CONFIGURES`` edge per (setter, reader), whose
``deployment_env`` record carries DF2's artifact evidence. Nothing is written to an ``env:``
node and nothing joins two repositories on a variable's name: a crossing exists only through
DF2's proven artifact, or inside one compose repository. What is configured, never what runs.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from repoatlas.graph.ci_facts import CiBuild, CiRelease, DecisionRows, Place, ref_of
from repoatlas.graph.deploy_identity import _FALSY, _copied
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    COMPOSE_SERVICE,
    DOCKERFILE_COMMAND,
    K8S_CONFIGMAP,
    PENDING_FACT,
)
from repoatlas.storage.protocols import (
    EdgeRecord,
    EnvReadRow,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

DEPLOYMENT_ENV = "deployment_env"
#: The resolution key a crossing's ``CONFIGURES`` edge records (EVIDENCE_PATHS).
DEPLOYMENT_ENV_EVIDENCE = DEPLOYMENT_ENV

LINKED = "linked"
#: The ``container_artifact`` decision that proves which code a container runs (DF2).
ARTIFACT_LINKED = "linked"
SETTER_UNRESOLVED = "setter-unresolved"
READER_OUTSIDE_IMAGE = "reader-outside-image"
IMAGE_UNREAD = "image-unread"

# Why no setter is established.
UNREAD_SOURCE = "unread-source"
CONDITIONAL = "conditional"
SECRET = "secret"  # noqa: S105 - a refusal reason, not a credential
PASSTHROUGH = "passthrough"
NOT_INDEXED = "not-indexed"
VALUES_UNRESOLVED = "values-unresolved"
DUPLICATE_ENTRY = "duplicate-entry"

# Why what an image holds is not read.
DOCKERFILE_UNREAD = "dockerfile-unread"
COPY_UNRESOLVED = "copy-unresolved"
BASE_UNRESOLVED = "base-unresolved"

# Where a setter is.
VALUES_FILE = "values-file"
SET = "set"
CONFIGMAP_KEY = "configmap-key"
ENV_FROM = "env-from"
COMPOSE_ENVIRONMENT = "compose-environment"

LITERAL = "literal"
SYMBOLIC = "symbolic"
WITHHELD = "withheld"
_VALUE_KINDS = {"lit": LITERAL, "sym": SYMBOLIC, "withheld": WITHHELD}

#: A compose file compose merges over the one beside it without being asked.
_OVERRIDE = re.compile(r"(docker-)?compose\.override\.ya?ml")
#: DF2's artifact evidence a crossing carries, as DF2 recorded it.
_ARTIFACT_KEYS = (
    "release",
    "variant",
    "container",
    "workload_in",
    "image",
    "version_relation",
    "binding_basis",
    "build_basis",
    "declared_in",
)


class Rendered(Protocol):
    """A value as DF2 renders it: its kind (``lit``, ``sym``, ``withheld`` or ``unknown``), its
    literal text, and the place that sets it."""

    @property
    def kind(self) -> str: ...
    @property
    def text(self) -> str: ...
    @property
    def place(self) -> Place | None: ...


class DeployedContainer(Protocol):
    """One rendered container of a release variant with its ``container_artifact`` decision,
    as the artifact join hands it to the global pass (``ArtifactPlan.containers``). Read here,
    never recomputed: the join is made once (DF2_PHASE_A.md §15, E7)."""

    @property
    def release(self) -> CiRelease: ...
    @property
    def variant(self) -> str: ...
    @property
    def release_name(self) -> str | None: ...
    @property
    def chart_repo_id(self) -> int: ...
    @property
    def chart_directory(self) -> str: ...
    @property
    def workload(self) -> UnresolvedRefRecord: ...
    @property
    def container(self) -> Mapping[str, object]: ...
    @property
    def decision(self) -> str: ...
    @property
    def evidence(self) -> Mapping[str, object]: ...
    @property
    def artifact_ref(self) -> str: ...
    @property
    def artifact_subject(self) -> str: ...
    @property
    def build(self) -> CiBuild | None: ...
    def value(self, path: str) -> Rendered | None: ...
    def holds(self, path: str) -> bool | None: ...


def _meta(claim: UnresolvedRefRecord) -> dict[str, object]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return loaded if isinstance(loaded, dict) else {}


@dataclass(frozen=True, slots=True)
class _Setter:
    kind: str
    #: Where the variable is set: what ``setter_at`` cites.
    place: Place
    #: The configuration key's qualified name; ``None`` for the CI step key holding the line.
    key: str | None
    value: str
    #: The key's own line, when it is not ``place``'s: a compose list entry's
    #: ``environment`` key, which holds the entry's line.
    key_line: int | None = None


@dataclass(frozen=True, slots=True)
class _Refused:
    reason: str
    #: Where the container's configuration names the variable or the source that refuses it.
    at: int
    evidence: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Image:
    """What a final image holds of its builder's files, as far as its Dockerfile says."""

    dockerfile: str
    files: frozenset[str] = frozenset()
    copies: tuple[tuple[object, ...], ...] = ()
    context: str = ""
    #: Why nothing it holds is read; ``""`` when its Dockerfile's final stage is read.
    unread: str = ""
    #: Why what it does not hold is not established; ``""`` when every copy is read.
    partial: str = ""

    def held(self, path: str) -> bool | None:
        """Whether the image holds the repository file ``path``: ``None`` when not known."""
        if self.unread:
            return None
        uncertain = False
        for sources, destination, _line, foreign, into in self.copies:
            if foreign or not isinstance(sources, list):
                continue
            for source in sources:
                base = posixpath.normpath(posixpath.join(self.context or ".", str(source)))
                if base == ".." or base.startswith(("../", "/")):
                    continue
                base = "" if base == "." else base
                if path == base:
                    relative = None
                elif base == "" or path.startswith(base + "/"):
                    relative = path[len(base) + 1 :] if base else path
                else:
                    continue
                if not isinstance(destination, str):
                    uncertain = True
                    continue
                if relative is not None:
                    target = posixpath.join(destination, relative)
                elif into or len(sources) > 1:
                    target = posixpath.join(destination, posixpath.basename(path))
                else:
                    target = destination
                # What the image layers there last: the reader's file, or another, or unknown.
                found = _copied(target, self.copies, self.context, set(self.files))
                if found == path:
                    return True
                if found is None:
                    uncertain = True
        return None if uncertain or self.partial else False


@dataclass(slots=True)
class DeploymentEnvPlan:
    #: One ``CONFIGURES`` edge per (setter, reader), its ``deployment_env`` record attached.
    edges: list[EdgeRecord] = field(default_factory=list)
    decisions: list[LinkDecisionRow] = field(default_factory=list)
    detail: str = "deployment env: none"


class _Planner:
    def __init__(
        self,
        claims: Sequence[UnresolvedRefRecord],
        by_id: Mapping[int, FileRow],
        symbols: Sequence[SymbolRow],
        reads: Sequence[EnvReadRow],
        producer: str,
    ) -> None:
        self.by_id = by_id
        self.producer = producer
        self.slugs = {row.repo_id: row.repo_slug for row in by_id.values()}
        self.files: dict[int, dict[str, int]] = defaultdict(dict)
        for row in by_id.values():
            self.files[row.repo_id][row.rel_path] = row.file_id
        # Configuration keys by file and name, and each Dockerfile's stages.
        self.keys: dict[tuple[int, str], list[SymbolRow]] = defaultdict(list)
        self.keys_in: dict[int, list[SymbolRow]] = defaultdict(list)
        self.stages: dict[int, list[tuple[int, str, str | None]]] = defaultdict(list)
        for symbol in symbols:
            if symbol.type is not EntityType.CONFIG:
                continue
            if symbol.meta_json and '"stage"' in symbol.meta_json:
                meta = symbol.meta
                if "stage" in meta:
                    self.stages[symbol.file_id].append(
                        (int(meta["stage"]), symbol.name, meta.get("image"))
                    )
                    continue
            if symbol.qualified_name:
                self.keys[(symbol.file_id, symbol.qualified_name)].append(symbol)
                self.keys_in[symbol.file_id].append(symbol)
        self.commands: dict[int, UnresolvedRefRecord] = {}
        self.configmaps: dict[tuple[int, str], list[UnresolvedRefRecord]] = defaultdict(list)
        self.services: list[UnresolvedRefRecord] = []
        for claim in claims:
            if claim.file_id not in by_id:
                continue
            role = _meta(claim).get(PENDING_FACT)
            if role == DOCKERFILE_COMMAND:
                self.commands[claim.file_id] = claim
            elif role == K8S_CONFIGMAP:
                chart = str(_meta(claim).get("chart", ""))
                self.configmaps[(claim.repo_id, chart)].append(claim)
            elif role == COMPOSE_SERVICE:
                self.services.append(claim)
        self.reads: dict[tuple[int, str], list[EnvReadRow]] = defaultdict(list)
        for read in reads:
            if read.path is not None:
                self.reads[(read.repo_id, read.variable)].append(read)
        self.read_in: dict[int, set[str]] = defaultdict(set)
        for repo_id, variable in self.reads:
            self.read_in[repo_id].add(variable)
        self.decisions = DecisionRows()
        #: (setter entity, reader entity) -> the first decision's subject, record and setter
        #: place, and every deployment configuring it.
        self.crossings: dict[tuple[int, int], tuple[str, dict[str, object], Place, set[str]]] = {}

    # -- the image ------------------------------------------------------------------------

    def image(self, repo_id: int, dockerfile: object, context: object) -> _Image:
        files = self.files.get(repo_id, {})
        if not isinstance(dockerfile, str) or not dockerfile or not isinstance(context, str):
            return _Image(str(dockerfile or ""), unread=DOCKERFILE_UNREAD)
        file_id = files.get(dockerfile)
        command = self.commands.get(file_id) if file_id is not None else None
        if file_id is None or command is None:
            return _Image(dockerfile, unread=DOCKERFILE_UNREAD)
        copies = json.loads(str(_meta(command).get("copies", "[]")))
        listed = tuple(tuple(copy) for copy in copies if isinstance(copy, list) and len(copy) == 5)
        partial = ""
        if len(listed) != len(copies) or any(c[3] or not isinstance(c[1], str) for c in listed):
            partial = COPY_UNRESOLVED
        else:
            stages = sorted(self.stages.get(file_id, []))
            earlier = {name for _, name, _ in stages[:-1]}
            base = stages[-1][2] if stages else None
            if base is None or base in earlier:
                partial = BASE_UNRESOLVED
        return _Image(dockerfile, frozenset(files), listed, context, partial=partial)

    # -- setters --------------------------------------------------------------------------

    def configmap(
        self, name: object, deployed: DeployedContainer
    ) -> tuple[UnresolvedRefRecord, dict[str, int]] | str:
        """The chart's one rendered ConfigMap of that literal name and its keys, or why not."""
        maps = self.configmaps.get((deployed.chart_repo_id, deployed.chart_directory), [])
        named = [m for m in maps if m.target_key == name and "unread" not in _meta(m)]
        if len(named) != 1 or any("unread" in _meta(m) for m in maps if m not in named):
            return UNREAD_SOURCE
        found = named[0]
        condition = _meta(found).get("if")
        if condition:
            rendered = deployed.holds(str(condition))
            if rendered is None:
                return CONDITIONAL
            if rendered is False:
                return UNREAD_SOURCE
        keys = json.loads(str(_meta(found).get("keys", "[]")))
        return found, {str(key): int(line) for key, line in keys}

    def readable(self, source: Mapping[str, object], deployed: DeployedContainer) -> bool:
        return source.get("source") == "configMapRef" and not isinstance(
            self.configmap(source.get("name"), deployed), str
        )

    def helm_setter(
        self, variable: str, deployed: DeployedContainer, set_place: Place
    ) -> _Setter | _Refused:
        container = deployed.container
        entries = [e for e in _list(container.get("env")) if e.get("name") == variable]
        if len(entries) > 1:
            return _Refused(DUPLICATE_ENTRY, _line(entries[-1]))
        if entries:
            return self.direct(entries[0], deployed, set_place)
        sources = _list(container.get("env_from"))
        line = _line(sources[-1]) if sources else _line(container)
        if container.get("envFrom_open"):
            return _Refused(UNREAD_SOURCE, line, {"source": "an action may add sources"})
        for source in reversed(sources):
            kind, name = source.get("source"), source.get("name")
            if kind == "conditional":
                return _Refused(CONDITIONAL, _line(source))
            if kind == "secretRef":
                return _Refused(
                    SECRET,
                    _line(source),
                    {"reference_kind": "secretRef", "reference": str(name or "")},
                )
            found = self.configmap(name, deployed) if kind == "configMapRef" else UNREAD_SOURCE
            if isinstance(found, str):
                return _Refused(found, _line(source), {"source": str(name or "")})
            configmap, keys = found
            if variable in keys:
                origin = self.by_id[configmap.file_id]
                return _Setter(
                    ENV_FROM,
                    Place(configmap.repo_id, origin.rel_path, keys[variable]),
                    f"data.{variable}",
                    LITERAL,
                )
        return _Refused(UNREAD_SOURCE, line)

    def direct(
        self, entry: Mapping[str, object], deployed: DeployedContainer, set_place: Place
    ) -> _Setter | _Refused:
        line = _line(entry)
        source = entry.get("source")
        if source == "conditional":
            return _Refused(CONDITIONAL, line)
        if source == "secretKeyRef":
            return _Refused(
                SECRET,
                line,
                {
                    "reference_kind": "secretKeyRef",
                    "reference": f"{entry.get('ref_name', '')}/{entry.get('ref_key', '')}",
                },
            )
        if source == "configMapKeyRef":
            found = self.configmap(entry.get("ref_name"), deployed)
            key = str(entry.get("ref_key", ""))
            if isinstance(found, str) or key not in found[1]:
                reason = found if isinstance(found, str) else UNREAD_SOURCE
                return _Refused(reason, line, {"source": str(entry.get("ref_name", ""))})
            configmap, keys = found
            origin = self.by_id[configmap.file_id]
            return _Setter(
                CONFIGMAP_KEY,
                Place(configmap.repo_id, origin.rel_path, keys[key]),
                f"data.{key}",
                LITERAL,
            )
        if source == "unread":
            return _Refused(UNREAD_SOURCE, line)
        path = entry.get("values")
        if source != "value" or not isinstance(path, str):
            # A literal value, a fieldRef: set by the template itself, which has no key.
            return _Refused(NOT_INDEXED, line)
        value = deployed.value(path)
        if value is None:
            return _Refused(NOT_INDEXED, line, {"values": path})
        kind = _VALUE_KINDS.get(value.kind)
        if kind is None or value.place is None:
            return _Refused(VALUES_UNRESOLVED, line, {"values": path})
        if entry.get("default") and (kind != LITERAL or value.text in _FALSY):
            # The template's own default may stand in for an empty value.
            return _Refused(VALUES_UNRESOLVED, line, {"values": path, "default": "true"})
        if value.place == set_place:
            return _Setter(SET, value.place, None, kind)
        return _Setter(VALUES_FILE, value.place, path, kind)

    def setter_entity(self, setter: _Setter) -> SymbolRow | None:
        file_id = self.files.get(setter.place.repo_id, {}).get(setter.place.path)
        if file_id is None:
            return None
        if setter.key is not None:
            line = setter.key_line if setter.key_line is not None else setter.place.line
            found = [s for s in self.keys.get((file_id, setter.key), []) if s.line_start == line]
            return found[0] if len(found) == 1 else None
        holding = [
            s
            for s in self.keys_in.get(file_id, [])
            if s.line_start is not None
            and s.line_start <= setter.place.line <= (s.line_end or s.line_start)
        ]
        # The deepest key holding the line: the step's ``run`` (or its orb parameter).
        holding.sort(
            key=lambda s: (-(s.line_start or 0), (s.line_end or 0), s.qualified_name or "")
        )
        return holding[0] if holding else None

    # -- deciding -------------------------------------------------------------------------

    def decide(
        self,
        *,
        subject: str,
        variable: str,
        outcome: _Setter | _Refused,
        image: _Image,
        readers: list[EnvReadRow],
        claim: UnresolvedRefRecord,
        entry_place: Place,
        base: dict[str, object],
        deployment: str,
    ) -> None:
        """One decision for ``variable``, and the crossings it makes."""
        first = readers[0].src_id
        evidence: dict[str, object] = {**base, "variable": variable}
        at = Place(entry_place.repo_id, entry_place.path, entry_place.line)
        evidence["entry_at"] = f"{at.path}:{at.line}"
        if isinstance(outcome, _Refused):
            evidence.update({"reason": outcome.reason, **outcome.evidence})
            evidence["readers"] = _places(readers)
            self.record(claim, at, first, subject, SETTER_UNRESOLVED, evidence)
            return
        entity = self.setter_entity(outcome)
        setter_at = f"{outcome.place.path}:{outcome.place.line}"
        if entity is None:
            evidence.update(
                {"reason": NOT_INDEXED, "setter_at": setter_at, "readers": _places(readers)}
            )
            self.record(claim, at, first, subject, SETTER_UNRESOLVED, evidence)
            return
        evidence.update(
            {
                "setter_kind": outcome.kind,
                "setter_at": setter_at,
                "setter": entity.qualified_name or "",
                "value": outcome.value,
                "dockerfile": image.dockerfile,
            }
        )
        held = {read.src_id: image.held(read.path or "") for read in readers}
        inside = [read for read in readers if held[read.src_id] is True]
        unknown = [read for read in readers if held[read.src_id] is None]
        outside = [read for read in readers if held[read.src_id] is False]
        setter_place = outcome.place
        if inside:
            evidence["reader_at"] = _places(inside)
            if unknown:
                evidence["unestablished"] = _places(unknown)
            if outside:
                evidence["outside"] = _places(outside)
            subject = self.record(claim, setter_place, inside[0].src_id, subject, LINKED, evidence)
            for read in inside:
                record = {
                    **{
                        k: v
                        for k, v in evidence.items()
                        if k not in ("reader_at", "unestablished", "outside")
                    },
                    "reader_at": f"{read.path}:{read.line_start}",
                    "decision_ref": ref_of(DEPLOYMENT_ENV, subject),
                }
                key = (entity.entity_id, read.src_id)
                before = self.crossings.get(key)
                if before is None or subject < before[0]:
                    deployments = before[3] if before is not None else set()
                    self.crossings[key] = (subject, record, setter_place, deployments)
                self.crossings[key][3].add(deployment)
            return
        if image.unread or unknown or image.partial:
            evidence["reason"] = image.unread or image.partial or COPY_UNRESOLVED
            evidence["readers"] = _places(readers)
            self.record(claim, setter_place, first, subject, IMAGE_UNREAD, evidence)
            return
        evidence["outside"] = _places(readers)
        self.record(claim, setter_place, first, subject, READER_OUTSIDE_IMAGE, evidence)

    def record(
        self,
        claim: UnresolvedRefRecord,
        place: Place,
        source: int,
        subject: str,
        decision: str,
        evidence: Mapping[str, object],
    ) -> str:
        file_id = self.files.get(place.repo_id, {}).get(place.path, claim.file_id)
        return self.decisions.add(
            DEPLOYMENT_ENV,
            self.by_id[file_id],
            claim.model_copy(
                update={
                    "file_id": file_id,
                    "src_id": source,
                    "line_start": place.line,
                    "line_end": place.line,
                }
            ),
            subject,
            decision,
            evidence,
        )

    # -- a CI-established container -------------------------------------------------------

    def container(self, deployed: DeployedContainer) -> None:
        build = deployed.build
        if deployed.decision != ARTIFACT_LINKED or build is None:
            return  # no proven image: its readers are not known
        builder = build.origin.repo_id
        container = deployed.container
        entries = _list(container.get("env"))
        sources = _list(container.get("env_from"))
        named = {str(e["name"]) for e in entries if isinstance(e.get("name"), str)}
        unnamed = any(not isinstance(e.get("name"), str) or "{{" in str(e["name"]) for e in entries)
        open_env = bool(container.get("env_open")) or unnamed
        variables = {name for name in named if "{{" not in name}
        unreadable = bool(container.get("envFrom_open"))
        for source in sources:
            found = (
                self.configmap(source.get("name"), deployed)
                if source.get("source") == "configMapRef"
                else UNREAD_SOURCE
            )
            if isinstance(found, str):
                unreadable = True
            else:
                variables |= set(found[1])
        read = self.read_in.get(builder, set())
        image = self.image(builder, build.dockerfile, build.context)
        if open_env or unreadable:
            # A source this cannot read may set any variable the image is established to read.
            variables |= {
                variable
                for variable in read
                if any(image.held(r.path or "") is True for r in self.reads[(builder, variable)])
            }
        variables &= read
        if not variables:
            return
        release = deployed.release
        set_place = Place(
            release.origin.repo_id, release.origin.rel_path, release.claim.line_start or 1
        )
        workload = self.by_id[deployed.workload.file_id]
        base: dict[str, object] = {
            key: deployed.evidence[key] for key in _ARTIFACT_KEYS if key in deployed.evidence
        }
        base["deployed_by"] = self.slugs.get(deployed.chart_repo_id, "")
        base["built_by"] = build.origin.repo_slug
        base["artifact_ref"] = deployed.artifact_ref
        name = deployed.release_name or str(deployed.evidence.get("release", ""))
        deployment = f"{name}#{deployed.variant}" if deployed.variant else name
        for variable in sorted(variables):
            outcome = (
                _Refused(CONDITIONAL, _line(container), {"source": "an action may add entries"})
                if open_env
                else self.helm_setter(variable, deployed, set_place)
            )
            at = outcome.at if isinstance(outcome, _Refused) else _entry_line(entries, variable)
            self.decide(
                subject=f"{deployed.artifact_subject}/env:{variable}",
                variable=variable,
                outcome=outcome,
                image=image,
                readers=self.reads[(builder, variable)],
                claim=deployed.workload,
                entry_place=Place(
                    workload.repo_id, workload.rel_path, at or deployed.workload.line_start or 1
                ),
                base=base,
                deployment=deployment,
            )

    # -- a compose service building its own image -----------------------------------------

    def service(self, claim: UnresolvedRefRecord) -> None:
        meta = _meta(claim)
        build = json.loads(str(meta.get("build", "null")))
        environment = json.loads(str(meta.get("environment", "[]")))
        if not isinstance(build, dict) or not isinstance(environment, list) or not environment:
            return
        origin = self.by_id[claim.file_id]
        repo = claim.repo_id
        read = self.read_in.get(repo, set())
        entries = [e for e in environment if isinstance(e, list) and len(e) >= 2]
        if not any(str(e[0]) in read for e in entries):
            return
        directory = posixpath.dirname(origin.rel_path)
        override = None
        if not _OVERRIDE.fullmatch(posixpath.basename(origin.rel_path)):
            override = next(
                (
                    path
                    for path in sorted(self.files.get(repo, {}))
                    if posixpath.dirname(path) == directory
                    and _OVERRIDE.fullmatch(posixpath.basename(path))
                ),
                None,
            )
        image = self.image(repo, build.get("dockerfile"), build.get("context"))
        slug = self.slugs.get(repo, "")
        base: dict[str, object] = {
            "service": claim.target_key,
            "compose_in": f"{origin.rel_path}:{claim.line_start}",
            "deployed_by": slug,
            "built_by": slug,
            "build_basis": "source",
        }
        # One decision per variable: a name listed twice is decided once, at its last entry.
        last: dict[str, list[object]] = {}
        counts = Counter(str(e[0]) for e in entries)
        for entry in entries:
            last[str(entry[0])] = entry
        prefix = f"services.{claim.target_key}.environment"
        for name, entry in last.items():
            line = int(str(entry[1]))
            kind = str(entry[2]) if len(entry) > 2 else "unread"
            if name not in read:
                continue
            outcome: _Setter | _Refused
            if override is not None:
                outcome = _Refused(UNREAD_SOURCE, line, {"override": override})
            elif counts[name] > 1:
                outcome = _Refused(DUPLICATE_ENTRY, line)
            elif kind == PASSTHROUGH:
                outcome = _Refused(PASSTHROUGH, line)
            elif kind not in (LITERAL, SYMBOLIC):
                outcome = _Refused(UNREAD_SOURCE, line)
            else:
                outcome = self.compose_setter(claim, prefix, name, line, kind)
            self.decide(
                subject=f"compose:{slug}:{origin.rel_path}:{claim.target_key}@{claim.line_start}/env:{name}",
                variable=name,
                outcome=outcome,
                image=image,
                readers=self.reads[(repo, name)],
                claim=claim,
                entry_place=Place(repo, origin.rel_path, line),
                base=base,
                deployment=f"compose:{claim.target_key}",
            )

    def compose_setter(
        self, claim: UnresolvedRefRecord, prefix: str, name: str, line: int, kind: str
    ) -> _Setter | _Refused:
        """A compose entry with a value: its own key in the mapping form; in the list form, the
        service's ``environment`` key, which holds the entry's line (list items are no keys)."""
        place = Place(claim.repo_id, self.by_id[claim.file_id].rel_path, line)
        if any(
            s.line_start == line for s in self.keys.get((claim.file_id, f"{prefix}.{name}"), [])
        ):
            return _Setter(COMPOSE_ENVIRONMENT, place, f"{prefix}.{name}", kind)
        block = [
            s
            for s in self.keys.get((claim.file_id, prefix), [])
            if s.line_start is not None and s.line_start < line <= (s.line_end or s.line_start)
        ]
        if len(block) != 1 or block[0].line_start is None:
            return _Refused(NOT_INDEXED, line)
        return _Setter(COMPOSE_ENVIRONMENT, place, prefix, kind, key_line=block[0].line_start)

    # -- the plan -------------------------------------------------------------------------

    def edges(self) -> list[EdgeRecord]:
        found: list[tuple[str, EdgeRecord]] = []
        for (setter, reader), (subject, record, place, deployments) in self.crossings.items():
            if len(deployments) > 1:
                record = {**record, "deployments": sorted(deployments)}
            found.append(
                (
                    f"{subject}\0{record['reader_at']}",
                    EdgeRecord(
                        src_id=setter,
                        dst_id=reader,
                        type=EdgeType.CONFIGURES,
                        repo_id=place.repo_id,
                        file_id=self.files.get(place.repo_id, {}).get(place.path),
                        line_start=place.line,
                        line_end=place.line,
                        parser=self.producer,
                        resolution=json.dumps(
                            {DEPLOYMENT_ENV_EVIDENCE: record}, sort_keys=True, separators=(",", ":")
                        ),
                    ),
                )
            )
        return [edge for _, edge in sorted(found, key=lambda item: item[0])]


def _list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _line(item: Mapping[str, object]) -> int:
    for key in ("line", "image_line"):
        value = item.get(key)
        if isinstance(value, int):
            return value
    return 0


def _entry_line(entries: list[dict[str, object]], variable: str) -> int:
    return next((_line(e) for e in entries if e.get("name") == variable), 0)


def _places(readers: Sequence[EnvReadRow]) -> str:
    return "; ".join(f"{read.path}:{read.line_start}" for read in readers)


def plan_deployment_env(
    claims: Sequence[UnresolvedRefRecord],
    by_id: Mapping[int, FileRow],
    symbols: Sequence[SymbolRow],
    containers: Sequence[DeployedContainer],
    reads: Sequence[EnvReadRow],
    *,
    producer: str,
) -> DeploymentEnvPlan:
    """Every CI-established container and compose service, its configuration crossed to the
    readers its image holds. Writes nothing."""
    planner = _Planner(claims, by_id, symbols, reads, producer)
    for deployed in containers:
        planner.container(deployed)
    for service in planner.services:
        planner.service(service)
    rows = sorted(planner.decisions.rows.values(), key=lambda row: (row.subject, row.decision_ref))
    counts = Counter(row.decision for row in rows)
    edges = planner.edges()
    detail = (
        "deployment env: "
        + ", ".join(f"{decision} {count}" for decision, count in sorted(counts.items()))
        + f"; {len(edges)} crossing(s)"
        if counts
        else "deployment env: none"
    )
    return DeploymentEnvPlan(edges=edges, decisions=rows, detail=detail)
