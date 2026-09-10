"""SQLite implementations of the storage protocols (ARCHITECTURE.md §3, §4).

This module and its siblings are the only place SQL appears.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from repoatlas.models.dependencies import (
    DependencyObservation,
    DependencyPackage,
    DependencyRecord,
    DependencyRole,
    DependencyScope,
)
from repoatlas.models.parsing import EdgeDirection, EdgeType, EntityType, NeighborOrder
from repoatlas.models.repository import FileState, IndexedFile, Repository, SyncStatus
from repoatlas.storage.protocols import (
    ChunkHit,
    EdgeRecord,
    EntityDescription,
    EntityIdentity,
    EntityRecord,
    FileFacts,
    FileRow,
    NeighborEdge,
    SymbolRow,
    SyncCompletion,
    UnresolvedRefRecord,
)
from repoatlas.storage.sqlite.database import Database

#: Everything an :class:`EntityIdentity` needs, shared by both key lookups.
#: ``LEFT JOIN`` throughout: an interned entity has no file and no repository row,
#: and resolving its key must still work.
_ENTITY_IDENTITY_SELECT = """
    SELECT e.id, e.key, e.type, e.name, e.qualified_name, e.line_start,
           e.line_end, e.signature, f.rel_path AS path, r.slug AS repository_slug
    FROM entities e
    LEFT JOIN files        f ON f.id = e.file_id
    LEFT JOIN repositories r ON r.id = e.repo_id
