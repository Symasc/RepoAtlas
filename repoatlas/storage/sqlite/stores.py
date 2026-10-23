"""SQLite implementations of the storage protocols (ARCHITECTURE.md §3, §4).

This module and its siblings are the only place SQL appears.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from repoatlas.models.dependencies import (
    DependencyObservation,
    DependencyPackage,
    DependencyRecord,
    DependencyRole,
    DependencyScope,
)
from repoatlas.models.deployment_targets import DeploymentLink
from repoatlas.models.http import ROUTE_KEY_PREFIX
from repoatlas.models.ordinary_outcomes import ORDINARY_FAMILY, OutcomeRow
from repoatlas.models.parsing import (
    USES_EXPORT_TABLE,
    EdgeDirection,
    EdgeType,
    EntityType,
    NeighborOrder,
)
from repoatlas.models.repository import FileState, IndexedFile, Repository, SyncStatus
from repoatlas.storage.protocols import (
    EVIDENCE_PATHS,
    ChunkHit,
    Confinement,
    EdgeRecord,
    EntityDescription,
    EntityIdentity,
    EntityRecord,
    EnvReadRow,
    FileFacts,
    FileRow,
    HttpCallDecisionRow,
    HttpEdgeRow,
    HttpRefusalNear,
    LinkDecisionRow,
    NeighborCount,
    NeighborCursor,
    NeighborEdge,
    NeighborGroup,
    NeighborRow,
    NeighborRule,
    RouteServerRow,
    SymbolRow,
    SyncCompletion,
    UnresolvedRefRecord,
)
from repoatlas.storage.sqlite.database import Database
from repoatlas.storage.sqlite.deployment_links import DEPLOYMENT_LINKS_SELECT, deployment_link
from repoatlas.storage.sqlite.search import path_scope

#: What every environment variable's key starts with (repoatlas.graph.keys.env_key).
ENV_KEY_PREFIX = "env:"

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


#: What every shared HTTP node's key starts with: routes, endpoints, deployments.
_HTTP_KEY_PREFIX = "http-"


#: The ``meta`` key holding when the global pass last committed.
LINKS_PASS_KEY = "links_pass_at"  # noqa: S105 - a key name
#: The ``meta`` key under which the ``USES`` stage records whether its edges are complete.
USES_LINKS_KEY = "uses_links"
#: The ``meta`` key the global commit sets once it has written ``http_call_decisions``:
#: until then an empty table means "not decided yet", not "no requests".
HTTP_CALL_DECISIONS_KEY = "http_call_decisions"
#: The columns a standing claim is read back with (:func:`_claim`).
_CLAIM_COLUMNS = (
    "u.id, u.repo_id, u.file_id, u.src_id, u.edge_type, u.target_kind, "
    "u.target_key, u.line_start, u.line_end, u.confidence, u.meta_json"
)
#: Bound parameters per ``IN (...)`` list, well under SQLite's limit.
_CHUNK = 500


def _claim(row: sqlite3.Row) -> UnresolvedRefRecord:
    return UnresolvedRefRecord(
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


def _chunks(values: Sequence[int]) -> Iterator[Sequence[int]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _after(prefix: str) -> str:
    """The smallest string greater than every string starting with ``prefix``."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


