"""``inclusion_declarations``: which indexed repository a user says an inclusion is.

Written only by a person (the API, an import), never by a sync. The global pass reads it as a
fallback for an inclusion its path and remotes do not settle (repoatlas.graph.inclusions), and
it is keyed by slugs and paths so a repository removed and indexed again finds its declarations
where it left them (``009_repository_inclusions.sql``).

One declaration per entry -- the including repository, the file stating the entry and the
entry's path. Declaring the repository already declared returns the declaration and writes
nothing; declaring another replaces it. An import never replaces a declaration: it adds the
ones not held, skips those held as stated, and refuses all of them for one that differs
(``import_all``). Every change is also one row of ``inclusion_declaration_audit``, written in
the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from repoatlas.models.deployment_targets import DeclarationProvenance
from repoatlas.models.inclusions import (
    MAX_INCLUSION_DECLARATIONS,
    InclusionDeclaration,
    InclusionDeclarationError,
    InclusionDeclarationFields,
    imported_declarations,
)
from repoatlas.storage.sqlite.database import Database

_SELECT = (
    "SELECT id, repository, file, path, target, created_at, updated_at FROM inclusion_declarations "
)


def _declaration(row: sqlite3.Row) -> InclusionDeclaration:
    return InclusionDeclaration(
        id=row["id"],
        repository=row["repository"],
        file=row["file"],
        path=row["path"],
        target=row["target"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _fields(declaration: InclusionDeclaration) -> InclusionDeclarationFields:
    return InclusionDeclarationFields(
        repository=declaration.repository,
        file=declaration.file,
        path=declaration.path,
        target=declaration.target,
    )


class SqliteInclusionDeclarationStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list_all(self) -> list[InclusionDeclaration]:
        """Every declaration, by id."""
        return [_declaration(row) for row in self._db.execute(_SELECT + "ORDER BY id").fetchall()]

    def get(self, declaration_id: int) -> InclusionDeclaration | None:
        row = self._db.execute(_SELECT + "WHERE id = ?", (declaration_id,)).fetchone()
        return _declaration(row) if row is not None else None

    def create(
        self,
        fields: InclusionDeclarationFields,
        *,
        provenance: DeclarationProvenance | None = None,
        via: str | None = None,
    ) -> InclusionDeclaration:
        """Store a declaration, replacing the one the same entry already has. ``via`` is
        ``import`` or ``cli`` for those, and otherwise taken from the ``provenance``."""
        now = datetime.now(timezone.utc).isoformat()
        source = via or (provenance.via if provenance is not None else "api")
        with self._db.transaction() as conn:
            row = conn.execute(
                _SELECT + "WHERE repository = ? AND file = ? AND path = ?",
                (fields.repository, fields.file, fields.path),
            ).fetchone()
            if row is not None:
                existing = _declaration(row)
                if existing.target == fields.target:
                    return existing
                conn.execute(
                    "UPDATE inclusion_declarations SET target = ?, updated_at = ? WHERE id = ?",
                    (fields.target, now, existing.id),
                )
                self._audit(
                    conn, now, "update", existing.id, source, provenance, _fields(existing), fields
                )
                declaration_id = existing.id
            else:
                (count,) = conn.execute("SELECT COUNT(*) FROM inclusion_declarations").fetchone()
                if count >= MAX_INCLUSION_DECLARATIONS:
                    raise InclusionDeclarationError(
                        f"at most {MAX_INCLUSION_DECLARATIONS} inclusion declarations are kept"
                    )
                cursor = conn.execute(
                    "INSERT INTO inclusion_declarations (repository, file, path, target, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (fields.repository, fields.file, fields.path, fields.target, now, now),
                )
                declaration_id = int(cursor.lastrowid or 0)
                action = "import" if source == "import" else "create"
                self._audit(conn, now, action, declaration_id, source, provenance, None, fields)
        created = self.get(declaration_id)
        assert created is not None
        return created

    def import_all(self, stated: Sequence[InclusionDeclarationFields]) -> int:
        """Store the declarations of an import the index does not hold yet, all or none, and
        return how many were new. One already held with the same target is skipped; any other
        outcome of :func:`imported_declarations` raises and stores nothing."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            held = {
                (row["repository"], row["file"], row["path"]): row["target"]
                for row in conn.execute(_SELECT + "ORDER BY id").fetchall()
            }
            new = imported_declarations(held, stated)
            for fields in new:
                cursor = conn.execute(
                    "INSERT INTO inclusion_declarations (repository, file, path, target, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (fields.repository, fields.file, fields.path, fields.target, now, now),
                )
                self._audit(
                    conn, now, "import", int(cursor.lastrowid or 0), "import", None, None, fields
                )
        return len(new)

    def delete(
        self, declaration_id: int, *, provenance: DeclarationProvenance | None = None
    ) -> bool:
        """Revoke a declaration. What it stated stays in the history."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.transaction() as conn:
            before = self.get(declaration_id)
            if before is None:
                return False
            cursor = conn.execute(
                "DELETE FROM inclusion_declarations WHERE id = ?", (declaration_id,)
            )
            self._audit(
                conn,
                now,
                "revoke",
                declaration_id,
                provenance.via if provenance is not None else "api",
                provenance,
                _fields(before),
                None,
            )
            return cursor.rowcount > 0

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        now: str,
        action: str,
        declaration_id: int,
        via: str,
        provenance: DeclarationProvenance | None,
        before: InclusionDeclarationFields | None,
        after: InclusionDeclarationFields | None,
    ) -> None:
        stated = after if after is not None else before
        assert stated is not None
        conn.execute(
            "INSERT INTO inclusion_declaration_audit (at, action, subject_id, repository, via, "
            "note, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                action,
                declaration_id,
                stated.repository,
                via,
                provenance.note if provenance is not None else None,
                json.dumps(before.model_dump()) if before is not None else None,
                json.dumps(after.model_dump()) if after is not None else None,
            ),
        )
