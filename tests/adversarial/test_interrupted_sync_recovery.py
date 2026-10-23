"""Crash consistency of derived facts (ARCHITECTURE.md §9, V1_SPEC §12).

The defect this suite exists for, stated as the final release review found it:

> A sync killed with ``SIGTERM`` or ``SIGKILL`` could leave a file row carrying the
> file's **current** content hash and facts version, with its entities and chunks
> written but its edges, standing claims and dependency rows missing. Every later
> sync compared content hashes, found them equal, and skipped the file — so the
> loss was permanent. ``sync --full`` did not repair it either, because ``--full``
> forces a re-*hash* and the bytes really had not changed. ``status`` then reported
> ``Index state: current``.

The cause was that one file's durable consequences were spread over four
transactions — the file row, ``replace_file_facts``, ``link_file``, and the
dependency write — with the *first* of them being the one that made the file
skippable. Any death between them left a row claiming an indexing that had not
finished.

The property asserted throughout is therefore not "the process shuts down
tidily", which no process can promise against ``SIGKILL``, but:

> A file row exists only if every fact derived from that exact content was
> committed with it. The commit **is** the completeness marker.

which makes recovery a consequence of ordinary planning rather than a special
path: a file that was mid-flight has no row (or still has its previous row), so
the next ordinary ``sync`` sees new-or-changed content and reprocesses it.

Two kinds of test enforce it. Deterministic **cut points** plant a failure at each
persistent boundary and assert what a *separate connection* can observe. Real
**subprocess** trials kill an actual CLI sync with real signals, because a
monkeypatched exception cannot prove anything about ``SIGKILL``.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from repoatlas.config.settings import ReposSettings, Settings
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.repository import IndexedFile, SyncStatus
from repoatlas.services.init_service import initialize
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import EdgeRecord, FileFacts
from repoatlas.storage.sqlite.stores import (
    SqliteDependencyStore,
    SqliteFileStore,
    SqliteKnowledgeStore,
)
from repoatlas.sync.derive import WORKERS_ENV
from repoatlas.sync.engine import GROUP_MAX_FILES
from tests.conftest import git, init_git_repository

pytestmark = pytest.mark.adversarial

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Edges the *run* owns rather than any single file. They are deleted and rebuilt
#: wholesale by the global pass at the end of a run, so a crash mid-run legitimately
#: leaves them absent — per-file completeness is not a claim about them.
GLOBAL_PARSER = "linker:global"


# -- fixture -----------------------------------------------------------------


def _module(index: int, neighbour: int) -> str:
    """A file that yields every per-file fact kind at once.

    ``DEFINES`` (the module and the function), ``IMPORTS`` (interned ``os`` plus a
    sibling that only the global pass can resolve), ``READS`` an environment
    variable, two standing claims, and more than one chunk.
    """
    return (
        f"import os\n"
        f"import m{neighbour}\n"
        f'V{index} = os.environ["E{index}"]\n'
        f"\n"
        f"\n"
        f"def f{index}():\n"
        f'    """Docstring kept long enough to matter for chunking."""\n'
        f"    return V{index}\n"
    )


def _build_tree(root: Path, count: int) -> Path:
    """A managed root with one repository whose files produce facts of every kind."""
    repo = root / "svc"
    repo.mkdir(parents=True)
    for index in range(1, count + 1):
        neighbour = (index % count) + 1
        (repo / f"m{index}.py").write_text(_module(index, neighbour))
    # A manifest, so dependency rows are part of the state under test.
    (repo / "package.json").write_text(
        '{"name":"svc","version":"1.0.0","dependencies":{"express":"^4.0.0"}}\n'
    )
    # The flagship configuration chain: shell sets it, Python reads it.
    (repo / "run.sh").write_text("#!/bin/bash\nexport SERVICE_HOST=prod\npython m1.py\n")
    (repo / "config.py").write_text('import os\nHOST = os.environ["SERVICE_HOST"]\n')
    init_git_repository(repo)
    return repo


@pytest.fixture
def crash_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    _build_tree(root, 12)
    return root


def _settings(tmp_path: Path, tag: str) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
        }
    )


def _index(settings: Settings, root: Path, tmp_path: Path, tag: str) -> None:
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / f"{tag}-config.yaml")
    finally:
        workspace.close()


def _resync(settings: Settings, root: Path, *, full: bool = False) -> None:
    """Reopen the workspace at ``root`` and run the recovery the user would run."""
    workspace = open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )
    try:
        context = workspace.resolver().resolve(root, command="sync")
        sync(workspace, context, full=full)
    finally:
        workspace.close()


# -- observation -------------------------------------------------------------


def _open_at(settings: Settings, root: Path) -> Workspace:
    """Reopen an existing index with ``root`` configured, as a later CLI run would."""
    return open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )


def _connect(settings: Settings) -> sqlite3.Connection:
    """A separate connection. What another process would see."""
    database = settings.storage.database
    assert database is not None, "every settings object in this suite names a database"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def _per_file_facts(connection: sqlite3.Connection, rel_path: str) -> dict[str, object]:
    """Everything one file asserts, excluding what the *run* owns.

    ``linker:global`` edges are excluded deliberately: they belong to the global
    swap at the end of a run, not to the file, so their absence after a mid-run
    crash is correct rather than a partial file.
    """
    return {
        "entities": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT e.type, e.key, e.name, e.qualified_name, e.line_start, "
                "e.line_end, e.signature FROM entities e "
                "JOIN files f ON f.id = e.file_id WHERE f.rel_path = ?",
                (rel_path,),
            )
        ),
        "edges": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT g.type, src.key, dst.key, g.confidence, g.line_start, "
                "g.line_end, g.parser FROM edges g "
                "JOIN entities src ON src.id = g.src_id "
                "JOIN entities dst ON dst.id = g.dst_id "
                "JOIN files f ON f.id = g.file_id "
                "WHERE f.rel_path = ? AND g.parser <> ?",
                (rel_path, GLOBAL_PARSER),
            )
        ),
        "claims": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT u.edge_type, u.target_kind, u.target_key, u.line_start, "
                "u.line_end, u.confidence FROM unresolved_refs u "
                "JOIN files f ON f.id = u.file_id WHERE f.rel_path = ?",
                (rel_path,),
            )
        ),
        "chunks": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT k.line_start, k.line_end, k.text, k.span_index, "
                "k.split_index, k.split_total FROM chunks k "
                "JOIN files f ON f.id = k.file_id WHERE f.rel_path = ?",
                (rel_path,),
            )
        ),
        "dependencies": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT d.ecosystem, d.normalized_name, d.role, d.constraint_text, "
                "d.version, d.line_start FROM dependencies d "
                "JOIN files f ON f.id = d.file_id WHERE f.rel_path = ?",
                (rel_path,),
            )
        ),
    }


def _rows(connection: sqlite3.Connection, rel_path: str) -> list[tuple[str, str]]:
    return [
        (row["rel_path"], row["content_hash"])
        for row in connection.execute(
            "SELECT rel_path, content_hash FROM files WHERE rel_path = ?", (rel_path,)
        )
    ]


def assert_no_partial_file(observed: Settings, reference: Settings) -> None:
    """Every file row that survived a crash carries complete facts for its content.

    This is the invariant in its checkable form. A file may be absent, and it may
    still hold its *previous* content and facts — both are coherent. What it may
    not be is present at the new content hash with anything derived from that
    content missing, because that is the state a later sync would skip forever.
    """
    crashed = _connect(observed)
    clean = _connect(reference)
    try:
        clean_files = {
            row["rel_path"]: row["content_hash"]
            for row in clean.execute("SELECT rel_path, content_hash FROM files")
        }
        surviving = list(
            crashed.execute("SELECT rel_path, content_hash FROM files ORDER BY rel_path")
        )
        assert surviving, "the crash left no file rows at all, so nothing was proven"
        for row in surviving:
            rel_path = row["rel_path"]
            if clean_files.get(rel_path) != row["content_hash"]:
                # A row at some other content version is not this content's claim.
                continue
            assert _per_file_facts(crashed, rel_path) == _per_file_facts(clean, rel_path), (
                f"{rel_path} is recorded at its current content hash but its derived "
                f"facts are not the ones that content produces"
            )
    finally:
        crashed.close()
        clean.close()


def logical_snapshot(settings: Settings) -> dict[str, object]:
    """Id-independent snapshot covering every derived representation.

    Wider than ``tests.conftest.fact_snapshot``: it also compares the FTS content,
    because a recovery that rebuilt the rows but not the search index would answer
    questions differently while looking equivalent.
    """
    connection = _connect(settings)
    try:
        out: dict[str, object] = {
            "repositories": sorted(
                tuple(r)
                for r in connection.execute("SELECT name, slug, last_sync_status FROM repositories")
            ),
            "files": sorted(
                tuple(r)
                for r in connection.execute(
                    "SELECT rel_path, content_hash, language, parser, parser_version, "
                    "is_binary, size FROM files"
                )
            ),
            "entities": sorted(
                tuple(r)
                for r in connection.execute(
                    "SELECT type, key, name, qualified_name, line_start, line_end, "
                    "signature, meta_json FROM entities"
                )
            ),
            "edges": sorted(
                tuple(r)
                for r in connection.execute(
                    "SELECT g.type, src.key, dst.key, g.confidence, g.line_start, "
                    "g.line_end, g.parser, COALESCE(f.rel_path, '') FROM edges g "
                    "JOIN entities src ON src.id = g.src_id "
                    "JOIN entities dst ON dst.id = g.dst_id "
                    "LEFT JOIN files f ON f.id = g.file_id"
                )
            ),
            "unresolved_refs": sorted(
                tuple(r)
                for r in connection.execute(
                    "SELECT COALESCE(f.rel_path, ''), u.edge_type, u.target_kind, "
                    "u.target_key, u.line_start, u.line_end, u.confidence "
                    "FROM unresolved_refs u LEFT JOIN files f ON f.id = u.file_id"
                )
            ),
            "chunks": sorted(
                tuple(r)
                for r in connection.execute(
                    "SELECT f.rel_path, k.line_start, k.line_end, k.text, "
                    "k.span_index, k.split_index, k.split_total "
                    "FROM chunks k JOIN files f ON f.id = k.file_id"
                )
            ),
            "dependencies": sorted(
                tuple(r)
                for r in connection.execute(
                    "SELECT f.rel_path, d.ecosystem, d.normalized_name, d.role, "
                    "d.constraint_text, d.version, d.group_name, d.line_start "
                    "FROM dependencies d JOIN files f ON f.id = d.file_id"
                )
            ),
        }
        for table in ("chunks_fts", "entities_fts"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            # Both the table and the column names come from the schema itself, so
            # there is no caller-supplied text anywhere in this statement.
            query = f"SELECT {','.join(columns)} FROM {table}"  # noqa: S608
            out[table] = sorted(tuple(r) for r in connection.execute(query))
        return out
    finally:
        connection.close()


def assert_converged(recovered: Settings, clean: Settings) -> None:
    """Exact logical equality, reported per table so a failure names what diverged."""
    a = logical_snapshot(recovered)
    b = logical_snapshot(clean)
    differing = [key for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)]
    assert not differing, f"recovered index differs from a clean rebuild in: {differing}"


# -- deterministic cut points ------------------------------------------------


class Injected(RuntimeError):
    """A failure planted at a named persistent boundary."""


def _plant(monkeypatch: pytest.MonkeyPatch, point: str, target: str) -> None:
    """Fail at ``point`` while the file ``target`` is being indexed.

    The points are the real boundaries of one file's write lifecycle, in order.
    Before the fix each of them left durable state behind; after it, every one of
    them must roll back to a coherent index.
    """
    original_upsert = SqliteFileStore.upsert
    original_replace = SqliteKnowledgeStore.replace_file_facts
    original_edges = SqliteKnowledgeStore.add_edges
    original_deps = SqliteDependencyStore.replace_for_file

    def upsert(self: SqliteFileStore, repo_id: int, file: IndexedFile) -> int:
        if file.rel_path == target:
            if point == "before_file_row":
                raise Injected("died before the file row was written")
            file_id = original_upsert(self, repo_id, file)
            if point == "after_file_row":
                raise Injected("died after the file row, before any fact")
            return file_id
        return original_upsert(self, repo_id, file)

    def replace(self: SqliteKnowledgeStore, file_id: int, facts: FileFacts) -> None:
        if _path_of(self, file_id) != target:
            original_replace(self, file_id, facts)
            return
        if point == "mid_facts":
            # Deletes run, then only the first entity is inserted: the shape a
            # naive rollback gets wrong.
            original_replace(self, file_id, FileFacts(entities=facts.entities[:1]))
            raise Injected("died part way through writing entities and chunks")
        original_replace(self, file_id, facts)
        if point == "after_facts":
            raise Injected("died after entities and chunks, before linking")

    def add_edges(self: SqliteKnowledgeStore, edges: Sequence[EdgeRecord]) -> None:
        listed = list(edges)
        per_file = listed and all(edge.parser != GLOBAL_PARSER for edge in listed)
        if point == "mid_link" and per_file and _path_of(self, listed[0].file_id) == target:
            original_edges(self, listed[:1])
            raise Injected("died part way through writing this file's edges")
        original_edges(self, listed)

    def replace_deps(
        self: SqliteDependencyStore, file_id: int, records: Sequence[DependencyRecord]
    ) -> None:
        if _path_of(self, file_id) != target:
            original_deps(self, file_id, records)
            return
        if point == "after_link":
            raise Injected("died after linking, before dependency facts")
        if point == "mid_dependencies":
            original_deps(self, file_id, list(records)[:1])
            raise Injected("died part way through writing dependency facts")
        original_deps(self, file_id, records)
        if point == "after_dependencies":
            raise Injected("died after every fact, before the transaction committed")

    monkeypatch.setattr(SqliteFileStore, "upsert", upsert)
    monkeypatch.setattr(SqliteKnowledgeStore, "replace_file_facts", replace)
    monkeypatch.setattr(SqliteKnowledgeStore, "add_edges", add_edges)
    monkeypatch.setattr(SqliteDependencyStore, "replace_for_file", replace_deps)


def _path_of(
    store: SqliteFileStore | SqliteKnowledgeStore | SqliteDependencyStore, file_id: int | None
) -> str | None:
    """The path a ``file_id`` names, read through the store's own connection.

    Through the store's connection deliberately: an injection point runs *inside*
    the per-file transaction, and a second connection would not see the uncommitted
    row it needs to identify.
    """
    if file_id is None:
        return None
    row = store._db.execute("SELECT rel_path FROM files WHERE id = ?", (file_id,)).fetchone()
    return None if row is None else str(row["rel_path"])


#: Every persistent boundary in one file's write lifecycle, in execution order.
_BOUNDARIES = (
    "before_file_row",
    "after_file_row",
    "mid_facts",
    "after_facts",
    "mid_link",
    "after_link",
    "mid_dependencies",
    "after_dependencies",
)

#: The last three boundaries are inside the dependency write, which only runs for a
#: file some dependency parser claims — so they are exercised against the manifest.
#: The earlier ones are exercised against both a manifest and a source file, because
#: only the source file produces standing claims and an interned environment entity.
CUT_POINTS = [(point, "package.json") for point in _BOUNDARIES] + [
    (point, "m5.py") for point in _BOUNDARIES[:5]
]


@pytest.mark.parametrize(("point", "target"), CUT_POINTS)
def test_a_failure_at_any_boundary_leaves_no_half_indexed_file(
    point: str, target: str, crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing durable ever claims content is indexed when its facts are missing."""
    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")

    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, point, target)
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")

    assert_no_partial_file(observed, reference)


