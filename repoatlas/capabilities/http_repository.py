"""One repository's HTTP inventory, read from the index.

:mod:`repoatlas.capabilities.http_inventory` is pure over what it is given and never touches the
index. This module is the reader beside it: it replans one repository with
:func:`~repoatlas.graph.linker.plan_repository_http` and reads the edges, decisions and entities its
rows need, through the workspace. What the service lists, and what a preview of a declaration reads
for the repository it would map, come from here, so the two cannot differ.
"""

from __future__ import annotations

from collections.abc import Mapping

from repoatlas.capabilities import http_inventory
from repoatlas.graph.linker import RepositoryHttp, plan_repository_http
from repoatlas.services.workspace import Workspace


def repository_inventory(
    workspace: Workspace,
    repository_id: int,
    slugs: Mapping[int, str],
    *,
    include_diagnostics: bool = False,
    planned: RepositoryHttp | None = None,
) -> http_inventory.Inventory:
    """Replan one repository (unless ``planned`` is its plan already) and read what its rows
    need. Inside one snapshot. What the service lists, and a preview of a declaration reads for
    the repository it would map, so the two cannot differ."""
    knowledge = workspace.knowledge
    if planned is None:
        planned = plan_repository_http(knowledge, repository_id)
    edges = knowledge.list_http_edges(repository_id)
    decisions = knowledge.list_http_call_decisions(repository_id)
    wanted = {claim.src_id for claim in planned.claims}
    wanted.update(row.src_id for row in decisions)
    wanted.update(http_inventory.handler_ids(decisions))
    return http_inventory.build_inventory(
        plan=planned.plan,
        claims=planned.claims,
        entities=knowledge.keys_for_entities(sorted(wanted)),
        files=planned.files,
        index=planned.index,
        edges=edges,
        parsers=dict(knowledge.list_file_parsers(repository_id)),
        joined=decisions,
        slugs=slugs,
        crossings_built=knowledge.http_call_decisions_built(),
        include_diagnostics=include_diagnostics,
    )
