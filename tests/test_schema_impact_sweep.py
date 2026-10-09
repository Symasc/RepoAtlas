"""The schema-impact sweep: every kind of input changed, and the index equal to a fresh one.

One workspace holds a federated GraphQL service, its consumers and an SDK
(``tests/federation_corpus.py``) beside Prisma-described databases and their code
(``tests/persistence_corpus.py``). Each case changes one input the way a working tree does --
edits it, deletes it, renames it, restores it, breaks it -- syncs only the repository that
changed, and requires the whole index to be what a fresh index of the same trees writes
(ARCHITECTURE.md §9, invariant 2). The rest pin what the global pass must never do: leave
part of a plan behind when its commit fails, change anything on a repeated sync, or depend on
the order rows are read in.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.parsers.prisma import PrismaParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, reindex_from_scratch
from tests.federation_corpus import repos as federation_repos
from tests.impact_corpus import build
from tests.persistence_corpus import repos as persistence_repos


def _repos() -> dict[str, dict[str, str]]:
    return {**federation_repos(), **persistence_repos()}


def _edit(old: str, new: str) -> Callable[[Path], None]:
    def change(path: Path) -> None:
        text = path.read_text()
        assert old in text, old
        path.write_text(text.replace(old, new))

    return change


def _delete(path: Path) -> None:
    path.unlink()


#: (case, repository, file, change), each applied to a freshly built workspace.
CHANGES: list[tuple[str, str, str, Callable[[Path], None]]] = [
    (
        "provider schema loses a value",
        "parcel-api",
        "schema/parcels.graphql",
        _edit("  LOST\n", ""),
    ),
    ("provider schema deleted", "parcel-api", "schema/parcels.graphql", _delete),
    ("schema construction deleted", "parcel-api", "src/server.ts", _delete),
    ("deployment release deleted", "parcel-api", "Tiltfile", _delete),
    (
        "codegen config drops a plugin",
        "parcel-web",
        "codegen.yml",
        _edit("      - typed-document-node\n", ""),
    ),
    ("generated output deleted", "parcel-web", "src/gen/types.ts", _delete),
    (
        "supergraph copy edited",
        "parcel-web",
        "src/graphql/supergraph.graphql",
        _edit("  LOST @join__enumValue(graph: PARCELS)\n", ""),
    ),
    (
        "document renamed",
        "parcel-web",
        "src/queries/parcels.graphql",
        _edit("query SentParcels", "query ShippedParcels"),
    ),
    ("fetch script deleted", "parcel-sdk", "scripts/fetch_schema.py", _delete),
    (
        "ariadne output loses a member",
        "parcel-sdk",
        "parcel_sdk/enums.py",
        _edit('    RETURNED = "RETURNED"\n', ""),
    ),
    (
        "prisma value renamed for the database",
        "depot-api",
        "prisma/schema.prisma",
        _edit('@map("missing")', '@map("gone")'),
    ),
    (
        "migration deleted",
        "depot-api",
        "prisma/migrations/20260201000000_lost/migration.sql",
        _delete,
    ),
    (
        "query built at run time",
        "depot-api",
        "src/queries.ts",
        _edit("where('state_code', 'packed')", "where('state_code', state())"),
    ),
    (
        "mapping loses an entry",
        "depot-api",
        "src/mapping.ts",
        _edit("  [Stage.Lost]: ParcelState.lost,\n", ""),
    ),
    (
        "model field retyped",
        "depot-ops",
        "ops/models.py",
        _edit("Optional[Stage]", "Optional[str]"),
    ),
    ("schema broken", "depot-api", "prisma/schema.prisma", _edit("enum ParcelState {", "enum {")),
]


@pytest.mark.parametrize(
    ("repository", "path", "change"),
    [pytest.param(r, p, c, id=name) for name, r, p, c in CHANGES],
)
def test_a_change_then_its_undoing_leave_what_a_fresh_index_writes(
    workspace: Workspace,
    tmp_path: Path,
    settings: Settings,
    repository: str,
    path: str,
    change: Callable[[Path], None],
) -> None:
    build(workspace, tmp_path, _repos())
    root = tmp_path / "repos"
    target = root / repository / path
    original = target.read_text()
    before = fact_snapshot(workspace)

    change(target)
    sync(workspace, workspace.resolver().resolve(root / repository))
    changed = fact_snapshot(workspace)
    assert changed != before  # the change reached the index: no case is vacuous
    assert changed == reindex_from_scratch(settings, root, tmp_path, "changed")

    target.write_text(original)  # restored, or repaired
    sync(workspace, workspace.resolver().resolve(root / repository))
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path, "restored")


@pytest.mark.parametrize(
    ("repository", "old", "new"),
    [
        ("parcel-web", "src/queries/parcels.graphql", "src/queries/moved.graphql"),
        ("depot-api", "src/mapping.ts", "src/maps.ts"),
        ("parcel-web", "src/graphql/supergraph.graphql", "src/graphql/super.graphql"),
    ],
)
def test_a_renamed_file_leaves_what_a_fresh_index_writes(
    workspace: Workspace, tmp_path: Path, settings: Settings, repository: str, old: str, new: str
) -> None:
    build(workspace, tmp_path, _repos())
    root = tmp_path / "repos"
    (root / repository / old).rename(root / repository / new)
    sync(workspace, workspace.resolver().resolve(root / repository))
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_a_parser_version_change_reparses_into_what_a_fresh_index_writes(
    workspace: Workspace, tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, _repos())
    monkeypatch.setattr(PrismaParser, "version", "1+sweep")
    root = tmp_path / "repos"
    sync(workspace, workspace.resolver().resolve(root))
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path)


def test_a_repeated_sync_changes_nothing(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, _repos())
    root = tmp_path / "repos"
    before = fact_snapshot(workspace)
    sync(workspace, workspace.resolver().resolve(root))
    sync(workspace, workspace.resolver().resolve(root))
    assert fact_snapshot(workspace) == before


def test_a_failed_commit_leaves_the_previous_index_whole(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, _repos())
    before = fact_snapshot(workspace)
    root = tmp_path / "repos"
    target = root / "depot-api" / "src/mapping.ts"
    target.write_text(target.read_text().replace("  [Stage.Lost]: ParcelState.lost,\n", ""))
    linker = ReferenceLinker(workspace.knowledge)
    plan = linker.prepare_global()

    def broken(rows: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(workspace.knowledge, "replace_link_decisions", broken)
    with pytest.raises(RuntimeError, match="injected"):
        linker.commit_global(plan)
    assert fact_snapshot(workspace) == before


def test_the_global_plan_does_not_depend_on_the_order_rows_are_read(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, _repos())
    knowledge = workspace.knowledge
    first = ReferenceLinker(knowledge).prepare_global()
    shuffler = random.Random(13)  # noqa: S311 - an order to try, not a secret
    for name in ("list_unresolved_refs", "list_symbols", "list_file_rows"):
        original = getattr(knowledge, name)

        def shuffled(*args: object, _original=original, **kwargs: object) -> list[object]:
            rows = list(_original(*args, **kwargs))
            shuffler.shuffle(rows)
            return rows

        monkeypatch.setattr(knowledge, name, shuffled)
    second = ReferenceLinker(knowledge).prepare_global()
    assert sorted(second.decisions, key=repr) == sorted(first.decisions, key=repr)
    assert sorted(second.edges, key=repr) == sorted(first.edges, key=repr)
