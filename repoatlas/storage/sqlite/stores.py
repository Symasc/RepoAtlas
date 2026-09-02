"""SQLite implementations of the storage protocols (ARCHITECTURE.md §3, §4).

This module and its siblings are the only place SQL appears.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.repository import FileState, IndexedFile, Repository, SyncStatus
from repoatlas.storage.protocols import EdgeRecord, EntityDescription, EntityRecord, FileFacts
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
                        file_id, repo_id, line_start, line_end, text, token_estimate
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.file_id,
                        chunk.repo_id,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                        chunk.token_estimate,
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
