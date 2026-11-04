"""The R1 golden snapshot: what the HTTP surface is before any R1 product code exists.

R1 reports on routes and changes none of them. The snapshot pins that: for every corpus of
:mod:`tests.r1_provider_corpus` it records, with every row id stripped,

* ``edges``         the HTTP edges (``SERVES_HTTP_ROUTE``, ``CALLS_HTTP_*`` ...), per-file and
                    ``linker:global``;
* ``decisions``     the ``http_call_decisions`` table, evidence and chain included;
* ``operations``    the default ``http_operations`` of every repository, all pages (flag off);
* ``plans``         each repository's plan: ``routes``, ``roots``, ``promoted``, ``declared`` and
                    ``code_routes``;
* ``declarations``  the operators' declarations and their derived state;
* ``previews``      the preview of every (origin group x indexed repository);
* ``worklist``      the worklist's summary.

``python -m tests.r1_golden --write`` writes ``tests/data/r1_golden.json``. It is generated ONCE,
at the code of commit 2da633f, before any R1 product code, and never regenerated: a difference
afterwards is a finding to escalate, not a file to refresh (``tests/test_r1_golden.py``).
Run it from the repository root, under the same isolation as the test suite
(``XDG_CONFIG_HOME``, ``XDG_DATA_HOME``).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph.linker import claim_role, plan_repository_http
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_connections import HttpConnectionPreviewRequest, HttpConnectionsRequest
from repoatlas.models.http_inventory import MAX_PAGE_SIZE, HttpOperationsRequest
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from tests.r1_provider_corpus import CORPUS_NAMES, GLOBAL, Corpora

GOLDEN = Path(__file__).parent / "data" / "r1_golden.json"
BASE_COMMIT = "2da633f"

#: The sections of a corpus's snapshot, in the order they are written.
SECTIONS = (
    "repositories",
    "edges",
    "decisions",
    "operations",
    "plans",
    "declarations",
    "worklist",
    "previews",
)

#: Row identities: allocation order, not fact. A snapshot never holds one.
_IDS = frozenset({"id", "entity_id", "src_id", "dst_id", "file_id", "repo_id"})


def strip_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_ids(v) for k, v in value.items() if k not in _IDS}
    if isinstance(value, list | tuple):
        return [strip_ids(v) for v in value]
    return value


def _decoded(text: str | None) -> Any:
    return strip_ids(json.loads(text)) if text else None


def normalise(value: Any) -> Any:
    """The JSON a snapshot reads back as: tuples are lists, keys are strings."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _ordered(rows: Sequence[Any]) -> list[Any]:
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def _repositories(workspace: Workspace) -> list[tuple[int, str]]:
    return sorted(
        (repo.id, repo.slug)
        for repo in workspace.repositories.list_all()
        if repo.id is not None and repo.slug != DECLARATIONS_SLUG
    )


# -- sections -------------------------------------------------------------------------------


def edges(workspace: Workspace) -> list[Any]:
    """Every HTTP edge, by the keys of its ends, the file that asserted it and who wrote it."""
    rows = workspace.database.execute(
        """
        SELECT e.type, s.key, d.key, f.rel_path, e.line_start, e.line_end, e.parser, e.resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        LEFT JOIN files f ON f.id = e.file_id
        WHERE e.type LIKE '%HTTP%'
        """
    ).fetchall()
    return _ordered(
        [
            {
                "type": row[0],
                "src": row[1],
                "dst": row[2],
                "asserted_in": row[3],
                "line_start": row[4],
                "line_end": row[5],
                "parser": row[6],
                "resolution": _decoded(row[7]),
            }
            for row in rows
        ]
    )


def decisions(workspace: Workspace) -> list[Any]:
    """The ``http_call_decisions`` table: why each request was or was not joined."""
    slugs = dict(_repositories(workspace))
    found = []
    for row in workspace.knowledge.list_http_call_decisions():
        data = row.model_dump(
            exclude={"repo_id", "file_id", "src_id", "evidence_json", "chain_json"}
        )
        data["repository"] = slugs.get(row.repo_id)
        data["evidence"] = _decoded(row.evidence_json)
        data["chain"] = _decoded(row.chain_json)
        found.append(data)
    return sorted(found, key=lambda data: data["decision_ref"])


def operations(workspace: Workspace) -> dict[str, Any]:
    """Each repository's default inventory (flag off): basis, summary and coverage of the first
    page, and the rows of every page, exactly as the wire writes them."""
    service = CapabilityService(workspace)
    found: dict[str, Any] = {}
    for _, slug in _repositories(workspace):
        context = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)
        head: dict[str, Any] | None = None
        rows: list[Any] = []
        cursor: str | None = None
        while True:
            response = service.http_operations(
                HttpOperationsRequest(context=context, limit=MAX_PAGE_SIZE, cursor=cursor)
            )
            data = json.loads(response.model_dump_json())
            if head is None:
                head = {key: data[key] for key in ("basis", "summary", "coverage")}
            rows += data["operations"]
            cursor = response.next_cursor
            if cursor is None:
                break
        assert head is not None
        found[slug] = strip_ids({**head, "operations": rows})
    return found