"""


def _entity_identity(row: sqlite3.Row) -> EntityIdentity:
    return EntityIdentity(
        entity_id=row["id"],
        key=row["key"],
        type=EntityType(row["type"]),
        name=row["name"],
        qualified_name=row["qualified_name"],
        repository_slug=row["repository_slug"],
        path=row["path"],
        line_start=row["line_start"],
        line_end=row["line_end"],
        signature=row["signature"],
    )


def _json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    loaded = json.loads(raw)
    return tuple(str(item) for item in loaded) if isinstance(loaded, list) else ()


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class SqliteRepositoryStore:
    """Managed repositories and their indexed state."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @staticmethod
    def _row_to_repository(row: sqlite3.Row) -> Repository:
        dirty_flag = row["last_indexed_dirty"]
        return Repository(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            path=Path(row["path"]),
            branch=row["branch"],
            head_sha=row["head_sha"],
            is_dirty=bool(row["is_dirty"]),
            languages=_json_list(row["languages_json"]),
            manifests=_json_list(row["manifests_json"]),
            lockfiles=_json_list(row["lockfiles_json"]),
            last_indexed_at=_parse_dt(row["last_indexed_at"]),
            last_indexed_head=row["last_indexed_head"],
            last_indexed_dirty=None if dirty_flag is None else bool(dirty_flag),
            last_sync_status=SyncStatus(row["last_sync_status"]),
        )

    def upsert(self, repo: Repository) -> int:
        """Insert or update by canonical path, which is the natural key.

        The path being the natural key is also the answer to what a *move* means:
        a repository that changes path is a removal plus a discovery, not the same
        repository somewhere else (ARCHITECTURE.md §3). ``slug`` follows from that
        — it is assigned once and never rewritten, so a later change to the
        configured root cannot silently invalidate every entity key that was built
        from it.
        """
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO repositories (
                    name, slug, path, branch, head_sha, is_dirty,
                    languages_json, manifests_json, lockfiles_json,
                    last_indexed_at, last_indexed_head, last_indexed_dirty, last_sync_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    slug = CASE
                        WHEN repositories.slug = '' THEN excluded.slug
                        ELSE repositories.slug
                    END,
                    branch = excluded.branch,
                    head_sha = excluded.head_sha,
                    is_dirty = excluded.is_dirty,
                    languages_json = excluded.languages_json,
                    manifests_json = excluded.manifests_json,
                    lockfiles_json = excluded.lockfiles_json,
                    last_indexed_at = excluded.last_indexed_at,
                    last_indexed_head = excluded.last_indexed_head,
                    last_indexed_dirty = excluded.last_indexed_dirty,
                    last_sync_status = excluded.last_sync_status
                """,
                (
                    repo.name,
                    repo.slug,
                    str(repo.path),
                    repo.branch,
                    repo.head_sha,
                    int(repo.is_dirty),
                    json.dumps(list(repo.languages)),
                    json.dumps(list(repo.manifests)),
                    json.dumps(list(repo.lockfiles)),
                    repo.last_indexed_at.isoformat() if repo.last_indexed_at else None,
                    repo.last_indexed_head,
                    None if repo.last_indexed_dirty is None else int(repo.last_indexed_dirty),
                    str(repo.last_sync_status),
                ),
            )
            row = conn.execute(
                "SELECT id FROM repositories WHERE path = ?", (str(repo.path),)
            ).fetchone()
        return int(row["id"])

    def get_by_name(self, name: str) -> Repository | None:
        row = self._db.execute(
            "SELECT * FROM repositories WHERE name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        return None if row is None else self._row_to_repository(row)

    def find_all_by_name(self, name: str) -> list[Repository]:
        """All repositories sharing a name — used to report ambiguity (V1_SPEC §6)."""
        rows = self._db.execute(
            "SELECT * FROM repositories WHERE name = ? ORDER BY path", (name,)
        ).fetchall()
        return [self._row_to_repository(row) for row in rows]

    def get_by_slug(self, slug: str) -> Repository | None:
        """Lookup by identity. Unlike a name, this can only ever match one row."""
        row = self._db.execute(
            "SELECT * FROM repositories WHERE slug = ? AND slug != ''", (slug,)
        ).fetchone()
        return None if row is None else self._row_to_repository(row)

    def assign_slug(self, repo_id: int, slug: str) -> None:
        """Give an identity to a row written before identities existed.

        Only ever fills an empty value. An assigned identity is immutable: the
        entity keys built from it are already persisted, and rewriting it would
        strand them without any reparse being triggered (ARCHITECTURE.md §3, §9).
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE repositories SET slug = ? WHERE id = ? AND slug = ''", (slug, repo_id)
            )

    def relocate(self, repo_id: int, path: Path) -> None:
        """Move a registered repository to a new canonical path, keeping its identity.

        Deliberately *not* folded into :meth:`upsert`. ``upsert`` keys on the path,
        which is exactly the assumption that breaks when the managed root moves:
        the same repository arrives with a path the table has never seen, so an
        insert is attempted and the slug it still holds collides. Overloading
        ``upsert`` with identity migration would hide that decision inside a method
        whose contract is "insert or update by path"; a caller that means to
        relocate a repository should have to say so.

        Only the location changes. The row id is preserved because ``files``,
        ``entities`` and every fact keyed by ``repo_id`` reference it, and the slug
        is preserved because entity keys are built from it — a relocation that
        renumbered either would strand the facts it was supposed to carry across
        (ARCHITECTURE.md §3). Absolute location is mutable metadata; identity is
        not.

        Idempotent, and safe to call inside a caller's transaction: the whole
        repository set must relocate as one unit, so the caller opens a single
        transaction and every row moves within it (ARCHITECTURE.md §3).
        """
        with self._db.transaction() as conn:
            conn.execute("UPDATE repositories SET path = ? WHERE id = ?", (str(path), repo_id))

    def get_by_path(self, path: Path) -> Repository | None:
        row = self._db.execute("SELECT * FROM repositories WHERE path = ?", (str(path),)).fetchone()
        return None if row is None else self._row_to_repository(row)

    def list_all(self) -> list[Repository]:
        rows = self._db.execute("SELECT * FROM repositories ORDER BY name, path").fetchall()
        return [self._row_to_repository(row) for row in rows]

    def delete(self, repo_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM repositories WHERE id = ?", (repo_id,))

    def mark_sync_started(self, repo_id: int) -> None:
        """Record that a sync is in flight.

        If the process dies here the status stays ``interrupted``, which forces a
        full rehash next time rather than trusting stat (V1_SPEC §12).
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE repositories SET last_sync_status = ? WHERE id = ?",
                (str(SyncStatus.INTERRUPTED), repo_id),
            )

    def mark_sync_failed(self, repo_id: int) -> None:
        """Record that this repository could not be synchronized at all.

        Separate from ``interrupted``, which means a run started and did not
        finish. This means the run could not establish what to do: git could not
        enumerate the repository and RepoAtlas refused to substitute a different
        file set (see :class:`~repoatlas.sync.scanner.EnumerationUnavailable`).

        Both are "not ``ok``", so both stop ``status`` claiming currency and both
        force a full rehash on the next run — but only one of them is worth telling
        an operator to investigate, and a shared value could not.
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE repositories SET last_sync_status = ? WHERE id = ?",
                (str(SyncStatus.FAILED), repo_id),
            )

    def mark_many_synced(self, completions: Sequence[SyncCompletion]) -> None:
        """Mark every repository of one run finished, in one commit.

        Called *after* the global-link swap, never before: the cross-repository
        graph is part of the knowledge base, so a run whose link pass failed did
        not produce a current index and must not record one (ARCHITECTURE.md §9).
        Grouping the updates means the graph and the currency claim it justifies
        commit together.
        """
        if not completions:
            return
        finished = datetime.now(UTC).isoformat()
        with self._db.transaction() as conn:
            conn.executemany(
                """
                UPDATE repositories SET
                    last_sync_status = ?,
                    last_indexed_at = ?,
                    last_indexed_head = ?,
                    last_indexed_dirty = ?,
                    head_sha = ?,
                    is_dirty = ?,
                    branch = ?
                WHERE id = ?
                """,
                [
                    (
                        str(SyncStatus.OK),
                        finished,
                        done.head_sha,
                        int(done.is_dirty),
                        done.head_sha,
                        int(done.is_dirty),
                        done.branch,
                        done.repo_id,
                    )
                    for done in completions
                ],
            )

    def update_detected_metadata(
        self,
        repo_id: int,
        *,
        languages: Sequence[str],
        manifests: Sequence[str],
        lockfiles: Sequence[str],
    ) -> None:
        """Record what the working tree contains (V1_SPEC §5)."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE repositories SET languages_json = ?, manifests_json = ?, "
                "lockfiles_json = ? WHERE id = ?",
                (
                    json.dumps(list(languages)),
                    json.dumps(list(manifests)),
                    json.dumps(list(lockfiles)),
                    repo_id,
                ),
            )


class SqliteFileStore:
    """File rows and the cascade that removes everything a file asserted."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_states(self, repo_id: int) -> dict[str, FileState]:
        rows = self._db.execute(
            "SELECT id, rel_path, size, mtime_ns, content_hash, parser_version "
            "FROM files WHERE repo_id = ?",
            (repo_id,),
        ).fetchall()
        return {
            row["rel_path"]: FileState(
                file_id=row["id"],
                size=row["size"],
                mtime_ns=row["mtime_ns"],
                content_hash=row["content_hash"],
                parser_version=row["parser_version"],
            )
            for row in rows
        }

    def states_for(self, repo_id: int, rel_paths: Sequence[str]) -> dict[str, FileState]:
        """:meth:`list_states` narrowed to the paths actually asked about.

        A freshness question about a handful of files should not read a whole
        repository's file table to answer it.
        """
        if not rel_paths:
            return {}
        placeholders = ",".join("?" * len(rel_paths))
        rows = self._db.execute(
            f"""
            SELECT id, rel_path, size, mtime_ns, content_hash, parser_version
            FROM files WHERE repo_id = ? AND rel_path IN ({placeholders})
            """,  # noqa: S608 - placeholders are a generated count, values are bound
            (repo_id, *rel_paths),
        ).fetchall()
        return {
            row["rel_path"]: FileState(
                file_id=row["id"],
                size=row["size"],
                mtime_ns=row["mtime_ns"],
                content_hash=row["content_hash"],
                parser_version=row["parser_version"],
            )
            for row in rows
        }

    def upsert(self, repo_id: int, file: IndexedFile) -> int:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO files (
                    repo_id, rel_path, size, mtime_ns, content_hash,
                    language, parser, parser_version, is_binary, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, rel_path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    content_hash = excluded.content_hash,
                    language = excluded.language,
                    parser = excluded.parser,
                    parser_version = excluded.parser_version,
                    is_binary = excluded.is_binary,
                    indexed_at = excluded.indexed_at
                """,
                (
                    repo_id,
                    file.rel_path,
                    file.size,
                    file.mtime_ns,
                    file.content_hash,
                    file.language,
                    file.parser,
                    file.parser_version,
                    int(file.is_binary),
                    (file.indexed_at or datetime.now(UTC)).isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM files WHERE repo_id = ? AND rel_path = ?",
                (repo_id, file.rel_path),
            ).fetchone()
        return int(row["id"])

    def delete_many(self, file_ids: Sequence[int]) -> None:
        """Delete files and, by cascade, every fact they asserted."""
        if not file_ids:
            return
        with self._db.transaction() as conn:
            conn.executemany("DELETE FROM files WHERE id = ?", [(fid,) for fid in file_ids])

    def count_outdated(self, repo_id: int, parser_version: str) -> int:
        """Files whose facts were produced by a different facts version.

        The cheap half of freshness. A parser fix, a new parser or a chunking
        change makes stored facts stale while every file on disk is untouched, so
        HEAD, mtime and content hash all still agree and nothing else would notice
        (V1_SPEC §12). ``status`` asks this so it never reports an index as current
        when RepoAtlas can cheaply tell that a sync would rewrite it.
        """
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM files WHERE repo_id = ? AND parser_version <> ?",
            (repo_id, parser_version),
        ).fetchone()
        return int(row["n"])

    def count(self, repo_ids: Sequence[int] | None = None) -> int:
        if repo_ids is None:
            row = self._db.execute("SELECT COUNT(*) AS n FROM files").fetchone()
        else:
            if not repo_ids:
                return 0
            placeholders = ",".join("?" * len(repo_ids))
            row = self._db.execute(
                f"SELECT COUNT(*) AS n FROM files WHERE repo_id IN ({placeholders})",  # noqa: S608
                tuple(repo_ids),
            ).fetchone()
        return int(row["n"])


class SqliteKnowledgeStore:
    """Entities and edges, with the atomic per-file replacement path."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Make several store calls one atomic unit.

        Two callers need it, and both for the same reason — a group of writes that
        is only meaningful whole (ARCHITECTURE.md §9):

        * the **global-link swap**, which must delete the previous graph and insert
          its replacement in a single commit so no other connection can observe the
          gap;
        * the **per-file indexing unit**, which must commit a file's row together
          with every fact derived from that content, because the row carries the
          hash a later sync uses to decide the file needs no work.

        Store methods keep their own transactions, for when each is called alone,
        and join this one when it is open.
        """
        with self._db.transaction():
            yield

    def replace_file_facts(self, file_id: int, facts: FileFacts) -> None:
        """Delete everything this file asserted, then insert the new facts.

        Atomic: a crash leaves either the old facts or the new ones, never a mix.
        """
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM entities WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM edges WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM unresolved_refs WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM dependencies WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))

            for entity in facts.entities:
                conn.execute(
                    """
                    INSERT INTO entities (
                        type, key, repo_id, file_id, name, qualified_name,
                        line_start, line_end, signature, meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(entity.type),
                        entity.key,
                        entity.repo_id,
                        entity.file_id,
                        entity.name,
                        entity.qualified_name,
                        entity.line_start,
                        entity.line_end,
                        entity.signature,
                        entity.meta_json,
                    ),
                )

            for chunk in facts.chunks:
                conn.execute(
                    """
                    INSERT INTO chunks (
                        file_id, repo_id, line_start, line_end, text,
                        token_estimate, content_hash, span_index, split_index, split_total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.file_id,
                        chunk.repo_id,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                        chunk.token_estimate,
                        chunk.content_hash,
                        chunk.span_index,
                        chunk.split_index,
                        chunk.split_total,
                    ),
                )

    def intern_entity(self, entity: EntityRecord) -> int:
        """Get-or-create a global entity by its natural key.

        The insert carries its own conflict clause rather than trusting the
        preceding read. A bare SELECT-then-INSERT is a check-then-act: two writers
        that both find nothing both insert, and the partial unique index turns the
        loser into ``UNIQUE constraint failed: entities.key`` — which is precisely
        what two concurrent syncs used to produce (ARCHITECTURE.md §9). The writer
        lock now makes that pair impossible; this makes the statement correct on
        its own rather than correct because of something elsewhere.
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM entities WHERE key = ? AND file_id IS NULL", (entity.key,)
            ).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = conn.execute(
                """
                INSERT INTO entities (type, key, repo_id, file_id, name, qualified_name)
                VALUES (?, ?, ?, NULL, ?, ?)
                ON CONFLICT (key) WHERE file_id IS NULL DO NOTHING
                """,
                (
                    str(entity.type),
                    entity.key,
                    entity.repo_id,
                    entity.name,
                    entity.qualified_name,
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid or 0)
            # The conflict clause fired: somebody else interned it first.
            existing = conn.execute(
                "SELECT id FROM entities WHERE key = ? AND file_id IS NULL", (entity.key,)
            ).fetchone()
        return int(existing["id"])

    def find_file_entity(self, file_id: int, key: str) -> int | None:
        row = self._db.execute(
            "SELECT id FROM entities WHERE file_id = ? AND key = ?", (file_id, key)
        ).fetchone()
        return None if row is None else int(row["id"])

    def add_edges(self, edges: Sequence[EdgeRecord]) -> None:
        if not edges:
            return
        with self._db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO edges (
                    src_id, dst_id, type, confidence, repo_id, file_id,
                    line_start, line_end, parser
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        e.src_id,
                        e.dst_id,
                        str(e.type),
                        e.confidence,
                        e.repo_id,
                        e.file_id,
                        e.line_start,
                        e.line_end,
                        e.parser,
                    )
                    for e in edges
                ],
            )

    def add_unresolved_refs(self, refs: Sequence[UnresolvedRefRecord]) -> None:
        """Record claims whose target the asserting file cannot resolve alone."""
        if not refs:
            return
        with self._db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO unresolved_refs (
                    repo_id, file_id, src_id, edge_type, target_kind, target_key,
                    line_start, line_end, confidence, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r.repo_id,
                        r.file_id,
                        r.src_id,
                        str(r.edge_type),
                        str(r.target_kind),
                        r.target_key,
                        r.line_start,
                        r.line_end,
                        r.confidence,
                        r.meta_json,
                    )
                    for r in refs
                ],
            )

    def list_unresolved_refs(self) -> list[UnresolvedRefRecord]:
        rows = self._db.execute(
            """
            SELECT id, repo_id, file_id, src_id, edge_type, target_kind, target_key,
                   line_start, line_end, confidence, meta_json
            FROM unresolved_refs
            ORDER BY id
            """
        ).fetchall()
        return [
            UnresolvedRefRecord(
                id=row["id"],
                repo_id=row["repo_id"],
                file_id=row["file_id"],
                src_id=row["src_id"],
                edge_type=EdgeType(row["edge_type"]),
                target_kind=EntityType(row["target_kind"]),
                target_key=row["target_key"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                confidence=row["confidence"],
                meta_json=row["meta_json"],
            )
            for row in rows
        ]

    def delete_edges_by_parser(self, parser: str) -> int:
        """Drop every edge a given producer created.

        The global link pass owns its edges exclusively, so deleting them by
        producer and rebuilding makes the pass idempotent: running it twice gives
        the same graph as running it once, which is what makes an incremental sync
        equal a full reindex (V1_SPEC §12).
        """
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM edges WHERE parser = ?", (parser,))
        return cursor.rowcount or 0

    def list_file_rows(self) -> list[FileRow]:
        """Every indexed file joined to its repository and its file entity."""
        rows = self._db.execute(
            """
            SELECT f.id AS file_id, e.id AS file_entity_id, f.repo_id AS repo_id,
                   r.name AS repo_name, r.slug AS repo_slug, r.path AS repo_path,
                   f.rel_path AS rel_path
            FROM files f
            JOIN repositories r ON r.id = f.repo_id
            JOIN entities e ON e.file_id = f.id AND e.type = 'file'
            """
        ).fetchall()
        return [
            FileRow(
                file_id=row["file_id"],
                file_entity_id=row["file_entity_id"],
                repo_id=row["repo_id"],
                repo_name=row["repo_name"],
                repo_slug=row["repo_slug"],
                repo_path=Path(row["repo_path"]),
                rel_path=row["rel_path"],
            )
            for row in rows
        ]

    def list_symbols(self, types: Sequence[EntityType] | None = None) -> list[SymbolRow]:
        """File-scoped definitions, for cross-file and cross-repository resolution."""
        sql = """
            SELECT id, file_id, repo_id, type, name, qualified_name, line_start
            FROM entities
            WHERE file_id IS NOT NULL
        """
        params: tuple[object, ...] = ()
        if types is not None:
            if not types:
                return []
            placeholders = ",".join("?" * len(types))
            sql += f" AND type IN ({placeholders})"
            params = tuple(str(t) for t in types)
        rows = self._db.execute(sql, params).fetchall()
        return [
            SymbolRow(
                entity_id=row["id"],
                file_id=row["file_id"],
                repo_id=row["repo_id"],
                type=EntityType(row["type"]),
                name=row["name"],
                qualified_name=row["qualified_name"],
                line_start=row["line_start"],
            )
            for row in rows
        ]

    def neighbors(
        self,
        entity_ids: Sequence[int],
        edge_types: Sequence[EdgeType] | None = None,
        limit: int = 100,
    ) -> list[EdgeRecord]:
        """Edges touching any of ``entity_ids``, in either direction."""
        if not entity_ids:
            return []
        id_placeholders = ",".join("?" * len(entity_ids))
        params: list[object] = [*entity_ids, *entity_ids]
        type_clause = ""
        if edge_types:
            type_placeholders = ",".join("?" * len(edge_types))
            type_clause = f" AND type IN ({type_placeholders})"
            params.extend(str(t) for t in edge_types)
        params.append(limit)

        rows = self._db.execute(
            f"""
            SELECT src_id, dst_id, type, confidence, repo_id, file_id,
                   line_start, line_end, parser
            FROM edges
            WHERE (src_id IN ({id_placeholders}) OR dst_id IN ({id_placeholders})){type_clause}
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()
        return [
            EdgeRecord(
                src_id=row["src_id"],
                dst_id=row["dst_id"],
                type=EdgeType(row["type"]),
                confidence=row["confidence"],
                repo_id=row["repo_id"],
                file_id=row["file_id"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                parser=row["parser"],
            )
            for row in rows
        ]

    def expand_neighbors(
        self,
        entity_ids: Sequence[int],
        edge_types: Sequence[EdgeType] | None = None,
        limit: int = 200,
        direction: EdgeDirection = EdgeDirection.ANY,
        order: NeighborOrder = NeighborOrder.CONFIDENCE,
    ) -> list[NeighborEdge]:
        """One hop out of ``entity_ids``, with both endpoints' repositories.

        The repository of each endpoint comes back with the edge so the caller can
        enforce the cross-repository traversal rule without a second query
        (ARCHITECTURE.md §10 stage 4). So does the file that asserted the edge, so
        a caller can go on to supply those lines rather than only name them.

        ``direction`` narrows which end the queried entity must sit on. ``ANY`` is
        the neighbourhood question retrieval asks, and stays the default.

        ``order`` decides which edges survive ``limit``. The default is the order
        retrieval has always received; :attr:`~NeighborOrder.NATURAL` sorts on the
        endpoints' own keys instead, for a caller whose answer *is* the cut rather
        than a stage that is about to fuse everything anyway.
        """
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        if direction is EdgeDirection.OUT:
            id_clause = f"e.src_id IN ({placeholders})"
            params: list[object] = [*entity_ids]
        elif direction is EdgeDirection.IN:
            id_clause = f"e.dst_id IN ({placeholders})"
            params = [*entity_ids]
        else:
            id_clause = f"(e.src_id IN ({placeholders}) OR e.dst_id IN ({placeholders}))"
            params = [*entity_ids, *entity_ids]
        type_clause = ""
        if edge_types:
            type_placeholders = ",".join("?" * len(edge_types))
            type_clause = f" AND e.type IN ({type_placeholders})"
            params.extend(str(t) for t in edge_types)
        params.append(limit)
        # Ordered on the endpoints' natural keys, or on the confidence the funnel
        # reads as proximity. Which one is chosen decides which edges survive the
        # limit, so it is the caller's decision rather than a detail.
        order_clause = (
            "e.type, src.key, dst.key, e.line_start, e.line_end, e.id"
            if order is NeighborOrder.NATURAL
            else "e.confidence DESC, e.id"
        )

        rows = self._db.execute(
            f"""
            SELECT e.src_id, e.dst_id, e.type, e.confidence, e.file_id,
                   e.line_start, e.line_end, e.parser,
                   src.repo_id AS src_repo_id, dst.repo_id AS dst_repo_id
            FROM edges e
            JOIN entities src ON src.id = e.src_id
            JOIN entities dst ON dst.id = e.dst_id
            WHERE {id_clause}{type_clause}
            ORDER BY {order_clause}
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()
        return [
            NeighborEdge(
                src_id=row["src_id"],
                dst_id=row["dst_id"],
                type=EdgeType(row["type"]),
                confidence=row["confidence"],
                src_repo_id=row["src_repo_id"],
                dst_repo_id=row["dst_repo_id"],
                file_id=row["file_id"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                parser=row["parser"],
            )
            for row in rows
        ]

    def describe_entities(self, entity_ids: Sequence[int]) -> list[EntityDescription]:
        """Resolve entity ids to their provenance, for rendering as evidence.

        Only file-scoped entities can be cited: an interned entity such as an
        environment variable has no location of its own, so it is skipped rather
        than given an invented one.
        """
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        rows = self._db.execute(
            f"""
            SELECT e.id, e.type, e.name, e.qualified_name, e.line_start, e.line_end,
                   e.signature, f.rel_path AS path, r.name AS repository,
                   r.slug AS repository_slug
            FROM entities e
            JOIN files        f ON f.id = e.file_id
            JOIN repositories r ON r.id = e.repo_id
            WHERE e.id IN ({placeholders})
            """,  # noqa: S608 - placeholders are a generated count, values are bound
            tuple(entity_ids),
        ).fetchall()
        return [
            EntityDescription(
                entity_id=row["id"],
                repository=row["repository"],
                repository_slug=row["repository_slug"] or "",
                path=row["path"],
                line_start=row["line_start"] or 1,
                line_end=row["line_end"] or row["line_start"] or 1,
                text=row["signature"] or "",
                description=f"defines {row['name']}",
            )
            for row in rows
        ]

    def get_entities_by_key(self, keys: Sequence[str]) -> list[EntityIdentity]:
        """Entities whose natural key is exactly one of ``keys``.

        Exact only: no prefix synthesis, no name clause, no fuzzy fallback. A key
        is an identity, and resolving one through a name search answers a
        different question — ``find_entities_by_name`` matches a name *or* a
        qualified name *or* a key, which is right for search and wrong for
        identity.

        Returns a list rather than a mapping because ``entities.key`` is unique
        only for interned rows: ``idx_entities_global_key`` is partial
        (``WHERE file_id IS NULL``). A key resolving to more than one row is an
        ambiguity for the caller to report, never one for storage to settle by
        picking.
        """
        if not keys:
            return []
        placeholders = ",".join("?" * len(keys))
        rows = self._db.execute(
            f"""
            {_ENTITY_IDENTITY_SELECT}
            WHERE e.key IN ({placeholders})
            ORDER BY e.id
            """,
            tuple(keys),
        ).fetchall()
        return [_entity_identity(row) for row in rows]

    def keys_for_entities(self, entity_ids: Sequence[int]) -> dict[int, EntityIdentity]:
        """The natural key of each entity id, in one query.

        The inverse of :meth:`get_entities_by_key`, and the reason a response can
        carry entity handles without a lookup per item.
        """
        if not entity_ids:
            return {}
        placeholders = ",".join("?" * len(entity_ids))
        rows = self._db.execute(
            f"""
            {_ENTITY_IDENTITY_SELECT}
            WHERE e.id IN ({placeholders})
            """,
            tuple(entity_ids),
        ).fetchall()
        return {int(row["id"]): _entity_identity(row) for row in rows}

    def chunks_for_file(self, file_id: int, line_start: int, line_end: int) -> list[ChunkHit]:
        """Indexed chunks overlapping a line range of one file.

        The text behind a span RepoAtlas already knows about — an edge's asserting
        lines, say — so that provenance can be *supplied* as evidence rather than
        offered as coordinates the caller has no way to verify.

        ``score`` is 0.0 throughout: these chunks were addressed, not searched, so
        there is no relevance to report and inventing one would be a claim.
        """
        rows = self._db.execute(
            """
            SELECT c.id AS chunk_id, c.file_id, c.repo_id, c.line_start, c.line_end,
                   c.text, c.span_index, c.split_index, c.split_total,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug
            FROM chunks c
            JOIN files        f ON f.id = c.file_id
            JOIN repositories r ON r.id = c.repo_id
            WHERE c.file_id = ? AND c.line_start <= ? AND c.line_end >= ?
            ORDER BY c.line_start, c.span_index, c.split_index
            """,
            (file_id, line_end, line_start),
        ).fetchall()
        return [
            ChunkHit(
                chunk_id=row["chunk_id"],
                file_id=row["file_id"],
                repo_id=row["repo_id"],
                repository=row["repository"],
                repository_slug=row["repository_slug"] or "",
                path=row["path"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                text=row["text"],
                score=0.0,
                split_index=row["split_index"],
                split_total=row["split_total"],
            )
            for row in rows
        ]

    def gc_orphan_entities(self) -> int:
        """Drop interned entities no edge references any more (ARCHITECTURE §3)."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM entities
                WHERE file_id IS NULL
                  AND type != ?
                  AND id NOT IN (SELECT src_id FROM edges)
                  AND id NOT IN (SELECT dst_id FROM edges)
                """,
                (str(EntityType.REPOSITORY),),
            )
        return cursor.rowcount or 0

    def count_entities(self, repo_ids: Sequence[int] | None = None) -> int:
        return self._count("entities", repo_ids)

    def count_edges(self, repo_ids: Sequence[int] | None = None) -> int:
        return self._count("edges", repo_ids)

    def _count(self, table: str, repo_ids: Sequence[int] | None) -> int:
        if table not in {"entities", "edges"}:  # defensive: table name is interpolated
            raise ValueError(f"unsupported table: {table}")
        if repo_ids is None:
            row = self._db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
        else:
            if not repo_ids:
                return 0
            placeholders = ",".join("?" * len(repo_ids))
            row = self._db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE repo_id IN ({placeholders})",  # noqa: S608
                tuple(repo_ids),
            ).fetchone()
        return int(row["n"])


def _dependency_scope(source_path: str) -> str:
    """The dependency domain a file belongs to: the directory it sits in.

    ``package.json`` → ``.``; ``backend/package.json`` → ``backend``. Deterministic,
    derived from the layout alone, and the unit of manifest-to-lockfile pairing.
    Nothing here infers a runtime package environment, because RepoAtlas cannot
    observe one without executing the ecosystem's own resolver (V1_SPEC §17).
    """
    if not source_path:
        return "."
    parent = PurePosixPath(source_path).parent.as_posix()
    return parent or "."


def _dependency_scopes(
    observations: Sequence[DependencyObservation],
) -> tuple[DependencyScope, ...]:
    """Partition one package's observations by scope, preserving order within each."""
    by_scope: dict[str, dict[DependencyRole, list[DependencyObservation]]] = {}
    for observation in observations:
        roles = by_scope.setdefault(observation.scope, {})
        roles.setdefault(observation.role, []).append(observation)
    return tuple(
        DependencyScope(
            scope=scope,
            declarations=tuple(roles.get(DependencyRole.DECLARED, ())),
            resolutions=tuple(roles.get(DependencyRole.RESOLVED, ())),
            provisions=tuple(roles.get(DependencyRole.PROVIDED, ())),
        )
        for scope, roles in sorted(by_scope.items())
    )


class SqliteDependencyStore:
    """Declared, resolved and provided packages, with per-file provenance."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def replace_for_file(self, file_id: int, records: Sequence[DependencyRecord]) -> None:
        """Replace everything one manifest or lockfile asserts. Atomic."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM dependencies WHERE file_id = ?", (file_id,))
            if not records:
                return
            conn.executemany(
                """
                INSERT INTO dependencies (
                    repo_id, file_id, ecosystem, name, normalized_name, role,
                    constraint_text, version, group_name, line_start, line_end, parser
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r.repo_id,
                        r.file_id,
                        r.ecosystem,
                        r.name,
                        r.normalized_name,
                        r.role,
                        r.constraint,
                        r.version,
                        r.group,
                        r.line_start,
                        r.line_end,
                        r.parser,
                    )
                    for r in records
                ],
            )

    def providers(self, ecosystem: str, normalized_name: str) -> list[tuple[int, str]]:
        """Repositories that provide this exact package (V1_SPEC §9)."""
        rows = self._db.execute(
            """
            SELECT DISTINCT d.repo_id AS repo_id, r.name AS repo_name
            FROM dependencies d
            JOIN repositories r ON r.id = d.repo_id
            WHERE d.role = 'provided' AND d.ecosystem = ? AND d.normalized_name = ?
            """,
            (ecosystem, normalized_name),
        ).fetchall()
        return [(int(row["repo_id"]), str(row["repo_name"])) for row in rows]

    def list_for_repos(self, repo_ids: Sequence[int] | None = None) -> list[DependencyRecord]:
        sql = """
            SELECT repo_id, file_id, ecosystem, name, normalized_name, role,
                   constraint_text, version, group_name, line_start, line_end, parser
            FROM dependencies
        """
        params: tuple[object, ...] = ()
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            sql += f" WHERE repo_id IN ({placeholders})"
            params = tuple(repo_ids)
        rows = self._db.execute(sql, params).fetchall()
        return [
            DependencyRecord(
                repo_id=row["repo_id"],
                file_id=row["file_id"],
                ecosystem=row["ecosystem"],
                name=row["name"],
                normalized_name=row["normalized_name"],
                role=row["role"],
                constraint=row["constraint_text"],
                version=row["version"],
                group=row["group_name"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                parser=row["parser"] or "",
            )
            for row in rows
        ]

    def list_packages(self, repo_ids: Sequence[int] | None = None) -> list[DependencyPackage]:
        """Every dependency observation, grouped by package. Nothing is discarded.

        Grouping, not merging. The previous implementation keyed on
        ``(repo_id, ecosystem, normalized_name)`` and then *assigned*
        ``constraint``, ``version``, ``manifest_path`` and ``lockfile_path`` while
        looping over the rows, so a repository holding a root ``package.json`` and
        a ``backend/package.json`` reported exactly one constraint and one version
        — whichever row SQLite returned last — and every other declaration
        disappeared with no trace that it had existed. The raw rows were right the
        whole time; the view over them was the destructive step (V1_SPEC §13).

        Manifest-to-lockfile pairing is by **scope**: the directory the file sits
        in. ``backend/package.json`` pairs with ``backend/package-lock.json`` and
        never with the root lockfile, because co-location is what every one of
        these tools actually means by "this lockfile belongs to that manifest".
        Where the layout does not prove a pairing — a workspace root lockfile and a
        nested member manifest — the observations simply stay in separate scopes:
        an unpaired declaration is an honest state, and attributing a root
        lockfile's version to a nested package would be a guess (V1_SPEC §13, §2).
        """
        records = self.list_for_repos(repo_ids)
        repositories = {
            row["id"]: (row["name"], row["slug"] or "")
            for row in self._db.execute("SELECT id, name, slug FROM repositories").fetchall()
        }
        paths = {
            row["id"]: row["rel_path"]
            for row in self._db.execute("SELECT id, rel_path FROM files").fetchall()
        }

        grouped: dict[tuple[str, str, str], list[DependencyObservation]] = {}
        display: dict[tuple[str, str, str], str] = {}
        for record in records:
            repository, slug = repositories.get(record.repo_id, ("", ""))
            source_path = paths.get(record.file_id, "")
            observation = DependencyObservation(
                repository=repository,
                repository_slug=slug,
                ecosystem=record.ecosystem,
                name=record.name,
                normalized_name=record.normalized_name,
                role=DependencyRole(record.role),
                value=record.constraint if record.role == "declared" else record.version,
                group=record.group,
                scope=_dependency_scope(source_path),
                source_path=source_path,
                line=record.line_start,
                line_end=record.line_end,
                parser=record.parser,
            )
            key = (slug or repository, record.ecosystem, record.normalized_name)
            grouped.setdefault(key, []).append(observation)
            # The display name is the one a human wrote; normalization is for
            # matching. Chosen by sort so it never depends on row order either.
            current = display.get(key)
            if current is None or record.name < current:
                display[key] = record.name

        packages: list[DependencyPackage] = []
        for key, observations in grouped.items():
            ordered = tuple(sorted(observations, key=lambda o: o.order))
            first = ordered[0]
            packages.append(
                DependencyPackage(
                    repository=first.repository,
                    repository_slug=first.repository_slug,
                    ecosystem=first.ecosystem,
                    name=display[key],
                    normalized_name=first.normalized_name,
                    observations=ordered,
                    scopes=_dependency_scopes(ordered),
                )
            )
        return sorted(
            packages,
            key=lambda p: (
                p.repository_slug or p.repository,
                p.ecosystem,
                p.normalized_name,
            ),
        )

    def count(self, repo_ids: Sequence[int] | None = None) -> int:
        return len(self.list_for_repos(repo_ids))
