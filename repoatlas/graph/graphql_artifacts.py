"""Schema copies fetched from elsewhere: a decision for each fetch, never an ownership link.

A ``graphql-artifact`` claim (:mod:`repoatlas.parsers.graphql_artifacts`) says that a
command or script writes a schema file from a remote source. Each is decided
``artifact-remote`` (``link_decisions``, family ``graphql_artifacts``): its subject
``<repository>:package.json::<script>`` or ``<repository>:<module>``, its evidence the tool,
the redacted URL and the file it writes, resolved against the admitted files when it is
one. The file it writes is reported to :mod:`repoatlas.graph.graphql_ownership`, whose
decisions for that copy cite the fetch. A fetch never ties a coordinate to a provider.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.globs import resolve_file
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.storage.protocols import FileRow, LinkDecisionRow, UnresolvedRefRecord

FAMILY = "graphql_artifacts"
ARTIFACT_REMOTE = "artifact-remote"


@dataclass(frozen=True, slots=True)
class ArtifactsPlan:
    decisions: tuple[LinkDecisionRow, ...] = ()
    #: Admitted file -> each fetch that writes it, as ``<repository>:<where stated>``.
    fetched: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    detail: str = ""


def plan_artifacts(
    claims: Sequence[UnresolvedRefRecord], index: ResolutionIndex, files: Mapping[int, FileRow]
) -> ArtifactsPlan:
    """Decide every schema fetch. Writes nothing."""
    decisions: dict[str, LinkDecisionRow] = {}
    fetched: dict[int, set[str]] = defaultdict(set)
    for claim in claims:
        origin = files.get(claim.file_id)
        if origin is None:
            continue
        meta = json.loads(claim.meta_json or "{}")
        script = meta.get("script")
        subject = (
            f"{origin.repo_slug}:{origin.rel_path}::{script}"
            if script
            else f"{origin.repo_slug}:{origin.rel_path}"
        )
        output = meta.get("output")
        written = resolve_file(index, origin, str(output)) if output else None
        evidence = {key: meta[key] for key in ("tool", "url", "output") if meta.get(key)}
        if written is not None:
            evidence["writes"] = f"{written.repo_slug}/{written.rel_path}"
            fetched[written.file_id].add(subject)
        ref = hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]
        decisions[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=FAMILY,
            repo_id=origin.repo_id,
            file_id=origin.file_id,
            src_id=claim.src_id,
            line_start=claim.line_start,
            line_end=claim.line_end,
            subject=subject,
            decision=ARTIFACT_REMOTE,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )
    ordered = sorted(decisions.values(), key=lambda row: row.subject)
    return ArtifactsPlan(
        decisions=tuple(ordered),
        fetched={file_id: tuple(sorted(found)) for file_id, found in sorted(fetched.items())},
        detail=f"artifacts: {len(ordered)} fetched",
    )
