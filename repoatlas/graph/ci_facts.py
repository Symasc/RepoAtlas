"""Deciding what CI configurations configure: builds, releases, steps and declarations (DF1).

The global pass's reading of the claims :mod:`repoatlas.parsers.ci_config` records, as
stored link decisions in four families (DF1_PHASE_A.md §10). Nothing here makes an edge;
DF2 joins what is decided here. Writes nothing.

* ``ci_build`` -- one per image reference a job builds: ``publish-configured``,
  ``built-not-published`` or ``publication-unresolved`` for a literal image, else
  ``image-unresolved`` or ``registry-unresolved``. *Configured* publishing is never a claim
  that an image was pushed or exists anywhere.
* ``ci_release`` -- one per *established* release, a helm invocation or a declared orb job:
  ``bound``; ``chart-external`` when the chart is not a literal chart directory of the same
  repository; ``values-unresolved`` when a values file is not a literal, repository-local,
  indexed path. Each values file is recorded as ``referenced`` (named by ``-f``) or
  ``discovered`` (a ``values*.y(a)ml`` the index read).
* ``ci_step`` -- one per invocation not interpreted: ``unsupported-action`` (an action or
  orb job RepoAtlas gives no semantics to, a reusable workflow call), ``delegated-to-script``,
  ``control-flow-unsupported``, ``dynamic-config``, ``caller-dependent``.
* ``ci_semantics`` -- one per operator declaration: ``accepted`` (well-formed, unique and
  used), ``unused``, ``conflict`` (both ignored), ``invalid``, ``outside-declarations`` (a
  declaration file in an ordinary repository, ignored).

A declaration counts only from the reserved ``@declarations`` source
(:data:`~repoatlas.config.settings.DECLARATIONS_SLUG`), and an orb job only matches one
naming its exact ``ns/name@version`` and job: resolving an alias names the orb, and proves
nothing about what it does.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.models.container_image import EXPR, TEXT, VAR, parse_reference, text_of
from repoatlas.parsers.ci_config import (
    CI_HELM_RELEASE,
    CI_IMAGE_BUILD,
    CI_SEMANTICS,
    CI_STEP_UNSUPPORTED,
    set_pairs,
)
from repoatlas.parsers.http_facts import HELM_CHART, HELM_VALUES, HELM_VALUES_FILE, PENDING_FACT
from repoatlas.storage.protocols import FileRow, LinkDecisionRow, UnresolvedRefRecord

CI_BUILD = "ci_build"
CI_RELEASE = "ci_release"
CI_STEP = "ci_step"
CI_SEMANTICS_FAMILY = "ci_semantics"

#: Each recorded step kind, and the ``ci_step`` decision it is (``None``: recorded only).
_STEP_DECISIONS: dict[str, str | None] = {
    "action": "unsupported-action",
    "docker-action": "unsupported-action",
    "local-action": "unsupported-action",
    "reusable-workflow": "unsupported-action",
    "orb-job": "unsupported-action",
    "orb-command": "unsupported-action",
    "script": "delegated-to-script",
    "control-flow": "control-flow-unsupported",
    "dynamic-config": "dynamic-config",
    "caller-dependent": "caller-dependent",
    "kubectl-apply": None,
}


@dataclass(frozen=True, slots=True)
class CiFactsPlan:
    decisions: tuple[LinkDecisionRow, ...]
    detail: str


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    return json.loads(claim.meta_json or "{}")


def _site(meta: Mapping[str, str]) -> str:
    system, workflow, job = meta.get("system"), meta.get("workflow"), meta.get("job")
    return f"{system}/{workflow}/{job}#{meta.get('invocation')}/{meta.get('step')}"


def _site_evidence(meta: Mapping[str, str]) -> dict[str, object]:
    found: dict[str, object] = {"step_line": int(meta.get("step_line", "0") or 0)}
    for key in ("domains", "requires"):
        if meta.get(key):
            found[key] = json.loads(meta[key])
    if meta.get("conditional") == "true":
        found["conditional"] = True
    if meta.get("truncated") == "true":
        found["truncated"] = True
    return found


class _Decisions:
    def __init__(self) -> None:
        self.rows: dict[str, LinkDecisionRow] = {}

    def add(
        self,
        family: str,
        origin: FileRow,
        claim: UnresolvedRefRecord,
        subject: str,
        decision: str,
        evidence: Mapping[str, object],
    ) -> None:
        ref = hashlib.sha256(f"{family}\0{subject}".encode()).hexdigest()[:24]
        self.rows[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=family,
            repo_id=origin.repo_id,
            file_id=origin.file_id,
            src_id=claim.src_id,
            line_start=claim.line_start,
            line_end=claim.line_end,
            subject=subject,
            decision=decision,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )


@dataclass
class _Declaration:
    claim: UnresolvedRefRecord
    origin: FileRow
    meta: dict[str, str]
    used: bool = False


def plan_ci_facts(
    claims: Sequence[UnresolvedRefRecord], files: Mapping[int, FileRow]
) -> CiFactsPlan:
    """Decide every CI claim. Writes nothing."""
    by_role: dict[str, list[tuple[UnresolvedRefRecord, FileRow, dict[str, str]]]] = defaultdict(
        list
    )
    charts: dict[int, set[str]] = defaultdict(set)
    discovered: dict[int, set[str]] = defaultdict(set)
    paths: dict[int, set[str]] = defaultdict(set)
    for row in files.values():
        paths[row.repo_id].add(row.rel_path)
    for claim in claims:
        origin = files.get(claim.file_id)
        if origin is None:
            continue
        meta = _meta(claim)
        role = meta.get(PENDING_FACT, "")
        if role == HELM_CHART:
            charts[claim.repo_id].add(meta.get("chart", ""))
        elif role in (HELM_VALUES, HELM_VALUES_FILE):
            discovered[claim.repo_id].add(origin.rel_path)
        elif role in (CI_IMAGE_BUILD, CI_HELM_RELEASE, CI_STEP_UNSUPPORTED, CI_SEMANTICS):
            by_role[role].append((claim, origin, meta))

    decisions = _Decisions()
    orbs, projects = _declarations(by_role[CI_SEMANTICS], decisions)
    context = _Context(charts, discovered, paths, decisions, projects)

    for claim, origin, meta in by_role[CI_IMAGE_BUILD]:
        _builds(claim, origin, meta, decisions, basis=meta.get("basis", "source"))
    for claim, origin, meta in by_role[CI_HELM_RELEASE]:
        context.release(claim, origin, meta, basis="source")
    for claim, origin, meta in by_role[CI_STEP_UNSUPPORTED]:
        kind = meta.get("kind", "")
        if kind == "orb-job":
            declared = orbs.get((meta.get("action", ""), meta.get("orb_job", "")))
            if declared is not None:
                declared.used = True
                context.declared(claim, origin, meta, declared)
                continue
        decision = _STEP_DECISIONS.get(kind)
        if decision is None:
            continue
        evidence = {"kind": kind, **_site_evidence(meta)}
        if meta.get("action"):
            evidence["action"] = meta["action"]
        if kind in ("orb-job", "orb-command"):
            evidence["orb_job"] = meta.get("orb_job", "")
        if kind == "script":
            evidence["script"] = claim.target_key
        subject = f"{origin.repo_slug}:{origin.rel_path}::{_site(meta)}@{claim.line_start}"
        decisions.add(CI_STEP, origin, claim, subject, decision, evidence)

    for entry in [*orbs.values(), *projects.values()]:
        place = f"{entry.origin.repo_slug}:{entry.origin.rel_path}"
        subject = f"{place}::{entry.meta.get('kind')}:{entry.claim.target_key}"
        decisions.add(
            CI_SEMANTICS_FAMILY,
            entry.origin,
            entry.claim,
            subject,
            "accepted" if entry.used else "unused",
            {"kind": entry.meta.get("kind", "")},
        )
    ordered = tuple(
        sorted(decisions.rows.values(), key=lambda row: (row.family, row.subject, row.decision_ref))
    )
    counts: dict[str, int] = defaultdict(int)
    for row in ordered:
        counts[row.family] += 1
    detail = (
        "ci: " + ", ".join(f"{family} {count}" for family, count in sorted(counts.items()))
        if counts
        else "ci: none"
    )
    return CiFactsPlan(decisions=ordered, detail=detail)


def _builds(
    claim: UnresolvedRefRecord,
    origin: FileRow,
    meta: Mapping[str, str],
    decisions: _Decisions,
    *,
    basis: str,
    declared_in: str | None = None,
) -> None:
    for index, ref in enumerate(json.loads(meta.get("refs", "[]"))):
        evidence: dict[str, object] = {
            "basis": basis,
            "image": ref.get("image", ""),
            "key": ref.get("key"),
            "version_unresolved": bool(ref.get("version_unresolved")),
            **_site_evidence(meta),
        }
        if ref.get("tag"):
            evidence["tag"] = ref["tag"]
        if ref.get("digest"):
            evidence["digest"] = ref["digest"]
        if ref.get("registry_implicit"):
            evidence["registry_implicit"] = True
        if meta.get("context"):
            evidence["context"] = meta["context"]
        if meta.get("dockerfile"):
            evidence["dockerfile"] = meta["dockerfile"]
        if declared_in:
            evidence["declared_in"] = declared_in
        subject = f"{origin.repo_slug}:{origin.rel_path}::{_site(meta)}@{claim.line_start}/{index}"
        decisions.add(
            CI_BUILD, origin, claim, subject, str(ref.get("decision", "image-unresolved")), evidence
        )


class _Context:
    def __init__(
        self,
        charts: Mapping[int, set[str]],
        discovered: Mapping[int, set[str]],
        paths: Mapping[int, set[str]],
        decisions: _Decisions,
        projects: Mapping[str, _Declaration],
    ) -> None:
        self.charts = charts
        self.discovered = discovered
        self.paths = paths
        self.decisions = decisions
        self.projects = projects

    def release(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        meta: Mapping[str, str],
        *,
        basis: str,
        declared_in: str | None = None,
    ) -> None:
        chart = meta.get("chart", "")
        values = json.loads(meta.get("values", "[]"))
        listed: list[list[object]] = []
        unresolved = False
        for path, shown in values:
            if path is None or path not in self.paths[origin.repo_id]:
                unresolved = True
                listed.append([None, shown, "unresolved"])
            else:
                how = "discovered" if path in self.discovered[origin.repo_id] else "referenced"
                listed.append([path, shown, how])
        release = json.loads(meta.get("release", "[]"))
        evidence: dict[str, object] = {
            "basis": basis,
            "release": text_of(release),
            "release_resolved": all(kind == TEXT for kind, _ in release),
            "chart": chart or meta.get("chart_text", ""),
            "values": [[path if path is not None else shown, how] for path, shown, how in listed],
            "set": [
                [path, text_of(parts) if path is not None else "<unread>"]
                for path, parts in json.loads(meta.get("set", "[]"))
            ],
            **_site_evidence(meta),
        }
        namespace = json.loads(meta.get("namespace", "null"))
        if namespace is not None:
            evidence["namespace"] = text_of(namespace)
        if meta.get("set_unread") == "true":
            evidence["set_unread"] = True
        if declared_in:
            evidence["declared_in"] = declared_in
        if not chart or chart not in self.charts[origin.repo_id]:
            decision = "chart-external"
        elif unresolved:
            decision = "values-unresolved"
        else:
            decision = "bound"
        subject = f"{origin.repo_slug}:{origin.rel_path}::{_site(meta)}@{claim.line_start}"
        self.decisions.add(CI_RELEASE, origin, claim, subject, decision, evidence)

    def declared(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        meta: Mapping[str, str],
        declaration: _Declaration,
    ) -> None:
        """An orb job an accepted declaration gives meaning to: a release or a build."""
        mapping = json.loads(declaration.meta.get("mapping", "{}"))
        parameters = json.loads(meta.get("parameters", "{}"))
        source = declaration.origin
        declared_in = f"{source.repo_slug}/{source.rel_path}:{declaration.claim.line_start}"
        project = self.projects.get(origin.repo_slug)

        def parameter(field: str) -> list[list[str]] | None:
            spec = mapping.get(field)
            if not isinstance(spec, dict):
                return None
            value = parameters.get(spec.get("parameter"))
            if value is None:
                return None
            if project is not None:
                resolved: list[list[str]] = []
                for kind, text in value:
                    if kind == VAR and text == "CIRCLE_PROJECT_REPONAME":
                        project.used = True
                        resolved.append([TEXT, project.meta.get("project", "")])
                    else:
                        resolved.append([kind, text])
                value = resolved
            return value

        site = dict(meta)
        if mapping.get("kind") == "image-build":
            image = parameter("image") or [[EXPR, ""]]
            reference = parse_reference(image)
            publish = mapping.get("publish") == "true"
            decision = (
                reference.state
                if reference.state != "resolved"
                else ("publish-configured" if publish else "built-not-published")
            )
            site["refs"] = json.dumps(
                [
                    {
                        "image": text_of(image),
                        "key": reference.join_key(),
                        "tag": reference.tag,
                        "digest": reference.digest,
                        "registry_implicit": reference.registry_implicit,
                        "version_unresolved": reference.version_unresolved,
                        "decision": decision,
                    }
                ]
            )
            _builds(claim, origin, site, self.decisions, basis="declared", declared_in=declared_in)
            return
        chart = parameter("chart")
        chart_text = (
            "".join(text for kind, text in chart)
            if chart and all(k == TEXT for k, _ in chart)
            else None
        )
        site["chart"] = (
            posixpath.normpath(chart_text)
            if chart_text and not chart_text.startswith(("/", ".."))
            else ""
        )
        site["chart_text"] = text_of(chart) if chart else ""
        site["release"] = json.dumps(parameter("release") or [[EXPR, ""]])
        namespace = parameter("namespace")
        site["namespace"] = json.dumps(namespace)
        sets: list[list[object]] = []
        overrides = parameter("set")
        if overrides is not None:
            # One reading for `--set` and a declared override parameter.
            sets = set_pairs([overrides])
        site["set"] = json.dumps(sets)
        values = parameter("values")
        site["values"] = json.dumps(
            []
            if values is None
            else [
                [
                    "".join(t for _, t in values) if all(k == TEXT for k, _ in values) else None,
                    text_of(values),
                ]
            ]
        )
        self.release(claim, origin, site, basis="declared", declared_in=declared_in)


def _declarations(
    entries: Sequence[tuple[UnresolvedRefRecord, FileRow, dict[str, str]]], decisions: _Decisions
) -> tuple[dict[tuple[str, str], _Declaration], dict[str, _Declaration]]:
    """The accepted declarations, by orb and job and by project slug; every other one is
    decided here (invalid, conflict, outside-declarations)."""
    candidates: dict[tuple[str, str], list[_Declaration]] = defaultdict(list)
    for claim, origin, meta in entries:
        entry = _Declaration(claim, origin, meta)
        subject = f"{origin.repo_slug}:{origin.rel_path}::{meta.get('kind')}:{claim.target_key}"
        if origin.repo_slug != DECLARATIONS_SLUG:
            decisions.add(
                CI_SEMANTICS_FAMILY,
                origin,
                claim,
                subject,
                "outside-declarations",
                {"kind": meta.get("kind", "")},
            )
            continue
        if meta.get("invalid"):
            decisions.add(
                CI_SEMANTICS_FAMILY,
                origin,
                claim,
                subject,
                "invalid",
                {"kind": meta.get("kind", ""), "reason": meta["invalid"]},
            )
            continue
        key = (meta.get("kind", ""), claim.target_key)
        candidates[key].append(entry)
    orbs: dict[tuple[str, str], _Declaration] = {}
    projects: dict[str, _Declaration] = {}
    for (kind, target), listed in candidates.items():
        if len(listed) > 1:
            for entry in listed:
                place = f"{entry.origin.repo_slug}:{entry.origin.rel_path}"
                subject = f"{place}::{kind}:{target}@{entry.claim.line_start}"
                decisions.add(
                    CI_SEMANTICS_FAMILY,
                    entry.origin,
                    entry.claim,
                    subject,
                    "conflict",
                    {"kind": kind},
                )
            continue
        entry = listed[0]
        if kind == "orb":
            orbs[(entry.meta.get("orb", ""), entry.meta.get("orb_job", ""))] = entry
        else:
            projects[target] = entry
    return orbs, projects