#: An evidence key a neighbour rule filters on: a resolution field name, nothing else.
_EVIDENCE_KEY = re.compile(r"[a-z_]+")


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
        finished = datetime.now(timezone.utc).isoformat()
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
                    (file.indexed_at or datetime.now(timezone.utc)).isoformat(),
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
                        token_estimate, span_index, split_index, split_total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.file_id,
                        chunk.repo_id,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                        chunk.token_estimate,
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

    def file_entity_ids(self, file_id: int) -> dict[str, int]:
        """:meth:`find_file_entity` for every key of one file, in one query.

        File-scoped keys are not declared unique, so where one recurs the lowest id is
        kept: the row :meth:`find_file_entity` returns, from either index it is read by.
        """
        found: dict[str, int] = {}
        for row in self._db.execute(
            "SELECT id, key FROM entities WHERE file_id = ? ORDER BY id", (file_id,)
        ):
            found.setdefault(str(row["key"]), int(row["id"]))
        return found

    def add_edges(self, edges: Sequence[EdgeRecord]) -> None:
        if not edges:
            return
        with self._db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO edges (
                    src_id, dst_id, type, confidence, repo_id, file_id,
                    line_start, line_end, parser, resolution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        e.resolution,
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

    def list_unresolved_refs(
        self,
        repo_id: int | None = None,
        *,
        pending_only: bool = False,
        exclude_uses: bool = False,
    ) -> list[UnresolvedRefRecord]:
        """Standing claims, optionally one repository's, optionally pending facts only.

        A repository's claims are the ones its files assert, reached through
        ``files`` (indexed on ``repo_id``) rather than a column ``unresolved_refs``
        has no index for. ``pending_only`` is a cheap prefilter on the recorded
        role; callers that need the exact rule still apply it. ``exclude_uses`` leaves
        out ``USES`` claims, which the global pass resolves in a stage of their own.
        """
        sql = f"SELECT {_CLAIM_COLUMNS} FROM unresolved_refs u"  # noqa: S608 - column names only
        clauses: list[str] = []
        params: list[object] = []
        if repo_id is not None:
            sql += " JOIN files f ON f.id = u.file_id"
            clauses.append("f.repo_id = ?")
            params.append(repo_id)
        if pending_only:
            clauses.append("u.meta_json LIKE ?")
            params.append('%"pending_fact"%')
        if exclude_uses:
            clauses.append("u.edge_type != ?")
            params.append(str(EdgeType.USES))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._db.execute(sql + " ORDER BY u.id", tuple(params)).fetchall()
        return [_claim(row) for row in rows]

    def list_use_claims(self, file_ids: Sequence[int] | None) -> list[UnresolvedRefRecord]:
        """The ``USES`` and ``IMPORTS`` claims of these files, or of every file.

        A use is resolved from its own claim and the imports beside it, which say where
        its name was imported (:mod:`repoatlas.graph.uses_link`). Read by file through
        ``idx_unresolved_file``, so resolving a few files reads only their claims.
        """
        kinds = (str(EdgeType.USES), str(EdgeType.IMPORTS))
        sql = (
            f"SELECT {_CLAIM_COLUMNS} FROM unresolved_refs u "  # noqa: S608 - column names only
            "WHERE u.edge_type IN (?, ?)"
        )
        if file_ids is None:
            rows = self._db.execute(sql + " ORDER BY u.id", kinds).fetchall()
            return [_claim(row) for row in rows]
        found: list[sqlite3.Row] = []
        for chunk in _chunks(sorted(set(file_ids))):
            found.extend(
                self._db.execute(
                    sql + f" AND u.file_id IN ({','.join('?' * len(chunk))})", (*kinds, *chunk)
                ).fetchall()
            )
        return [_claim(row) for row in sorted(found, key=lambda row: row["id"])]

    def list_use_claims_from(self, modules: Sequence[str]) -> list[UnresolvedRefRecord]:
        """The ``USES`` claims of names imported from any of these module specifiers.

        A cheap text prefilter on the recorded specifier, then the exact rule on the
        decoded claim: what the Prisma client binding reads
        (:mod:`repoatlas.graph.prisma_schema`).
        """
        found: dict[int, UnresolvedRefRecord] = {}
        sql = (
            f"SELECT {_CLAIM_COLUMNS} FROM unresolved_refs u "  # noqa: S608 - column names only
            "WHERE u.edge_type = ? AND u.meta_json LIKE ?"
        )
        for module in sorted(set(modules)):
            rows = self._db.execute(sql, (str(EdgeType.USES), f'%"{module}"%')).fetchall()
            for row in rows:
                claim = _claim(row)
                if json.loads(claim.meta_json or "{}").get("module") == module and claim.id:
                    found[claim.id] = claim
        return [found[key] for key in sorted(found)]

    def list_uses_targets(self, src_ids: Sequence[int]) -> list[tuple[int, int, int, str]]:
        """(user, used, asserting file, producer) of every stored ``USES`` edge from these
        entities: the per-file pass's and the global pass's."""
        found: list[tuple[int, int, int, str]] = []
        for chunk in _chunks(sorted(set(src_ids))):
            found.extend(
                (row[0], row[1], row[2], row[3] or "")
                for row in self._db.execute(
                    "SELECT src_id, dst_id, file_id, parser FROM edges "  # noqa: S608 - placeholders
                    f"WHERE type = ? AND src_id IN ({','.join('?' * len(chunk))})",
                    (str(EdgeType.USES), *chunk),
                ).fetchall()
            )
        return sorted(found)

    def list_module_facts(self, file_id: int | None = None) -> list[UnresolvedRefRecord]:
        """Module-resolution facts (:mod:`repoatlas.parsers.module_facts`): all, or one file's.

        A key range on ``idx_unresolved_target``: each such claim targets the config
        ``module-resolution:<kind>``. The caller still reads the fact's own marker.
        """
        sql = (
            f"SELECT {_CLAIM_COLUMNS} FROM unresolved_refs u "  # noqa: S608 - column names only
            "WHERE u.target_kind = ? AND u.target_key >= ? AND u.target_key < ?"
        )
        params: tuple[object, ...] = (
            str(EntityType.CONFIG),
            "module-resolution:",
            "module-resolution;",
        )
        if file_id is not None:
            sql += " AND u.file_id = ?"
            params = (*params, file_id)
        rows = self._db.execute(sql + " ORDER BY u.id", params).fetchall()
        return [_claim(row) for row in rows]

    def list_facts_of_kind(
        self, target_key: str, repo_id: int | None = None
    ) -> list[tuple[int, str, str]]:
        """Every module-resolution fact of one kind (``target_key``), as (repository, file,
        meta): all of them, or one repository's."""
        sql = (
            "SELECT u.repo_id, f.rel_path, u.meta_json FROM unresolved_refs u "
            "JOIN files f ON f.id = u.file_id WHERE u.target_kind = ? AND u.target_key = ?"
        )
        params: tuple[object, ...] = (str(EntityType.CONFIG), target_key)
        if repo_id is not None:
            sql += " AND u.repo_id = ?"
            params = (*params, repo_id)
        rows = self._db.execute(sql + " ORDER BY u.repo_id, f.rel_path", params).fetchall()
        return [(int(row["repo_id"]), str(row["rel_path"]), row["meta_json"] or "") for row in rows]

    def replace_facts_of_kind(
        self, target_key: str, file_ids: Sequence[int], facts: Sequence[UnresolvedRefRecord]
    ) -> None:
        """Replace the module-resolution facts of one kind these files state with ``facts``,
        each a fact of one of them, atomically. Every other claim of theirs is kept."""
        with self._db.transaction() as conn:
            for chunk in _chunks(sorted(set(file_ids))):
                placeholders = ",".join("?" * len(chunk))
                conn.execute(
                    "DELETE FROM unresolved_refs WHERE target_kind = ? AND target_key = ? "  # noqa: S608 - placeholders are generated counts, values are bound
                    f"AND file_id IN ({placeholders})",
                    (str(EntityType.CONFIG), target_key, *chunk),
                )
            self.add_unresolved_refs(facts)

    def list_export_tables(self, file_ids: Sequence[int] | None) -> dict[int, str]:
        """Each file's export-table claim (:mod:`repoatlas.parsers.ts_uses`), as its raw meta.

        One per file; were there two, the later one is kept, as it always was.
        """
        sql = (
            "SELECT u.file_id, u.meta_json FROM unresolved_refs u "
            "WHERE u.target_kind = ? AND u.target_key = ? AND u.edge_type = ?"
        )
        params: tuple[object, ...] = (str(EntityType.SYMBOL), USES_EXPORT_TABLE, str(EdgeType.USES))
        rows: list[sqlite3.Row] = []
        if file_ids is None:
            rows = self._db.execute(sql + " ORDER BY u.id", params).fetchall()
        else:
            for chunk in _chunks(sorted(set(file_ids))):
                rows.extend(
                    self._db.execute(
                        sql + f" AND u.file_id IN ({','.join('?' * len(chunk))}) ORDER BY u.id",
                        (*params, *chunk),
                    ).fetchall()
                )
        return {int(row["file_id"]): row["meta_json"] for row in rows if row["meta_json"]}

    def delete_use_edges(self, parser: str, file_ids: Sequence[int] | None) -> int:
        """Drop the ``USES`` edges ``parser`` wrote for these files, or for every file."""
        sql = "DELETE FROM edges WHERE type = ? AND parser = ?"
        params: tuple[object, ...] = (str(EdgeType.USES), parser)
        with self._db.transaction() as conn:
            if file_ids is None:
                return conn.execute(sql, params).rowcount or 0
            removed = 0
            for chunk in _chunks(sorted(set(file_ids))):
                removed += (
                    conn.execute(
                        sql + f" AND file_id IN ({','.join('?' * len(chunk))})", (*params, *chunk)
                    ).rowcount
                    or 0
                )
            return removed

    def replace_use_reads(
        self, file_ids: Sequence[int] | None, reads: Sequence[tuple[int, int]]
    ) -> None:
        """Record which export tables these files' uses read, replacing what they read before.

        ``reads`` holds (consumer file, file whose export table it read) pairs, and
        ``file_ids`` the consumers they replace -- every consumer when ``None``.
        """
        with self._db.transaction() as conn:
            if file_ids is None:
                conn.execute("DELETE FROM use_reads")
            else:
                for chunk in _chunks(sorted(set(file_ids))):
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(
                        f"DELETE FROM use_reads WHERE file_id IN ({placeholders})",  # noqa: S608 - placeholders are generated counts, values are bound
                        tuple(chunk),
                    )
            conn.executemany(
                "INSERT INTO use_reads (file_id, read_file_id) VALUES (?, ?)", list(reads)
            )

    def use_consumers_landing_in(self, file_id: int, parser: str) -> set[int]:
        """Files with a ``USES`` edge ``parser`` wrote into one of this file's entities.

        Asked before the file's facts are replaced or deleted: its entities are written
        again with new ids, and every edge into the old ones goes with them.
        """
        rows = self._db.execute(
            """
            SELECT DISTINCT e.file_id AS file_id
            FROM entities d JOIN edges e ON e.dst_id = d.id AND e.type = ?
            WHERE d.file_id = ? AND e.parser = ? AND e.file_id IS NOT NULL
            """,
            (str(EdgeType.USES), file_id, parser),
        ).fetchall()
        return {int(row["file_id"]) for row in rows}

    def use_readers(self, file_ids: Sequence[int]) -> set[int]:
        """Files whose uses read the export table of any of these files (``use_reads``)."""
        found: set[int] = set()
        for chunk in _chunks(sorted(set(file_ids))):
            placeholders = ",".join("?" * len(chunk))
            rows = self._db.execute(
                "SELECT DISTINCT file_id FROM use_reads "  # noqa: S608 - placeholders are generated counts, values are bound
                f"WHERE read_file_id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            found.update(int(row["file_id"]) for row in rows)
        return found

    def file_ids_of_repositories(self, repo_ids: Sequence[int]) -> set[int]:
        """Every indexed file of these repositories."""
        found: set[int] = set()
        for chunk in _chunks(sorted(set(repo_ids))):
            placeholders = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT id FROM files WHERE repo_id IN ({placeholders})",  # noqa: S608 - placeholders are generated counts, values are bound
                tuple(chunk),
            ).fetchall()
            found.update(int(row["id"]) for row in rows)
        return found

    def files_of_repository(self, repo_id: int) -> list[tuple[int, str]]:
        """(id, repository-relative path) of every indexed file of one repository, by path."""
        rows = self._db.execute(
            "SELECT id, rel_path FROM files WHERE repo_id = ? ORDER BY rel_path", (repo_id,)
        ).fetchall()
        return [(int(row["id"]), str(row["rel_path"])) for row in rows]

    def nested_repositories(self, repo_ids: Sequence[int]) -> set[int]:
        """Repositories whose root lies inside the root of any of these repositories."""
        found: set[int] = set()
        for chunk in _chunks(sorted(set(repo_ids))):
            placeholders = ",".join("?" * len(chunk))
            rows = self._db.execute(
                "SELECT n.id AS id FROM repositories o JOIN repositories n "  # noqa: S608 - placeholders are generated counts, values are bound
                "ON substr(n.path, 1, length(o.path) + 1) = o.path || '/' "
                f"WHERE o.id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            found.update(int(row["id"]) for row in rows)
        return found

    def count_use_edges(self, parser: str) -> int:
        """``USES`` edges ``parser`` wrote: one per claim it resolved."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE type = ? AND parser = ?",
            (str(EdgeType.USES), parser),
        ).fetchone()
        return int(row["n"])

    def count_use_claims(self) -> int:
        """Standing ``USES`` claims, export tables aside: every use the global pass resolves."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM unresolved_refs WHERE edge_type = ? AND target_key != ?",
            (str(EdgeType.USES), USES_EXPORT_TABLE),
        ).fetchone()
        return int(row["n"])

    def record_link_pass(self, at: str) -> None:
        """Note when the global pass last committed. Written inside its transaction, so the time
        and the graph it describes are visible together; a declaration stated after it is
        pending, and one stated before it has been joined or has been refused."""
        self._db.set_meta(LINKS_PASS_KEY, at)

    def link_pass_at(self) -> str | None:
        """When the global pass last committed, or ``None`` before the first."""
        return self._db.get_meta(LINKS_PASS_KEY)

    def uses_links_state(self) -> str | None:
        """What the ``USES`` stage last recorded about the edges it keeps (``graph/uses_link``)."""
        return self._db.get_meta(USES_LINKS_KEY)

    def set_uses_links_state(self, state: str) -> None:
        with self._db.transaction():
            self._db.set_meta(USES_LINKS_KEY, state)

    def delete_edges_by_parser(self, parser: str, *, keep: EdgeType | None = None) -> int:
        """Drop every edge a given producer created, except those of type ``keep``.

        The global link pass owns its edges exclusively, so deleting them by
        producer and rebuilding makes the pass idempotent: running it twice gives
        the same graph as running it once, which is what makes an incremental sync
        equal a full reindex (V1_SPEC §12). Its ``USES`` edges are kept and replaced
        per file by a stage of their own (:mod:`repoatlas.graph.uses_link`).
        """
        with self._db.transaction() as conn:
            if keep is None:
                cursor = conn.execute("DELETE FROM edges WHERE parser = ?", (parser,))
            else:
                cursor = conn.execute(
                    "DELETE FROM edges WHERE parser = ? AND type != ?", (parser, str(keep))
                )
        return cursor.rowcount or 0

    def list_file_rows(self, repo_id: int | None = None) -> list[FileRow]:
        """Every indexed file — or one repository's — joined to its repository and entity."""
        sql = """
            SELECT f.id AS file_id, e.id AS file_entity_id, f.repo_id AS repo_id,
                   r.name AS repo_name, r.slug AS repo_slug, r.path AS repo_path,
                   f.rel_path AS rel_path
            FROM files f
            JOIN repositories r ON r.id = f.repo_id
            JOIN entities e ON e.file_id = f.id AND e.type = 'file'
        """
        params: tuple[object, ...] = ()
        if repo_id is not None:
            sql += " WHERE f.repo_id = ?"
            params = (repo_id,)
        rows = self._db.execute(sql, params).fetchall()
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

    def list_symbols(
        self,
        types: Sequence[EntityType] | None = None,
        repo_id: int | None = None,
        *,
        file_id: int | None = None,
    ) -> list[SymbolRow]:
        """File-scoped definitions, for cross-file and cross-repository resolution."""
        sql = """
            SELECT id, file_id, repo_id, type, name, qualified_name, line_start, line_end,
                   meta_json
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
        if repo_id is not None:
            sql += " AND repo_id = ?"
            params = (*params, repo_id)
        if file_id is not None:
            sql += " AND file_id = ?"
            params = (*params, file_id)
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
                line_end=row["line_end"],
                meta_json=row["meta_json"],
            )
            for row in rows
        ]

    def list_served_routes(
        self, *, exclude_parser: str, repo_id: int | None = None
    ) -> dict[int, list[str]]:
        """Per repository, the route bodies its files serve, minus one producer's edges.

        Reached through the route nodes (a key range on ``idx_entities_key``, then
        ``idx_edges_dst``) rather than by scanning every edge. The global pass
        excludes its own previous output, so what it decides never depends on what
        it decided last time.
        """
        rows = self._db.execute(
            """
            SELECT DISTINCT e.repo_id AS repo_id, t.name AS body
            FROM entities t
            JOIN edges e ON e.dst_id = t.id
            WHERE t.key >= ? AND t.key < ?
              AND t.type = ?
              AND e.type = ?
              AND e.repo_id IS NOT NULL
              AND (e.parser IS NULL OR e.parser <> ?)
              AND (? IS NULL OR e.repo_id = ?)
            ORDER BY e.repo_id, t.name
            """,
            (
                ROUTE_KEY_PREFIX,
                _after(ROUTE_KEY_PREFIX),
                str(EntityType.HTTP_ROUTE),
                str(EdgeType.SERVES_HTTP_ROUTE),
                exclude_parser,
                repo_id,
                repo_id,
            ),
        ).fetchall()
        served: dict[int, list[str]] = {}
        for row in rows:
            served.setdefault(row["repo_id"], []).append(row["body"])
        return served

    def list_route_servers(
        self, *, exclude_parser: str, repo_id: int | None = None
    ) -> list[RouteServerRow]:
        """Every per-file edge serving a route: who serves it, in which file, at which lines.

        The same walk as :meth:`list_served_routes`, keeping each edge rather than each
        body. One producer's edges -- the global pass's own -- are left out, so what it
        decides never depends on what it decided last time.
        """
        rows = self._db.execute(
            """
            SELECT e.repo_id AS repo_id, e.file_id AS file_id, e.src_id AS src_id,
                   t.name AS body, e.parser AS parser, e.line_start AS line_start,
                   e.line_end AS line_end
            FROM entities t
            JOIN edges e ON e.dst_id = t.id
            WHERE t.key >= ? AND t.key < ?
              AND t.type = ?
              AND e.type = ?
              AND e.repo_id IS NOT NULL
              AND (e.parser IS NULL OR e.parser <> ?)
              AND (? IS NULL OR e.repo_id = ?)
            ORDER BY e.repo_id, e.file_id, e.src_id, t.name
            """,
            (
                ROUTE_KEY_PREFIX,
                _after(ROUTE_KEY_PREFIX),
                str(EntityType.HTTP_ROUTE),
                str(EdgeType.SERVES_HTTP_ROUTE),
                exclude_parser,
                repo_id,
                repo_id,
            ),
        ).fetchall()
        return [RouteServerRow(**dict(row)) for row in rows]

    def reads_in(self, entity_id: int, repo_id: int) -> bool:
        """Whether anything in one repository reads the environment variable ``entity_id``."""
        row = self._db.execute(
            """
            SELECT 1 FROM edges e JOIN entities s ON s.id = e.src_id
            WHERE e.dst_id = ? AND e.type = ? AND s.repo_id = ?
            LIMIT 1
            """,
            (entity_id, str(EdgeType.READS), repo_id),
        ).fetchone()
        return row is not None

    def list_env_reads(self, *, exclude_parser: str) -> list[EnvReadRow]:
        """Every per-file read of an environment variable: its reader, file and line.

        Reached through the variables' nodes (a key range on ``idx_entities_key``, then
        ``idx_edges_dst``). The global pass's own edges are left out, so what it decides
        never depends on what it decided last time.
        """
        rows = self._db.execute(
            """
            SELECT t.name AS variable, e.src_id AS src_id, s.key AS src_key,
                   e.repo_id AS repo_id, e.file_id AS file_id, f.rel_path AS path,
                   e.line_start AS line_start
            FROM entities t
            JOIN edges e ON e.dst_id = t.id
            JOIN entities s ON s.id = e.src_id
            LEFT JOIN files f ON f.id = e.file_id
            WHERE t.key >= ? AND t.key < ?
              AND t.type = ?
              AND e.type = ?
              AND e.repo_id IS NOT NULL
              AND (e.parser IS NULL OR e.parser <> ?)
            ORDER BY t.name, e.repo_id, f.rel_path, e.line_start, s.key
            """,
            (
                ENV_KEY_PREFIX,
                _after(ENV_KEY_PREFIX),
                str(EntityType.ENV_VAR),
                str(EdgeType.READS),
                exclude_parser,
            ),
        ).fetchall()
        return [EnvReadRow(**dict(row)) for row in rows]

    def replace_http_call_decisions(self, rows: Sequence[HttpCallDecisionRow]) -> None:
        """Replace every stored HTTP call decision, and record that they have been written.

        Called by the global commit inside its transaction, so the rows and the edges
        they account for change together.
        """
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM http_call_decisions")
            conn.executemany(
                """
                INSERT INTO http_call_decisions (
                    decision_ref, repo_id, file_id, src_id, line_start, line_end,
                    method, url, variant, decision, evidence_json, chain_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.decision_ref,
                        row.repo_id,
                        row.file_id,
                        row.src_id,
                        row.line_start,
                        row.line_end,
                        row.method,
                        row.url,
                        row.variant,
                        row.decision,
                        row.evidence_json,
                        row.chain_json,
                    )
                    for row in rows
                ],
            )
            self._db.set_meta(HTTP_CALL_DECISIONS_KEY, "built")

    def list_http_call_decisions(self, repo_id: int | None = None) -> list[HttpCallDecisionRow]:
        """The stored decisions of one repository's requests, or of every repository's,
        in file and line order, with each caller's path and entity key."""
        where = "WHERE d.repo_id = ?" if repo_id is not None else ""
        rows = self._db.execute(
            f"""
            SELECT d.*, f.rel_path AS path, s.key AS src_key
            FROM http_call_decisions d
            JOIN files f ON f.id = d.file_id
            JOIN entities s ON s.id = d.src_id
            {where}
            ORDER BY d.repo_id, f.rel_path, d.line_start, d.decision_ref
            """,  # noqa: S608 - the one optional clause is fixed text, values are bound
            (repo_id,) if repo_id is not None else (),
        ).fetchall()
        return [
            HttpCallDecisionRow(
                decision_ref=row["decision_ref"],
                repo_id=row["repo_id"],
                file_id=row["file_id"],
                src_id=row["src_id"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                method=row["method"],
                url=row["url"],
                variant=row["variant"],
                decision=row["decision"],
                evidence_json=row["evidence_json"],
                chain_json=row["chain_json"],
                path=row["path"],
                src_key=row["src_key"],
            )
            for row in rows
        ]

    def replace_link_decisions(self, rows: Sequence[LinkDecisionRow]) -> None:
        """Replace every stored link decision.

        Called by the global commit inside its transaction, so the decisions and the
        edges they account for change together.
        """
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM link_decisions")
            conn.executemany(
                """
                INSERT INTO link_decisions (
                    decision_ref, family, repo_id, file_id, src_id, line_start, line_end,
                    subject, decision, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.decision_ref,
                        row.family,
                        row.repo_id,
                        row.file_id,
                        row.src_id,
                        row.line_start,
                        row.line_end,
                        row.subject,
                        row.decision,
                        row.evidence_json,
                    )
                    for row in rows
                ],
            )

    def replace_ordinary_outcomes(
        self, rows: Sequence[OutcomeRow], accounting: Mapping[str, int]
    ) -> None:
        """Replace the ordinary-claim account (``ordinary_outcomes``, ``ordinary_accounting``).

        Called by the global commit inside its transaction, so the account and the graph it
        accounts for change together.
        """
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM ordinary_outcomes")
            conn.execute("DELETE FROM ordinary_accounting")
            conn.executemany(
                """
                INSERT INTO ordinary_outcomes (
                    repo_id, language, edge_type, target_kind, outcome, subreason, claims
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.repo_id,
                        row.language,
                        row.edge_type,
                        row.target_kind,
                        row.outcome,
                        row.subreason,
                        row.claims,
                    )
                    for row in rows
                ],
            )
            conn.executemany(
                "INSERT INTO ordinary_accounting (key, value) VALUES (?, ?)",
                sorted(accounting.items()),
            )

    def list_ordinary_outcomes(self) -> list[OutcomeRow]:
        """The stored account, in a fixed order."""
        rows = self._db.execute(
            """
            SELECT repo_id, language, edge_type, target_kind, outcome, subreason, claims
            FROM ordinary_outcomes
            ORDER BY repo_id, language, edge_type, target_kind, outcome, subreason
            """
        ).fetchall()
        return [OutcomeRow(**{key: row[key] for key in row.keys()}) for row in rows]  # noqa: SIM118

    def ordinary_accounting(self) -> dict[str, int]:
        """The stored accounting figures by key."""
        rows = self._db.execute(
            "SELECT key, value FROM ordinary_accounting ORDER BY key"
        ).fetchall()
        return {str(row["key"]): int(row["value"]) for row in rows}

    def count_standing_claims(self) -> tuple[int, int]:
        """Every standing claim, and how many of them are ``USES`` claims."""
        row = self._db.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(edge_type = ?), 0) AS uses FROM unresolved_refs",
            (str(EdgeType.USES),),
        ).fetchone()
        return int(row["total"]), int(row["uses"])

    def link_decisions_near(
        self, entity_keys: Sequence[str]
    ) -> list[tuple[str, str, str, str, str, int | None]]:
        """Every decision other than a link made (``linked``, ``resolved``) decided at one of
        these entities or in a file one of them is in: (family, subject, decision,
        ``<repository>/<path>``, evidence, line), in family and subject order. What the
        change-impact answer lists. The ``ordinary`` family is a report of what is still open
        and is never listed here: impact answers keep exactly the decisions they had."""
        if not entity_keys:
            return []
        entity_ids: set[int] = set()
        file_ids: set[int] = set()
        keys = sorted(set(entity_keys))
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._db.execute(
                f"SELECT id, file_id FROM entities WHERE key IN ({marks})",  # noqa: S608
                chunk,
            ).fetchall():
                entity_ids.add(row[0])
                if row[1] is not None:
                    file_ids.add(row[1])
        found: dict[str, tuple[str, str, str, str, str, int | None]] = {}
        for column, ids in (("src_id", entity_ids), ("file_id", file_ids)):
            for chunk in _chunks(sorted(ids)):
                marks = ",".join("?" * len(chunk))
                sql = (
                    "SELECT d.decision_ref, d.family, d.subject, d.decision, d.evidence_json, "  # noqa: S608 - the column is one of two fixed names
                    "d.line_start, r.slug || '/' || f.rel_path AS place "
                    "FROM link_decisions d JOIN files f ON f.id = d.file_id "
                    "JOIN repositories r ON r.id = f.repo_id "
                    f"WHERE d.decision NOT IN ('linked', 'resolved') AND d.family != ? "
                    f"AND d.{column} IN ({marks})"
                )
                for row in self._db.execute(sql, [ORDINARY_FAMILY, *chunk]).fetchall():
                    found[row["decision_ref"]] = (
                        row["family"],
                        row["subject"],
                        row["decision"],
                        row["place"],
                        row["evidence_json"] or "{}",
                        row["line_start"],
                    )
        return sorted(found.values(), key=lambda d: (d[0], d[1], d[2]))

    def http_decisions_near(self, entity_keys: Sequence[str]) -> list[HttpRefusalNear]:
        """The refused HTTP requests where an impact walk went (DF3c), never a linked one.

        ``at``: a refusal whose caller entity is one of these, or whose file's own entity is --
        reached, never merely a file some reached declaration is in. ``toward``: a refusal whose
        stored ``would_reach`` names one of these as a handler. A refusal may be both; the
        caller decides which is listed. In decision-ref order.
        """
        keys = sorted({key for key in entity_keys if key})
        if not keys:
            return []
        reached: dict[int, str] = {}
        files: set[int] = set()
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._db.execute(
                f"SELECT id, type, file_id FROM entities WHERE key IN ({marks})",  # noqa: S608
                chunk,
            ).fetchall():
                reached[int(row[0])] = str(row[1])
                if row[1] == EntityType.FILE.value and row[2] is not None:
                    files.add(int(row[2]))
        select = (
            "SELECT d.decision_ref, d.decision, d.method, d.url, d.src_id, d.file_id, "
            "d.line_start, d.evidence_json, d.chain_json, r.slug, f.rel_path "
            "FROM http_call_decisions d JOIN files f ON f.id = d.file_id "
            "JOIN repositories r ON r.id = f.repo_id WHERE d.decision <> 'linked' AND "
        )
        found: list[HttpRefusalNear] = []
        seen: set[str] = set()
        for column, ids in (("src_id", sorted(reached)), ("file_id", sorted(files))):
            for chunk in _chunks(ids):
                marks = ",".join("?" * len(chunk))
                for row in self._db.execute(f"{select}d.{column} IN ({marks})", chunk).fetchall():
                    if row["decision_ref"] in seen:
                        continue
                    seen.add(row["decision_ref"])
                    caller = (
                        row["src_id"] in reached and reached[row["src_id"]] != EntityType.FILE.value
                    )
                    found.append(_near(row, "at", reached="caller" if caller else "file"))
        wanted = set(keys)
        for row in self._db.execute(
            f"{select}json_extract(d.evidence_json, '$.would_reach') IS NOT NULL"
        ).fetchall():
            handlers = tuple(
                h
                for h in json.loads(row["evidence_json"] or "{}").get("would_reach") or []
                if isinstance(h, dict) and h.get("handler") in wanted
            )
            if handlers:
                found.append(_near(row, "toward", handlers=handlers))
        return sorted(found, key=lambda n: (n.decision_ref, n.relation))

    def list_deployment_links(self) -> list[DeploymentLink]:
        """Every user's deployment link, of every repository, by id: what the global pass
        offers the HTTP join to fall back on (repoatlas.graph.link_exposures)."""
        rows = self._db.execute(DEPLOYMENT_LINKS_SELECT + "ORDER BY id").fetchall()
        return [deployment_link(row) for row in rows]

    def list_link_decisions(
        self, family: str | None = None, repo_id: int | None = None
    ) -> list[LinkDecisionRow]:
        """Stored link decisions, of one family or all and of one repository or all, in
        repository, file, line and subject order, each with its file's path."""
        clauses: list[str] = []
        params: list[object] = []
        if family is not None:
            clauses.append("d.family = ?")
            params.append(family)
        if repo_id is not None:
            clauses.append("d.repo_id = ?")
            params.append(repo_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.execute(
            f"""
            SELECT d.*, f.rel_path AS path
            FROM link_decisions d JOIN files f ON f.id = d.file_id
            {where}
            ORDER BY d.repo_id, f.rel_path, d.line_start, d.subject, d.decision_ref
            """,  # noqa: S608 - the clauses are fixed text, values are bound
            params,
        ).fetchall()
        return [
            LinkDecisionRow(
                decision_ref=row["decision_ref"],
                family=row["family"],
                repo_id=row["repo_id"],
                file_id=row["file_id"],
                src_id=row["src_id"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                subject=row["subject"],
                decision=row["decision"],
                evidence_json=row["evidence_json"],
                path=row["path"],
            )
            for row in rows
        ]

    def http_call_decision_repository(self, decision_ref: str) -> int | None:
        """The repository whose request a stored decision is, by its exact ref."""
        row = self._db.execute(
            "SELECT repo_id FROM http_call_decisions WHERE decision_ref = ?", (decision_ref,)
        ).fetchone()
        return None if row is None else int(row["repo_id"])

    def http_call_decisions_built(self) -> bool:
        """Whether a global commit has written ``http_call_decisions`` since the table existed."""
        return self._db.get_meta(HTTP_CALL_DECISIONS_KEY) is not None

    def list_http_edges(
        self, repo_id: int, types: Sequence[EdgeType] | None = None
    ) -> list[HttpEdgeRow]:
        """Every edge one repository asserts to a shared HTTP route or endpoint.

        Reached through the HTTP nodes (a key range on ``idx_entities_key``, then
        ``idx_edges_dst``) and narrowed to the asserting repository, which is the
        edge's own ``repo_id``: the other repositories meeting on the same node are
        not this repository's facts. ``types`` defaults to the call and serve edges.
        """
        chosen = (
            types
            if types is not None
            else (
                EdgeType.CALLS_HTTP_ROUTE,
                EdgeType.SERVES_HTTP_ROUTE,
                EdgeType.CALLS_HTTP_ENDPOINT,
            )
        )
        if not chosen:
            return []
        marks = ", ".join("?" for _ in chosen)
        rows = self._db.execute(
            f"""
            SELECT e.type, e.parser, e.src_id, s.key AS src_key, s.name AS src_name,
                   s.qualified_name AS src_qualified_name, t.key AS target_key,
                   t.name AS target_name, f.rel_path AS path, e.line_start, e.line_end
            FROM entities t
            JOIN edges e ON e.dst_id = t.id
            JOIN entities s ON s.id = e.src_id
            LEFT JOIN files f ON f.id = e.file_id
            WHERE t.key >= ? AND t.key < ?
              AND e.repo_id = ?
              AND e.type IN ({marks})
            ORDER BY f.rel_path, e.line_start, t.key, s.key
            """,  # noqa: S608 - placeholders are a generated count, values are bound
            (
                _HTTP_KEY_PREFIX,
                _after(_HTTP_KEY_PREFIX),
                repo_id,
                *(str(edge_type) for edge_type in chosen),
            ),
        ).fetchall()
        return [
            HttpEdgeRow(
                type=EdgeType(row["type"]),
                parser=row["parser"],
                src_id=row["src_id"],
                src_key=row["src_key"],
                src_name=row["src_name"],
                src_qualified_name=row["src_qualified_name"],
                target_key=row["target_key"],
                target_name=row["target_name"],
                path=row["path"],
                line_start=row["line_start"],
                line_end=row["line_end"],
            )
            for row in rows
        ]

    def list_file_parsers(self, repo_id: int) -> list[tuple[str, str | None]]:
        """``(path, parser)`` for every indexed file of one repository."""
        rows = self._db.execute(
            "SELECT rel_path, parser FROM files WHERE repo_id = ? ORDER BY rel_path", (repo_id,)
        ).fetchall()
        return [(row["rel_path"], row["parser"]) for row in rows]

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

    def count_keys_with_prefix(
        self,
        prefix: str,
        *,
        exclude_types: Sequence[EntityType] = (),
        exclude_kinds: Sequence[str] = (),
    ) -> int:
        """How many entities have a natural key beginning with ``prefix``.

        A key names what it is part of -- ``sym:<repo>:<path>:<qualified>:<line>`` and
        ``file:<repo>:<path>`` -- so a prefix counts a file's declarations, a
        declaration's members or a directory's files exactly. A range on the key
        index rather than ``LIKE``, which would scan and fold case.
        """
        params: list[object] = [prefix, prefix + "\U0010ffff"]
        excluded = ""
        if exclude_types:
            excluded = f" AND type NOT IN ({','.join('?' * len(exclude_types))})"
            params.extend(str(t) for t in exclude_types)
        if exclude_kinds:
            excluded += (
                " AND NOT (type = 'symbol' AND COALESCE(json_extract(meta_json, '$.kind'), '')"
                f" IN ({','.join('?' * len(exclude_kinds))}))"
            )
            params.extend(exclude_kinds)
        row = self._db.execute(
            f"SELECT COUNT(*) FROM entities WHERE key >= ? AND key < ?{excluded}",  # noqa: S608
            tuple(params),
        ).fetchone()
        return int(row[0])

    def entities_with_key_prefix(
        self,
        prefix: str,
        *,
        exclude_types: Sequence[EntityType] = (),
        exclude_kinds: Sequence[str] = (),
        limit: int,
    ) -> list[EntityIdentity]:
        """At most ``limit`` entities whose natural key begins with ``prefix``, by key."""
        params: list[object] = [prefix, prefix + "\U0010ffff"]
        excluded = ""
        if exclude_types:
            excluded = f" AND e.type NOT IN ({','.join('?' * len(exclude_types))})"
            params.extend(str(t) for t in exclude_types)
        if exclude_kinds:
            excluded += (
                " AND NOT (e.type = 'symbol' AND COALESCE(json_extract(e.meta_json, '$.kind'), '')"
                f" IN ({','.join('?' * len(exclude_kinds))}))"
            )
            params.extend(exclude_kinds)
        params.append(limit)
        rows = self._db.execute(
            f"""{_ENTITY_IDENTITY_SELECT}
            WHERE e.key >= ? AND e.key < ?{excluded}
            ORDER BY e.key, e.id
            LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [_entity_identity(row) for row in rows]

    # -- bounded neighbour retrieval (the change-impact walk) -----------------
    #
    # A hub's size must not decide how much work one question does. So nothing here
    # fetches "every neighbour, then cuts": a count is a COUNT, a page is a keyset
    # page, a distribution is grouped and bounded, and representatives are fetched
    # by name. Admission and confinement are part of every query's WHERE clause, so
    # what is counted, paged or grouped is what the walk may follow, and a limit
    # never falls on edges the walk would have discarded.

    def _impact_scope(
        self, rule: NeighborRule, confinement: Confinement, near_ids: Sequence[int]
    ) -> tuple[str, str, str, list[object]]:
        """The near and far columns, and the FROM/WHERE one rule's admitted edges share."""
        near, far = ("src", "dst") if rule.outgoing else ("dst", "src")
        kinds = sorted(str(t) for t in rule.edge_types)
        params: list[object] = [*near_ids, *kinds]
        constrained = ""
        for edge_type, entity_types in rule.far_types:
            constrained += (
                f" AND (e.type != ? OR {far}.type IN ({','.join('?' * len(entity_types))}))"
            )
            params.extend([str(edge_type), *(str(t) for t in entity_types)])
        keys_by_type: dict[EdgeType, list[str]] = {}
        for edge_type, key in rule.evidence:
            # Only the edges one pass made on its own evidence (repoatlas.graph.impact,
            # Family.evidence). The key is a constant of the rule, never input.
            if not _EVIDENCE_KEY.fullmatch(key):
                raise ValueError(f"not an evidence key: {key!r}")
            keys_by_type.setdefault(edge_type, []).append(key)
        for edge_type, keys in keys_by_type.items():
            # Any one of the type's evidence keys admits an edge of it.
            recorded = " OR ".join(
                f"json_extract(e.resolution, '$.{key}') IS NOT NULL" for key in keys
            )
            constrained += f" AND (e.type != ? OR {recorded})"
            params.append(str(edge_type))
        if rule.uses_replace_file_facts:
            # The far end is a file asserting a file-level import or call; its own USES,
            # or a declaration's in it, to the same entity replaces that fact.
            constrained += f"""
              AND NOT (e.type IN ('REFERENCES', 'CALLS') AND {far}.type = 'file' AND EXISTS (
                  SELECT 1 FROM edges u JOIN entities ue ON ue.id = u.src_id
                  WHERE u.type = 'USES' AND u.dst_id = e.{near}_id
                    AND ue.file_id = {far}.file_id))"""  # noqa: S608 - column names only
        # Which edges may join two repositories: a crossing type, or an evidenced type
        # whose own resolution records the evidence (EVIDENCE_PATHS): a package link's
        # (repoatlas.graph.packages), an HTTP join's (repoatlas.graph.http_crossings).
        crossed = sorted(str(t) for t in confinement.crossing)
        evidenced = sorted(
            (str(t), EVIDENCE_PATHS[t]) for t in confinement.evidenced if t in EVIDENCE_PATHS
        )
        admitted: list[str] = []
        admitted_params: list[object] = []
        if crossed:
            admitted.append(f"e.type IN ({','.join('?' * len(crossed))})")
            admitted_params.extend(crossed)
        for edge_type, path in evidenced:
            admitted.append(f"(e.type = ? AND json_extract(e.resolution, '{path}') IS NOT NULL)")
            admitted_params.append(edge_type)
        may_cross = " OR ".join(admitted)
        crossing = ""
        if may_cross:
            crossing = f" OR {may_cross}"
            params.extend(admitted_params)
        home = ""
        if confinement.home_repo_id is not None:
            moves = ""
            if may_cross and confinement.crossing_moves_home:
                moves = (
                    f" OR (({may_cross})"
                    " AND src.repo_id IS NOT NULL AND dst.repo_id IS NOT NULL"
                    " AND src.repo_id != dst.repo_id)"
                )
            home = f" AND ({far}.repo_id IS NULL OR {far}.repo_id = ?{moves})"
            params.append(confinement.home_repo_id)
            if moves:
                params.extend(admitted_params)
        scope = ""
        if confinement.scope_repo_ids is not None:
            allowed = sorted(confinement.scope_repo_ids)
            if allowed:
                placeholders = ",".join("?" * len(allowed))
                scope = f" AND ({far}.repo_id IS NULL OR {far}.repo_id IN ({placeholders}))"
                params.extend(allowed)
            else:
                scope = f" AND {far}.repo_id IS NULL"
        where = f"""
            FROM edges e
            JOIN entities src ON src.id = e.src_id
            JOIN entities dst ON dst.id = e.dst_id
            LEFT JOIN files af ON af.id = e.file_id
            LEFT JOIN files ff ON ff.id = {far}.file_id
            LEFT JOIN repositories fr ON fr.id = {far}.repo_id
            WHERE e.{near}_id IN ({",".join("?" * len(near_ids))})
              AND e.type IN ({",".join("?" * len(kinds))}){constrained}
              AND e.src_id != e.dst_id
              AND (src.repo_id IS NULL OR dst.repo_id IS NULL
                   OR src.repo_id = dst.repo_id{crossing}){home}{scope}
        """
        return near, far, where, params

    @staticmethod
    def _impact_columns(near: str, far: str) -> str:
        return f"""
            e.id AS edge_id, e.type AS type, e.{near}_id AS near_id, e.{far}_id AS far_id,
            {far}.key AS far_key, {far}.type AS far_type, {far}.name AS far_name,
            {far}.qualified_name AS far_qualified_name, {far}.repo_id AS far_repo_id,
            ff.rel_path AS far_path,
            {far}.line_start AS far_line_start, {far}.line_end AS far_line_end,
            e.file_id AS file_id, af.repo_id AS file_repo_id, af.rel_path AS file_path,
            e.line_start AS line_start,
            e.line_end AS line_end, e.parser AS parser, e.resolution AS resolution
        """

    #: The natural order of a bounded query: facts about the repositories, so the same
    #: question over the same repositories pages identically after any rebuild. The
    #: row id breaks ties only between copies of one relationship.
    _IMPACT_ORDER = (
        "e.type, {far}.key, COALESCE(af.rel_path, ''), COALESCE(e.line_start, 0), "
        "COALESCE(e.line_end, 0), e.id"
    )

    @staticmethod
    def _impact_row(row: sqlite3.Row, rule: NeighborRule) -> NeighborRow:
        return NeighborRow(
            edge_id=row["edge_id"],
            type=EdgeType(row["type"]),
            near_id=row["near_id"],
            far_id=row["far_id"],
            outgoing=rule.outgoing,
            far_key=row["far_key"],
            far_type=EntityType(row["far_type"]),
            far_name=row["far_name"],
            far_qualified_name=row["far_qualified_name"],
            far_repo_id=row["far_repo_id"],
            far_path=row["far_path"],
            far_line_start=row["far_line_start"],
            far_line_end=row["far_line_end"],
            file_id=row["file_id"],
            file_repo_id=row["file_repo_id"],
            file_path=row["file_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            parser=row["parser"],
            resolution=row["resolution"],
        )

    def impact_counts(
        self, near_ids: Sequence[int], rule: NeighborRule, confinement: Confinement
    ) -> dict[int, NeighborCount]:
        """Exactly how many far entities and relationships each node offers; one query."""
        if not near_ids:
            return {}
        near, far, where, params = self._impact_scope(rule, confinement, near_ids)
        rows = self._db.execute(
            f"""
            SELECT near_id, COUNT(DISTINCT far_id) AS far, COUNT(*) AS relationships FROM (
                SELECT DISTINCT e.{near}_id AS near_id, e.{far}_id AS far_id, e.type, e.file_id,
                       e.line_start, e.line_end
                {where}
            ) GROUP BY near_id
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()
        return {
            row["near_id"]: NeighborCount(
                near_id=row["near_id"], far=row["far"], relationships=row["relationships"]
            )
            for row in rows
        }

    def impact_page(
        self,
        near_id: int,
        rule: NeighborRule,
        confinement: Confinement,
        *,
        after: NeighborCursor | None,
        limit: int,
    ) -> list[NeighborRow]:
        """At most ``limit`` admitted relationships, strictly after ``after``, in natural order."""
        if limit <= 0:
            return []
        near, far, where, params = self._impact_scope(rule, confinement, [near_id])
        keyset = ""
        if after is not None:
            keyset = (
                f" AND (e.type, {far}.key, COALESCE(af.rel_path, ''), COALESCE(e.line_start, 0),"
                " COALESCE(e.line_end, 0), e.id) > (?, ?, ?, ?, ?, ?)"
            )
            params.extend(
                [
                    str(after.type),
                    after.far_key,
                    after.file_path,
                    after.line_start,
                    after.line_end,
                    after.edge_id,
                ]
            )
        params.append(limit)
        rows = self._db.execute(
            f"""
            SELECT {self._impact_columns(near, far)}
            {where}{keyset}
            ORDER BY {self._IMPACT_ORDER.format(far=far)}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [self._impact_row(row, rule) for row in rows]

    def impact_distribution(
        self, near_id: int, rule: NeighborRule, confinement: Confinement, *, max_groups: int
    ) -> tuple[list[NeighborGroup], bool]:
        """Far entities per far file, largest first, at most ``max_groups`` of them.

        Repositories take turns: every repository's largest file comes before any
        repository's second, so one repository with many files cannot fill the list
        while another's dependents go unseen. Within one repository the order is simply
        largest first. Each group also carries its repository's exact total, counted
        over every group rather than those that fit.

        The second value says the groups did not all fit: the list is then partial
        and says so, rather than a count that looks complete.
        """
        _, far, where, params = self._impact_scope(rule, confinement, [near_id])
        params.append(max_groups + 1)
        rows = self._db.execute(
            f"""
            SELECT repo_id, path, far, repository_far FROM (
                SELECT g.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(g.repo_id, 0)
                           ORDER BY g.far DESC, COALESCE(g.path, '')
                       ) AS turn,
                       SUM(g.far) OVER (PARTITION BY COALESCE(g.repo_id, 0)) AS repository_far
                FROM (
                    SELECT {far}.repo_id AS repo_id, ff.rel_path AS path,
                           COALESCE(MIN(fr.slug), '') AS slug,
                           COUNT(DISTINCT e.{far}_id) AS far
                    {where}
                    GROUP BY {far}.repo_id, ff.rel_path
                ) g
            )
            ORDER BY turn, far DESC, slug, COALESCE(path, '')
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()
        groups = [
            NeighborGroup(
                repo_id=row["repo_id"],
                path=row["path"],
                far=row["far"],
                repository_far=row["repository_far"],
            )
            for row in rows[:max_groups]
        ]
        return groups, len(rows) > max_groups

    def impact_rows_for(
        self,
        near_id: int,
        rule: NeighborRule,
        confinement: Confinement,
        *,
        groups: Sequence[tuple[int | None, str | None]],
        per_group: int,
    ) -> list[NeighborRow]:
        """The first ``per_group`` relationships, in natural order, reaching each named file."""
        if not groups or per_group <= 0:
            return []
        near, far, where, params = self._impact_scope(rule, confinement, [near_id])
        values = ",".join("(?, ?)" for _ in groups)
        for repo_id, path in groups:
            params.extend([repo_id if repo_id is not None else 0, path or ""])
        params.append(per_group)
        rows = self._db.execute(
            f"""
            SELECT * FROM (
                SELECT {self._impact_columns(near, far)},
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE({far}.repo_id, 0), COALESCE(ff.rel_path, '')
                           ORDER BY {self._IMPACT_ORDER.format(far=far)}
                       ) AS rank
                {where}
                  AND (COALESCE({far}.repo_id, 0), COALESCE(ff.rel_path, '')) IN (VALUES {values})
            )
            WHERE rank <= ?
            ORDER BY type, far_key, COALESCE(file_path, ''), COALESCE(line_start, 0),
                     COALESCE(line_end, 0), edge_id
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()
        return [self._impact_row(row, rule) for row in rows]

    def count_whole_module_uses(
        self,
        package: str,
        provider_repo_id: int,
        dependent_repo_id: int,
        *,
        exclude_ids: Sequence[int] = (),
    ) -> int:
        """Declarations of one repository using a package's module as a whole, counted.

        The uses a package link could only resolve to a file (a computed member, the
        namespace passed as a value): consumers of the package that name no declaration
        of it. ``exclude_ids`` are left out -- declarations already exact consumers.
        """
        excluded = ""
        params: list[object] = [provider_repo_id, dependent_repo_id, package]
        if exclude_ids:
            excluded = f" AND e.src_id NOT IN ({','.join('?' * len(exclude_ids))})"
            params.extend(exclude_ids)
        row = self._db.execute(
            f"""
            SELECT COUNT(DISTINCT e.src_id)
            FROM edges e
            JOIN entities d ON d.id = e.dst_id
            JOIN entities s ON s.id = e.src_id
            WHERE e.type = 'USES' AND d.type = 'file' AND d.repo_id = ? AND s.repo_id = ?
              AND json_extract(e.resolution, '$.crossing.package') = ?{excluded}
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchone()
        return int(row[0])

    def describe_entities(
        self, entity_ids: Sequence[int], *, path_prefix: str | None = None
    ) -> list[EntityDescription]:
        """Resolve entity ids to their provenance, for rendering as evidence.

        Only file-scoped entities can be cited: an interned entity such as an
        environment variable has no location of its own, so it is skipped rather
        than given an invented one. Under a ``path_prefix``, so is every entity
        declared outside it.
        """
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        params: list[object] = list(entity_ids)
        scope = path_scope("f", path_prefix, params)
        rows = self._db.execute(
            f"""
            SELECT e.id, e.type, e.name, e.qualified_name, e.line_start, e.line_end,
                   e.signature, f.rel_path AS path, r.name AS repository,
                   r.slug AS repository_slug
            FROM entities e
            JOIN files        f ON f.id = e.file_id
            JOIN repositories r ON r.id = e.repo_id
            WHERE e.id IN ({placeholders}){scope}
            """,  # noqa: S608 - placeholders are a generated count, values are bound
            tuple(params),
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
                entity_type=EntityType(row["type"]),
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

    def entity_kinds(self, entity_ids: Sequence[int]) -> dict[int, str]:
        """Each entity's recorded ``kind`` (``meta["kind"]``), for those that record one."""
        if not entity_ids:
            return {}
        placeholders = ",".join("?" * len(entity_ids))
        rows = self._db.execute(
            f"""
            SELECT id, json_extract(meta_json, '$.kind') FROM entities
            WHERE id IN ({placeholders}) AND json_extract(meta_json, '$.kind') IS NOT NULL
            """,  # noqa: S608 - placeholders only
            tuple(entity_ids),
        ).fetchall()
        return {int(row[0]): str(row[1]) for row in rows}

    def file_locations(self, file_ids: Sequence[int]) -> dict[int, tuple[str, str]]:
        """Each file id's repository slug and path, in one query.

        What an edge's provenance names when no source text is being supplied: the
        asserting file's own row, never the edge's source entity. Reading the
        whole file's chunks would answer the same question at the cost of every
        line in it.
        """
        if not file_ids:
            return {}
        placeholders = ",".join("?" * len(file_ids))
        rows = self._db.execute(
            f"""
            SELECT f.id, r.slug, f.rel_path
            FROM files f
            JOIN repositories r ON r.id = f.repo_id
            WHERE f.id IN ({placeholders})
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(file_ids),
        ).fetchall()
        return {int(row["id"]): (row["slug"] or "", row["rel_path"]) for row in rows}

    def provided_manifests(self, repository_slugs: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """Each repository's manifests that provide a package, by path, in one query.

        The facts a walk derives its units from (:mod:`repoatlas.graph.units`): a
        ``provided`` row is a manifest publishing a name, recorded by the
        dependency parsers. A repository with none is absent from the result.
        """
        if not repository_slugs:
            return {}
        placeholders = ",".join("?" * len(repository_slugs))
        rows = self._db.execute(
            f"""
            SELECT DISTINCT r.slug, f.rel_path
            FROM dependencies d
            JOIN files f ON f.id = d.file_id
            JOIN repositories r ON r.id = d.repo_id
            WHERE d.role = 'provided' AND r.slug IN ({placeholders})
            ORDER BY r.slug, f.rel_path
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(repository_slugs),
        ).fetchall()
        found: dict[str, list[str]] = {}
        for row in rows:
            found.setdefault(row["slug"], []).append(row["rel_path"])
        return {slug: tuple(paths) for slug, paths in found.items()}

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

    def package_rows_for_file(
        self, file_id: int, ecosystem: str
    ) -> tuple[tuple[str, str, str, str, int], ...]:
        """One file's declared and provided rows of one ecosystem, as package links read them.

        (role, normalized name, name, constraint, line), sorted: what a declaration or a
        provision contributes to a link, including where it is written.
        """
        rows = self._db.execute(
            """
            SELECT role, normalized_name, name, COALESCE(constraint_text, '') AS constraint_text,
                   line_start
            FROM dependencies
            WHERE file_id = ? AND ecosystem = ? AND role IN ('declared', 'provided')
            """,
            (file_id, ecosystem),
        ).fetchall()
        return tuple(
            sorted(
                (
                    str(row["role"]),
                    str(row["normalized_name"]),
                    str(row["name"]),
                    str(row["constraint_text"]),
                    int(row["line_start"]),
                )
                for row in rows
            )
        )

    def names_provided_by(self, repo_ids: Sequence[int], ecosystem: str) -> set[str]:
        """Normalized names of the packages these repositories provide."""
        found: set[str] = set()
        for chunk in _chunks(sorted(set(repo_ids))):
            placeholders = ",".join("?" * len(chunk))
            rows = self._db.execute(
                "SELECT DISTINCT normalized_name FROM dependencies "  # noqa: S608 - placeholders are generated counts, values are bound
                f"WHERE role = 'provided' AND ecosystem = ? AND repo_id IN ({placeholders})",
                (ecosystem, *chunk),
            ).fetchall()
            found.update(str(row["normalized_name"]) for row in rows)
        return found

    def repositories_declaring(self, names: Sequence[str], ecosystem: str) -> set[int]:
        """Repositories with a manifest declaring any of these packages."""
        found: set[int] = set()
        ordered = sorted(set(names))
        for start in range(0, len(ordered), _CHUNK):
            chunk = ordered[start : start + _CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._db.execute(
                "SELECT DISTINCT repo_id FROM dependencies "  # noqa: S608 - placeholders are generated counts, values are bound
                "WHERE role = 'declared' AND ecosystem = ? "
                f"AND normalized_name IN ({placeholders})",
                (ecosystem, *chunk),
            ).fetchall()
            found.update(int(row["repo_id"]) for row in rows)
        return found

    def repositories_requiring(self, keys: Sequence[str]) -> set[int]:
        """Repositories whose root ``requirements.txt`` declares a PyPI name with any of these
        legacy keys (:func:`repoatlas.graph.py_packages.legacy_key`): ASCII lowercase, ``_``
        for ``-`` -- which SQLite's own ``lower`` and ``replace`` compute exactly."""
        found: set[int] = set()
        ordered = sorted(set(keys))
        for start in range(0, len(ordered), _CHUNK):
            chunk = ordered[start : start + _CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._db.execute(
                "SELECT DISTINCT d.repo_id FROM dependencies d JOIN files f ON f.id = d.file_id "  # noqa: S608 - placeholders are generated counts, values are bound
                "WHERE d.role = 'declared' AND d.ecosystem = 'pypi' "
                "AND f.rel_path = 'requirements.txt' "
                f"AND replace(lower(d.name), '_', '-') IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            found.update(int(row["repo_id"]) for row in rows)
        return found

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


def _near(
    row: sqlite3.Row,
    relation: str,
    *,
    reached: str = "",
    handlers: tuple[dict[str, object], ...] = (),
) -> HttpRefusalNear:
    return HttpRefusalNear(
        decision_ref=row["decision_ref"],
        decision=row["decision"],
        method=row["method"],
        url=row["url"],
        repository=row["slug"],
        path=row["rel_path"],
        line=row["line_start"],
        relation=relation,
        reached=reached,
        handlers=handlers,
        chain_json=row["chain_json"] or "[]",
    )
