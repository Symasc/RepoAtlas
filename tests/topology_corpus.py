"""Workspaces and oracles for the topology capability tests.

The linked corpus is the cross-repository one impact is tested on: package
crossings (``USES`` with recorded evidence), manifest dependencies the linker
resolved (``DEPENDS_ON``), an ambiguous publisher, and one HTTP caller-to-handler
pair. Every oracle here reads the index directly, with its own statement of
which edge types are which kind, so a change to the capability's classification
cannot also change what it is checked against.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import HTTP_API, HTTP_WEB, build
from tests.xrepo_corpus import LINKING

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

LINKED: dict[str, dict[str, str]] = {**LINKING, "http-api": HTTP_API, "http-web": HTTP_WEB}

#: The oracle's own classification, written out rather than imported.
HTTP_TYPES = frozenset(
    {
        "SERVES_HTTP_ROUTE",
        "CALLS_HTTP_ROUTE",
        "DECLARES_HTTP_DEPLOYMENT",
        "EXPOSES_HTTP_ENDPOINT",
        "CALLS_HTTP_ENDPOINT",
        "CALLS_HTTP_HANDLER",
    }
)
DEPENDENCY_TYPES = frozenset({"DEPENDS_ON"})
STRUCTURAL_TYPES = ("CONTAINS", "DEFINES")


def kind_of(edge_type: str) -> str:
    if edge_type in HTTP_TYPES:
        return "http"
    if edge_type in DEPENDENCY_TYPES:
        return "dependency"
    return "observed"


def repository(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def open_corpus(root: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    return build(workspace, root, repos)


def cross_repository_counts(workspace: Workspace) -> dict[tuple[str, str, str], int]:
    """Stored non-structural edges joining two repositories, by (kind, source, target)."""
    rows = workspace.database.execute(
        """
        SELECT e.type AS type, rs.slug AS source, rd.slug AS target, COUNT(*) AS n
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN repositories rs ON rs.id = s.repo_id
        JOIN repositories rd ON rd.id = d.repo_id
        WHERE s.repo_id <> d.repo_id AND e.type NOT IN (?, ?)
        GROUP BY e.type, rs.slug, rd.slug
        """,
        STRUCTURAL_TYPES,
    ).fetchall()
    counts: dict[tuple[str, str, str], int] = {}
    for row in rows:
        key = (kind_of(row["type"]), row["source"], row["target"])
        counts[key] = counts.get(key, 0) + int(row["n"])
    return counts


@contextmanager
def statements(workspace: Workspace) -> Iterator[list[str]]:
    """Every SQL statement the workspace's connection runs inside the block."""
    seen: list[str] = []
    connection = workspace.database.connection
    connection.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        connection.set_trace_callback(None)


def main(argv: list[str] | None = None) -> None:
    """Build the linked corpus for the graph explorer's end-to-end run.

    ``python -m tests.topology_corpus <dir>`` indexes it into ``<dir>``, which
    must be absent or empty, leaving ``config.yaml`` and ``state/atlas.db`` there
    for ``repoatlas serve`` (via REPOATLAS_CONFIG and REPOATLAS_DB). It is the
    one index whose repositories link to each other, which a live index may not.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m tests.topology_corpus <empty directory>")
    root = Path(args[0])
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    open_corpus(root, LINKED).close()


if __name__ == "__main__":
    main()