def _claim_label(claim: UnresolvedRefRecord, files: dict[int, FileRow]) -> str:
    where = files[claim.file_id].rel_path if claim.file_id in files else "?"
    return f"{where}:{claim.line_start}:{claim.line_end}:{claim_role(claim)}:{claim.target_key}"


def _named(table: dict[int, Any], labels: dict[int | None, str]) -> list[Any]:
    """A plan table keyed by claim id, re-keyed by what names the claim."""
    return _ordered(
        [[labels.get(claim_id, f"unknown:{claim_id}"), value] for claim_id, value in table.items()]
    )


def plans(workspace: Workspace) -> dict[str, Any]:
    """Each repository's HTTP plan, as the inventory replans it: claims are named by their file,
    lines, role and target; a handler by its entity key."""
    found: dict[str, Any] = {}
    for repo_id, slug in _repositories(workspace):
        planned = plan_repository_http(workspace.knowledge, repo_id)
        plan = planned.plan
        files = {row.file_id: row for row in planned.files}
        labels = {claim.id: _claim_label(claim, files) for claim in planned.claims}
        code_routes = []
        for route in plan.code_routes:
            handler = workspace.database.execute(
                "SELECT key FROM entities WHERE id = ?", (route.src_id,)
            ).fetchone()
            code_routes.append(
                {
                    "file": files[route.file_id].rel_path if route.file_id in files else None,
                    "handler": handler[0] if handler else None,
                    "body": route.body,
                    "line_start": route.line_start,
                    "line_end": route.line_end,
                    "roots": sorted(files[r].rel_path for r in route.roots if r in files),
                    "declared_at": route.declared_at,
                }
            )
        found[slug] = {
            "detail": plan.detail,
            "resolved": plan.resolved,
            "unresolved": plan.unresolved,
            "routes": _named(plan.routes, labels),
            "roots": _named(plan.roots, labels),
            "promoted": _named({k: list(v) for k, v in plan.promoted.items()}, labels),
            "declared": _named(plan.declared, labels),
            "code_routes": _ordered(code_routes),
        }
    return found


def _worklist(workspace: Workspace) -> Any:
    return CapabilityService(workspace).http_connections(
        HttpConnectionsRequest(context=GLOBAL, include_resolved=True)
    )


def declarations(workspace: Workspace) -> list[Any]:
    """The operators' declarations with their derived state; timestamps are not fact. A
    declaration's own id is kept: it is the order they were stated in, not a row's allocation."""
    return [
        json.loads(view.model_dump_json(exclude={"created_at", "updated_at"}))
        for view in _worklist(workspace).declarations
    ]


def worklist(workspace: Workspace) -> Any:
    summary = _worklist(workspace).summary
    return strip_ids(json.loads(summary.model_dump_json()))


def previews(workspace: Workspace) -> list[Any]:
    """The preview of every stated-origin group against every indexed repository."""
    service = CapabilityService(workspace)
    found = []
    for group in _worklist(workspace).groups:
        if group.kind != "stated-origin":
            continue
        for _, slug in _repositories(workspace):
            try:
                response = service.http_connection_preview(
                    HttpConnectionPreviewRequest(
                        context=GLOBAL, group=group.key, repository=slug, limit=100
                    )
                )
                data = strip_ids(json.loads(response.model_dump_json()))
                data.pop("context", None)
            except InvalidDeploymentLinkError as error:
                data = {"refused": str(error)}
            found.append({"origin": group.origin, "repository": slug, "preview": data})
    return _ordered(found)


def snapshot(workspace: Workspace) -> dict[str, Any]:
    """Everything the golden pins about one corpus, as the JSON it is stored as."""
    return normalise(
        {
            "repositories": sorted(slug for _, slug in _repositories(workspace)),
            "edges": edges(workspace),
            "decisions": decisions(workspace),
            "operations": operations(workspace),
            "plans": plans(workspace),
            "declarations": declarations(workspace),
            "worklist": worklist(workspace),
            "previews": previews(workspace),
        }
    )


def snapshot_all(base: Path) -> dict[str, Any]:
    corpora = Corpora(base)
    try:
        return {name: snapshot(corpora.workspace(name)) for name in CORPUS_NAMES}
    finally:
        corpora.close()


def write() -> Path:
    with tempfile.TemporaryDirectory(prefix="r1-golden-") as scratch:
        corpora = snapshot_all(Path(scratch))
    document = {"base_commit": BASE_COMMIT, "sections": list(SECTIONS), "corpora": corpora}
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return GOLDEN


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"write {GOLDEN} (once, at {BASE_COMMIT}; a written file is never regenerated)",
    )
    arguments = parser.parse_args(argv)
    if not arguments.write:
        parser.error("nothing to do: pass --write to snapshot the current output")
    if GOLDEN.exists():
        print(f"{GOLDEN} exists: the golden is written once. Delete it deliberately to redo.")
        return 1
    print(f"wrote {write()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
