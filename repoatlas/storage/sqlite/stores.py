"""SQLite implementations of the storage protocols (ARCHITECTURE.md §3, §4).

This module and its siblings are the only place SQL appears.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from repoatlas.models.dependencies import DependencyRecord, DependencyView
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.repository import FileState, IndexedFile, Repository, SyncStatus
from repoatlas.storage.protocols import (
    EdgeRecord,
    EntityDescription,
    EntityRecord,
    FileFacts,
    FileRow,
    NeighborEdge,
    SymbolRow,
    UnresolvedRefRecord,
)
from repoatlas.storage.sqlite.database import Database


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
        """Insert or update by canonical path, which is the natural key."""
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO repositories (
                    name, path, branch, head_sha, is_dirty,
                    languages_json, manifests_json, lockfiles_json,
                    last_indexed_at, last_indexed_head, last_indexed_dirty, last_sync_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
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

    def mark_sync_finished(
        self, repo_id: int, *, head_sha: str | None, is_dirty: bool, branch: str | None
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
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
                (
                    str(SyncStatus.OK),
                    datetime.now(UTC).isoformat(),
                    head_sha,
                    int(is_dirty),
                    head_sha,
                    int(is_dirty),
                    branch,
                    repo_id,
                ),
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
                        token_estimate, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.file_id,
                        chunk.repo_id,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                        chunk.token_estimate,
                        chunk.content_hash,
                    ),
                )

    def intern_entity(self, entity: EntityRecord) -> int:
        """Get-or-create a global entity by its natural key."""
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
                """,
                (
                    str(entity.type),
                    entity.key,
                    entity.repo_id,
                    entity.name,
                    entity.qualified_name,
                ),
            )
        return int(cursor.lastrowid or 0)

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
                   r.name AS repo_name, r.path AS repo_path, f.rel_path AS rel_path
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
    ) -> list[NeighborEdge]:
        """One hop out of ``entity_ids``, with both endpoints' repositories.

        The repository of each endpoint comes back with the edge so the caller can
        enforce the cross-repository traversal rule without a second query
        (ARCHITECTURE.md §10 stage 4).
        """
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        params: list[object] = [*entity_ids, *entity_ids]
        type_clause = ""
        if edge_types:
            type_placeholders = ",".join("?" * len(edge_types))
            type_clause = f" AND e.type IN ({type_placeholders})"
            params.extend(str(t) for t in edge_types)
        params.append(limit)

        rows = self._db.execute(
            f"""
            SELECT e.src_id, e.dst_id, e.type, e.confidence,
                   src.repo_id AS src_repo_id, dst.repo_id AS dst_repo_id
            FROM edges e
            JOIN entities src ON src.id = e.src_id
            JOIN entities dst ON dst.id = e.dst_id
            WHERE (e.src_id IN ({placeholders}) OR e.dst_id IN ({placeholders})){type_clause}
            ORDER BY e.confidence DESC, e.id
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
                   e.signature, f.rel_path AS path, r.name AS repository
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
                path=row["path"],
                line_start=row["line_start"] or 1,
                line_end=row["line_end"] or row["line_start"] or 1,
                text=row["signature"] or "",
                description=f"defines {row['name']}",
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

    def join_view(self, repo_ids: Sequence[int] | None = None) -> list[DependencyView]:
        """Declared constraints joined to resolved versions, per repository.

        The join is a LEFT JOIN in both directions by design: a declared package
        with no lockfile entry and a locked transitive package with no declaration
        are both real states, and neither may be silently dropped (V1_SPEC §13).
        """
        records = self.list_for_repos(repo_ids)
        names = {
            row["id"]: row["name"]
            for row in self._db.execute("SELECT id, name FROM repositories").fetchall()
        }
        paths = {
            row["id"]: row["rel_path"]
            for row in self._db.execute("SELECT id, rel_path FROM files").fetchall()
        }

        merged: dict[tuple[int, str, str], dict[str, object]] = {}
        for record in records:
            if record.role == "provided":
                continue
            key = (record.repo_id, record.ecosystem, record.normalized_name)
            entry = merged.setdefault(
                key,
                {
                    "repository": names.get(record.repo_id, ""),
                    "ecosystem": record.ecosystem,
                    "name": record.name,
                    "normalized_name": record.normalized_name,
                },
            )
            if record.role == "declared":
                entry["constraint"] = record.constraint
                entry["group"] = record.group
                entry["manifest_path"] = paths.get(record.file_id)
                entry["manifest_line"] = record.line_start
            else:
                entry["version"] = record.version
                entry["lockfile_path"] = paths.get(record.file_id)
                entry["lockfile_line"] = record.line_start

        return sorted(
            (DependencyView.model_validate(entry) for entry in merged.values()),
            key=lambda v: (v.repository, v.ecosystem, v.normalized_name),
        )

    def count(self, repo_ids: Sequence[int] | None = None) -> int:
        return len(self.list_for_repos(repo_ids))