@pytest.mark.parametrize(("point", "target"), CUT_POINTS)
def test_an_ordinary_sync_recovers_from_a_failure_at_any_boundary(
    point: str, target: str, crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repoatlas sync` is the whole repair. No flag, no deletion, no special command."""
    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")

    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, point, target)
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")

    monkeypatch.undo()
    _resync(observed, crash_root)
    assert_converged(observed, reference)


@pytest.mark.parametrize(
    ("point", "target"),
    [
        ("after_file_row", "m5.py"),
        ("after_facts", "m5.py"),
        ("after_link", "package.json"),
    ],
)
def test_sync_full_also_recovers_a_file_whose_bytes_did_not_change(
    point: str, target: str, crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--full`` must never be *weaker* than ordinary recovery.

    It was, for exactly this defect: ``--full`` forces a re-hash, the bytes were
    genuinely unchanged, and the file stayed skipped.
    """
    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")

    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, point, target)
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")

    monkeypatch.undo()
    _resync(observed, crash_root, full=True)
    assert_converged(observed, reference)


def test_a_crash_never_leaves_the_repository_reported_current(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The currency claim is the run's, and a run that died did not make it."""
    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()

    workspace = _open_at(observed, crash_root)
    try:
        report = status(workspace, workspace.resolver().resolve(crash_root, command="status"))
        assert report.index_state != "current", report.index_state
        assert all(
            repo.last_sync_status is not SyncStatus.OK for repo in workspace.repositories.list_all()
        )
    finally:
        workspace.close()


def test_a_crash_during_a_later_sync_is_reported_interrupted_not_current(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case a first-index crash cannot cover.

    A repository that has never been indexed reports ``not indexed`` whatever else
    is true, so a crash during ``init`` proves nothing about the currency claim.
    This one starts from a healthy ``ok`` repository and interrupts the *second*
    run, which is the only way to observe ``interrupted`` being reported.
    """
    observed = _settings(tmp_path, "crashed")
    _index(observed, crash_root, tmp_path, "crashed")

    workspace = _open_at(observed, crash_root)
    try:
        assert (
            status(workspace, workspace.resolver().resolve(crash_root, command="status"))
        ).index_state == "current"
    finally:
        workspace.close()

    (crash_root / "svc" / "m5.py").write_text('import os\nY = os.environ["E_SECOND"]\n')
    git(crash_root / "svc", "add", "-A")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _resync(observed, crash_root)
    monkeypatch.undo()

    workspace = _open_at(observed, crash_root)
    try:
        report = status(workspace, workspace.resolver().resolve(crash_root, command="status"))
        assert report.index_state == "interrupted", report.index_state
    finally:
        workspace.close()

    _resync(observed, crash_root)
    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")
    assert_converged(observed, reference)


def test_reading_an_incomplete_index_does_not_repair_it(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` observes; it never writes to make the index look healthy."""
    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()

    before = logical_snapshot(observed)
    workspace = _open_at(observed, crash_root)
    try:
        status(workspace, workspace.resolver().resolve(crash_root, command="status"))
    finally:
        workspace.close()
    assert logical_snapshot(observed) == before


def test_evidence_from_an_interrupted_index_is_never_wrong_only_missing(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader may see *less* than a complete index, never something untrue.

    ``locate`` deliberately carries no staleness banner — index state is what
    ``status`` reports (V1_SPEC §4), and that division is unchanged here. What this
    pins is the part crash consistency owns: every fact a partially built index can
    serve is a fact a clean index also has, so an interrupted sync degrades recall
    and never correctness.
    """
    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")

    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()

    partial = logical_snapshot(observed)
    complete = logical_snapshot(reference)
    for table in ("files", "entities", "chunks", "dependencies", "unresolved_refs"):
        extra = set(partial[table]) - set(complete[table])  # type: ignore[arg-type]
        assert not extra, (
            f"the interrupted index holds {table} rows a clean index does not: {extra}"
        )


# -- what the file was before matters ----------------------------------------


def test_a_crash_while_replacing_facts_does_not_resurrect_or_lose_the_old_ones(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T0 complete, T1 killed mid-replacement, then recovery: T1 exactly, no T0 residue."""
    observed = _settings(tmp_path, "crashed")
    _index(observed, crash_root, tmp_path, "crashed")

    target = crash_root / "svc" / "m5.py"
    target.write_text(
        "import os\n"
        'NEW_VALUE = os.environ["E_BRAND_NEW"]\n'
        "\n"
        "\n"
        "def renamed():\n"
        "    return NEW_VALUE\n"
    )
    git(crash_root / "svc", "add", "-A")

    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _resync(observed, crash_root)
    monkeypatch.undo()
    _resync(observed, crash_root)

    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")
    assert_converged(observed, reference)

    connection = _connect(observed)
    try:
        assert not list(connection.execute("SELECT 1 FROM entities WHERE key LIKE '%:f5:%'")), (
            "a symbol from the previous content survived the replacement"
        )
        assert list(connection.execute("SELECT 1 FROM entities WHERE key = 'env:E_BRAND_NEW'")), (
            "the new content's environment variable is missing"
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("deleted", lambda repo: (repo / "m5.py").unlink()),
        (
            "rewritten",
            lambda repo: (repo / "m5.py").write_text('import os\nX = os.environ["E_LATER"]\n'),
        ),
        ("renamed", lambda repo: (repo / "m5.py").rename(repo / "m5_renamed.py")),
        ("binary", lambda repo: (repo / "m5.py").write_bytes(b"\x00\x01\x02binary\x00")),
    ],
)
def test_the_tree_changing_after_a_crash_converges_to_the_tree_as_it_now_is(
    name: str, mutate, crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery indexes what is on disk *now*, never what was mid-flight."""
    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()

    mutate(crash_root / "svc")
    git(crash_root / "svc", "add", "-A")
    _resync(observed, crash_root)

    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")
    assert_converged(observed, reference)


def test_a_file_ignored_after_a_crash_is_retired_not_forced_through_a_parse(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission policy decides, not the incomplete state of the previous run."""
    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()

    (crash_root / "svc" / ".repoatlasignore").write_text("m5.py\n")
    git(crash_root / "svc", "add", "-A")
    _resync(observed, crash_root)

    connection = _connect(observed)
    try:
        assert not _rows(connection, "m5.py"), "an ignored file was kept for repair"
    finally:
        connection.close()

    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")
    assert_converged(observed, reference)


# -- what the recovered graph must be able to answer --------------------------


def test_the_environment_variable_chain_survives_a_crash(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flagship capability, checked as a graph fact rather than as a count.

    An interned environment entity read by exactly one file is the fact the
    original defect destroyed most completely: nothing else referenced it, so it
    did not merely lose an edge, it ceased to exist.
    """
    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_facts", "m5.py")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()
    _resync(observed, crash_root)

    connection = _connect(observed)
    try:
        reads = list(
            connection.execute(
                "SELECT f.rel_path FROM edges g "
                "JOIN entities dst ON dst.id = g.dst_id "
                "JOIN files f ON f.id = g.file_id "
                "WHERE dst.key = 'env:E5' AND g.type = 'READS'"
            )
        )
        assert [row["rel_path"] for row in reads] == ["m5.py"]
        assert list(connection.execute("SELECT 1 FROM entities WHERE key = 'env:SERVICE_HOST'"))
    finally:
        connection.close()


def test_a_cross_file_reference_survives_a_crash_on_either_side(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``m5 -> m6`` must come back whichever of the two was mid-flight."""
    reference = _settings(tmp_path, "clean")
    _index(reference, crash_root, tmp_path, "clean")

    for tag, target in (("src", "m5.py"), ("dst", "m6.py")):
        observed = _settings(tmp_path, f"crashed-{tag}")
        _plant(monkeypatch, "after_facts", target)
        with pytest.raises(Injected):
            _index(observed, crash_root, tmp_path, f"crashed-{tag}")
        monkeypatch.undo()
        _resync(observed, crash_root)

        connection = _connect(observed)
        try:
            edge = list(
                connection.execute(
                    "SELECT g.confidence FROM edges g "
                    "JOIN entities src ON src.id = g.src_id "
                    "JOIN entities dst ON dst.id = g.dst_id "
                    "WHERE src.key = 'file:svc:m5.py' AND dst.key = 'file:svc:m6.py' "
                    "AND g.type = 'IMPORTS'"
                )
            )
            assert edge, f"the cross-file edge did not come back after crashing on {target}"
        finally:
            connection.close()
        assert_converged(observed, reference)


def test_dependency_facts_survive_a_crash_between_parse_and_write(
    crash_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, "after_link", "package.json")
    with pytest.raises(Injected):
        _index(observed, crash_root, tmp_path, "crashed")
    monkeypatch.undo()
    _resync(observed, crash_root)

    connection = _connect(observed)
    try:
        declared = list(
            connection.execute(
                "SELECT normalized_name, role FROM dependencies WHERE normalized_name = 'express'"
            )
        )
        assert declared, "the manifest's declared dependency was lost"
    finally:
        connection.close()


# -- the incremental fast path is not the price of this ----------------------


def test_a_healthy_unchanged_sync_still_reparses_nothing(crash_root: Path, tmp_path: Path) -> None:
    """Completeness is proven by the row existing, so unchanged files stay skipped."""
    observed = _settings(tmp_path, "clean")
    _index(observed, crash_root, tmp_path, "clean")

    workspace = _open_at(observed, crash_root)
    try:
        context = workspace.resolver().resolve(crash_root, command="sync")
        report = sync(workspace, context)
    finally:
        workspace.close()

    repository = report.repositories[0]
    assert repository.files_indexed == 0, "an unchanged tree was reparsed"
    assert repository.changed == 0
    assert repository.new == 0


# -- real signals ------------------------------------------------------------
#
# These kill a real CLI process with a real OS signal, because a monkeypatched
# exception cannot prove anything about ``SIGKILL``. What they used to get wrong
# was *when*.
#
# The old harness slept 1.5 seconds and inferred an in-flight sync from
# ``process.poll() is None``. That inference is unsound, and measurably so: a run
# has a **completion boundary** partway through it, and the process stays alive
# afterwards while it renders output, closes SQLite and tears down the
# interpreter. Measured on this fixture — 500 files, the completion commit at
# 2.70 s and process exit at 2.76 s — that shutdown interval is ~55 ms, about 2%
# of the run. Aiming a fixed sleep into it produced a live PID over a repository
# already marked ``ok`` in **3 of 12** trials, and the assertion that the index is
# not current then fails while nothing is wrong with the product.
#
# A faster machine reaches the same place with the sleep left at 1.5 s. So the
# repair is not a longer sleep or a retry; it is to stop inferring. The child now
# *tells* the parent it has arrived at the boundary and waits there, and the
# signal is sent only after that word arrives. Timing stops carrying meaning.


#: The child's side of the protocol. One word per line on a dedicated pipe.
STARTED = "STARTED"
READY_TO_INTERRUPT = "READY_TO_INTERRUPT"
COMPLETION_ENTERED = "COMPLETION_ENTERED"
SHUTDOWN_BLOCKED = "SHUTDOWN_BLOCKED"
FINISHED = "FINISHED"
HARNESS_TIMEOUT = "HARNESS_TIMEOUT"

#: What makes each awaited word unreachable if it arrives first. Stated per word
#: rather than as one terminal list, because the two waits mean opposite things:
#: reaching the completion boundary makes an interruption impossible, but it is
#: the *precondition* for the shutdown control. Anything not listed aborts only on
#: a child that gave up at the barrier.
_UNREACHABLE_AFTER: dict[str, tuple[str, ...]] = {
    "READY_TO_INTERRUPT": ("COMPLETION_ENTERED", "FINISHED", "HARNESS_TIMEOUT"),
}
_DEFAULT_UNREACHABLE: tuple[str, ...] = ("HARNESS_TIMEOUT",)

#: How long the parent waits for a protocol word, and how long the child waits at
#: the barrier before giving up. Generous, because they bound a hang rather than
#: establish anything: correctness comes from the words, never from the clock.
PROTOCOL_TIMEOUT_SECONDS = 180.0
CHILD_HOLD_SECONDS = 240.0

#: A test-only runner. It monkeypatches the boundary *in its own process* and then
#: invokes the real console entry point, so the shipped package carries no test
#: hook, no environment backdoor and no artificial delay (§6, §21).
RUNNER_SOURCE = '''
import atexit, os, sys, time

_channel = int(os.environ["REPOATLAS_TEST_CHANNEL_FD"])
_mode = os.environ["REPOATLAS_TEST_MODE"]


def say(word):
    os.write(_channel, (word + "\\n").encode())


def hold(limit):
    """Block until something kills us. Bounded, so an orphan cannot outlive CI."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        time.sleep(0.02)
    return False


say("STARTED")

from repoatlas.storage.sqlite.stores import SqliteRepositoryStore

original = SqliteRepositoryStore.mark_many_synced

if _mode == "barrier":
    # Immediately before the run's completion commit: every per-file transaction
    # has landed, `commit_global` has run inside this transaction, and the
    # repository is still `interrupted` because nothing has marked it otherwise.
    # The original is deliberately never called, so a signal delivered here is an
    # interruption by construction rather than by timing.
    def wrapped(self, completions):
        say("READY_TO_INTERRUPT")
        if not hold(float(os.environ["REPOATLAS_TEST_HOLD"])):
            say("HARNESS_TIMEOUT")
            raise SystemExit(97)
        return original(self, completions)

elif _mode == "shutdown":
    # The control: allowed *past* the boundary, then held during interpreter
    # shutdown — the state the old harness could not tell from an interrupted one.
    def wrapped(self, completions):
        say("COMPLETION_ENTERED")
        return original(self, completions)

    @atexit.register
    def _hold_during_shutdown():
        say("SHUTDOWN_BLOCKED")
        hold(float(os.environ["REPOATLAS_TEST_HOLD"]))

else:
    raise SystemExit(f"unknown mode {_mode}")

SqliteRepositoryStore.mark_many_synced = wrapped

from repoatlas.cli.main import run

sys.argv = ["repoatlas", "init", sys.argv[1]]
try:
    run()
except SystemExit:
    pass
say("FINISHED")
'''


class Protocol:
    """The child's words, read from a pipe with a bounded wait.

    A pipe rather than a file or a directory poll, for two reasons. It carries
    *events* rather than state, so there is no "count the files" predicate that
    could also be true after completion (§4). And the parent sees EOF the moment
    the child dies, so a ``SIGKILL``ed child produces a clean end-of-stream
    instead of a timeout.
    """

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self._buffer = b""
        self.seen: list[str] = []

    def close_write(self) -> None:
        """Drop the parent's copy, so the child holds the only writer."""
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1

    def wait_for(self, expected: str, *, timeout: float = PROTOCOL_TIMEOUT_SECONDS) -> None:
        """Block until ``expected`` arrives, or fail saying what arrived instead.

        Deliberately not a retry loop over the whole trial. If the child reports
        that it finished, the harness has failed to interrupt anything, and
        saying so is the entire point of the protocol — a harness that quietly
        tried again until the timing worked would be the old bug with extra
        steps (§11).
        """
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(self.read_fd, selectors.EVENT_READ)
        try:
            while True:
                # Judged from everything said so far, before waiting for more. A
                # child that has passed the point of no return is usually silent
                # afterwards, so a check that only ran on new input would report a
                # timeout — the least informative version of the truth.
                if expected in self.seen:
                    return
                for word in _UNREACHABLE_AFTER.get(expected, _DEFAULT_UNREACHABLE):
                    if word in self.seen:
                        raise AssertionError(
                            f"the child reached {word} without reaching {expected}: "
                            f"nothing was interrupted. Said: {self.seen}"
                        )
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    f"timed out after {timeout:.0f}s waiting for {expected}; "
                    f"the child said {self.seen or 'nothing'}"
                )
                if not selector.select(min(remaining, 1.0)):
                    continue
                chunk = os.read(self.read_fd, 4096)
                if not chunk:
                    raise AssertionError(
                        f"the child exited before saying {expected}; it said {self.seen}"
                    )
                self._buffer += chunk
                *complete, self._buffer = self._buffer.split(b"\n")
                self.seen.extend(word.decode() for word in complete if word)
        finally:
            selector.close()

    def drain(self, *, timeout: float = 5.0) -> None:
        """Collect whatever is left, up to end of stream."""
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(self.read_fd, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if not selector.select(0.1):
                    continue
                chunk = os.read(self.read_fd, 4096)
                if not chunk:
                    return
                self._buffer += chunk
                *complete, self._buffer = self._buffer.split(b"\n")
                self.seen.extend(word.decode() for word in complete if word)
        finally:
            selector.close()

    def close(self) -> None:
        self.close_write()
        os.close(self.read_fd)


def _cli_env(home: Path, config: Path) -> dict[str, str]:
    """A child environment that cannot reach the operator's real state.

    Every XDG root is redirected as well as ``REPOATLAS_CONFIG``, because
    ``config_path()`` and ``database_path()`` fall back to ``XDG_CONFIG_HOME`` and
    ``XDG_DATA_HOME`` when no override is set — and a child that inherits one
    default silently writes ``~/.config/repoatlas``. ``test_no_test_touches_real_repoatlas_state``
    is the assertion; this is the thing it asserts about.
    """
    return {
        **os.environ,
        "HOME": str(home),
        "REPOATLAS_CONFIG": str(config),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    }


class Harness:
    """One isolated workspace plus the machinery to interrupt a sync in it."""

    def __init__(self, base: Path, file_count: int) -> None:
        self.base = base
        self.root = base / "repos"
        self.root.mkdir(parents=True)
        _build_tree(self.root, file_count)
        self.home = base / "home"
        (self.home / ".config").mkdir(parents=True)
        (self.home / ".local" / "share").mkdir(parents=True)
        self.database = base / "state" / "atlas.db"
        self.config = base / "config.yaml"
        self.config.write_text(
            f"repos:\n  root: {self.root}\nstorage:\n  database: {self.database}\n"
        )
        self.runner = base / "runner.py"
        self.runner.write_text(RUNNER_SOURCE)

    @property
    def env(self) -> dict[str, str]:
        return _cli_env(self.home, self.config)

    def settings(self) -> Settings:
        return Settings.model_validate(
            {
                "storage": {"database": str(self.database)},
            }
        )

    @contextlib.contextmanager
    def child(self, mode: str) -> Iterator[tuple[subprocess.Popen[bytes], Protocol]]:
        """Start a real CLI ``init`` under the test runner, wired to a protocol pipe."""
        protocol = Protocol()
        environment = {
            **self.env,
            "PYTHONPATH": str(REPO_ROOT),
            "REPOATLAS_TEST_CHANNEL_FD": str(protocol.write_fd),
            "REPOATLAS_TEST_MODE": mode,
            "REPOATLAS_TEST_HOLD": str(CHILD_HOLD_SECONDS),
        }
        process = subprocess.Popen(
            [sys.executable, str(self.runner), str(self.root)],
            cwd=str(self.root),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(protocol.write_fd,),
        )
        protocol.close_write()
        try:
            yield process, protocol
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=60)
            protocol.close()

    def repository_status(self) -> str | None:
        """The persisted status, read by a separate connection as another process would."""
        if not self.database.exists():
            return None
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute("SELECT last_sync_status FROM repositories").fetchone()
            return None if row is None else str(row[0])
        finally:
            connection.close()

    def index_state(self) -> str:
        workspace = _open_at(self.settings(), self.root)
        try:
            report = status(workspace, workspace.resolver().resolve(self.root, command="status"))
            return report.index_state
        finally:
            workspace.close()

    def recover(self) -> subprocess.CompletedProcess[str]:
        return _repoatlas(["sync"], self.env, self.root)


def _repoatlas(args: Sequence[str], env: dict[str, str], cwd: Path):
    return subprocess.run(
        [sys.executable, "-m", "repoatlas.cli.main", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


#: Two sizes. The small one finishes so fast that a 1.5-second sleep would land
#: well past the end of it every time — which is exactly the case the old harness
#: could not test at all, and the barrier handles identically (§19, §20).
SIZES = {"tiny": 2, "realistic": 500}


@pytest.fixture(params=sorted(SIZES), ids=sorted(SIZES))
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Harness:
    return Harness(tmp_path / request.param, SIZES[request.param])


@pytest.mark.slow
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGKILL", "SIGINT"])
def test_a_real_signal_at_the_completion_boundary_recovers_with_one_ordinary_sync(
    signal_name: str, harness: Harness, tmp_path: Path
) -> None:
    """A real process, a real signal, and a *proven* interruption.

    ``SIGTERM`` is what a container runtime sends and ``SIGKILL`` cannot be
    caught, so recovery has to fall out of durable state rather than out of any
    handler. ``SIGINT`` is the one whose old test produced the known false
    failure, and it is now pinned by the same construction as the others.

    Nothing here is timed. The child says it has reached the boundary; only then
    is the signal sent.
    """
    with harness.child("barrier") as (process, protocol):
        protocol.wait_for(READY_TO_INTERRUPT)

        # The boundary itself, asserted two independent ways (§13).
        assert COMPLETION_ENTERED not in protocol.seen, protocol.seen
        assert harness.repository_status() == SyncStatus.INTERRUPTED.value, (
            "the run's completion marker committed before the signal was sent"
        )
        assert process.poll() is None, "the child died before it could be signalled"

        process.send_signal(getattr(signal, signal_name))
        process.wait(timeout=120)

    assert FINISHED not in protocol.seen, protocol.seen
    assert harness.database.exists(), "the interrupted run left no database at all"
    assert harness.index_state() != "current", harness.index_state()

    assert harness.recover().returncode == 0
    assert harness.index_state() == "current"

    reference = _settings(tmp_path, "clean")
    _index(reference, harness.root, tmp_path, "clean")
    assert_converged(harness.settings(), reference)


@pytest.mark.slow
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGKILL"])
def test_the_writer_lock_is_free_after_a_signal(signal_name: str, tmp_path: Path) -> None:
    """§16: the lock is an ``flock`` held by an open file description.

    The kernel drops it when the process dies, whatever killed it, so no stale
    lock survives and no cleanup code has to run. Asserted by the next sync
    simply succeeding — and by it not taking the lock timeout to do so.
    """
    harness = Harness(tmp_path / "lock", SIZES["tiny"])
    with harness.child("barrier") as (process, protocol):
        protocol.wait_for(READY_TO_INTERRUPT)
        process.send_signal(getattr(signal, signal_name))
        process.wait(timeout=120)

    began = time.monotonic()
    assert harness.recover().returncode == 0
    assert time.monotonic() - began < 20, "the recovery sync queued behind a stale lock"


# -- the control that proves the old inference was unsound -------------------


@pytest.mark.slow
def test_a_live_process_can_already_have_finished_syncing(tmp_path: Path) -> None:
    """§22: ``poll() is None`` over a repository that is already ``current``.

    The child is allowed *past* the completion boundary and then held during
    interpreter shutdown — rendering, closing SQLite, tearing down. That state is
    indistinguishable from an in-flight sync by process liveness alone, and it is
    the state the old harness silently classified as "interrupted" whenever a
    fixed sleep happened to land in it.

    This is the regression guard for the test bug rather than for the product:
    it fails if anyone reintroduces liveness as the proof of interruption.
    """
    harness = Harness(tmp_path / "control", SIZES["tiny"])
    with harness.child("shutdown") as (process, protocol):
        protocol.wait_for(SHUTDOWN_BLOCKED)

        # The old harness's premise, and its conclusion, side by side.
        assert process.poll() is None, "the child should still be alive in shutdown"
        assert harness.repository_status() == SyncStatus.OK.value
        assert harness.index_state() == "current"

        # The new harness's rule reaches the opposite verdict from the same state,
        # because it reads the child's words rather than its liveness.
        assert COMPLETION_ENTERED in protocol.seen
        assert READY_TO_INTERRUPT not in protocol.seen
        process.kill()
        process.wait(timeout=60)

    # Killing a process whose work was already durable changes nothing.
    assert harness.index_state() == "current"


@pytest.mark.slow
def test_the_harness_refuses_to_call_a_completed_run_an_interruption(tmp_path: Path) -> None:
    """§11: if the child finishes first, that is a harness failure, said out loud.

    The protocol wait raises rather than returning, so no trial can quietly
    proceed to assert things about an interruption that never happened. Without
    this, a barrier that stopped working would degrade back into the timing test
    it replaced, and nothing would say so.
    """
    harness = Harness(tmp_path / "refuses", SIZES["tiny"])
    with harness.child("shutdown") as (process, protocol):
        protocol.wait_for(SHUTDOWN_BLOCKED)
        with pytest.raises(AssertionError, match="without reaching READY_TO_INTERRUPT"):
            protocol.wait_for(READY_TO_INTERRUPT, timeout=5.0)
        process.kill()
        process.wait(timeout=60)


@pytest.mark.slow
def test_the_protocol_wait_times_out_rather_than_hanging(tmp_path: Path) -> None:
    """§12: a child that never arrives ends the test, not the CI job."""
    harness = Harness(tmp_path / "timeout", SIZES["tiny"])
    with harness.child("barrier") as (process, protocol):
        protocol.wait_for(READY_TO_INTERRUPT)
        with pytest.raises(AssertionError, match="timed out"):
            protocol.wait_for(FINISHED, timeout=1.0)
        process.kill()
        process.wait(timeout=60)


# -- isolation ---------------------------------------------------------------


def test_the_signal_children_never_reach_real_repoatlas_state(tmp_path: Path) -> None:
    """§23: every child path is under ``tmp_path``, checked rather than assumed.

    The environment is asserted directly because the failure mode is silent: a
    child that inherits one default writes the operator's ``~/.config/repoatlas``
    and nothing in the trial notices.
    """
    harness = Harness(tmp_path / "isolation", SIZES["tiny"])
    environment = harness.env
    for key in ("HOME", "REPOATLAS_CONFIG", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        assert key in environment, key
        assert str(tmp_path) in environment[key], (key, environment[key])
    assert str(tmp_path) in str(harness.database)
    assert str(tmp_path) in str(harness.root)

    from repoatlas.config.settings import config_path, database_path

    for path in (config_path(), database_path()):
        assert "pytest" in str(path) or "tmp" in str(path) or not path.exists(), path


# -- a large pass, committed in groups ---------------------------------------

#: Enough files to be parsed by workers and committed in groups (repoatlas.sync.engine),
#: and more than one group's worth, so the failing file is not in the first.
LARGE_PASS = 2 * GROUP_MAX_FILES + 20


@pytest.mark.parametrize("point", ["after_file_row", "mid_facts", "mid_link", "after_dependencies"])
def test_a_failure_inside_a_commit_group_leaves_no_half_indexed_file_and_recovers(
    point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group commits several whole files at once; a failure rolls the group back whole.

    The target is the last file the pass writes, so earlier groups have committed and
    the one holding it has not: the survivors must each be complete, and an ordinary sync
    must converge on a clean build.
    """
    root = tmp_path / "large"
    root.mkdir()
    _build_tree(root, LARGE_PASS)
    monkeypatch.setenv(WORKERS_ENV, "2")
    target = "package.json" if point == "after_dependencies" else "run.sh"
    if point in {"mid_link"}:
        target = sorted(f"m{i}.py" for i in range(1, LARGE_PASS + 1))[-1]
    reference = _settings(tmp_path, "clean")
    _index(reference, root, tmp_path, "clean")

    observed = _settings(tmp_path, "crashed")
    _plant(monkeypatch, point, target)
    with pytest.raises(Injected):
        _index(observed, root, tmp_path, "crashed")

    assert_no_partial_file(observed, reference)
    monkeypatch.undo()
    _resync(observed, root)
    assert_converged(observed, reference)
